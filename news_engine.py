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
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STATE_FILE = "state.json"

# Timing constants
MAX_AGE_SECONDS = 2 * 3600  # 2 hours buffer required for Finnhub indexing delays
LOOP_DURATION_SECONDS = 4 * 3600 + 55 * 60  # 4 hours 55 minutes
POLL_INTERVAL_SECONDS = 5  # 3 categories x 12 polls/min = 36 Finnhub calls/min, still under the 60/min ceiling
IST_OFFSET = timedelta(hours=5, minutes=30)

# --- Rule-based day-trader relevance classifier (no AI, fully deterministic) ---
# Default is NOT relevant: an article only gets sent if it actually matches one of
# these keyword groups. Safer than an AI fallback that defaults to "send everything."

FOREX_KEYWORDS = {
    "EURUSD": ["euro", "eurozone", "ecb", "european central bank"],
    "GBPUSD": ["pound sterling", "bank of england", " boe ", "sterling"],
    "USDJPY": [" yen", "bank of japan", " boj "],
    "NZDUSD": ["new zealand dollar", "rbnz"],
    "AUDUSD": ["australian dollar", " rba "],
    "USDCAD": ["canadian dollar", "bank of canada", " boc "],
}

COMMODITY_KEYWORDS = {
    "XAUUSD": ["gold price", "gold prices", "gold rose", "gold fell", "bullion", "gold rally", "gold surge"],
    "XAGUSD": ["silver price", "silver prices"],
    "USOIL": ["crude oil", " wti ", "opec", "oil price", "oil prices", "per barrel"],
    "UKOIL": ["brent crude", "brent oil"],
}

INDEX_KEYWORDS = {
    "US500": ["s&p 500", "s&p500", "sp500"],
    "US30": ["dow jones", "dow industrial"],
    "NAS100": ["nasdaq"],
}

MACRO_KEYWORDS = {
    "USD": [
        "federal reserve", " fed ", "fomc", "interest rate", "rate hike", "rate cut",
        "inflation", " cpi ", "consumer price index", "nonfarm payroll", "non-farm payroll",
        "jobs report", "unemployment rate", " gdp ", "treasury yield", "treasury yields",
        "powell", "central bank", "recession", "tariff", "trade war", "sanctions",
    ]
}

HIGH_IMPACT_KEYWORDS = [
    "rate hike", "rate cut", "fomc", "nonfarm payroll", "non-farm payroll", " cpi ",
    "consumer price index", "war", "invasion", "sanctions", "opec", "central bank",
    "powell", "recession",
]
MEDIUM_IMPACT_KEYWORDS = [
    "inflation", " gdp ", "jobs report", "unemployment", "treasury yield", "trade war",
    "tariff", "crude oil", "gold price",
]

_ALL_CATEGORY_GROUPS = [FOREX_KEYWORDS, COMMODITY_KEYWORDS, INDEX_KEYWORDS, MACRO_KEYWORDS]


def _extract_bullets(headline, summary, body_text):
    """No AI paraphrasing - pull real sentences from the actual article text (or
    fall back to the Finnhub summary) rather than inventing anything."""
    source_text = body_text if body_text and len(body_text) > 60 else summary
    if not source_text:
        return headline, "No further details available."

    # crude sentence split, good enough for extractive bullets
    sentences = [s.strip() for s in source_text.replace("\n", " ").split(". ") if len(s.strip()) > 20]
    bullet_1 = sentences[0][:220] if len(sentences) > 0 else headline
    bullet_2 = sentences[1][:220] if len(sentences) > 1 else (summary[:220] if summary else "No further details available.")
    if not bullet_1.endswith((".", "!", "?")):
        bullet_1 += "."
    if not bullet_2.endswith((".", "!", "?")):
        bullet_2 += "."
    return bullet_1, bullet_2


def classify_article(headline, summary, body_text):
    haystack = f" {headline} {summary} {body_text[:1500]} ".lower()

    matched_symbol = None
    for group in _ALL_CATEGORY_GROUPS:
        for symbol, keywords in group.items():
            if any(kw in haystack for kw in keywords):
                matched_symbol = symbol
                break
        if matched_symbol:
            break

    if not matched_symbol:
        return {"is_relevant": False}

    if any(kw in haystack for kw in HIGH_IMPACT_KEYWORDS):
        impact_emoji = "🔴"
    elif any(kw in haystack for kw in MEDIUM_IMPACT_KEYWORDS):
        impact_emoji = "🟠"
    else:
        impact_emoji = "🟡"

    bullet_1, bullet_2 = _extract_bullets(headline, summary, body_text)

    return {
        "is_relevant": True,
        "impact_emoji": impact_emoji,
        "market_symbol": matched_symbol,
        "bullet_1": bullet_1,
        "bullet_2": bullet_2,
    }

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

            classification = classify_article(headline, summary, body_text)

            if not classification.get("is_relevant", False):
                state["sent_ids"].append(article_id)
                save_state(state)
                continue

            ist_time = (datetime.fromtimestamp(pub_time, tz=timezone.utc) + IST_OFFSET).strftime("%d %b %Y, %I:%M %p IST")
            impact_dot = classification.get("impact_emoji", "🟡")
            market_symbol = html.escape(str(classification.get("market_symbol", "USD")))
            bullet_1 = html.escape(str(classification.get("bullet_1", headline)))
            bullet_2 = html.escape(str(classification.get("bullet_2", summary[:200])))
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
