#!/usr/bin/env python3
"""
ML Scalp Bot v1 — Bybit Multi-Pair
====================================
Strategy:
  1. Daily/Weekly/Monthly key levels as anchor
  2. Price MUST reach level FIRST — no late entries
  3. Confluence stack: Order Flow + CVD Divergence + Fib + Volume + RSI + OB/Sweep
  4. ML self-learning — adjusts weights from every trade result
  5. Backtest engine built-in — finds best timeframe per pair
  6. TG alerts: entry reason, TP1/TP2/TP3, SL, TP/SL hit notifications
  7. Persistent data — survives redeploys
"""
import os, sys, json, time, math, logging, threading, requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict, deque
from http.server import HTTPServer, BaseHTTPRequestHandler

# ═══ CONFIG ══════════════════════════════════════════════════════
TG_TOKEN   = os.environ.get('TG_TOKEN', '')
TG_CHAT    = os.environ.get('TG_CHAT', '')
PORT       = int(os.environ.get('PORT', 8080))
BYBIT_BASE = 'https://api.bybit.com'
DATA_DIR   = os.environ.get('DATA_DIR', '/app/data')

# Pairs to scan
PAIRS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT',
    'XRPUSDT', 'ADAUSDT', 'AVAXUSDT', 'DOGEUSDT',
    'LINKUSDT', 'DOTUSDT',
]

# Timeframes to backtest — bot picks best per pair
TIMEFRAMES = ['1', '3', '5', '15']  # minutes

# Scan interval
SCAN_EVERY_MIN = int(os.environ.get('SCAN_EVERY_MIN', 1))
COOLDOWN_MIN   = int(os.environ.get('COOLDOWN_MIN', 45))

# ═══ PATHS ═══════════════════════════════════════════════════════
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
ML_FILE       = os.path.join(DATA_DIR, 'ml_brain.json')
JOURNAL_FILE  = os.path.join(DATA_DIR, 'journal.json')
LEVELS_FILE   = os.path.join(DATA_DIR, 'levels.json')
BACKTEST_FILE = os.path.join(DATA_DIR, 'backtest.json')
BT_DONE_FILE  = os.path.join(DATA_DIR, 'backtest_done.json')

# ═══ LOGGING ═════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger('scalp')

# ═══ STATE ═══════════════════════════════════════════════════════
state = {
    'started':      datetime.now(timezone.utc).isoformat(),
    'scans':        0,
    'signals_sent': 0,
    'open_trades':  {},   # sym → trade dict
    'last_fired':   {},   # sym → timestamp
    'best_tf':      {},   # sym → '5' (from backtest)
    'bt_done':      False,
}

# ═══════════════════════════════════════════════════════════════════
# ML BRAIN
# Weights for every confluence. Updated after every trade close.
# Bayesian-style: good condition → weight up, bad → weight down
# ═══════════════════════════════════════════════════════════════════
DEFAULT_ML = {
    'version': 3,
    'created': datetime.now(timezone.utc).isoformat(),
    'total_trades': 0,
    'wins': 0, 'losses': 0, 'be': 0,
    'total_pnl': 0.0,

    # Feature weights (multipliers on base score)
    'weights': {
        # Key level proximity
        'at_daily_level':    2.0,
        'at_weekly_level':   2.5,
        'at_monthly_level':  3.0,
        'near_level_0.1pct': 1.5,
        'near_level_0.2pct': 1.2,

        # Order flow
        'delta_positive':    1.3,
        'delta_negative':    1.3,
        'delta_spike':       1.5,  # delta > 2x avg
        'absorption':        1.8,  # high vol, small candle
        'bid_imbalance':     1.4,
        'ask_imbalance':     1.4,
        'stacked_imbalance': 1.8,

        # CVD divergence
        'cvd_bull_div':      2.0,  # price lower low, CVD higher low
        'cvd_bear_div':      2.0,  # price higher high, CVD lower high
        'cvd_leading':       1.5,  # CVD turned before price
        'cvd_lagging':       0.8,  # CVD not confirming

        # Fibonacci
        'fib_0618':          1.6,
        'fib_0705':          1.4,
        'fib_0786':          1.5,
        'fib_0500':          1.2,

        # RSI
        'rsi_oversold_30':   1.5,
        'rsi_oversold_40':   1.2,
        'rsi_overbought_70': 1.5,
        'rsi_overbought_60': 1.2,
        'rsi_divergence':    1.8,

        # Volume
        'vol_spike_2x':      1.6,
        'vol_spike_1.5x':    1.3,
        'vol_dry_up':        1.4,  # vol drying = exhaustion
        'vol_climax':        1.7,  # extreme vol at level

        # Order blocks
        'ob_bullish':        1.5,
        'ob_bearish':        1.5,
        'ob_sweep_bull':     2.0,  # OB swept then reversed
        'ob_sweep_bear':     2.0,

        # Timing (late signal penalty)
        'entry_at_level':    2.0,  # price AT level = good
        'entry_after_move':  0.3,  # price already moved = bad
        'move_pct_0.1':      1.8,  # only 0.1% move so far = early
        'move_pct_0.5':      1.0,
        'move_pct_1.0':      0.4,  # 1% move = too late

        # Structure
        'choch_confirmed':   1.6,
        'bos_confirmed':     1.4,
        'swing_sweep':       1.7,
        'hl_confirmed':      1.3,
        'lh_confirmed':      1.3,
    },

    # Min score to fire signal
    'min_score': 8.0,

    # Per-pair performance
    'by_pair':     {},
    'by_tf':       {},
    'by_setup':    {},
    'by_hour':     {},
    'by_session':  {},

    # Learning log
    'learning_log': [],
    'last_learned': None,

    # Signal timing analysis
    'early_signals': 0,   # fired before move = good
    'late_signals':  0,   # fired after move = penalized
}

def load_ml():
    if Path(ML_FILE).exists():
        try:
            with open(ML_FILE) as f: return json.load(f)
        except: pass
    return DEFAULT_ML.copy()

def save_ml(db):
    try:
        with open(ML_FILE, 'w') as f: json.dump(db, f, indent=2)
    except Exception as e:
        log.warning(f"ML save: {e}")

def load_journal():
    if Path(JOURNAL_FILE).exists():
        try:
            with open(JOURNAL_FILE) as f: return json.load(f)
        except: pass
    return {'signals': [], 'open': {}, 'closed': [], 'stats': {}}

def save_journal(j):
    try:
        with open(JOURNAL_FILE, 'w') as f: json.dump(j, f, indent=2)
    except Exception as e:
        log.warning(f"Journal save: {e}")

def load_levels():
    if Path(LEVELS_FILE).exists():
        try:
            with open(LEVELS_FILE) as f: return json.load(f)
        except: pass
    return {}

def save_levels(lv):
    try:
        with open(LEVELS_FILE, 'w') as f: json.dump(lv, f, indent=2)
    except: pass

def load_backtest():
    if Path(BACKTEST_FILE).exists():
        try:
            with open(BACKTEST_FILE) as f: return json.load(f)
        except: pass
    return {}

def save_backtest(bt):
    try:
        with open(BACKTEST_FILE, 'w') as f: json.dump(bt, f, indent=2)
    except: pass

# ═══════════════════════════════════════════════════════════════════
# BYBIT DATA FETCH
# ═══════════════════════════════════════════════════════════════════
def bybit_klines(symbol, interval, limit=300):
    """Fetch OHLCV from Bybit. interval: '1','3','5','15','60','D','W','M'"""
    try:
        r = requests.get(
            f'{BYBIT_BASE}/v5/market/kline',
            params={'category':'linear','symbol':symbol,'interval':interval,'limit':limit},
            timeout=12
        )
        d = r.json()
        if d.get('retCode') != 0:
            log.warning(f"Bybit kline {symbol}/{interval}: {d.get('retMsg')}")
            return None
        raw = d['result']['list']
        # raw: [timestamp, open, high, low, close, volume, turnover]
        kl = []
        for k in reversed(raw):
            kl.append({
                't': int(k[0]),
                'o': float(k[1]),
                'h': float(k[2]),
                'l': float(k[3]),
                'c': float(k[4]),
                'v': float(k[5]),
            })
        return kl
    except Exception as e:
        log.warning(f"Bybit fetch {symbol}: {e}")
        return None

def bybit_ticker(symbol):
    """Get current price + 24h data"""
    try:
        r = requests.get(
            f'{BYBIT_BASE}/v5/market/tickers',
            params={'category':'linear','symbol':symbol},
            timeout=8
        )
        d = r.json()
        if d.get('retCode') == 0 and d['result']['list']:
            t = d['result']['list'][0]
            return {
                'price':   float(t['lastPrice']),
                'high24':  float(t['highPrice24h']),
                'low24':   float(t['lowPrice24h']),
                'vol24':   float(t['volume24h']),
                'chg24':   float(t.get('price24hPcnt', 0)) * 100,
            }
    except Exception as e:
        log.warning(f"Ticker {symbol}: {e}")
    return None

