"""
Gold (XAU/USD) 5-minute scalping signal bot
--------------------------------------------
Strategy: EMA9/EMA21 crossover + RSI(14) filter for direction,
ATR(14) for stop-loss / take-profit sizing at a 1:2 risk-reward ratio.
"""

import time
import requests
import pandas as pd
from datetime import datetime, timezone

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
# ==================================================

TD_URL = "https://api.twelvedata.com/time_series"
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

last_signal_candle_time = None


def fetch_candles(outputsize=100):
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "outputsize": outputsize,
        "apikey": TWELVEDATA_API_KEY,
        "order": "ASC",
    }
    r = requests.get(TD_URL, params=params, timeout=15)
    data = r.json()
    if "values" not in data:
        raise RuntimeError(f"TwelveData error: {data}")
    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    df = df.sort_values("datetime").reset_index(drop=True)
    return df


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


def check_signal(df):
    if len(df) < max(EMA_SLOW, RSI_PERIOD, ATR_PERIOD) + 2:
        return None

    prev, curr = df.iloc[-2], df.iloc[-1]

    crossed_up = prev["ema_fast"] <= prev["ema_slow"] and curr["ema_fast"] > curr["ema_slow"]
    crossed_down = prev["ema_fast"] >= prev["ema_slow"] and curr["ema_fast"] < curr["ema_slow"]

    atr = curr["atr"]
    price = curr["close"]

    if crossed_up and curr["rsi"] > 50:
        sl = price - atr
        tp = price + atr * RISK_REWARD_RATIO
        return {"direction": "BUY", "price": price, "sl": sl, "tp": tp, "rsi": curr["rsi"], "time": curr["datetime"]}

    if crossed_down and curr["rsi"] < 50:
        sl = price + atr
        tp = price - atr * RISK_REWARD_RATIO
        return {"direction": "SELL", "price": price, "sl": sl, "tp": tp, "rsi": curr["rsi"], "time": curr["datetime"]}

    return None


def send_telegram_alert(signal):
    emoji = "🟢" if signal["direction"] == "BUY" else "🔴"
    msg = (
        f"{emoji} *{signal['direction']} SIGNAL — XAU/USD (5m)*\n\n"
        f"Entry: `{signal['price']:.2f}`\n"
        f"Stop Loss: `{signal['sl']:.2f}`\n"
        f"Take Profit: `{signal['tp']:.2f}`\n"
        f"RSI: `{signal['rsi']:.1f}`\n"
        f"R:R — 1:{RISK_REWARD_RATIO:.0f}\n"
        f"Candle: `{signal['time']}`"
    )
    requests.post(TELEGRAM_URL, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "Markdown",
    }, timeout=15)


def main_loop():
    global last_signal_candle_time
    print("Gold scalper bot started...")
    while True:
        try:
            df = fetch_candles()
            df = compute_indicators(df)
            signal = check_signal(df)

            if signal and signal["time"] != last_signal_candle_time:
                send_telegram_alert(signal)
                last_signal_candle_time = signal["time"]
                print(f"[{datetime.now(timezone.utc)}] Sent {signal['direction']} signal @ {signal['price']}")
            else:
                print(f"[{datetime.now(timezone.utc)}] No new signal.")

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main_loop()
