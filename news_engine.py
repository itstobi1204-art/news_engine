import os
import time
import json
import html
import requests
import trafilatura
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

# Detect if running in GitHub Actions CI vs Local PC
RUNNING_IN_CI = os.getenv("GITHUB_ACTIONS") == "true"

if not RUNNING_IN_CI:
    try:
        from dotenv import load_dotenv
        load_dotenv()  # Reads .env file sitting in the same directory locally
    except ImportError:
        pass

# Environment Variables
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STATE_FILE = "state.json"

# Timing constants
MAX_AGE_SECONDS = 2 * 3600  # 2 hours lookback limit
# Local PC runs infinitely until you stop it (Ctrl+C); CI runs for ~5h cycles
LOOP_DURATION_SECONDS = (4 * 3600 + 55 * 60) if RUNNING_IN_CI else float("inf")
POLL_INTERVAL_SECONDS = 15
REQUEST_TIMEOUT = 15
IST_OFFSET = timedelta(hours=5, minutes=30)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# Non-English URL paths to drop immediately
NON_ENGLISH_PATH_MARKERS = ["/nl/", "/de/", "/fr/", "/es/", "/it/", "/pt/", "/zh/", "/ja/", "/ru/", "/kr/"]

# Tier-1 Day Trader Feed Sources
FINNHUB_CATEGORIES = ["general", "forex"]

RSS_FEEDS = {
    "https://investinglive.com/rss": "ForexLive",
    "https://www.fxstreet.com/rss/news": "FXStreet",
    "https://www.dailyfx.com/feeds/market-news": "DailyFX",
    "https://feeds.content.dowjones.com/public/rss/mw_topstories": "MarketWatch",
    "https://www.cnbc.com/id/10000664/device/rss/rss.html": "CNBC Markets",
    "https://www.investing.com/rss/news_1.rss": "Investing.com (Forex)",
    "https://www.investing.com/rss/news_11.rss": "Investing.com (Commodities)",
}

# Asset & Keyword Mappings
FOREX_KEYWORDS = {
    "EURUSD": ["euro", "eurozone", "ecb", "european central bank", "eur/usd", "eurusd", " German Ifo ", "ifo index", "ez cpi"],
    "GBPUSD": ["pound sterling", "bank of england", " boe ", "sterling", "gbp/usd", "gbpusd", "british pound", "uk cpi"],
    "USDJPY": [" yen", "bank of japan", " boj ", "usd/jpy", "usdjpy", "japan cpi"],
    "NZDUSD": ["new zealand dollar", "rbnz", "nzd/usd", "nzdusd"],
    "AUDUSD": ["australian dollar", " rba ", "aud/usd", "audusd"],
    "USDCAD": ["canadian dollar", "bank of canada", " boc ", "usd/cad", "usdcad"],
}

COMMODITY_KEYWORDS = {
    "XAUUSD": ["gold price", "gold prices", "gold rose", "gold fell", "bullion", "gold rally", "gold surge", "xau/usd", "xauusd"],
    "XAGUSD": ["silver price", "silver prices", "xag/usd", "xagusd"],
    "USOIL": ["crude oil", " wti ", "opec", "oil price", "oil prices", "per barrel", "eia inventory"],
    "UKOIL": ["brent crude", "brent oil"],
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
        "jobs report", "unemployment rate", " gdp ", "pce inflation", "powell", "central bank",
        "recession", "tariff", "trade war", "sanctions", "dollar index", " dxy ", " retail sales "
    ]
}

# Forex Factory Style Impact Rules
HIGH_IMPACT_KEYWORDS = [
    "rate hike", "rate cut", "fomc", "interest rate", "nonfarm payroll", "non-farm payroll", 
    " cpi ", "consumer price index", "pce ", " gdp ", "central bank", "powell", "war ", 
    "invasion", "sanctions", "opec emergency", "recession"
]

MEDIUM_IMPACT_KEYWORDS = [
    "inflation", "jobs report", "unemployment", "pmi", "retail sales", "trade balance", 
    "crude oil", "gold price", "german ifo", "ppi ", "durable goods", "eia"
]

# Commentary / Preview markers (Demotes bank opinions from false RED alerts to YELLOW)
COMMENTARY_KEYWORDS = [
    "economists", "preview", "says", "predicts", "analysts", "view", "expects", 
    "forecasts", "dbs", "ing", "citi", "socgen", "commerzbank", "research note"
]