def bybit_orderbook(symbol, limit=50):
    """Get order book for imbalance detection"""
    try:
        r = requests.get(
            f'{BYBIT_BASE}/v5/market/orderbook',
            params={'category':'linear','symbol':symbol,'limit':limit},
            timeout=8
        )
        d = r.json()
        if d.get('retCode') == 0:
            bids = [[float(x[0]), float(x[1])] for x in d['result']['b']]
            asks = [[float(x[0]), float(x[1])] for x in d['result']['a']]
            return {'bids': bids, 'asks': asks}
    except Exception as e:
        log.warning(f"OB {symbol}: {e}")
    return None

# ═══════════════════════════════════════════════════════════════════
# INDICATORS
# ═══════════════════════════════════════════════════════════════════
def calc_ema(closes, period):
    if len(closes) < period: return [None]*len(closes)
    k = 2/(period+1)
    r = [None]*(period-1)
    s = sum(closes[:period])/period
    r.append(s); pv = s
    for i in range(period, len(closes)):
        pv = closes[i]*k + pv*(1-k); r.append(pv)
    return r

def calc_rsi(closes, period=14):
    if len(closes) < period+1: return [None]*len(closes)
    r = [None]*period; g = l = 0.0
    for i in range(1, period+1):
        d = closes[i]-closes[i-1]
        if d > 0: g += d
        else: l += abs(d)
    ag, al = g/period, l/period
    r.append(100 if al==0 else 100-100/(1+ag/al))
    for i in range(period+1, len(closes)):
        d = closes[i]-closes[i-1]
        ag = (ag*(period-1)+(d if d>0 else 0))/period
        al = (al*(period-1)+(abs(d) if d<0 else 0))/period
        r.append(100 if al==0 else 100-100/(1+ag/al))
    return r

def calc_atr(kl, period=14):
    if len(kl) < period+1: return [None]*len(kl)
    tr = [None] + [max(kl[i]['h']-kl[i]['l'],
                       abs(kl[i]['h']-kl[i-1]['c']),
                       abs(kl[i]['l']-kl[i-1]['c']))
                   for i in range(1, len(kl))]
    r = [None]*period
    s = sum(tr[1:period+1])/period; r.append(s); pv = s
    for i in range(period+1, len(tr)):
        pv = (pv*(period-1)+tr[i])/period; r.append(pv)
    return r

def calc_vol_avg(vols, period=20):
    r = [None]*period
    for i in range(period, len(vols)):
        r.append(sum(vols[i-period:i])/period)
    return r

def calc_cvd(kl):
    """
    Cumulative Volume Delta.
    Per candle delta = (close>open) ? +volume : -volume
    More accurate with tick data but this approximation works well.
    If close > open = net buying pressure
    If close < open = net selling pressure
    Body ratio used to weight delta more accurately.
    """
    cvd = []
    cumulative = 0.0
    for k in kl:
        rng = k['h'] - k['l']
        if rng == 0:
            delta = 0
        else:
            body = k['c'] - k['o']
            # body ratio: +1 = full bull candle, -1 = full bear
            ratio = body / rng
            delta = ratio * k['v']
        cumulative += delta
        cvd.append({
            'delta': delta,
            'cvd':   round(cumulative, 4),
            'cum':   round(cumulative, 4),
        })
    return cvd

def calc_swings(kl, lb=5):
    sh, sl = [], []
    for i in range(lb, len(kl)-lb):
        if all(kl[i]['h'] >= kl[j]['h'] for j in range(i-lb, i+lb+1) if j != i):
            sh.append((i, kl[i]['h']))
        if all(kl[i]['l'] <= kl[j]['l'] for j in range(i-lb, i+lb+1) if j != i):
            sl.append((i, kl[i]['l']))
    return sh, sl

def calc_fibs(high, low):
    """Fibonacci retracement levels between swing high and low"""
    diff = high - low
    return {
        '0.236': round(high - diff*0.236, 4),
        '0.382': round(high - diff*0.382, 4),
        '0.500': round(high - diff*0.500, 4),
        '0.618': round(high - diff*0.618, 4),
        '0.705': round(high - diff*0.705, 4),
        '0.786': round(high - diff*0.786, 4),
        '1.000': round(low, 4),
    }

def find_order_blocks(kl, lookback=30):
    """
    Order block = last bearish candle before a bullish move (bullish OB)
                = last bullish candle before a bearish move (bearish OB)
    Returns list of {type, top, bot, index, swept}
    """
    obs = []
    n = len(kl)
    for i in range(2, min(lookback, n-2)):
        idx = n - 1 - i
        c = kl[idx]
        # Bullish OB: bearish candle followed by strong bullish move
        if c['c'] < c['o']:  # bearish candle
            fwd = kl[idx+1]
            if fwd['c'] > fwd['o'] and (fwd['c']-fwd['o']) > (c['o']-c['c'])*0.8:
                swept = kl[-1]['l'] < c['l']  # price swept below OB
                obs.append({
                    'type': 'bull',
                    'top':  c['o'],
                    'bot':  c['l'],
                    'idx':  idx,
                    'swept': swept,
                })
        # Bearish OB: bullish candle followed by strong bearish move
        elif c['c'] > c['o']:
            fwd = kl[idx+1]
            if fwd['c'] < fwd['o'] and (fwd['o']-fwd['c']) > (c['c']-c['o'])*0.8:
                swept = kl[-1]['h'] > c['h']
                obs.append({
                    'type': 'bear',
                    'top':  c['h'],
                    'bot':  c['c'],
                    'idx':  idx,
                    'swept': swept,
                })
    return obs

def analyze_orderbook(ob_data):
    """
    Detect bid/ask imbalances and absorption from order book.
    Returns dict of conditions.
    """
    if not ob_data:
        return {}
    bids = ob_data['bids']
    asks = ob_data['asks']
    if not bids or not asks:
        return {}

    bid_vol = sum(b[1] for b in bids[:10])
    ask_vol = sum(a[1] for a in asks[:10])
    total = bid_vol + ask_vol + 1e-10

    bid_ratio = bid_vol / total
    ask_ratio = ask_vol / total

    # Thin book = fast move incoming
    thin_above = sum(a[1] for a in asks[:5]) < sum(a[1] for a in asks[5:10]) * 0.5
    thin_below = sum(b[1] for b in bids[:5]) < sum(b[1] for b in bids[5:10]) * 0.5

    return {
        'bid_ratio':     round(bid_ratio, 3),
        'ask_ratio':     round(ask_ratio, 3),
        'bid_imbalance': bid_ratio > 0.65,  # strong buying in book
        'ask_imbalance': ask_ratio > 0.65,  # strong selling
        'bid_dom':       bid_vol > ask_vol * 1.5,
        'ask_dom':       ask_vol > bid_vol * 1.5,
        'thin_above':    thin_above,  # easy path up
        'thin_below':    thin_below,  # easy path down
        'total_bid':     round(bid_vol, 2),
        'total_ask':     round(ask_vol, 2),
    }

# ═══════════════════════════════════════════════════════════════════
# KEY LEVELS ENGINE
# Daily / Weekly / Monthly highs and lows
# ═══════════════════════════════════════════════════════════════════
def fetch_key_levels(symbol):
    """
    Fetch daily, weekly, monthly OHLC and extract key levels.
    Returns dict of levels with their source (daily/weekly/monthly).
    """
    levels = []

    # Daily levels (last 5 days)
    kl_d = bybit_klines(symbol, 'D', limit=10)
    if kl_d and len(kl_d) >= 2:
        prev = kl_d[-2]  # previous day
        levels += [
            {'price': prev['h'], 'type': 'D_high',  'weight': 2.0},
            {'price': prev['l'], 'type': 'D_low',   'weight': 2.0},
            {'price': (prev['h']+prev['l'])/2, 'type': 'D_mid', 'weight': 1.5},
        ]
        # Last 3 daily closes as key levels
        for k in kl_d[-4:-1]:
            levels.append({'price': k['c'], 'type': 'D_close', 'weight': 1.3})

    # Weekly levels (last 4 weeks)
    kl_w = bybit_klines(symbol, 'W', limit=5)
    if kl_w and len(kl_w) >= 2:
        prev_w = kl_w[-2]
        levels += [
            {'price': prev_w['h'], 'type': 'W_high', 'weight': 3.0},
            {'price': prev_w['l'], 'type': 'W_low',  'weight': 3.0},
            {'price': (prev_w['h']+prev_w['l'])/2, 'type': 'W_mid', 'weight': 2.0},
        ]

    # Monthly levels (last 3 months)
    kl_m = bybit_klines(symbol, 'M', limit=4)
    if kl_m and len(kl_m) >= 2:
        prev_m = kl_m[-2]
        levels += [
            {'price': prev_m['h'], 'type': 'M_high', 'weight': 4.0},
            {'price': prev_m['l'], 'type': 'M_low',  'weight': 4.0},
        ]

    # POC approximation from daily volume (highest volume = magnet)
    if kl_d and len(kl_d) >= 5:
        high_vol_day = max(kl_d[-5:], key=lambda x: x['v'])
        poc = (high_vol_day['h'] + high_vol_day['l']) / 2
        levels.append({'price': poc, 'type': 'POC', 'weight': 2.5})

    return levels

