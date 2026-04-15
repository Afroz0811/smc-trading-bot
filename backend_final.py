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

# ===== CONFIG =====
pairs = ["btcusdt"]  # keep 1 pair for free plan stability

market_data = {}
signals = []
trades = []

COOLDOWN = 300  # 5 min cooldown
last_signal_time = {}

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# ===== TELEGRAM =====
def send_telegram(msg):
    if not BOT_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": msg})


def send_signal(sig):
    msg = f"""
🚀 SMC SIGNAL

📊 {sig['pair']} {sig['signal']}

💰 Entry: {round(sig['entry'],2)}
🛑 SL: {round(sig['sl'],2)}
🎯 TP: {round(sig['tp'],2)}

⚖️ RR ~ 1:3
"""
    send_telegram(msg)


def send_close(t):
    msg = f"""
📊 TRADE CLOSED

{t['pair']} {t['signal']}
Result: {t['status']}
"""
    send_telegram(msg)


# ===== SMC LOGIC (IMPROVED) =====
def generate_signal(closes, pair):

    if len(closes) < 100:
        return None

    price = closes[-1]

    # EMA trend
    ema20 = sum(closes[-20:]) / 20
    ema50 = sum(closes[-50:]) / 50
    ema200 = sum(closes[-100:]) / 100

    trend = "UP" if ema20 > ema50 > ema200 else "DOWN" if ema20 < ema50 < ema200 else "RANGE"

    # Liquidity sweep
    high = max(closes[-30:-1])
    low = min(closes[-30:-1])

    sweep = None
    if price > high:
        sweep = "SELL"
    elif price < low:
        sweep = "BUY"

    if not sweep:
        return None

    # Trend confirmation
    if sweep == "BUY" and trend != "UP":
        return None
    if sweep == "SELL" and trend != "DOWN":
        return None

    # Volatility filter
    move = abs(price - closes[-2]) / closes[-2] * 100
    if move < 0.2:
        return None

    # Entry + RR
    entry = price

    if sweep == "BUY":
        sl = price * 0.994
        tp = price * 1.03
    else:
        sl = price * 1.006
        tp = price * 0.97

    return {
        "pair": pair.upper(),
        "price": round(price, 2),
        "signal": sweep,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "time": time.time()
    }


# ===== TRADE MANAGEMENT =====
def update_trades(price):

    for t in trades:
        if t["status"] != "OPEN":
            continue

        if t["signal"] == "BUY":
            if price <= t["sl"]:
                t["status"] = "LOSS"
                send_close(t)
            elif price >= t["tp"]:
                t["status"] = "WIN"
                send_close(t)

        elif t["signal"] == "SELL":
            if price >= t["sl"]:
                t["status"] = "LOSS"
                send_close(t)
            elif price <= t["tp"]:
                t["status"] = "WIN"
                send_close(t)


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


# ===== MAIN LOOP (REST API SAFE) =====
async def stream(pair):

    url = f"https://api.exchange.coinbase.com/products/BTC-USDT/candles?granularity=60"
    while True:
        try:
            res = requests.get(url, timeout=5)

            # ✅ check response
            if res.status_code != 200:
                print("API ERROR:", res.text)
                await asyncio.sleep(10)
                continue

            data = res.json()

            # ✅ validate data
            if not isinstance(data, list) or len(data) < 50:
                print("INVALID DATA:", data)
                await asyncio.sleep(10)
                continue

            closes = []

            for candle in data:
                if len(candle) > 4:
                    try:
                        closes.append(float(candle[4]))
                    except:
                        continue

            # ✅ ensure enough data
            if len(closes) < 50:
                print("NOT ENOUGH VALID CANDLES")
                await asyncio.sleep(10)
                continue

            market_data[pair] = closes

            price = closes[-1]

            update_trades(price)

            sig = generate_signal(closes, pair)

            if sig:
                now = time.time()

                if pair in last_signal_time and now - last_signal_time[pair] < COOLDOWN:
                    await asyncio.sleep(5)
                    continue

                if signals:
                    last = signals[-1]
                    if abs(sig["entry"] - last["entry"]) / last["entry"] < 0.004:
                        await asyncio.sleep(5)
                        continue

                last_signal_time[pair] = now

                signals.append(sig)
                signals[:] = signals[-30:]

                trades.append({
                    "pair": sig["pair"],
                    "signal": sig["signal"],
                    "entry": sig["entry"],
                    "sl": sig["sl"],
                    "tp": sig["tp"],
                    "status": "OPEN"
                })
                trades[:] = trades[-50:]

                send_signal(sig)

        except Exception as e:
            print("CRITICAL ERROR:", e)

        await asyncio.sleep(12)


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