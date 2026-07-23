import os
import time
import json
import requests
from datetime import datetime, timezone, timedelta

# Environment Variables
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")
EIA_KEY = os.getenv("EIA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STATE_FILE = "state.json"

# Timing constants
MAX_AGE_SECONDS = 3 * 60  # 3 minutes for regular breaking news
CALENDAR_MAX_AGE = 2 * 3600  # 2 hours for Economic Calendar (to bypass Finnhub's data delay)
LOOP_DURATION_SECONDS = 4 * 3600 + 55 * 60 
IST_OFFSET = timedelta(hours=5, minutes=30)

# Asset Tagging Keywords
KEYWORD_MAP = {
    "XAUUSD": ["gold", "bullion", "precious metal", "safe haven", "xau"],
    "USOIL": ["oil", "crude", "wti", "brent", "opec", "eia", "petroleum", "energy"],
    "US500": ["s&p", "sp500", "stocks", "wall street", "fed", "powell", "rate", "inflation", "cpi", "nfp", "trump", "sanction", "tariff", "white house"],
    "EURUSD": ["euro", "ecb", "lagarde", "eur"],
    "NZDUSD": ["nzd", "rbnz", "new zealand"]
}

HIGH_VOLATILITY_GEOPOLITICAL = ["war", "iran", "missile", "attack", "sanction", "military", "tariff", "conflict", "trump", "strait of hormuz", "breaking"]

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                return {
                    "sent_ids": data.get("sent_ids", []),
                    "last_date_sent": data.get("last_date_sent", ""),
                    "sent_eia_periods": data.get("sent_eia_periods", [])
                }
        except Exception as e:
            print(f"Error loading state file: {e}")
    return {"sent_ids": [], "last_date_sent": "", "sent_eia_periods": []}

def save_state(state):
    try:
        state["sent_ids"] = state["sent_ids"][-500:]
        state["sent_eia_periods"] = state["sent_eia_periods"][-50:]
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

def ensure_daily_date_header(state):
    now_ist = datetime.now(timezone.utc) + IST_OFFSET
    today_str = now_ist.strftime("%d/%m/%Y")
    
    if state.get("last_date_sent") != today_str:
        date_banner = f"--------------------{today_str}--------------------"
        if send_telegram_msg(date_banner):
            state["last_date_sent"] = today_str
            save_state(state)

def process_finnhub_news(state, now_ts):
    articles = []
    for cat in ["general", "forex"]:
        try:
            url = f"https://finnhub.io/api/v1/news?category={cat}&token={FINNHUB_KEY}"
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                articles.extend(res.json())
        except Exception as e:
            print(f"Error fetching Finnhub {cat}: {e}")

    for item in articles:
        article_id = str(item.get("id") or item.get("url"))
        pub_time = item.get("datetime", 0)

        if article_id in state["sent_ids"]:
            continue
        if (now_ts - pub_time) > MAX_AGE_SECONDS:
            continue

        headline = item.get("headline", "")
        summary = item.get("summary", "")
        full_text = f"{headline} {summary}".lower()

        matched_markets = []
        for symbol, keywords in KEYWORD_MAP.items():
            if any(kw in full_text for kw in keywords):
                matched_markets.append(symbol)

        if any(kw in full_text for kw in HIGH_VOLATILITY_GEOPOLITICAL):
            for geo_symbol in ["XAUUSD", "USOIL", "US500"]:
                if geo_symbol not in matched_markets:
                    matched_markets.append(geo_symbol)

        if matched_markets or any(kw in full_text for kw in HIGH_VOLATILITY_GEOPOLITICAL):
            ensure_daily_date_header(state)
            ist_time = (datetime.fromtimestamp(pub_time, tz=timezone.utc) + IST_OFFSET).strftime("%d %b %Y, %I:%M %p IST")
            markets_str = ", ".join(matched_markets) if matched_markets else "GENERAL MACRO"

            message = (
                f"<b>Market:</b> {markets_str}\n"
                f"<b>News Content:</b>\n"
                f"• {headline}\n"
            )
            if summary and len(summary) > 20:
                message += f"• {summary[:250]}...\n"

            message += (
                f"<b>Released Time:</b> {ist_time}\n"
                f"<b>Main News Link:</b> {item.get('url', 'N/A')}"
            )

            image_url = item.get("image") if item.get("image") else None
            if send_telegram_msg(message, image_url):
                state["sent_ids"].append(article_id)
                save_state(state)

def process_economic_calendar(state):
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    url = f"https://finnhub.io/api/v1/calendar/economic?from={today_str}&to={today_str}&token={FINNHUB_KEY}"
    
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            events = res.json().get("economicCalendar", [])
            for event in events:
                event_id = f"cal_{event.get('country')}_{event.get('event')}_{event.get('time')}"
                
                if event_id in state["sent_ids"] or event.get("actual") is None:
                    continue
                
                event_time_str = event.get("time", "")
                if event_time_str:
                    event_dt = datetime.strptime(event_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    age = (now_utc - event_dt).total_seconds()
                    
                    # FIX 1: Allow up to 2 hours for Finnhub's API to update the 'actual' number.
                    if age > CALENDAR_MAX_AGE or age < -300:
                        continue

                country = event.get('country', '')
                # FIX 2: Added 'EA' and 'EMU' because Finnhub doesn't strictly use 'EU'.
                if country not in ['US', 'EU', 'EA', 'EMU', 'NZ']:
                    continue
                
                markets = []
                if country == 'US': markets.extend(["XAUUSD", "EURUSD", "NZDUSD", "US500"])
                if country in ['EU', 'EA', 'EMU']: markets.append("EURUSD")
                if country == 'NZ': markets.append("NZDUSD")

                ensure_daily_date_header(state)
                ist_time = (now_utc + IST_OFFSET).strftime("%d %b %Y, %I:%M %p IST")
                markets_str = ", ".join(list(set(markets)))
                
                msg = (
                    f"<b>Market:</b> {markets_str}\n"
                    f"<b>News Content:</b>\n"
                    f"• Major Economic Data Release: {event.get('event')}\n"
                    f"<b>Data Comparison:</b> Actual: {event.get('actual')} | Forecast: {event.get('estimate')} | Previous: {event.get('prev')}\n"
                    f"<b>Released Time:</b> {ist_time}\n"
                    f"<b>Main News Link:</b> https://finnhub.io"
                )
                if send_telegram_msg(msg):
                    state["sent_ids"].append(event_id)
                    save_state(state)
    except Exception as e:
        print(f"Calendar Fetch Error: {e}")

def process_eia_data(state):
    url = f"https://api.eia.gov/v2/petroleum/stoc/wstk/data/?api_key={EIA_KEY}&frequency=weekly&data[0]=value&facets[series][]=WCRSTUS1&sort[0][column]=period&sort[0][direction]=desc&length=1"
    
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            data_list = res.json().get("response", {}).get("data", [])
            if not data_list:
                return
            
            latest = data_list[0]
            period = latest.get("period")
            value = latest.get("value")
            
            if not period:
                return

            try:
                period_dt = datetime.strptime(period, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                now_utc = datetime.now(timezone.utc)
                if (now_utc - period_dt).days > 7:
                    if period not in state["sent_eia_periods"]:
                        state["sent_eia_periods"].append(period)
                        save_state(state)
                    return
            except Exception:
                pass

            if period not in state.get("sent_eia_periods", []):
                ensure_daily_date_header(state)
                now_ist = datetime.now(timezone.utc) + IST_OFFSET
                ist_time = now_ist.strftime("%d %b %Y, %I:%M %p IST")
                
                msg = (
                    f"<b>Market:</b> USOIL\n"
                    f"<b>News Content:</b>\n"
                    f"• EIA Official Weekly Crude Oil Stocks Report\n"
                    f"<b>Data Comparison:</b> Ending Stocks: {value} Thousand Barrels (Period: {period})\n"
                    f"<b>Released Time:</b> {ist_time}\n"
                    f"<b>Main News Link:</b> https://www.eia.gov"
                )
                if send_telegram_msg(msg):
                    state["sent_eia_periods"].append(period)
                    save_state(state)
    except Exception as e:
        print(f"EIA Fetch Error: {e}")

def main():
    print("Starting Live News Engine...")
    state = load_state()

    # Silent startup seed for Breaking News
    try:
        for cat in ["general", "forex"]:
            res = requests.get(f"https://finnhub.io/api/v1/news?category={cat}&token={FINNHUB_KEY}", timeout=10)
            if res.status_code == 200:
                for item in res.json():
                    art_id = str(item.get("id") or item.get("url"))
                    if art_id not in state["sent_ids"]:
                        state["sent_ids"].append(art_id)
    except Exception:
        pass
        
    # Silent startup seed for Economic Calendar
    try:
        now_utc = datetime.now(timezone.utc)
        today_str = now_utc.strftime("%Y-%m-%d")
        cal_url = f"https://finnhub.io/api/v1/calendar/economic?from={today_str}&to={today_str}&token={FINNHUB_KEY}"
        res = requests.get(cal_url, timeout=10)
        if res.status_code == 200:
            for event in res.json().get("economicCalendar", []):
                event_id = f"cal_{event.get('country')}_{event.get('event')}_{event.get('time')}"
                if event_id not in state["sent_ids"] and event.get("actual") is not None:
                    event_time_str = event.get("time", "")
                    if event_time_str:
                        event_dt = datetime.strptime(event_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                        # Seed only if it's older than 10 minutes so we don't accidentally swallow a live drop
                        if (now_utc - event_dt).total_seconds() > 600:
                            state["sent_ids"].append(event_id)
    except Exception:
        pass

    try:
        eia_url = f"https://api.eia.gov/v2/petroleum/stoc/wstk/data/?api_key={EIA_KEY}&frequency=weekly&data[0]=value&facets[series][]=WCRSTUS1&sort[0][column]=period&sort[0][direction]=desc&length=1"
        res = requests.get(eia_url, timeout=10)
        if res.status_code == 200:
            data_list = res.json().get("response", {}).get("data", [])
            if data_list:
                p = data_list[0].get("period")
                if p and p not in state["sent_eia_periods"]:
                    state["sent_eia_periods"].append(p)
    except Exception:
        pass

    save_state(state)
    print("Startup seed complete. Listening for live news events...")

    start_time = time.time()
    while (time.time() - start_time) < LOOP_DURATION_SECONDS:
        try:
            current_ts = time.time()
            process_finnhub_news(state, current_ts)
            process_economic_calendar(state)
            process_eia_data(state)
        except Exception as e:
            print(f"Loop error: {e}")
        time.sleep(3)

    print("4h 55m continuous loop completed cleanly. Ready for GitHub hand-off.")

if __name__ == "__main__":
    main()