def price_near_level(price, levels, atr):
    """
    Check if price is near any key level.
    Returns (closest_level, distance_pct, level_weight) or None.
    """
    if not levels or not atr: return None
    best = None
    best_dist = float('inf')
    for lv in levels:
        dist = abs(price - lv['price'])
        dist_pct = dist / price * 100
        # Within 0.3% or 1.5x ATR = at the level
        if dist_pct < 0.3 or dist < atr * 1.5:
            if dist < best_dist:
                best_dist = dist
                best = (lv, dist_pct)
    return best

# ═══════════════════════════════════════════════════════════════════
# CVD DIVERGENCE DETECTOR
# ═══════════════════════════════════════════════════════════════════
def detect_cvd_divergence(kl, cvd_data, lookback=20):
    """
    Detect bullish/bearish CVD divergence.

    Bullish: price makes LOWER LOW but CVD makes HIGHER LOW
    → sellers exhausted, buyers absorbing = long scalp

    Bearish: price makes HIGHER HIGH but CVD makes LOWER HIGH
    → buyers exhausted, sellers absorbing = short scalp

    Returns dict with divergence info.
    """
    if len(kl) < lookback or len(cvd_data) < lookback:
        return {'bull': False, 'bear': False, 'strength': 0}

    recent_kl  = kl[-lookback:]
    recent_cvd = cvd_data[-lookback:]

    # Find price swings in recent window
    price_lows  = [(i, k['l'])  for i, k in enumerate(recent_kl)]
    price_highs = [(i, k['h'])  for i, k in enumerate(recent_kl)]
    cvd_vals    = [c['cvd']     for c in recent_cvd]

    # Look for last 2 significant lows
    n = len(recent_kl)
    pivot_lows  = []
    pivot_highs = []
    lb = 3
    for i in range(lb, n-lb):
        if all(recent_kl[i]['l'] <= recent_kl[j]['l'] for j in range(i-lb, i+lb+1) if j != i):
            pivot_lows.append((i, recent_kl[i]['l'], cvd_vals[i]))
        if all(recent_kl[i]['h'] >= recent_kl[j]['h'] for j in range(i-lb, i+lb+1) if j != i):
            pivot_highs.append((i, recent_kl[i]['h'], cvd_vals[i]))

    bull_div = False
    bear_div = False
    bull_strength = 0.0
    bear_strength = 0.0
    bull_desc = ''
    bear_desc = ''

    # Bullish divergence: last 2 price lows
    if len(pivot_lows) >= 2:
        prev_l = pivot_lows[-2]
        curr_l = pivot_lows[-1]
        # Price: lower low
        if curr_l[1] < prev_l[1]:
            # CVD: higher low
            if curr_l[2] > prev_l[2]:
                price_diff = (prev_l[1] - curr_l[1]) / prev_l[1] * 100
                cvd_diff   = (curr_l[2] - prev_l[2])
                bull_div = True
                bull_strength = min(3.0, 1.0 + price_diff * 0.5)
                bull_desc = (f"Price LL ${curr_l[1]:.2f}<${prev_l[1]:.2f} "
                             f"but CVD HL {curr_l[2]:.0f}>{prev_l[2]:.0f}")

    # Bearish divergence: last 2 price highs
    if len(pivot_highs) >= 2:
        prev_h = pivot_highs[-2]
        curr_h = pivot_highs[-1]
        # Price: higher high
        if curr_h[1] > prev_h[1]:
            # CVD: lower high
            if curr_h[2] < prev_h[2]:
                price_diff = (curr_h[1] - prev_h[1]) / prev_h[1] * 100
                bear_div = True
                bear_strength = min(3.0, 1.0 + price_diff * 0.5)
                bear_desc = (f"Price HH ${curr_h[1]:.2f}>${prev_h[1]:.2f} "
                             f"but CVD LH {curr_h[2]:.0f}<{prev_h[2]:.0f}")

    return {
        'bull':           bull_div,
        'bear':           bear_div,
        'bull_strength':  bull_strength,
        'bear_strength':  bear_strength,
        'bull_desc':      bull_desc,
        'bear_desc':      bear_desc,
        'cvd_current':    cvd_vals[-1] if cvd_vals else 0,
        'cvd_prev':       cvd_vals[-2] if len(cvd_vals) >= 2 else 0,
        'cvd_rising':     len(cvd_vals) >= 2 and cvd_vals[-1] > cvd_vals[-2],
        'cvd_falling':    len(cvd_vals) >= 2 and cvd_vals[-1] < cvd_vals[-2],
    }

def detect_order_flow(kl, cvd_data, vol_avg_data):
    """
    Analyze order flow from candle data + CVD.
    Returns flow conditions dict.
    """
    if len(kl) < 5:
        return {}

    i = len(kl) - 1
    k = kl[i]
    va = vol_avg_data[i] if vol_avg_data[i] else 1.0
    cd = cvd_data[i] if cvd_data else {}

    delta  = cd.get('delta', 0)
    # Delta average over last 10 bars
    recent_deltas = [cvd_data[j]['delta'] for j in range(max(0,i-10), i) if cvd_data[j]]
    avg_delta = sum(abs(d) for d in recent_deltas) / max(len(recent_deltas), 1)

    body   = abs(k['c'] - k['o'])
    rng    = k['h'] - k['l'] + 1e-10
    body_r = body / rng  # 0=doji, 1=full body

    # Absorption: high volume but small candle = institutions absorbing
    absorption = k['v'] > va * 1.5 and body_r < 0.3

    # Delta spike = strong directional flow
    delta_spike = avg_delta > 0 and abs(delta) > avg_delta * 2.0

    # Stacked imbalance approximation: 3+ consecutive same-dir candles with growing vol
    last5 = kl[max(0,i-4):i+1]
    bull_stack = all(c['c'] > c['o'] for c in last5[-3:]) and \
                 all(last5[j]['v'] >= last5[j-1]['v'] for j in range(-2, 0))
    bear_stack = all(c['c'] < c['o'] for c in last5[-3:]) and \
                 all(last5[j]['v'] >= last5[j-1]['v'] for j in range(-2, 0))

    return {
        'delta':          delta,
        'delta_positive': delta > 0,
        'delta_negative': delta < 0,
        'delta_spike':    delta_spike,
        'absorption':     absorption,
        'bull_stack':     bull_stack,
        'bear_stack':     bear_stack,
        'body_ratio':     round(body_r, 3),
        'vol_ratio':      round(k['v']/va, 3) if va else 0,
        'is_bull_candle': k['c'] > k['o'],
        'is_bear_candle': k['c'] < k['o'],
    }

