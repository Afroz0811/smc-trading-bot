#!/usr/bin/env python3
"""
ML Scalp Bot v1 — Bybit Multi-Pair
====================================
FIX: Added proxy support + safe JSON parsing to handle Bybit CloudFront geo-block.

Set these env vars:
  TG_TOKEN     — Telegram bot token
  TG_CHAT      — Telegram chat ID
  PROXY_URL    — e.g. socks5h://user:pass@host:port  (fixes geo-block)
  BYBIT_BASE   — override API base if needed
  DATA_DIR     — persistent storage path (default /app/data)
  SCAN_EVERY_MIN / COOLDOWN_MIN
"""
import os, sys, json, time, logging, threading, requests
from datetime import datetime, timezone
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

# ═══ CONFIG ══════════════════════════════════════════════════════
TG_TOKEN   = os.environ.get('TG_TOKEN', '')
TG_CHAT    = os.environ.get('TG_CHAT', '')
PORT       = int(os.environ.get('PORT', 8080))
DATA_DIR   = os.environ.get('DATA_DIR', '/app/data')
BYBIT_BASE = os.environ.get('BYBIT_BASE', 'https://api.bybit.com')
PROXY_URL  = os.environ.get('PROXY_URL', '')

# Proxy-aware session — all Bybit calls go through this
_session = requests.Session()
if PROXY_URL:
    _session.proxies = {'http': PROXY_URL, 'https': PROXY_URL}
    print(f"[PROXY] Using: {PROXY_URL.split('@')[-1]}")
else:
    print("[PROXY] None set. If geo-blocked, set PROXY_URL env var.")

PAIRS          = ['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT',
                  'ADAUSDT','AVAXUSDT','DOGEUSDT','LINKUSDT','DOTUSDT']
TIMEFRAMES     = ['1','3','5','15']
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
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger('scalp')

# ═══ STATE ═══════════════════════════════════════════════════════
state = {
    'started':      datetime.now(timezone.utc).isoformat(),
    'scans':        0,
    'signals_sent': 0,
    'open_trades':  {},
    'last_fired':   {},
    'best_tf':      {},
    'bt_done':      False,
    'geo_blocked':  False,
}

# ═══════════════════════════════════════════════════════════════════
# ML BRAIN
# ═══════════════════════════════════════════════════════════════════
DEFAULT_ML = {
    'version': 3,
    'created': datetime.now(timezone.utc).isoformat(),
    'total_trades': 0, 'wins': 0, 'losses': 0, 'be': 0, 'total_pnl': 0.0,
    'weights': {
        'at_daily_level': 2.0, 'at_weekly_level': 2.5, 'at_monthly_level': 3.0,
        'near_level_0.1pct': 1.5, 'near_level_0.2pct': 1.2,
        'delta_positive': 1.3, 'delta_negative': 1.3, 'delta_spike': 1.5,
        'absorption': 1.8, 'bid_imbalance': 1.4, 'ask_imbalance': 1.4, 'stacked_imbalance': 1.8,
        'cvd_bull_div': 2.0, 'cvd_bear_div': 2.0, 'cvd_leading': 1.5, 'cvd_lagging': 0.8,
        'fib_0618': 1.6, 'fib_0705': 1.4, 'fib_0786': 1.5, 'fib_0500': 1.2,
        'rsi_oversold_30': 1.5, 'rsi_oversold_40': 1.2,
        'rsi_overbought_70': 1.5, 'rsi_overbought_60': 1.2, 'rsi_divergence': 1.8,
        'vol_spike_2x': 1.6, 'vol_spike_1.5x': 1.3, 'vol_dry_up': 1.4, 'vol_climax': 1.7,
        'ob_bullish': 1.5, 'ob_bearish': 1.5, 'ob_sweep_bull': 2.0, 'ob_sweep_bear': 2.0,
        'entry_at_level': 2.0, 'entry_after_move': 0.3,
        'move_pct_0.1': 1.8, 'move_pct_0.5': 1.0, 'move_pct_1.0': 0.4,
        'choch_confirmed': 1.6, 'bos_confirmed': 1.4, 'swing_sweep': 1.7,
        'hl_confirmed': 1.3, 'lh_confirmed': 1.3,
    },
    'min_score': 8.0,
    'by_pair': {}, 'by_tf': {}, 'by_setup': {}, 'by_hour': {}, 'by_session': {},
    'learning_log': [], 'last_learned': None,
    'early_signals': 0, 'late_signals': 0,
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
    except Exception as e: log.warning(f"ML save: {e}")

def load_journal():
    if Path(JOURNAL_FILE).exists():
        try:
            with open(JOURNAL_FILE) as f: return json.load(f)
        except: pass
    return {'signals': [], 'open': {}, 'closed': [], 'stats': {}}

def save_journal(j):
    try:
        with open(JOURNAL_FILE, 'w') as f: json.dump(j, f, indent=2)
    except Exception as e: log.warning(f"Journal save: {e}")

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
# BYBIT DATA FETCH — proxy-aware, geo-block safe
# ═══════════════════════════════════════════════════════════════════
def _safe_json(r, label):
    """
    Safely parse JSON from a Bybit response.
    Detects geo-blocks (CloudFront 403) and logs the real problem.
    Returns parsed dict or None.
    """
    try:
        ct = r.headers.get('Content-Type', '')
        if 'application/json' not in ct and 'text/json' not in ct:
            body = r.text[:400]
            if 'CloudFront' in body or 'block access' in body or r.status_code == 403:
                if not state['geo_blocked']:   # log once
                    state['geo_blocked'] = True
                    log.error(
                        f"GEO-BLOCKED: Bybit is blocking this server's IP (CloudFront 403).\n"
                        f"  Fix: Set PROXY_URL env var — e.g. socks5h://user:pass@host:port\n"
                        f"  Or migrate to a VPS in an allowed region (EU/US).\n"
                        f"  Body snippet: {body[:200]}"
                    )
                else:
                    log.warning(f"{label}: still geo-blocked (403)")
            else:
                log.warning(f"{label}: non-JSON response (HTTP {r.status_code}) — {body[:200]}")
            return None
        return r.json()
    except Exception as e:
        log.warning(f"{label}: JSON parse error — {e} (HTTP {r.status_code}) — {r.text[:150]}")
        return None


def bybit_klines(symbol, interval, limit=300):
    """Fetch OHLCV candles via proxy-aware session."""
    try:
        r = _session.get(
            f'{BYBIT_BASE}/v5/market/kline',
            params={'category': 'linear', 'symbol': symbol,
                    'interval': interval, 'limit': limit},
            timeout=12
        )
        d = _safe_json(r, f"kline {symbol}/{interval}")
        if d is None: return None
        if d.get('retCode') != 0:
            log.warning(f"kline {symbol}/{interval}: {d.get('retMsg')}")
            return None
        kl = []
        for k in reversed(d['result']['list']):
            kl.append({'t': int(k[0]), 'o': float(k[1]), 'h': float(k[2]),
                       'l': float(k[3]), 'c': float(k[4]), 'v': float(k[5])})
        return kl
    except Exception as e:
        log.warning(f"kline {symbol}: {e}")
        return None


def bybit_ticker(symbol):
    """Current price + 24h data."""
    try:
        r = _session.get(f'{BYBIT_BASE}/v5/market/tickers',
                         params={'category': 'linear', 'symbol': symbol}, timeout=8)
        d = _safe_json(r, f"ticker {symbol}")
        if d is None: return None
        if d.get('retCode') == 0 and d['result']['list']:
            t = d['result']['list'][0]
            return {'price':  float(t['lastPrice']),
                    'high24': float(t['highPrice24h']),
                    'low24':  float(t['lowPrice24h']),
                    'vol24':  float(t['volume24h']),
                    'chg24':  float(t.get('price24hPcnt', 0)) * 100}
    except Exception as e:
        log.warning(f"ticker {symbol}: {e}")
    return None


def bybit_orderbook(symbol, limit=50):
    """Order book for imbalance detection."""
    try:
        r = _session.get(f'{BYBIT_BASE}/v5/market/orderbook',
                         params={'category': 'linear', 'symbol': symbol, 'limit': limit},
                         timeout=8)
        d = _safe_json(r, f"ob {symbol}")
        if d is None: return None
        if d.get('retCode') == 0:
            return {
                'bids': [[float(x[0]), float(x[1])] for x in d['result']['b']],
                'asks': [[float(x[0]), float(x[1])] for x in d['result']['a']],
            }
    except Exception as e:
        log.warning(f"ob {symbol}: {e}")
    return None

# ═══════════════════════════════════════════════════════════════════
# INDICATORS
# ═══════════════════════════════════════════════════════════════════
def calc_ema(closes, period):
    if len(closes) < period: return [None] * len(closes)
    k = 2 / (period + 1)
    r = [None] * (period - 1)
    s = sum(closes[:period]) / period; r.append(s); pv = s
    for i in range(period, len(closes)):
        pv = closes[i] * k + pv * (1 - k); r.append(pv)
    return r

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return [None] * len(closes)
    r = [None] * period; g = l = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0: g += d
        else: l += abs(d)
    ag, al = g / period, l / period
    r.append(100 if al == 0 else 100 - 100 / (1 + ag / al))
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + (d if d > 0 else 0)) / period
        al = (al * (period - 1) + (abs(d) if d < 0 else 0)) / period
        r.append(100 if al == 0 else 100 - 100 / (1 + ag / al))
    return r

