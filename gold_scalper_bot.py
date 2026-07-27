"""
Gold (XAU/USD) 5-minute scalping signal bot
Runs as a Web Service (free tier) with a keep-alive endpoint.
"""

import time
import threading
import requests
import pandas as pd
from datetime import datetime, timezone
from flask import Flask

# ============ CONFIG ============
TWELVEDATA_API_KEY = "400531b0f15a4c98a2b0401ab23a8d86"
TELEGRAM_BOT_TOKEN = "8617888671:AAFypwwi1PYwXRFRfXYELr0LnstAcfd4yhI"
TELEGRAM_CHAT_ID = "8449979307"

SYMBOL = "XAU/USD"
INTERVAL = "5min"
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
ATR_PERIOD = 14
RISK_REWARD_RATIO = 2.0
POLL_SECONDS = 60
RSI_BUY_THRESHOLD = 55
RSI_SELL_THRESHOLD = 45
# ==================================================

TD_URL = "https://api.twelvedata.com/time_series"
TELEGRAM_SEND_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
TELEGRAM_UPDATES_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"

last_sent_candle_time = None
last_update_id = None

app = Flask(__name__)

@app.route("/")
def home():
    return "Gold scalper bot is running."


def fetch_candles(outputsize=100):
    params = {
        "symbol": SYMBOL, "interval": INTERVAL, "outputsize": outputsize,
        "apikey": TWELVEDATA_API_KEY, "order": "ASC",
    }
    r = requests.get(TD_URL, params=params, timeout=15)
    data = r.json()
    if "values" not in data:
        raise RuntimeError(f"TwelveData error: {data}")
    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    return df.sort_values("datetime").reset_index(drop=True)


def compute_indicators(df):
    df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(RSI_PERIOD).mean()
    avg_loss = loss.rolling(RSI_PERIOD).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    df["rsi"] = 100 - (100 / (1 + rs))

    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr"] = tr.rolling(ATR_PERIOD).mean()
    return df


def get_call(df):
    curr = df.iloc[-1]
    price, atr, rsi = curr["close"], curr["atr"], curr["rsi"]
    trend_up = curr["ema_fast"] > curr["ema_slow"]

    if trend_up and rsi > RSI_BUY_THRESHOLD:
        direction = "BUY"
        sl, tp = price - atr, price + atr * RISK_REWARD_RATIO
    elif not trend_up and rsi < RSI_SELL_THRESHOLD:
        direction = "SELL"
        sl, tp = price + atr, price - atr * RISK_REWARD_RATIO
    else:
        direction = "WAIT"
        sl = tp = None

    return {
        "direction": direction, "price": price, "sl": sl, "tp": tp,
        "rsi": rsi, "trend": "Bullish" if trend_up else "Bearish",
        "time": curr["datetime"],
    }


def send_message(text):
    requests.post(TELEGRAM_SEND_URL, data={
        "chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown",
    }, timeout=15)


def format_call(call):
    if call["direction"] == "WAIT":
        return (
            f"⚪ *WAIT — XAU/USD (5m)*\n\n"
            f"Price: `{call['price']:.2f}`\n"
            f"RSI: `{call['rsi']:.1f}`\n"
            f"Trend: {call['trend']}\n"
            f"No clean entry this candle.\n"
            f"Candle: `{call['time']}`"
        )
    emoji = "🟢" if call["direction"] == "BUY" else "🔴"
    return (
        f"{emoji} *{call['direction']} — XAU/USD (5m)*\n\n"
        f"Entry: `{call['price']:.2f}`\n"
        f"Stop Loss: `{call['sl']:.2f}`\n"
        f"Take Profit: `{call['tp']:.2f}`\n"
        f"RSI: `{call['rsi']:.1f}`\n"
        f"Trend: {call['trend']}\n"
        f"R:R — 1:{RISK_REWARD_RATIO:.0f}\n"
        f"Candle: `{call['time']}`"
    )


def check_for_commands():
    global last_update_id
    params = {"timeout": 0}
    if last_update_id is not None:
        params["offset"] = last_update_id + 1
    r = requests.get(TELEGRAM_UPDATES_URL, params=params, timeout=15)
    data = r.json()
    if not data.get("ok"):
        return
    for update in data.get("result", []):
        last_update_id = update["update_id"]
        text = update.get("message", {}).get("text", "").strip().lower()
        if text == "/signal":
            try:
                df = compute_indicators(fetch_candles())
                send_message(format_call(get_call(df)))
            except Exception as e:
                send_message(f"Error: {e}")


def bot_loop():
    global last_sent_candle_time
    print("Gold scalper bot started...")
    while True:
        try:
            check_for_commands()
            df = compute_indicators(fetch_candles())
            call = get_call(df)

            if call["time"] != last_sent_candle_time:
                send_message(format_call(call))
                last_sent_candle_time = call["time"]
                print(f"[{datetime.now(timezone.utc)}] Sent {call['direction']}")

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    import os
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
