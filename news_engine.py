import os
import time
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta

# Environment Variables
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STATE_FILE = "state.json"

# Timing constants
MAX_AGE_SECONDS = 2 * 3600  # 2 hours buffer required for Finnhub indexing delays
LOOP_DURATION_SECONDS = 4 * 3600 + 55 * 60  # 4 hours 55 minutes
IST_OFFSET = timedelta(hours=5, minutes=30)

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


def scrape_article_details(article_url):
    cover_image = None
    body_text = ""
    if not article_url or article_url == "N/A":
        return cover_image, body_text

    try:
        res = requests.get(article_url, headers=HEADERS, timeout=6)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            og_img = (
                soup.find("meta", property="og:image") 
                or soup.find("meta", attrs={"name": "twitter:image"})
                or soup.find("meta", property="twitter:image")
            )
            if og_img and og_img.get("content"):
                cover_image = og_img["content"]

            paragraphs = [p.get_text().strip() for p in soup.find_all("p") if len(p.get_text().strip()) > 30]
            body_text = " ".join(paragraphs[:8])
    except Exception as e:
        print(f"Scraper notice for {article_url}: {e}")

    return cover_image, body_text


def analyze_with_ai(headline, summary, body_text):
    if not GEMINI_KEY:
        return {
            "is_relevant": True,
            "impact_emoji": "🔴",
            "market_symbol": "USD",
            "bullet_1": headline,
            "bullet_2": summary[:200] if summary else "No further details available."
        }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}"
    prompt = f"""
    You are an elite Forex and Global Macro market analyst AI engine. Analyze the incoming headline and article body text.
    Target Asset Scope: XAUUSD, NZDUSD, EURUSD, US500, USOIL, USD.
    
    1. Determine if this news is RELEVANT to macro/forex markets or the target assets.
    2. Assign a Forex-Factory style impact colored circle: 🔴 High, 🟠 Medium, 🟡 Low, ⚪ Neutral.
    3. Select the SINGLE most relevant market tag from: [XAUUSD, NZDUSD, EURUSD, US500, USOIL, USD].
    4. Provide 2 CONCISE, HIGHLY INFORMATIVE executive bullet points. Do NOT repeat or paraphrase the headline.

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
    BODY TEXT: {body_text[:2000]}
    """
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.2}
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            res = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=15)
            if res.status_code == 200:
                data = res.json()
                text_response = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text_response)
            elif res.status_code == 429:
                print(f"Rate limit hit (429). Waiting 10s before retry {attempt + 1}/{max_retries}...")
                time.sleep(10)
                continue
            else:
                print(f"Gemini API Error {res.status_code}: {res.text}")
                break
        except Exception as e:
            print(f"Gemini Request Failed: {e}")
            time.sleep(5)

    return {
        "is_relevant": True, "impact_emoji": "⚪", "market_symbol": "USD",
        "bullet_1": headline, "bullet_2": "Detailed AI context temporarily unavailable."
    }


def send_telegram_msg(formatted_text, image_url=None):
    if image_url:
        photo_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "photo": image_url, "caption": formatted_text, "parse_mode": "HTML"}
        try:
            if requests.post(photo_url, data=payload, timeout=10).status_code == 200:
                return True
        except Exception as e:
            print(f"Telegram photo post failed: {e}")

    text_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": formatted_text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        return requests.post(text_url, data=payload, timeout=10).status_code == 200
    except Exception as e:
        print(f"Telegram message error: {e}")
        return False


def process_live_news(state, now_ts):
    if not FINNHUB_KEY:
        print("Error: FINNHUB_API_KEY environment variable is missing.")
        return

    url = f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_KEY}"
    try:
        res = requests.get(url, timeout=10)
        if res.status_code != 200:
            return

        articles = res.json()
        for item in articles:
            article_id = str(item.get("id") or item.get("url"))
            pub_time = item.get("datetime", 0)

            if article_id in state["sent_ids"]:
                continue
            if (now_ts - pub_time) > MAX_AGE_SECONDS:
                continue

            headline = item.get("headline", "").strip()
            summary = item.get("summary", "").strip()
            article_url = item.get("url", "N/A")
            publisher = item.get("source", "Reuters")

            scraped_img, body_text = scrape_article_details(article_url)
            final_image = scraped_img if scraped_img else item.get("image")

            ai_data = analyze_with_ai(headline, summary, body_text)
            
            if not ai_data or not ai_data.get("is_relevant", True):
                state["sent_ids"].append(article_id)
                save_state(state)
                continue

            ist_time = (datetime.fromtimestamp(pub_time, tz=timezone.utc) + IST_OFFSET).strftime("%d %b %Y, %I:%M %p IST")
            impact_dot = ai_data.get("impact_emoji", "🔴")
            market_symbol = ai_data.get("market_symbol", "USD")
            bullet_1 = ai_data.get("bullet_1", headline)
            bullet_2 = ai_data.get("bullet_2", summary[:200])

            message = (
                f"{impact_dot} <b>{market_symbol} | {headline}</b>\n\n"
                f"• {bullet_1}\n"
                f"• {bullet_2}\n\n"
                f"<b>Released Time:</b> {ist_time}\n"
                f"<b>Publisher:</b> {publisher}\n"
                f"<b>Link:</b> {article_url}"
            )

            if send_telegram_msg(message, final_image):
                state["sent_ids"].append(article_id)
                save_state(state)
                print(f"[{ist_time}] Alert Sent: {headline}")
                time.sleep(4.5) 

    except Exception as e:
        print(f"Error during news processing: {e}")


def main():
    print("Starting AI Market News Engine...")
    state = load_state()

    # Seed only items older than 1 hour on boot so recent items trigger IMMEDIATELY
    try:
        res = requests.get(f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_KEY}", timeout=10)
        if res.status_code == 200:
            now_ts = time.time()
            for item in res.json():
                art_id = str(item.get("id") or item.get("url"))
                pub_time = item.get("datetime", 0)
                # If article is older than 1 hour, ignore it. If newer, let it process!
                if (now_ts - pub_time) > 3600:
                    if art_id not in state["sent_ids"]:
                        state["sent_ids"].append(art_id)
            save_state(state)
    except Exception as e:
        print(f"Boot seeding warning: {e}")

    print("Startup complete. Processing live market news...")

    start_time = time.time()
    while (time.time() - start_time) < LOOP_DURATION_SECONDS:
        try:
            current_ts = time.time()
            process_live_news(state, current_ts)
        except Exception as e:
            print(f"Loop iteration error: {e}")
        time.sleep(3) 

    print("4h 55m daemon cycle finished cleanly.")


if __name__ == "__main__":
    main()
