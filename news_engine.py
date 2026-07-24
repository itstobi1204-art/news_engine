import os
import time
import json
import html
import re
import requests
import trafilatura
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta

# Environment Variables
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STATE_FILE = "state.json"

# Timing constants
MAX_AGE_SECONDS = 15 * 60  # Discard news older than 15 minutes (ensures zero old news delay)
LOOP_DURATION_SECONDS = 4 * 3600 + 50 * 60  # Continuous runner (~4h 50m)
POLL_INTERVAL_SECONDS = 5
IST_OFFSET = timedelta(hours=5, minutes=30)

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_RPM_LIMIT = 14
_gemini_call_times = []

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}

def _gemini_rate_limit_wait():
    now = time.time()
    while _gemini_call_times and now - _gemini_call_times[0] > 60:
        _gemini_call_times.pop(0)
    if len(_gemini_call_times) >= GEMINI_RPM_LIMIT:
        wait_for = 60 - (now - _gemini_call_times[0]) + 0.5
        if wait_for > 0:
            time.sleep(wait_for)
    _gemini_call_times.append(time.time())

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

def is_generic_logo(url):
    if not url:
        return True
    lower = url.lower()
    bad_keywords = [
        "logo", "favicon", "placeholder", "default", "avatar", "icon", 
        "reuters_brand", "cnbc_logo", "social_default", "banner_small"
    ]
    return any(k in lower for k in bad_keywords)

def scrape_article_details(article_url):
    cover_image = None
    body_text = ""
    if not article_url or article_url == "N/A":
        return cover_image, body_text

    try:
        res = requests.get(article_url, headers=HEADERS, timeout=4)
        if res.status_code == 200:
            raw_html = res.text
            soup = BeautifulSoup(raw_html, "html.parser")
            
            og_img = (
                soup.find("meta", property="og:image")
                or soup.find("meta", attrs={"name": "twitter:image"})
                or soup.find("meta", property="twitter:image")
            )
            if og_img and og_img.get("content"):
                candidate_img = og_img["content"].strip()
                if not is_generic_logo(candidate_img):
                    cover_image = candidate_img

            extracted = trafilatura.extract(raw_html, include_comments=False, include_tables=False)
            if extracted:
                body_text = extracted.strip()
    except Exception as e:
        print(f"Fast scrape notice for {article_url}: {e}")

    return cover_image, body_text

