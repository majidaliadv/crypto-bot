"""
Crypto signal bot for GitHub Actions (runs once per schedule).
- Fetches candles, finds trend + signals
- Sends WhatsApp alerts via CallMeBot (secrets WA_PHONE, WA_APIKEY)
- Writes docs/index.html (live page) and docs/state.json (dedupe)
Not financial advice.
"""
import json
import os
import time
from datetime import datetime, timezone

import pandas as pd
import requests

COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "SUIUSDT"]
TIMEFRAME = "1h"
PUMP_DUMP_PCT = 3.0
VOLUME_SPIKE = 2.0

BASES = ["https://data-api.binance.vision", "https://api.binance.com",
         "https://api1.binance.com", "https://api.binance.us"]


def get_candles(symbol, interval=TIMEFRAME, limit=300):
    last_err = None
    for base in BASES:
        try:
            r = requests.get(base + "/api/v3/klines",
                             params={"symbol": symbol, "interval": interval,
                                     "limit": limit}, timeout=20)
            r.raise_for_status()
            df = pd.DataFrame(r.json(), columns=[
                "t", "open", "high", "low", "close", "volume", "ct", "qv",
                "n", "tb", "tq", "x"])
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = df[c].astype(float)
            return df
        except Exception as e:
            last_err = e
    raise RuntimeError(f"data source blocked/failed: {last_err}")


def add_indicators(df):
    c = df["close"]
    df["ema20"] = c.ewm(span=20, adjust=False).mean()
    df["ema50"] = c.ewm(span=50, adjust=False).mean()
    df["ema200"] = c.ewm(span=200, adjust=False).mean()
    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + g / l)
    e12 = c.ewm(span=12, adjust=False).mean()
    e26 = c.ewm(span=26, adjust=False).mean()
    df["macd"] = e12 - e26
    df["macd_sig"] = df["macd"].ewm(span=9, adjust=False).mean()
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - c.shift()).abs(),
                    (df["low"] - c.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    df["vol_avg"] = df["volume"].rolling(20).mean()
    return df


def trend(df):
    r = df.iloc[-1]
    slope = (df["ema50"].iloc[-1] - df["ema50"].iloc[-11]) / df["ema50"].iloc[-11] * 100
    spread = abs(r.ema20 - r.ema50) / r.close * 100
    if r.ema20 > r.ema50 > r.ema200 and slope > 0.5:
        return "UP"
    if r.ema20 < r.ema50 < r.ema200 and slope < -0.5:
        return "DOWN"
    if spread < 0.5 or abs(slope) < 0.5:
        return "SIDEWAYS"
    return "UP" if slope > 0 else "DOWN"


def analyze(symbol):
    df = add_indicators(get_candles(symbol))
    r, p = df.iloc[-2], df.iloc[-3]
    tr = trend(df.iloc[:-1])
    alerts = []
    move = (r.close - r.open) / r.open * 100
    vol_x = r.volume / r.vol_avg if r.vol_avg else 0
    if move >= PUMP_DUMP_PCT and vol_x >= VOLUME_SPIKE:
        alerts.append(("PUMP", f"PUMP: +{move:.1f}% with {vol_x:.1f}x volume"))
    if move <= -PUMP_DUMP_PCT and vol_x >= VOLUME_SPIKE:
        alerts.append(("DUMP", f"DUMP WARNING: {move:.1f}% with {vol_x:.1f}x volume"))
    macd_up = p.macd < p.macd_sig and r.macd > r.macd_sig
    macd_dn = p.macd > p.macd_sig and r.macd < r.macd_sig
    rsi_up = p.rsi < 35 <= r.rsi
    if tr == "UP" and (macd_up or rsi_up) and r.rsi < 65:
        alerts.append(("BUY", f"SPOT BUY signal | SL ~{r.close - 1.5 * r.atr:.4f} (1.5x ATR)"))
    if (macd_dn and r.rsi > 55) or r.rsi > 75:
        alerts.append(("SELL", "SPOT SELL / take-profit signal"))
    if tr == "DOWN" and r.close < r.ema200:
        alerts.append(("WEAK", "Weak market: below EMA200, avoid fresh buys"))
    return tr, float(df.iloc[-1].close), float(r.rsi), alerts


def whatsapp(text):
    phone, key = os.getenv("WA_PHONE"), os.getenv("WA_APIKEY")
    print("ALERT:", text)
    if not (phone and key):
        return
    try:
        requests.get("https://api.callmebot.com/whatsapp.php",
                     params={"phone": phone, "apikey": key, "text": text},
                     timeout=30)
    except Exception as e:
        print("WhatsApp error:", e)


def main():
    os.makedirs("docs", exist_ok=True)
    try:
        sent = json.load(open("docs/state.json"))
    except Exception:
        sent = {}
    hour = int(time.time() // 3600)
    cards = []
    for s in COINS:
        try:
            tr, price, rsi, alerts = analyze(s)
            for kind, msg in alerts:
                k = f"{s}:{kind}"
                if sent.get(k) != hour and kind != "WEAK":
                    sent[k] = hour
                    whatsapp(f"[{s}] {msg} | price {price:.4f} | trend {tr}")
            cards.append(f"<div class=c><b>{s}</b> &nbsp; {price:.4f}"
                         f"<br>Trend: <b class={tr}>{tr}</b> | RSI {rsi:.0f}"
                         + "".join(f"<div class=a>&#9888; {m}</div>" for _, m in alerts)
                         + "</div>")
        except Exception as e:
            cards.append(f"<div class=c><b>{s}</b>: {e}</div>")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300"><title>Crypto Live</title>
<style>body{{font-family:sans-serif;background:#0f1115;color:#eee;margin:0;padding:12px}}
.c{{background:#1a1d24;border-radius:12px;padding:12px;margin-bottom:10px}}
.UP{{color:#3ddc84}}.DOWN{{color:#ff5c5c}}.SIDEWAYS{{color:#f5c542}}
.a{{font-size:13px;margin-top:6px;color:#ffd}}small{{color:#888}}</style></head>
<body><h3>Crypto Live</h3>{''.join(cards)}<small>Updated: {now} ({TIMEFRAME} candles)</small></body></html>"""
    open("docs/index.html", "w").write(html)
    json.dump(sent, open("docs/state.json", "w"))


if __name__ == "__main__":
    main()
