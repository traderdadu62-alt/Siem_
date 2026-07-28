"""
Gold (XAU/USD) 5-minute SMC scalping bot
------------------------------------------
Detects: liquidity sweeps + fair value gaps (FVG), filtered by EMA trend.
Sends signals to Telegram with inline buttons (Taken / Skip / Details).
"""

import time
import threading
import requests
import pandas as pd
from datetime import datetime, timezone
from flask import Flask

TWELVEDATA_API_KEY = "400531b0f15a4c98a2b0401ab23a8d86"
TELEGRAM_BOT_TOKEN = "8617888671:AAFypwwi1PYwXRFRfXYELr0LnstAcfd4yhI"
TELEGRAM_CHAT_ID = "8449979307"

SYMBOL = "XAU/USD"
INTERVAL = "5min"
EMA_FAST = 9
EMA_SLOW = 21
ATR_PERIOD = 14
RISK_REWARD_RATIO = 2.0
POLL_SECONDS = 60
SWING_LOOKBACK = 10

TD_URL = "https://api.twelvedata.com/time_series"
TG_SEND = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
TG_ANSWER = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
TG_UPDATES = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"

last_sent_candle_time = None
last_update_id = None

app = Flask(__name__)


@app.route("/")
def home():
    return "SMC gold scalper bot is running."


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
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr"] = tr.rolling(ATR_PERIOD).mean()
    return df


def find_swing_levels(df, lookback):
    window = df.iloc[-(lookback + 1):-1]
    return window["high"].max(), window["low"].min()


def detect_liquidity_sweep(df, swing_high, swing_low):
    curr = df.iloc[-1]
    swept_high = curr["high"] > swing_high and curr["close"] < swing_high
    swept_low = curr["low"] < swing_low and curr["close"] > swing_low
    if swept_low:
        return "bullish"
    if swept_high:
        return "bearish"
    return None


def detect_fvg(df):
    if len(df) < 4:
        return None
    c1, c2, c3 = df.iloc[-4], df.iloc[-3], df.iloc[-2]
    if c3["low"] > c1["high"]:
        return "bullish"
    if c3["high"] < c1["low"]:
        return "bearish"
    return None


def get_call(df):
    curr = df.iloc[-1]
    price, atr = curr["close"], curr["atr"]
    trend_up = curr["ema_fast"] > curr["ema_slow"]

    swing_high, swing_low = find_swing_levels(df, SWING_LOOKBACK)
    sweep = detect_liquidity_sweep(df, swing_high, swing_low)
    fvg = detect_fvg(df)

    direction = "WAIT"
    sl = tp = None
    reason = "No sweep + FVG alignment this candle."

    if sweep == "bullish" and fvg == "bullish" and trend_up:
        direction = "BUY"
        sl = min(curr["low"], swing_low) - atr * 0.2
        tp = price + (price - sl) * RISK_REWARD_RATIO
        reason = "Liquidity sweep below swing low + bullish FVG, trend up."
    elif sweep == "bearish" and fvg == "bearish" and not trend_up:
        direction = "SELL"
        sl = max(curr["high"], swing_high) + atr * 0.2
        tp = price - (sl - price) * RISK_REWARD_RATIO
        reason = "Liquidity sweep above swing high + bearish FVG, trend down."

    return {
        "direction": direction, "price": price, "sl": sl, "tp": tp,
        "trend": "Bullish" if trend_up else "Bearish",
        "sweep": sweep or "none", "fvg": fvg or "none",
        "reason": reason, "time": curr["datetime"],
    }


def format_call(call):
    if call["direction"] == "WAIT":
        return (
            f"⚪ *WAIT — XAU/USD (5m)*\n\n"
            f"Price: `{call['price']:.2f}`\n"
            f"Trend: {call['trend']}\n"
            f"Sweep: {call['sweep']} | FVG: {call['fvg']}\n"
            f"{call['reason']}\n"
            f"Candle: `{call['time']}`"
        )
    emoji = "🟢" if call["direction"] == "BUY" else "🔴"
    return (
        f"{emoji} *{call['direction']} — XAU/USD (5m) — SMC*\n\n"
        f"Entry: `{call['price']:.2f}`\n"
        f"Stop Loss: `{call['sl']:.2f}`\n"
        f"Take Profit: `{call['tp']:.2f}`\n"
        f"Trend: {call['trend']}\n"
        f"R:R — 1:{RISK_REWARD_RATIO:.0f}\n"
        f"Reason: {call['reason']}\n"
        f"Candle: `{call['time']}`"
    )


def build_keyboard():
    return {
        "inline_keyboard": [[
            {"text": "✅ Taken", "callback_data": "taken"},
            {"text": "⏭ Skip", "callback_data": "skip"},
            {"text": "📊 Details", "callback_data": "details"},
        ]]
    }


def send_message(text, with_buttons=False):
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    if with_buttons:
        import json
        payload["reply_markup"] = json.dumps(build_keyboard())
    requests.post(TG_SEND, data=payload, timeout=15)


def answer_callback(callback_id, text):
    requests.post(TG_ANSWER, data={"callback_query_id": callback_id, "text": text}, timeout=15)


def check_for_commands():
    global last_update_id
    params = {"timeout": 0}
    if last_update_id is not None:
        params["offset"] = last_update_id + 1
    r = requests.get(TG_UPDATES, params=params, timeout=15)
    data = r.json()
    if not data.get("ok"):
        return
    for update in data.get("result", []):
        last_update_id = update["update_id"]
        if "callback_query" in update:
            cq = update["callback_query"]
            action = cq.get("data")
            responses = {
                "taken": "Logged as taken ✅ — track it and check back later.",
                "skip": "Skipped ⏭",
                "details": "This signal used a liquidity sweep + FVG on 5m gold, filtered by EMA trend.",
            }
            answer_callback(cq["id"], responses.get(action, "OK"))
            continue
        text = update.get("message", {}).get("text", "").strip().lower()
        if text == "/signal":
            try:
                df = compute_indicators(fetch_candles())
                call = get_call(df)
                send_message(format_call(call), with_buttons=(call["direction"] != "WAIT"))
            except Exception as e:
                send_message(f"Error: {e}")


def bot_loop():
    global last_sent_candle_time
    print("SMC gold scalper bot started...", flush=True)
    while True:
        try:
            check_for_commands()
            df = compute_indicators(fetch_candles())
            call = get_call(df)
            if call["time"] != last_sent_candle_time:
                send_message(format_call(call), with_buttons=(call["direction"] != "WAIT"))
                last_sent_candle_time = call["time"]
                print(f"[{datetime.now(timezone.utc)}] Sent {call['direction']}", flush=True)
        except Exception as e:
            print(f"Error: {e}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    import os
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