def analyze_with_ai(headline, summary, body_text):
    if not GEMINI_KEY:
        return {
            "is_relevant": True,
            "impact_emoji": "🔴",
            "market_symbol": "USD",
            "bullet_1": headline,
            "bullet_2": summary[:200] if summary else "No further text available."
        }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    
    prompt = f"""
You are an expert market trader news analyst. Analyze this market news for active traders in:
Forex (EURUSD, NZDUSD, USD), Commodities (XAUUSD, USOIL), Indices (US500).

TARGET ASSET SYMBOLS:
- XAUUSD (Gold)
- NZDUSD
- EURUSD
- US500 (S&P 500)
- USOIL (Crude Oil)
- USD (Broad Dollar impact / Fed policy / US Economic Data)

NEWS COVERAGE SCOPE:
- Central bank decisions, Fed policy, VIP speeches (Fed Chair, Trump, World Leaders).
- Economic calendar data (NFP, CPI, Rates, GDP, Retail Sales, PMI).
- Geopolitical shocks & developments across the world.
- Forex & Commodity market drivers, USD strengthening or weakening factors.

INSTRUCTIONS:
1. is_relevant = true if it matches any of the above macro or target market triggers. Set false for sports, lifestyle, local non-market news.
2. Select impact_emoji based on Forex Factory impact style:
   🔴 = High Impact (Major market mover)
   🟠 = Medium Impact
   🟡 = Low Impact
   ⚪ = Neutral / Informational
3. Assign the SINGLE best matching symbol strictly from: ["XAUUSD", "NZDUSD", "EURUSD", "US500", "USOIL", "USD"].
4. Generate 2 HIGHLY INFORMATIVE, DATA-DENSE bullet points:
   - Do NOT just restate or simplify the headline.
   - Extract exact facts, numbers, rates, policy details, geopolitical context, or market effects from the article text.
   - Keep them concise, direct, and valuable for professional day traders.

OUTPUT SYNTAX (Strict JSON, no markdown code block wrapping):
{{
  "is_relevant": true,
  "impact_emoji": "🔴",
  "market_symbol": "XAUUSD",
  "bullet_1": "Exact quantitative finding or policy development with context...",
  "bullet_2": "Fundamental market impact rationale or asset direction..."
}}

HEADLINE: {headline}
SUMMARY: {summary}
ARTICLE TEXT: {body_text[:3500]}
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.1,
            "maxOutputTokens": 450,
            "thinkingConfig": {"thinkingBudget": 0}
        }
    }

    try:
        _gemini_rate_limit_wait()
        res = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=8)
        if res.status_code == 200:
            data = res.json()
            text_response = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text_response)
        else:
            print(f"Gemini API Error {res.status_code}: {res.text[:200]}")
    except Exception as e:
        print(f"Gemini Request exception: {e}")

    return None

def send_telegram_msg(formatted_text, image_url=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram configuration missing.")
        return False

    if image_url and not is_generic_logo(image_url):
        photo_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        caption = formatted_text if len(formatted_text) <= 1024 else formatted_text[:1000] + "..."
        payload = {"chat_id": TELEGRAM_CHAT_ID, "photo": image_url, "caption": caption, "parse_mode": "HTML"}
        try:
            resp = requests.post(photo_url, data=payload, timeout=8)
            if resp.status_code == 200:
                return True
        except Exception as e:
            print(f"Telegram photo send exception: {e}")

    text_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": formatted_text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        resp = requests.post(text_url, data=payload, timeout=8)
        return resp.status_code == 200
    except Exception as e:
        print(f"Telegram text send error: {e}")
        return False

FINNHUB_CATEGORIES = ["general", "forex", "merger"]

def fetch_finnhub_articles(category):
    url = f"https://finnhub.io/api/v1/news?category={category}&token={FINNHUB_KEY}"
    try:
        res = requests.get(url, timeout=6)
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        print(f"Finnhub fetch error ({category}): {e}")
    return []

def process_live_news(state, now_ts):
    if not FINNHUB_KEY:
        print("Missing FINNHUB_API_KEY.")
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

        for item in articles:
            article_id = str(item.get("id") or item.get("url"))
            pub_time = item.get("datetime", 0)

            if article_id in state["sent_ids"]:
                continue
            
            # STRICT AGE CHECK: Ignore anything older than MAX_AGE_SECONDS (15 minutes)
            if (now_ts - pub_time) > MAX_AGE_SECONDS:
                continue

            headline = item.get("headline", "").strip()
            summary = item.get("summary", "").strip()
            article_url = item.get("url", "N/A")
            publisher = item.get("source", "Market News")

            scraped_img, body_text = scrape_article_details(article_url)
            
            # Prefer real article cover image over generic API default thumbnail
            final_image = scraped_img if scraped_img else item.get("image")
            if is_generic_logo(final_image):
                final_image = None

            ai_data = analyze_with_ai(headline, summary, body_text)
            if ai_data is None:
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

            # Updated Telegram Message Layout
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
                print(f"[{ist_time}] High-Speed Alert Sent: {market_symbol} | {headline}")
                time.sleep(1.0)
            else:
                state["sent_ids"].append(article_id)
                save_state(state)

    except Exception as e:
        print(f"Error processing news: {e}")

def main():
    print("Starting AI Market News Engine...")
    state = load_state()

    # Seed existing/old articles older than 10 minutes on boot so re-runs never trigger legacy news
    try:
        now_ts = time.time()
        seeded = 0
        for category in FINNHUB_CATEGORIES:
            for item in fetch_finnhub_articles(category):
                art_id = str(item.get("id") or item.get("url"))
                pub_time = item.get("datetime", 0)
                if (now_ts - pub_time) > 600:
                    if art_id not in state["sent_ids"]:
                        state["sent_ids"].append(art_id)
                        seeded += 1
        save_state(state)
        print(f"Boot seeding complete: Marked {seeded} old articles as processed.")
    except Exception as e:
        print(f"Boot seeding warning: {e}")

    start_time = time.time()
    while (time.time() - start_time) < LOOP_DURATION_SECONDS:
        try:
            process_live_news(state, time.time())
        except Exception as e:
            print(f"Loop iteration error: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