_ALL_CATEGORY_GROUPS = [FOREX_KEYWORDS, COMMODITY_KEYWORDS, INDEX_KEYWORDS, MACRO_KEYWORDS]


def is_english_content(text, url):
    """Detects and rejects non-English articles."""
    if any(marker in url.lower() for marker in NON_ENGLISH_PATH_MARKERS):
        return False
    
    if not text:
        return True

    words = set(text.lower().split())
    english_stopwords = {"the", "and", "is", "in", "to", "of", "for", "on", "with", "at", "by", "this", "from"}
    if len(words) > 8:
        if not any(w in words for w in english_stopwords):
            return False
    return True


def _extract_bullets(headline, summary, body_text):
    source_text = body_text if body_text and len(body_text) > 60 else summary
    if not source_text:
        return headline, "No further details available."

    sentences = [s.strip() for s in source_text.replace("\n", " ").split(". ") if len(s.strip()) > 20]
    bullet_1 = sentences[0][:220] if len(sentences) > 0 else headline
    bullet_1_norm = bullet_1.strip().rstrip(".!?").lower()
    
    if len(sentences) > 1:
        bullet_2 = sentences[1][:220]
    elif summary and summary[:220].strip().rstrip(".!?").lower() != bullet_1_norm:
        bullet_2 = summary[:220]
    else:
        bullet_2 = "No further detail available from the source feed."

    if not bullet_1.endswith((".", "!", "?")):
        bullet_1 += "."
    if not bullet_2.endswith((".", "!", "?")):
        bullet_2 += "."
    return bullet_1, bullet_2


def classify_article(headline, summary, body_text, article_url):
    """Filters noise, demotes commentary notes, and classifies impact."""
    if not is_english_content(f"{headline} {summary}", article_url):
        return {"is_relevant": False}

    haystack = f" {headline} {summary} ".lower()

    matched_symbol = None
    for group in _ALL_CATEGORY_GROUPS:
        for symbol, keywords in group.items():
            if any(kw.lower() in haystack for kw in keywords):
                matched_symbol = symbol
                break
        if matched_symbol:
            break

    # STRICT RELEVANCE: Reject corporate noise or unmatched articles
    if not matched_symbol:
        return {"is_relevant": False}

    # Demote analyst opinion/previews to Yellow
    is_commentary = any(cw in haystack for cw in COMMENTARY_KEYWORDS)

    # Forex Factory Impact Classification
    if any(kw in haystack for kw in HIGH_IMPACT_KEYWORDS):
        impact_emoji = "🟡" if is_commentary else "🔴"
    elif any(kw in haystack for kw in MEDIUM_IMPACT_KEYWORDS):
        impact_emoji = "🟡" if is_commentary else "🟠"
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
        state["sent_ids"] = state["sent_ids"][-1500:]
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"Error saving state file: {e}")


