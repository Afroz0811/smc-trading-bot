import asyncio, json, websockets, requests, time
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

pairs = ["btcusdt", "ethusdt", "bnbusdt"]

market_data = {}
signals = []
trades = []

COOLDOWN = 180
last_signal_time = {}

# ===== TELEGRAM CONFIG =====
BOT_TOKEN = "8790151434:AAH_qgxdAP8kJGyAv6EIoQ7QMRvtp7Mttl4"
CHAT_ID = "881051504"

def send_telegram_signal(sig):
    if not BOT_TOKEN:
        return

    msg = f"""
🚀 SMC SIGNAL

📊 Pair: {sig['pair']}
📈 Type: {sig['signal']}

💰 Entry: {round(sig['entry'],2)}
🛑 SL: {round(sig['sl'],2)}
🎯 TP: {round(sig['tp'],2)}

⚖️ RR: 1:3
🔥 Setup: OB + Sweep + Trend

⏰ {time.strftime('%H:%M:%S')}
"""

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": msg})


def send_telegram_close(trade):
    if not BOT_TOKEN:
        return

    msg = f"""
📊 TRADE CLOSED

{trade['pair']} {trade['signal']}

Result: {trade['status']}

Entry: {round(trade['entry'],2)}
TP: {round(trade['tp'],2)}
SL: {round(trade['sl'],2)}
"""

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": msg})


# ===== SMC STRATEGY =====
def generate_signal(candles, pair):

    if len(candles) < 120:
        return None

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    price = closes[-1]

    ema20 = sum(closes[-20:]) / 20
    ema50 = sum(closes[-50:]) / 50
    trend = "UP" if ema20 > ema50 else "DOWN"

    recent_high = max(highs[-30:-5])
    recent_low = min(lows[-30:-5])

    sweep = None
    if highs[-1] > recent_high and closes[-1] < recent_high:
        sweep = "SELL"
    if lows[-1] < recent_low and closes[-1] > recent_low:
        sweep = "BUY"

    if not sweep:
        return None

    ob = None
    for i in range(len(candles)-10, len(candles)-2):
        c = candles[i]

        if c["close"] < c["open"] and candles[i+2]["close"] > c["high"]:
            ob = ("BUY", c["low"], c["high"])

        if c["close"] > c["open"] and candles[i+2]["close"] < c["low"]:
            ob = ("SELL", c["low"], c["high"])

    if not ob:
        return None

    ob_type, ob_low, ob_high = ob

    mid = (max(highs[-50:]) + min(lows[-50:])) / 2
    zone = "DISCOUNT" if price < mid else "PREMIUM"

    if sweep != ob_type:
        return None

    if sweep == "BUY" and (trend != "UP" or zone != "DISCOUNT"):
        return None

    if sweep == "SELL" and (trend != "DOWN" or zone != "PREMIUM"):
        return None

    entry = price

    if sweep == "BUY":
        sl = ob_low * 0.998
        tp = price + (price - sl) * 3
    else:
        sl = ob_high * 1.002
        tp = price - (sl - price) * 3

    move = abs(price - closes[-2]) / closes[-2] * 100
    if move < 0.2:
        return None

    return {
        "pair": pair.upper(),
        "price": round(price, 2),
        "signal": sweep,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "time": time.time()
    }


# ===== TRADE TRACKING =====
def update_trades(pair, price):
    for t in trades:
        if t["pair"] != pair.upper() or t["status"] != "OPEN":
            continue

        if t["signal"] == "BUY":
            if price <= t["sl"]:
                t["status"] = "LOSS"
                send_telegram_close(t)
            elif price >= t["tp"]:
                t["status"] = "WIN"
                send_telegram_close(t)

        if t["signal"] == "SELL":
            if price >= t["sl"]:
                t["status"] = "LOSS"
                send_telegram_close(t)
            elif price <= t["tp"]:
                t["status"] = "WIN"
                send_telegram_close(t)


# ===== STATS =====
def get_stats():
    wins = len([t for t in trades if t["status"] == "WIN"])
    losses = len([t for t in trades if t["status"] == "LOSS"])
    total = len(trades)

    wr = (wins / total * 100) if total else 0

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "winrate": round(wr, 2)
    }


# ===== STREAM =====
async def stream(pair):

    url = f"wss://stream.binance.com:9443/ws/{pair}@kline_1m"

    while True:
        try:
            async with websockets.connect(url) as ws:
                print(f"Connected: {pair}")

                market_data[pair] = []

                while True:
                    data = json.loads(await ws.recv())
                    k = data["k"]

                    candle = {
                        "open": float(k["o"]),
                        "high": float(k["h"]),
                        "low": float(k["l"]),
                        "close": float(k["c"])
                    }

                    market_data[pair].append(candle)

                    if len(market_data[pair]) > 150:
                        market_data[pair].pop(0)

                    price = candle["close"]

                    update_trades(pair, price)

                    sig = generate_signal(market_data[pair], pair)

                    if sig:
                        now = time.time()

                        # COOLDOWN
                        if pair in last_signal_time and now - last_signal_time[pair] < COOLDOWN:
                            continue

                        # DUPLICATE FILTER
                        if signals:
                            last = signals[-1]
                            if abs(sig["entry"] - last["entry"]) / last["entry"] < 0.003:
                                continue

                        last_signal_time[pair] = now

                        signals.append(sig)

                        trades.append({
                            "pair": sig["pair"],
                            "signal": sig["signal"],
                            "entry": sig["entry"],
                            "sl": sig["sl"],
                            "tp": sig["tp"],
                            "status": "OPEN"
                        })

                        send_telegram_signal(sig)

        except Exception as e:
            print("Reconnect...", pair, e)
            await asyncio.sleep(3)


# ===== API =====
@app.get("/")
def home():
    return {"status": "SMC BOT LIVE 🚀"}

@app.get("/signals")
def get_signals():
    return signals[-10:]

@app.get("/trades")
def get_trades():
    return trades[-20:]

@app.get("/stats")
def stats():
    return get_stats()


# ===== START =====
@app.on_event("startup")
async def start():
    for p in pairs:
        asyncio.create_task(stream(p))