# ═══════════════════════════════════════════════════════════════════
# SCORING ENGINE — ML weighted score
# ═══════════════════════════════════════════════════════════════════
def compute_score(conditions, direction, ml_db):
    """
    Score signal using ML-adjusted weights.
    conditions = dict of active features
    direction = 'BUY' or 'SELL'
    Returns (score, active_features, reasons)
    """
    w = ml_db['weights']
    score = 0.0
    active = []
    reasons = []

    def add(feat, desc, base=1.0):
        nonlocal score
        if conditions.get(feat):
            wt = w.get(feat, base)
            score += wt
            active.append(feat)
            reasons.append(f"{desc} (+{wt:.1f})")

    # ── Key level ──────────────────────────────────────────────
    add('at_monthly_level',   'Monthly level hit')
    add('at_weekly_level',    'Weekly level hit')
    add('at_daily_level',     'Daily level hit')
    add('near_level_0.1pct',  'Very close to level')
    add('near_level_0.2pct',  'Near key level')

    # ── CVD Divergence ─────────────────────────────────────────
    if direction == 'BUY':
        add('cvd_bull_div',   'CVD bullish divergence')
        add('cvd_leading',    'CVD leading price')
    else:
        add('cvd_bear_div',   'CVD bearish divergence')
    if not conditions.get('cvd_bull_div') and not conditions.get('cvd_bear_div'):
        add('cvd_lagging',    'CVD lagging — caution', 0)
        if conditions.get('cvd_lagging'):
            score -= w.get('cvd_lagging', 0.8)

    # ── Order flow ──────────────────────────────────────────────
    add('absorption',         'Absorption at level')
    add('stacked_imbalance',  'Stacked imbalance')
    add('delta_spike',        'Delta spike')
    if direction == 'BUY':
        add('delta_positive', 'Positive delta')
        add('bid_imbalance',  'Bid imbalance (book)')
    else:
        add('delta_negative', 'Negative delta')
        add('ask_imbalance',  'Ask imbalance (book)')

    # ── Fibonacci ───────────────────────────────────────────────
    add('fib_0618',           'Fib 0.618 level')
    add('fib_0705',           'Fib 0.705 level')
    add('fib_0786',           'Fib 0.786 level')
    add('fib_0500',           'Fib 0.500 level')

    # ── RSI ─────────────────────────────────────────────────────
    if direction == 'BUY':
        add('rsi_oversold_30', 'RSI oversold <30')
        add('rsi_oversold_40', 'RSI oversold <40')
    else:
        add('rsi_overbought_70', 'RSI overbought >70')
        add('rsi_overbought_60', 'RSI overbought >60')
    add('rsi_divergence',     'RSI divergence')

    # ── Volume ──────────────────────────────────────────────────
    add('vol_climax',         'Volume climax')
    add('vol_spike_2x',       'Volume spike 2x')
    add('vol_spike_1.5x',     'Volume spike 1.5x')
    add('vol_dry_up',         'Volume dry-up (exhaustion)')

    # ── Order blocks ────────────────────────────────────────────
    if direction == 'BUY':
        add('ob_sweep_bull',  'Bullish OB sweep+reject')
        add('ob_bullish',     'Inside bullish OB')
    else:
        add('ob_sweep_bear',  'Bearish OB sweep+reject')
        add('ob_bearish',     'Inside bearish OB')

    # ── Structure ───────────────────────────────────────────────
    add('choch_confirmed',    'CHoCH confirmed')
    add('bos_confirmed',      'BOS confirmed')
    add('swing_sweep',        'Swing swept')

    # ── CRITICAL: Timing penalty ────────────────────────────────
    # If price already moved significantly = late signal
    if conditions.get('entry_at_level'):
        sc = w.get('entry_at_level', 2.0)
        score += sc
        active.append('entry_at_level')
        reasons.append(f"Entry AT level (+{sc:.1f})")
    if conditions.get('entry_after_move'):
        pen = w.get('entry_after_move', 0.3)
        score -= (1.0 - pen) * 3.0  # heavy penalty
        reasons.append(f"Entry AFTER move (LATE signal penalty)")

    move_pct = conditions.get('move_pct', 0)
    if move_pct <= 0.1:
        sc = w.get('move_pct_0.1', 1.8)
        score += sc
        reasons.append(f"Early entry 0.1% move (+{sc:.1f})")
    elif move_pct <= 0.5:
        sc = w.get('move_pct_0.5', 1.0)
        score += sc
    elif move_pct >= 1.0:
        pen = w.get('move_pct_1.0', 0.4)
        score -= (1.0 - pen) * 2.0
        reasons.append(f"Late entry {move_pct:.1f}% move already happened (penalty)")

    return round(max(0, min(12, score)), 2), active, reasons

# ═══════════════════════════════════════════════════════════════════
# SIGNAL ENGINE — full confluence check
# ═══════════════════════════════════════════════════════════════════
def analyze_pair(symbol, tf, ml_db, levels_cache):
    """
    Full analysis for one pair + timeframe.
    Returns signal dict or None.
    """
    kl = bybit_klines(symbol, tf, limit=300)
    if not kl or len(kl) < 100:
        log.info(f"  {symbol}/{tf}: insufficient data")
        return None

    n = len(kl)
    i = n - 1
    closes = [k['c'] for k in kl]
    vols   = [k['v'] for k in kl]
    price  = closes[i]
    k_cur  = kl[i]

    # Indicators
    rsi_a   = calc_rsi(closes)
    atr_a   = calc_atr(kl)
    va_a    = calc_vol_avg(vols)
    e20_a   = calc_ema(closes, 20)
    e50_a   = calc_ema(closes, 50)
    cvd_a   = calc_cvd(kl)
    sh, sl  = calc_swings(kl, lb=5)

    rsi_v = rsi_a[i]
    atr_v = atr_a[i]
    va_v  = va_a[i]
    e20_v = e20_a[i]
    e50_v = e50_a[i]

    if not all([rsi_v, atr_v, va_v]):
        return None

    # Key levels
    lv_key = f"{symbol}_levels"
    if lv_key not in levels_cache or \
       time.time() - levels_cache.get(f"{lv_key}_time", 0) > 3600:
        levels = fetch_key_levels(symbol)
        levels_cache[lv_key] = levels
        levels_cache[f"{lv_key}_time"] = time.time()
        save_levels(levels_cache)
    else:
        levels = levels_cache[lv_key]

    # Check if price is near a key level
    near = price_near_level(price, levels, atr_v)
    if not near:
        log.info(f"  {symbol}/{tf}: price not near key level (price={price:.2f})")
        return None

    closest_level, dist_pct = near
    log.info(f"  {symbol}/{tf}: AT level {closest_level['type']} ${closest_level['price']:.2f} dist={dist_pct:.3f}%")

    # CVD analysis
    cvd_div = detect_cvd_divergence(kl, cvd_a, lookback=25)
    of_data = detect_order_flow(kl, cvd_a, va_a)

    # Order book
    ob_data    = bybit_orderbook(symbol, limit=50)
    ob_analysis = analyze_orderbook(ob_data)

    # Order blocks
    obs = find_order_blocks(kl, lookback=40)

    # Fibonacci: use last significant swing
    recent_sh = sh[-3:] if len(sh) >= 3 else sh
    recent_sl = sl[-3:] if len(sl) >= 3 else sl
    fib_levels = {}
    fib_hit = {}
    if recent_sh and recent_sl:
        last_high = max(recent_sh, key=lambda x: x[0])[1]
        last_low  = min(recent_sl, key=lambda x: x[0])[1]
        fib_levels = calc_fibs(last_high, last_low)
        for lvl, fib_p in fib_levels.items():
            if abs(price - fib_p) / price < 0.002:  # within 0.2%
                fib_hit[lvl] = True

    # Determine direction
    # BUY: at support level + bullish signals
    # SELL: at resistance level + bearish signals
    level_type = closest_level['type']
    is_support    = any(x in level_type for x in ['low', 'Low', 'bot'])
    is_resistance = any(x in level_type for x in ['high', 'High', 'top'])
    is_mid        = 'mid' in level_type or 'mid' in level_type

    # Direction from CVD divergence (strongest signal)
    if cvd_div['bull']:
        direction = 'BUY'
    elif cvd_div['bear']:
        direction = 'SELL'
    elif is_support:
        direction = 'BUY'
    elif is_resistance:
        direction = 'SELL'
    elif cvd_div['cvd_rising']:
        direction = 'BUY'
    else:
        direction = 'SELL'

    # How much has price moved FROM the level already?
    level_price = closest_level['price']
    move_from_level = abs(price - level_price) / level_price * 100

    # Build conditions dict
    conditions = {
        # Key level
        'at_daily_level':   'D_' in level_type,
        'at_weekly_level':  'W_' in level_type,
        'at_monthly_level': 'M_' in level_type,
        'near_level_0.1pct': dist_pct < 0.1,
        'near_level_0.2pct': dist_pct < 0.2,

        # CVD
        'cvd_bull_div':  cvd_div['bull'],
        'cvd_bear_div':  cvd_div['bear'],
        'cvd_leading':   cvd_div['bull'] and cvd_div['cvd_rising'],
        'cvd_lagging':   not cvd_div['bull'] and not cvd_div['bear'],

        # Order flow
        'delta_positive':    of_data.get('delta_positive', False),
        'delta_negative':    of_data.get('delta_negative', False),
        'delta_spike':       of_data.get('delta_spike', False),
        'absorption':        of_data.get('absorption', False),
        'stacked_imbalance': of_data.get('bull_stack' if direction=='BUY' else 'bear_stack', False),
        'bid_imbalance':     ob_analysis.get('bid_imbalance', False),
        'ask_imbalance':     ob_analysis.get('ask_imbalance', False),

        # Fib
        'fib_0618': fib_hit.get('0.618', False),
        'fib_0705': fib_hit.get('0.705', False),
        'fib_0786': fib_hit.get('0.786', False),
        'fib_0500': fib_hit.get('0.500', False),

        # RSI
        'rsi_oversold_30':   rsi_v < 30,
        'rsi_oversold_40':   rsi_v < 40,
        'rsi_overbought_70': rsi_v > 70,
        'rsi_overbought_60': rsi_v > 60,
        'rsi_divergence':    False,  # enhanced below

        # Volume
        'vol_spike_2x':  of_data.get('vol_ratio', 0) > 2.0,
        'vol_spike_1.5x':of_data.get('vol_ratio', 0) > 1.5,
        'vol_dry_up':    of_data.get('vol_ratio', 0) < 0.5,
        'vol_climax':    of_data.get('vol_ratio', 0) > 3.0,

        # OB
        'ob_bullish':    any(o['type']=='bull' and kl[-1]['l'] >= o['bot'] and kl[-1]['l'] <= o['top'] for o in obs),
        'ob_bearish':    any(o['type']=='bear' and kl[-1]['h'] <= o['top'] and kl[-1]['h'] >= o['bot'] for o in obs),
        'ob_sweep_bull': any(o['type']=='bull' and o['swept'] for o in obs[:3]),
        'ob_sweep_bear': any(o['type']=='bear' and o['swept'] for o in obs[:3]),

        # Timing — CRITICAL
        'entry_at_level':   move_from_level < 0.15,
        'entry_after_move': move_from_level > 0.8,
        'move_pct':         move_from_level,
    }

    # RSI divergence check (approximate)
    if len(rsi_a) >= 10 and rsi_a[-1] and rsi_a[-5]:
        if direction == 'BUY' and closes[-1] < closes[-5] and rsi_a[-1] > rsi_a[-5]:
            conditions['rsi_divergence'] = True
        if direction == 'SELL' and closes[-1] > closes[-5] and rsi_a[-1] < rsi_a[-5]:
            conditions['rsi_divergence'] = True

    # Score it
    score, active_feats, reasons = compute_score(conditions, direction, ml_db)
    min_score = ml_db.get('min_score', 8.0)

    log.info(f"  {symbol}/{tf}: dir={direction} score={score:.1f} (min={min_score}) "
             f"rsi={rsi_v:.0f} cvd_bull={cvd_div['bull']} cvd_bear={cvd_div['bear']} "
             f"move_from_level={move_from_level:.2f}%")

    if score < min_score:
        log.info(f"  {symbol}/{tf}: score {score:.1f} < {min_score} — skip")
        return None

    # ── SL / TP CALCULATION ──────────────────────────────────────
    is_buy = direction == 'BUY'

    # SL: just beyond the level + ATR buffer
    if is_buy:
        sl_p = level_price - atr_v * 0.5
        # Don't let SL be > 1% away
        if (price - sl_p) / price > 0.010:
            sl_p = price - price * 0.010
    else:
        sl_p = level_price + atr_v * 0.5
        if (sl_p - price) / price > 0.010:
            sl_p = price + price * 0.010

    risk = abs(price - sl_p)
    if risk <= 0: return None

    # TP based on next key level + RR
    tp1 = price + risk*1.5 if is_buy else price - risk*1.5
    tp2 = price + risk*2.5 if is_buy else price - risk*2.5
    tp3 = price + risk*4.0 if is_buy else price - risk*4.0

    # Find next level as TP target
    if levels:
        if is_buy:
            above = [lv['price'] for lv in levels if lv['price'] > price]
            if above:
                next_lv = min(above)
                if next_lv > tp1:
                    tp2 = next_lv  # use next level as TP2
        else:
            below = [lv['price'] for lv in levels if lv['price'] < price]
            if below:
                next_lv = max(below)
                if next_lv < tp1:
                    tp2 = next_lv

    rr = round(abs(tp2-price)/risk, 1)
    risk_pct  = round(risk/price*100, 3)
    conf      = min(98, round(score/12*100))

    return {
        'sym':          symbol,
        'tf':           tf,
        'dir':          direction,
        'score':        score,
        'conf':         conf,
        'price':        price,
        'entry':        price,
        'sl':           round(sl_p, 4),
        'tp1':          round(tp1, 4),
        'tp2':          round(tp2, 4),
        'tp3':          round(tp3, 4),
        'rr':           rr,
        'risk_pct':     risk_pct,
        'level_hit':    closest_level,
        'level_dist':   round(dist_pct, 4),
        'move_from_lv': round(move_from_level, 3),
        'rsi':          round(rsi_v, 1),
        'atr':          round(atr_v, 4),
        'vol_ratio':    round(of_data.get('vol_ratio', 0), 2),
        'cvd_div':      cvd_div,
        'fib_hit':      fib_hit,
        'ob_data':      ob_analysis,
        'active_feats': active_feats,
        'reasons':      reasons,
        'conditions':   {k: v for k,v in conditions.items() if isinstance(v, bool)},
        'time':         datetime.now(timezone.utc).isoformat(),
        'is_early':     move_from_level < 0.2,
    }

