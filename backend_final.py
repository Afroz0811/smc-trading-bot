import asyncio, requests, time, os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

# ===== GLOBAL DATA =====
pairs = ["BTC-USDT"]
signals = []
trades = []
COOLDOWN = 300
last_signal_time = {}

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# ===== TELEGRAM =====
def send_telegram(msg):
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg},
            timeout=5
        )
    except:
        pass


def send_signal(sig):
    msg = f"🚀 {sig['pair']} {sig['signal']}\nEntry: {round(sig['entry'],2)}"
    send_telegram(msg)


def send_close(t):
    msg = f"📊 CLOSED {t['pair']} {t['status']}"
    send_telegram(msg)


# ===== STRATEGY =====
def generate_signal(closes, pair):

    if len(closes) < 50:
        return None

    price = closes[-1]

    ema20 = sum(closes[-20:]) / 20
    ema50 = sum(closes[-50:]) / 50

    trend = "UP" if ema20 > ema50 else "DOWN"

    high = max(closes[-20:-1])
    low = min(closes[-20:-1])

    if price > high and trend == "DOWN":
        signal = "SELL"
    elif price < low and trend == "UP":
        signal = "BUY"
    else:
        return None

    return {
        "pair": pair,
        "signal": signal,
        "entry": price,
        "sl": price * 0.995 if signal == "BUY" else price * 1.005,
        "tp": price * 1.01 if signal == "BUY" else price * 0.99
    }


# ===== TRADE TRACKING =====
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


# ===== STREAM =====
async def stream(pair):
    url = f"https://api.exchange.coinbase.com/products/{pair}/candles?granularity=60"

    while True:
        try:
            res = requests.get(url, timeout=5)

            if res.status_code != 200:
                await asyncio.sleep(30)
                continue

            data = res.json()

            closes = [float(c[4]) for c in data if len(c) > 4]
            closes.reverse()

            if len(closes) < 50:
                await asyncio.sleep(30)
                continue

            price = closes[-1]

            update_trades(price)

            sig = generate_signal(closes, pair)

            if sig:
                now = time.time()

                if pair in last_signal_time and now - last_signal_time[pair] < 300:
                    await asyncio.sleep(10)
                    continue

                last_signal_time[pair] = now

                signals.append(sig)
                signals[:] = signals[-20:]

                trades.append({
                    "pair": sig["pair"],
                    "signal": sig["signal"],
                    "entry": sig["entry"],
                    "sl": sig["sl"],
                    "tp": sig["tp"],
                    "status": "OPEN"
                })

                send_signal(sig)

        except Exception as e:
            print("ERROR:", e)

        # 🔥 KEY FIX → LONGER SLEEP
        await asyncio.sleep(30)


async def run_forever(pair):
    while True:
        try:
            await stream(pair)
        except Exception as e:
            print("RESTART:", e)
            await asyncio.sleep(5)


# ===== LIFESPAN =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 BOT STARTED")

    tasks = []
    for p in pairs:
        tasks.append(asyncio.create_task(run_forever(p)))

    yield

    for t in tasks:
        t.cancel()


# ===== APP =====
app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===== ROUTES =====
@app.get("/")
def home():
    return {"status": "LIVE 🚀"}


@app.get("/signals")
def get_signals():
    return signals


@app.get("/trades")
def get_trades():
    return trades