def calc_atr(kl, period=14):
    if len(kl) < period + 1: return [None] * len(kl)
    tr = [None] + [max(kl[i]['h'] - kl[i]['l'],
                       abs(kl[i]['h'] - kl[i-1]['c']),
                       abs(kl[i]['l'] - kl[i-1]['c'])) for i in range(1, len(kl))]
    r = [None] * period
    s = sum(tr[1:period+1]) / period; r.append(s); pv = s
    for i in range(period + 1, len(tr)):
        pv = (pv * (period - 1) + tr[i]) / period; r.append(pv)
    return r

def calc_vol_avg(vols, period=20):
    r = [None] * period
    for i in range(period, len(vols)):
        r.append(sum(vols[i - period:i]) / period)
    return r

def calc_cvd(kl):
    cvd = []; cum = 0.0
    for k in kl:
        rng = k['h'] - k['l']
        delta = 0.0 if rng == 0 else (k['c'] - k['o']) / rng * k['v']
        cum += delta
        cvd.append({'delta': delta, 'cvd': round(cum, 4), 'cum': round(cum, 4)})
    return cvd

def calc_swings(kl, lb=5):
    sh, sl = [], []
    for i in range(lb, len(kl) - lb):
        if all(kl[i]['h'] >= kl[j]['h'] for j in range(i-lb, i+lb+1) if j != i):
            sh.append((i, kl[i]['h']))
        if all(kl[i]['l'] <= kl[j]['l'] for j in range(i-lb, i+lb+1) if j != i):
            sl.append((i, kl[i]['l']))
    return sh, sl

def calc_fibs(high, low):
    diff = high - low
    return {'0.236': round(high-diff*0.236, 4), '0.382': round(high-diff*0.382, 4),
            '0.500': round(high-diff*0.500, 4), '0.618': round(high-diff*0.618, 4),
            '0.705': round(high-diff*0.705, 4), '0.786': round(high-diff*0.786, 4),
            '1.000': round(low, 4)}

def find_order_blocks(kl, lookback=30):
    obs = []; n = len(kl)
    for i in range(2, min(lookback, n - 2)):
        idx = n - 1 - i; c = kl[idx]
        if c['c'] < c['o']:
            fwd = kl[idx + 1]
            if fwd['c'] > fwd['o'] and (fwd['c']-fwd['o']) > (c['o']-c['c']) * 0.8:
                obs.append({'type':'bull','top':c['o'],'bot':c['l'],
                            'idx':idx,'swept':kl[-1]['l'] < c['l']})
        elif c['c'] > c['o']:
            fwd = kl[idx + 1]
            if fwd['c'] < fwd['o'] and (fwd['o']-fwd['c']) > (c['c']-c['o']) * 0.8:
                obs.append({'type':'bear','top':c['h'],'bot':c['c'],
                            'idx':idx,'swept':kl[-1]['h'] > c['h']})
    return obs

def analyze_orderbook(ob_data):
    if not ob_data: return {}
    bids = ob_data['bids']; asks = ob_data['asks']
    if not bids or not asks: return {}
    bv = sum(b[1] for b in bids[:10]); av = sum(a[1] for a in asks[:10]); tot = bv + av + 1e-10
    br = bv / tot; ar = av / tot
    return {'bid_ratio': round(br,3), 'ask_ratio': round(ar,3),
            'bid_imbalance': br > 0.65, 'ask_imbalance': ar > 0.65,
            'bid_dom': bv > av*1.5, 'ask_dom': av > bv*1.5,
            'thin_above': sum(a[1] for a in asks[:5]) < sum(a[1] for a in asks[5:10]) * 0.5,
            'thin_below': sum(b[1] for b in bids[:5]) < sum(b[1] for b in bids[5:10]) * 0.5,
            'total_bid': round(bv,2), 'total_ask': round(av,2)}

# ═══════════════════════════════════════════════════════════════════
# KEY LEVELS ENGINE
# ═══════════════════════════════════════════════════════════════════
def fetch_key_levels(symbol):
    levels = []
    kl_d = bybit_klines(symbol, 'D', limit=10)
    if kl_d and len(kl_d) >= 2:
        prev = kl_d[-2]
        levels += [{'price': prev['h'], 'type': 'D_high', 'weight': 2.0},
                   {'price': prev['l'], 'type': 'D_low',  'weight': 2.0},
                   {'price': (prev['h']+prev['l'])/2, 'type': 'D_mid', 'weight': 1.5}]
        for k in kl_d[-4:-1]:
            levels.append({'price': k['c'], 'type': 'D_close', 'weight': 1.3})

    kl_w = bybit_klines(symbol, 'W', limit=5)
    if kl_w and len(kl_w) >= 2:
        pw = kl_w[-2]
        levels += [{'price': pw['h'], 'type': 'W_high', 'weight': 3.0},
                   {'price': pw['l'], 'type': 'W_low',  'weight': 3.0},
                   {'price': (pw['h']+pw['l'])/2, 'type': 'W_mid', 'weight': 2.0}]

    kl_m = bybit_klines(symbol, 'M', limit=4)
    if kl_m and len(kl_m) >= 2:
        pm = kl_m[-2]
        levels += [{'price': pm['h'], 'type': 'M_high', 'weight': 4.0},
                   {'price': pm['l'], 'type': 'M_low',  'weight': 4.0}]

    if kl_d and len(kl_d) >= 5:
        hv = max(kl_d[-5:], key=lambda x: x['v'])
        levels.append({'price': (hv['h']+hv['l'])/2, 'type': 'POC', 'weight': 2.5})
    return levels

