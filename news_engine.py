import os
import time
import json
import html
import requests
import trafilatura
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

# Running on GitHub Actions? It sets this env var automatically - we use it to tell
# CI mode (bounded ~5h loop, secrets from GH Secrets) apart from local mode
# (loop forever, credentials from a .env file since there's no GH Secrets locally).
RUNNING_IN_CI = os.getenv("GITHUB_ACTIONS") == "true"

if not RUNNING_IN_CI:
    try:
        from dotenv import load_dotenv
        load_dotenv()  # reads a .env file sitting next to this script, if present
    except ImportError:
        pass  # dotenv not installed - fine, will just read real OS env vars instead

# Environment Variables
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STATE_FILE = "state.json"

# Timing constants
MAX_AGE_SECONDS = 2 * 3600  # 2 hours buffer required for Finnhub indexing delays
# On GitHub Actions we must stop before the 6-hour job limit and hand off to the next
# self-triggered run. Running locally has no such limit, so just loop forever.
LOOP_DURATION_SECONDS = (4 * 3600 + 55 * 60) if RUNNING_IN_CI else float("inf")
POLL_INTERVAL_SECONDS = 15  # 5s was too aggressive - real-world testing showed it triggering
                             # timeouts/throttling on Finnhub and investingLive from sustained
                             # rapid-fire requests. 15s is still fast for news alerts and far gentler.
REQUEST_TIMEOUT = 15  # was 10s - too tight under normal network variance, caused false failures
IST_OFFSET = timedelta(hours=5, minutes=30)

# --- Rule-based day-trader relevance classifier (no AI, fully deterministic) ---
# Default is NOT relevant: an article only gets sent if it actually matches one of
# these keyword groups. Safer than an AI fallback that defaults to "send everything."

FOREX_KEYWORDS = {
    "EURUSD": ["euro", "eurozone", "ecb", "european central bank", "eur/usd", "eurusd", " eur "],
    "GBPUSD": ["pound sterling", "bank of england", " boe ", "sterling", "gbp/usd", "gbpusd", " gbp ", "british pound"],
    "USDJPY": [" yen", "bank of japan", " boj ", "usd/jpy", "usdjpy"],
    "NZDUSD": ["new zealand dollar", "rbnz", "nzd/usd", "nzdusd", " nzd "],
    "AUDUSD": ["australian dollar", " rba ", "aud/usd", "audusd", " aud "],
    "USDCAD": ["canadian dollar", "bank of canada", " boc ", "usd/cad", "usdcad"],
}

COMMODITY_KEYWORDS = {
    "XAUUSD": ["gold price", "gold prices", "gold rose", "gold fell", "bullion", "gold rally",
               "gold surge", "gold ", "xau/usd", "xauusd"],
    "XAGUSD": ["silver price", "silver prices", "silver ", "xag/usd", "xagusd"],
    "USOIL": ["crude oil", " wti ", "opec", "oil price", "oil prices", "per barrel", " oil "],
    "UKOIL": ["brent crude", "brent oil", "brent "],
}

INDEX_KEYWORDS = {
    "US500": ["s&p 500", "s&p500", "sp500", " spx ", " spy "],
    "US30": ["dow jones", "dow industrial", " djia "],
    "NAS100": ["nasdaq", " ndx ", " qqq "],
}

MACRO_KEYWORDS = {
    "USD": [
        "federal reserve", " fed ", "fomc", "interest rate", "rate hike", "rate cut",
        "inflation", " cpi ", "consumer price index", "nonfarm payroll", "non-farm payroll",
        "jobs report", "unemployment rate", " gdp ", "treasury yield", "treasury yields",
        "powell", "central bank", "recession", "tariff", "trade war", "sanctions",
        "stock market", "wall street", "global markets", "equity markets", "risk-off",
        "risk-on", "safe haven", "volatility", "dollar index", " dxy ",
    ]
}

