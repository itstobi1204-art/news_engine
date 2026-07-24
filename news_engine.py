import os
import time
import json
import html
import requests
import trafilatura
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

# Environment Variables
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STATE_FILE = "state.json"

# Timing constants
MAX_AGE_SECONDS = 2 * 3600  # 2 hours buffer required for Finnhub indexing delays
LOOP_DURATION_SECONDS = 4 * 3600 + 55 * 60  # 4 hours 55 minutes
POLL_INTERVAL_SECONDS = 5  # 3 categories x 12 polls/min = 36 Finnhub calls/min, still under the 60/min ceiling
IST_OFFSET = timedelta(hours=5, minutes=30)

# gemini-1.5-flash was shut down by Google (404s on every call).
# gemini-2.5-flash-lite is built for high-volume classification tasks like this one,
# with a meaningfully higher free-tier throughput ceiling than full gemini-2.5-flash.
GEMINI_MODEL = "gemini-2.5-flash-lite"
GEMINI_RPM_LIMIT = 12  # stay a little under the free-tier per-minute ceiling as safety margin
_gemini_call_times = []


def _gemini_rate_limit_wait():
    """Proactively self-throttle so we approach the RPM ceiling but rarely hit a 429."""
    now = time.time()
    # drop timestamps older than 60s
    while _gemini_call_times and now - _gemini_call_times[0] > 60:
        _gemini_call_times.pop(0)
    if len(_gemini_call_times) >= GEMINI_RPM_LIMIT:
        wait_for = 60 - (now - _gemini_call_times[0]) + 0.5
        if wait_for > 0:
            print(f"Gemini RPM budget reached ({GEMINI_RPM_LIMIT}/min) - pausing {wait_for:.1f}s to stay under the limit.")
            time.sleep(wait_for)
    _gemini_call_times.append(time.time())

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                return {"sent_ids": data.get("sent_ids", [])}
        except Exception as e:
            print(f"Error loading state file: {e}")
    return {"sent_ids": []}


def save_state(state):
    try:
        state["sent_ids"] = state["sent_ids"][-1000:]
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"Error saving state file: {e}")