# ═══════════════════════════════════════════════════════════════════
# TELEGRAM MESSAGES
# ═══════════════════════════════════════════════════════════════════
def send_tg(msg):
    if not TG_TOKEN or not TG_CHAT: return False
    try:
        r = requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT, 'text': msg,
                  'parse_mode': 'HTML', 'disable_web_page_preview': True},
            timeout=10
        )
        if r.ok: return True
        log.warning(f"TG: {r.json().get('description','?')}")
        # retry plain
        r2 = requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT, 'text': msg[:4000]},
            timeout=10
        )
        return r2.ok
    except Exception as e:
        log.error(f"TG error: {e}"); return False

def fp(p, sym=''):
    """Format price"""
    if not p: return '—'
    if p >= 10000: return f'${p:,.2f}'
    if p >= 100:   return f'${p:.2f}'
    if p >= 1:     return f'${p:.4f}'
    return f'${p:.6f}'

def build_signal_msg(sig):
    ib = sig['dir'] == 'BUY'
    lv = sig['level_hit']
    cvd = sig['cvd_div']
    fib = sig['fib_hit']
    ob  = sig['ob_data']

    # Early/late indicator
    timing = '🟢 EARLY ENTRY' if sig['is_early'] else f"⚠️ {sig['move_from_lv']:.1f}% from level"

    # CVD description
    cvd_line = ''
    if cvd['bull']:
        cvd_line = f"📉📈 CVD Bull Div: {cvd['bull_desc']}"
    elif cvd['bear']:
        cvd_line = f"📈📉 CVD Bear Div: {cvd['bear_desc']}"

    # Fib hits
    fib_line = ''
    if fib:
        fib_line = f"📐 Fib: {', '.join(fib.keys())} levels"

    # OB line
    ob_line = ''
    if ob:
        ratio = ob.get('bid_ratio', 0) if ib else ob.get('ask_ratio', 0)
        ob_line = f"📖 OB: {'Bid' if ib else 'Ask'} dom {ratio:.0%}"

    # Reason list (top 5)
    top_reasons = sig['reasons'][:6]
    reasons_str = '\n'.join(f"  {'✅' if '+' in r else '⚠️'} {r}" for r in top_reasons)

    return '\n'.join(filter(None, [
        f"{'🟢' if ib else '🔴'} <b>{'BUY' if ib else 'SELL'} SCALP — {sig['sym']}</b>  [{sig['tf']}m]",
        f"",
        f"⏱ {timing}",
        f"📍 Level: <code>{lv['type']}</code> @ {fp(lv['price'])} (dist: {sig['level_dist']:.2f}%)",
        cvd_line,
        fib_line,
        ob_line,
        f"",
        f"📖 <b>Why this trade:</b>",
        reasons_str,
        f"",
        f"💰 <b>Trade Levels</b>",
        f"  Entry:  <code>{fp(sig['entry'])}</code>",
        f"  SL:     <code>{fp(sig['sl'])}</code>  (-{sig['risk_pct']}%)",
        f"  TP1:    <code>{fp(sig['tp1'])}</code>  (1:1.5 — close 50%)",
        f"  TP2:    <code>{fp(sig['tp2'])}</code>  (1:{sig['rr']} — close 30%)",
        f"  TP3:    <code>{fp(sig['tp3'])}</code>  (runner 20%)",
        f"",
        f"📊 Score: <b>{sig['score']}/12</b>  Conf: {sig['conf']}%  RSI: {sig['rsi']}",
        f"  Vol: {sig['vol_ratio']:.1f}x avg  ATR: {fp(sig['atr'])}",
        f"",
        f"⚡ <i>Intraday scalp — manage at TP1. Not financial advice.</i>",
        f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC  |  🤖 <b>ML Scalp Bot v1</b>",
    ]))

def build_tp_msg(trade, tp_num, exit_price):
    ib = trade['dir'] == 'BUY'
    pnl = ((exit_price-trade['entry'])/trade['entry']*100) if ib \
          else ((trade['entry']-exit_price)/trade['entry']*100)
    action = {1: "Close 50%, move SL to entry", 2: "Close 30%, let runner go", 3: "Full exit"}.get(tp_num,'')
    return (
        f"🎯 <b>TP{tp_num} HIT — {trade['sym']} +{pnl:.2f}%</b>\n\n"
        f"{'BUY' if ib else 'SELL'} {trade['tf']}m\n"
        f"Entry {fp(trade['entry'])} → {fp(exit_price)}\n\n"
        f"💡 {action}\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC  |  🤖 ML Scalp Bot"
    )