# Finnhub's 'merger' category doesn't naturally use any of the keywords above,
# so every M&A article was being silently rejected. M&A moves the indices this
# feed is supposed to cover, so it gets its own group.
MERGER_KEYWORDS = {
    "US500": [
        "acquisition", "acquire", "acquires", "acquiring", "merger", "merges",
        "buyout", "takeover", "to buy", "to acquire", "deal to buy", "definitive agreement",
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

# A "week ahead" / "3 things to watch" / calendar-preview piece mentioning the Fed
# is a very different urgency than the Fed actually announcing something right now -
# but keyword presence alone can't tell those apart. Roundup language caps severity
# down a notch, since it's a preview/summary of things, not a single breaking event.
PREVIEW_ROUNDUP_PHRASES = [
    "week ahead", "things to watch", "things we're watching", "main events for",
    "what to expect", "what to watch", "day ahead", "week in focus", "in focus this week",
    "key events this week", "here are the",
]

# "War" or "central bank" appearing next to de-escalation language (a ceasefire,
# a pause, talks resuming) is a calming development, not a shock - it shouldn't
# read at the same alarm level as an escalation or a surprise policy move.
DE_ESCALATION_PHRASES = [
    "halt strikes", "halts strikes", "halt attacks", "ceasefire", "cease-fire",
    "pause continues", "maintains pause", "de-escalat", "peace talks", "truce",
]

_ALL_CATEGORY_GROUPS = [FOREX_KEYWORDS, COMMODITY_KEYWORDS, INDEX_KEYWORDS, MACRO_KEYWORDS, MERGER_KEYWORDS]


def _extract_bullets(headline, summary, body_text):
    """No AI paraphrasing - pull real sentences from the actual article text (or
    fall back to the Finnhub summary) rather than inventing anything."""
    source_text = body_text if body_text and len(body_text) > 60 else summary
    if not source_text:
        return headline, "No further details available."

    # crude sentence split, good enough for extractive bullets
    sentences = [s.strip() for s in source_text.replace("\n", " ").split(". ") if len(s.strip()) > 20]
    bullet_1 = sentences[0][:220] if len(sentences) > 0 else headline
    bullet_1_normalized = bullet_1.strip().rstrip(".!?").lower()
    if len(sentences) > 1:
        bullet_2 = sentences[1][:220]
    elif summary and summary[:220].strip().rstrip(".!?").lower() != bullet_1_normalized:
        bullet_2 = summary[:220]
    else:
        bullet_2 = "No further detail available from the source feed."
    if not bullet_1.endswith((".", "!", "?")):
        bullet_1 += "."
    if not bullet_2.endswith((".", "!", "?")):
        bullet_2 += "."
    return bullet_1, bullet_2


def classify_article(headline, summary, body_text):
    # Deliberately headline+summary ONLY, not body_text - a soft feature story
    # can easily mention "recession" or "Wall Street" once in passing while
    # discussing something unrelated, and that was getting misread as the
    # story itself being high-impact market news. Headlines/summaries are
    # curated by the publisher to reflect what the story is actually about;
    # body text is not a reliable relevance signal here.
    haystack = f" {headline} {summary} ".lower()

    matched_symbol = None
    for group in _ALL_CATEGORY_GROUPS:
        for symbol, keywords in group.items():
            if any(kw in haystack for kw in keywords):
                matched_symbol = symbol
                break
        if matched_symbol:
            break

    # No specific match -> still send it, just tagged as general/neutral instead
    # of a specific instrument. Nothing gets silently dropped anymore.
    if not matched_symbol:
        bullet_1, bullet_2 = _extract_bullets(headline, summary, body_text)
        return {
            "is_relevant": True,
            "impact_emoji": "⚪",
            "market_symbol": "GENERAL",
            "bullet_1": bullet_1,
            "bullet_2": bullet_2,
        }

    is_preview_roundup = any(phrase in haystack for phrase in PREVIEW_ROUNDUP_PHRASES)
    is_deescalation = any(phrase in haystack for phrase in DE_ESCALATION_PHRASES)

    if any(kw in haystack for kw in HIGH_IMPACT_KEYWORDS):
        impact_emoji = "🟠" if is_deescalation else "🔴"
    elif any(kw in haystack for kw in MEDIUM_IMPACT_KEYWORDS):
        impact_emoji = "🟡" if is_deescalation else "🟠"
    else:
        impact_emoji = "🟡"

    # A calendar/roundup piece is a preview of several things, not one breaking
    # event - cap it down a notch regardless of which topics it happens to cover.
    if is_preview_roundup and impact_emoji == "🔴":
        impact_emoji = "🟠"
    elif is_preview_roundup and impact_emoji == "🟠":
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


_BLOCK_PAGE_SIGNS = [
    "access denied", "you don't have permission to access",
    "reference #", "request blocked", "403 forbidden",
    "attention required", "checking your browser",
]


def _looks_like_block_page(text):
    """Some WAFs (Akamai etc.) serve their block/challenge page with a normal
    200 status, so a status-code check alone doesn't catch it - trafilatura
    happily 'extracts' the block page's own text as if it were the article.
    Catch it by content instead."""
    if not text or len(text) > 600:
        return False  # a real article this short is rare, but block pages are always short
    lowered = text.lower()
    return any(sign in lowered for sign in _BLOCK_PAGE_SIGNS)


def scrape_article_details(article_url, browser=None):
    cover_image = None
    body_text = ""
    if not article_url or article_url == "N/A":
        return cover_image, body_text

    # Tier 1: fast static fetch + trafilatura (proper main-content extraction,
    # strips ads/nav/related-articles junk far better than raw <p> joining)
    try:
        res = requests.get(article_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
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
            if extracted and not _looks_like_block_page(extracted):
                body_text = html.unescape(extracted.strip())
            elif extracted:
                print(f"Scrape notice: {article_url} returned a block/access-denied page, discarding as content.")
                cover_image = None  # the "image" on a block page is the WAF's own logo, not a real photo
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
            if extracted and not _looks_like_block_page(extracted) and len(extracted) > len(body_text):
                body_text = html.unescape(extracted.strip())

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

# No API key, no rate limit tier, nothing to exhaust - these are publisher RSS feeds
# dedicated to live market news, often faster than Finnhub's own indexing delay.
RSS_FEEDS = {
    "https://www.investing.com/rss/news_1.rss": "Investing.com (Forex)",
    "https://www.investing.com/rss/news_11.rss": "Investing.com (Commodities)",
    "https://investinglive.com/rss": "investingLive",
}


def fetch_finnhub_articles(category):
    url = f"https://finnhub.io/api/v1/news?category={category}&token={FINNHUB_KEY}"
    for attempt in range(3):  # GitHub Actions' shared IPs occasionally get a transient
                              # Cloudflare-level 502 that a residential IP wouldn't see -
                              # a short backoff clears this far more reliably than a bare retry.
        try:
            res = requests.get(url, timeout=REQUEST_TIMEOUT)
            if res.status_code == 200:
                return res.json()
            if res.status_code >= 500 and attempt < 2:
                print(f"Finnhub {res.status_code} ({category}) - transient, retrying in {2 * (attempt + 1)}s...")
                time.sleep(2 * (attempt + 1))
                continue
            print(f"Finnhub error ({category}) {res.status_code}: {res.text[:200]}")
            return []
        except Exception as e:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            print(f"Finnhub fetch failed ({category}): {e}")
            return []
    return []


def fetch_rss_articles(feed_url, source_name):
    """Returns items normalized to the same shape Finnhub items use, so they flow
    through the exact same scrape/classify/send pipeline with no special-casing."""
    import xml.etree.ElementTree as ET

    for attempt in range(2):  # one retry - a single timeout shouldn't cost us the whole poll
        try:
            res = requests.get(feed_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            break
        except Exception as e:
            if attempt == 0:
                continue
            print(f"RSS fetch failed ({source_name}): {e}")
            return []

    try:
        if res.status_code != 200:
            print(f"RSS error ({source_name}) {res.status_code}")
            return []

        root = ET.fromstring(res.content)
        items = []
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            description = (item.findtext("description") or "").strip()
            pub_date_str = item.findtext("pubDate")

            pub_ts = 0
            if pub_date_str:
                for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
                    try:
                        pub_ts = datetime.strptime(pub_date_str, fmt).timestamp()
                        break
                    except ValueError:
                        continue

            if not link or not title:
                continue

            items.append({
                "id": f"rss_{hash(link)}",
                "datetime": int(pub_ts),
                "headline": title,
                "summary": description,
                "url": link,
                "source": source_name,
                "image": None,
            })
        return items
    except Exception as e:
        print(f"RSS fetch failed ({source_name}): {e}")
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

        for feed_url, source_name in RSS_FEEDS.items():
            for item in fetch_rss_articles(feed_url, source_name):
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

            # Google News redirect links (licensed wire-content redistribution) never
            # give a real image or real extractable text, and every attempt to work
            # around that has proven unreliable. Drop these entirely - mark seen,
            # never send - rather than keep sending thin/imageless alerts.
            if "news.google.com" in article_url:
                state["sent_ids"].append(article_id)
                save_state(state)
                continue

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
    except KeyboardInterrupt:
        print("\nStopped by user (Ctrl+C).")
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

    if RUNNING_IN_CI:
        print("4h 55m daemon cycle finished cleanly.")
    else:
        print("Engine stopped.")


if __name__ == "__main__":
    main()