def scrape_article_details(article_url, browser=None):
    cover_image = None
    body_text = ""
    if not article_url or article_url == "N/A":
        return cover_image, body_text

    # Tier 1: fast static fetch + trafilatura (proper main-content extraction,
    # strips ads/nav/related-articles junk far better than raw <p> joining)
    try:
        res = requests.get(article_url, headers=HEADERS, timeout=6)
        if res.status_code == 200:
            raw_html = res.text
            soup = BeautifulSoup(raw_html, "html.parser")
            og_img = (
                soup.find("meta", property="og:image")
                or soup.find("meta", attrs={"name": "twitter:image"})
                or soup.find("meta", property="twitter:image")
            )
            if og_img and og_img.get("content"):
                cover_image = og_img["content"]

            extracted = trafilatura.extract(raw_html, include_comments=False, include_tables=False)
            if extracted:
                body_text = extracted.strip()
        else:
            print(f"Scrape notice: {article_url} returned status {res.status_code}")
    except Exception as e:
        print(f"Scraper notice for {article_url}: {e}")

    # Tier 2: static extraction came up thin - likely a JS-rendered site.
    # Render it in a real (headless) browser and try extraction again.
    if len(body_text) < 200 and browser is not None:
        page = None
        try:
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.set_default_timeout(15000)
            page.goto(article_url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(800)  # let JS finish painting the article body
            rendered_html = page.content()

            extracted = trafilatura.extract(rendered_html, include_comments=False, include_tables=False)
            if extracted and len(extracted) > len(body_text):
                body_text = extracted.strip()

            if not cover_image:
                soup2 = BeautifulSoup(rendered_html, "html.parser")
                og_img2 = soup2.find("meta", property="og:image")
                if og_img2 and og_img2.get("content"):
                    cover_image = og_img2["content"]
        except Exception as e:
            print(f"Headless render fallback failed for {article_url}: {e}")
        finally:
            if page:
                page.close()

    return cover_image, body_text


def analyze_with_ai(headline, summary, body_text):
    if not GEMINI_KEY:
        print("GEMINI_API_KEY not set - using fallback analysis (no AI summary).")
        return {
            "is_relevant": True,
            "impact_emoji": "🔴",
            "market_symbol": "USD",
            "bullet_1": headline,
            "bullet_2": summary[:200] if summary else "No further details available."
        }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    prompt = f"""
    You are an elite market analyst AI screening headlines for an ACTIVE DAY TRADER'S alert feed.
    The trader cares ONLY about news that can move: forex majors, commodities, or stock indices,
    or broader macro events (central bank decisions, inflation/jobs data, geopolitical shocks,
    major earnings/M&A, energy supply news) that ripple into those markets.

    Target Asset Scope (tag the SINGLE most relevant one):
    - Forex: EURUSD, GBPUSD, USDJPY, NZDUSD, AUDUSD, USDCAD, USD (general dollar strength/policy)
    - Commodities: XAUUSD (gold), XAGUSD (silver), USOIL, UKOIL
    - Indices: US500 (S&P 500), US30 (Dow), NAS100 (Nasdaq)

    RELEVANCE RULES:
    - is_relevant = true: central bank/rate decisions, inflation/employment/GDP data, geopolitical
      events affecting markets, oil/energy supply news, major index-moving earnings or M&A, currency
      policy, commodity supply/demand shocks, major financial institution news.
    - is_relevant = false: consumer products, lifestyle, entertainment, sports, local/regional stories
      with no macro market impact, or anything not tied to the asset scope above — even if a company
      name appears in it. Example of NOT relevant: a soft drink can size/price change in one country.

    1. Determine is_relevant per the rules above.
    2. Assign a Forex-Factory style impact colored circle: 🔴 High, 🟠 Medium, 🟡 Low, ⚪ Neutral.
    3. Select the SINGLE most relevant market tag from the Target Asset Scope list.
    4. Provide 2 CONCISE, HIGHLY INFORMATIVE executive bullet points based on the FULL ARTICLE TEXT
       (not just the headline). Do NOT repeat or paraphrase the headline. If the article text is too
       thin/empty to extract real detail, base the bullets on the summary instead and keep them general.

    Output strictly in JSON format without markdown wrapping:
    {{
        "is_relevant": true,
        "impact_emoji": "🔴",
        "market_symbol": "XAUUSD",
        "bullet_1": "Executive detail on what occurred...",
        "bullet_2": "Market effect or fundamental impact direction..."
    }}

    HEADLINE: {headline}
    SUMMARY: {summary}
    FULL ARTICLE TEXT (may be partial if the source paywalled or blocked extraction): {body_text[:4000]}
    """

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.2,
            "maxOutputTokens": 500,
            # Gemini 2.5 Flash "thinks" by default and thinking tokens are deducted
            # from the SAME budget as the actual answer. For a simple classification
            # task this was silently eating the whole response (empty output -> your
            # fallback text) and adding 5-30s of pure latency per article. Off = fast + reliable.
            "thinkingConfig": {"thinkingBudget": 0}
        }
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            _gemini_rate_limit_wait()
            res = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=15)
            if res.status_code == 200:
                data = res.json()
                text_response = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text_response)
            elif res.status_code == 429:
                print(f"Gemini rate limit hit (429) despite throttling. Waiting 15s before retry {attempt + 1}/{max_retries}...")
                time.sleep(15)
                continue
            else:
                print(f"Gemini API Error {res.status_code}: {res.text[:300]}")
                break
        except Exception as e:
            print(f"Gemini Request Failed: {e}")
            time.sleep(5)

    # Genuine failure after retries: return None instead of a generic fallback.
    # The caller will skip sending this article now and retry it automatically
    # on the next poll (10s later) instead of blasting out an unfiltered/inaccurate alert.
    return None