def price_near_level(price, levels, atr):
    if not levels or not atr: return None
    best = None; best_dist = float('inf')
    for lv in levels:
        dist = abs(price - lv['price']); dist_pct = dist / price * 100
        if dist_pct < 0.3 or dist < atr * 1.5:
            if dist < best_dist: best_dist = dist; best = (lv, dist_pct)
    return best

# ═══════════════════════════════════════════════════════════════════
# CVD DIVERGENCE
# ═══════════════════════════════════════════════════════════════════
def detect_cvd_divergence(kl, cvd_data, lookback=20):
    empty = {'bull': False, 'bear': False, 'bull_strength': 0, 'bear_strength': 0,
             'bull_desc': '', 'bear_desc': '', 'cvd_current': 0, 'cvd_prev': 0,
             'cvd_rising': False, 'cvd_falling': False}
    if len(kl) < lookback or len(cvd_data) < lookback: return empty
    rkl = kl[-lookback:]; rcvd = cvd_data[-lookback:]
    cvd_vals = [c['cvd'] for c in rcvd]; n = len(rkl); lb = 3
    pl = []; ph = []
    for i in range(lb, n - lb):
        if all(rkl[i]['l'] <= rkl[j]['l'] for j in range(i-lb, i+lb+1) if j != i):
            pl.append((i, rkl[i]['l'], cvd_vals[i]))
        if all(rkl[i]['h'] >= rkl[j]['h'] for j in range(i-lb, i+lb+1) if j != i):
            ph.append((i, rkl[i]['h'], cvd_vals[i]))
    bull = bear = False; bs = brs = 0.0; bd = brd = ''
    if len(pl) >= 2:
        a, b = pl[-2], pl[-1]
        if b[1] < a[1] and b[2] > a[2]:
            bull = True; bs = min(3.0, 1.0 + (a[1]-b[1])/a[1]*100*0.5)
            bd = f"Price LL ${b[1]:.2f}<${a[1]:.2f} but CVD HL {b[2]:.0f}>{a[2]:.0f}"
    if len(ph) >= 2:
        a, b = ph[-2], ph[-1]
        if b[1] > a[1] and b[2] < a[2]:
            bear = True; brs = min(3.0, 1.0 + (b[1]-a[1])/a[1]*100*0.5)
            brd = f"Price HH ${b[1]:.2f}>${a[1]:.2f} but CVD LH {b[2]:.0f}<{a[2]:.0f}"
    return {'bull': bull, 'bear': bear, 'bull_strength': bs, 'bear_strength': brs,
            'bull_desc': bd, 'bear_desc': brd,
            'cvd_current': cvd_vals[-1] if cvd_vals else 0,
            'cvd_prev': cvd_vals[-2] if len(cvd_vals) >= 2 else 0,
            'cvd_rising': len(cvd_vals) >= 2 and cvd_vals[-1] > cvd_vals[-2],
            'cvd_falling': len(cvd_vals) >= 2 and cvd_vals[-1] < cvd_vals[-2]}

def detect_order_flow(kl, cvd_data, vol_avg_data):
    if len(kl) < 5: return {}
    i = len(kl) - 1; k = kl[i]
    va = vol_avg_data[i] if vol_avg_data[i] else 1.0
    cd = cvd_data[i] if cvd_data else {}
    delta = cd.get('delta', 0)
    rd = [cvd_data[j]['delta'] for j in range(max(0,i-10), i) if cvd_data[j]]
    avg_d = sum(abs(d) for d in rd) / max(len(rd), 1)
    body = abs(k['c']-k['o']); rng = k['h']-k['l']+1e-10; br = body/rng
    last5 = kl[max(0,i-4):i+1]
    bull_s = all(c['c']>c['o'] for c in last5[-3:]) and all(last5[j]['v']>=last5[j-1]['v'] for j in range(-2,0))
    bear_s = all(c['c']<c['o'] for c in last5[-3:]) and all(last5[j]['v']>=last5[j-1]['v'] for j in range(-2,0))
    return {'delta': delta, 'delta_positive': delta>0, 'delta_negative': delta<0,
            'delta_spike': avg_d>0 and abs(delta)>avg_d*2.0,
            'absorption': k['v']>va*1.5 and br<0.3,
            'bull_stack': bull_s, 'bear_stack': bear_s,
            'body_ratio': round(br,3),
            'vol_ratio': round(k['v']/va,3) if va else 0,
            'is_bull_candle': k['c']>k['o'], 'is_bear_candle': k['c']<k['o']}

# ═══════════════════════════════════════════════════════════════════
# SCORING ENGINE
# ═══════════════════════════════════════════════════════════════════
def compute_score(conditions, direction, ml_db):
    w = ml_db['weights']; score = 0.0; active = []; reasons = []

    def add(feat, desc, base=1.0):
        nonlocal score
        if conditions.get(feat):
            wt = w.get(feat, base); score += wt
            active.append(feat); reasons.append(f"{desc} (+{wt:.1f})")

    add('at_monthly_level', 'Monthly level hit')
    add('at_weekly_level',  'Weekly level hit')
    add('at_daily_level',   'Daily level hit')
    add('near_level_0.1pct','Very close to level')
    add('near_level_0.2pct','Near key level')

    if direction == 'BUY':
        add('cvd_bull_div', 'CVD bullish divergence')
        add('cvd_leading',  'CVD leading price')
    else:
        add('cvd_bear_div', 'CVD bearish divergence')
    if not conditions.get('cvd_bull_div') and not conditions.get('cvd_bear_div'):
        if conditions.get('cvd_lagging'): score -= w.get('cvd_lagging', 0.8)

    add('absorption',        'Absorption at level')
    add('stacked_imbalance', 'Stacked imbalance')
    add('delta_spike',       'Delta spike')
    if direction == 'BUY':
        add('delta_positive','Positive delta')
        add('bid_imbalance', 'Bid imbalance (book)')
    else:
        add('delta_negative','Negative delta')
        add('ask_imbalance', 'Ask imbalance (book)')

    add('fib_0618','Fib 0.618'); add('fib_0705','Fib 0.705')
    add('fib_0786','Fib 0.786'); add('fib_0500','Fib 0.500')

    if direction == 'BUY':
        add('rsi_oversold_30','RSI oversold <30')
        add('rsi_oversold_40','RSI oversold <40')
    else:
        add('rsi_overbought_70','RSI overbought >70')
        add('rsi_overbought_60','RSI overbought >60')
    add('rsi_divergence','RSI divergence')

    add('vol_climax',   'Volume climax')
    add('vol_spike_2x', 'Volume spike 2x')
    add('vol_spike_1.5x','Volume spike 1.5x')
    add('vol_dry_up',   'Volume dry-up')

    if direction == 'BUY':
        add('ob_sweep_bull','Bullish OB sweep+reject')
        add('ob_bullish',   'Inside bullish OB')
    else:
        add('ob_sweep_bear','Bearish OB sweep+reject')
        add('ob_bearish',   'Inside bearish OB')

    add('choch_confirmed','CHoCH confirmed')
    add('bos_confirmed',  'BOS confirmed')
    add('swing_sweep',    'Swing swept')

    if conditions.get('entry_at_level'):
        sc = w.get('entry_at_level', 2.0); score += sc
        active.append('entry_at_level'); reasons.append(f"Entry AT level (+{sc:.1f})")
    if conditions.get('entry_after_move'):
        score -= (1.0 - w.get('entry_after_move', 0.3)) * 3.0
        reasons.append("Entry AFTER move (LATE penalty)")

    mp = conditions.get('move_pct', 0)
    if mp <= 0.1:
        sc = w.get('move_pct_0.1', 1.8); score += sc; reasons.append(f"Early entry (+{sc:.1f})")
    elif mp <= 0.5:
        score += w.get('move_pct_0.5', 1.0)
    elif mp >= 1.0:
        score -= (1.0 - w.get('move_pct_1.0', 0.4)) * 2.0
        reasons.append(f"Late {mp:.1f}% move (penalty)")

    return round(max(0, min(12, score)), 2), active, reasons

