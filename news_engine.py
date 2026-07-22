import os
import json
import time
import re
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

STATE_FILE = "state.json"

# ---- API Keys & Telegram Secrets ----
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")
EIA_KEY = os.getenv("EIA_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TG_SEND_MESSAGE = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
TG_SEND_PHOTO = f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto"

IST = timezone(timedelta(hours=5, minutes=30))

# Hard 2-minute recency window: Only picks up items released RIGHT NOW
RECENCY_WINDOW_MIN = 2  

# Loop polling interval (seconds)
POLL_INTERVAL_SECONDS = 3 

# Max execution time per GitHub Actions run: 5 hours (18,000 seconds)
MAX_RUN_TIME_SECONDS = 5 * 3600 

# ---- Asset & Geopolitical Filters ----
INSTRUMENT_KEYWORDS = {
    "XAUUSD": ["gold", "xau", "bullion", "precious metal", "safe haven"],
    "EURUSD": ["eur", "euro", "ecb", "lagarde", "eurozone"],
    "NZDUSD": ["nzd", "rbnz", "new zealand"],
    "USOIL":  ["oil", "wti", "crude", "opec", "petroleum", "energy market"],
    "US500":  ["s&p 500", "us500", "spx", "fed", "fomc", "powell", "wall street", "us stocks", "nasdaq", "cpi", "nfp", "unemployment", "gdp"],
}

GEOPOLITICAL_KEYWORDS = [
    "war", "iran", "missile", "attack", "sanctions", "trump", 
    "military", "tariff", "conflict", "strait of hormuz", "escalation"
]

CURRENCY_FILTER = ["USD", "EUR", "NZD"]


# ============================================================
# STATE & DATE TRACKING
# ============================================================

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                if "sent_ids" not in data:
                    data["sent_ids"] = []
                if "last_date" not in data:
                    data["last_date"] = ""
                return data
        except Exception as e:
            print("State load error:", e)
    return {"sent_ids": [], "last_date": ""}


def save_state(state):
    try:
        state["sent_ids"] = state.get("sent_ids", [])[-500:]
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print("State save error:", e)


def check_and_send_date_header(state: dict):
    """Sends --------------------DD/MM/YYYY-------------------- when day rolls over in IST."""
    now_ist = datetime.now(IST)
    current_date_str = now_ist.strftime("%d/%m/%Y")
    
    if state.get("last_date") != current_date_str:
        header_msg = f"--------------------{current_date_str}--------------------"
        if send_telegram(header_msg):
            state["last_date"] = current_date_str
            save_state(state)


# ============================================================
# HELPERS
# ============================================================

def to_ist_string(dt_utc: datetime) -> str:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    ist_time = dt_utc.astimezone(IST)
    return ist_time.strftime("%d %b %Y, %I:%M %p IST")


def extract_key_points(headline: str, summary: str) -> str:
    full_text = f"{headline}. {summary}".strip()
    full_text = re.sub(r'<[^>]+>', '', full_text)
    sentences = re.split(r'(?<=[.!?]) +', full_text)
    
    extracted = []
    for s in sentences:
        s_clean = s.strip()
        if len(s_clean) < 15:
            continue
        if any(char.isdigit() for char in s_clean) or any(kw in s_clean.lower() for kw in [
            "said", "announced", "reported", "rose", "fell", "dropped", "surged", 
            "cut", "hike", "war", "rate", "data", "cpi", "nfp", "gdp", "fomc"
        ]):
            extracted.append(f"• {s_clean}")
        elif len(extracted) < 2:
            extracted.append(f"• {s_clean}")

    if not extracted:
        extracted = [f"• {headline}"]

    return "\n".join(extracted[:3])


# ============================================================
# TELEGRAM MESSAGING
# ============================================================

def send_telegram(message: str) -> bool:
    try:
        resp = requests.post(
            TG_SEND_MESSAGE,
            data={
                "chat_id": TG_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=5,
        )
        return resp.status_code == 200
    except Exception as e:
        print("Telegram send error:", e)
        return False


def send_telegram_with_image(caption: str, image_url: str) -> bool:
    if not image_url:
        return send_telegram(caption)

    try:
        resp = requests.post(
            TG_SEND_PHOTO,
            data={
                "chat_id": TG_CHAT_ID,
                "photo": image_url,
                "caption": caption,
                "parse_mode": "HTML",
            },
            timeout=7,
        )
        if resp.status_code == 200:
            return True
    except Exception:
        pass

    return send_telegram(caption)


# ============================================================
# REAL-TIME DATA FETCHERS
# ============================================================

def fetch_finnhub_calendar(state: dict):
    if not FINNHUB_KEY:
        return
    today = datetime.utcnow().strftime("%Y-%m-%d")
    tomorrow = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
    url = "https://finnhub.io/api/v1/calendar/economic"
    
    try:
        resp = requests.get(url, params={"from": today, "to": tomorrow, "token": FINNHUB_KEY}, timeout=5)
        events = resp.json().get("economicCalendar", [])
    except Exception:
        return

    now = datetime.utcnow()

    for ev in events:
        currency = ev.get("currency", "")
        actual = ev.get("actual")

        if currency not in CURRENCY_FILTER or actual is None:
            continue

        event_time_str = ev.get("time")
        try:
            event_time = datetime.strptime(event_time_str, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue

        age_minutes = (now - event_time).total_seconds() / 60
        if age_minutes > RECENCY_WINDOW_MIN or age_minutes < -1:
            continue

        event_id = f"cal_{ev.get('event')}_{event_time_str}_{actual}"
        if event_id in state["sent_ids"]:
            continue

        check_and_send_date_header(state)

        ist_str = to_ist_string(event_time)
        estimate = ev.get("estimate", "N/A")
        prev = ev.get("prev", "N/A")

        market_map = {"USD": "XAUUSD, EURUSD, NZDUSD, US500", "EUR": "EURUSD", "NZD": "NZDUSD"}
        market_name = market_map.get(currency, currency)

        msg = (
            f"Market: {market_name}\n"
            f"News Content:\n• Major Economic Data Release: {ev.get('event')}\n"
            f"Data Comparison: Actual: {actual} | Forecast: {estimate} | Previous: {prev}\n"
            f"Released Time: {ist_str}\n"
            f"Main News Link: https://finnhub.io"
        )

        if send_telegram(msg):
            state["sent_ids"].append(event_id)
            save_state(state)


def fetch_finnhub_news(state: dict):
    if not FINNHUB_KEY:
        return
    url = "https://finnhub.io/api/v1/news"
    
    try:
        resp = requests.get(url, params={"category": "forex", "token": FINNHUB_KEY}, timeout=5)
        articles = resp.json()
    except Exception:
        return

    now_ts = time.time()

    for art in articles:
        published_ts = art.get("datetime")
        if not published_ts:
            continue

        age_minutes = (now_ts - published_ts) / 60
        if age_minutes > RECENCY_WINDOW_MIN or age_minutes < -1:
            continue

        article_url = art.get("url", "")
        article_id = f"news_{art.get('id', article_url)}"

        if article_id in state["sent_ids"]:
            continue

        headline = art.get("headline", "").strip()
        summary = art.get("summary", "").strip()
        text_for_match = f"{headline} {summary}".lower()

        matched_markets = [
            instr for instr, kws in INSTRUMENT_KEYWORDS.items()
            if any(kw in text_for_match for kw in kws)
        ]

        if any(g_kw in text_for_match for g_kw in GEOPOLITICAL_KEYWORDS):
            for geo_asset in ["XAUUSD", "USOIL", "US500"]:
                if geo_asset not in matched_markets:
                    matched_markets.append(geo_asset)

        if not matched_markets:
            continue

        check_and_send_date_header(state)

        published_dt = datetime.utcfromtimestamp(published_ts)
        ist_str = to_ist_string(published_dt)
        key_points = extract_key_points(headline, summary)
        markets_str = ", ".join(matched_markets)

        caption = (
            f"Market: {markets_str}\n"
            f"News Content:\n{key_points}\n"
            f"Released Time: {ist_str}\n"
            f"Main News Link: {article_url}"
        )

        image_url = art.get("image")
        if send_telegram_with_image(caption, image_url):
            state["sent_ids"].append(article_id)
            save_state(state)


def fetch_eia_oil_data(state: dict):
    if not EIA_KEY:
        return
    url = "https://api.eia.gov/v2/petroleum/stoc/wstk/data/"
    params = {
        "api_key": EIA_KEY,
        "frequency": "weekly",
        "data[0]": "value",
        "facets[series][]": "WCESTUS1",
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "length": 1,
    }

    try:
        resp = requests.get(url, params=params, timeout=5)
        rows = resp.json().get("response", {}).get("data", [])
    except Exception:
        return

    if not rows:
        return

    latest = rows[0]
    period = latest.get("period")
    value = latest.get("value")
    units = latest.get("units", "Thousand Barrels")

    record_id = f"eia_{period}_{value}"
    if record_id in state["sent_ids"]:
        return

    check_and_send_date_header(state)
    now_ist = to_ist_string(datetime.utcnow())

    msg = (
        f"Market: USOIL\n"
        f"News Content:\n• EIA Official Weekly Crude Oil Stocks Report\n"
        f"Data Comparison: Ending Stocks: {value:,} {units} (Period: {period})\n"
        f"Released Time: {now_ist}\n"
        f"Main News Link: https://www.eia.gov"
    )

    if send_telegram(msg):
        state["sent_ids"].append(record_id)
        save_state(state)


# ============================================================
# CONTINUOUS WORKER LOOP (5-HOUR CAP PER GITHUB ACTION RUN)
# ============================================================

def main():
    print("🚀 CONTINUOUS LIVE STREAM STARTED ON GITHUB RUNNER...")
    start_time = time.time()
    state = load_state()

    while True:
        elapsed = time.time() - start_time
        if elapsed >= MAX_RUN_TIME_SECONDS:
            print("⏱️ 5-Hour session cap reached. Exiting cleanly for next scheduled job...")
            break

        try:
            fetch_finnhub_calendar(state)
            fetch_finnhub_news(state)
            fetch_eia_oil_data(state)
        except Exception as e:
            print("Loop error:", e)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
