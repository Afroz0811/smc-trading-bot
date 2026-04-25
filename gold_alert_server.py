#!/usr/bin/env python3
"""
<<<<<<< HEAD
SMC Scalp Engine — 15m Candle Scalp Bot
========================================
Separate from main SMC bot. Runs on 15m candles.
Setups: PD/PW/PM levels + Order Flow + Sweep+OB on 15m
Self-learning: tracks which level/session/OF combos win
Paper mode: 30 trades before going live
Deploy on Railway as separate service.
"""
import os, sys, json, time, logging, threading, requests, math
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── CONFIG ────────────────────────────────────────────────────────────────────
TG_TOKEN       = os.environ.get('TG_TOKEN', '')
TG_CHAT        = os.environ.get('TG_CHAT', '')
PORT           = int(os.environ.get('PORT', 8080))
SCAN_EVERY     = int(os.environ.get('SCAN_EVERY', 1))        # minutes
COOLDOWN_M     = int(os.environ.get('COOLDOWN_M', 90))       # min between same-coin signals
MIN_SCORE      = float(os.environ.get('MIN_SCORE', 4.5))     # confluence score threshold
MAX_SL_PCT     = float(os.environ.get('MAX_SL_PCT', 0.006))  # max 0.6% SL
TP1_MULT       = float(os.environ.get('TP1_MULT', 1.5))
TP2_MULT       = float(os.environ.get('TP2_MULT', 2.0))
TP3_MULT       = float(os.environ.get('TP3_MULT', 2.5))
PAPER_MODE     = os.environ.get('PAPER_MODE', 'true').lower() == 'true'
PAPER_TARGET   = int(os.environ.get('PAPER_TARGET', 30))
DAILY_CAP      = int(os.environ.get('DAILY_CAP', 3))         # max signals per coin per day

# Data files
LEARN_FILE  = os.environ.get('LEARN_FILE',  '/data/scalp_learning.json')
JOURNAL_FILE= os.environ.get('JOURNAL_FILE', '/data/scalp_journal.json')

# Data sources
KR = 'https://api.kraken.com/0/public'
CG = 'https://api.coingecko.com/api/v3'

PAIRS = [
    {'sym':'BTC',  'kr':'XXBTZUSD', 'cg':'bitcoin'},
    {'sym':'ETH',  'kr':'XETHZUSD', 'cg':'ethereum'},
    {'sym':'SOL',  'kr':'SOLUSD',   'cg':'solana'},
    {'sym':'BNB',  'kr':'BNBUSD',   'cg':'binancecoin'},
    {'sym':'XRP',  'kr':'XXRPZUSD', 'cg':'ripple'},
    {'sym':'ADA',  'kr':'ADAUSD',   'cg':'cardano'},
    {'sym':'AVAX', 'kr':'AVAXUSD',  'cg':'avalanche-2'},
    {'sym':'LINK', 'kr':'LINKUSD',  'cg':'chainlink'},
    {'sym':'DOGE', 'kr':'XDGUSD',   'cg':'dogecoin'},
    {'sym':'DOT',  'kr':'DOTUSD',   'cg':'polkadot'},
]
=======
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
>>>>>>> e882b248c427eeed0e0c7219161b885306f59ede