# ═══════════════════════════════════════════════════════════════════
# SIGNAL ENGINE
# ═══════════════════════════════════════════════════════════════════
def analyze_pair(symbol, tf, ml_db, levels_cache):
    kl = bybit_klines(symbol, tf, limit=300)
    if not kl or len(kl) < 100:
        log.info(f"  {symbol}/{tf}: insufficient data"); return None

    n = len(kl); i = n - 1
    closes = [k['c'] for k in kl]; vols = [k['v'] for k in kl]; price = closes[i]
    rsi_a = calc_rsi(closes); atr_a = calc_atr(kl)
    va_a = calc_vol_avg(vols); cvd_a = calc_cvd(kl)
    sh, sl = calc_swings(kl, lb=5)
    rsi_v = rsi_a[i]; atr_v = atr_a[i]; va_v = va_a[i]
    if not all([rsi_v, atr_v, va_v]): return None

    lv_key = f"{symbol}_levels"
    if lv_key not in levels_cache or time.time() - levels_cache.get(f"{lv_key}_time", 0) > 3600:
        levels = fetch_key_levels(symbol)
        levels_cache[lv_key] = levels; levels_cache[f"{lv_key}_time"] = time.time()
        save_levels(levels_cache)
    else:
        levels = levels_cache[lv_key]

    near = price_near_level(price, levels, atr_v)
    if not near: log.info(f"  {symbol}/{tf}: not near key level"); return None
    closest_level, dist_pct = near
    log.info(f"  {symbol}/{tf}: AT level {closest_level['type']} ${closest_level['price']:.2f} dist={dist_pct:.3f}%")

    cvd_div = detect_cvd_divergence(kl, cvd_a, lookback=25)
    of_data = detect_order_flow(kl, cvd_a, va_a)
    ob_analysis = analyze_orderbook(bybit_orderbook(symbol, limit=50))
    obs = find_order_blocks(kl, lookback=40)

    rsh = sh[-3:] if len(sh) >= 3 else sh
    rsl = sl[-3:] if len(sl) >= 3 else sl
    fib_hit = {}
    if rsh and rsl:
        fl = calc_fibs(max(rsh, key=lambda x: x[0])[1], min(rsl, key=lambda x: x[0])[1])
        for lvl, fp2 in fl.items():
            if abs(price - fp2) / price < 0.002: fib_hit[lvl] = True

    lt = closest_level['type']
    is_sup = any(x in lt for x in ['low','Low','bot'])
    is_res = any(x in lt for x in ['high','High','top'])

    if cvd_div['bull']:        direction = 'BUY'
    elif cvd_div['bear']:      direction = 'SELL'
    elif is_sup:               direction = 'BUY'
    elif is_res:               direction = 'SELL'
    elif cvd_div['cvd_rising']:direction = 'BUY'
    else:                      direction = 'SELL'

    lp = closest_level['price']; mfl = abs(price - lp) / lp * 100

    cond = {
        'at_daily_level':   'D_' in lt, 'at_weekly_level': 'W_' in lt, 'at_monthly_level': 'M_' in lt,
        'near_level_0.1pct': dist_pct < 0.1, 'near_level_0.2pct': dist_pct < 0.2,
        'cvd_bull_div': cvd_div['bull'], 'cvd_bear_div': cvd_div['bear'],
        'cvd_leading': cvd_div['bull'] and cvd_div['cvd_rising'],
        'cvd_lagging': not cvd_div['bull'] and not cvd_div['bear'],
        'delta_positive': of_data.get('delta_positive',False),
        'delta_negative': of_data.get('delta_negative',False),
        'delta_spike': of_data.get('delta_spike',False),
        'absorption': of_data.get('absorption',False),
        'stacked_imbalance': of_data.get('bull_stack' if direction=='BUY' else 'bear_stack',False),
        'bid_imbalance': ob_analysis.get('bid_imbalance',False),
        'ask_imbalance': ob_analysis.get('ask_imbalance',False),
        'fib_0618': fib_hit.get('0.618',False), 'fib_0705': fib_hit.get('0.705',False),
        'fib_0786': fib_hit.get('0.786',False), 'fib_0500': fib_hit.get('0.500',False),
        'rsi_oversold_30': rsi_v < 30, 'rsi_oversold_40': rsi_v < 40,
        'rsi_overbought_70': rsi_v > 70, 'rsi_overbought_60': rsi_v > 60,
        'rsi_divergence': False,
        'vol_spike_2x': of_data.get('vol_ratio',0) > 2.0,
        'vol_spike_1.5x': of_data.get('vol_ratio',0) > 1.5,
        'vol_dry_up': of_data.get('vol_ratio',0) < 0.5,
        'vol_climax': of_data.get('vol_ratio',0) > 3.0,
        'ob_bullish': any(o['type']=='bull' and kl[-1]['l']>=o['bot'] and kl[-1]['l']<=o['top'] for o in obs),
        'ob_bearish': any(o['type']=='bear' and kl[-1]['h']<=o['top'] and kl[-1]['h']>=o['bot'] for o in obs),
        'ob_sweep_bull': any(o['type']=='bull' and o['swept'] for o in obs[:3]),
        'ob_sweep_bear': any(o['type']=='bear' and o['swept'] for o in obs[:3]),
        'entry_at_level': mfl < 0.15, 'entry_after_move': mfl > 0.8, 'move_pct': mfl,
    }
    if len(rsi_a) >= 10 and rsi_a[-1] and rsi_a[-5]:
        if direction=='BUY'  and closes[-1]<closes[-5] and rsi_a[-1]>rsi_a[-5]: cond['rsi_divergence']=True
        if direction=='SELL' and closes[-1]>closes[-5] and rsi_a[-1]<rsi_a[-5]: cond['rsi_divergence']=True

    score, active_feats, reasons = compute_score(cond, direction, ml_db)
    min_score = ml_db.get('min_score', 8.0)
    log.info(f"  {symbol}/{tf}: {direction} score={score:.1f}/{min_score} rsi={rsi_v:.0f} mfl={mfl:.2f}%")
    if score < min_score: log.info(f"  {symbol}/{tf}: skip (score too low)"); return None

    is_buy = direction == 'BUY'
    sl_p = lp - atr_v*0.5 if is_buy else lp + atr_v*0.5
    if is_buy  and (price-sl_p)/price > 0.010: sl_p = price - price*0.010
    if not is_buy and (sl_p-price)/price > 0.010: sl_p = price + price*0.010
    risk = abs(price - sl_p)
    if risk <= 0: return None

    tp1 = price + risk*1.5 if is_buy else price - risk*1.5
    tp2 = price + risk*2.5 if is_buy else price - risk*2.5
    tp3 = price + risk*4.0 if is_buy else price - risk*4.0
    if levels:
        if is_buy:
            above = [lv['price'] for lv in levels if lv['price'] > price]
            if above:
                nxt = min(above)
                if nxt > tp1: tp2 = nxt
        else:
            below = [lv['price'] for lv in levels if lv['price'] < price]
            if below:
                nxt = max(below)
                if nxt < tp1: tp2 = nxt

    return {
        'sym': symbol, 'tf': tf, 'dir': direction,
        'score': score, 'conf': min(98, round(score/12*100)),
        'price': price, 'entry': price,
        'sl': round(sl_p,4), 'tp1': round(tp1,4), 'tp2': round(tp2,4), 'tp3': round(tp3,4),
        'rr': round(abs(tp2-price)/risk,1), 'risk_pct': round(risk/price*100,3),
        'level_hit': closest_level, 'level_dist': round(dist_pct,4),
        'move_from_lv': round(mfl,3), 'rsi': round(rsi_v,1), 'atr': round(atr_v,4),
        'vol_ratio': round(of_data.get('vol_ratio',0),2),
        'cvd_div': cvd_div, 'fib_hit': fib_hit, 'ob_data': ob_analysis,
        'active_feats': active_feats, 'reasons': reasons,
        'conditions': {k: v for k,v in cond.items() if isinstance(v,bool)},
        'time': datetime.now(timezone.utc).isoformat(),
        'is_early': mfl < 0.2,
    }

