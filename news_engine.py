"""
News AI Engine - pulls official economic calendar + market news from
THREE data sources and pushes formatted alerts to Telegram.

Instruments tracked: XAUUSD, EURUSD, NZDUSD, USOIL, US500

Data sources:
1. Finnhub        -> Economic calendar (NFP, CPI, rate decisions etc) + general market news
2. Alpha Vantage   -> News + sentiment feed (secondary news source, good backup/cross-check)
3. EIA (eia.gov)   -> Official US oil inventory data (best official source for USOIL)

Every message sent to Telegram is tagged at the bottom with which
platform/API the data came from, so you always know the source.
"""

import os
import time
import requests
import schedule
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# ---- API keys (put these in your .env file) ----
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")
ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_API_KEY")
EIA_KEY = os.getenv("EIA_API_KEY")

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

# ---- Dedup trackers so we never push the same item twice ----
sent_calendar_ids = set()
sent_finnhub_news_ids = set()
sent_av_news_ids = set()
sent_eia_ids = set()

# ---- Keyword tags to filter news relevant to your instruments ----
INSTRUMENT_KEYWORDS = {
    "XAUUSD": ["gold", "xau", "precious metal"],
    "EURUSD": ["eur", "euro", "ecb", "eurozone"],
    "NZDUSD": ["nzd", "new zealand", "rbnz"],
    "USOIL":  ["oil", "wti", "crude", "opec", "eia", "petroleum"],
    "US500":  ["s&p", "us500", "fed", "fomc", "wall street", "us stocks"],
}