# ═══ LOGGING ═════════════════════════════════════════════════════
logging.basicConfig(level=logging.INFO,
<<<<<<< HEAD
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger('scalp')

# ══════════════════════════════════════════════════════════════════════════════
# SELF-LEARNING ENGINE
# ══════════════════════════════════════════════════════════════════════════════
DEFAULT_WEIGHTS = {
    'level_weights': {
        'PDH': 1.0, 'PDL': 1.0, 'PDVAH': 1.2, 'PDVAL': 1.2, 'PDPOC': 0.9,
        'PWH': 1.1, 'PWL': 1.1, 'PWVAH': 1.3, 'PWVAL': 1.3, 'PWPOC': 1.0,
        'PMH': 1.2, 'PML': 1.2, 'PMVAH': 1.4, 'PMVAL': 1.4, 'PMPOC': 1.1,
    },
    'session_weights': {
        'London': 1.2, 'NY': 1.1, 'Asian': 0.7, 'Off': 0.8,
    },
    'of_threshold': 0.58,   # order flow dominance threshold (ML adjusts)
    'min_score':    4.5,     # ML adjusts this based on WR
    'setup_weights': {
        'SWEEP_OB':  1.0,
        'OF_LEVEL':  1.0,
        'EMA_PULL':  0.9,
        'VWAP_REV':  0.9,
    },
    'rsi_zones': {
        'oversold':   1.2,   # RSI < 35 for BUY
        'neutral':    1.0,
        'overbought': 1.2,   # RSI > 65 for SELL
    },
}

def load_db():
    try:
        if Path(LEARN_FILE).exists():
            with open(LEARN_FILE) as f:
                db = json.load(f)
            if 'weights' not in db:
                db['weights'] = DEFAULT_WEIGHTS.copy()
            return db
    except: pass
    return {
        'weights': DEFAULT_WEIGHTS.copy(),
        'signals': [],
        'stats': {
            'total': 0, 'wins': 0, 'losses': 0, 'be': 0,
            'by_level': {}, 'by_session': {}, 'by_setup': {},
            'paper_count': 0,
        },
        'insights': [],
        'version': 1,
    }
=======
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
>>>>>>> e882b248c427eeed0e0c7219161b885306f59ede

def save_ml(db):
    try:
<<<<<<< HEAD
        Path(LEARN_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(LEARN_FILE, 'w') as f:
            json.dump(db, f, indent=2)
    except Exception as e:
        log.debug(f"Save DB: {e}")

def log_signal(sig, pair, session):
    db = load_db()
    record = {
        'id':       f"{pair['sym']}_{int(time.time())}",
        'sym':      pair['sym'],
        'dir':      sig['dir'],
        'setup':    sig['setup'],
        'level':    sig.get('level_name', ''),
        'score':    sig['score'],
        'entry':    sig['price'],
        'sl':       sig['sl'],
        'tp1':      sig['tp1'],
        'tp2':      sig['tp2'],
        'session':  session,
        'rsi_val':  sig.get('rsi_val', 50),
        'of_pct':   sig.get('of_pct', 0.5),
        'tags':     sig.get('tags', []),
        'time':     datetime.now(timezone.utc).isoformat(),
        'result':   None, 'pnl': 0,
        'paper':    PAPER_MODE,
        # Advanced ML fields
        'sl_pct':   round(abs(sig['price']-sig['sl'])/sig['price']*100, 3),
        'exit_reason': None,
        'bars_held': 0,
        'max_adverse': 0.0,
        'max_favourable': 0.0,
        'score_tier': 'A+' if sig['score']>=6 else ('A' if sig['score']>=5 else 'B'),
        'rsi_zone': ('oversold' if sig.get('rsi_val',50)<35
                     else 'overbought' if sig.get('rsi_val',50)>65 else 'neutral'),
        'of_dominant': sig.get('of_dominant', 'neutral'),
    }
    db['signals'].append(record)
    db['stats']['total'] += 1
    if PAPER_MODE:
        db['stats']['paper_count'] = db['stats'].get('paper_count', 0) + 1
    save_db(db)
    return record['id']

def close_signal(signal_id, result, exit_price, exit_reason='tp2'):
    db = load_db()
    for sig in db['signals']:
        if sig['id'] == signal_id:
            entry = sig['entry']
            ib    = sig['dir'] == 'BUY'
            pnl   = ((exit_price-entry)/entry*100) if ib else ((entry-exit_price)/entry*100)
            sig['result']      = result
            sig['pnl']         = round(pnl, 3)
            sig['exit_reason'] = exit_reason
            # Update stats
            db['stats'][result if result in ('wins','losses','be') else 'losses'] = \
                db['stats'].get(result, 0) + 1
            by_level = db['stats'].setdefault('by_level', {})
            lv = sig.get('level', 'unknown')
            if lv not in by_level: by_level[lv] = {'w':0,'l':0,'be':0,'pnl':0}
            by_level[lv]['w' if result=='wins' else 'l' if result=='losses' else 'be'] += 1
            by_level[lv]['pnl'] += pnl
            by_sess = db['stats'].setdefault('by_session', {})
            ss = sig.get('session','?')
            if ss not in by_sess: by_sess[ss] = {'w':0,'l':0,'be':0}
            by_sess[ss]['w' if result=='wins' else 'l' if result=='losses' else 'be'] += 1
            # Trigger learning every 5 trades
            done = [s for s in db['signals'] if s.get('result')]
            if len(done) % 5 == 0 and len(done) >= 5:
                _learn(db)
            break
    save_db(db)

def _learn(db):
    """Update weights based on completed trades"""
    done = [s for s in db['signals'] if s.get('result')]
    if len(done) < 5: return
    lr = 0.12; changes = []

    def wr_for(fn):
        sub = [t for t in done if fn(t)]
        if len(sub) < 3: return None, 0
        w = sum(1 for t in sub if t['result'] == 'wins')
        return w/len(sub), len(sub)

    # Level weight learning
    for level in list(db['weights']['level_weights'].keys()):
        wr, n = wr_for(lambda t, lv=level: t.get('level') == lv)
        if wr is None: continue
        target = 0.4 + wr * 1.2
        old = db['weights']['level_weights'][level]
        new = round(max(0.3, min(2.0, old + lr*(target-old))), 3)
        if abs(new-old) > 0.02:
            db['weights']['level_weights'][level] = new
            changes.append(f"{level}: {old:.2f}→{new:.2f} (WR:{wr:.0%})")

    # Session weight learning
    for sess in ['London','NY','Asian','Off']:
        wr, n = wr_for(lambda t, s=sess: t.get('session') == s)
        if wr is None: continue
        target = 0.4 + wr * 1.2
        old = db['weights']['session_weights'].get(sess, 1.0)
        new = round(max(0.3, min(2.0, old + lr*(target-old))), 3)
        if abs(new-old) > 0.02:
            db['weights']['session_weights'][sess] = new
            changes.append(f"sess_{sess}: {old:.2f}→{new:.2f} (WR:{wr:.0%})")

    # OF threshold learning: if high OF WR > low OF WR, raise threshold
    wr_hof, _ = wr_for(lambda t: t.get('of_pct', 0) > 0.65)
    wr_lof, _ = wr_for(lambda t: 0.55 < t.get('of_pct', 0) <= 0.65)
    if wr_hof and wr_lof and wr_hof > wr_lof + 0.10:
        old = db['weights']['of_threshold']
        new = round(min(0.70, old + 0.01), 3)
        if new != old:
            db['weights']['of_threshold'] = new
            changes.append(f"OF threshold: {old:.2f}→{new:.2f} (high OF WR better)")

    # MAE/MFE ratio
    timed = [t for t in done if t.get('max_adverse') is not None and t.get('max_favourable') is not None]
    if len(timed) >= 5:
        avg_mae = sum(t['max_adverse']    for t in timed) / len(timed)
        avg_mfe = sum(t['max_favourable'] for t in timed) / len(timed)
        ratio   = avg_mfe / max(avg_mae, 0.01)
        db['stats']['mae_avg']      = round(avg_mae, 3)
        db['stats']['mfe_avg']      = round(avg_mfe, 3)
        db['stats']['mfe_mae_ratio'] = round(ratio, 2)

    # Score tier win rates
    for tier in ['A+','A','B']:
        wr_t, n_t = wr_for(lambda t, _tier=tier: t.get('score_tier') == _tier)
        if wr_t is not None and n_t >= 3:
            db['stats'].setdefault('score_tier_wr', {})[tier] = round(wr_t, 3)

    if changes:
        db['insights'].append({
            'time': datetime.now(timezone.utc).isoformat(),
            'trades': len(done), 'changes': changes
        })
        log.info(f"Scalp ML updated ({len(done)} trades): {len(changes)} weight changes")

def performance_report():
    db = load_db(); done = [s for s in db['signals'] if s.get('result')]
    total = len(done); stats = db['stats']
    if total == 0: return "No completed trades yet."
    wins = sum(1 for t in done if t['result']=='wins')
    losses = sum(1 for t in done if t['result']=='losses')
    be = sum(1 for t in done if t['result']=='be')
    wr = wins/total*100 if total else 0
    pnls = [t['pnl'] for t in done]
    avg_win  = sum(p for p in pnls if p>0)/max(1,wins)
    avg_loss = sum(abs(p) for p in pnls if p<0)/max(1,losses)
    pf = (wins*avg_win)/(losses*avg_loss) if losses and avg_loss else 0
    w = db['weights']
    lines = [
        "<b>Scalp ML Performance Report</b>",
        f"Based on {total} real trades | Paper: {stats.get('paper_count',0)}",
        "",
        f"W:{wins}  L:{losses}  BE:{be}  WR:{wr:.1f}%  PF:{pf:.2f}",
        f"AvgW: +{avg_win:.3f}%  AvgL: -{avg_loss:.3f}%",
        "",
        "<b>Level performance:</b>",
    ]
    for lv, lvs in sorted(stats.get('by_level',{}).items()):
        t = lvs['w']+lvs['l']+lvs.get('be',0)
        if t: lines.append(f"  {lv}: WR {lvs['w']/t*100:.0f}% (n={t})")
    lines += ["", "<b>Session performance:</b>"]
    for ss, sv in sorted(stats.get('by_session',{}).items()):
        t = sv['w']+sv['l']+sv.get('be',0)
        if t: lines.append(f"  {ss}: WR {sv['w']/t*100:.0f}% (n={t})")
    if stats.get('mfe_mae_ratio'):
        lines += ["", f"MFE/MAE ratio: {stats['mfe_mae_ratio']:.1f}x "
                  + ("(good timing)" if stats['mfe_mae_ratio']>1.5 else "(entries too late!)")]
    if db['insights']:
        last = db['insights'][-1]
        lines += ["", f"<b>Last ML update ({last['time'][:10]}):</b>"]
        for ch in last['changes'][:4]: lines.append(f"  {ch}")
    return '\n'.join(lines)

def load_journal():
    try:
        if Path(JOURNAL_FILE).exists():
            with open(JOURNAL_FILE) as f: return json.load(f)
    except: pass
    return {'signals': []}

def journal_log(sig, pair):
    try:
        j = load_journal()
        j['signals'].append({
            'sym': pair['sym'], 'dir': sig['dir'], 'setup': sig['setup'],
            'entry': sig['price'], 'sl': sig['sl'],
            'tp1': sig['tp1'], 'tp2': sig['tp2'],
            'score': sig['score'], 'tags': sig.get('tags',[]),
            'time': datetime.now(timezone.utc).isoformat(), 'status': 'open',
        })
        Path(JOURNAL_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(JOURNAL_FILE,'w') as f: json.dump(j,f,indent=2)
    except Exception as e: log.debug(f"Journal: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# DATA FETCHER — 15m candles
# ══════════════════════════════════════════════════════════════════════════════
def fetch_15m(pair, limit=300):
    """Fetch 15m candles: Kraken primary, CoinGecko fallback"""
    # Kraken
    try:
        r = requests.get(f'{KR}/OHLC',
            params={'pair': pair['kr'], 'interval': 15},
            timeout=12, headers={'User-Agent':'Mozilla/5.0'})
        d = r.json()
        if not d.get('error'):
            key = next((k for k in d['result'] if k != 'last'), None)
            raw = d['result'].get(key, [])
            if len(raw) > 30:
                kl = [{'t':int(k[0]),'o':float(k[1]),'h':float(k[2]),
                        'l':float(k[3]),'c':float(k[4]),'v':float(k[6]),
                        # Estimate buy/sell vol from candle direction
                        'bv': float(k[6])*(0.65 if float(k[4])>float(k[1]) else 0.35),
                        'sv': float(k[6])*(0.35 if float(k[4])>float(k[1]) else 0.65),
                       } for k in raw[-limit:]]
                for i,k in enumerate(kl):
                    k['delta'] = k['bv'] - k['sv']
                    k['h_']    = datetime.fromtimestamp(k['t'], timezone.utc).hour
                    h = k['h_']
                    k['sess'] = ('London' if 7<=h<13 else 'NY' if 13<=h<19
                                 else 'Asian' if h<7 else 'Off')
                return kl
    except Exception as e:
        log.debug(f"Kraken 15m {pair['sym']}: {e}")
    # CoinGecko fallback (returns 1h data, we use it as approximate)
    try:
        r = requests.get(f'{CG}/coins/{pair["cg"]}/ohlc',
            params={'vs_currency':'usd','days':3}, timeout=12)
        raw = r.json()
        if isinstance(raw,list) and len(raw)>10:
            kl = [{'t':int(k[0]/1000),'o':float(k[1]),'h':float(k[2]),
                    'l':float(k[3]),'c':float(k[4]),'v':50.0,
                    'bv':25.0,'sv':25.0,'delta':0.0} for k in raw[-limit:]]
            for k in kl:
                h = datetime.fromtimestamp(k['t'], timezone.utc).hour
                k['h_']  = h
                k['sess'] = ('London' if 7<=h<13 else 'NY' if 13<=h<19
                              else 'Asian' if h<7 else 'Off')
            return kl
    except: pass
    return None

def fetch_price(pair):
    try:
        r = requests.get(f'{CG}/simple/price',
            params={'ids':pair['cg'],'vs_currencies':'usd'}, timeout=8)
        return float(r.json()[pair['cg']]['usd'])
    except: return None

# ══════════════════════════════════════════════════════════════════════════════
# INDICATORS
# ══════════════════════════════════════════════════════════════════════════════
def ema(c, p):
    if len(c)<p: return [None]*len(c)
    k=2/(p+1); r=[None]*(p-1); s=sum(c[:p])/p; r.append(s); pv=s
    for i in range(p,len(c)): pv=c[i]*k+pv*(1-k); r.append(pv)
    return r

def rsi(c, p=14):
    if len(c)<p+1: return [None]*len(c)
    r=[None]*p; g=l=0.0
    for i in range(1,p+1):
        d=c[i]-c[i-1]
        if d>0: g+=d
        else: l+=abs(d)
    ag,al=g/p,l/p; r.append(100 if al==0 else 100-100/(1+ag/al))
    for i in range(p+1,len(c)):
        d=c[i]-c[i-1]; ag=(ag*(p-1)+(d if d>0 else 0))/p
        al=(al*(p-1)+(abs(d) if d<0 else 0))/p
        r.append(100 if al==0 else 100-100/(1+ag/al))
    return r

def macd_hist(c):
    e12=ema(c,12); e26=ema(c,26)
    ln=[e12[i]-e26[i] if e12[i] and e26[i] else None for i in range(len(c))]
    vl=[v for v in ln if v is not None]
    if len(vl)<9: return [None]*len(c)
    sr=ema(vl,9); sg=[None]*len(c); si=0
    for i in range(len(c)):
        if ln[i] is not None: sg[i]=sr[si] if si<len(sr) else None; si+=1
    return [ln[i]-sg[i] if ln[i] is not None and sg[i] is not None else None
            for i in range(len(c))]

def calc_atr(kl, p=14):
    tr=[None]+[max(kl[i]['h']-kl[i]['l'],
               abs(kl[i]['h']-kl[i-1]['c']),
               abs(kl[i]['l']-kl[i-1]['c'])) for i in range(1,len(kl))]
    if len(tr)<p+1: return [None]*len(kl)
    r=[None]*p; s=sum(tr[1:p+1])/p; r.append(s); pv=s
    for i in range(p+1,len(tr)): pv=(pv*(p-1)+tr[i])/p; r.append(pv)
    return r

def vol_avg(v, p=20):
    r=[None]*p
    for i in range(p,len(v)): r.append(sum(v[i-p:i])/p)
    return r

def swings(kl, lb=4):
    sh=[]; sl=[]
    for i in range(lb,len(kl)-lb):
        if all(kl[i]['h']>=kl[j]['h'] for j in range(i-lb,i+lb+1) if j!=i):
            sh.append((i,kl[i]['h']))
        if all(kl[i]['l']<=kl[j]['l'] for j in range(i-lb,i+lb+1) if j!=i):
            sl.append((i,kl[i]['l']))
    return sh,sl

# ══════════════════════════════════════════════════════════════════════════════
# LEVEL CALCULATORS — PD / PW / PM
# ══════════════════════════════════════════════════════════════════════════════
def _vp(candles):
    """Volume profile: VAH, VAL, POC from candles"""
    if not candles: return None
    lo=min(k['l'] for k in candles); hi=max(k['h'] for k in candles)
    rng=hi-lo
    if rng<=0: return None
    nb=24; bsz=rng/nb; bkts=[0.0]*nb
    for k in candles:
        b=min(int(((k['h']+k['l']+k['c'])/3-lo)/bsz),nb-1)
        bkts[b]+=k['v']
    tv=sum(bkts)
    if tv<=0: return None
    poc_b=bkts.index(max(bkts)); poc=lo+(poc_b+0.5)*bsz
    tgt=tv*0.70; cov=bkts[poc_b]; lb_=poc_b; hb_=poc_b
    while cov<tgt:
        la=bkts[lb_-1] if lb_>0 else 0
        ha=bkts[hb_+1] if hb_<nb-1 else 0
        if la>=ha and lb_>0: lb_-=1; cov+=la
        elif hb_<nb-1: hb_+=1; cov+=ha
        else: break
    return {'high':hi,'low':lo,'poc':poc,
            'vah':lo+(hb_+1)*bsz,'val':lo+lb_*bsz}

def get_levels(kl, i):
    """
    Calculate PD/PW/PM levels.
    15m candles: 1 day = 96 bars, 1 week = 672, 1 month = 2880
    """
    levels = {}
    # Previous Day (96 bars = 24h, use the day before current)
    if i >= 192:
        pd = kl[i-192:i-96]
        vp = _vp(pd)
        if vp:
            levels.update({
                'PDH':   vp['high'],  'PDL':  vp['low'],
                'PDVAH': vp['vah'],   'PDVAL':vp['val'],
                'PDPOC': vp['poc'],
            })
    # Previous Week (use 2 weeks ago window)
    if i >= 1344:
        pw = kl[i-1344:i-672]
        vp = _vp(pw)
        if vp:
            levels.update({
                'PWH':   vp['high'],  'PWL':  vp['low'],
                'PWVAH': vp['vah'],   'PWVAL':vp['val'],
                'PWPOC': vp['poc'],
            })
    # Previous Month
    if i >= 5760:
        pm = kl[i-5760:i-2880]
        vp = _vp(pm)
        if vp:
            levels.update({
                'PMH':   vp['high'],  'PML':  vp['low'],
                'PMVAH': vp['vah'],   'PMVAL':vp['val'],
                'PMPOC': vp['poc'],
            })
    return levels

def nearby_levels(price, levels, atr, tol=0.7):
    """Return levels price is close to, with type (resistance/support)"""
    result = []
    for name, lvl in levels.items():
        if not lvl or abs(price-lvl)/atr > tol: continue
        if any(x in name for x in ['VAH','H']): ltype = 'resistance'
        elif any(x in name for x in ['VAL','L']): ltype = 'support'
        else: ltype = 'neutral'
        result.append((name, lvl, ltype))
    # Sort: PM > PW > PD (higher timeframe = more weight)
    priority = {'PM':3,'PW':2,'PD':1}
    result.sort(key=lambda x: priority.get(x[0][:2],0), reverse=True)
    return result

# ══════════════════════════════════════════════════════════════════════════════
# ORDER FLOW
# ══════════════════════════════════════════════════════════════════════════════
def order_flow(kl, i, lookback=6):
    """
    Aggregate buy vs sell volume over last N bars.
    Returns (bias, buy_pct, sell_pct)
    """
    if i < lookback: return 'neutral', 0.5, 0.5
    w = kl[i-lookback:i+1]
    tbv = sum(k['bv'] for k in w)
    tsv = sum(k['sv'] for k in w)
    tv  = tbv + tsv
    if tv <= 0: return 'neutral', 0.5, 0.5
    buy_p = tbv/tv; sell_p = tsv/tv
    if sell_p > 0.58: return 'sell', buy_p, sell_p
    if buy_p  > 0.58: return 'buy',  buy_p, sell_p
    return 'neutral', buy_p, sell_p

# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL ENGINE — Confluence Scorer
# ══════════════════════════════════════════════════════════════════════════════
def compute_scalp(kl, pair):
    """
    15m scalp signal engine.
    Scores confluence across:
    1. PD/PW/PM level proximity
    2. Order flow dominance
    3. Micro sweep + OB on 15m
    4. RSI zone
    5. VWAP alignment
    6. EMA stack
    7. Rejection wick
    Returns signal dict or None.
    """
    if len(kl) < 100: return None
    i = len(kl)-1

    # Guard: open trade lock applied in run_scan, not here
    closes = [k['c'] for k in kl]
    vols   = [k['v'] for k in kl]
    ri_a   = rsi(closes)
    e9_a   = ema(closes, 9)
    e20_a  = ema(closes, 20)
    e50_a  = ema(closes, 50)
    ht_a   = macd_hist(closes)
    at_a   = calc_atr(kl)
    va_a   = vol_avg(vols)

    if any(x is None for x in [ri_a[i],e9_a[i],e20_a[i],at_a[i],va_a[i]]):
        return None

    price = closes[i]; k = kl[i]
    at    = float(at_a[i]); va_v = float(va_a[i])
    ri_v  = float(ri_a[i]); e9 = float(e9_a[i])
    e20   = float(e20_a[i]); e50 = float(e50_a[i]) if e50_a[i] else e20
    ht    = ht_a[i]

    # Chop filter: ATR must be reasonable
    past_atr = [a for a in at_a[max(0,i-20):i] if a]
    if not past_atr or at < sum(past_atr)/len(past_atr)*0.30:
        return None  # market too quiet

    # ── Session ────────────────────────────────────────────────────────
    sess = kl[i]['sess']

    # ── Load ML weights ────────────────────────────────────────────────
    db = load_db()
    w  = db['weights']
    sess_mult = w['session_weights'].get(sess, 1.0)
    of_thresh  = w.get('of_threshold', 0.58)

    # ── Levels ─────────────────────────────────────────────────────────
    levels = get_levels(kl, i)
    near   = nearby_levels(price, levels, at, tol=0.7)

    # ── Order Flow ─────────────────────────────────────────────────────
    of_bias, buy_pct, sell_pct = order_flow(kl, i)
    of_pct = sell_pct if of_bias=='sell' else buy_pct if of_bias=='buy' else 0.5

    # ── Swings (for micro sweep detection) ─────────────────────────────
    sh_, sl_ = swings(kl[:i+1], 4)

    # ── VWAP (20 bar = 5 hour on 15m) ──────────────────────────────────
    w20 = kl[max(0,i-19):i+1]; tv20 = sum(x['v'] for x in w20)
    vwap = sum(((x['h']+x['l']+x['c'])/3)*x['v'] for x in w20)/tv20 if tv20 else price

    # ══════════════════════════════════════════════════════════════════
    # TRY EACH SETUP — return first that scores above threshold
    # ══════════════════════════════════════════════════════════════════

    # ── SETUP 1: Sweep + OB on 15m ─────────────────────────────────────
    # Micro sweep of recent low (last 10 bars on 15m = 2.5 hours)
    def try_sweep():
        for li,lvl in [(ix,float(p)) for ix,p in sl_ if i-10<ix<i-1][-4:]:
            if not(k['l']<lvl<price): continue
            if lvl-k['l']<at*0.15: continue
            if k['v']<va_v*1.05: continue
            if ri_v>70: continue  # don't buy into overbought sweep
            ob=None
            for j in range(li-1,max(0,li-6),-1):
                if (kl[j]['c']<kl[j]['o'] and
                    (kl[min(j+2,len(kl)-1)]['c']-kl[j]['c'])/kl[j]['c']>0.002):
                    ob={'top':kl[j]['o'],'bot':kl[j]['l']}; break
            if not ob: continue
            if not(ob['bot']<=price<=ob['top']*1.005): continue
            sl_p=k['l']-at*0.08
            if (price-sl_p)/price>MAX_SL_PCT: continue
            return 'BUY','SWEEP_OB',sl_p,ob,k['l']
        for hi_,lvl in [(ix,float(p)) for ix,p in sh_ if i-10<ix<i-1][-4:]:
            if not(k['h']>lvl>price): continue
            if k['h']-lvl<at*0.15: continue
            if k['v']<va_v*1.05: continue
            if ri_v<30: continue
            ob=None
            for j in range(hi_-1,max(0,hi_-6),-1):
                if (kl[j]['c']>kl[j]['o'] and
                    (kl[min(j+2,len(kl)-1)]['c']-kl[j]['c'])/kl[j]['c']<-0.002):
                    ob={'top':kl[j]['h'],'bot':kl[j]['c']}; break
            if not ob: continue
            if not(ob['bot']*0.995<=price<=ob['top']): continue
            sl_p=k['h']+at*0.08
            if (sl_p-price)/price>MAX_SL_PCT: continue
            return 'SELL','SWEEP_OB',sl_p,ob,k['h']
        return None,None,None,None,None

    # ── SETUP 2: EMA9 Pullback in trend ────────────────────────────────
    def try_ema_pull():
        if not all([e9_a[i],e20_a[i],ht]): return None,None,None
        if e9>e20>e50 and ht>0 and abs(price-e9)<at*0.5 and 38<ri_v<56:
            if closes[i]<closes[max(0,i-3)]:  # actually pulling back
                sl_p=e20-at*0.12
                if (price-sl_p)/price<MAX_SL_PCT:
                    return 'BUY','EMA_PULL',sl_p
        if e9<e20<e50 and ht<0 and abs(price-e9)<at*0.5 and 44<ri_v<62:
            if closes[i]>closes[max(0,i-3)]:
                sl_p=e20+at*0.12
                if (sl_p-price)/price<MAX_SL_PCT:
                    return 'SELL','EMA_PULL',sl_p
        return None,None,None

    # ── SETUP 3: VWAP Reversion ─────────────────────────────────────────
    def try_vwap():
        dev=(price-vwap)/vwap
        if dev<-0.005 and k['c']>k['o'] and ri_v<42:
            sl_p=k['l']-at*0.08
            if (price-sl_p)/price<MAX_SL_PCT:
                return 'BUY','VWAP_REV',sl_p
        if dev>0.005 and k['c']<k['o'] and ri_v>58:
            sl_p=k['h']+at*0.08
            if (sl_p-price)/price<MAX_SL_PCT:
                return 'SELL','VWAP_REV',sl_p
        return None,None,None

    # Try setups in priority order
    setup_sig = None
    direction = None; setup_name = None; sl_price = None
    ob_data = None; wick_val = None

    direction,setup_name,sl_price,ob_data,wick_val = try_sweep()
    if not direction:
        d2,sn2,sl2 = try_ema_pull()
        if d2: direction,setup_name,sl_price = d2,sn2,sl2
    if not direction:
        d3,sn3,sl3 = try_vwap()
        if d3: direction,setup_name,sl_price = d3,sn3,sl3

    if not direction: return None

    # ══════════════════════════════════════════════════════════════════
    # CONFLUENCE SCORING
    # ══════════════════════════════════════════════════════════════════
    is_buy = direction == 'BUY'
    score  = 0.0; tags = []; level_name = None

    # Setup base score
    su_w = w['setup_weights'].get(setup_name, 1.0)
    score += 2.0 * su_w; tags.append(setup_name)

    # 1. PD/PW/PM level proximity — main edge
    best_level = None; best_level_score = 0
    for lname, llvl, ltype in near:
        lv_w = w['level_weights'].get(lname, 1.0)
        # Direction must match level type
        if ltype=='resistance' and not is_buy:
            ls = 1.5 * lv_w; tags.append(f'{lname}✓'); best_level=lname; best_level_score=ls; break
        elif ltype=='support' and is_buy:
            ls = 1.5 * lv_w; tags.append(f'{lname}✓'); best_level=lname; best_level_score=ls; break
        elif ltype=='neutral':
            ls = 0.8 * lv_w; tags.append(f'{lname}✓'); best_level=lname; best_level_score=ls; break
    if best_level_score: score += best_level_score; level_name = best_level

    # 2. Order flow — biggest single edge (PF 2.25 from backtest)
    if of_bias=='sell' and not is_buy and sell_pct>=of_thresh:
        score += 1.5 + (sell_pct-of_thresh)*5; tags.append(f'OF_Sell{round(sell_pct*100):.0f}%')
    elif of_bias=='buy' and is_buy and buy_pct>=of_thresh:
        score += 1.5 + (buy_pct-of_thresh)*5; tags.append(f'OF_Buy{round(buy_pct*100):.0f}%')
    elif of_bias=='neutral':
        score -= 0.5  # neutral OF = weaker signal

    # 3. RSI zone
    rsi_zone_w = (w['rsi_zones']['oversold'] if ri_v<35 else
                  w['rsi_zones']['overbought'] if ri_v>65 else
                  w['rsi_zones']['neutral'])
    if is_buy and ri_v<40:  score += 0.8*rsi_zone_w; tags.append(f'RSI{round(ri_v)}')
    elif is_buy and ri_v<52: score += 0.4; tags.append(f'RSI{round(ri_v)}')
    elif not is_buy and ri_v>60: score += 0.8*rsi_zone_w; tags.append(f'RSI{round(ri_v)}')
    elif not is_buy and ri_v>48: score += 0.4; tags.append(f'RSI{round(ri_v)}')

    # 4. VWAP alignment
    if is_buy  and price > vwap: score += 0.4; tags.append('VWAP↑')
    elif not is_buy and price < vwap: score += 0.4; tags.append('VWAP↓')
    elif is_buy and price < vwap*0.997: score -= 0.4
    elif not is_buy and price > vwap*1.003: score -= 0.4

    # 5. EMA alignment
    if is_buy  and e9>e20>e50: score += 0.5; tags.append('EMA↑')
    elif not is_buy and e9<e20<e50: score += 0.5; tags.append('EMA↓')

    # 6. MACD confirmation
    if ht:
        if is_buy  and ht>0: score += 0.4; tags.append('MACD+')
        elif not is_buy and ht<0: score += 0.4; tags.append('MACD-')

    # 7. Wick rejection at current bar
    if is_buy  and (price-k['l'])>at*0.25 and k['c']>k['o']:
        score += 0.6; tags.append('WickRej✓')
    elif not is_buy and (k['h']-price)>at*0.25 and k['c']<k['o']:
        score += 0.6; tags.append('WickRej✓')

    # 8. Volume surge
    if k['v'] > va_v*1.5: score += 0.5; tags.append('Vol++')
    elif k['v'] > va_v*1.2: score += 0.2; tags.append('Vol✓')

    # Session multiplier
    score *= sess_mult
    score  = round(max(0, min(10, score)), 1)

    min_score_thresh = w.get('min_score', MIN_SCORE)
    if score < min_score_thresh: return None

    # ── Risk/Reward ────────────────────────────────────────────────────
    risk = abs(price - sl_price)
    if risk <= 0 or risk/price > MAX_SL_PCT:
        return None

    tp1 = price + risk*TP1_MULT if is_buy else price - risk*TP1_MULT
    tp2 = price + risk*TP2_MULT if is_buy else price - risk*TP2_MULT
    tp3 = price + risk*TP3_MULT if is_buy else price - risk*TP3_MULT

    conf = min(96, round(score*9 + min(TP2_MULT,3)*3))

    return {
        'dir':        direction,
        'setup':      setup_name,
        'score':      score,
        'conf':       conf,
        'price':      price,
        'entry':      price,
        'sl':         round(sl_price, 8),
        'tp1':        round(tp1, 8),
        'tp2':        round(tp2, 8),
        'tp3':        round(tp3, 8),
        'rr':         round(TP2_MULT, 1),
        'risk_pct':   round(risk/price*100, 3),
        'tags':       tags,
        'session':    sess,
        'rsi_val':    round(ri_v, 1),
        'of_bias':    of_bias,
        'of_pct':     round(of_pct, 3),
        'of_dominant': of_bias,
        'level_name': level_name,
        'vwap':       round(vwap, 8),
        'ob':         ob_data,
        'wick_sl':    wick_val,
        'near_levels': [(n,round(l,8),t) for n,l,t in near[:3]],
    }

# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════
def fp(p):
    if not p: return '—'
    if p>=10000: return f'${p:,.0f}'
    if p>=100:   return f'${p:.2f}'
    if p>=1:     return f'${p:.4f}'
    return f'${p:.6f}'

def esc(s):
    return str(s).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')

def send_tg(msg):
    if not TG_TOKEN or not TG_CHAT: return False
    try:
        r = requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id':TG_CHAT,'text':msg,'parse_mode':'HTML'},
            timeout=12)
        return r.ok
    except: return False

def build_signal_msg(sig, pair):
    ib   = sig['dir'] == 'BUY'
    e    = sig['entry']
    sl   = sig['sl']
    tp1  = sig['tp1']
    tp2  = sig['tp2']
    tp3  = sig['tp3']
    slp  = round(abs(e-sl)/e*100, 2)
    tp1p = round(abs(tp1-e)/e*100, 2)
    tp2p = round(abs(tp2-e)/e*100, 2)
    tp3p = round(abs(tp3-e)/e*100, 2)
    pm   = '📋 PAPER' if PAPER_MODE else '⚡ SCALP'
    pc   = state['stats'].get('paper_count', 0) if PAPER_MODE else ''
    paper_hdr = (f"\n📋 <b>PAPER #{pc}/{PAPER_TARGET} — observe only</b>\n" if PAPER_MODE else "")
    setup_icons={'SWEEP_OB':'⚡','EMA_PULL':'📊','VWAP_REV':'🎯'}
    ei = setup_icons.get(sig['setup'],'📡')
    # Level explanation
    lev_str = ''
    if sig.get('near_levels'):
        lev_str = '\n'.join(f"  {n}: {fp(l)} ({t})" for n,l,t in sig['near_levels'][:2])

    lines = filter(None,[
        f"{'🟢' if ib else '🔴'} <b>{'BUY' if ib else 'SELL'} — {pair['sym']}/USD [{pm}]</b>",
        f"{ei} <b>{sig['setup']}</b>  |  Score: {sig['score']}/10  |  Conf: {sig['conf']}%",
        paper_hdr,
        "",
        "💰 <b>Trade Levels</b>",
        f"  Entry:  <code>{fp(e)}</code>",
        f"  SL:     <code>{fp(sl)}</code>  <i>(-{slp}%)</i>",
        f"  TP1:    <code>{fp(tp1)}</code>  <i>(+{tp1p}% — 1:1.5 — close 70%)</i>",
        f"  TP2:    <code>{fp(tp2)}</code>  <i>(+{tp2p}% — 1:2.0 — close 25%)</i>",
        f"  TP3:    <code>{fp(tp3)}</code>  <i>(+{tp3p}% — runner 5%)</i>",
        "",
        f"📊 <b>Confluence</b>",
        f"  {esc(' · '.join(sig['tags']))}",
        f"  RSI: {sig['rsi_val']}  |  OF: {sig['of_bias']} ({round(sig['of_pct']*100):.0f}%)  |  Session: {sig['session']}",
        f"  VWAP: {fp(sig.get('vwap'))}",
        "",
        "<b>PD/PW/PM Levels nearby:</b>",
        lev_str if lev_str else "  (none in proximity)",
        sig.get('ob') and f"  OB: {fp(sig['ob']['bot'])} – {fp(sig['ob']['top'])}",
        "",
        f"⚡ <i>Scalp: TP1 fast, move SL to entry. Max hold: 5 hours.</i>",
        f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC  |  🔬 <b>SMC Scalp 15m</b>",
    ])
    return '\n'.join(l for l in lines if l is not None)

# ══════════════════════════════════════════════════════════════════════════════
# STATE & PRICE MONITORING
# ══════════════════════════════════════════════════════════════════════════════
state = {
    'started':     datetime.now(timezone.utc).isoformat(),
    'last_scan':   'Never',
    'scans_done':  0,
    'alerts_sent': 0,
    'open_trades': {},   # sym → trade dict
    'stats':       {'paper_count': 0, 'wins': 0, 'losses': 0, 'be': 0},
}
last_fired  = {}  # sym → {time}
daily_count = {}  # sym → {date: count}

def check_prices():
    for sym, trade in list(state['open_trades'].items()):
        try:
            pair  = next(p for p in PAIRS if p['sym']==sym)
            price = fetch_price(pair)
            if not price: continue
            ib    = trade['dir']=='BUY'; en=trade['entry']
            sl_p  = trade['sl']; tp1_p=trade['tp1']; tp2_p=trade['tp2']; tp3_p=trade['tp3']

            # Track MAE/MFE
            move    = (price-en)/en*100 if ib else (en-price)/en*100
            adverse = (en-price)/en*100 if ib else (price-en)/en*100
            trade['max_favourable'] = max(trade.get('max_favourable',0), move)
            trade['max_adverse']    = max(trade.get('max_adverse',0), adverse)
            trade['bars_held']      = trade.get('bars_held',0)+1

            # TP1
            if not trade.get('be_triggered'):
                if (ib and price>=tp1_p) or (not ib and price<=tp1_p):
                    trade['be_triggered']=True
                    pnl1=round(abs(tp1_p-en)/en*100,3)
                    send_tg(
                        f"🎯 <b>SCALP TP1 — {sym}/USD +{pnl1}%</b>\n\n"
                        f"Close 70% at <code>{fp(price)}</code>\n"
                        f"Move SL → entry: <code>{fp(en)}</code>\n"
                        f"Runner: TP2 <code>{fp(tp2_p)}</code>  TP3 <code>{fp(tp3_p)}</code>\n"
                        f"Trade now risk-free!  |  🔬 SMC Scalp 15m"
                    ); log.info(f"  TP1 {sym} +{pnl1}%")

            # TP2 — WIN
            if not trade.get('tp2_hit'):
                if (ib and price>=tp2_p) or (not ib and price<=tp2_p):
                    trade['tp2_hit']=True
                    pnl=round(abs(tp2_p-en)/en*100,3)
                    send_tg(
                        f"✅ <b>SCALP WIN — {sym}/USD +{pnl}%</b>\n\n"
                        f"Setup: {trade.get('setup','—')} | Score: {trade.get('score',0)}/10\n"
                        f"Entry {fp(en)} → {fp(price)}\n"
                        f"Held: {trade.get('bars_held',0)} bars (~{trade.get('bars_held',0)*15}min)\n\n"
                        f"Runner at TP3: <code>{fp(tp3_p)}</code>\n"
                        f"🤖 ML learning from this win...\n"
                        f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC  |  🔬 SMC Scalp 15m"
                    )
                    _close_trade(sym, trade, 'wins', price, 'tp2')
                    del state['open_trades'][sym]; continue

            # TP3 — Runner
            if trade.get('tp2_hit') and not trade.get('tp3_hit'):
                if (ib and price>=tp3_p) or (not ib and price<=tp3_p):
                    trade['tp3_hit']=True
                    pnl=round(abs(tp3_p-en)/en*100,3)
                    send_tg(f"🚀 <b>SCALP RUNNER — {sym}/USD +{pnl}%</b>\nFull exit.  🔬 SMC Scalp 15m")
                    continue

            # SL — LOSS
            if (ib and price<=sl_p) or (not ib and price>=sl_p):
                pnl=round(abs(sl_p-en)/en*100,3)
                why=_loss_reason(trade, price)
                send_tg(
                    f"❌ <b>SCALP LOSS — {sym}/USD -{pnl}%</b>\n\n"
                    f"Setup: {trade.get('setup','—')} | Score: {trade.get('score',0)}/10\n"
                    f"Entry {fp(en)} → SL {fp(price)}\n"
                    f"Held: {trade.get('bars_held',0)} bars\n\n"
                    f"🔍 <b>Why failed:</b>\n{why}\n\n"
                    f"🤖 ML adjusting weights...\n"
                    f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC  |  🔬 SMC Scalp 15m"
                )
                _close_trade(sym, trade, 'losses', price, 'sl')
                del state['open_trades'][sym]
        except Exception as e:
            log.debug(f"check_prices {sym}: {e}")

def _close_trade(sym, trade, result, price, reason):
    lid = trade.get('learn_id')
    if lid: close_signal(lid, result, price, reason)
    if result=='wins': state['stats']['wins']+=1
    elif result=='losses': state['stats']['losses']+=1
    log.info(f"  {'✅' if result=='wins' else '❌'} {sym} {result} {reason}")

def _loss_reason(trade, price):
    lines=[]; en=trade['entry']; ib=trade['dir']=='BUY'
    ri=trade.get('rsi_val',50); sess=trade.get('session','')
    mv=abs(price-en)/en*100
    if ib and trade.get('weekly_bias')=='bearish': lines.append("  ⚠️ BUY against weekly bearish")
    if not ib and trade.get('weekly_bias')=='bullish': lines.append("  ⚠️ SELL against weekly bullish")
    if mv<0.15: lines.append("  ⚠️ Stopped immediately — bad timing or news")
    if sess=='Asian': lines.append("  ⚠️ Asian session — low liquidity")
    if trade.get('of_bias')=='neutral': lines.append("  ⚠️ No OF confirmation — weak setup")
    mfe=trade.get('max_favourable',0); mae=trade.get('max_adverse',0)
    if mfe<mae*0.3: lines.append("  ⚠️ Price moved against immediately — entry too early")
    if not lines: lines.append("  Market structure changed unexpectedly")
    return '\n'.join(lines)

# ══════════════════════════════════════════════════════════════════════════════
# MAIN SCAN
# ══════════════════════════════════════════════════════════════════════════════
def run_scan():
    state['scans_done']+=1
    state['last_scan']=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    log.info(f"Scalp scan #{state['scans_done']} — {len(PAIRS)} pairs")
    today=datetime.now(timezone.utc).strftime('%Y-%m-%d')

    for pair in PAIRS:
        sym=pair['sym']
        try:
            # Hard lock: no new signal while trade is open
            if sym in state['open_trades']:
                log.debug(f"  {sym}: trade open — skip")
                continue

            # Daily cap
            dc = daily_count.get(sym,{})
            if dc.get('date')!=today: daily_count[sym]={'date':today,'count':0}
            if daily_count[sym]['count'] >= DAILY_CAP:
                log.debug(f"  {sym}: daily cap ({DAILY_CAP}) reached")
                continue

            # Cooldown
            lf=last_fired.get(sym,{})
            if lf and (time.time()-lf.get('time',0))/60 < COOLDOWN_M:
                continue

            # Fetch 15m candles
            kl=fetch_15m(pair, limit=350)
            if not kl or len(kl)<120:
                log.debug(f"  {sym}: insufficient data ({len(kl) if kl else 0} bars)")
                continue

            # Only scan during London or NY
            sess=kl[-1]['sess']
            if sess not in ('London','NY'):
                log.debug(f"  {sym}: {sess} session — skip")
                continue

            sig=compute_scalp(kl, pair)
            if not sig:
                log.debug(f"  {sym}: no setup")
                continue

            # Build and send
            msg=build_signal_msg(sig, pair)
            ok=send_tg(msg)
            if ok:
                last_fired[sym]={'time':time.time()}
                daily_count[sym]['count']+=1
                state['alerts_sent']+=1
                if PAPER_MODE:
                    state['stats']['paper_count']=state['stats'].get('paper_count',0)+1

                learn_id=log_signal(sig, pair, sess)
                journal_log(sig, pair)

                state['open_trades'][sym]={
                    **sig,
                    'sym':sym,'pair':pair,
                    'time':datetime.now(timezone.utc).isoformat(),
                    'be_triggered':False,'tp2_hit':False,'tp3_hit':False,
                    'session':sess,'session_name':sess,
                    'learn_id':learn_id,
                    'bars_held':0,'max_adverse':0.0,'max_favourable':0.0,
                }
                log.info(f"  ✓ {sym}: {sig['setup']} {sig['dir']} "
                         f"score={sig['score']} OF={sig['of_bias']} → TG sent")

                # Paper mode: auto-report at target
                if PAPER_MODE and state['stats'].get('paper_count',0)>=PAPER_TARGET:
                    send_tg(
                        f"📊 <b>Scalp Paper Mode Complete — {PAPER_TARGET} signals</b>\n\n"
                        + performance_report() +
                        f"\n\n💡 Set <b>PAPER_MODE=false</b> in Railway to go live.\n"
                        f"🔬 SMC Scalp 15m"
                    )
            else:
                log.error(f"  {sym}: TG send failed")

        except Exception as e:
            log.error(f"  {sym} scan error: {e}")
        time.sleep(0.4)  # rate limit between coins

# ══════════════════════════════════════════════════════════════════════════════
# HEALTH SERVER
# ══════════════════════════════════════════════════════════════════════════════
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        w=state['stats'].get('wins',0); l=state['stats'].get('losses',0)
        b=state['stats'].get('be',0); tot=w+l+b
        ot=', '.join(f"{s}: {v['dir']} {v.get('setup','?')} @{fp(v['entry'])}"
                     for s,v in state['open_trades'].items()) or 'none'
        body=(
            f"SMC Scalp Engine — 15m\n{'='*40}\n"
            f"Started:   {state['started']}\n"
            f"Last scan: {state['last_scan']}\n"
            f"Scans:     {state['scans_done']}\n"
            f"Alerts:    {state['alerts_sent']}\n"
            f"Mode:      {'PAPER' if PAPER_MODE else 'LIVE'}\n\n"
            f"W:{w} L:{l} BE:{b} WR:{round(w/tot*100) if tot else 0}%\n"
            f"Open: {ot}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        ).encode()
        self.send_response(200)
        self.send_header('Content-Type','text/plain')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self,*a): pass

# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM COMMANDS
# ══════════════════════════════════════════════════════════════════════════════
=======
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
>>>>>>> e882b248c427eeed0e0c7219161b885306f59ede
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

<<<<<<< HEAD
                    if txt in ('/stats','/report'):
                        send_tg(performance_report())
                    elif txt in ('/paper','/status'):
                        pc=state['stats'].get('paper_count',0)
                        send_tg(
                            f"📋 <b>Scalp Engine Status</b>\n\n"
                            f"Mode: {'📋 PAPER' if PAPER_MODE else '🟢 LIVE'}\n"
                            f"Paper signals: {pc}/{PAPER_TARGET}\n"
                            f"Remaining: {max(0,PAPER_TARGET-pc)}\n\n"
                            + (f"✅ Ready to go live!" if pc>=PAPER_TARGET else
                               f"⏳ Still collecting data...") +
                            f"\n\n{performance_report()}"
                        )
                    elif txt=='/open':
                        if state['open_trades']:
                            msg="🔓 <b>Open scalp trades:</b>\n"+'\n'.join(
                                f"  {s}: {v['dir']} {v.get('setup','?')} @{fp(v['entry'])}"
                                for s,v in state['open_trades'].items())
                        else: msg="✅ No open scalp trades"
                        send_tg(msg)
                    elif txt=='/levels':
                        send_tg("📐 Levels are calculated from 15m candle data.\nSend /open to see active trades.")
                    elif txt=='/help':
                        send_tg(
                            "🔬 <b>SMC Scalp 15m Commands</b>\n\n"
                            "/stats   — performance + ML report\n"
                            "/paper   — paper mode status\n"
                            "/open    — open scalp trades\n"
                            "/levels  — info about level calculation\n"
                            "/help    — this menu"
                        )
        except Exception as e: log.debug(f"TG cmd: {e}")
        time.sleep(30)

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
=======
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
>>>>>>> e882b248c427eeed0e0c7219161b885306f59ede
def main():
    if not TG_TOKEN or not TG_CHAT:
        log.error("Need TG_TOKEN + TG_CHAT"); raise SystemExit(1)

<<<<<<< HEAD
    log.info("="*55)
    log.info("SMC SCALP ENGINE — 15m CANDLES")
    log.info(f"Pairs: {len(PAIRS)} | SL≤{MAX_SL_PCT*100}% | TP1:1:{TP1_MULT} TP2:1:{TP2_MULT}")
    log.info(f"Scan: {SCAN_EVERY}min | Cooldown: {COOLDOWN_M}min | Daily cap: {DAILY_CAP}/coin")
    log.info(f"Mode: {'PAPER ('+str(PAPER_TARGET)+' signals before live)' if PAPER_MODE else 'LIVE'}")
    log.info("="*55)

    threading.Thread(
        target=lambda: HTTPServer(('',PORT),Health).serve_forever(),
        daemon=True).start()
    log.info(f"Health on :{PORT}")
=======
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
>>>>>>> e882b248c427eeed0e0c7219161b885306f59ede

    bt_done = Path(BT_DONE_FILE).exists()
    if bt_done: state['bt_done']=True; log.info("Backtest previously completed")

    threading.Thread(target=lambda: HTTPServer(('',PORT),Health).serve_forever(), daemon=True).start()
    log.info(f"Health server :{PORT}")
    threading.Thread(target=tg_commands, daemon=True).start()

    proxy_note = f"🌐 Proxy: {PROXY_URL.split('@')[-1]}" if PROXY_URL else "⚠️ No proxy — if geo-blocked, set PROXY_URL"
    send_tg(
<<<<<<< HEAD
        "🔬 <b>SMC Scalp Engine — 15m Started</b>\n\n"
        f"Pairs: {', '.join(p['sym'] for p in PAIRS)}\n"
        f"Timeframe: 15m candles\n"
        f"SL max: {MAX_SL_PCT*100}%  |  TP1: 1:{TP1_MULT}  TP2: 1:{TP2_MULT}\n"
        f"Sessions: London + NY only\n\n"
        f"<b>Setups:</b>\n"
        f"⚡ Sweep+OB on 15m (primary)\n"
        f"📊 EMA9 pullback in trend\n"
        f"🎯 VWAP reversion\n\n"
        f"<b>Filters:</b>\n"
        f"🏛️ PD/PW/PM levels (VAH, VAL, POC)\n"
        f"📦 Order flow dominant (≥58% one side)\n"
        f"🔢 RSI zone + VWAP alignment\n\n"
        + (f"📋 <b>PAPER MODE</b> — {PAPER_TARGET} signals before going live\n"
           f"Signals marked [PAPER] — just observe\n\n" if PAPER_MODE else
           "🟢 <b>LIVE MODE</b>\n\n") +
        "/stats /paper /open /help\n\n"
        "🔬 <b>SMC Scalp 15m — Self Learning</b>"
    )

    # Price monitor thread (every 60s)
    def monitor():
        while True:
            try: check_prices()
            except Exception as e: log.debug(f"Monitor: {e}")
            time.sleep(60)
    threading.Thread(target=monitor, daemon=True).start()

    # TG commands thread
    threading.Thread(target=tg_commands, daemon=True).start()
    log.info("✓ All threads started")

    # Main scan loop
    while True:
        try: run_scan()
        except Exception as e: log.error(f"Scan: {e}")
        log.info(f"Next scan in {SCAN_EVERY}m...")
        time.sleep(SCAN_EVERY * 60)

if __name__ == '__main__':
    main()
=======
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
>>>>>>> e882b248c427eeed0e0c7219161b885306f59ede