# ═══════════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════════
def send_tg(msg):
    if not TG_TOKEN or not TG_CHAT: return False
    try:
        r = requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                          json={'chat_id':TG_CHAT,'text':msg,'parse_mode':'HTML',
                                'disable_web_page_preview':True}, timeout=10)
        if r.ok: return True
        r2 = requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                           json={'chat_id':TG_CHAT,'text':msg[:4000]}, timeout=10)
        return r2.ok
    except Exception as e: log.error(f"TG: {e}"); return False

def fp(p):
    if not p: return '—'
    if p >= 10000: return f'${p:,.2f}'
    if p >= 100:   return f'${p:.2f}'
    if p >= 1:     return f'${p:.4f}'
    return f'${p:.6f}'

def build_signal_msg(sig):
    ib = sig['dir']=='BUY'; lv=sig['level_hit']; cvd=sig['cvd_div']; fib=sig['fib_hit']; ob=sig['ob_data']
    timing = '🟢 EARLY ENTRY' if sig['is_early'] else f"⚠️ {sig['move_from_lv']:.1f}% from level"
    cvd_line = (f"📉📈 CVD Bull Div: {cvd['bull_desc']}" if cvd['bull'] else
                f"📈📉 CVD Bear Div: {cvd['bear_desc']}" if cvd['bear'] else '')
    fib_line = f"📐 Fib: {', '.join(fib.keys())} levels" if fib else ''
    ob_line  = (f"📖 OB: {'Bid' if ib else 'Ask'} dom {ob.get('bid_ratio' if ib else 'ask_ratio',0):.0%}"
                if ob else '')
    rs = '\n'.join(f"  {'✅' if '+' in r else '⚠️'} {r}" for r in sig['reasons'][:6])
    return '\n'.join(filter(None,[
        f"{'🟢' if ib else '🔴'} <b>{'BUY' if ib else 'SELL'} SCALP — {sig['sym']}</b>  [{sig['tf']}m]",
        f"", f"⏱ {timing}",
        f"📍 Level: <code>{lv['type']}</code> @ {fp(lv['price'])} (dist: {sig['level_dist']:.2f}%)",
        cvd_line, fib_line, ob_line,
        f"", f"📖 <b>Why this trade:</b>", rs,
        f"", f"💰 <b>Trade Levels</b>",
        f"  Entry:  <code>{fp(sig['entry'])}</code>",
        f"  SL:     <code>{fp(sig['sl'])}</code>  (-{sig['risk_pct']}%)",
        f"  TP1:    <code>{fp(sig['tp1'])}</code>  (1:1.5 — close 50%)",
        f"  TP2:    <code>{fp(sig['tp2'])}</code>  (1:{sig['rr']} — close 30%)",
        f"  TP3:    <code>{fp(sig['tp3'])}</code>  (runner 20%)",
        f"", f"📊 Score: <b>{sig['score']}/12</b>  Conf: {sig['conf']}%  RSI: {sig['rsi']}",
        f"  Vol: {sig['vol_ratio']:.1f}x avg  ATR: {fp(sig['atr'])}",
        f"", f"⚡ <i>Intraday scalp — manage at TP1. Not financial advice.</i>",
        f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC  |  🤖 <b>ML Scalp Bot v1</b>",
    ]))

def build_tp_msg(trade, tp_num, exit_price):
    ib = trade['dir']=='BUY'
    pnl = ((exit_price-trade['entry'])/trade['entry']*100) if ib else ((trade['entry']-exit_price)/trade['entry']*100)
    action = {1:"Close 50%, move SL to entry",2:"Close 30%, let runner go",3:"Full exit"}.get(tp_num,'')
    return (f"🎯 <b>TP{tp_num} HIT — {trade['sym']} +{pnl:.2f}%</b>\n\n"
            f"{'BUY' if ib else 'SELL'} {trade['tf']}m  Entry {fp(trade['entry'])} → {fp(exit_price)}\n\n"
            f"💡 {action}\n⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC  |  🤖 ML Scalp Bot")

def build_sl_msg(trade, exit_price):
    ib = trade['dir']=='BUY'
    pnl = ((exit_price-trade['entry'])/trade['entry']*100) if ib else ((trade['entry']-exit_price)/trade['entry']*100)
    feats = trade.get('active_feats',[])
    warn = []
    if not trade.get('is_early'): warn.append("Entry was late (after move)")
    if 'cvd_bull_div' not in feats and 'cvd_bear_div' not in feats: warn.append("No CVD divergence confirmed")
    ws = '\n'.join(f"  ⚠️ {w}" for w in warn) if warn else "  ℹ️ Valid setup — macro moved against"
    return (f"❌ <b>SL HIT — {trade['sym']} {pnl:.2f}%</b>\n\n"
            f"{'BUY' if ib else 'SELL'} {trade['tf']}m  Entry {fp(trade['entry'])} → SL {fp(exit_price)}\n\n"
            f"🔍 <b>Analysis:</b>\n{ws}\n\n"
            f"🧠 <i>ML adjusting weights...</i>\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC  |  🤖 ML Scalp Bot")

