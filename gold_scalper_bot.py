"""
Gold (XAU/USD) 5-minute SMC scalping bot — with Telegram dashboard
"""

import time
import threading
import json
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
last_call = None

stats = {"sent": 0, "buy": 0, "sell": 0, "wait": 0, "taken": 0, "skipped": 0}

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
    if curr["low"] < swing_low and curr["close"] > swing_low:
        return "bullish"
    if curr["high"] > swing_high and curr["close"] < swing_high:
        return "bearish"
    return None


def detect_fvg(df):
    if len(df) < 4:
        return None
    c1, c3 = df.iloc[-4], df.iloc[-2]
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

    direction, sl, tp = "WAIT", None, None
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


def signal_keyboard():
    return {"inline_keyboard": [[
        {"text": "✅ Taken", "callback_data": "taken"},
        {"text": "⏭ Skip", "callback_data": "skip"},
        {"text": "📊 Details", "callback_data": "details"},
    ]]}


def menu_keyboard():
    return {"inline_keyboard": [
        [{"text": "📈 Last Signal", "callback_data": "menu_last"},
         {"text": "📊 Status", "callback_data": "menu_status"}],
        [{"text": "📉 Stats", "callback_data": "menu_stats"},
         {"text": "❓ Help", "callback_data": "menu_help"}],
    ]}


def dashboard_text():
    return (
        "🏆 *Gold Scalper — SMC Dashboard*\n\n"
        "Strategy: liquidity sweep + fair value gap, filtered by EMA trend.\n"
        "Timeframe: XAU/USD 5m\n\n"
        "Choose an option below:"
    )


def send_message(text, keyboard=None):
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard)
    requests.post(TG_SEND, data=payload, timeout=15)


def answer_callback(callback_id, text):
    requests.post(TG_ANSWER, data={"callback_query_id": callback_id, "text": text}, timeout=15)


def set_bot_commands():
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands"
    commands = [
        {"command": "menu", "description": "Open the dashboard"},
        {"command": "signal", "description": "Check for a signal now"},
        {"command": "stats", "description": "View session stats"},
        {"command": "help", "description": "How this bot works"},
    ]
    requests.post(url, data={"commands": json.dumps(commands)}, timeout=15)


def handle_signal_request():
    global last_call
    try:
        df = compute_indicators(fetch_candles())
        call = get_call(df)
        last_call = call
        stats["sent"] += 1
        stats[call["direction"].lower()] += 1
        kb = signal_keyboard() if call["direction"] != "WAIT" else None
        send_message(format_call(call), keyboard=kb)
    except Exception as e:
        send_message(f"Error: {e}")


def stats_text():
    return (
        "📉 *Session Stats*\n\n"
        f"Total signals: {stats['sent']}\n"
        f"BUY: {stats['buy']} | SELL: {stats['sell']} | WAIT: {stats['wait']}\n"
        f"Marked Taken: {stats['taken']} | Skipped: {stats['skipped']}"
    )


def help_text():
    return (
        "❓ *How this works*\n\n"
        "Strategy looks for a liquidity sweep (stop-hunt past a recent "
        "swing high/low) lining up with a fair value gap, filtered by "
        "EMA trend direction. Signals are intentionally rare — quality "
        "over quantity.\n\n"
        "Commands: /menu /signal /stats /help"
    )


def handle_menu_callback(action, callback_id):
    if action == "menu_last":
        if last_call:
            answer_callback(callback_id, "Showing last signal")
            send_message(format_call(last_call))
        else:
            answer_callback(callback_id, "No signal computed yet")
    elif action == "menu_status":
        answer_callback(callback_id, "Status")
        send_message(
            "📊 *Bot Status*\n\nRunning: ✅\n"
            f"Signals sent this session: {stats['sent']}\n"
            f"Poll interval: {POLL_SECONDS}s"
        )
    elif action == "menu_stats":
        answer_callback(callback_id, "Stats")
        send_message(stats_text())
    elif action == "menu_help":
        answer_callback(callback_id, "Help")
        send_message(help_text())


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
            if action in ("menu_last", "menu_status", "menu_stats", "menu_help"):
                handle_menu_callback(action, cq["id"])
            elif action == "taken":
                stats["taken"] += 1
                answer_callback(cq["id"], "Logged as taken ✅")
            elif action == "skip":
                stats["skipped"] += 1
                answer_callback(cq["id"], "Skipped ⏭")
            elif action == "details":
                answer_callback(cq["id"], "Sweep + FVG + EMA trend alignment — see /help")
            continue

        text = update.get("message", {}).get("text", "").strip().lower()
        if text in ("/start", "/menu"):
            send_message(dashboard_text(), keyboard=menu_keyboard())
        elif text == "/signal":
            handle_signal_request()
        elif text == "/stats":
            send_message(stats_text())
        elif text == "/help":
            send_message(help_text())


def bot_loop():
    global last_sent_candle_time, last_call
    print("SMC gold scalper bot started...", flush=True)
    set_bot_commands()
    while True:
        try:
            check_for_commands()
            df = compute_indicators(fetch_candles())
            call = get_call(df)
            last_call = call

            if call["time"] != last_sent_candle_time:
                stats["sent"] += 1
                stats[call["direction"].lower()] += 1
                kb = signal_keyboard() if call["direction"] != "WAIT" else None
                send_message(format_call(call), keyboard=kb)
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
