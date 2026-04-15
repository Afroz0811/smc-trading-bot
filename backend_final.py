import asyncio, json, websockets, requests, time, os
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

# 🔥 USE ONLY 1 PAIR (IMPORTANT FOR MEMORY)
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


# ===== SMC LOGIC (LIGHT VERSION) =====
def generate_signal(candles, pair):

    if len(candles) < 80:
        return None

    closes = candles

    price = closes[-1]

    ema20 = sum(closes[-20:]) / 20
    ema50 = sum(closes[-50:]) / 50

    trend = "UP" if ema20 > ema50 else "DOWN"

    recent_high = max(closes[-20:-1])
    recent_low = min(closes[-20:-1])

    sweep = None

    if price > recent_high:
        sweep = "SELL"

    if price < recent_low:
        sweep = "BUY"

    if not sweep:
        return None

    # FILTER
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


# ===== TRADE TRACK =====
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


# ===== STREAM (LOW MEMORY + RECONNECT) =====
async def stream(pair):

    url = f"wss://stream.binance.com:9443/ws/{pair}@kline_1m"

    while True:
        try:
            async with websockets.connect(url) as ws:
                print(f"Connected: {pair}")

                market_data[pair] = []

                while True:
                    data = json.loads(await ws.recv())
                    price = float(data["k"]["c"])

                    market_data[pair].append(price)

                    # 🔥 LIMIT DATA
                    if len(market_data[pair]) > 100:
                        market_data[pair] = market_data[pair][-100:]

                    update_trades(price)

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

                        # 🔥 LIMIT SIGNALS
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

                        # 🔥 LIMIT TRADES
                        if len(trades) > 50:
                            trades[:] = trades[-50:]

                        send_telegram_signal(sig)

        except Exception as e:
            print("Reconnect...", e)
            await asyncio.sleep(3)


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