def scrape_article_details(article_url, browser=None):
    cover_image = None
    body_text = ""
    if not article_url or article_url == "N/A":
        return cover_image, body_text

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
            if extracted:
                body_text = html.unescape(extracted.strip())
    except Exception as e:
        print(f"Scraper notice for {article_url}: {e}")

    # Fallback to Playwright Headless Browser for JS rendering
    if len(body_text) < 200 and browser is not None:
        page = None
        try:
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.set_default_timeout(15000)
            page.goto(article_url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(800)
            rendered_html = page.content()

            extracted = trafilatura.extract(rendered_html, include_comments=False, include_tables=False)
            if extracted and len(extracted) > len(body_text):
                body_text = html.unescape(extracted.strip())

            if not cover_image:
                soup2 = BeautifulSoup(rendered_html, "html.parser")
                og_img2 = soup2.find("meta", property="og:image")
                if og_img2 and og_img2.get("content"):
                    cover_image = og_img2["content"]
        except Exception as e:
            print(f"Browser fallback notice for {article_url}: {e}")
        finally:
            if page:
                page.close()

    return cover_image, body_text


def send_telegram_msg(formatted_text, image_url=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram configuration missing. Check your .env file.")
        return False

    for attempt in range(3):
        try:
            if image_url:
                photo_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
                caption = formatted_text if len(formatted_text) <= 1024 else formatted_text[:1000] + "..."
                payload = {"chat_id": TELEGRAM_CHAT_ID, "photo": image_url, "caption": caption, "parse_mode": "HTML"}
                resp = requests.post(photo_url, data=payload, timeout=12)
                if resp.status_code == 200:
                    return True

            # Text fallback
            text_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": formatted_text, "parse_mode": "HTML", "disable_web_page_preview": True}
            resp = requests.post(text_url, data=payload, timeout=12)
            if resp.status_code == 200:
                return True
            elif resp.status_code == 429:  # Rate limited
                time.sleep(3)
                continue
        except Exception as e:
            print(f"Telegram send attempt {attempt+1} error: {e}")
            time.sleep(2)

    return False


def fetch_finnhub_articles(category):
    if not FINNHUB_KEY:
        return []
    url = f"https://finnhub.io/api/v1/news?category={category}&token={FINNHUB_KEY}"
    try:
        res = requests.get(url, timeout=REQUEST_TIMEOUT)
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        print(f"Finnhub fetch error ({category}): {e}")
    return []


def fetch_rss_articles(feed_url, source_name):
    try:
        res = requests.get(feed_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if res.status_code != 200:
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
        print(f"RSS fetch error ({source_name}): {e}")
        return []


def process_live_news(state, now_ts, browser=None):
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

        # Filter candidates
        candidate_items = []
        for item in articles:
            article_id = str(item.get("id") or item.get("url"))
            pub_time = item.get("datetime", 0)

            if article_id in state["sent_ids"]:
                continue
            if (now_ts - pub_time) > MAX_AGE_SECONDS:
                continue

            candidate_items.append(item)

        # Sort candidate items chronologically (oldest first)
        candidate_items.sort(key=lambda x: x.get("datetime", 0))

        new_count = 0
        for item in candidate_items:
            article_id = str(item.get("id") or item.get("url"))
            pub_time = item.get("datetime", 0)
            headline = item.get("headline", "").strip()
            summary = item.get("summary", "").strip()
            article_url = item.get("url", "N/A")
            publisher = item.get("source", "Market News")

            if "news.google.com" in article_url:
                state["sent_ids"].append(article_id)
                save_state(state)
                continue

            scraped_img, body_text = scrape_article_details(article_url, browser)
            final_image = scraped_img if scraped_img else item.get("image")

            classification = classify_article(headline, summary, body_text, article_url)

            if not classification.get("is_relevant", False):
                state["sent_ids"].append(article_id)
                save_state(state)
                continue

            new_count += 1
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
                time.sleep(1.8)  # Throttle Telegram messages
            else:
                state["sent_ids"].append(article_id)
                save_state(state)

        if new_count == 0:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Polled feeds — no new qualifying day-trading news.")

    except Exception as e:
        print(f"Error during news processing: {e}")


def main():
    print("==================================================")
    print("      Starting Day-Trader Local Engine...        ")
    print("==================================================")

    if not FINNHUB_KEY:
        print("WARNING: FINNHUB_API_KEY is not set in your .env file!")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("WARNING: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing from .env!")

    state = load_state()

    # Seed all active news on startup so old news isn't re-sent on local boot
    try:
        seeded = 0
        for category in FINNHUB_CATEGORIES:
            for item in fetch_finnhub_articles(category):
                art_id = str(item.get("id") or item.get("url"))
                if art_id not in state["sent_ids"]:
                    state["sent_ids"].append(art_id)
                    seeded += 1

        for feed_url, source_name in RSS_FEEDS.items():
            for item in fetch_rss_articles(feed_url, source_name):
                art_id = str(item.get("id") or item.get("url"))
                if art_id not in state["sent_ids"]:
                    state["sent_ids"].append(art_id)
                    seeded += 1

        save_state(state)
        print(f"Boot seeding complete: Marked {seeded} current articles as seen.")
    except Exception as e:
        print(f"Boot seeding warning: {e}")

    # Launch Headless Chromium Browser locally via Playwright
    playwright_ctx = None
    browser = None
    try:
        playwright_ctx = sync_playwright().start()
        browser = playwright_ctx.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        print("Local Headless Browser started successfully.")
    except Exception as e:
        print(f"Headless browser launch failed ({e}) — proceeding with static HTTP fetching.")

    print("\n[ACTIVE] Engine is listening for live market news. Press Ctrl + C to exit.\n")

    try:
        start_time = time.time()
        while (time.time() - start_time) < LOOP_DURATION_SECONDS:
            process_live_news(state, time.time(), browser)
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\n[STOPPED] Day-Trader Engine closed by user.")
    finally:
        if browser:
            try: browser.close()
            except Exception: pass
        if playwright_ctx:
            try: playwright_ctx.stop()
            except Exception: pass


if __name__ == "__main__":
    main()