def build_sl_msg(trade, exit_price, reason=''):
    ib = trade['dir'] == 'BUY'
    pnl = ((exit_price-trade['entry'])/trade['entry']*100) if ib \
          else ((trade['entry']-exit_price)/trade['entry']*100)
    feats = trade.get('active_feats', [])
    warn = []
    if trade.get('is_early') is False:
        warn.append("Entry was late (after move)")
    if 'cvd_bull_div' not in feats and 'cvd_bear_div' not in feats:
        warn.append("No CVD divergence confirmed")
    warn_str = '\n'.join(f"  ⚠️ {w}" for w in warn) if warn else "  ℹ️ Valid setup — macro moved against"
    return (
        f"❌ <b>SL HIT — {trade['sym']} {pnl:.2f}%</b>\n\n"
        f"{'BUY' if ib else 'SELL'} {trade['tf']}m\n"
        f"Entry {fp(trade['entry'])} → SL {fp(exit_price)}\n\n"
        f"🔍 <b>Analysis:</b>\n{warn_str}\n\n"
        f"🧠 <i>ML adjusting weights to avoid this pattern next time...</i>\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC  |  🤖 ML Scalp Bot"
    )

# ═══════════════════════════════════════════════════════════════════
# ML LEARNING ENGINE
# Called after every trade close
# ═══════════════════════════════════════════════════════════════════
def ml_update(trade, result, exit_price, ml_db):
    """
    Update ML weights based on trade outcome.
    result: 'win' | 'loss' | 'be'
    Core insight: if trade won → boost weights of conditions that were present
                  if trade lost → reduce weights of conditions that were present
    Extra: if signal was LATE (entry_after_move) → extra penalty on timing weights
    """
    lr = 0.10  # learning rate
    feats = trade.get('active_feats', [])
    w = ml_db['weights']
    is_win  = result == 'win'
    is_loss = result == 'loss'

    changes = []

    # Update each active feature
    for feat in feats:
        if feat not in w: continue
        old = w[feat]
        if is_win:
            # Boost: feature was present in a winning trade
            new = min(4.0, old + lr * 0.5)
        elif is_loss:
            # Reduce: feature was present in losing trade
            new = max(0.1, old - lr * 0.5)
        else:
            # BE: small positive nudge
            new = min(4.0, old + lr * 0.1)
        w[feat] = round(new, 3)
        if abs(new - old) > 0.02:
            changes.append(f"{'↑' if new>old else '↓'} {feat}: {old:.2f}→{new:.2f}")

    # Extra learning: late signal penalty
    if not trade.get('is_early', True) and is_loss:
        # Extra penalty on timing weights
        for tf in ['entry_after_move', 'move_pct_1.0']:
            old = w.get(tf, 0.3)
            w[tf] = max(0.01, old - lr)
            changes.append(f"↓↓ TIMING {tf}: {old:.2f}→{w[tf]:.2f} (late signal loss)")
        # Also penalize the conditions that triggered despite late entry
        for feat in ['cvd_lagging', 'rsi_divergence']:
            if feat in feats:
                old = w.get(feat, 1.0)
                w[feat] = max(0.1, old - lr * 0.3)

    # Extra learning: if CVD divergence was present and trade WON
    if is_win and ('cvd_bull_div' in feats or 'cvd_bear_div' in feats):
        for feat in ['cvd_bull_div', 'cvd_bear_div']:
            if feat in feats:
                old = w.get(feat, 2.0)
                w[feat] = min(4.0, old + lr)
                changes.append(f"↑↑ CVD {feat}: {old:.2f}→{w[feat]:.2f} (CVD win boost)")

    # If early entry won → boost early timing weights
    if trade.get('is_early') and is_win:
        old = w.get('entry_at_level', 2.0)
        w['entry_at_level'] = min(4.0, old + lr * 0.5)
        changes.append(f"↑ entry_at_level: {old:.2f}→{w['entry_at_level']:.2f}")

    # Adjust min_score based on recent performance
    recent = ml_db.get('learning_log', [])[-20:]
    if len(recent) >= 10:
        recent_wins = sum(1 for l in recent if l.get('result')=='win')
        wr = recent_wins / len(recent)
        if wr > 0.60 and ml_db['min_score'] > 7.0:
            ml_db['min_score'] = max(7.0, ml_db['min_score'] - 0.1)
            changes.append(f"↓ min_score: {ml_db['min_score']:.1f} (WR {wr:.0%})")
        elif wr < 0.35 and ml_db['min_score'] < 11.0:
            ml_db['min_score'] = min(11.0, ml_db['min_score'] + 0.2)
            changes.append(f"↑ min_score: {ml_db['min_score']:.1f} (WR {wr:.0%})")

    # Update stats
    ml_db['total_trades'] += 1
    ml_db['weights'] = w
    if is_win:   ml_db['wins']   += 1
    elif is_loss:ml_db['losses'] += 1
    else:        ml_db['be']     += 1

    # Per-pair stats
    sym = trade['sym']
    if sym not in ml_db['by_pair']:
        ml_db['by_pair'][sym] = {'w':0,'l':0,'be':0,'pnl':0.0}
    pd = ml_db['by_pair'][sym]
    pd['w' if is_win else ('l' if is_loss else 'be')] += 1
    pnl_val = ((exit_price-trade['entry'])/trade['entry']*100) * (1 if trade['dir']=='BUY' else -1)
    pd['pnl'] = round(pd['pnl'] + pnl_val, 3)
    ml_db['total_pnl'] = round(ml_db.get('total_pnl', 0) + pnl_val, 3)

    # Per-TF stats
    tf = trade.get('tf', '5')
    if tf not in ml_db['by_tf']:
        ml_db['by_tf'][tf] = {'w':0,'l':0,'pnl':0.0}
    ml_db['by_tf'][tf]['w' if is_win else 'l'] += 1
    ml_db['by_tf'][tf]['pnl'] = round(ml_db['by_tf'][tf]['pnl'] + pnl_val, 3)

    # Log
    if changes:
        entry = {
            'time':    datetime.now(timezone.utc).isoformat(),
            'sym':     sym,
            'result':  result,
            'pnl':     round(pnl_val, 3),
            'changes': changes[:8],
            'is_early':trade.get('is_early', False),
        }
        ml_db['learning_log'].append(entry)
        if len(ml_db['learning_log']) > 500:
            ml_db['learning_log'] = ml_db['learning_log'][-500:]
        log.info(f"ML updated for {sym} {result}: {len(changes)} weight changes")

    save_ml(ml_db)
    return changes

