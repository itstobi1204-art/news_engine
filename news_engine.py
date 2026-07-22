import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

# ---- Persistent state file (survives between GitHub Actions runs) ----
STATE_FILE = "state.json"


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                if "sent_ids" not in data:
                    data["sent_ids"] = []
                return data
        except Exception as e:
            print("State load error:", e)
    return {"sent_ids": []}


def save_state(state):
    try:
        # Keep only the last 200 IDs to avoid endless file growth
        state["sent_ids"] = state.get("sent_ids", [])[-200:]
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print("State save error:", e)


# ---- API keys ----
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")
EIA_KEY = os.getenv("EIA_API_KEY")

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TG_SEND_MESSAGE = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
TG_SEND_PHOTO = f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto"

# ---- Keyword tags to filter news relevant to your instruments ----
INSTRUMENT_KEYWORDS = {
    "XAUUSD": ["gold", "xau", "precious metal"],
    "EURUSD": ["eur", "euro", "ecb", "eurozone"],
    "NZDUSD": ["nzd", "new zealand", "rbnz"],
    "USOIL":  ["oil", "wti", "crude", "opec", "petroleum"],
    "US500":  ["s&p 500", "us500", "fed", "fomc", "wall street", "us stocks", "nasdaq"],
}

CURRENCY_FILTER = ["USD", "EUR", "NZD"]

RECENCY_WINDOW_MIN = 20

IST = timezone(timedelta(hours=5, minutes=30))


def to_ist_string(dt_utc: datetime) -> str:
    """Convert a naive UTC datetime to a formatted IST string."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    ist_time = dt_utc.astimezone(IST)
    return ist_time.strftime("%d %b %Y, %I:%M %p IST")


def compact_summary(text: str, max_chars: int = 280) -> str:
    """
    Trim a headline/summary down to its essential point without cutting
    mid-sentence where possible.
    """
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text

    cut = text[:max_chars]
    for punct in [". ", "! ", "? "]:
        idx = cut.rfind(punct)
        if idx > max_chars * 0.4:
            return cut[:idx + 1].strip()

    idx = cut.rfind(" ")
    if idx > 0:
        cut = cut[:idx]
    return cut.strip() + "..."


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message: str, retries: int = 3):
    """Push a text-only message to your Telegram chat. Retries on timeout/network errors."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                TG_SEND_MESSAGE,
                data={
                    "chat_id": TG_CHAT_ID,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=20,
            )
            if resp.status_code == 200:
                return True
            print(f"Telegram send failed (attempt {attempt}):", resp.text)
        except Exception as e:
            print(f"Telegram error (attempt {attempt}):", e)
        time.sleep(2)
    print("Telegram send permanently failed after retries.")
    return False


def send_telegram_with_image(caption: str, image_url: str, retries: int = 3):
    """Push a message WITH an image if the source provided one."""
    if not image_url:
        return send_telegram(caption, retries=retries)

    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                TG_SEND_PHOTO,
                data={
                    "chat_id": TG_CHAT_ID,
                    "photo": image_url,
                    "caption": caption,
                    "parse_mode": "HTML",
                },
                timeout=20,
            )
            if resp.status_code == 200:
                return True
            print(f"Telegram photo send failed (attempt {attempt}):", resp.text)
        except Exception as e:
            print(f"Telegram photo error (attempt {attempt}):", e)
        time.sleep(2)

    print("Falling back to text-only message after image failure.")
    return send_telegram(caption, retries=1)


def impact_emoji(impact: str) -> str:
    impact = (impact or "").lower()
    if impact in ("high", "3"):
        return "🔴"
    if impact in ("medium", "2"):
        return "🟠"
    if impact in ("low", "1"):
        return "🟢"
    return "⚪"


# ============================================================
# SOURCE 1: FINNHUB - Economic Calendar
# ============================================================