# ═══════════════════════════════════════════════════════════════════
# ML LEARNING ENGINE
# ═══════════════════════════════════════════════════════════════════
def ml_update(trade, result, exit_price, ml_db):
    lr=0.10; feats=trade.get('active_feats',[]); w=ml_db['weights']
    is_win=result=='win'; is_loss=result=='loss'; changes=[]

    for feat in feats:
        if feat not in w: continue
        old = w[feat]
        new = min(4.0,old+lr*0.5) if is_win else (max(0.1,old-lr*0.5) if is_loss else min(4.0,old+lr*0.1))
        w[feat] = round(new,3)
        if abs(new-old) > 0.02: changes.append(f"{'↑' if new>old else '↓'} {feat}: {old:.2f}→{new:.2f}")

    if not trade.get('is_early',True) and is_loss:
        for feat in ['entry_after_move','move_pct_1.0']:
            old=w.get(feat,0.3); w[feat]=max(0.01,old-lr)
            changes.append(f"↓↓ TIMING {feat}: {old:.2f}→{w[feat]:.2f}")

    if is_win and ('cvd_bull_div' in feats or 'cvd_bear_div' in feats):
        for feat in ['cvd_bull_div','cvd_bear_div']:
            if feat in feats:
                old=w.get(feat,2.0); w[feat]=min(4.0,old+lr)
                changes.append(f"↑↑ CVD {feat}: {old:.2f}→{w[feat]:.2f}")

    if trade.get('is_early') and is_win:
        old=w.get('entry_at_level',2.0); w['entry_at_level']=min(4.0,old+lr*0.5)
        changes.append(f"↑ entry_at_level: {old:.2f}→{w['entry_at_level']:.2f}")

    recent = ml_db.get('learning_log',[])[-20:]
    if len(recent) >= 10:
        wr = sum(1 for l in recent if l.get('result')=='win') / len(recent)
        if wr > 0.60 and ml_db['min_score'] > 7.0:  ml_db['min_score'] = max(7.0,  ml_db['min_score']-0.1)
        elif wr < 0.35 and ml_db['min_score'] < 11.0: ml_db['min_score'] = min(11.0, ml_db['min_score']+0.2)

    ml_db['total_trades'] += 1; ml_db['weights'] = w
    if is_win: ml_db['wins'] += 1
    elif is_loss: ml_db['losses'] += 1
    else: ml_db['be'] += 1

    sym = trade['sym']
    if sym not in ml_db['by_pair']: ml_db['by_pair'][sym]={'w':0,'l':0,'be':0,'pnl':0.0}
    pd = ml_db['by_pair'][sym]
    pd['w' if is_win else ('l' if is_loss else 'be')] += 1
    pnl_val = ((exit_price-trade['entry'])/trade['entry']*100) * (1 if trade['dir']=='BUY' else -1)
    pd['pnl'] = round(pd['pnl']+pnl_val,3); ml_db['total_pnl'] = round(ml_db.get('total_pnl',0)+pnl_val,3)

    tf = trade.get('tf','5')
    if tf not in ml_db['by_tf']: ml_db['by_tf'][tf]={'w':0,'l':0,'pnl':0.0}
    ml_db['by_tf'][tf]['w' if is_win else 'l'] += 1
    ml_db['by_tf'][tf]['pnl'] = round(ml_db['by_tf'][tf]['pnl']+pnl_val,3)

    if changes:
        ml_db['learning_log'].append({
            'time': datetime.now(timezone.utc).isoformat(), 'sym': sym,
            'result': result, 'pnl': round(pnl_val,3),
            'changes': changes[:8], 'is_early': trade.get('is_early',False)
        })
        if len(ml_db['learning_log']) > 500: ml_db['learning_log'] = ml_db['learning_log'][-500:]
        log.info(f"ML updated {sym} {result}: {len(changes)} changes")

    save_ml(ml_db); return changes

# ═══════════════════════════════════════════════════════════════════
# BACKTEST ENGINE
# ═══════════════════════════════════════════════════════════════════
def run_backtest(symbol, ml_db):
    log.info(f"Backtesting {symbol}..."); results = {}
    for tf in TIMEFRAMES:
        kl = bybit_klines(symbol, tf, limit=500)
        if not kl or len(kl) < 150: continue
        trades=[]; in_trade=False; trade_dir=None; entry_p=sl_p=tp1_p=tp2_p=0
        levels = fetch_key_levels(symbol)

        for idx in range(100, len(kl)-1):
            sub = kl[:idx+1]; sc=[k['c'] for k in sub]; sv=[k['v'] for k in sub]
            price=sub[-1]['c']
            atr_v=calc_atr(sub)[-1]; va_v=calc_vol_avg(sv)[-1]; rsi_v=calc_rsi(sc)[-1]
            cvd_a=calc_cvd(sub)
            if not all([atr_v,va_v,rsi_v]): continue

            if in_trade:
                nk=kl[idx+1]
                if trade_dir=='BUY':
                    if nk['l']<=sl_p:
                        trades.append({'result':'loss','pnl':round((sl_p-entry_p)/entry_p*100,3)}); in_trade=False
                    elif nk['h']>=tp2_p:
                        trades.append({'result':'win','pnl':round((tp2_p-entry_p)/entry_p*100,3)}); in_trade=False
                    elif nk['h']>=tp1_p:
                        trades.append({'result':'win','pnl':round((tp1_p-entry_p)/entry_p*100,3)}); in_trade=False
                else:
                    if nk['h']>=sl_p:
                        trades.append({'result':'loss','pnl':round((entry_p-sl_p)/entry_p*100,3)}); in_trade=False
                    elif nk['l']<=tp2_p:
                        trades.append({'result':'win','pnl':round((entry_p-tp2_p)/entry_p*100,3)}); in_trade=False
                continue

            near = price_near_level(price, levels, atr_v)
            if not near or near[1] > 0.25: continue
            cl2, dp2 = near
            cvd_div = detect_cvd_divergence(sub, cvd_a, lookback=20)
            of = detect_order_flow(sub, cvd_a, calc_vol_avg(sv))
            vr = sub[-1]['v']/va_v if va_v else 0

            bts=0; dr='BUY' if cvd_div['bull'] else 'SELL'
            if cvd_div['bull'] or cvd_div['bear']: bts+=3
            if of.get('absorption'): bts+=2
            if of.get('delta_spike'): bts+=1.5
            if vr>1.5: bts+=1
            if (dr=='BUY' and rsi_v<40) or (dr=='SELL' and rsi_v>60): bts+=1
            if dp2<0.15: bts+=2
            if bts<6: continue

            is_buy=dr=='BUY'; entry_p=price
            sl_p=cl2['price']-atr_v*0.5 if is_buy else cl2['price']+atr_v*0.5
            risk=abs(entry_p-sl_p); tp1_p=entry_p+risk*1.5 if is_buy else entry_p-risk*1.5
            tp2_p=entry_p+risk*2.5 if is_buy else entry_p-risk*2.5
            trade_dir=dr; in_trade=True
            if len(trades)>=50: break

        if not trades: results[tf]={'trades':0,'wr':0,'pnl':0,'score':0}; continue
        wins=sum(1 for t in trades if t['result']=='win')
        losses=sum(1 for t in trades if t['result']=='loss')
        total=len(trades); wr=wins/total if total else 0
        pnl=sum(t['pnl'] for t in trades)
        win_pnl=abs(sum(t['pnl'] for t in trades if t['result']=='win'))
        los_pnl=abs(sum(t['pnl'] for t in trades if t['result']=='loss'))
        pf=(wins*win_pnl)/max(0.01,losses*los_pnl)
        score=wr*40+min(pnl,20)+min(pf,3)*5
        results[tf]={'trades':total,'wins':wins,'losses':losses,'wr':round(wr*100,1),
                     'pnl':round(pnl,2),'pf':round(pf,2),'score':round(score,2)}
        log.info(f"  BT {symbol}/{tf}: {total}tr WR:{wr*100:.0f}% PnL:{pnl:.1f}% PF:{pf:.2f}")

    if not results: return '5', {}
    best_tf = max(results, key=lambda x: results[x].get('score',0))
    log.info(f"  Best TF {symbol}: {best_tf}m"); return best_tf, results