# ═══════════════════════════════════════════════════════════════════
# BACKTEST ENGINE
# Tests all timeframes, finds best per pair
# ═══════════════════════════════════════════════════════════════════
def run_backtest(symbol, ml_db):
    """
    Backtest all timeframes for a symbol.
    Returns best timeframe and results.
    """
    log.info(f"Backtesting {symbol}...")
    results = {}
    dummy_levels_cache = {}

    for tf in TIMEFRAMES:
        kl = bybit_klines(symbol, tf, limit=500)
        if not kl or len(kl) < 150:
            continue

        # Simulate trading on historical data
        trades = []
        in_trade = False
        trade_dir = None
        entry_p = 0
        sl_p = 0
        tp1_p = 0
        tp2_p = 0

        # Fetch key levels once
        levels = fetch_key_levels(symbol)

        for idx in range(100, len(kl)-1):
            sub_kl = kl[:idx+1]
            sub_closes = [k['c'] for k in sub_kl]
            sub_vols = [k['v'] for k in sub_kl]
            price = sub_kl[-1]['c']
            atr_a = calc_atr(sub_kl)
            va_a  = calc_vol_avg(sub_vols)
            rsi_a = calc_rsi(sub_closes)
            cvd_a = calc_cvd(sub_kl)
            atr_v = atr_a[-1]
            va_v  = va_a[-1]
            rsi_v = rsi_a[-1]

            if not all([atr_v, va_v, rsi_v]):
                continue

            if in_trade:
                # Check TP/SL
                next_k = kl[idx+1]
                if trade_dir == 'BUY':
                    if next_k['l'] <= sl_p:
                        pnl = (sl_p - entry_p) / entry_p * 100
                        trades.append({'result':'loss','pnl':round(pnl,3),'tf':tf})
                        in_trade = False
                    elif next_k['h'] >= tp2_p:
                        pnl = (tp2_p - entry_p) / entry_p * 100
                        trades.append({'result':'win','pnl':round(pnl,3),'tf':tf})
                        in_trade = False
                    elif next_k['h'] >= tp1_p:
                        pnl = (tp1_p - entry_p) / entry_p * 100
                        trades.append({'result':'win','pnl':round(pnl,3),'tf':tf})
                        in_trade = False
                else:
                    if next_k['h'] >= sl_p:
                        pnl = (entry_p - sl_p) / entry_p * 100
                        trades.append({'result':'loss','pnl':round(pnl,3),'tf':tf})
                        in_trade = False
                    elif next_k['l'] <= tp2_p:
                        pnl = (entry_p - tp2_p) / entry_p * 100
                        trades.append({'result':'win','pnl':round(pnl,3),'tf':tf})
                        in_trade = False
                continue

            # Look for entry
            near = price_near_level(price, levels, atr_v)
            if not near:
                continue

            closest_level, dist_pct = near
            if dist_pct > 0.25:
                continue  # not close enough

            cvd_div = detect_cvd_divergence(sub_kl, cvd_a, lookback=20)
            of_data = detect_order_flow(sub_kl, cvd_a, va_a)
            vol_ratio = sub_kl[-1]['v'] / va_v if va_v else 0

            # Simple backtest score
            bt_score = 0
            direction = 'BUY' if cvd_div['bull'] else 'SELL'
            if cvd_div['bull'] or cvd_div['bear']: bt_score += 3
            if of_data.get('absorption'): bt_score += 2
            if of_data.get('delta_spike'): bt_score += 1.5
            if vol_ratio > 1.5: bt_score += 1
            if (direction=='BUY' and rsi_v < 40) or (direction=='SELL' and rsi_v > 60): bt_score += 1
            if dist_pct < 0.15: bt_score += 2  # very close to level

            # Only take high-quality backtest signals
            if bt_score < 6:
                continue

            # Set trade levels
            is_buy = direction == 'BUY'
            entry_p = price
            sl_p    = closest_level['price'] - atr_v*0.5 if is_buy else closest_level['price'] + atr_v*0.5
            risk    = abs(entry_p - sl_p)
            tp1_p   = entry_p + risk*1.5 if is_buy else entry_p - risk*1.5
            tp2_p   = entry_p + risk*2.5 if is_buy else entry_p - risk*2.5
            trade_dir = direction
            in_trade  = True

            if len(trades) >= 50:
                break

        if not trades:
            results[tf] = {'trades': 0, 'wr': 0, 'pnl': 0, 'score': 0}
            continue

        wins   = sum(1 for t in trades if t['result']=='win')
        losses = sum(1 for t in trades if t['result']=='loss')
        total  = len(trades)
        wr     = wins/total if total > 0 else 0
        pnl    = sum(t['pnl'] for t in trades)
        pf     = (wins * abs(sum(t['pnl'] for t in trades if t['result']=='win'))) / \
                 max(0.01, losses * abs(sum(t['pnl'] for t in trades if t['result']=='loss')))

        # Score: weighted combination of WR, PnL, profit factor
        score = wr * 40 + min(pnl, 20) + min(pf, 3) * 5

        results[tf] = {
            'trades': total,
            'wins':   wins,
            'losses': losses,
            'wr':     round(wr*100, 1),
            'pnl':    round(pnl, 2),
            'pf':     round(pf, 2),
            'score':  round(score, 2),
        }
        log.info(f"  BT {symbol}/{tf}: {total}tr WR:{wr*100:.0f}% PnL:{pnl:.1f}% PF:{pf:.2f} score={score:.1f}")

    if not results:
        return '5', {}  # default

    best_tf = max(results, key=lambda x: results[x].get('score', 0))
    log.info(f"  Best TF for {symbol}: {best_tf}m (score={results[best_tf].get('score',0):.1f})")
    return best_tf, results

def run_all_backtests():
    """Run backtest for all pairs, store results"""
    log.info("="*50)
    log.info("BACKTEST: Starting for all pairs...")
    ml_db = load_ml()
    bt_results = load_backtest()
    send_tg("🔬 <b>ML Scalp Bot — Running Backtest</b>\nTesting all timeframes for each pair. This takes ~5 mins...")

    for sym in PAIRS:
        try:
            best_tf, results = run_backtest(sym, ml_db)
            state['best_tf'][sym] = best_tf
            bt_results[sym] = {
                'best_tf': best_tf,
                'results': results,
                'time':    datetime.now(timezone.utc).isoformat(),
            }
            save_backtest(bt_results)
            time.sleep(1)
        except Exception as e:
            log.error(f"BT {sym}: {e}")
            state['best_tf'][sym] = '5'

    state['bt_done'] = True
    with open(BT_DONE_FILE, 'w') as f:
        json.dump({'done': True, 'time': datetime.now(timezone.utc).isoformat()}, f)

    # Build report
    lines = ["🔬 <b>Backtest Complete</b>\n"]
    for sym, res in bt_results.items():
        best = res.get('best_tf', '5')
        r    = res.get('results', {}).get(best, {})
        lines.append(
            f"  <b>{sym}</b>: {best}m — "
            f"WR:{r.get('wr',0):.0f}% PnL:{r.get('pnl',0):+.1f}% "
            f"PF:{r.get('pf',0):.1f} ({r.get('trades',0)} trades)"
        )
    lines.append(f"\n🟢 <b>Bot now live scanning with best timeframes!</b>")
    send_tg('\n'.join(lines))
    log.info("BACKTEST: Complete")

# ═══════════════════════════════════════════════════════════════════
# PRICE MONITOR — TP/SL hit detection
# ═══════════════════════════════════════════════════════════════════
def monitor_open_trades():
    """Check all open trades for TP/SL hits"""
    for sym in list(state['open_trades'].keys()):
        trade = state['open_trades'][sym]
        ticker = bybit_ticker(sym)
        if not ticker:
            continue
        price = ticker['price']
        ib = trade['dir'] == 'BUY'
        en = trade['entry']

        # TP1
        if not trade.get('tp1_hit'):
            if (ib and price >= trade['tp1']) or (not ib and price <= trade['tp1']):
                trade['tp1_hit'] = True
                send_tg(build_tp_msg(trade, 1, price))
                log.info(f"  TP1 hit: {sym}")

        # TP2 — WIN
        if not trade.get('tp2_hit'):
            if (ib and price >= trade['tp2']) or (not ib and price <= trade['tp2']):
                trade['tp2_hit'] = True
                send_tg(build_tp_msg(trade, 2, price))
                pnl = ((price-en)/en*100) if ib else ((en-price)/en*100)
                _close_trade(sym, trade, 'win', price)
                continue

        # TP3 — RUNNER
        if trade.get('tp2_hit') and not trade.get('tp3_hit'):
            if (ib and price >= trade['tp3']) or (not ib and price <= trade['tp3']):
                trade['tp3_hit'] = True
                send_tg(build_tp_msg(trade, 3, price))
                _close_trade(sym, trade, 'win', price)
                continue

        # SL — LOSS
        if (ib and price <= trade['sl']) or (not ib and price >= trade['sl']):
            send_tg(build_sl_msg(trade, price))
            _close_trade(sym, trade, 'loss', price)

def _close_trade(sym, trade, result, exit_price):
    """Close trade, update ML, journal"""
    ml_db = load_ml()
    changes = ml_update(trade, result, exit_price, ml_db)
    save_ml(ml_db)

    # Journal
    j = load_journal()
    ib = trade['dir'] == 'BUY'
    pnl = ((exit_price-trade['entry'])/trade['entry']*100) if ib \
          else ((trade['entry']-exit_price)/trade['entry']*100)
    trade['result']     = result
    trade['exit_price'] = exit_price
    trade['exit_time']  = datetime.now(timezone.utc).isoformat()
    trade['pnl']        = round(pnl, 3)
    j['closed'].append(trade)
    if sym in j['open']: del j['open'][sym]
    save_journal(j)

    del state['open_trades'][sym]
    log.info(f"Trade closed: {sym} {result} {pnl:+.2f}% | ML changes: {len(changes)}")

# ═══════════════════════════════════════════════════════════════════
# MAIN SCAN LOOP
# ═══════════════════════════════════════════════════════════════════
last_fired = {}
levels_cache = {}

def run_scan():
    state['scans'] += 1
    ml_db = load_ml()
    log.info(f"Scan #{state['scans']} | min_score={ml_db['min_score']:.1f} | open={len(state['open_trades'])}")

    # Monitor open trades first
    try:
        monitor_open_trades()
    except Exception as e:
        log.error(f"Monitor: {e}")

    for sym in PAIRS:
        # Cooldown
        lf = last_fired.get(sym, 0)
        if time.time() - lf < COOLDOWN_MIN * 60:
            remain = int((COOLDOWN_MIN*60 - (time.time()-lf)) / 60)
            log.info(f"  {sym}: cooldown {remain}m")
            continue

        # Already in trade
        if sym in state['open_trades']:
            log.info(f"  {sym}: already in open trade")
            continue

        # Get best timeframe from backtest
        tf = state['best_tf'].get(sym, '5')

        try:
            sig = analyze_pair(sym, tf, ml_db, levels_cache)
            if not sig:
                continue

            # Fire signal
            msg = build_signal_msg(sig)
            ok = send_tg(msg)
            if ok:
                last_fired[sym] = time.time()
                state['signals_sent'] += 1

                # Store open trade
                trade = {**sig, 'tp1_hit': False, 'tp2_hit': False, 'tp3_hit': False}
                state['open_trades'][sym] = trade

                # Journal
                j = load_journal()
                j['signals'].append(sig)
                j['open'][sym] = sig['time']
                save_journal(j)

                log.info(f"  SIGNAL: {sym} {sig['dir']} score={sig['score']} tf={tf}m → TG ✓")
        except Exception as e:
            log.error(f"  {sym} scan error: {e}")

        time.sleep(0.3)