def send_telegram_msg(formatted_text, image_url=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured (missing bot token or chat id) - cannot send.")
        return False

    if image_url:
        photo_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        # Telegram caption limit is 1024 chars (vs 4096 for text messages)
        caption = formatted_text if len(formatted_text) <= 1024 else formatted_text[:1000] + "..."
        payload = {"chat_id": TELEGRAM_CHAT_ID, "photo": image_url, "caption": caption, "parse_mode": "HTML"}
        try:
            resp = requests.post(photo_url, data=payload, timeout=10)
            if resp.status_code == 200:
                return True
            else:
                print(f"Telegram photo send failed ({resp.status_code}): {resp.text[:300]} — falling back to text.")
        except Exception as e:
            print(f"Telegram photo post failed: {e}")

    text_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": formatted_text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        resp = requests.post(text_url, data=payload, timeout=10)
        if resp.status_code == 200:
            return True
        print(f"Telegram text send failed ({resp.status_code}): {resp.text[:300]}")
        return False
    except Exception as e:
        print(f"Telegram message error: {e}")
        return False


FINNHUB_CATEGORIES = ["general", "forex", "merger"]  # general covers commodities/indices/macro; forex = FX; merger = M&A moves


def fetch_finnhub_articles(category):
    url = f"https://finnhub.io/api/v1/news?category={category}&token={FINNHUB_KEY}"
    try:
        res = requests.get(url, timeout=10)
        if res.status_code != 200:
            print(f"Finnhub error ({category}) {res.status_code}: {res.text[:300]}")
            return []
        return res.json()
    except Exception as e:
        print(f"Finnhub fetch failed ({category}): {e}")
        return []


def process_live_news(state, now_ts, browser=None):
    if not FINNHUB_KEY:
        print("Error: FINNHUB_API_KEY environment variable is missing.")
        return

    try:
        articles = []
        seen_in_batch = set()
        for category in FINNHUB_CATEGORIES:
            for item in fetch_finnhub_articles(category):
                art_id = str(item.get("id") or item.get("url"))
                if art_id not in seen_in_batch:
                    seen_in_batch.add(art_id)
                    articles.append(item)

        new_count = 0
        for item in articles:
            article_id = str(item.get("id") or item.get("url"))
            pub_time = item.get("datetime", 0)

            if article_id in state["sent_ids"]:
                continue
            if (now_ts - pub_time) > MAX_AGE_SECONDS:
                continue

            new_count += 1

            headline = item.get("headline", "").strip()
            summary = item.get("summary", "").strip()
            article_url = item.get("url", "N/A")
            publisher = item.get("source", "Reuters")

            scraped_img, body_text = scrape_article_details(article_url, browser)
            final_image = scraped_img if scraped_img else item.get("image")

            ai_data = analyze_with_ai(headline, summary, body_text)

            if ai_data is None:
                # Genuine AI failure (rate limit, outage, etc). Don't mark as sent -
                # it stays eligible and will be retried automatically on the next poll.
                print(f"AI analysis unavailable for '{headline}' right now - will retry next poll.")
                continue

            if not ai_data.get("is_relevant", True):
                state["sent_ids"].append(article_id)
                save_state(state)
                continue

            ist_time = (datetime.fromtimestamp(pub_time, tz=timezone.utc) + IST_OFFSET).strftime("%d %b %Y, %I:%M %p IST")
            impact_dot = ai_data.get("impact_emoji", "🔴")
            market_symbol = html.escape(str(ai_data.get("market_symbol", "USD")))
            bullet_1 = html.escape(str(ai_data.get("bullet_1", headline)))
            bullet_2 = html.escape(str(ai_data.get("bullet_2", summary[:200])))
            safe_headline = html.escape(headline)
            safe_publisher = html.escape(publisher)
            safe_url = html.escape(article_url, quote=False)

            message = (
                f"{impact_dot} <b>{market_symbol} | {safe_headline}</b>\n\n"
                f"• {bullet_1}\n"
                f"• {bullet_2}\n\n"
                f"<b>Released Time:</b> {ist_time}\n"
                f"<b>Publisher:</b> {safe_publisher}\n"
                f"<b>Link:</b> {safe_url}"
            )

            if send_telegram_msg(message, final_image):
                state["sent_ids"].append(article_id)
                save_state(state)
                print(f"[{ist_time}] Alert Sent: {headline}")
                time.sleep(1.5)
            else:
                # Mark as sent anyway so a permanently-malformed article doesn't loop forever
                # eating retries every 30s for the rest of the run.
                state["sent_ids"].append(article_id)
                save_state(state)
                print(f"[{ist_time}] Alert FAILED to send (see error above), skipping: {headline}")

        if new_count == 0:
            print(f"No new qualifying articles this poll ({len(articles)} fetched from Finnhub).")

    except Exception as e:
        print(f"Error during news processing: {e}")


def main():
    print("Starting AI Market News Engine...")
    if not FINNHUB_KEY:
        print("WARNING: FINNHUB_API_KEY is not set.")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("WARNING: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set.")
    if not GEMINI_KEY:
        print("WARNING: GEMINI_API_KEY is not set - AI analysis will be skipped (fallback only).")

    state = load_state()

    # Seed only items older than 1 hour on boot so recent items trigger IMMEDIATELY
    try:
        now_ts = time.time()
        seeded = 0
        for category in FINNHUB_CATEGORIES:
            for item in fetch_finnhub_articles(category):
                art_id = str(item.get("id") or item.get("url"))
                pub_time = item.get("datetime", 0)
                # If article is older than 1 hour, ignore it. If newer, let it process!
                if (now_ts - pub_time) > 3600:
                    if art_id not in state["sent_ids"]:
                        state["sent_ids"].append(art_id)
                        seeded += 1
        save_state(state)
        print(f"Boot seeding complete: marked {seeded} old articles as already-seen.")
    except Exception as e:
        print(f"Boot seeding warning: {e}")

    print("Startup complete. Processing live market news...")

    # One browser instance reused for the entire run (launching per-article would be very slow).
    # If it fails to launch for any reason, fall back to static-only scraping rather than crashing.
    playwright_ctx = None
    browser = None
    try:
        playwright_ctx = sync_playwright().start()
        browser = playwright_ctx.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        print("Headless browser ready for JS-rendered article fallback.")
    except Exception as e:
        print(f"Could not start headless browser ({e}) - continuing with static-only scraping.")

    try:
        start_time = time.time()
        while (time.time() - start_time) < LOOP_DURATION_SECONDS:
            try:
                current_ts = time.time()
                process_live_news(state, current_ts, browser)
            except Exception as e:
                print(f"Loop iteration error: {e}")
            time.sleep(POLL_INTERVAL_SECONDS)
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if playwright_ctx:
            try:
                playwright_ctx.stop()
            except Exception:
                pass

    print("4h 55m daemon cycle finished cleanly.")


if __name__ == "__main__":
    main()

    
           





   
