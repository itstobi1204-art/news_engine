import os
import requests

FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_msg(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    res = requests.post(url, data=payload)
    if res.status_code == 200:
        print("✅ Message successfully sent to Telegram!")
    else:
        print(f"❌ Telegram Error: {res.status_code} - {res.text}")

def main():
    print("Fetching general news from Finnhub...")
    url = f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_KEY}"
    res = requests.get(url)
    
    if res.status_code == 200:
        articles = res.json()
        if not articles:
            print("No articles returned from Finnhub.")
            return
            
        latest = articles[0]
        msg = (
            f"🚨 <b>TEST NEWS ALERT</b> 🚨\n\n"
            f"<b>Headline:</b> {latest.get('headline')}\n"
            f"<b>Category:</b> {latest.get('category')}\n"
            f"<b>URL:</b> {latest.get('url')}\n\n"
            f"<i>If you received this, your Finnhub and Telegram API connections are working perfectly!</i>"
        )
        print(f"Found Article: {latest.get('headline')}\nSending to Telegram...")
        send_telegram_msg(msg)
    else:
        print(f"❌ Finnhub API Error: {res.status_code}")

if __name__ == "__main__":
    main()