# ═══════════════════════════════════════════════════════════════════
# TG COMMANDS
# ═══════════════════════════════════════════════════════════════════
def tg_commands():
    last_upd = [0]
    while True:
        try:
            r = requests.get(
                f'https://api.telegram.org/bot{TG_TOKEN}/getUpdates',
                params={'offset': last_upd[0]+1, 'timeout': 10},
                timeout=15
            )
            if r.ok:
                for upd in r.json().get('result', []):
                    last_upd[0] = upd['update_id']
                    txt = upd.get('message', {}).get('text', '').strip().lower()

                    if txt == '/stats':
                        ml = load_ml()
                        tot = ml['total_trades']
                        wr  = ml['wins']/tot*100 if tot else 0
                        msg = (
                            f"📊 <b>ML Scalp Bot Stats</b>\n\n"
                            f"Total trades: {tot}\n"
                            f"✅ Wins: {ml['wins']} ({wr:.1f}%)\n"
                            f"❌ Losses: {ml['losses']}\n"
                            f"➡️ BE: {ml['be']}\n"
                            f"💰 Total P&L: {ml['total_pnl']:+.2f}%\n"
                            f"🎯 Min score: {ml['min_score']:.1f}\n\n"
                            f"Open trades: {len(state['open_trades'])}\n"
                            f"Signals sent: {state['signals_sent']}\n"
                            f"Scans: {state['scans']}"
                        )
                        send_tg(msg)

                    elif txt == '/open':
                        if not state['open_trades']:
                            send_tg("✅ No open trades")
                        else:
                            lines = ["🔓 <b>Open Trades:</b>"]
                            for sym, t in state['open_trades'].items():
                                lines.append(
                                    f"  {sym} {t['dir']} {t['tf']}m @ {fp(t['entry'])} "
                                    f"SL:{fp(t['sl'])} TP2:{fp(t['tp2'])}"
                                )
                            send_tg('\n'.join(lines))

                    elif txt == '/ml':
                        ml = load_ml()
                        top_w = sorted(ml['weights'].items(), key=lambda x:-x[1])[:10]
                        lines = ["🧠 <b>Top ML Weights:</b>"]
                        for feat, val in top_w:
                            lines.append(f"  {feat}: {val:.2f}")
                        if ml['learning_log']:
                            last = ml['learning_log'][-1]
                            lines.append(f"\n<b>Last update ({last['time'][:10]}):</b>")
                            for ch in last.get('changes', [])[:4]:
                                lines.append(f"  {ch}")
                        send_tg('\n'.join(lines))

                    elif txt == '/backtest':
                        bt = load_backtest()
                        if not bt:
                            send_tg("No backtest data yet. Run /rebacktest")
                        else:
                            lines = ["🔬 <b>Backtest Results:</b>"]
                            for sym, res in bt.items():
                                best = res.get('best_tf','5')
                                r2   = res.get('results',{}).get(best,{})
                                lines.append(f"  {sym}: {best}m WR:{r2.get('wr',0):.0f}% PnL:{r2.get('pnl',0):+.1f}%")
                            send_tg('\n'.join(lines))

                    elif txt == '/rebacktest':
                        send_tg("🔄 Re-running backtest for all pairs...")
                        threading.Thread(target=run_all_backtests, daemon=True).start()

                    elif txt == '/pairs':
                        lines = [f"📡 <b>Scanning {len(PAIRS)} pairs:</b>"]
                        for sym in PAIRS:
                            tf = state['best_tf'].get(sym, '5')
                            lines.append(f"  {sym} → {tf}m")
                        send_tg('\n'.join(lines))

                    elif txt == '/help':
                        send_tg(
                            "🤖 <b>ML Scalp Bot Commands</b>\n\n"
                            "/stats      — performance stats\n"
                            "/open       — open trades\n"
                            "/ml         — ML weights\n"
                            "/backtest   — backtest results\n"
                            "/rebacktest — re-run backtest\n"
                            "/pairs      — pairs + best TF\n"
                            "/help       — this menu"
                        )
        except Exception as e:
            log.warning(f"TG cmd: {e}")
        time.sleep(30)

# ═══════════════════════════════════════════════════════════════════
# HEALTH SERVER
# ═══════════════════════════════════════════════════════════════════
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        ml = load_ml()
        tot = ml['total_trades']
        wr  = ml['wins']/tot*100 if tot else 0
        body = (
            f"ML Scalp Bot v1\n{'='*40}\n"
            f"Started:   {state['started']}\n"
            f"Scans:     {state['scans']}\n"
            f"Signals:   {state['signals_sent']}\n"
            f"Open:      {len(state['open_trades'])}\n"
            f"BT done:   {state['bt_done']}\n\n"
            f"Trades:    {tot} | WR: {wr:.1f}%\n"
            f"PnL:       {ml['total_pnl']:+.2f}%\n"
            f"Min score: {ml['min_score']:.1f}\n\n"
            f"Pairs: {', '.join(PAIRS)}\n"
            f"Best TF: {state['best_tf']}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        ).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

# ═══════════════════════════════════════════════════════════════════
# STARTUP — restore data, backtest, then scan
# ═══════════════════════════════════════════════════════════════════
def main():
    if not TG_TOKEN or not TG_CHAT:
        log.error("Need TG_TOKEN + TG_CHAT env vars")
        raise SystemExit(1)

    log.info("="*60)
    log.info("ML SCALP BOT v1 — Bybit Multi-Pair")
    log.info(f"Pairs: {PAIRS}")
    log.info(f"TFs tested: {TIMEFRAMES}")
    log.info(f"Data dir: {DATA_DIR}")
    log.info("="*60)

    # Restore previous ML data
    ml = load_ml()
    log.info(f"ML: {ml['total_trades']} prev trades | wins:{ml['wins']} losses:{ml['losses']}")

    # Restore backtest results
    bt = load_backtest()
    if bt:
        for sym, res in bt.items():
            state['best_tf'][sym] = res.get('best_tf', '5')
        log.info(f"Restored backtest for {len(bt)} pairs")

    # Check if backtest was done before
    bt_already_done = Path(BT_DONE_FILE).exists()
    if bt_already_done:
        state['bt_done'] = True
        log.info("Backtest previously completed — using stored results")

    # Health server
    threading.Thread(
        target=lambda: HTTPServer(('', PORT), Health).serve_forever(),
        daemon=True
    ).start()
    log.info(f"Health server on :{PORT}")

    # TG commands
    threading.Thread(target=tg_commands, daemon=True).start()

    # Startup message
    prev_trades = ml['total_trades']
    send_tg(
        f"🤖 <b>ML Scalp Bot v1 Started</b>\n\n"
        f"📊 Previous data loaded:\n"
        f"  Trades: {prev_trades} | W:{ml['wins']} L:{ml['losses']}\n"
        f"  PnL: {ml['total_pnl']:+.2f}%\n"
        f"  Min score: {ml['min_score']:.1f}\n\n"
        f"📡 Pairs: {', '.join(PAIRS)}\n"
        f"🔬 Backtest: {'✅ Loaded' if bt_already_done else '⏳ Running...'}\n\n"
        f"Strategy:\n"
        f"  1️⃣ Daily/Weekly/Monthly key levels\n"
        f"  2️⃣ CVD divergence confirmation\n"
        f"  3️⃣ Order flow + delta analysis\n"
        f"  4️⃣ Fibonacci + OB + RSI + Volume\n"
        f"  5️⃣ ML scoring — learns from every trade\n\n"
        f"/help — commands\n"
        f"🤖 <b>ML Scalp Bot v1</b>"
    )

    # Run backtest if not done
    if not bt_already_done:
        threading.Thread(target=run_all_backtests, daemon=True).start()
    else:
        log.info("Skipping backtest — already done")

    # Main scan loop
    log.info("Starting main scan loop...")
    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"Scan loop: {e}")
        log.info(f"Next scan in {SCAN_EVERY_MIN}m...")
        time.sleep(SCAN_EVERY_MIN * 60)

if __name__ == '__main__':
    main()