CURRENCY_FILTER = ["USD", "EUR", "NZD"]  # calendar events tied to these currencies matter to you


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message: str, retries: int = 3):
    """Push a message to your Telegram chat. Retries on timeout/network errors."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                TG_API,
                data={
                    "chat_id": TG_CHAT_ID,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=20,
            )
            if resp.status_code == 200:
                return
            print(f"Telegram send failed (attempt {attempt}):", resp.text)
        except Exception as e:
            print(f"Telegram error (attempt {attempt}):", e)
        time.sleep(2)  # brief pause before retry
    print("Telegram send permanently failed after retries.")


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
# SOURCE 1: FINNHUB - Economic Calendar (live actual/forecast data)
# ============================================================

def fetch_finnhub_calendar():
    """
    Docs: https://finnhub.io/docs/api/economic-calendar
    """
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
    for ev in events:
        currency = ev.get("currency", "")
        if currency not in CURRENCY_FILTER:
            continue

        event_id = f"{ev.get('event')}_{ev.get('time')}_{currency}"
        actual = ev.get("actual")

        if actual is None:
            continue
        if event_id in sent_calendar_ids:
            continue

        sent_calendar_ids.add(event_id)

        msg = (
            f"{impact_emoji(ev.get('impact'))} <b>{ev.get('impact', 'N/A').upper()} IMPACT — {currency}</b>\n"
            f"📊 Event: {ev.get('event')}\n"
            f"🕒 Time: {ev.get('time')}\n"
            f"📈 Actual: {ev.get('actual')}\n"
            f"📉 Forecast: {ev.get('estimate')}\n"
            f"📊 Previous: {ev.get('prev')}\n\n"
            f"📌 <b>Platform: Finnhub (Economic Calendar)</b>"
        )
        send_telegram(msg)


def fetch_finnhub_news():
    """
    Docs: https://finnhub.io/docs/api/market-news
    """
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

    for art in articles:
        news_id = art.get("id")
        if news_id in sent_finnhub_news_ids:
            continue

        text = (art.get("headline", "") + " " + art.get("summary", "")).lower()
        matched = [instr for instr, kws in INSTRUMENT_KEYWORDS.items()
                   if any(kw in text for kw in kws)]
        if not matched:
            continue

        sent_finnhub_news_ids.add(news_id)

        msg = (
            f"📰 <b>MARKET NEWS</b>\n"
            f"🎯 Relevant to: {', '.join(matched)}\n"
            f"📝 {art.get('headline')}\n"
            f"🔗 {art.get('url')}\n\n"
            f"📌 <b>Platform: Finnhub (Market News)</b>"
        )
        send_telegram(msg)


# ============================================================
# SOURCE 2: ALPHA VANTAGE - News feed (secondary/cross-check source)
# ============================================================

def fetch_alpha_vantage_news():
    """
    Docs: https://www.alphavantage.co/documentation/#news-sentiment
    Using topics=forex + financial_markets. Free tier: 25 requests/day,
    so this is polled far less frequently than Finnhub (see schedule at bottom).
    """
    if not ALPHA_VANTAGE_KEY:
        return
    url = "https://www.alphavantage.co/query"
    params = {
        "function": "NEWS_SENTIMENT",
        "topics": "forex,economy_macro,financial_markets",
        "apikey": ALPHA_VANTAGE_KEY,
    }

    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
    except Exception as e:
        print("Alpha Vantage fetch error:", e)
        return

    articles = data.get("feed", [])
    for art in articles:
        news_id = art.get("url")
        if not news_id or news_id in sent_av_news_ids:
            continue

        text = (art.get("title", "") + " " + art.get("summary", "")).lower()
        matched = [instr for instr, kws in INSTRUMENT_KEYWORDS.items()
                   if any(kw in text for kw in kws)]
        if not matched:
            continue

        sent_av_news_ids.add(news_id)

        msg = (
            f"📰 <b>MARKET NEWS</b>\n"
            f"🎯 Relevant to: {', '.join(matched)}\n"
            f"📝 {art.get('title')}\n"
            f"🔗 {art.get('url')}\n\n"
            f"📌 <b>Platform: Alpha Vantage (News Sentiment Feed)</b>"
        )
        send_telegram(msg)


# ============================================================
# SOURCE 3: EIA.gov - Official US oil inventory data (for USOIL)
# ============================================================

def fetch_eia_oil_data():
    """
    Docs: https://www.eia.gov/opendata/
    Series: Weekly U.S. Ending Stocks of Crude Oil (official govt data)
    """
    if not EIA_KEY:
        return
    url = "https://api.eia.gov/v2/petroleum/stoc/wstk/data/"
    params = {
        "api_key": EIA_KEY,
        "frequency": "weekly",
        "data[0]": "value",
        "facets[series][]": "WCESTUS1",  # weekly ending stocks, crude oil, US
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
    record_id = f"{latest.get('period')}_{latest.get('value')}"
    if record_id in sent_eia_ids:
        return
    sent_eia_ids.add(record_id)

    msg = (
        f"🛢️ <b>US OIL INVENTORY DATA</b>\n"
        f"🎯 Relevant to: USOIL\n"
        f"📅 Period: {latest.get('period')}\n"
        f"📊 Ending Stocks (crude oil): {latest.get('value')} {latest.get('units', '')}\n\n"
        f"📌 <b>Platform: EIA.gov (U.S. Energy Information Administration - Official)</b>"
    )
    send_telegram(msg)


# ============================================================
# SCHEDULER
# ============================================================

def job_frequent():
    """Runs every 30 sec - the two free/generous APIs."""
    print(f"[{datetime.now()}] Checking Finnhub calendar + news...")
    fetch_finnhub_calendar()
    fetch_finnhub_news()


def job_infrequent():
    """Runs every 30 min - APIs with tighter rate limits."""
    print(f"[{datetime.now()}] Checking Alpha Vantage + EIA...")
    fetch_alpha_vantage_news()
    fetch_eia_oil_data()


if __name__ == "__main__":
    print("News engine started. Press Ctrl+C to stop.")
    send_telegram(
        "✅ News engine online.\n"
        "Tracking: XAUUSD, EURUSD, NZDUSD, USOIL, US500\n"
        "Sources: Finnhub, Alpha Vantage, EIA.gov"
    )

    schedule.every(30).seconds.do(job_frequent)
    schedule.every(30).minutes.do(job_infrequent)

    job_frequent()
    job_infrequent()

    while True:
        schedule.run_pending()
        time.sleep(1)
