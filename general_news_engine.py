import os
import time
import json
import requests
from datetime import datetime, timezone, timedelta

# Environment Variables
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STATE_FILE = "state_general.json"

# Timing constants
MAX_AGE_SECONDS = 2 * 3600  # 2 hours buffer for Finnhub indexing delays
LOOP_DURATION_SECONDS = 4 * 3600 + 55 * 60  # 4 hours 55 minutes
IST_OFFSET = timedelta(hours=5, minutes=30)

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
        state["sent_ids"] = state["sent_ids"][-500:]
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"Error saving state file: {e}")

def send_telegram_msg(text, image_url=None):
    if image_url:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "photo": image_url, "caption": text, "parse_mode": "HTML"}
        try:
            res = requests.post(url, data=payload, timeout=10)
            if res.status_code == 200:
                return True
        except Exception as e:
            print(f"Image send failed: {e}")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        res = requests.post(url, data=payload, timeout=10)
        return res.status_code == 200
    except Exception as e:
        print(f"Telegram text send error: {e}")
        return False

def process_general_news(state, now_ts):
    try:
        url = f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_KEY}"
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            articles = res.json()
            for item in articles:
                article_id = str(item.get("id") or item.get("url"))
                pub_time = item.get("datetime", 0)

                if article_id in state["sent_ids"]:
                    continue
                if (now_ts - pub_time) > MAX_AGE_SECONDS:
                    continue

                headline = item.get("headline", "")
                summary = item.get("summary", "")
                ist_time = (datetime.fromtimestamp(pub_time, tz=timezone.utc) + IST_OFFSET).strftime("%d %b %Y, %I:%M %p IST")

                message = (
                    f"🌐 <b>GENERAL NEWS ALERT</b> 🌐\n\n"
                    f"• <b>{headline}</b>\n"
                )
                if summary and len(summary) > 20:
                    message += f"• {summary[:250]}...\n"

                message += (
                    f"\n<b>Released Time:</b> {ist_time}\n"
                    f"<b>Link:</b> {item.get('url', 'N/A')}"
                )

                image_url = item.get("image") if item.get("image") else None
                if send_telegram_msg(message, image_url):
                    state["sent_ids"].append(article_id)
                    save_state(state)
    except Exception as e:
        print(f"Error fetching General News: {e}")

def main():
    print("Starting Continuous General News Daemon...")
    state = load_state()

    # Seed current news silently on boot so old articles aren't re-sent
    try:
        res = requests.get(f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_KEY}", timeout=10)
        if res.status_code == 200:
            for item in res.json():
                art_id = str(item.get("id") or item.get("url"))
                if art_id not in state["sent_ids"]:
                    state["sent_ids"].append(art_id)
        save_state(state)
    except Exception:
        pass

    print("Startup seed complete. Listening for live general news...")

    start_time = time.time()
    while (time.time() - start_time) < LOOP_DURATION_SECONDS:
        try:
            current_ts = time.time()
            process_general_news(state, current_ts)
        except Exception as e:
            print(f"Loop error: {e}")
        time.sleep(3)

    print("4h 55m continuous run completed cleanly.")

if __name__ == "__main__":
    main()