def run_all_backtests():
    log.info("BACKTEST: Starting all pairs...")
    ml_db=load_ml(); bt=load_backtest()
    send_tg("🔬 <b>ML Scalp Bot — Running Backtest</b>\nTesting all timeframes (~5 mins)...")
    for sym in PAIRS:
        try:
            best_tf, res = run_backtest(sym, ml_db)
            state['best_tf'][sym] = best_tf
            bt[sym] = {'best_tf':best_tf,'results':res,'time':datetime.now(timezone.utc).isoformat()}
            save_backtest(bt); time.sleep(1)
        except Exception as e:
            log.error(f"BT {sym}: {e}"); state['best_tf'][sym]='5'
    state['bt_done']=True
    with open(BT_DONE_FILE,'w') as f:
        json.dump({'done':True,'time':datetime.now(timezone.utc).isoformat()},f)
    lines = ["🔬 <b>Backtest Complete</b>\n"]
    for sym,res in bt.items():
        best=res.get('best_tf','5'); r=res.get('results',{}).get(best,{})
        lines.append(f"  <b>{sym}</b>: {best}m — WR:{r.get('wr',0):.0f}% "
                     f"PnL:{r.get('pnl',0):+.1f}% ({r.get('trades',0)} trades)")
    lines.append("\n🟢 <b>Bot now live scanning!</b>")
    send_tg('\n'.join(lines)); log.info("BACKTEST: Complete")

# ═══════════════════════════════════════════════════════════════════
# PRICE MONITOR
# ═══════════════════════════════════════════════════════════════════
def monitor_open_trades():
    for sym in list(state['open_trades'].keys()):
        trade=state['open_trades'][sym]; ticker=bybit_ticker(sym)
        if not ticker: continue
        price=ticker['price']; ib=trade['dir']=='BUY'

        if not trade.get('tp1_hit'):
            if (ib and price>=trade['tp1']) or (not ib and price<=trade['tp1']):
                trade['tp1_hit']=True; send_tg(build_tp_msg(trade,1,price))
                log.info(f"  TP1 hit: {sym}")

        if not trade.get('tp2_hit'):
            if (ib and price>=trade['tp2']) or (not ib and price<=trade['tp2']):
                trade['tp2_hit']=True; send_tg(build_tp_msg(trade,2,price))
                _close_trade(sym,trade,'win',price); continue

        if trade.get('tp2_hit') and not trade.get('tp3_hit'):
            if (ib and price>=trade['tp3']) or (not ib and price<=trade['tp3']):
                trade['tp3_hit']=True; send_tg(build_tp_msg(trade,3,price))
                _close_trade(sym,trade,'win',price); continue

        if (ib and price<=trade['sl']) or (not ib and price>=trade['sl']):
            send_tg(build_sl_msg(trade,price)); _close_trade(sym,trade,'loss',price)

def _close_trade(sym, trade, result, exit_price):
    ml_db=load_ml(); changes=ml_update(trade,result,exit_price,ml_db); save_ml(ml_db)
    j=load_journal(); ib=trade['dir']=='BUY'
    pnl=((exit_price-trade['entry'])/trade['entry']*100) if ib else ((trade['entry']-exit_price)/trade['entry']*100)
    trade.update({'result':result,'exit_price':exit_price,
                  'exit_time':datetime.now(timezone.utc).isoformat(),'pnl':round(pnl,3)})
    j['closed'].append(trade)
    if sym in j['open']: del j['open'][sym]
    save_journal(j); del state['open_trades'][sym]
    log.info(f"Trade closed: {sym} {result} {pnl:+.2f}% | {len(changes)} ML changes")

# ═══════════════════════════════════════════════════════════════════
# MAIN SCAN LOOP
# ═══════════════════════════════════════════════════════════════════
last_fired   = {}
levels_cache = {}