def fetch_finnhub_calendar(state: dict):
    """Docs: https://finnhub.io/docs/api/economic-calendar"""
    if not FINNHUB_KEY:
        return
    today = datetime.utcnow().strftime("%Y-%m-%d")
    tomorrow = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
    url = "https://finnhub.io/api/v1/calendar/economic"
    params = {"from": today, "to": tomorrow, "token": FINNHUB_KEY}

    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
    except Exception as e:
        print("Finnhub calendar fetch error:", e)
        return

    events = data.get("economicCalendar", [])
    now = datetime.utcnow()

    for ev in events:
        currency = ev.get("currency", "")
        if currency not in CURRENCY_FILTER:
            continue

        actual = ev.get("actual")
        if actual is None:
            continue

        event_time_str = ev.get("time")
        try:
            event_time = datetime.strptime(event_time_str, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue

        age_minutes = (now - event_time).total_seconds() / 60
        if age_minutes > RECENCY_WINDOW_MIN or age_minutes < -5:
            continue

        # Prevent sending duplicates
        event_id = f"cal_{ev.get('event')}_{event_time_str}_{actual}"
        if event_id in state["sent_ids"]:
            continue

        ist_str = to_ist_string(event_time)

        msg = (
            f"{impact_emoji(ev.get('impact'))} <b>ECONOMIC DATA RELEASE</b>\n"
            f"📌 <b>{ev.get('event')}</b> ({currency})\n\n"
            f"📈 Actual: <b>{ev.get('actual')}</b> | Forecast: {ev.get('estimate', 'N/A')} | Prev: {ev.get('prev', 'N/A')}\n"
            f"🕒 {ist_str}\n\n"
            f"📌 Platform: Finnhub (Economic Calendar)"
        )
        if send_telegram(msg):
            state["sent_ids"].append(event_id)


def fetch_finnhub_news(state: dict):
    """Docs: https://finnhub.io/docs/api/market-news"""
    if not FINNHUB_KEY:
        return
    url = "https://finnhub.io/api/v1/news"
    params = {"category": "forex", "token": FINNHUB_KEY}

    try:
        resp = requests.get(url, params=params, timeout=10)
        articles = resp.json()
    except Exception as e:
        print("Finnhub news fetch error:", e)
        return

    now_ts = time.time()

    for art in articles:
        published_ts = art.get("datetime")
        if not published_ts:
            continue

        age_minutes = (now_ts - published_ts) / 60
        if age_minutes > RECENCY_WINDOW_MIN or age_minutes < -5:
            continue

        # Prevent sending duplicates
        article_id = f"news_{art.get('id', art.get('url'))}"
        if article_id in state["sent_ids"]:
            continue

        headline = art.get("headline", "")
        summary = art.get("summary", "")
        text_for_match = (headline + " " + summary).lower()

        matched = [instr for instr, kws in INSTRUMENT_KEYWORDS.items()
                   if any(kw in text_for_match for kw in kws)]
        if not matched:
            continue

        published_dt = datetime.utcfromtimestamp(published_ts)
        ist_str = to_ist_string(published_dt)
        body = compact_summary(summary or headline)
        matched_str = " ".join([f"#{m}" for m in matched])

        caption = (
            f"🚨 <b>MARKET NEWS</b>\n"
            f"🎯 Relevant to: <b>{matched_str}</b>\n\n"
            f"📰 <b>{headline}</b>\n\n"
            f"{body}\n\n"
            f"🕒 {ist_str}\n"
            f"🔗 <a href='{art.get('url')}'>Read Source Article</a>\n\n"
            f"📌 Platform: Finnhub (Market News)"
        )

        image_url = art.get("image")
        if send_telegram_with_image(caption, image_url):
            state["sent_ids"].append(article_id)


# ============================================================
# SOURCE 2: EIA.gov - Official US oil inventory data
# ============================================================

def fetch_eia_oil_data(state: dict):
    """Docs: https://www.eia.gov/opendata/"""
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
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
    except Exception as e:
        print("EIA fetch error:", e)
        return

    rows = data.get("response", {}).get("data", [])
    if not rows:
        return

    latest = rows[0]
    record_id = f"eia_{latest.get('period')}_{latest.get('value')}"
    
    if record_id in state["sent_ids"]:
        return

    now_ist = to_ist_string(datetime.utcnow())

    msg = (
        f"🛢️ <b>US OIL INVENTORY DATA</b>\n"
        f"🎯 Relevant to: <b>#USOIL</b>\n\n"
        f"📅 Period: <code>{latest.get('period')}</code>\n"
        f"📊 Ending Stocks: <b>{latest.get('value')} {latest.get('units', 'MBBL')}</b>\n\n"
        f"🕒 {now_ist}\n\n"
        f"📌 Platform: EIA.gov (Official U.S. Govt Data)"
    )
    if send_telegram(msg):
        state["sent_ids"].append(record_id)


# ============================================================
# MAIN - runs ONCE per execution
# ============================================================

def main():
    print(f"[{datetime.now()}] Running news check...")

    state = load_state()

    fetch_finnhub_calendar(state)
    fetch_finnhub_news(state)

    if datetime.utcnow().minute < 10:
        fetch_eia_oil_data(state)

    save_state(state)
    print(f"[{datetime.now()}] Check complete.")


if __name__ == "__main__":
    main()

