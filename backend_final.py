import asyncio, requests, time, os
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

# 🔥 SINGLE PAIR (IMPORTANT)
pairs = ["btcusdt"]

market_data = {}
signals = []
trades = []

COOLDOWN = 180
last_signal_time = {}

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# ===== TELEGRAM =====
def send_telegram_signal(sig):
    if not BOT_TOKEN:
        return

    msg = f"""
🚀 SMC SIGNAL

📊 {sig['pair']} {sig['signal']}

💰 Entry: {round(sig['entry'],2)}
🛑 SL: {round(sig['sl'],2)}
🎯 TP: {round(sig['tp'],2)}

⚖️ RR 1:3
"""

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": msg})


def send_telegram_close(t):
    if not BOT_TOKEN:
        return

    msg = f"""
📊 TRADE CLOSED

{t['pair']} {t['signal']}
Result: {t['status']}
"""

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": msg})


# ===== LIGHT SMC LOGIC =====
def generate_signal(closes, pair):

    if len(closes) < 80:
        return None

    price = closes[-1]

    ema20 = sum(closes[-20:]) / 20
    ema50 = sum(closes[-50:]) / 50

    trend = "UP" if ema20 > ema50 else "DOWN"

    recent_high = max(closes[-20:-1])
    recent_low = min(closes[-20:-1])

    sweep = None

    if price > recent_high:
        sweep = "SELL"
    elif price < recent_low:
        sweep = "BUY"

    if not sweep:
        return None

    if sweep == "BUY" and trend != "UP":
        return None

    if sweep == "SELL" and trend != "DOWN":
        return None

    entry = price

    if sweep == "BUY":
        sl = price * 0.995
        tp = price * 1.02
    else:
        sl = price * 1.005
        tp = price * 0.98

    move = abs(price - closes[-2]) / closes[-2] * 100
    if move < 0.15:
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
def update_trades(price):
    for t in trades:
        if t["status"] != "OPEN":
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


# ===== STREAM (REST API POLLING) =====
async def stream(pair):

    url = f"https://api.binance.com/api/v3/klines?symbol={pair.upper()}&interval=1m&limit=100"

    while True:
        try:
            res = requests.get(url).json()

            closes = [float(x[4]) for x in res]

            market_data[pair] = closes

            price = closes[-1]

            update_trades(price)

            sig = generate_signal(closes, pair)

            if sig:
                now = time.time()

                # COOLDOWN
                if pair in last_signal_time and now - last_signal_time[pair] < COOLDOWN:
                    await asyncio.sleep(5)
                    continue

                # DUPLICATE FILTER
                if signals:
                    last = signals[-1]
                    if abs(sig["entry"] - last["entry"]) / last["entry"] < 0.003:
                        await asyncio.sleep(5)
                        continue

                last_signal_time[pair] = now

                signals.append(sig)

                if len(signals) > 30:
                    signals[:] = signals[-30:]

                trades.append({
                    "pair": sig["pair"],
                    "signal": sig["signal"],
                    "entry": sig["entry"],
                    "sl": sig["sl"],
                    "tp": sig["tp"],
                    "status": "OPEN"
                })

                if len(trades) > 50:
                    trades[:] = trades[-50:]

                send_telegram_signal(sig)

        except Exception as e:
            print("Error:", e)

        await asyncio.sleep(10)  # 🔥 10 sec polling


# ===== API =====
@app.get("/")
def home():
    return {"status": "SMC BOT LIVE 🚀"}

@app.get("/signals")
def get_signals():
    return signals

@app.get("/trades")
def get_trades():
    return trades

@app.get("/stats")
def stats():
    return get_stats()


# ===== START =====
@app.on_event("startup")
async def start():
    for p in pairs:
        asyncio.create_task(stream(p))