def run_scan():
    state['scans'] += 1; ml_db=load_ml()
    log.info(f"Scan #{state['scans']} | min_score={ml_db['min_score']:.1f} | open={len(state['open_trades'])}"
             + (" | ⚠️ GEO-BLOCKED" if state['geo_blocked'] else ""))
    try: monitor_open_trades()
    except Exception as e: log.error(f"Monitor: {e}")

    for sym in PAIRS:
        elapsed = time.time() - last_fired.get(sym, 0)
        if elapsed < COOLDOWN_MIN * 60:
            log.info(f"  {sym}: cooldown {int((COOLDOWN_MIN*60-elapsed)/60)}m"); continue
        if sym in state['open_trades']:
            log.info(f"  {sym}: in open trade"); continue
        if state['geo_blocked'] and not PROXY_URL:
            log.warning(f"  {sym}: skipping — geo-blocked. Set PROXY_URL to fix."); continue

        tf = state['best_tf'].get(sym, '5')
        try:
            sig = analyze_pair(sym, tf, ml_db, levels_cache)
            if not sig: continue
            ok = send_tg(build_signal_msg(sig))
            if ok:
                last_fired[sym] = time.time(); state['signals_sent'] += 1
                state['open_trades'][sym] = {**sig,'tp1_hit':False,'tp2_hit':False,'tp3_hit':False}
                j=load_journal(); j['signals'].append(sig); j['open'][sym]=sig['time']; save_journal(j)
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
            r = requests.get(f'https://api.telegram.org/bot{TG_TOKEN}/getUpdates',
                             params={'offset':last_upd[0]+1,'timeout':10}, timeout=15)
            if r.ok:
                for upd in r.json().get('result',[]):
                    last_upd[0]=upd['update_id']
                    txt=upd.get('message',{}).get('text','').strip().lower()

                    if txt=='/stats':
                        ml=load_ml(); tot=ml['total_trades']; wr=ml['wins']/tot*100 if tot else 0
                        geo = '❌ YES — set PROXY_URL env var' if state['geo_blocked'] else '✅ No'
                        send_tg(f"📊 <b>ML Scalp Bot Stats</b>\n\n"
                                f"Total trades: {tot}\n✅ Wins: {ml['wins']} ({wr:.1f}%)\n"
                                f"❌ Losses: {ml['losses']}\n➡️ BE: {ml['be']}\n"
                                f"💰 Total P&L: {ml['total_pnl']:+.2f}%\n🎯 Min score: {ml['min_score']:.1f}\n\n"
                                f"Open: {len(state['open_trades'])} | Signals: {state['signals_sent']} | Scans: {state['scans']}\n"
                                f"Geo-blocked: {geo}")

                    elif txt=='/open':
                        if not state['open_trades']: send_tg("✅ No open trades")
                        else:
                            lines=["🔓 <b>Open Trades:</b>"]
                            for sym,t in state['open_trades'].items():
                                lines.append(f"  {sym} {t['dir']} {t['tf']}m @ {fp(t['entry'])} SL:{fp(t['sl'])} TP2:{fp(t['tp2'])}")
                            send_tg('\n'.join(lines))

                    elif txt=='/ml':
                        ml=load_ml()
                        top_w=sorted(ml['weights'].items(),key=lambda x:-x[1])[:10]
                        lines=["🧠 <b>Top ML Weights:</b>"]
                        for feat,val in top_w: lines.append(f"  {feat}: {val:.2f}")
                        if ml['learning_log']:
                            last=ml['learning_log'][-1]
                            lines.append(f"\n<b>Last update ({last['time'][:10]}):</b>")
                            for ch in last.get('changes',[])[:4]: lines.append(f"  {ch}")
                        send_tg('\n'.join(lines))

                    elif txt=='/backtest':
                        bt=load_backtest()
                        if not bt: send_tg("No backtest data. Run /rebacktest")
                        else:
                            lines=["🔬 <b>Backtest Results:</b>"]
                            for sym,res in bt.items():
                                best=res.get('best_tf','5'); r2=res.get('results',{}).get(best,{})
                                lines.append(f"  {sym}: {best}m WR:{r2.get('wr',0):.0f}% PnL:{r2.get('pnl',0):+.1f}%")
                            send_tg('\n'.join(lines))

                    elif txt=='/rebacktest':
                        send_tg("🔄 Re-running backtest...")
                        threading.Thread(target=run_all_backtests, daemon=True).start()

                    elif txt=='/pairs':
                        lines=[f"📡 <b>Scanning {len(PAIRS)} pairs:</b>"]
                        for sym in PAIRS: lines.append(f"  {sym} → {state['best_tf'].get(sym,'5')}m")
                        send_tg('\n'.join(lines))

                    elif txt=='/proxy':
                        status = f"✅ {PROXY_URL.split('@')[-1]}" if PROXY_URL else "❌ Not configured"
                        geo    = "❌ BLOCKED" if state['geo_blocked'] else "✅ Reachable"
                        send_tg(f"🌐 <b>Connection Status</b>\n\n"
                                f"Proxy: {status}\nBybit API: {geo}\n\n"
                                f"<b>To fix geo-block:</b>\n"
                                f"Set env var: <code>PROXY_URL=socks5h://user:pass@host:1080</code>\n"
                                f"Then redeploy.")

                    elif txt=='/help':
                        send_tg("🤖 <b>ML Scalp Bot Commands</b>\n\n"
                                "/stats      — performance stats\n"
                                "/open       — open trades\n"
                                "/ml         — ML weights\n"
                                "/backtest   — backtest results\n"
                                "/rebacktest — re-run backtest\n"
                                "/pairs      — pairs + best TF\n"
                                "/proxy      — connection/geo-block status\n"
                                "/help       — this menu")
        except Exception as e: log.warning(f"TG cmd: {e}")
        time.sleep(30)

# ═══════════════════════════════════════════════════════════════════
# HEALTH SERVER
# ═══════════════════════════════════════════════════════════════════
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        ml=load_ml(); tot=ml['total_trades']; wr=ml['wins']/tot*100 if tot else 0
        geo_warn = "\n⚠️  GEO-BLOCKED — set PROXY_URL env var!\n" if state['geo_blocked'] else ""
        body = (
            f"ML Scalp Bot v1\n{'='*44}\n"
            f"Started:   {state['started']}\n"
            f"Scans:     {state['scans']}\n"
            f"Signals:   {state['signals_sent']}\n"
            f"Open:      {len(state['open_trades'])}\n"
            f"BT done:   {state['bt_done']}\n"
            f"{geo_warn}\n"
            f"Trades:    {tot} | WR: {wr:.1f}%\n"
            f"PnL:       {ml['total_pnl']:+.2f}%\n"
            f"Min score: {ml['min_score']:.1f}\n\n"
            f"Proxy:     {PROXY_URL.split('@')[-1] if PROXY_URL else 'None'}\n"
            f"Bybit:     {BYBIT_BASE}\n"
            f"Pairs:     {', '.join(PAIRS)}\n"
            f"Best TF:   {state['best_tf']}\n"
            f"Time:      {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        ).encode()
        self.send_response(200)
        self.send_header('Content-Type','text/plain')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self,*a): pass

# ═══════════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════════
def main():
    if not TG_TOKEN or not TG_CHAT:
        log.error("Need TG_TOKEN + TG_CHAT env vars"); raise SystemExit(1)

    log.info("="*60)
    log.info("ML SCALP BOT v1 — Bybit Multi-Pair")
    log.info(f"Bybit base: {BYBIT_BASE}")
    log.info(f"Proxy:      {PROXY_URL.split('@')[-1] if PROXY_URL else 'None (set PROXY_URL if geo-blocked)'}")
    log.info(f"Pairs:      {PAIRS}")
    log.info(f"Data dir:   {DATA_DIR}")
    log.info("="*60)

    ml = load_ml()
    log.info(f"ML: {ml['total_trades']} prev trades | W:{ml['wins']} L:{ml['losses']}")

    bt = load_backtest()
    if bt:
        for sym,res in bt.items(): state['best_tf'][sym]=res.get('best_tf','5')
        log.info(f"Restored backtest for {len(bt)} pairs")

    bt_done = Path(BT_DONE_FILE).exists()
    if bt_done: state['bt_done']=True; log.info("Backtest previously completed")

    threading.Thread(target=lambda: HTTPServer(('',PORT),Health).serve_forever(), daemon=True).start()
    log.info(f"Health server :{PORT}")
    threading.Thread(target=tg_commands, daemon=True).start()

    proxy_note = f"🌐 Proxy: {PROXY_URL.split('@')[-1]}" if PROXY_URL else "⚠️ No proxy — if geo-blocked, set PROXY_URL"
    send_tg(
        f"🤖 <b>ML Scalp Bot v1 Started</b>\n\n"
        f"📊 Trades:{ml['total_trades']} W:{ml['wins']} L:{ml['losses']}\n"
        f"PnL: {ml['total_pnl']:+.2f}% | Min score: {ml['min_score']:.1f}\n\n"
        f"📡 Pairs: {', '.join(PAIRS)}\n"
        f"🔬 Backtest: {'✅ Loaded' if bt_done else '⏳ Running...'}\n"
        f"{proxy_note}\n\n"
        f"1️⃣ D/W/M key levels  2️⃣ CVD divergence\n"
        f"3️⃣ Order flow + delta  4️⃣ Fib+OB+RSI+Vol  5️⃣ ML self-learning\n\n"
        f"/help — commands | /proxy — connection status"
    )

    if not bt_done:
        threading.Thread(target=run_all_backtests, daemon=True).start()
    else:
        log.info("Skipping backtest — already done")

    log.info("Starting main scan loop...")
    while True:
        try: run_scan()
        except Exception as e: log.error(f"Scan loop: {e}")
        log.info(f"Next scan in {SCAN_EVERY_MIN}m...")
        time.sleep(SCAN_EVERY_MIN * 60)

if __name__ == '__main__':
    main()
