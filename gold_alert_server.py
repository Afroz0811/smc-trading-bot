#!/usr/bin/env python3
"""
SMC Gold Engine v1 — XAU/USD 24/7 Alert Server
Separate from crypto server. Self-learning. Scalp mode.
"""
import os, sys, json, time, logging, threading, requests
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
import math
from http.server import HTTPServer, BaseHTTPRequestHandler

TG_TOKEN   = os.environ.get('TG_TOKEN','')
TG_CHAT    = os.environ.get('TG_CHAT','')
PORT       = int(os.environ.get('PORT', 8080))
SCAN_EVERY = int(os.environ.get('SCAN_EVERY', 1))
COOLDOWN_M = int(os.environ.get('COOLDOWN_M', 45))
GOLD_YF    = 'https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF'

# Crypto pairs for scalp signals
CRYPTO_PAIRS = [
    {'sym':'BTC',  'kr':'XXBTZUSD', 'cg':'bitcoin'},
    {'sym':'ETH',  'kr':'XETHZUSD', 'cg':'ethereum'},
    {'sym':'SOL',  'kr':'SOLUSD',   'cg':'solana'},
    {'sym':'BNB',  'kr':'BNBUSD',   'cg':'binancecoin'},
    {'sym':'ADA',  'kr':'ADAUSD',   'cg':'cardano'},
    {'sym':'LINK', 'kr':'LINKUSD',  'cg':'chainlink'},
    {'sym':'AVAX', 'kr':'AVAXUSD',  'cg':'avalanche-2'},
    {'sym':'XRP',  'kr':'XXRPZUSD', 'cg':'ripple'},
    {'sym':'DOGE', 'kr':'XDGUSD',   'cg':'dogecoin'},
    {'sym':'DOT',  'kr':'DOTUSD',   'cg':'polkadot'},
]
KR  = 'https://api.kraken.com/0/public'
CG  = 'https://api.coingecko.com/api/v3'

# Crypto scalp config
CRYPTO_SCALP_MIN_SCORE = float(os.environ.get('CRYPTO_SCALP_MIN_SCORE', '8.5'))
CRYPTO_SCALP_DAILY_CAP = int(os.environ.get('CRYPTO_SCALP_DAILY_CAP',   '3'))   # max per coin/day
CRYPTO_SCALP_TP1       = float(os.environ.get('CRYPTO_SCALP_TP1',       '1.5'))
CRYPTO_SCALP_TP2       = float(os.environ.get('CRYPTO_SCALP_TP2',       '2.0'))
CRYPTO_SCALP_MAX_SL    = float(os.environ.get('CRYPTO_SCALP_MAX_SL',    '0.008')) # 0.8%
GOLD_MIN_SCORE = float(os.environ.get('GOLD_MIN_SCORE', 6.0))
SCALP_MODE   = os.environ.get('SCALP_MODE','true').lower()=='true'
PAPER_MODE   = os.environ.get('PAPER_MODE','true').lower()=='true'  # true = observe only, don't track as real trades
PAPER_TARGET = int(os.environ.get('PAPER_TARGET','30'))  # signals needed before going live
# Scalp targets: quick in/out
TP1_MULT = 1.5 if SCALP_MODE else 2.0
TP2_MULT = 2.0 if SCALP_MODE else 2.8
TP3_MULT = 2.5 if SCALP_MODE else 3.5
MAX_SL_PCT = 0.005 if SCALP_MODE else 0.008  # 0.5% = ~$10 at $2000

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger('gold')

# ═══ SELF-LEARNING ENGINE ═══════════════════════════════════════
DEFAULT_WEIGHTS = {
    # Setup base scores
    'setup_scores': {
        'SWEEP_OB':        8.0,
        'HTF_CONFLUENCE':  8.0,
        'CHOCH':           8.0,
        'BOS':             7.0,
    },
    # Tag multipliers (how much each confluence adds)
    'tag_weights': {
        'Sweep↑':      1.0, 'Sweep↓':      1.0,
        'OB_Retest':   1.0, 'Vol✓':        0.8,
        'HTF✓':        0.8, 'Week✓':       0.6,
        'EMA↑':        0.5, 'EMA↓':        0.5,
        'MACD✓':       0.5, 'CHoCH↑':      1.0,
        'CHoCH↓':      1.0, 'BOS↑':        0.8,
        'BOS↓':        0.8, 'HH+HL':       0.6,
        'CleanStr':    0.5, 'RSI_Div✓':    1.5,
        'Fib✓':        0.5, 'VWAP✓':       0.3,
    },
    # Session multipliers
    'session_weights': {
        'London':    1.2,
        'New York':  1.2,
        'Asian':     0.8,
        'Weekend':   0.5,
    },
    # RSI zone effectiveness
    'rsi_zones': {
        '20-30': 1.3,  # deep oversold = strong buy
        '30-40': 1.1,
        '40-50': 1.0,
        '50-60': 0.9,
        '60-70': 1.1,
        '70-80': 1.3,  # deep overbought = strong sell
    },
    # Weekly bias multiplier
    'weekly_bias_mult': {
        'bullish': 1.2,
        'neutral': 0.9,
        'bearish': 0.7,  # against weekly = risky
    },
    # Minimum score to fire (adjusted based on session)
    'min_score_session': {
        'London':    6.0,
        'New York':  6.0,
        'Asian':     7.0,
        'Weekend':   8.0,
    }
}

# ── DATA SCHEMA ──────────────────────────────────────────────────────
def load_db():
    if Path(LEARN_FILE).exists():
        try:
            with open(LEARN_FILE) as f:
                return json.load(f)
        except:
            pass
    return {
        'version': 2,
        'created': datetime.now(timezone.utc).isoformat(),
        'weights': DEFAULT_WEIGHTS.copy(),
        'signals': [],        # every signal fired
        'outcomes': [],       # completed trades with result
        'stats': {
            'total_signals': 0,
            'total_trades':  0,
            'wins': 0, 'losses': 0, 'be': 0,
            'total_pnl': 0.0,
            'by_setup': {},
            'by_session': {},
            'by_rsi_zone': {},
            'by_tag': {},
            'by_weekly': {},
            'by_score_range': {},
        },
        'learning_log': [],   # what changed and why
        'last_learned': None,
    }

def save_db(db):
    try:
        with open(LEARN_FILE, 'w') as f:
            json.dump(db, f, indent=2)
    except Exception as e:
        print(f"DB save error: {e}")

# ── LOG A NEW SIGNAL ─────────────────────────────────────────────────
def log_signal(sig, pair, session):
    db = load_db()
    rsi_zone = get_rsi_zone(sig.get('rsi_val', 50))
    entry = {
        'id':           f"{pair['sym']}_{int(time.time())}",
        'sym':          pair['sym'],
        'setup':        sig['setup'],
        'dir':          sig['dir'],
        'score':        sig['score'],
        'raw_score':    sig.get('raw_score', sig['score']),
        'conf':         sig['conf'],
        'entry':        sig['price'],
        'sl':           sig['sl'],
        'tp1':          sig['tp1'],
        'tp2':          sig['tp'],
        'tp3':          sig['tp3'],
        'rr':           sig['rr'],
        'risk_pct':     sig['risk_pct'],
        'tags':         sig.get('tags', []),
        'session':      session,
        'weekly':       sig.get('weekly', 'neutral'),
        'daily':        sig.get('daily', 'neutral'),
        'rsi_val':      sig.get('rsi_val', 50),
        'rsi_zone':     rsi_zone,
        'time':         datetime.now(timezone.utc).isoformat(),
        'status':       'open',
        'exit_price':   None,
        'exit_time':    None,
        'pnl':          None,
        'result':       None,
        'bars_held':    None,
    }
    db['signals'].append(entry)
    db['stats']['total_signals'] += 1
    save_db(db)
    return entry['id']

# ── CLOSE A TRADE + LEARN ────────────────────────────────────────────
def close_trade(trade_id, result, exit_price, bars_held=0):
    """
    result: 'win' | 'loss' | 'be'
    This is the CORE learning moment — update stats and adjust weights
    """
    db = load_db()
    sig = next((s for s in db['signals'] if s['id'] == trade_id), None)
    if not sig:
        return None

    is_buy = sig['dir'] == 'BUY'
    if exit_price:
        pnl = ((exit_price - sig['entry']) / sig['entry'] * 100) if is_buy \
              else ((sig['entry'] - exit_price) / sig['entry'] * 100)
    else:
        pnl = 0.0

    sig['status']     = result
    sig['result']     = result
    sig['exit_price'] = exit_price
    sig['exit_time']  = datetime.now(timezone.utc).isoformat()
    sig['pnl']        = round(pnl, 3)
    sig['bars_held']  = bars_held

    # Update aggregate stats
    db['stats']['total_trades'] += 1
    db['stats']['total_pnl']    = round(db['stats']['total_pnl'] + pnl, 3)
    if result == 'win':   db['stats']['wins']   += 1
    elif result == 'loss': db['stats']['losses'] += 1
    else:                  db['stats']['be']     += 1

    # Update per-dimension stats
    _update_dimension(db, 'by_setup',       sig['setup'],    result, pnl)
    _update_dimension(db, 'by_session',     sig['session'],  result, pnl)
    _update_dimension(db, 'by_rsi_zone',    sig['rsi_zone'], result, pnl)
    _update_dimension(db, 'by_weekly',      sig['weekly'],   result, pnl)
    score_bucket = f"{int(sig['score'])}-{int(sig['score'])+1}"
    _update_dimension(db, 'by_score_range', score_bucket,    result, pnl)
    for tag in sig.get('tags', []):
        _update_dimension(db, 'by_tag', tag, result, pnl)

    db['outcomes'].append(sig)
    save_db(db)

    # Auto-learn after every 5 trades
    total = db['stats']['total_trades']
    if total >= 10 and total % 5 == 0:
        learn(db)

    return sig

def _update_dimension(db, dim, key, result, pnl):
    if key not in db['stats'][dim]:
        db['stats'][dim][key] = {'w':0,'l':0,'be':0,'total':0,'pnl':0.0}
    d = db['stats'][dim][key]
    d['total'] += 1
    d['pnl']   = round(d['pnl'] + pnl, 3)
    if result == 'win':   d['w'] += 1
    elif result == 'loss': d['l'] += 1
    else:                  d['be'] += 1

# ── LEARNING ENGINE ──────────────────────────────────────────────────
def learn(db):
    """
    Analyze completed trades and adjust weights.
    Uses Bayesian-style update: weight += learning_rate * (actual - expected)
    """
    outcomes = [s for s in db['signals'] if s['result']]
    if len(outcomes) < 10:
        return  # not enough data

    lr = 0.15  # learning rate — how fast to adjust
    changes = []

    # ── Learn setup scores ──────────────────────────────────────────
    for setup, stats in db['stats']['by_setup'].items():
        if stats['total'] < 5: continue
        wr = stats['w'] / stats['total']
        avg_pnl = stats['pnl'] / stats['total']
        # Expected WR at current score
        current_score = db['weights']['setup_scores'].get(setup, 7.0)
        # If WR > 55% and positive PnL → increase base score
        # If WR < 35% → decrease base score
        if wr > 0.55 and avg_pnl > 0:
            new_score = min(9.5, current_score + lr)
            if abs(new_score - current_score) > 0.05:
                db['weights']['setup_scores'][setup] = round(new_score, 2)
                changes.append(f"↑ {setup} score {current_score:.1f}→{new_score:.1f} (WR:{wr:.0%})")
        elif wr < 0.35 or avg_pnl < -1:
            new_score = max(5.0, current_score - lr)
            if abs(new_score - current_score) > 0.05:
                db['weights']['setup_scores'][setup] = round(new_score, 2)
                changes.append(f"↓ {setup} score {current_score:.1f}→{new_score:.1f} (WR:{wr:.0%})")

    # ── Learn tag effectiveness ──────────────────────────────────────
    for tag, stats in db['stats']['by_tag'].items():
        if stats['total'] < 5: continue
        wr = stats['w'] / stats['total']
        current_w = db['weights']['tag_weights'].get(tag, 0.5)
        if wr > 0.60:
            new_w = min(2.5, current_w + lr*0.5)
            if abs(new_w - current_w) > 0.05:
                db['weights']['tag_weights'][tag] = round(new_w, 2)
                changes.append(f"↑ tag '{tag}' weight {current_w:.2f}→{new_w:.2f} (WR:{wr:.0%})")
        elif wr < 0.30:
            new_w = max(0.1, current_w - lr*0.5)
            if abs(new_w - current_w) > 0.05:
                db['weights']['tag_weights'][tag] = round(new_w, 2)
                changes.append(f"↓ tag '{tag}' weight {current_w:.2f}→{new_w:.2f} (WR:{wr:.0%})")

    # ── Learn session effectiveness ──────────────────────────────────
    for sess, stats in db['stats']['by_session'].items():
        if stats['total'] < 5: continue
        wr = stats['w'] / stats['total']
        current_m = db['weights']['session_weights'].get(sess, 1.0)
        target_m = 0.6 + wr * 1.2  # scales from 0.6 to 1.8
        new_m = round(current_m + lr * (target_m - current_m), 2)
        new_m = max(0.3, min(1.5, new_m))
        if abs(new_m - current_m) > 0.05:
            db['weights']['session_weights'][sess] = new_m
            changes.append(f"{'↑' if new_m>current_m else '↓'} session '{sess}' mult {current_m:.2f}→{new_m:.2f} (WR:{wr:.0%})")

    # ── Learn RSI zone effectiveness ─────────────────────────────────
    for zone, stats in db['stats']['by_rsi_zone'].items():
        if stats['total'] < 5: continue
        wr = stats['w'] / stats['total']
        current_m = db['weights']['rsi_zones'].get(zone, 1.0)
        target_m = 0.5 + wr * 1.5
        new_m = round(current_m + lr * (target_m - current_m), 2)
        new_m = max(0.3, min(2.0, new_m))
        if abs(new_m - current_m) > 0.05:
            db['weights']['rsi_zones'][zone] = new_m
            changes.append(f"RSI zone '{zone}': mult {current_m:.2f}→{new_m:.2f} (WR:{wr:.0%})")

    if changes:
        log_entry = {
            'time':    datetime.now(timezone.utc).isoformat(),
            'trades':  len(outcomes),
            'changes': changes
        }
        db['learning_log'].append(log_entry)
        db['last_learned'] = log_entry['time']

    save_db(db)
    return changes

# ── COMPUTE SCORE USING LEARNED WEIGHTS ──────────────────────────────
def compute_learned_score(setup, tags, session, weekly, rsi_val, base_score):
    """
    Returns adjusted score using learned weights.
    Called instead of fixed score thresholds.
    """
    db = load_db()
    w = db['weights']

    # Start with learned setup base score
    score = w['setup_scores'].get(setup, base_score)

    # Add learned tag weights
    for tag in tags:
        tag_clean = tag.split('RSI')[0].strip()  # normalize RSI35, RSI42 etc
        score += w['tag_weights'].get(tag_clean, 0.3)

    # Apply session multiplier
    score *= w['session_weights'].get(session, 1.0)

    # Apply RSI zone multiplier
    zone = get_rsi_zone(rsi_val)
    score *= w['rsi_zones'].get(zone, 1.0)

    # Apply weekly bias multiplier
    score *= w['weekly_bias_mult'].get(weekly, 1.0)

    return round(min(10, score), 1)

def get_min_score(session):
    """Dynamic minimum score threshold based on session"""
    db = load_db()
    return db['weights']['min_score_session'].get(session, 6.5)

# ── HELPERS ──────────────────────────────────────────────────────────
def get_rsi_zone(rsi):
    if rsi < 30:   return '20-30'
    if rsi < 40:   return '30-40'
    if rsi < 50:   return '40-50'
    if rsi < 60:   return '50-60'
    if rsi < 70:   return '60-70'
    return '70-80'

def get_session():
    h = datetime.now(timezone.utc).hour
    d = datetime.now(timezone.utc).weekday()
    if d >= 5: return 'Weekend'
    if 7 <= h <= 12:  return 'London'
    if 13 <= h <= 18: return 'New York'
    return 'Asian'

# ── PERFORMANCE REPORT ───────────────────────────────────────────────
def performance_report():
    db = load_db()
    s = db['stats']
    total = s['total_trades']
    if total == 0:
        return "📊 No completed trades yet. Learning begins after first trade closes."

    wr = s['wins']/total*100 if total else 0
    pf_num = s['wins'] * (s['total_pnl']/max(s['wins'],1)) if s['wins'] else 0
    pf_den = s['losses'] * abs(s['total_pnl']/max(s['losses'],1)) if s['losses'] else 1
    pf = pf_num/pf_den if pf_den else 0

    lines = [
        "🧠 <b>SMC Self-Learning Report</b>",
        f"Based on {total} real trades\n",
        f"✅ Wins:     {s['wins']} ({wr:.1f}%)",
        f"❌ Losses:   {s['losses']}",
        f"➡️ BE:        {s['be']}",
        f"💰 Total P&amp;L: {s['total_pnl']:+.2f}%\n",
        "<b>📊 Setup Performance:</b>",
    ]

    for setup, st in sorted(s['by_setup'].items(),
                            key=lambda x: x[1]['w']/max(x[1]['total'],1), reverse=True):
        if st['total'] < 2: continue
        wr_s = st['w']/st['total']*100
        bar = '█' * int(wr_s/10) + '░' * (10-int(wr_s/10))
        learned = db['weights']['setup_scores'].get(setup, 7.0)
        lines.append(f"  {'⚡📊🔄📈'[['SWEEP_OB','HTF_CONFLUENCE','CHOCH','BOS'].index(setup)] if setup in ['SWEEP_OB','HTF_CONFLUENCE','CHOCH','BOS'] else '📡'} "
                     f"{setup}: {st['total']}tr WR:{wr_s:.0f}% {bar}")
        lines.append(f"     Learned score: {learned:.1f}/10 | P&L: {st['pnl']:+.1f}%")

    lines.append("\n<b>📅 Session Performance:</b>")
    for sess, st in s['by_session'].items():
        if st['total'] < 2: continue
        wr_s = st['w']/st['total']*100
        mult = db['weights']['session_weights'].get(sess, 1.0)
        lines.append(f"  {sess}: {st['total']}tr WR:{wr_s:.0f}% → weight:{mult:.2f}x")

    lines.append("\n<b>📈 Best performing tags:</b>")
    tag_stats = [(t,v) for t,v in s['by_tag'].items() if v['total']>=3]
    tag_stats.sort(key=lambda x: x[1]['w']/max(x[1]['total'],1), reverse=True)
    for tag, st in tag_stats[:5]:
        wr_t = st['w']/st['total']*100
        w = db['weights']['tag_weights'].get(tag, 0.5)
        lines.append(f"  {tag}: WR:{wr_t:.0f}% ({st['total']}tr) weight:{w:.2f}")

    if db['learning_log']:
        last = db['learning_log'][-1]
        lines.append(f"\n<b>🧠 Last learning update:</b> {last['time'][:10]}")
        for ch in last['changes'][:5]:
            lines.append(f"  {ch}")

    lines.append(f"\n⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    lines.append("📡 <b>SMC Engine Pro v3 — Self Learning</b>")
    return '\n'.join(lines)

def weekly_learning_report():
    """Sent every Monday — what the engine learned this week"""
    db = load_db()
    recent = [s for s in db['signals']
              if s.get('result') and
              (datetime.now(timezone.utc).timestamp() -
               datetime.fromisoformat(s['time']).timestamp()) < 7*24*3600]
    if not recent:
        return "📅 No trades completed this week."

    wins   = [t for t in recent if t['result']=='win']
    losses = [t for t in recent if t['result']=='loss']
    pnl    = sum(t.get('pnl',0) or 0 for t in recent)
    wr     = len(wins)/len(recent)*100

    # What patterns show up in winners vs losers?
    win_tags  = defaultdict(int)
    loss_tags = defaultdict(int)
    for t in wins:
        for tag in t.get('tags',[]): win_tags[tag] += 1
    for t in losses:
        for tag in t.get('tags',[]): loss_tags[tag] += 1

    lines = [
        "📅 <b>Weekly Learning Report</b>",
        f"Week trades: {len(recent)} | W:{len(wins)} L:{len(losses)}",
        f"Win rate: {wr:.1f}% | P&amp;L: {pnl:+.2f}%\n",
        "<b>🏆 Tags in winning trades:</b>",
    ]
    for tag, cnt in sorted(win_tags.items(), key=lambda x:-x[1])[:5]:
        lines.append(f"  ✅ {tag}: {cnt} wins")
    lines.append("<b>⚠️ Tags in losing trades:</b>")
    for tag, cnt in sorted(loss_tags.items(), key=lambda x:-x[1])[:5]:
        lines.append(f"  ❌ {tag}: {cnt} losses")

    if db['learning_log']:
        lines.append(f"\n<b>Weight changes this week:</b>")
        week_logs = [l for l in db['learning_log']
                     if (datetime.now(timezone.utc).timestamp() -
                         datetime.fromisoformat(l['time']).timestamp()) < 7*24*3600]
        for log in week_logs[-3:]:
            for ch in log['changes'][:3]:
                lines.append(f"  {ch}")

    lines.append(f"\n⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    lines.append("📡 <b>SMC Engine — Self Learning v3</b>")
    return '\n'.join(lines)


# ════════════════════════════════════════════════

# ════════════════════════════════════════════════
# DEEP CHART LEARNING ENGINE
# Re-analyzes chart AFTER every trade closes
# Learns from actual price/volume/RSI conditions
# ════════════════════════════════════════════════
"""
Deep Chart Learning Engine
===========================
After every trade closes (win or loss):
1. Re-fetches the candles from that time period
2. Re-analyzes ALL metrics at the exact entry bar
3. Compares winning conditions vs losing conditions
4. Finds patterns: "When RSI was 25-35 AND volume >1.5x AND London session → 72% WR"
5. Adjusts thresholds based on REAL chart data, not just setup names
"""

# ═══ DEEP LEARNING ENGINE ═══════════════════════════════════════
DEEP_LEARN_FILE = os.environ.get('DEEP_LEARN_FILE', '/app/smc_deep_learning.json')
CG = 'https://api.coingecko.com/api/v3'
KR = 'https://api.kraken.com/0/public'
# ════════════════════════════════════════════════
# GOLD ENGINE (XAU/USD)
# ════════════════════════════════════════════════
# ════════════════════════════════════════════════
# GOLD (XAU/USD) ENGINE
# Data: Yahoo Finance (free, no API key needed)
# Setups: Sweep+OB · ICT Kill Zone · EMA Pullback
# Backtest: PF 1.47 · WR 39% · Score ≥ 6
# ════════════════════════════════════════════════

GOLD_YF  = 'https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF'
GOLD_SYMBOL = 'XAU'
GOLD_NAME   = 'Gold'
GOLD_MIN_SCORE = 6.0

def fetch_gold_candles(limit=200):
    """Fetch gold 1h candles from Yahoo Finance (no API key)"""
    try:
        url = f'{GOLD_YF}?interval=1h&range=30d'
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json',
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read())
        chart = d['chart']['result'][0]
        ts    = chart['timestamp']
        ohlcv = chart['indicators']['quote'][0]
        kl = []
        for j in range(len(ts)):
            o=ohlcv['open'][j]; h=ohlcv['high'][j]
            l=ohlcv['low'][j];  c_=ohlcv['close'][j]
            v=ohlcv.get('volume',[0]*len(ts))[j] or 0
            if None in (o,h,l,c_): continue
            kl.append({'t':int(ts[j]),'o':float(o),'h':float(h),
                       'l':float(l),'c':float(c_),'v':float(v)})
        return kl[-limit:] if len(kl)>=20 else None
    except Exception as e:
        log.debug(f"Gold YF fetch error: {e}")
        return None

def get_gold_session():
    """Gold sessions: London 07-12, NY 13-18 UTC"""
    h = datetime.now(timezone.utc).hour
    d = datetime.now(timezone.utc).weekday()
    if d >= 5: return 'Weekend'
    if 7 <= h <= 12: return 'London'
    if 13 <= h <= 18: return 'New York'
    return 'Asian'

def gold_chop_filter(atr_a, i, thresh=0.40):
    r = [x for x in atr_a[max(0,i-20):i] if x]
    return not r or atr_a[i] < sum(r)/len(r)*thresh

def gold_signal_engine(kl):
    """
    Gold SMC confluence engine.
    4 setups: Sweep+OB · ICT Kill Zone · CHoCH · EMA Pullback
    Returns signal dict or None.
    """
    if not kl or len(kl) < 60: return None
    n   = len(kl); i = n - 1
    closes = [k['c'] for k in kl]; vols = [k['v'] for k in kl]
    ri_a = rsi_a_fn(closes); e9_a  = ema_fn(closes, 9)
    e20_a= ema_fn(closes, 20); e50_a = ema_fn(closes, 50)
    ht_a = macd_hist_fn(closes); at_a  = atr_fn(kl); va_a  = vol_avg_fn(vols)

    if not all([ri_a[i], e9_a[i], e20_a[i], e50_a[i], at_a[i], va_a[i]]): return None
    if gold_chop_filter(at_a, i): return None

    price = closes[i]; k = kl[i]
    at    = at_a[i]; va = va_a[i]
    ri    = ri_a[i]; e9  = e9_a[i]
    e20   = e20_a[i]; e50 = e50_a[i]
    ht    = ht_a[i]

    sh, sl = swings_fn(kl[:i+1], 5)
    weekly = htf_bias_fn(kl, i, 21)
    daily  = htf_bias_fn(kl, i,  5)
    sess   = get_gold_session()

    is_buy  = None; score = 0.0; tags = []; setup = None; wick_sl = None; ob_hit = None

    # ── SETUP 1: SMC Sweep + OB ──────────────────
    for li, lvl in [(ix,float(p)) for ix,p in sl if ix < i-1 and ix > i-50][-4:]:
        if not (k['l'] < lvl < price): continue
        if lvl - k['l'] < at*0.3:       continue
        if k['v'] < va*1.1:             continue
        ob = None
        for j in range(li-1, max(0, li-12), -1):
            if kl[j]['c'] < kl[j]['o']:
                fwd=(kl[min(j+2,n-1)]['c']-kl[j]['c'])/kl[j]['c']
                if fwd > 0.002: ob={'top':kl[j]['o'],'bot':kl[j]['l']}; break
        if not ob or not (ob['bot'] <= price <= ob['top']*1.006): continue
        is_buy=True; setup='SWEEP_OB'; score+=3.5
        tags+=['Sweep↑','OB_Retest']; wick_sl=k['l']; ob_hit=ob; break

    for hi_, lvl in [(ix,float(p)) for ix,p in sh if ix < i-1 and ix > i-50][-4:]:
        if not (k['h'] > lvl > price): continue
        if k['h'] - lvl < at*0.3:      continue
        if k['v'] < va*1.1:            continue
        ob = None
        for j in range(hi_-1, max(0, hi_-12), -1):
            if kl[j]['c'] > kl[j]['o']:
                fwd=(kl[min(j+2,n-1)]['c']-kl[j]['c'])/kl[j]['c']
                if fwd < -0.002: ob={'top':kl[j]['h'],'bot':kl[j]['c']}; break
        if not ob or not (ob['bot']*0.994 <= price <= ob['top']): continue
        is_buy=False; setup='SWEEP_OB'; score+=3.5
        tags+=['Sweep↓','OB_Retest']; wick_sl=k['h']; ob_hit=ob; break

    # ── SETUP 2: ICT Kill Zone Break ─────────────
    if is_buy is None and sess in ('London', 'New York'):
        asia = kl[max(0,i-10):i-1]
        if len(asia) >= 6:
            ahi = max(x['h'] for x in asia); alo = min(x['l'] for x in asia)
            rng = ahi - alo
            if at*0.4 <= rng <= at*3.0:
                if k['c']>ahi and k['v']>va*1.25 and 38<ri<68 and ht and ht>0:
                    is_buy=True; setup='ICT_KZ'; score+=3.0
                    tags+=['KZ_Break↑','Sess✓']; wick_sl=alo
                elif k['c']<alo and k['v']>va*1.25 and 32<ri<62 and ht and ht<0:
                    is_buy=False; setup='ICT_KZ'; score+=3.0
                    tags+=['KZ_Break↓','Sess✓']; wick_sl=ahi

    # ── SETUP 3: CHoCH ───────────────────────────
    if is_buy is None:
        rh=[(ix,float(p)) for ix,p in sh if ix<=i][-5:]
        rl=[(ix,float(p)) for ix,p in sl if ix<=i][-5:]
        if len(rh)>=3 and len(rl)>=3:
            h2,h1p=rh[-2][1],rh[-3][1]; l2,l1p=rl[-2][1],rl[-3][1]
            vok = k['v'] > va*1.05
            if (abs(h2-h1p)/max(h1p,1)>=0.002 and abs(l2-l1p)/max(l1p,1)>=0.002):
                if h2<h1p and l2<l1p and price>h2 and ht and ht>0 and 28<ri<62 and vok:
                    is_buy=True; setup='CHOCH'; score+=3.5; tags+=['CHoCH↑']
                elif h2>h1p and l2>l1p and price<l2 and ht and ht<0 and 38<ri<72 and vok:
                    is_buy=False; setup='CHOCH'; score+=3.5; tags+=['CHoCH↓']

    # ── SETUP 4: EMA Pullback in trend ───────────
    if is_buy is None and e9:
        if e9>e20>e50 and ht and ht>0:
            if abs(price-e20)/e20<0.004 and price>e50 and 38<ri<52:
                is_buy=True; setup='EMA_PULL'; score+=2.5; tags+=['EMA_Pull↑']
        elif e9<e20<e50 and ht and ht<0:
            if abs(price-e20)/e20<0.004 and price<e50 and 48<ri<62:
                is_buy=False; setup='EMA_PULL'; score+=2.5; tags+=['EMA_Pull↓']

    if is_buy is None: return None

    # ── CONFLUENCES ──────────────────────────────
    if k['v'] > va*1.6:  score+=1.0; tags.append('Vol++')
    elif k['v'] > va*1.2: score+=0.5; tags.append('Vol✓')
    if e20 and e50:
        if is_buy and price>e20>e50:    score+=1.0; tags.append('EMA↑')
        elif not is_buy and price<e20<e50: score+=1.0; tags.append('EMA↓')
    if ht:
        if is_buy and ht>0:    score+=0.5; tags.append('MACD+')
        elif not is_buy and ht<0: score+=0.5; tags.append('MACD-')
    if is_buy and ri<35:    score+=1.0; tags.append(f'RSI{round(ri)}')
    elif is_buy and ri<50:  score+=0.5; tags.append(f'RSI{round(ri)}')
    elif not is_buy and ri>65: score+=1.0; tags.append(f'RSI{round(ri)}')
    elif not is_buy and ri>50: score+=0.5; tags.append(f'RSI{round(ri)}')
    if is_buy  and weekly=='bullish': score+=0.5; tags.append('W:Bull')
    elif not is_buy and weekly=='bearish': score+=0.5; tags.append('W:Bear')
    elif (is_buy and weekly=='bearish') or (not is_buy and weekly=='bullish'): score-=1.5
    if is_buy  and daily=='bullish': score+=0.5; tags.append('D:Bull')
    elif not is_buy and daily=='bearish': score+=0.5; tags.append('D:Bear')
    if sess in ('London','New York'): score+=0.5; tags.append('Sess✓') if 'Sess✓' not in tags else None
    step=25.0; nr=round(price/step)*step; dist=abs(price-nr)/at
    if dist<0.6: score+=0.5; tags.append(f'${int(nr)}')
    # Anti-trend
    r6=kl[max(0,i-6):i+1]
    bc=sum(1 for x in r6 if x['c']<x['o'])
    if is_buy and bc>=5:       score-=2.0
    if not is_buy and (6-bc)>=5: score-=2.0

    score = round(max(0, min(10, score)), 1)
    if score < GOLD_MIN_SCORE: return None

    # Weekend block
    if sess == 'Weekend' and score < 8.0: return None

    # ── SL PLACEMENT ─────────────────────────────
    if wick_sl is not None:
        sl_p = wick_sl - at*0.10 if is_buy else wick_sl + at*0.10
    else:
        sl_p = price - at*1.5 if is_buy else price + at*1.5
    # Gold max SL: 0.8% ($16 at $2000)
    max_risk = price * 0.008
    if is_buy  and (price-sl_p)>max_risk: sl_p=price-max_risk
    if not is_buy and (sl_p-price)>max_risk: sl_p=price+max_risk
    risk = abs(price-sl_p)
    if risk <= 0: return None

    rr_mult = 2.8 if setup=='SWEEP_OB' else 2.5
    tp  = price+risk*rr_mult if is_buy else price-risk*rr_mult
    tp1 = price+risk*2.0     if is_buy else price-risk*2.0
    tp3 = price+risk*3.0     if is_buy else price-risk*3.0
    rr  = round(abs(tp-price)/risk, 1)
    if rr < 2.0: return None

    risk_pct = round(abs(price-sl_p)/price*100, 2)
    rew_pct  = round(abs(tp-price)/price*100, 2)
    conf     = min(96, round(score*8 + min(rr,3)*2.5))

    setup_names = {
        'SWEEP_OB': '⚡ Gold Liq Sweep + OB Retest',
        'ICT_KZ':   '🕯️ Gold ICT Kill Zone Break',
        'CHOCH':    '🔄 Gold CHoCH Reversal',
        'EMA_PULL': '📊 Gold EMA Pullback',
    }

    return {
        'sym':      GOLD_SYMBOL,
        'name':     GOLD_NAME,
        'pair':     'XAU/USD',
        'dir':      'BUY' if is_buy else 'SELL',
        'setup':    setup,
        'setup_name': setup_names.get(setup, setup),
        'score':    score,
        'conf':     conf,
        'rr':       rr,
        'price':    price,
        'entry':    price,
        'sl':       round(sl_p, 2),
        'tp':       round(tp, 2),
        'tp1':      round(tp1, 2),
        'tp3':      round(tp3, 2),
        'risk_pct': risk_pct,
        'rew_pct':  rew_pct,
        'tags':     tags,
        'weekly':   weekly,
        'daily':    daily,
        'session':  sess,
        'ob':       ob_hit,
        'wick_sl':  round(wick_sl, 2) if wick_sl else None,
        'rsi_val':  round(ri, 1),
        'is_gold':  True,
    }

def build_gold_signal_msg(sig):
    """Gold-specific Telegram message with trade basis"""
    is_buy = sig['dir'] == 'BUY'
    setup_tips = {
        'SWEEP_OB': (
            "📌 <i>Institutions swept retail stops then reversed.\n"
            "Entry on OB retest — SL below swept wick.</i>"
        ),
        'ICT_KZ': (
            "📌 <i>ICT Kill Zone — price broke out of Asia range\n"
            "at London/NY session open with volume. Trend continuation.</i>"
        ),
        'CHOCH': (
            "📌 <i>Change of Character — gold structure shifted.\n"
            "First entry on new direction. Tight SL at last swing.</i>"
        ),
        'EMA_PULL': (
            "📌 <i>Gold pulled back to EMA20 in strong trend.\n"
            "Classic trend-following re-entry.</i>"
        ),
    }

    why = {
        'SWEEP_OB': (
            f"  1️⃣ Equal lows swept at <code>{fp(sig.get('wick_sl', sig['sl']))}</code>\n"
            f"  2️⃣ Gold closed back above — stop hunt complete\n"
            f"  3️⃣ OB zone: <code>{fp(sig['ob']['bot']) if sig.get('ob') else '—'}</code> – <code>{fp(sig['ob']['top']) if sig.get('ob') else '—'}</code>\n"
            f"  4️⃣ Retesting OB — institutions defending\n"
            f"  5️⃣ SL just below swept wick (tight)"
        ),
        'ICT_KZ': (
            f"  1️⃣ Gold consolidated during Asian session\n"
            f"  2️⃣ {sig.get('session','London')} open broke Asia range with volume\n"
            f"  3️⃣ MACD + EMA confirm direction\n"
            f"  4️⃣ SL below/above Asia range"
        ),
        'CHOCH': (
            f"  1️⃣ Gold was in {'downtrend' if is_buy else 'uptrend'}\n"
            f"  2️⃣ Structure break — CHoCH confirmed\n"
            f"  3️⃣ First entry on new {'bullish' if is_buy else 'bearish'} structure\n"
            f"  4️⃣ SL at last swing {'low' if is_buy else 'high'}"
        ),
        'EMA_PULL': (
            f"  1️⃣ Gold in strong {'uptrend' if is_buy else 'downtrend'}\n"
            f"  2️⃣ Pulled back to EMA20 {'from above' if is_buy else 'from below'}\n"
            f"  3️⃣ RSI reset to neutral {'from overbought' if not is_buy else 'from oversold'}\n"
            f"  4️⃣ Trend continuation — EMAs still aligned"
        ),
    }

    rr_mult = sig['rr']
    return '\n'.join(filter(None, [
        f"{'🟡' if is_buy else '🔴'} <b>GOLD {'BUY' if is_buy else 'SELL'} — XAU/USD</b>",
        f"{'⚡🕯️🔄📊'.split()[['SWEEP_OB','ICT_KZ','CHOCH','EMA_PULL'].index(sig['setup'])] if sig['setup'] in ['SWEEP_OB','ICT_KZ','CHOCH','EMA_PULL'] else '📡'} <b>{sig['setup_name']}</b>",
        f"",
        setup_tips.get(sig['setup'], ''),
        f"",
        f"📖 <b>Why this trade:</b>",
        why.get(sig['setup'], '  SMC confluence setup'),
        f"",
        f"💰 <b>Trade Levels</b>",
        f"  Entry:  <code>{fp(sig['price'])}</code>",
        f"  SL:     <code>{fp(sig['sl'])}</code>  <i>(-{sig['risk_pct']}%) ≈ ${round(abs(sig['price']-sig['sl']))}</i>",
        f"  TP1:    <code>{fp(sig['tp1'])}</code>  <i>(1:2 — close 50%, move SL to entry)</i>",
        f"  TP2:    <code>{fp(sig['tp'])}</code>   <i>(1:{rr_mult} — close 30%)</i>",
        f"  TP3:    <code>{fp(sig['tp3'])}</code>  <i>(1:3 — let runner go)</i>",
        f"",
        f"📊 <b>Score: {sig['score']}/10  |  Conf: {sig['conf']}%  |  R:R 1:{rr_mult}</b>",
        f"  Tags: {esc(' · '.join(sig['tags']))}",
        f"  Weekly: {esc(sig.get('weekly','—'))}  |  Daily: {esc(sig.get('daily','—'))}  |  RSI: {sig.get('rsi_val','—')}",
        f"  Session: {sig.get('session','—')}",
        sig['ob'] and f"  OB Zone: {fp(sig['ob']['bot'])} – {fp(sig['ob']['top'])}",
        f"",
        f"⚠️ <i>Gold intraday — respect session times. Not financial advice.</i>",
        f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC  |  🏅 <b>SMC Gold Engine</b>",
    ]))



# ── METRICS WE ANALYZE ON EVERY TRADE ────────────────────────────────
# These are the exact conditions at the entry bar
METRIC_KEYS = [
    'rsi',           # RSI value at entry
    'rsi_zone',      # oversold/neutral/overbought
    'volume_ratio',  # volume / 20-bar average
    'atr_ratio',     # current ATR / 20-bar ATR avg (chop measure)
    'session',       # London / NY / Asian / Weekend
    'weekly_bias',   # bullish / bearish / neutral
    'daily_bias',    # bullish / bearish / neutral
    'ema_aligned',   # True if EMA stack aligned with direction
    'macd_positive', # True if MACD histogram positive (for buys)
    'ob_quality',    # clean / messy (how well-defined OB was)
    'sweep_size',    # wick size / ATR ratio
    'bars_since_sweep', # how many bars since the sweep candle
    'score',         # signal score at time of entry
    'rr',            # risk:reward ratio
    'setup',         # SWEEP_OB / CHOCH / BOS / HTF
    'direction',     # BUY / SELL
]

def load_deep_db():
    if Path(DEEP_LEARN_FILE).exists():
        try:
            with open(DEEP_LEARN_FILE) as f:
                return json.load(f)
        except:
            pass
    return {
        'version': 1,
        'created': datetime.now(timezone.utc).isoformat(),
        'trades': [],           # full trade records with metrics
        'patterns': {},         # discovered winning patterns
        'thresholds': {         # learned optimal thresholds
            'min_volume_ratio':  1.15,
            'min_rsi_buy_max':   62,    # RSI must be below this for buys
            'max_rsi_buy_min':   25,    # RSI must be above this for buys
            'min_sweep_size':    0.28,  # min sweep wick / ATR
            'max_bars_retest':   8,     # max bars after sweep to retest
            'min_atr_ratio':     0.40,  # min ATR vs average (not choppy)
            'min_score':         7.0,
            'best_sessions':     ['London', 'New York'],
            'avoid_weekly':      ['bearish'],  # avoid buying in these
        },
        'condition_stats': {},  # win rate per condition value
        'insights': [],         # human-readable insights discovered
    }

def save_deep_db(db):
    try:
        with open(DEEP_LEARN_FILE, 'w') as f:
            json.dump(db, f, indent=2)
    except Exception as e:
        print(f"Deep DB save error: {e}")

# ── FETCH HISTORICAL CANDLES FOR RE-ANALYSIS ─────────────────────────
def fetch_candles_at_time(pair_cg, pair_kr, timestamp, limit=100):
    """Fetch candles around a specific timestamp for post-trade analysis"""
    try:
        r = requests.get(f'{KR}/OHLC',
            params={'pair': pair_kr, 'interval': 60, 'since': int(timestamp)-3600*limit},
            timeout=15)
        d = r.json()
        if not d.get('error'):
            key = next((k for k in d['result'] if k != 'last'), None)
            if key:
                raw = d['result'][key]
                return [{'t': int(k[0]), 'o':float(k[1]),'h':float(k[2]),
                         'l':float(k[3]),'c':float(k[4]),'v':float(k[6])}
                        for k in raw[-limit:]]
    except: pass
    try:
        r = requests.get(f'{CG}/coins/{pair_cg}/ohlc',
            params={'vs_currency':'usd','days':7}, timeout=15)
        raw = r.json()
        if isinstance(raw, list):
            return [{'t':int(k[0]/1000),'o':float(k[1]),'h':float(k[2]),
                     'l':float(k[3]),'c':float(k[4]),'v':50.0}
                    for k in raw[-limit:]]
    except: pass
    return []

# ── INDICATORS ────────────────────────────────────────────────────────
def _ema(c, p):
    if len(c) < p: return [None]*len(c)
    k=2/(p+1); r=[None]*(p-1); s=sum(c[:p])/p; r.append(s); pv=s
    for i in range(p,len(c)): pv=c[i]*k+pv*(1-k); r.append(pv)
    return r

def _rsi(c, p=14):
    if len(c)<p+1: return [None]*len(c)
    r=[None]*p; g=l=0.0
    for i in range(1,p+1):
        d=c[i]-c[i-1]
        if d>0: g+=d
        else: l+=abs(d)
    ag,al=g/p,l/p; r.append(100 if al==0 else 100-100/(1+ag/al))
    for i in range(p+1,len(c)):
        d=c[i]-c[i-1]; ag=(ag*(p-1)+(d if d>0 else 0))/p; al=(al*(p-1)+(abs(d) if d<0 else 0))/p
        r.append(100 if al==0 else 100-100/(1+ag/al))
    return r

def _macd_hist(c):
    e12=_ema(c,12); e26=_ema(c,26)
    ln=[e12[i]-e26[i] if e12[i] and e26[i] else None for i in range(len(c))]
    vl=[v for v in ln if v]
    if len(vl)<9: return [None]*len(c)
    sr=_ema(vl,9); sg=[None]*len(c); si=0
    for i in range(len(c)):
        if ln[i] is not None: sg[i]=sr[si] if si<len(sr) else None; si+=1
    return [ln[i]-sg[i] if ln[i] and sg[i] else None for i in range(len(c))]

def _atr(kl, p=14):
    tr=[None]+[max(kl[i]['h']-kl[i]['l'],abs(kl[i]['h']-kl[i-1]['c']),
               abs(kl[i]['l']-kl[i-1]['c'])) for i in range(1,len(kl))]
    if len(tr)<p+1: return [None]*len(kl)
    r=[None]*p; s=sum(tr[1:p+1])/p; r.append(s); pv=s
    for i in range(p+1,len(tr)): pv=(pv*(p-1)+tr[i])/p; r.append(pv)
    return r

def _vol_avg(v, p=20):
    r=[None]*p
    for i in range(p,len(v)): r.append(sum(v[i-p:i])/p)
    return r

# ── DEEP METRIC EXTRACTION ────────────────────────────────────────────
def extract_metrics_from_chart(kl, trade):
    """
    Re-analyze ALL chart conditions at the exact entry bar.
    This is what the engine ACTUALLY saw when it fired the signal.
    Returns a dict of all measurable conditions.
    """
    if not kl or len(kl) < 30:
        return None

    i = len(kl) - 1
    closes = [k['c'] for k in kl]
    vols   = [k['v'] for k in kl]
    price  = closes[i]

    rsi_a  = _rsi(closes)
    e20_a  = _ema(closes, 20)
    e50_a  = _ema(closes, 50)
    e9_a   = _ema(closes, 9)
    ht_a   = _macd_hist(closes)
    atr_a  = _atr(kl)
    va_a   = _vol_avg(vols)

    if not all([rsi_a[i], e20_a[i], e50_a[i], atr_a[i], va_a[i]]):
        return None

    is_buy      = trade['dir'] == 'BUY'
    rsi_val     = round(rsi_a[i], 1)
    vol_ratio   = round(kl[i]['v'] / va_a[i], 2) if va_a[i] else 0
    atr_ratio   = round(atr_a[i] / (sum(a for a in atr_a[max(0,i-20):i] if a) /
                        max(1, len([a for a in atr_a[max(0,i-20):i] if a]))), 2) \
                  if atr_a[i] else 0

    # EMA alignment
    ema_aligned = (price > e20_a[i] > e50_a[i]) if is_buy else (price < e20_a[i] < e50_a[i])
    ema_stack   = (e9_a[i] > e20_a[i] > e50_a[i]) if (is_buy and e9_a[i]) else \
                  (e9_a[i] < e20_a[i] < e50_a[i]) if (not is_buy and e9_a[i]) else False

    # MACD confirmation
    macd_ok = (ht_a[i] > 0) if is_buy else (ht_a[i] < 0) if ht_a[i] else False

    # RSI zone
    rsi_zone = ('oversold' if rsi_val < 35 else
                'neutral'  if rsi_val < 65 else 'overbought')

    # Candle quality at entry
    body    = abs(kl[i]['c'] - kl[i]['o'])
    rng     = kl[i]['h'] - kl[i]['l'] + 1e-10
    body_pct = round(body/rng*100, 1)

    # Sweep-specific metrics
    sweep_size   = 0.0
    bars_retest  = 0
    ob_quality   = 'none'

    if trade.get('setup') == 'SWEEP_OB':
        swept_lvl = trade.get('swept_price', price)
        if swept_lvl and atr_a[i]:
            # How big was the sweep wick relative to ATR?
            sweep_wick = abs(swept_lvl - min(kl[max(0,i-5):i+1], key=lambda x:x['l'])['l'])
            sweep_size = round(sweep_wick / atr_a[i], 2)
        # How many bars ago was the actual sweep?
        bars_retest = trade.get('bars_since_sweep', 0)
        # OB quality: how clean was the OB candle
        ob_top = trade.get('ob_top', 0)
        ob_bot = trade.get('ob_bot', 0)
        if ob_top and ob_bot and atr_a[i]:
            ob_size = ob_top - ob_bot
            ob_quality = 'clean' if ob_size > atr_a[i]*0.3 else 'small'

    # Recent trend strength (bearish or bullish pressure)
    recent6 = kl[max(0,i-6):i+1]
    bear_candles = sum(1 for x in recent6 if x['c'] < x['o'])
    bull_candles = len(recent6) - bear_candles
    trend_pressure = 'strong_bear' if bear_candles >= 5 else \
                     'strong_bull' if bull_candles >= 5 else 'mixed'

    # Consecutive same-direction candles
    consec = 0
    for j in range(i, max(0, i-8), -1):
        if is_buy and kl[j]['c'] < kl[j]['o']: consec += 1
        elif not is_buy and kl[j]['c'] > kl[j]['o']: consec += 1
        else: break

    return {
        'rsi':              rsi_val,
        'rsi_zone':         rsi_zone,
        'volume_ratio':     vol_ratio,
        'atr_ratio':        atr_ratio,
        'ema_aligned':      ema_aligned,
        'ema_stack':        ema_stack,
        'macd_ok':          macd_ok,
        'body_pct':         body_pct,
        'sweep_size':       sweep_size,
        'bars_retest':      bars_retest,
        'ob_quality':       ob_quality,
        'trend_pressure':   trend_pressure,
        'consec_against':   consec,
        'session':          trade.get('session', 'unknown'),
        'weekly_bias':      trade.get('weekly', 'neutral'),
        'daily_bias':       trade.get('daily', 'neutral'),
        'score':            trade.get('score', 0),
        'rr':               trade.get('rr', 0),
        'setup':            trade.get('setup', ''),
        'direction':        trade.get('dir', ''),
    }

# ── LEARN FROM CLOSED TRADE ───────────────────────────────────────────
def learn_from_trade(trade, result, kl=None):
    """
    Called when a trade closes.
    Extracts chart metrics and stores them.
    Analyzes patterns after every 10 trades.
    """
    db = load_deep_db()

    # Extract metrics from chart
    metrics = None
    if kl:
        metrics = extract_metrics_from_chart(kl, trade)

    record = {
        'id':       trade.get('id', f"{trade['sym']}_{int(time.time())}"),
        'sym':      trade['sym'],
        'setup':    trade.get('setup', ''),
        'dir':      trade['dir'],
        'result':   result,
        'pnl':      trade.get('pnl', 0),
        'time':     datetime.now(timezone.utc).isoformat(),
        'metrics':  metrics,
        'tags':     trade.get('tags', []),
    }
    db['trades'].append(record)

    # Update condition stats
    if metrics:
        _update_condition_stats(db, metrics, result)

    # Analyze patterns every 10 trades
    total = len([t for t in db['trades'] if t.get('result')])
    if total >= 10 and total % 5 == 0:
        insights = _find_patterns(db)
        if insights:
            db['insights'].extend(insights)
            # Apply threshold updates
            _update_thresholds(db)

    save_deep_db(db)
    return record

def _update_condition_stats(db, metrics, result):
    """Track win rate for each condition value"""
    is_win = result == 'win'

    conditions_to_track = {
        'session':       metrics.get('session'),
        'weekly_bias':   metrics.get('weekly_bias'),
        'rsi_zone':      metrics.get('rsi_zone'),
        'ema_aligned':   str(metrics.get('ema_aligned')),
        'macd_ok':       str(metrics.get('macd_ok')),
        'ob_quality':    metrics.get('ob_quality'),
        'trend_pressure':metrics.get('trend_pressure'),
        'high_volume':   str(metrics.get('volume_ratio', 0) >= 1.5),
        'very_high_vol': str(metrics.get('volume_ratio', 0) >= 2.0),
        'clean_rsi_buy': str(metrics.get('rsi', 50) < 45 and metrics.get('direction')=='BUY'),
        'no_trend_press':str(metrics.get('trend_pressure') == 'mixed'),
        'low_consec':    str(metrics.get('consec_against', 0) <= 2),
    }
    for key, val in conditions_to_track.items():
        if val is None: continue
        stat_key = f"{key}:{val}"
        if stat_key not in db['condition_stats']:
            db['condition_stats'][stat_key] = {'w':0,'l':0,'be':0,'total':0}
        s = db['condition_stats'][stat_key]
        s['total'] += 1
        if result == 'win':   s['w'] += 1
        elif result == 'loss': s['l'] += 1
        else:                  s['be'] += 1

def _find_patterns(db):
    """Find conditions that predict wins vs losses"""
    insights = []
    stats = db['condition_stats']
    thresholds = db['thresholds']

    for key, s in stats.items():
        if s['total'] < 5: continue
        wr = s['w'] / s['total']

        # High win rate condition
        if wr >= 0.65 and s['total'] >= 5:
            insights.append({
                'type': 'positive',
                'condition': key,
                'wr': round(wr*100),
                'trades': s['total'],
                'message': f"✅ When {key} → WR {round(wr*100)}% ({s['total']} trades)"
            })

        # Low win rate condition — this is a WARNING
        elif wr <= 0.30 and s['total'] >= 5:
            insights.append({
                'type': 'negative',
                'condition': key,
                'wr': round(wr*100),
                'trades': s['total'],
                'message': f"❌ When {key} → WR only {round(wr*100)}% — AVOID ({s['total']} trades)"
            })

    return insights[-10:] if insights else []  # keep latest 10

def _update_thresholds(db):
    """
    Auto-adjust detection thresholds based on real performance.
    This is where the engine actually gets smarter.
    """
    stats  = db['condition_stats']
    thresh = db['thresholds']
    changes = []

    # Session learning
    for sess in ['London', 'New York', 'Asian', 'Weekend']:
        key = f"session:{sess}"
        if key in stats and stats[key]['total'] >= 5:
            wr = stats[key]['w'] / stats[key]['total']
            if wr < 0.30 and sess in thresh['best_sessions']:
                thresh['best_sessions'].remove(sess)
                changes.append(f"Removed {sess} from best sessions (WR:{round(wr*100)}%)")
            elif wr >= 0.55 and sess not in thresh['best_sessions']:
                thresh['best_sessions'].append(sess)
                changes.append(f"Added {sess} to best sessions (WR:{round(wr*100)}%)")

    # Weekly bias learning
    for bias in ['bullish', 'neutral', 'bearish']:
        key = f"weekly_bias:{bias}"
        if key in stats and stats[key]['total'] >= 5:
            wr = stats[key]['w'] / stats[key]['total']
            if wr < 0.30 and bias not in thresh['avoid_weekly']:
                thresh['avoid_weekly'].append(bias)
                changes.append(f"Added weekly:{bias} to avoid list (WR:{round(wr*100)}%)")
            elif wr >= 0.55 and bias in thresh['avoid_weekly']:
                thresh['avoid_weekly'].remove(bias)
                changes.append(f"Removed weekly:{bias} from avoid list (WR:{round(wr*100)}%)")

    # Volume threshold
    hi_vol = stats.get('high_volume:True', {'w':0,'total':0})
    lo_vol_key = 'high_volume:False'
    lo_vol = stats.get(lo_vol_key, {'w':0,'total':0})
    if hi_vol['total'] >= 5 and lo_vol['total'] >= 5:
        wr_hi = hi_vol['w']/hi_vol['total']
        wr_lo = lo_vol['w']/lo_vol['total']
        if wr_hi > wr_lo + 0.15:
            thresh['min_volume_ratio'] = max(1.3, thresh['min_volume_ratio'])
            changes.append(f"Raised min_volume_ratio (high vol WR:{round(wr_hi*100)}% vs low:{round(wr_lo*100)}%)")
        elif wr_hi < wr_lo:
            thresh['min_volume_ratio'] = max(1.0, thresh['min_volume_ratio'] - 0.05)
            changes.append(f"Lowered min_volume_ratio")

    # Trend pressure
    no_press = stats.get('no_trend_press:True', {'w':0,'total':0})
    press    = stats.get('no_trend_press:False', {'w':0,'total':0})
    if no_press['total'] >= 5 and press['total'] >= 5:
        wr_np = no_press['w']/no_press['total']
        wr_p  = press['w']/press['total']
        if wr_np > wr_p + 0.15:
            # Mixed trend is better — relax the filter
            changes.append(f"Confirmed: no trend pressure better (WR:{round(wr_np*100)}% vs {round(wr_p*100)}%)")
        elif wr_p > wr_np + 0.15:
            changes.append(f"Trend pressure actually helps! WR:{round(wr_p*100)}%")

    if changes:
        db['insights'].append({
            'type': 'threshold_update',
            'time': datetime.now(timezone.utc).isoformat(),
            'changes': changes
        })

    db['thresholds'] = thresh

# ── DEEP LEARNING REPORT ──────────────────────────────────────────────
def deep_learning_report():
    db = load_deep_db()
    trades = [t for t in db['trades'] if t.get('result')]
    if not trades:
        return "🧠 No completed trades yet for deep analysis."

    wins   = [t for t in trades if t['result']=='win']
    losses = [t for t in trades if t['result']=='loss']
    wr     = len(wins)/len(trades)*100

    lines = [
        "🧠 <b>Deep Chart Learning Report</b>",
        f"Analyzed {len(trades)} trades from real chart data\n",
        f"✅ Wins: {len(wins)} | ❌ Losses: {len(losses)} | WR: {wr:.1f}%\n",
        "<b>📊 What the engine learned:</b>",
    ]

    # Show top positive patterns
    pos = [i for i in db['insights'] if i.get('type')=='positive']
    neg = [i for i in db['insights'] if i.get('type')=='negative']

    if pos:
        lines.append("\n✅ <b>Conditions that WIN:</b>")
        for p in sorted(pos, key=lambda x:-x['wr'])[:5]:
            lines.append(f"  {p['message']}")

    if neg:
        lines.append("\n❌ <b>Conditions to AVOID:</b>")
        for n in sorted(neg, key=lambda x:x['wr'])[:5]:
            lines.append(f"  {n['message']}")

    # Current thresholds
    t = db['thresholds']
    lines += [
        "\n<b>⚙️ Learned Thresholds:</b>",
        f"  Min volume ratio: {t['min_volume_ratio']}x avg",
        f"  Best sessions: {', '.join(t['best_sessions'])}",
        f"  Avoid weekly: {', '.join(t['avoid_weekly']) or 'none'}",
        f"  Min score: {t['min_score']}",
    ]

    # Win/loss metric comparison
    if len(wins) >= 3 and len(losses) >= 3:
        def avg_metric(trade_list, key):
            vals = [t['metrics'][key] for t in trade_list
                    if t.get('metrics') and t['metrics'].get(key) is not None
                    and isinstance(t['metrics'][key], (int, float))]
            return round(sum(vals)/len(vals), 2) if vals else None

        lines.append("\n<b>📈 Average metrics — Wins vs Losses:</b>")
        for metric, label in [('rsi','RSI'),('volume_ratio','Volume ratio'),
                               ('score','Score'),('rr','R:R')]:
            w_avg = avg_metric(wins, metric)
            l_avg = avg_metric(losses, metric)
            if w_avg and l_avg:
                diff = '↑ Better' if (metric in ('volume_ratio','score','rr') and w_avg>l_avg) or \
                                      (metric=='rsi' and abs(w_avg-50)<abs(l_avg-50)) else '↓ Worse'
                lines.append(f"  {label}: Wins={w_avg} | Losses={l_avg} {diff}")

    lines.append(f"\n⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    lines.append("📡 <b>SMC Deep Learning Engine</b>")
    return '\n'.join(lines)

def get_learned_thresholds():
    """Returns current learned thresholds for use in signal detection"""
    return load_deep_db()['thresholds']




logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger(__name__)

# ── CONFIG ─────────────────────────────────────
TG_TOKEN   = os.environ.get('TG_TOKEN', '')
TG_CHAT    = os.environ.get('TG_CHAT', '')
MIN_SCORE  = int(os.environ.get('MIN_SCORE', '6'))
SCAN_EVERY = int(os.environ.get('SCAN_EVERY_MIN', '1'))
COOLDOWN_M = int(os.environ.get('COOLDOWN_MIN', '30'))
PORT       = int(os.environ.get('PORT', '8080'))

PAIRS = [
    {'sym':'BTC',  'kr':'XXBTZUSD', 'cg':'bitcoin'},
    {'sym':'ETH',  'kr':'XETHZUSD', 'cg':'ethereum'},
    {'sym':'SOL',  'kr':'SOLUSD',   'cg':'solana'},
    {'sym':'XRP',  'kr':'XXRPZUSD', 'cg':'ripple'},
    {'sym':'ADA',  'kr':'ADAUSD',   'cg':'cardano'},
    {'sym':'DOGE', 'kr':'XDGUSD',   'cg':'dogecoin'},
    {'sym':'AVAX', 'kr':'AVAXUSD',  'cg':'avalanche-2'},
    {'sym':'DOT',  'kr':'DOTUSD',   'cg':'polkadot'},
    {'sym':'LINK', 'kr':'LINKUSD',  'cg':'chainlink'},
    {'sym':'MATIC','kr':'MATICUSD', 'cg':'matic-network'},
]

KR = 'https://api.kraken.com/0/public'
# ════════════════════════════════════════════════
# GOLD ENGINE (XAU/USD)
# ════════════════════════════════════════════════
# ════════════════════════════════════════════════
# GOLD (XAU/USD) ENGINE
# Data: Yahoo Finance (free, no API key needed)
# Setups: Sweep+OB · ICT Kill Zone · EMA Pullback
# Backtest: PF 1.47 · WR 39% · Score ≥ 6
# ════════════════════════════════════════════════

GOLD_YF  = 'https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF'
GOLD_SYMBOL = 'XAU'
GOLD_NAME   = 'Gold'
GOLD_MIN_SCORE = 6.0

def fetch_gold_candles(limit=200):
    """Fetch gold 1h candles from Yahoo Finance (no API key)"""
    try:
        url = f'{GOLD_YF}?interval=1h&range=30d'
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json',
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read())
        chart = d['chart']['result'][0]
        ts    = chart['timestamp']
        ohlcv = chart['indicators']['quote'][0]
        kl = []
        for j in range(len(ts)):
            o=ohlcv['open'][j]; h=ohlcv['high'][j]
            l=ohlcv['low'][j];  c_=ohlcv['close'][j]
            v=ohlcv.get('volume',[0]*len(ts))[j] or 0
            if None in (o,h,l,c_): continue
            kl.append({'t':int(ts[j]),'o':float(o),'h':float(h),
                       'l':float(l),'c':float(c_),'v':float(v)})
        return kl[-limit:] if len(kl)>=20 else None
    except Exception as e:
        log.debug(f"Gold YF fetch error: {e}")
        return None

def get_gold_session():
    """Gold sessions: London 07-12, NY 13-18 UTC"""
    h = datetime.now(timezone.utc).hour
    d = datetime.now(timezone.utc).weekday()
    if d >= 5: return 'Weekend'
    if 7 <= h <= 12: return 'London'
    if 13 <= h <= 18: return 'New York'
    return 'Asian'

def gold_chop_filter(atr_a, i, thresh=0.40):
    r = [x for x in atr_a[max(0,i-20):i] if x]
    return not r or atr_a[i] < sum(r)/len(r)*thresh

def gold_signal_engine(kl):
    """
    Gold SMC confluence engine.
    4 setups: Sweep+OB · ICT Kill Zone · CHoCH · EMA Pullback
    Returns signal dict or None.
    """
    if not kl or len(kl) < 60: return None
    n   = len(kl); i = n - 1
    closes = [k['c'] for k in kl]; vols = [k['v'] for k in kl]
    ri_a = rsi_a_fn(closes); e9_a  = ema_fn(closes, 9)
    e20_a= ema_fn(closes, 20); e50_a = ema_fn(closes, 50)
    ht_a = macd_hist_fn(closes); at_a  = atr_fn(kl); va_a  = vol_avg_fn(vols)

    if not all([ri_a[i], e9_a[i], e20_a[i], e50_a[i], at_a[i], va_a[i]]): return None
    if gold_chop_filter(at_a, i): return None

    price = closes[i]; k = kl[i]
    at    = at_a[i]; va = va_a[i]
    ri    = ri_a[i]; e9  = e9_a[i]
    e20   = e20_a[i]; e50 = e50_a[i]
    ht    = ht_a[i]

    sh, sl = swings_fn(kl[:i+1], 5)
    weekly = htf_bias_fn(kl, i, 21)
    daily  = htf_bias_fn(kl, i,  5)
    sess   = get_gold_session()

    is_buy  = None; score = 0.0; tags = []; setup = None; wick_sl = None; ob_hit = None

    # ── SETUP 1: SMC Sweep + OB ──────────────────
    for li, lvl in [(ix,float(p)) for ix,p in sl if ix < i-1 and ix > i-50][-4:]:
        if not (k['l'] < lvl < price): continue
        if lvl - k['l'] < at*0.3:       continue
        if k['v'] < va*1.1:             continue
        ob = None
        for j in range(li-1, max(0, li-12), -1):
            if kl[j]['c'] < kl[j]['o']:
                fwd=(kl[min(j+2,n-1)]['c']-kl[j]['c'])/kl[j]['c']
                if fwd > 0.002: ob={'top':kl[j]['o'],'bot':kl[j]['l']}; break
        if not ob or not (ob['bot'] <= price <= ob['top']*1.006): continue
        is_buy=True; setup='SWEEP_OB'; score+=3.5
        tags+=['Sweep↑','OB_Retest']; wick_sl=k['l']; ob_hit=ob; break

    for hi_, lvl in [(ix,float(p)) for ix,p in sh if ix < i-1 and ix > i-50][-4:]:
        if not (k['h'] > lvl > price): continue
        if k['h'] - lvl < at*0.3:      continue
        if k['v'] < va*1.1:            continue
        ob = None
        for j in range(hi_-1, max(0, hi_-12), -1):
            if kl[j]['c'] > kl[j]['o']:
                fwd=(kl[min(j+2,n-1)]['c']-kl[j]['c'])/kl[j]['c']
                if fwd < -0.002: ob={'top':kl[j]['h'],'bot':kl[j]['c']}; break
        if not ob or not (ob['bot']*0.994 <= price <= ob['top']): continue
        is_buy=False; setup='SWEEP_OB'; score+=3.5
        tags+=['Sweep↓','OB_Retest']; wick_sl=k['h']; ob_hit=ob; break

    # ── SETUP 2: ICT Kill Zone Break ─────────────
    if is_buy is None and sess in ('London', 'New York'):
        asia = kl[max(0,i-10):i-1]
        if len(asia) >= 6:
            ahi = max(x['h'] for x in asia); alo = min(x['l'] for x in asia)
            rng = ahi - alo
            if at*0.4 <= rng <= at*3.0:
                if k['c']>ahi and k['v']>va*1.25 and 38<ri<68 and ht and ht>0:
                    is_buy=True; setup='ICT_KZ'; score+=3.0
                    tags+=['KZ_Break↑','Sess✓']; wick_sl=alo
                elif k['c']<alo and k['v']>va*1.25 and 32<ri<62 and ht and ht<0:
                    is_buy=False; setup='ICT_KZ'; score+=3.0
                    tags+=['KZ_Break↓','Sess✓']; wick_sl=ahi

    # ── SETUP 3: CHoCH ───────────────────────────
    if is_buy is None:
        rh=[(ix,float(p)) for ix,p in sh if ix<=i][-5:]
        rl=[(ix,float(p)) for ix,p in sl if ix<=i][-5:]
        if len(rh)>=3 and len(rl)>=3:
            h2,h1p=rh[-2][1],rh[-3][1]; l2,l1p=rl[-2][1],rl[-3][1]
            vok = k['v'] > va*1.05
            if (abs(h2-h1p)/max(h1p,1)>=0.002 and abs(l2-l1p)/max(l1p,1)>=0.002):
                if h2<h1p and l2<l1p and price>h2 and ht and ht>0 and 28<ri<62 and vok:
                    is_buy=True; setup='CHOCH'; score+=3.5; tags+=['CHoCH↑']
                elif h2>h1p and l2>l1p and price<l2 and ht and ht<0 and 38<ri<72 and vok:
                    is_buy=False; setup='CHOCH'; score+=3.5; tags+=['CHoCH↓']

    # ── SETUP 4: EMA Pullback in trend ───────────
    if is_buy is None and e9:
        if e9>e20>e50 and ht and ht>0:
            if abs(price-e20)/e20<0.004 and price>e50 and 38<ri<52:
                is_buy=True; setup='EMA_PULL'; score+=2.5; tags+=['EMA_Pull↑']
        elif e9<e20<e50 and ht and ht<0:
            if abs(price-e20)/e20<0.004 and price<e50 and 48<ri<62:
                is_buy=False; setup='EMA_PULL'; score+=2.5; tags+=['EMA_Pull↓']

    if is_buy is None: return None

    # ── CONFLUENCES ──────────────────────────────
    if k['v'] > va*1.6:  score+=1.0; tags.append('Vol++')
    elif k['v'] > va*1.2: score+=0.5; tags.append('Vol✓')
    if e20 and e50:
        if is_buy and price>e20>e50:    score+=1.0; tags.append('EMA↑')
        elif not is_buy and price<e20<e50: score+=1.0; tags.append('EMA↓')
    if ht:
        if is_buy and ht>0:    score+=0.5; tags.append('MACD+')
        elif not is_buy and ht<0: score+=0.5; tags.append('MACD-')
    if is_buy and ri<35:    score+=1.0; tags.append(f'RSI{round(ri)}')
    elif is_buy and ri<50:  score+=0.5; tags.append(f'RSI{round(ri)}')
    elif not is_buy and ri>65: score+=1.0; tags.append(f'RSI{round(ri)}')
    elif not is_buy and ri>50: score+=0.5; tags.append(f'RSI{round(ri)}')
    if is_buy  and weekly=='bullish': score+=0.5; tags.append('W:Bull')
    elif not is_buy and weekly=='bearish': score+=0.5; tags.append('W:Bear')
    elif (is_buy and weekly=='bearish') or (not is_buy and weekly=='bullish'): score-=1.5
    if is_buy  and daily=='bullish': score+=0.5; tags.append('D:Bull')
    elif not is_buy and daily=='bearish': score+=0.5; tags.append('D:Bear')
    if sess in ('London','New York'): score+=0.5; tags.append('Sess✓') if 'Sess✓' not in tags else None
    step=25.0; nr=round(price/step)*step; dist=abs(price-nr)/at
    if dist<0.6: score+=0.5; tags.append(f'${int(nr)}')
    # Anti-trend
    r6=kl[max(0,i-6):i+1]
    bc=sum(1 for x in r6 if x['c']<x['o'])
    if is_buy and bc>=5:       score-=2.0
    if not is_buy and (6-bc)>=5: score-=2.0

    score = round(max(0, min(10, score)), 1)
    if score < GOLD_MIN_SCORE: return None

    # Weekend block
    if sess == 'Weekend' and score < 8.0: return None

    # ── SL PLACEMENT ─────────────────────────────
    if wick_sl is not None:
        sl_p = wick_sl - at*0.10 if is_buy else wick_sl + at*0.10
    else:
        sl_p = price - at*1.5 if is_buy else price + at*1.5
    # Gold max SL: 0.8% ($16 at $2000)
    max_risk = price * 0.008
    if is_buy  and (price-sl_p)>max_risk: sl_p=price-max_risk
    if not is_buy and (sl_p-price)>max_risk: sl_p=price+max_risk
    risk = abs(price-sl_p)
    if risk <= 0: return None

    rr_mult = 2.8 if setup=='SWEEP_OB' else 2.5
    tp  = price+risk*rr_mult if is_buy else price-risk*rr_mult
    tp1 = price+risk*2.0     if is_buy else price-risk*2.0
    tp3 = price+risk*3.0     if is_buy else price-risk*3.0
    rr  = round(abs(tp-price)/risk, 1)
    if rr < 2.0: return None

    risk_pct = round(abs(price-sl_p)/price*100, 2)
    rew_pct  = round(abs(tp-price)/price*100, 2)
    conf     = min(96, round(score*8 + min(rr,3)*2.5))

    setup_names = {
        'SWEEP_OB': '⚡ Gold Liq Sweep + OB Retest',
        'ICT_KZ':   '🕯️ Gold ICT Kill Zone Break',
        'CHOCH':    '🔄 Gold CHoCH Reversal',
        'EMA_PULL': '📊 Gold EMA Pullback',
    }

    return {
        'sym':      GOLD_SYMBOL,
        'name':     GOLD_NAME,
        'pair':     'XAU/USD',
        'dir':      'BUY' if is_buy else 'SELL',
        'setup':    setup,
        'setup_name': setup_names.get(setup, setup),
        'score':    score,
        'conf':     conf,
        'rr':       rr,
        'price':    price,
        'entry':    price,
        'sl':       round(sl_p, 2),
        'tp':       round(tp, 2),
        'tp1':      round(tp1, 2),
        'tp3':      round(tp3, 2),
        'risk_pct': risk_pct,
        'rew_pct':  rew_pct,
        'tags':     tags,
        'weekly':   weekly,
        'daily':    daily,
        'session':  sess,
        'ob':       ob_hit,
        'wick_sl':  round(wick_sl, 2) if wick_sl else None,
        'rsi_val':  round(ri, 1),
        'is_gold':  True,
    }

def build_gold_signal_msg(sig):
    """Gold-specific Telegram message with trade basis"""
    is_buy = sig['dir'] == 'BUY'
    setup_tips = {
        'SWEEP_OB': (
            "📌 <i>Institutions swept retail stops then reversed.\n"
            "Entry on OB retest — SL below swept wick.</i>"
        ),
        'ICT_KZ': (
            "📌 <i>ICT Kill Zone — price broke out of Asia range\n"
            "at London/NY session open with volume. Trend continuation.</i>"
        ),
        'CHOCH': (
            "📌 <i>Change of Character — gold structure shifted.\n"
            "First entry on new direction. Tight SL at last swing.</i>"
        ),
        'EMA_PULL': (
            "📌 <i>Gold pulled back to EMA20 in strong trend.\n"
            "Classic trend-following re-entry.</i>"
        ),
    }

    why = {
        'SWEEP_OB': (
            f"  1️⃣ Equal lows swept at <code>{fp(sig.get('wick_sl', sig['sl']))}</code>\n"
            f"  2️⃣ Gold closed back above — stop hunt complete\n"
            f"  3️⃣ OB zone: <code>{fp(sig['ob']['bot']) if sig.get('ob') else '—'}</code> – <code>{fp(sig['ob']['top']) if sig.get('ob') else '—'}</code>\n"
            f"  4️⃣ Retesting OB — institutions defending\n"
            f"  5️⃣ SL just below swept wick (tight)"
        ),
        'ICT_KZ': (
            f"  1️⃣ Gold consolidated during Asian session\n"
            f"  2️⃣ {sig.get('session','London')} open broke Asia range with volume\n"
            f"  3️⃣ MACD + EMA confirm direction\n"
            f"  4️⃣ SL below/above Asia range"
        ),
        'CHOCH': (
            f"  1️⃣ Gold was in {'downtrend' if is_buy else 'uptrend'}\n"
            f"  2️⃣ Structure break — CHoCH confirmed\n"
            f"  3️⃣ First entry on new {'bullish' if is_buy else 'bearish'} structure\n"
            f"  4️⃣ SL at last swing {'low' if is_buy else 'high'}"
        ),
        'EMA_PULL': (
            f"  1️⃣ Gold in strong {'uptrend' if is_buy else 'downtrend'}\n"
            f"  2️⃣ Pulled back to EMA20 {'from above' if is_buy else 'from below'}\n"
            f"  3️⃣ RSI reset to neutral {'from overbought' if not is_buy else 'from oversold'}\n"
            f"  4️⃣ Trend continuation — EMAs still aligned"
        ),
    }

    rr_mult = sig['rr']
    return '\n'.join(filter(None, [
        f"{'🟡' if is_buy else '🔴'} <b>GOLD {'BUY' if is_buy else 'SELL'} — XAU/USD</b>",
        f"{'⚡🕯️🔄📊'.split()[['SWEEP_OB','ICT_KZ','CHOCH','EMA_PULL'].index(sig['setup'])] if sig['setup'] in ['SWEEP_OB','ICT_KZ','CHOCH','EMA_PULL'] else '📡'} <b>{sig['setup_name']}</b>",
        f"",
        setup_tips.get(sig['setup'], ''),
        f"",
        f"📖 <b>Why this trade:</b>",
        why.get(sig['setup'], '  SMC confluence setup'),
        f"",
        f"💰 <b>Trade Levels</b>",
        f"  Entry:  <code>{fp(sig['price'])}</code>",
        f"  SL:     <code>{fp(sig['sl'])}</code>  <i>(-{sig['risk_pct']}%) ≈ ${round(abs(sig['price']-sig['sl']))}</i>",
        f"  TP1:    <code>{fp(sig['tp1'])}</code>  <i>(1:2 — close 50%, move SL to entry)</i>",
        f"  TP2:    <code>{fp(sig['tp'])}</code>   <i>(1:{rr_mult} — close 30%)</i>",
        f"  TP3:    <code>{fp(sig['tp3'])}</code>  <i>(1:3 — let runner go)</i>",
        f"",
        f"📊 <b>Score: {sig['score']}/10  |  Conf: {sig['conf']}%  |  R:R 1:{rr_mult}</b>",
        f"  Tags: {esc(' · '.join(sig['tags']))}",
        f"  Weekly: {esc(sig.get('weekly','—'))}  |  Daily: {esc(sig.get('daily','—'))}  |  RSI: {sig.get('rsi_val','—')}",
        f"  Session: {sig.get('session','—')}",
        sig['ob'] and f"  OB Zone: {fp(sig['ob']['bot'])} – {fp(sig['ob']['top'])}",
        f"",
        f"⚠️ <i>Gold intraday — respect session times. Not financial advice.</i>",
        f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC  |  🏅 <b>SMC Gold Engine</b>",
    ]))




# ═══ JOURNAL ═════════════════════════════════════════════════════
JOURNAL_FILE = os.environ.get('JOURNAL_FILE', '/app/smc_journal.json')

def load_journal():
    if Path(JOURNAL_FILE).exists():
        try:
            with open(JOURNAL_FILE) as f: return json.load(f)
        except: pass
    return {'trades':[],'open':{},'signals':[],'stats':{},'created':datetime.now(timezone.utc).isoformat()}

def save_journal(j):
    try:
        with open(JOURNAL_FILE,'w') as f: json.dump(j,f,indent=2)
    except Exception as e:
        log.debug(f"Journal save error: {e}")

def journal_log_signal(sig, pair):
    try:
        j = load_journal()
        entry = {
            'id':         f"{pair['sym']}_{int(time.time())}",
            'sym':        pair['sym'],
            'setup':      sig['setup'],
            'setup_name': sig['name'],
            'dir':        sig['dir'],
            'score':      sig['score'],
            'entry':      sig['price'],
            'sl':         sig['sl'],
            'tp1':        sig['tp1'],
            'tp2':        sig['tp'],
            'tp3':        sig['tp3'],
            'rr':         sig['rr'],
            'risk_pct':   sig['risk_pct'],
            'tags':       sig['tags'],
            'weekly':     sig.get('weekly','—'),
            'rsi':        sig.get('rsi_val',0),
            'time':       datetime.now(timezone.utc).isoformat(),
            'status':     'open',
            'exit_price': None,
            'exit_time':  None,
            'pnl':        None,
        }
        j['signals'].append(entry)
        j['open'][pair['sym']] = entry['id']
        save_journal(j)
        log.info(f"  📓 Journal: logged {pair['sym']} {sig['dir']} {sig['setup']}")
    except Exception as e:
        log.debug(f"Journal log error: {e}")

def journal_close_trade(sym, result, exit_price):
    try:
        j = load_journal()
        trade_id = j['open'].get(sym)
        if not trade_id: return
        sig = next((s for s in j['signals'] if s['id']==trade_id), None)
        if not sig: return
        is_buy = sig['dir']=='BUY'
        pnl = ((exit_price-sig['entry'])/sig['entry']*100) if is_buy else ((sig['entry']-exit_price)/sig['entry']*100)
        sig.update({'status':result,'exit_price':exit_price,
                    'exit_time':datetime.now(timezone.utc).isoformat(),'pnl':round(pnl,3)})
        j['trades'].append(sig)
        del j['open'][sym]
        # Update stats
        s = j['stats'].setdefault(sig['setup'],{'wins':0,'losses':0,'be':0,'total':0,'total_pnl':0})
        s['total']+=1; s['total_pnl']=round(s['total_pnl']+pnl,3)
        if result=='win': s['wins']+=1
        elif result=='loss': s['losses']+=1
        else: s['be']+=1
        save_journal(j)
    except Exception as e:
        log.debug(f"Journal close error: {e}")

def journal_stats_report():
    try:
        j = load_journal()
        trades=[t for t in j['signals'] if t['status']!='open']
        if not trades: return "📊 No completed trades yet."
        wins=[t for t in trades if t['status']=='win']
        losses=[t for t in trades if t['status']=='loss']
        be=[t for t in trades if t['status']=='be']
        total=len(trades); wr=len(wins)/total*100 if total else 0
        total_pnl=sum(t.get('pnl',0) or 0 for t in trades)
        avg_win=sum(t.get('pnl',0) or 0 for t in wins)/len(wins) if wins else 0
        avg_loss=sum(abs(t.get('pnl',0) or 0) for t in losses)/len(losses) if losses else 0
        pf=(len(wins)*avg_win)/(len(losses)*avg_loss) if losses and avg_loss>0 else 0
        lines=[
            "📊 <b>SMC Journal — Performance Report</b>","",
            f"📈 Total trades:   {total}",
            f"✅ Wins:           {len(wins)} ({wr:.1f}%)",
            f"❌ Losses:         {len(losses)}",
            f"➡️ Breakeven:      {len(be)}",
            f"💰 Total P&amp;L:  {total_pnl:+.2f}%",
            f"📐 Profit Factor:  {pf:.2f}",
            f"📊 Avg Win:        +{avg_win:.2f}%",
            f"📊 Avg Loss:       -{avg_loss:.2f}%","",
            "<b>Per Setup:</b>",
        ]
        for setup,s in j['stats'].items():
            if not s['total']: continue
            wr_s=s['wins']/s['total']*100
            e={'SWEEP_OB':'⚡','HTF_CONFLUENCE':'📊','CHOCH':'🔄','BOS':'📈'}.get(setup,'📡')
            lines.append(f"  {e} {setup}: {s['total']} | WR:{wr_s:.0f}% | PnL:{s['total_pnl']:+.1f}%")
        if trades:
            best=max(trades,key=lambda t:t.get('pnl',0) or 0)
            worst=min(trades,key=lambda t:t.get('pnl',0) or 0)
            lines+=["",
                f"🏆 Best:  {best['sym']} +{best.get('pnl',0):.2f}% ({best['setup']})",
                f"💔 Worst: {worst['sym']} {worst.get('pnl',0):.2f}% ({worst['setup']})"]
        if j['open']:
            lines.append(f"\n🔓 Open: {', '.join(j['open'].keys())}")
        lines.append(f"\n⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC  |  📡 SMC Engine Pro v3")
        return '\n'.join(lines)
    except Exception as e:
        return f"Journal error: {e}"

def check_circuit_breaker():
    """Pause trading after 3 consecutive losses"""
    try:
        j = load_journal()
        recent=[t for t in j['signals'] if t['status'] in ('win','loss')][-3:]
        if len(recent)<3: return False
        if all(t['status']=='loss' for t in recent):
            last_time=datetime.fromisoformat(recent[-1]['time']).timestamp()
            hours_since=(time.time()-last_time)/3600
            if hours_since<4:
                log.warning(f"⚠️ Circuit breaker: 3 losses in a row — pausing {4-hours_since:.1f}h")
                return True
    except: pass
    return False

CG = 'https://api.coingecko.com/api/v3'


# Override file paths for gold
import os as _os
_os.environ.setdefault('LEARN_FILE',       '/app/gold_learning.json')
_os.environ.setdefault('DEEP_LEARN_FILE',  '/app/gold_deep.json')
_os.environ.setdefault('JOURNAL_FILE',     '/app/gold_journal.json')
LEARN_FILE      = _os.environ['LEARN_FILE']
DEEP_LEARN_FILE = _os.environ['DEEP_LEARN_FILE']
JOURNAL_FILE    = _os.environ['JOURNAL_FILE']

# Separate scalp ML database
SCALP_LEARN_FILE = os.environ.get('SCALP_LEARN_FILE', '/app/gold_scalp_ml.json')

def _scalp_load():
    try:
        if Path(SCALP_LEARN_FILE).exists():
            with open(SCALP_LEARN_FILE) as f: return json.load(f)
    except: pass
    return {
        'version': 1,
        'trades': [],
        'total': 0, 'wins': 0, 'losses': 0, 'be': 0,
        # ML feature weights — adjusted automatically after each trade
        'weights': {
            'session_london':    1.0,  # London session multiplier
            'session_ny':        1.0,  # NY session multiplier
            'session_asian':     0.6,  # Asian = worse for scalps
            'rsi_oversold':      1.2,  # RSI < 35 for buys
            'rsi_neutral':       1.0,  # RSI 35-55
            'rsi_overbought':    1.2,  # RSI > 65 for sells
            'vol_high':          1.2,  # volume > 1.5x avg
            'vol_normal':        1.0,  # volume 1.0-1.5x avg
            'ema_aligned':       1.1,  # EMA stack aligned
            'ob_present':        1.2,  # OB zone confirmed
            'sweep_present':     1.2,  # sweep detected
            'macd_aligned':      1.1,  # MACD confirms
            'weekly_aligned':    1.1,  # weekly bias matches
            'weekly_against':    0.7,  # weekly bias against
            'base_score_8':      1.0,  # score 8.0-8.4
            'base_score_85':     1.1,  # score 8.5-8.9
            'base_score_9':      1.3,  # score 9.0+
        },
        'insights': [],   # patterns discovered
        'last_update': None,
        'update_count': 0,
    }

def _scalp_save(db):
    try:
        with open(SCALP_LEARN_FILE, 'w') as f: json.dump(db, f, indent=2)
    except Exception as e:
        log.debug(f"Scalp ML save: {e}")

def scalp_adjusted_score(sig, session):
    """
    Apply learned ML weights to raw signal score.
    Returns adjusted confidence score 0-100.
    """
    db = _scalp_load()
    w  = db['weights']
    base = sig['score']

    # Session weight
    sess_w = w.get(f'session_{session.lower().replace(" ","_")}',
                   w.get('session_london', 1.0))

    # RSI weight
    ri = sig.get('rsi_val', 50)
    is_buy = sig['dir'] == 'BUY'
    if is_buy:
        rsi_w = w['rsi_oversold'] if ri<35 else (w['rsi_neutral'] if ri<55 else 0.8)
    else:
        rsi_w = w['rsi_overbought'] if ri>65 else (w['rsi_neutral'] if ri>45 else 0.8)

    # Feature weights
    tags = sig.get('tags', [])
    vol_w   = w['vol_high']   if any('Vol++' in t for t in tags) else w['vol_normal']
    ema_w   = w['ema_aligned'] if any('EMA' in t for t in tags) else 1.0
    ob_w    = w['ob_present']  if 'OB_Retest' in tags else 1.0
    sw_w    = w['sweep_present'] if any('Sweep' in t for t in tags) else 1.0
    macd_w  = w['macd_aligned'] if any('MACD' in t for t in tags) else 1.0

    # Weekly weight
    weekly = sig.get('weekly', 'neutral')
    wk_w = w['weekly_aligned'] if (
        (is_buy and weekly=='bullish') or (not is_buy and weekly=='bearish')
    ) else (w['weekly_against'] if (
        (is_buy and weekly=='bearish') or (not is_buy and weekly=='bullish')
    ) else 1.0)

    # Score tier weight
    sc_w = w['base_score_9'] if base>=9 else (w['base_score_85'] if base>=8.5 else w['base_score_8'])

    # Combined adjusted score
    multiplier = (sess_w * rsi_w * vol_w * ema_w * ob_w * sw_w * macd_w * wk_w * sc_w) ** (1/9)
    adjusted = round(min(100, base * multiplier * 10), 1)

    db_stats = db['weights']
    log.debug(f"Scalp score: raw={base} sess_w={sess_w:.2f} adj={adjusted}")
    return adjusted

def scalp_record_trade(sig, session, result, pnl, kl=None):
    """
    Record scalp trade result and update ML weights.
    Called after every scalp TP/SL hit.
    """
    db = _scalp_load()
    tags = sig.get('tags', [])
    ri = sig.get('rsi_val', 50)
    is_buy = sig['dir'] == 'BUY'
    weekly = sig.get('weekly', 'neutral')

    record = {
        'time':     datetime.now(timezone.utc).isoformat(),
        'sym':      sig.get('sym', 'XAU'),
        'dir':      sig['dir'],
        'setup':    sig.get('setup', ''),
        'score':    sig.get('score', 0),
        'session':  session,
        'rsi':      ri,
        'weekly':   weekly,
        'tags':     tags,
        'result':   result,  # 'win' | 'loss' | 'be'
        'pnl':      pnl,
    }
    db['trades'].append(record)
    db['total']  += 1
    if result=='win':    db['wins']   += 1
    elif result=='loss': db['losses'] += 1
    else:                db['be']     += 1

    # ── ML WEIGHT UPDATE ───────────────────────────────────────────
    # Update after every 5 trades (enough data to be meaningful)
    if db['total'] >= 5 and db['total'] % 5 == 0:
        _update_scalp_weights(db)

    _scalp_save(db)
    return record

def _update_scalp_weights(db):
    """
    Bayesian-style weight update.
    Good condition → increase weight
    Bad condition  → decrease weight
    Learning rate = 0.12 (conservative)
    """
    trades = db['trades']
    if len(trades) < 5: return

    lr = 0.12  # learning rate
    w  = db['weights']
    changes = []

    def wr_for(condition_fn):
        """Win rate for trades where condition is true"""
        subset = [t for t in trades if condition_fn(t)]
        if len(subset) < 3: return None
        wins = sum(1 for t in subset if t['result']=='win')
        return wins / len(subset), len(subset)

    # Session learning
    for sess_key, condition in [
        ('session_london',  lambda t: t['session']=='London'),
        ('session_ny',      lambda t: t['session']=='New York'),
        ('session_asian',   lambda t: t['session']=='Asian'),
    ]:
        result = wr_for(condition)
        if result:
            wr, n = result
            target = 0.5 + wr * 1.0  # map WR to multiplier
            old = w[sess_key]
            w[sess_key] = round(max(0.3, min(1.8, old + lr*(target-old))), 3)
            if abs(w[sess_key]-old) > 0.02:
                changes.append(f"session_{sess_key}: {old:.2f}→{w[sess_key]:.2f} (WR:{wr:.0%} n={n})")

    # RSI zone learning
    for rsi_key, condition in [
        ('rsi_oversold',   lambda t: (t['dir']=='BUY' and t['rsi']<35) or (t['dir']=='SELL' and t['rsi']>65)),
        ('rsi_neutral',    lambda t: 35<=t['rsi']<=65),
    ]:
        result = wr_for(condition)
        if result:
            wr, n = result
            target = 0.5 + wr
            old = w[rsi_key]
            w[rsi_key] = round(max(0.5, min(2.0, old + lr*(target-old))), 3)
            if abs(w[rsi_key]-old) > 0.02:
                changes.append(f"{rsi_key}: {old:.2f}→{w[rsi_key]:.2f} (WR:{wr:.0%})")

    # Volume learning
    result_hvol = wr_for(lambda t: any('Vol++' in x for x in t.get('tags',[])))
    if result_hvol:
        wr, n = result_hvol
        target = 0.5 + wr
        old = w['vol_high']
        w['vol_high'] = round(max(0.5, min(2.0, old + lr*(target-old))), 3)
        if abs(w['vol_high']-old) > 0.02:
            changes.append(f"vol_high: {old:.2f}→{w['vol_high']:.2f} (WR:{wr:.0%})")

    # OB learning
    result_ob = wr_for(lambda t: 'OB_Retest' in t.get('tags',[]))
    if result_ob:
        wr, n = result_ob
        target = 0.5 + wr
        old = w['ob_present']
        w['ob_present'] = round(max(0.5, min(2.0, old + lr*(target-old))), 3)
        if abs(w['ob_present']-old) > 0.02:
            changes.append(f"ob_present: {old:.2f}→{w['ob_present']:.2f} (WR:{wr:.0%})")

    # Sweep learning
    result_sw = wr_for(lambda t: any('Sweep' in x for x in t.get('tags',[])))
    if result_sw:
        wr, n = result_sw
        target = 0.5 + wr
        old = w['sweep_present']
        w['sweep_present'] = round(max(0.5, min(2.0, old + lr*(target-old))), 3)
        if abs(w['sweep_present']-old) > 0.02:
            changes.append(f"sweep_present: {old:.2f}→{w['sweep_present']:.2f} (WR:{wr:.0%})")

    # Score tier learning
    for sc_key, condition in [
        ('base_score_9',   lambda t: t['score']>=9.0),
        ('base_score_85',  lambda t: 8.5<=t['score']<9.0),
        ('base_score_8',   lambda t: 8.0<=t['score']<8.5),
    ]:
        result = wr_for(condition)
        if result:
            wr, n = result
            target = 0.5 + wr
            old = w[sc_key]
            w[sc_key] = round(max(0.5, min(2.0, old + lr*(target-old))), 3)
            if abs(w[sc_key]-old) > 0.02:
                changes.append(f"{sc_key}: {old:.2f}→{w[sc_key]:.2f} (WR:{wr:.0%})")

    db['update_count'] += 1
    db['last_update']   = datetime.now(timezone.utc).isoformat()

    if changes:
        insight = {
            'time':    datetime.now(timezone.utc).isoformat(),
            'trades':  db['total'],
            'changes': changes,
        }
        db['insights'].append(insight)
        log.info(f"Scalp ML updated: {len(changes)} weight changes after {db['total']} trades")

def scalp_ml_report():
    db = _scalp_load()
    total = db['total']
    if total == 0: return "No scalp trades yet."
    wins = db['wins']; losses = db['losses']; be = db['be']
    wr = wins/total*100 if total else 0
    w = db['weights']
    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')
    parts = [
        "<b>Scalp ML Learning Report</b>",
        "Based on " + str(total) + " real scalp trades",
        "",
        "Wins: " + str(wins) + "  Losses: " + str(losses) + "  BE: " + str(be),
        "Win rate: " + str(round(wr,1)) + "%",
        "",
        "<b>Learned Weights:</b>",
        "  London:      " + str(w['session_london']) + "x",
        "  New York:    " + str(w['session_ny']) + "x",
        "  Asian:       " + str(w['session_asian']) + "x",
        "  RSI extreme: " + str(w['rsi_oversold']) + "x",
        "  Volume high: " + str(w['vol_high']) + "x",
        "  OB zone:     " + str(w['ob_present']) + "x",
        "  Sweep:       " + str(w['sweep_present']) + "x",
        "  Score 9+:    " + str(w['base_score_9']) + "x",
    ]
    if db['insights']:
        last = db['insights'][-1]
        parts.append("")
        parts.append("<b>Last ML update (" + last['time'][:10] + "):</b>")
        for ch in last['changes'][:4]:
            parts.append("  " + ch)
    parts.append("")
    parts.append(now_str + " UTC | SMC Gold Scalp ML")
    return '\n'.join(parts)

def _scalp_load():
    try:
        if Path(SCALP_LEARN_FILE).exists():
            with open(SCALP_LEARN_FILE) as f: return json.load(f)
    except: pass
    return {
        'version': 1,
        'trades': [],
        'total': 0, 'wins': 0, 'losses': 0, 'be': 0,
        # ML feature weights — adjusted automatically after each trade
        'weights': {
            'session_london':    1.0,  # London session multiplier
            'session_ny':        1.0,  # NY session multiplier
            'session_asian':     0.6,  # Asian = worse for scalps
            'rsi_oversold':      1.2,  # RSI < 35 for buys
            'rsi_neutral':       1.0,  # RSI 35-55
            'rsi_overbought':    1.2,  # RSI > 65 for sells
            'vol_high':          1.2,  # volume > 1.5x avg
            'vol_normal':        1.0,  # volume 1.0-1.5x avg
            'ema_aligned':       1.1,  # EMA stack aligned
            'ob_present':        1.2,  # OB zone confirmed
            'sweep_present':     1.2,  # sweep detected
            'macd_aligned':      1.1,  # MACD confirms
            'weekly_aligned':    1.1,  # weekly bias matches
            'weekly_against':    0.7,  # weekly bias against
            'base_score_8':      1.0,  # score 8.0-8.4
            'base_score_85':     1.1,  # score 8.5-8.9
            'base_score_9':      1.3,  # score 9.0+
        },
        'insights': [],   # patterns discovered
        'last_update': None,
        'update_count': 0,
    }

def _scalp_save(db):
    try:
        with open(SCALP_LEARN_FILE, 'w') as f: json.dump(db, f, indent=2)
    except Exception as e:
        log.debug(f"Scalp ML save: {e}")

def scalp_adjusted_score(sig, session):
    """
    Apply learned ML weights to raw signal score.
    Returns adjusted confidence score 0-100.
    """
    db = _scalp_load()
    w  = db['weights']
    base = sig['score']

    # Session weight
    sess_w = w.get(f'session_{session.lower().replace(" ","_")}',
                   w.get('session_london', 1.0))

    # RSI weight
    ri = sig.get('rsi_val', 50)
    is_buy = sig['dir'] == 'BUY'
    if is_buy:
        rsi_w = w['rsi_oversold'] if ri<35 else (w['rsi_neutral'] if ri<55 else 0.8)
    else:
        rsi_w = w['rsi_overbought'] if ri>65 else (w['rsi_neutral'] if ri>45 else 0.8)

    # Feature weights
    tags = sig.get('tags', [])
    vol_w   = w['vol_high']   if any('Vol++' in t for t in tags) else w['vol_normal']
    ema_w   = w['ema_aligned'] if any('EMA' in t for t in tags) else 1.0
    ob_w    = w['ob_present']  if 'OB_Retest' in tags else 1.0
    sw_w    = w['sweep_present'] if any('Sweep' in t for t in tags) else 1.0
    macd_w  = w['macd_aligned'] if any('MACD' in t for t in tags) else 1.0

    # Weekly weight
    weekly = sig.get('weekly', 'neutral')
    wk_w = w['weekly_aligned'] if (
        (is_buy and weekly=='bullish') or (not is_buy and weekly=='bearish')
    ) else (w['weekly_against'] if (
        (is_buy and weekly=='bearish') or (not is_buy and weekly=='bullish')
    ) else 1.0)

    # Score tier weight
    sc_w = w['base_score_9'] if base>=9 else (w['base_score_85'] if base>=8.5 else w['base_score_8'])

    # Combined adjusted score
    multiplier = (sess_w * rsi_w * vol_w * ema_w * ob_w * sw_w * macd_w * wk_w * sc_w) ** (1/9)
    adjusted = round(min(100, base * multiplier * 10), 1)

    db_stats = db['weights']
    log.debug(f"Scalp score: raw={base} sess_w={sess_w:.2f} adj={adjusted}")
    return adjusted

def scalp_record_trade(sig, session, result, pnl, kl=None):
    """
    Record scalp trade result and update ML weights.
    Called after every scalp TP/SL hit.
    """
    db = _scalp_load()
    tags = sig.get('tags', [])
    ri = sig.get('rsi_val', 50)
    is_buy = sig['dir'] == 'BUY'
    weekly = sig.get('weekly', 'neutral')

    record = {
        'time':     datetime.now(timezone.utc).isoformat(),
        'sym':      sig.get('sym', 'XAU'),
        'dir':      sig['dir'],
        'setup':    sig.get('setup', ''),
        'score':    sig.get('score', 0),
        'session':  session,
        'rsi':      ri,
        'weekly':   weekly,
        'tags':     tags,
        'result':   result,  # 'win' | 'loss' | 'be'
        'pnl':      pnl,
    }
    db['trades'].append(record)
    db['total']  += 1
    if result=='win':    db['wins']   += 1
    elif result=='loss': db['losses'] += 1
    else:                db['be']     += 1

    # ── ML WEIGHT UPDATE ───────────────────────────────────────────
    # Update after every 5 trades (enough data to be meaningful)
    if db['total'] >= 5 and db['total'] % 5 == 0:
        _update_scalp_weights(db)

    _scalp_save(db)
    return record

def _update_scalp_weights(db):
    """
    Bayesian-style weight update.
    Good condition → increase weight
    Bad condition  → decrease weight
    Learning rate = 0.12 (conservative)
    """
    trades = db['trades']
    if len(trades) < 5: return

    lr = 0.12  # learning rate
    w  = db['weights']
    changes = []

    def wr_for(condition_fn):
        """Win rate for trades where condition is true"""
        subset = [t for t in trades if condition_fn(t)]
        if len(subset) < 3: return None
        wins = sum(1 for t in subset if t['result']=='win')
        return wins / len(subset), len(subset)

    # Session learning
    for sess_key, condition in [
        ('session_london',  lambda t: t['session']=='London'),
        ('session_ny',      lambda t: t['session']=='New York'),
        ('session_asian',   lambda t: t['session']=='Asian'),
    ]:
        result = wr_for(condition)
        if result:
            wr, n = result
            target = 0.5 + wr * 1.0  # map WR to multiplier
            old = w[sess_key]
            w[sess_key] = round(max(0.3, min(1.8, old + lr*(target-old))), 3)
            if abs(w[sess_key]-old) > 0.02:
                changes.append(f"session_{sess_key}: {old:.2f}→{w[sess_key]:.2f} (WR:{wr:.0%} n={n})")

    # RSI zone learning
    for rsi_key, condition in [
        ('rsi_oversold',   lambda t: (t['dir']=='BUY' and t['rsi']<35) or (t['dir']=='SELL' and t['rsi']>65)),
        ('rsi_neutral',    lambda t: 35<=t['rsi']<=65),
    ]:
        result = wr_for(condition)
        if result:
            wr, n = result
            target = 0.5 + wr
            old = w[rsi_key]
            w[rsi_key] = round(max(0.5, min(2.0, old + lr*(target-old))), 3)
            if abs(w[rsi_key]-old) > 0.02:
                changes.append(f"{rsi_key}: {old:.2f}→{w[rsi_key]:.2f} (WR:{wr:.0%})")

    # Volume learning
    result_hvol = wr_for(lambda t: any('Vol++' in x for x in t.get('tags',[])))
    if result_hvol:
        wr, n = result_hvol
        target = 0.5 + wr
        old = w['vol_high']
        w['vol_high'] = round(max(0.5, min(2.0, old + lr*(target-old))), 3)
        if abs(w['vol_high']-old) > 0.02:
            changes.append(f"vol_high: {old:.2f}→{w['vol_high']:.2f} (WR:{wr:.0%})")

    # OB learning
    result_ob = wr_for(lambda t: 'OB_Retest' in t.get('tags',[]))
    if result_ob:
        wr, n = result_ob
        target = 0.5 + wr
        old = w['ob_present']
        w['ob_present'] = round(max(0.5, min(2.0, old + lr*(target-old))), 3)
        if abs(w['ob_present']-old) > 0.02:
            changes.append(f"ob_present: {old:.2f}→{w['ob_present']:.2f} (WR:{wr:.0%})")

    # Sweep learning
    result_sw = wr_for(lambda t: any('Sweep' in x for x in t.get('tags',[])))
    if result_sw:
        wr, n = result_sw
        target = 0.5 + wr
        old = w['sweep_present']
        w['sweep_present'] = round(max(0.5, min(2.0, old + lr*(target-old))), 3)
        if abs(w['sweep_present']-old) > 0.02:
            changes.append(f"sweep_present: {old:.2f}→{w['sweep_present']:.2f} (WR:{wr:.0%})")

    # Score tier learning
    for sc_key, condition in [
        ('base_score_9',   lambda t: t['score']>=9.0),
        ('base_score_85',  lambda t: 8.5<=t['score']<9.0),
        ('base_score_8',   lambda t: 8.0<=t['score']<8.5),
    ]:
        result = wr_for(condition)
        if result:
            wr, n = result
            target = 0.5 + wr
            old = w[sc_key]
            w[sc_key] = round(max(0.5, min(2.0, old + lr*(target-old))), 3)
            if abs(w[sc_key]-old) > 0.02:
                changes.append(f"{sc_key}: {old:.2f}→{w[sc_key]:.2f} (WR:{wr:.0%})")

    db['update_count'] += 1
    db['last_update']   = datetime.now(timezone.utc).isoformat()

    if changes:
        insight = {
            'time':    datetime.now(timezone.utc).isoformat(),
            'trades':  db['total'],
            'changes': changes,
        }
        db['insights'].append(insight)
        log.info(f"Scalp ML updated: {len(changes)} weight changes after {db['total']} trades")

def scalp_ml_report():
    """Telegram report of scalp ML learning"""
    db = _scalp_load()
    total = db['total']; w = db['total']
    if total == 0: return "📊 No scalp trades yet."

    wins = db['wins']; losses = db['losses']; be = db['be']
    wr = wins/total*100 if total else 0
    weights = db['weights']

    lines = [
        "🤖 <b>Scalp ML Learning Report</b>",
        f"Based on {total} real scalp trades",
        f"✅ Wins: {wins}  ❌ Losses: {losses}  ➡️ BE: {be}",
        f"Win rate: {wr:.1f}%",
        "<b>📊 Learned Weights:</b>",
        f"  London:    {weights['session_london']:.2f}x",
        f"  New York:  {weights['session_ny']:.2f}x",
        f"  Asian:     {weights['session_asian']:.2f}x",
        f"  RSI extreme: {weights['rsi_oversold']:.2f}x",
        f"  Volume high: {weights['vol_high']:.2f}x",
        f"  OB zone:     {weights['ob_present']:.2f}x",
        f"  Sweep:       {weights['sweep_present']:.2f}x",
        f"  Score 9+:    {weights['base_score_9']:.2f}x",
    ]
    if db['insights']:
        last = db['insights'][-1]
        lines.append(f"<b>Last update ({last['time'][:10]}):</b>")
        for ch in last['changes'][:4]:
            lines.append(f"  {ch}")

    lines.append(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M') + ' UTC')
    lines.append("🏅 <b>SMC Gold Scalp ML</b>")
    return '\n'.join(lines)

# ═══ INDICATORS ══════════════════════════════════════════════════
def ema(c, p):
    if len(c) < p: return [None]*len(c)
    k = 2/(p+1); r = [None]*(p-1)
    s = sum(c[:p])/p; r.append(s); pv = s
    for i in range(p, len(c)):
        pv = c[i]*k + pv*(1-k); r.append(pv)
    return r

def rsi(c, p=14):
    if len(c) < p+1: return [None]*len(c)
    r = [None]*p; g = l = 0.0
    for i in range(1, p+1):
        d = c[i]-c[i-1]
        if d > 0: g += d
        else: l += abs(d)
    ag, al = g/p, l/p
    r.append(100 if al==0 else 100-100/(1+ag/al))
    for i in range(p+1, len(c)):
        d = c[i]-c[i-1]
        ag = (ag*(p-1)+(d if d>0 else 0))/p
        al = (al*(p-1)+(abs(d) if d<0 else 0))/p
        r.append(100 if al==0 else 100-100/(1+ag/al))
    return r

def macd_hist(c):
    e12 = ema(c,12); e26 = ema(c,26)
    ln = [e12[i]-e26[i] if e12[i] and e26[i] else None for i in range(len(c))]
    vl = [v for v in ln if v is not None]
    if len(vl) < 9: return [None]*len(c)
    sr = ema(vl, 9); sg = [None]*len(c); si = 0
    for i in range(len(c)):
        if ln[i] is not None:
            sg[i] = sr[si] if si < len(sr) else None; si += 1
    return [ln[i]-sg[i] if ln[i] is not None and sg[i] is not None else None
            for i in range(len(c))]

def calc_atr(kl, p=14):
    tr = [None]+[max(kl[i]['h']-kl[i]['l'],
                     abs(kl[i]['h']-kl[i-1]['c']),
                     abs(kl[i]['l']-kl[i-1]['c']))
                 for i in range(1, len(kl))]
    if len(tr) < p+1: return [None]*len(kl)
    r = [None]*p; s = sum(tr[1:p+1])/p; r.append(s); pv = s
    for i in range(p+1, len(tr)):
        pv = (pv*(p-1)+tr[i])/p; r.append(pv)
    return r

def vol_avg(v, p=20):
    r = [None]*p
    for i in range(p, len(v)): r.append(sum(v[i-p:i])/p)
    return r

# ── IMPROVEMENT 1: SESSION FILTER ──────────────
def in_session(hour):
    """London 07-12 UTC | New York 13-18 UTC"""
    return 7 <= hour <= 12 or 13 <= hour <= 18

# ── IMPROVEMENT 2+3: BIAS HELPERS ──────────────
def calc_bias(kl, i, factor):
    if i < factor*25: return 'neutral'
    htf = [kl[j*factor+factor-1]['c'] for j in range((i+1)//factor)]
    if len(htf) < 25: return 'neutral'
    e20 = ema(htf, 20); e50 = ema(htf, 50); n = len(htf)-1
    if not e20[n] or not e50[n]: return 'neutral'
    if htf[n] > e20[n] > e50[n]: return 'bullish'
    if htf[n] < e20[n] < e50[n]: return 'bearish'
    return 'neutral'

def btc_gate(kl_btc, i, direction):
    """IMPROVEMENT 3: Block altcoin signals against BTC trend"""
    if not kl_btc: return True
    end = min(i, len(kl_btc)-1)
    c = [k['c'] for k in kl_btc[:end+1]]
    e20 = ema(c, 20); e50 = ema(c, 50)
    if not e20[end] or not e50[end]: return True
    if c[end] < e20[end] < e50[end] and direction == 'BUY':  return False
    if c[end] > e20[end] > e50[end] and direction == 'SELL': return False
    return True

def choppy(atr_a, i, thresh=0.40):
    r = [a for a in atr_a[max(0,i-20):i] if a is not None]
    return not r or (atr_a[i] < np.mean(r)*thresh if atr_a[i] else True)

def swings(kl, lb=5):
    sh = []; sl = []
    for i in range(lb, len(kl)-lb):
        if all(kl[i]['h'] >= kl[j]['h'] for j in range(i-lb, i+lb+1) if j != i):
            sh.append((i, kl[i]['h']))
        if all(kl[i]['l'] <= kl[j]['l'] for j in range(i-lb, i+lb+1) if j != i):
            sl.append((i, kl[i]['l']))
    return sh, sl

# ── IMPROVEMENT 4: STRUCTURE-BASED SL ──────────
def structure_sl(sh, sl_sw, i, direction, atr_v, swept=None, ob=None,
                  wick_low=None, wick_high=None, setup=None):
    """
    SL placement logic per setup:
    SWEEP_OB  → SL just below the wick low (tight, precise)
    CHOCH     → SL below last swing low (structural)
    BOS       → SL below last swing low (structural)
    HTF       → SL below OB or swing low
    """
    buf = atr_v * 0.10  # small buffer

    if direction == 'BUY':
        if setup == 'SWEEP_OB' and wick_low:
            # Tightest SL: just below the swept wick
            return wick_low - buf
        # Structural SL for other setups
        lvls = []
        if ob:    lvls.append(ob - buf)
        rl = [(idx,p) for idx,p in sl_sw if idx <= i][-2:]
        if rl: lvls.append(min(p for _,p in rl) - buf)
        return min(lvls) if lvls else None
    else:
        if setup == 'SWEEP_OB' and wick_high:
            return wick_high + buf
        lvls = []
        if ob:    lvls.append(ob + buf)
        rh = [(idx,p) for idx,p in sh if idx <= i][-3:]
        if rh: lvls.append(max(p for _,p in rh) + buf)
        return max(lvls) if lvls else None

# ── SIGNAL DETECTION ───────────────────────────

def fetch_gold_candles(limit=200):
    try:
        import urllib.request as _ur
        url = f'{GOLD_YF}?interval=1h&range=30d'
        req = _ur.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        with _ur.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read())
        ch = d['chart']['result'][0]
        ts = ch['timestamp']; q = ch['indicators']['quote'][0]
        kl = []
        for j in range(len(ts)):
            o=q['open'][j]; h=q['high'][j]; l=q['low'][j]; c=q['close'][j]
            v=q.get('volume',[0]*len(ts))[j] or 50.0
            if None in (o,h,l,c): continue
            kl.append({'t':int(ts[j]),'o':float(o),'h':float(h),'l':float(l),'c':float(c),'v':float(v)})
        return kl[-limit:] if len(kl)>=30 else None
    except Exception as e:
        log.warning(f"Gold fetch: {e}"); return None

def fetch_gold_price():
    try:
        import urllib.request as _ur
        url = f'{GOLD_YF}?interval=1m&range=1d'
        req = _ur.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        with _ur.urlopen(req, timeout=8) as resp:
            d = json.loads(resp.read())
        closes = d['chart']['result'][0]['indicators']['quote'][0]['close']
        return float(next(c for c in reversed(closes) if c))
    except: return None

def get_gold_session():
    h=datetime.now(timezone.utc).hour; d=datetime.now(timezone.utc).weekday()
    if d>=5: return 'Weekend'
    if 7<=h<=12: return 'London'
    if 13<=h<=18: return 'New York'
    return 'Asian'

def gold_market_open():
    """
    Gold (XAU/USD) market hours UTC:
    Open:  Sunday 23:00 UTC
    Close: Friday 21:00 UTC
    Closed: Fri 21:00 → Sun 23:00 (weekend)
    """
    now = datetime.now(timezone.utc)
    d   = now.weekday()  # 0=Mon ... 6=Sun
    h   = now.hour

    # Saturday = always closed
    if d == 5: return False, 'Closed (Saturday)'
    # Sunday: closed until 23:00
    if d == 6 and h < 23: return False, f'Closed (Sunday — opens at 23:00 UTC)'
    # Friday: closes at 21:00
    if d == 4 and h >= 21: return False, 'Closed (Friday close)'

    sess = get_gold_session()
    return True, sess

def is_scalp_session():
    """Scalp only during active sessions — London or NY"""
    open_, sess = gold_market_open()
    return open_ and sess in ('London', 'New York')

def htf_bias_fn(kl, i, f=5):
    if i<f*25: return 'neutral'
    htf=[kl[j*f+f-1]['c'] for j in range((i+1)//f)]
    if len(htf)<25: return 'neutral'
    e20=ema(htf,20); e50=ema(htf,50); n=len(htf)-1
    if not e20[n] or not e50[n]: return 'neutral'
    if htf[n]>e20[n]>e50[n]: return 'bullish'
    if htf[n]<e20[n]<e50[n]: return 'bearish'
    return 'neutral'

state = {
    'started':     datetime.now(timezone.utc).isoformat(),
    'last_scan':   'Never',
    'scans_done':  0,
    'alerts_sent': 0,
    'open_trades': {},
    'stats':       {'wins':0,'losses':0,'be':0,'by_setup':{},'paper_count':0}
}
last_fired = {}

# ═══ GOLD SIGNAL ENGINE ═══════════════════════════════════════════════════

# ═══ CRYPTO SCALP ENGINE ════════════════════════════════════
# ════════════════════════════════════════════════════════════════
# CRYPTO SCALP ENGINE
# Uses existing SMC v3 logic — score ≥ 8.5 = scalp quality
# Session only (London+NY), max 3 signals/coin/day
# Self-learning: same scalp ML weights as gold
# ════════════════════════════════════════════════════════════════

def fetch_crypto_candles(pair, limit=200):
    """Fetch 1h candles from Kraken → CoinGecko fallback"""
    try:
        r = requests.get(f'{KR}/OHLC',
            params={'pair': pair['kr'], 'interval': 60},
            timeout=12, headers={'User-Agent':'Mozilla/5.0'})
        d = r.json()
        if not d.get('error'):
            key = next((k for k in d['result'] if k != 'last'), None)
            raw = d['result'].get(key, [])
            if len(raw) > 20:
                return [{'t':int(k[0]),'o':float(k[1]),'h':float(k[2]),
                         'l':float(k[3]),'c':float(k[4]),'v':float(k[6])}
                        for k in raw[-limit:]]
    except: pass
    try:
        r = requests.get(f'{CG}/coins/{pair["cg"]}/ohlc',
            params={'vs_currency':'usd','days':7}, timeout=12)
        raw = r.json()
        if isinstance(raw,list) and len(raw)>10:
            return [{'t':int(k[0]/1000),'o':float(k[1]),'h':float(k[2]),
                     'l':float(k[3]),'c':float(k[4]),'v':50.0}
                    for k in raw[-limit:]]
    except: pass
    return None

def fetch_crypto_price(pair):
    """Get current price for TP/SL monitoring"""
    try:
        r = requests.get(f'{CG}/simple/price',
            params={'ids':pair['cg'],'vs_currencies':'usd'}, timeout=8)
        return float(r.json()[pair['cg']]['usd'])
    except: return None

def fp_crypto(p):
    if not p: return '—'
    if p>=10000: return f'${p:,.0f}'
    if p>=100:   return f'${p:.2f}'
    if p>=1:     return f'${p:.4f}'
    return f'${p:.6f}'

def compute_crypto_scalp(kl, pair):
    """
    SMC v3 engine adapted for scalp:
    - Sweep+OB: score 8-9 (best for scalps, wick-based SL)
    - CHoCH: score 8 (reversal scalp)
    - Session required (London/NY)
    - Anti-trend filter
    - Max SL 0.8%
    Returns signal dict or None.
    """
    if not kl or len(kl) < 60: return None
    n = len(kl); i = n-1
    closes = [k['c'] for k in kl]; vols = [k['v'] for k in kl]
    ri_a  = rsi(closes);    e9_a  = ema(closes, 9)
    e20_a = ema(closes, 20); e50_a = ema(closes, 50)
    ht_a  = macd_hist(closes); at_a = calc_atr(kl); va_a = vol_avg(vols)

    if not all([ri_a[i],e9_a[i],e20_a[i],e50_a[i],at_a[i],va_a[i]]): return None

    price = closes[i]; k = kl[i]
    at = float(at_a[i]); va = float(va_a[i])
    ri_v = float(ri_a[i]); e9 = float(e9_a[i])
    e20  = float(e20_a[i]); e50 = float(e50_a[i]); ht = ht_a[i]

    # Chop filter
    r_ = [x for x in at_a[max(0,i-20):i] if x]
    if not r_ or at < sum(r_)/len(r_)*0.40: return None

    sh_, sl_ = swings(kl[:i+1], 5)
    weekly = htf_bias_fn(kl, i, 21)
    daily  = htf_bias_fn(kl, i, 5)

    is_buy = None; score = 0.0; tags = []; setup = None; wick_sl = None; ob_hit = None

    # ── SETUP 1: Sweep + OB (priority — best scalp setup) ────────
    for li, lvl in [(ix,float(p)) for ix,p in sl_ if ix<i-1 and ix>i-50][-4:]:
        if not(k['l']<lvl<price) or lvl-k['l']<at*0.25 or k['v']<va*1.1: continue
        if daily != 'bullish' or weekly == 'bearish': continue
        if not(25<ri_v<65): continue
        # Anti-trend: skip if 5/6 recent candles are red
        r6 = kl[max(0,i-6):i+1]
        if sum(1 for x in r6 if x['c']<x['o']) >= 5: continue
        ob = None
        for j in range(li-1, max(0,li-12), -1):
            if kl[j]['c']<kl[j]['o'] and (kl[min(j+2,n-1)]['c']-kl[j]['c'])/kl[j]['c']>0.003:
                ob={'top':kl[j]['o'],'bot':kl[j]['l']}; break
        if not ob or not(ob['bot']<=price<=ob['top']*1.005): continue
        is_buy=True; setup='SWEEP_OB'; score+=3.5
        tags+=['Sweep↑','OB_Retest']; wick_sl=k['l']; ob_hit=ob; break

    for hi_, lvl in [(ix,float(p)) for ix,p in sh_ if ix<i-1 and ix>i-50][-4:]:
        if not(k['h']>lvl>price) or k['h']-lvl<at*0.25 or k['v']<va*1.1: continue
        if daily != 'bearish' or weekly == 'bullish': continue
        if not(35<ri_v<75): continue
        r6 = kl[max(0,i-6):i+1]
        if sum(1 for x in r6 if x['c']>x['o']) >= 5: continue
        ob = None
        for j in range(hi_-1, max(0,hi_-12), -1):
            if kl[j]['c']>kl[j]['o'] and (kl[min(j+2,n-1)]['c']-kl[j]['c'])/kl[j]['c']<-0.003:
                ob={'top':kl[j]['h'],'bot':kl[j]['c']}; break
        if not ob or not(ob['bot']*0.995<=price<=ob['top']): continue
        is_buy=False; setup='SWEEP_OB'; score+=3.5
        tags+=['Sweep↓','OB_Retest']; wick_sl=k['h']; ob_hit=ob; break

    # ── SETUP 2: CHoCH Reversal ────────────────────────────────
    if is_buy is None:
        rh = [(ix,float(p)) for ix,p in sh_ if ix<=i][-5:]
        rl = [(ix,float(p)) for ix,p in sl_ if ix<=i][-5:]
        if len(rh)>=3 and len(rl)>=3:
            h2,h1p = rh[-2][1],rh[-3][1]; l2,l1p = rl[-2][1],rl[-3][1]
            vok = k['v'] > va*1.05
            if abs(h2-h1p)/max(h1p,1)>=0.003 and abs(l2-l1p)/max(l1p,1)>=0.003:
                if h2<h1p and l2<l1p and price>h2 and ht and ht>0 and 28<ri_v<62 and vok and weekly!='bearish':
                    is_buy=True; setup='CHOCH'; score+=3.5; tags+=['CHoCH↑']
                elif h2>h1p and l2>l1p and price<l2 and ht and ht<0 and 38<ri_v<72 and vok and weekly!='bullish':
                    is_buy=False; setup='CHOCH'; score+=3.5; tags+=['CHoCH↓']

    if is_buy is None: return None

    # ── CONFLUENCES ────────────────────────────────────────────
    if k['v']>va*1.6:   score+=1.0; tags.append('Vol++')
    elif k['v']>va*1.2: score+=0.5; tags.append('Vol✓')
    if is_buy  and price>e20>e50: score+=1.0; tags.append('EMA↑')
    elif not is_buy and price<e20<e50: score+=1.0; tags.append('EMA↓')
    if ht:
        if is_buy  and ht>0: score+=0.5; tags.append('MACD+')
        elif not is_buy and ht<0: score+=0.5; tags.append('MACD-')
    if is_buy  and ri_v<35: score+=1.0; tags.append(f'RSI{round(ri_v)}')
    elif is_buy  and ri_v<50: score+=0.5; tags.append(f'RSI{round(ri_v)}')
    elif not is_buy and ri_v>65: score+=1.0; tags.append(f'RSI{round(ri_v)}')
    elif not is_buy and ri_v>50: score+=0.5; tags.append(f'RSI{round(ri_v)}')
    if is_buy  and weekly=='bullish': score+=0.5; tags.append('W:Bull')
    elif not is_buy and weekly=='bearish': score+=0.5; tags.append('W:Bear')
    elif (is_buy and weekly=='bearish') or (not is_buy and weekly=='bullish'): score-=1.5
    if is_buy  and daily=='bullish': score+=0.5; tags.append('D:Bull')
    elif not is_buy and daily=='bearish': score+=0.5; tags.append('D:Bear')
    # RSI divergence booster
    lb=12
    rl_div=[(ix,float(p)) for ix,p in sl_ if i-lb<ix<i][-3:]
    if is_buy and len(rl_div)>=2 and ri_a[rl_div[-2][0]] and ri_a[rl_div[-1][0]]:
        if rl_div[-1][1]<rl_div[-2][1] and ri_a[rl_div[-1][0]]>ri_a[rl_div[-2][0]]:
            score+=1.5; tags.append('RSI_Div✓')

    score = round(max(0, min(10, score)), 1)
    if score < CRYPTO_SCALP_MIN_SCORE: return None

    # ── SL: wick-based (tight) ────────────────────────────────
    if wick_sl is not None:
        sl_p = wick_sl - at*0.08 if is_buy else wick_sl + at*0.08
    else:
        rl2 = [(ix,float(p)) for ix,p in sl_ if ix<=i][-2:]
        rh2 = [(ix,float(p)) for ix,p in sh_ if ix<=i][-2:]
        sl_p = (min(p for _,p in rl2)-at*0.08) if (is_buy and rl2) else \
               (max(p for _,p in rh2)+at*0.08) if (not is_buy and rh2) else \
               (price-at*1.5 if is_buy else price+at*1.5)

    # Max SL cap
    if is_buy  and (price-sl_p)>price*CRYPTO_SCALP_MAX_SL: sl_p=price-price*CRYPTO_SCALP_MAX_SL
    if not is_buy and (sl_p-price)>price*CRYPTO_SCALP_MAX_SL: sl_p=price+price*CRYPTO_SCALP_MAX_SL
    risk = abs(price-sl_p)
    if risk <= 0 or risk < price*0.001: return None

    tp1  = price+risk*CRYPTO_SCALP_TP1 if is_buy else price-risk*CRYPTO_SCALP_TP1
    tp2  = price+risk*CRYPTO_SCALP_TP2 if is_buy else price-risk*CRYPTO_SCALP_TP2
    tp3  = price+risk*2.5 if is_buy else price-risk*2.5  # runner
    if abs(tp2-price)/risk < 1.8: return None

    # Apply ML adjusted score
    sess = get_gold_session()
    sig_tmp = {'dir':'BUY' if is_buy else 'SELL','score':score,'tags':tags,
               'rsi_val':round(ri_v,1),'weekly':weekly,'session':sess}
    ml_conf = scalp_adjusted_score(sig_tmp, sess)

    setup_names = {
        'SWEEP_OB': '⚡ Sweep+OB Retest',
        'CHOCH':    '🔄 CHoCH Reversal',
    }
    why_map = {
        'SWEEP_OB': (f"  1️⃣ Retail stops swept at {fp_crypto(wick_sl)}\n"
                     f"  2️⃣ Closed back above — stop hunt done\n"
                     f"  3️⃣ OB zone: {fp_crypto(ob_hit['bot'])} – {fp_crypto(ob_hit['top'])}\n" if ob_hit else
                     "  Sweep+OB retest\n")+
                    f"  4️⃣ SL below wick — tight risk\n"
                    f"  💡 Scalp: target TP1 fast, trail to TP2",
        'CHOCH':    (f"  1️⃣ {'Downtrend' if is_buy else 'Uptrend'} → structure shifted\n"
                     f"  2️⃣ CHoCH confirmed — {'bullish' if is_buy else 'bearish'} direction\n"
                     f"  3️⃣ Volume surge + MACD confirmed\n"
                     f"  💡 Scalp: SL at swing, quick TP1"),
    }

    return {
        'sym':        pair['sym'],
        'name':       pair['sym'],
        'pair':       f"{pair['sym']}/USD",
        'dir':        'BUY' if is_buy else 'SELL',
        'setup':      setup,
        'setup_name': setup_names.get(setup, setup),
        'why':        why_map.get(setup, ''),
        'score':      score,
        'ml_conf':    ml_conf,
        'conf':       min(96, round(score*8+min(CRYPTO_SCALP_TP2,3)*2.5)),
        'rr':         CRYPTO_SCALP_TP2,
        'price':      price,
        'entry':      price,
        'sl':         round(sl_p, 6),
        'tp1':        round(tp1, 6),
        'tp':         round(tp2, 6),
        'tp3':        round(tp3, 6),
        'risk_pct':   round(risk/price*100, 3),
        'rew_pct':    round(abs(tp2-price)/price*100, 3),
        'sl_dollar':  None,
        'tp1_dollar': None,
        'tp2_dollar': None,
        'tags':       tags,
        'weekly':     weekly,
        'daily':      daily,
        'session':    sess,
        'ob':         ob_hit,
        'wick_sl':    round(wick_sl, 6) if wick_sl else None,
        'rsi_val':    round(ri_v, 1),
        'is_crypto':  True,
    }

def build_crypto_scalp_msg(sig, count_today, total_today):
    """Crypto scalp TG message"""
    ib  = sig['dir'] == 'BUY'
    e   = sig['entry']
    risk = abs(e-sig['sl'])
    tp1d = round(abs(sig['tp1']-e), 6)
    tp2d = round(abs(sig['tp']-e), 6)
    tp1p = round(tp1d/e*100, 2)
    tp2p = round(tp2d/e*100, 2)
    em   = '⚡' if sig['setup']=='SWEEP_OB' else '🔄'

    return '\n'.join(filter(None, [
        f"{'🟢' if ib else '🔴'} <b>CRYPTO SCALP {'BUY' if ib else 'SELL'} — {sig['sym']}/USD</b>",
        f"{em} {sig['setup_name']}  |  Score: {sig['score']}/10  |  ML: {sig['ml_conf']:.0f}%",
        f"📅 Signal {count_today}/{CRYPTO_SCALP_DAILY_CAP} today  |  Session: {sig['session']}",
        "",
        "💰 <b>Scalp Levels</b>",
        f"  Entry:  <code>{fp_crypto(e)}</code>",
        f"  SL:     <code>{fp_crypto(sig['sl'])}</code>  <i>(-{sig['risk_pct']}%) — below wick</i>",
        f"  TP1:    <code>{fp_crypto(sig['tp1'])}</code>  <i>(+{tp1p}% — 1:1.5 — close 70%)</i>",
        f"  TP2:    <code>{fp_crypto(sig['tp'])}</code>   <i>(+{tp2p}% — 1:2.0 — runner 30%)</i>",
        f"  TP3:    <code>{fp_crypto(sig['tp3'])}</code>  <i>(1:2.5 — let go)</i>",
        "",
        "📖 <b>Why this scalp:</b>",
        sig['why'],
        "",
        f"🔍 {esc(' · '.join(sig['tags']))}",
        f"Weekly: {sig['weekly']}  |  RSI: {sig['rsi_val']}",
        sig['ob'] and f"OB: {fp_crypto(sig['ob']['bot'])} – {fp_crypto(sig['ob']['top'])}",
        "",
        f"⚡ <i>Scalp — aim for TP1 first. Move SL to entry at TP1.</i>",
        f"⚠️ <i>Exit at session close if TP not hit.</i>",
        f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC  |  📡 <b>SMC Crypto Scalp</b>",
    ]))

def run_crypto_scalp_scan():
    """
    Scan all 10 crypto pairs for scalp signals.
    Only fires during London/NY session.
    Max 3 scalps/coin/day.
    Tracks each in open_trades['SCALP_BTC'] etc.
    """
    if not is_scalp_session():
        log.debug("Crypto scalp: not in session — skip")
        return

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if today != state.get('scalp_crypto_day'):
        state['scalp_crypto_day']   = today
        state['scalp_crypto_count'] = {}  # {sym: count}

    signals_sent = 0
    for pair in CRYPTO_PAIRS:
        sym = pair['sym']
        # Daily cap check
        if state['scalp_crypto_count'].get(sym, 0) >= CRYPTO_SCALP_DAILY_CAP:
            continue
        # Cooldown: don't fire same coin twice within COOLDOWN_M
        lf = last_fired.get(f'SCALP_{sym}', {})
        if lf and time.time()-lf.get('time',0) < COOLDOWN_M*60:
            continue
        # Skip if already in open trade for this coin
        if f'SCALP_{sym}' in state['open_trades']:
            continue
        try:
            kl = fetch_crypto_candles(pair, limit=200)
            if not kl or len(kl)<60:
                log.debug(f"  Crypto scalp {sym}: no data"); continue
            sig = compute_crypto_scalp(kl, pair)
            if not sig:
                log.debug(f"  {sym}: no scalp setup"); continue
            # ML confidence gate
            if sig['ml_conf'] < 65:
                log.debug(f"  {sym}: low ML conf {sig['ml_conf']:.0f}% — skip")
                continue
            count = state['scalp_crypto_count'].get(sym, 0) + 1
            total = sum(state['scalp_crypto_count'].values()) + 1
            msg = build_crypto_scalp_msg(sig, count, total)
            ok  = send_tg(msg)
            if ok:
                last_fired[f'SCALP_{sym}'] = {'time':time.time(),'price':sig['price']}
                state['scalp_crypto_count'][sym] = count
                state['alerts_sent'] = state.get('alerts_sent', 0) + 1
                sess = get_gold_session()
                lid  = log_signal(sig, pair, sess)
                state['open_trades'][f'SCALP_{sym}'] = {
                    **sig,
                    'sym':          f'SCALP_{sym}',
                    'real_sym':     sym,
                    'pair_obj':     pair,
                    'time':         datetime.now(timezone.utc).isoformat(),
                    'be_triggered': False,
                    'tp2_hit':      False,
                    'session_name': sess,
                    'learn_id':     lid,
                }
                signals_sent += 1
                log.info(f"  Scalp {sym}: {sig['setup']} {sig['dir']} score={sig['score']} mlconf={sig['ml_conf']:.0f}% → TG ✓")
        except Exception as e:
            log.error(f"  Crypto scalp {sym} error: {e}")
        time.sleep(0.5)

    if signals_sent:
        log.info(f"Crypto scalp scan: {signals_sent} signals sent")

def check_crypto_scalp_prices():
    """Monitor open crypto scalp trades for TP/SL hits"""
    for trade_key in list(state['open_trades'].keys()):
        if not trade_key.startswith('SCALP_'): continue
        trade = state['open_trades'][trade_key]
        real_sym = trade.get('real_sym', trade_key.replace('SCALP_',''))
        pair_obj = trade.get('pair_obj')
        if not pair_obj: continue
        price = fetch_crypto_price(pair_obj)
        if not price: continue
        ib = trade['dir']=='BUY'; en = trade['entry']
        sl_p=trade['sl']; tp1_p=trade['tp1']; tp2_p=trade['tp']; tp3_p=trade.get('tp3',tp2_p)

        # TP1 — breakeven
        if not trade.get('be_triggered'):
            if (ib and price>=tp1_p) or (not ib and price<=tp1_p):
                trade['be_triggered']=True
                pnl1=round(abs(tp1_p-en)/en*100,3)
                send_tg(
                    f"🎯 <b>SCALP TP1 HIT — {real_sym}/USD +{pnl1}%</b>\n\n"
                    f"Close 70% at <code>{fp_crypto(price)}</code>\n"
                    f"Move SL to entry: <code>{fp_crypto(en)}</code>\n"
                    f"Runner → TP2: <code>{fp_crypto(tp2_p)}</code>\n"
                    f"Scalp now risk-free!  |  📡 SMC Crypto Scalp"
                )

        # TP2 — WIN
        if not trade.get('tp2_hit'):
            if (ib and price>=tp2_p) or (not ib and price<=tp2_p):
                trade['tp2_hit']=True
                pnl=round(abs(tp2_p-en)/en*100,3)
                send_tg(
                    f"✅ <b>SCALP WIN — {real_sym}/USD +{pnl}%</b>\n\n"
                    f"{trade.get('setup_name','—')}\n"
                    f"Entry {fp_crypto(en)} → Exit {fp_crypto(price)}\n"
                    f"Held: {_hours_held(trade)}\n\n"
                    f"🤖 ML learning from this win...\n"
                    f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC  |  📡 SMC Crypto Scalp"
                )
                scalp_record_trade(trade, trade.get('session_name','Unknown'), 'win', pnl)
                lid=trade.get('learn_id')
                if lid: close_trade(lid,'win',price)
                journal_close_trade(real_sym,'win',price)
                state['stats']['wins']=state['stats'].get('wins',0)+1
                del state['open_trades'][trade_key]; continue

        # TP3 — Runner
        if trade.get('tp2_hit') and not trade.get('tp3_hit'):
            if (ib and price>=tp3_p) or (not ib and price<=tp3_p):
                trade['tp3_hit']=True; pnl=round(abs(tp3_p-en)/en*100,3)
                send_tg(f"🚀 <b>SCALP RUNNER — {real_sym}/USD +{pnl}%</b>\nFull exit.  📡 SMC Crypto Scalp")
                continue

        # SL — LOSS
        if (ib and price<=sl_p) or (not ib and price>=sl_p):
            pnl=round(abs(sl_p-en)/en*100,3)
            tags=trade.get('tags',[]); rsi_v=trade.get('rsi_val',50); weekly=trade.get('weekly','neutral')
            why=[]
            if trade['dir']=='BUY' and weekly=='bearish': why.append("BUY vs weekly bearish")
            if trade['dir']=='SELL' and weekly=='bullish': why.append("SELL vs weekly bullish")
            if trade['dir']=='BUY' and rsi_v>62: why.append(f"RSI {rsi_v} overbought")
            if trade['dir']=='SELL' and rsi_v<38: why.append(f"RSI {rsi_v} oversold")
            if abs(price-en)/en*100<0.2: why.append("Stopped immediately — news/entry timing")
            reason = '\n'.join(f"  ⚠️ {r}" for r in why) if why else "  Market moved against setup"
            send_tg(
                f"❌ <b>SCALP LOSS — {real_sym}/USD -{pnl}%</b>\n\n"
                f"{trade.get('setup_name','—')}  Score: {trade.get('score',0)}/10\n"
                f"Entry {fp_crypto(en)} → SL {fp_crypto(price)}\n"
                f"Held: {_hours_held(trade)}\n\n"
                f"🔍 <b>Why failed:</b>\n{reason}\n\n"
                f"🤖 Adjusting ML weights...\n"
                f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC  |  📡 SMC Crypto Scalp"
            )
            scalp_record_trade(trade, trade.get('session_name','Unknown'), 'loss', -pnl)
            lid=trade.get('learn_id')
            if lid: close_trade(lid,'loss',price)
            journal_close_trade(real_sym,'loss',price)
            state['stats']['losses']=state['stats'].get('losses',0)+1
            del state['open_trades'][trade_key]

# ════════════════════════════════════════════════════════════

def gold_chop(atr_a, i, thresh=0.40):
    r=[x for x in atr_a[max(0,i-20):i] if x]
    return not r or atr_a[i]<sum(r)/len(r)*thresh

def compute_gold(kl):
    if not kl or len(kl)<60: return None
    n=len(kl); i=n-1
    closes=[k['c'] for k in kl]; vols=[k['v'] for k in kl]
    ri_a=rsi(closes); e9_a=ema(closes,9); e20_a=ema(closes,20); e50_a=ema(closes,50)
    ht_a=macd_hist(closes); at_a=calc_atr(kl); va_a=vol_avg(vols)
    if not all([ri_a[i],e9_a[i],e20_a[i],e50_a[i],at_a[i],va_a[i]]): return None
    if gold_chop(at_a,i): return None
    price=closes[i]; k=kl[i]
    at=float(at_a[i]); va=float(va_a[i])
    ri_v=float(ri_a[i]); e9=float(e9_a[i]); e20=float(e20_a[i]); e50=float(e50_a[i])
    ht=ht_a[i]
    sh,sl_sw=swings(kl[:i+1],5)
    weekly=htf_bias_fn(kl,i,21); daily=htf_bias_fn(kl,i,5)
    sess=get_gold_session()
    is_buy=None; score=0.0; tags=[]; setup=None; wick_sl=None; ob_hit=None

    # SETUP 1: Sweep + OB
    for li,lvl in [(ix,float(p)) for ix,p in sl_sw if ix<i-1 and ix>i-50][-4:]:
        if not(k['l']<lvl<price) or lvl-k['l']<at*0.28 or k['v']<va*1.1: continue
        ob=None
        for j in range(li-1,max(0,li-12),-1):
            if kl[j]['c']<kl[j]['o'] and (kl[min(j+2,n-1)]['c']-kl[j]['c'])/kl[j]['c']>0.002:
                ob={'top':kl[j]['o'],'bot':kl[j]['l']}; break
        if not ob or not(ob['bot']<=price<=ob['top']*1.006): continue
        is_buy=True; setup='SWEEP_OB'; score+=3.5; tags+=['Sweep↑','OB_Retest']; wick_sl=k['l']; ob_hit=ob; break
    for hi_,lvl in [(ix,float(p)) for ix,p in sh if ix<i-1 and ix>i-50][-4:]:
        if not(k['h']>lvl>price) or k['h']-lvl<at*0.28 or k['v']<va*1.1: continue
        ob=None
        for j in range(hi_-1,max(0,hi_-12),-1):
            if kl[j]['c']>kl[j]['o'] and (kl[min(j+2,n-1)]['c']-kl[j]['c'])/kl[j]['c']<-0.002:
                ob={'top':kl[j]['h'],'bot':kl[j]['c']}; break
        if not ob or not(ob['bot']*0.994<=price<=ob['top']): continue
        is_buy=False; setup='SWEEP_OB'; score+=3.5; tags+=['Sweep↓','OB_Retest']; wick_sl=k['h']; ob_hit=ob; break

    # SETUP 2: ICT Kill Zone (London/NY only)
    if is_buy is None and sess in('London','New York'):
        asia=kl[max(0,i-10):i-1]
        if len(asia)>=6:
            ahi=max(x['h'] for x in asia); alo=min(x['l'] for x in asia); rng=ahi-alo
            if at*0.4<=rng<=at*3.0:
                if k['c']>ahi and k['v']>va*1.2 and 38<ri_v<68 and ht and ht>0:
                    is_buy=True; setup='ICT_KZ'; score+=3.0; tags+=['KZ↑','Sess✓']; wick_sl=alo
                elif k['c']<alo and k['v']>va*1.2 and 32<ri_v<62 and ht and ht<0:
                    is_buy=False; setup='ICT_KZ'; score+=3.0; tags+=['KZ↓','Sess✓']; wick_sl=ahi

    # SETUP 3: CHoCH
    if is_buy is None:
        rh=[(ix,float(p)) for ix,p in sh if ix<=i][-5:]
        rl=[(ix,float(p)) for ix,p in sl_sw if ix<=i][-5:]
        if len(rh)>=3 and len(rl)>=3:
            h2,h1p=rh[-2][1],rh[-3][1]; l2,l1p=rl[-2][1],rl[-3][1]; vok=k['v']>va*1.05
            if abs(h2-h1p)/max(h1p,1)>=0.002 and abs(l2-l1p)/max(l1p,1)>=0.002:
                if h2<h1p and l2<l1p and price>h2 and ht and ht>0 and 28<ri_v<62 and vok:
                    is_buy=True; setup='CHOCH'; score+=3.5; tags+=['CHoCH↑']
                elif h2>h1p and l2>l1p and price<l2 and ht and ht<0 and 38<ri_v<72 and vok:
                    is_buy=False; setup='CHOCH'; score+=3.5; tags+=['CHoCH↓']

    # SETUP 4: EMA Pullback
    if is_buy is None and e9:
        if e9>e20>e50 and ht and ht>0 and abs(price-e20)/e20<0.004 and price>e50 and 38<ri_v<52:
            is_buy=True; setup='EMA_PULL'; score+=2.5; tags+=['EMA_Pull↑']
        elif e9<e20<e50 and ht and ht<0 and abs(price-e20)/e20<0.004 and price<e50 and 48<ri_v<62:
            is_buy=False; setup='EMA_PULL'; score+=2.5; tags+=['EMA_Pull↓']

    if is_buy is None: return None

    # Confluences
    if k['v']>va*1.6: score+=1.0; tags.append('Vol++')
    elif k['v']>va*1.2: score+=0.5; tags.append('Vol✓')
    if is_buy  and price>e20>e50: score+=1.0; tags.append('EMA↑')
    elif not is_buy and price<e20<e50: score+=1.0; tags.append('EMA↓')
    if ht:
        if is_buy  and ht>0: score+=0.5; tags.append('MACD+')
        elif not is_buy and ht<0: score+=0.5; tags.append('MACD-')
    if is_buy  and ri_v<35: score+=1.0; tags.append(f'RSI{round(ri_v)}')
    elif is_buy  and ri_v<50: score+=0.5; tags.append(f'RSI{round(ri_v)}')
    elif not is_buy and ri_v>65: score+=1.0; tags.append(f'RSI{round(ri_v)}')
    elif not is_buy and ri_v>50: score+=0.5; tags.append(f'RSI{round(ri_v)}')
    if is_buy  and weekly=='bullish': score+=0.5; tags.append('W:Bull')
    elif not is_buy and weekly=='bearish': score+=0.5; tags.append('W:Bear')
    elif (is_buy and weekly=='bearish') or (not is_buy and weekly=='bullish'): score-=1.5
    if is_buy  and daily=='bullish':  score+=0.5; tags.append('D:Bull')
    elif not is_buy and daily=='bearish': score+=0.5; tags.append('D:Bear')
    if sess in('London','New York') and 'Sess✓' not in tags:
        score+=0.5; tags.append('Sess✓')
    nr=round(price/25)*25
    if abs(price-nr)/at<0.6: score+=0.5; tags.append(f'${int(nr)}')
    r6=kl[max(0,i-6):i+1]; bc=sum(1 for x in r6 if x['c']<x['o'])
    if is_buy  and bc>=5: score-=2.0
    if not is_buy and (6-bc)>=5: score-=2.0
    if sess=='Weekend': score-=1.5
    score=round(max(0,min(10,score)),1)
    if score<GOLD_MIN_SCORE: return None

    # SL placement
    if wick_sl is not None:
        sl_p=wick_sl-at*0.08 if is_buy else wick_sl+at*0.08
    else:
        sl_p=(price-at*1.2) if is_buy else (price+at*1.2)
    if is_buy  and (price-sl_p)>price*MAX_SL_PCT: sl_p=price-price*MAX_SL_PCT
    if not is_buy and (sl_p-price)>price*MAX_SL_PCT: sl_p=price+price*MAX_SL_PCT
    risk=abs(price-sl_p)
    if risk<=0 or risk<0.5: return None

    tp1=price+risk*TP1_MULT if is_buy else price-risk*TP1_MULT
    tp2=price+risk*TP2_MULT if is_buy else price-risk*TP2_MULT
    tp3=price+risk*TP3_MULT if is_buy else price-risk*TP3_MULT
    rr=round(TP2_MULT,1)
    if rr<1.4: return None

    sl_dollar=round(abs(price-sl_p),2); tp1_dollar=round(abs(tp1-price),2); tp2_dollar=round(abs(tp2-price),2)
    sn={'SWEEP_OB':'⚡ Gold Sweep+OB','ICT_KZ':'🕯️ Gold ICT Kill Zone','CHOCH':'🔄 Gold CHoCH','EMA_PULL':'📊 Gold EMA Pull'}
    why_map={
        'SWEEP_OB': (f"  1️⃣ Equal lows swept at ${wick_sl:.2f}\n"
                     f"  2️⃣ Closed back above — stop hunt done\n"
                     f"  3️⃣ OB zone: ${ob_hit['bot']:.2f} – ${ob_hit['top']:.2f}\n" if ob_hit else
                     "  Sweep + OB retest\n")+
                    f"  4️⃣ SL below wick = tight ${sl_dollar} risk\n"
                    f"  💡 SCALP: TP1 at ${tp1:.2f} (+${tp1_dollar})",
        'ICT_KZ':  (f"  1️⃣ Asia range consolidated\n"
                    f"  2️⃣ {sess} open broke range with volume\n"
                    f"  3️⃣ MACD + EMA confirm direction\n"
                    f"  4️⃣ SL at Asia range boundary\n"
                    f"  💡 SCALP: exit TP1 if momentum slows"),
        'CHOCH':   (f"  1️⃣ Gold structure shifted {'bearish→bullish' if is_buy else 'bullish→bearish'}\n"
                    f"  2️⃣ CHoCH confirmed — new direction\n"
                    f"  3️⃣ SL at last swing {'low' if is_buy else 'high'}\n"
                    f"  💡 SCALP: TP1 at first structure level"),
        'EMA_PULL':(f"  1️⃣ Strong {'up' if is_buy else 'down'}trend, pulled to EMA20\n"
                    f"  2️⃣ RSI reset to neutral = good re-entry\n"
                    f"  3️⃣ Trend continuation\n"
                    f"  💡 SCALP: enter on EMA touch, quick TP1"),
    }
    return {
        'sym':'XAU','name':'Gold','pair':'XAU/USD',
        'dir':'BUY' if is_buy else 'SELL',
        'setup':setup,'setup_name':sn.get(setup,setup),'why':why_map.get(setup,''),
        'score':score,'conf':min(96,round(score*8+min(rr,3)*2.5)),'rr':rr,
        'price':price,'entry':price,
        'sl':round(sl_p,2),'tp':round(tp2,2),'tp1':round(tp1,2),'tp3':round(tp3,2),
        'risk_pct':round(risk/price*100,3),'rew_pct':round(abs(tp2-price)/price*100,3),
        'sl_dollar':sl_dollar,'tp1_dollar':tp1_dollar,'tp2_dollar':tp2_dollar,
        'tags':tags,'weekly':weekly,'daily':daily,'session':sess,
        'ob':ob_hit,'wick_sl':round(wick_sl,2) if wick_sl else None,'rsi_val':round(ri_v,1),
    }

# ═══ HELPERS + TELEGRAM ════════════════════════════════════════════════════
def fp(p):
    if not p: return '—'
    if p >= 10000: return f'${p:,.0f}'
    if p >= 100:   return f'${p:.2f}'
    if p >= 1:     return f'${p:.3f}'
    return f'${p:.5f}'

def esc(s):
    return str(s).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')


def send_tg(msg):
    if not TG_TOKEN or not TG_CHAT: return False
    try:
        r = requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id':TG_CHAT,'text':msg,'parse_mode':'HTML',
                  'disable_web_page_preview':True},
            timeout=10)
        if r.ok: return True
        err = r.json().get('description','unknown')
        log.error(f"TG failed: {err}")
        # Retry plain
        r2 = requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id':TG_CHAT,'text':msg[:4000],'disable_web_page_preview':True},
            timeout=10)
        return r2.ok
    except Exception as e:
        log.error(f"TG exception: {e}"); return False

def saveTG():
    pass  # config via env vars only

def run_scan():
    state['scans_done'] += 1
    state['last_scan'] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    log.info(f"Scan #{state['scans_done']} — {len(PAIRS)} pairs")
    if check_circuit_breaker(): pass

def _hours_held(trade):
    try:
        secs=time.time()-datetime.fromisoformat(trade['time']).timestamp()
        return f"{int(secs//3600)}h {int((secs%3600)//60)}m"
    except: return '—'


def build_scalp_msg(sig):
    """Scalp-specific TG message — different format from main trade"""
    ib=sig['dir']=='BUY'; e=sig['entry']
    sl_d=round(abs(e-sig['sl']),2)
    tp1_d=round(abs(sig['tp_scalp1']-e),2)
    tp2_d=round(abs(sig['tp_scalp2']-e),2)
    emojis={'SWEEP_OB':'⚡','ICT_KZ':'🕯️','CHOCH':'🔄','EMA_PULL':'📊'}
    em=emojis.get(sig['setup'],'📡')
    today_count=state['stats'].get('scalp_today',1)
    ml_conf=sig.get('ml_conf',0)
    lines=[
        f"{'🟡' if ib else '🔴'} <b>GOLD SCALP {'BUY' if ib else 'SELL'} #{today_count}/3 — XAU/USD</b>",
        f"{em} {sig.get('setup_name','Gold Scalp')}  |  Score: {sig['score']}/10  |  ML: {ml_conf:.0f}%",
        "",
        f"💰 <b>Scalp Levels</b>",
        f"  Entry:  <code>${e:.2f}</code>",
        f"  SL:     <code>${sig['sl']:.2f}</code>  <i>(-${sl_d}) — tight, use limit order</i>",
        f"  TP1:    <code>${sig['tp_scalp1']:.2f}</code>  <i>(+${tp1_d} — 1:1.5 — close 70%)</i>",
        f"  TP2:    <code>${sig['tp_scalp2']:.2f}</code>  <i>(+${tp2_d} — 1:2.0 — runner 30%)</i>",
        "",
        f"📖 <b>Why scalp:</b>",
        sig['why'],
        "",
        f"🔍 Tags: {esc(' · '.join(sig['tags']))}",
        f"📅 Session: {sig['session']}  |  RSI: {sig['rsi_val']}  |  Weekly: {sig['weekly']}",
        "",
        f"⚡ <i>SCALP — target TP1 fast. Move SL to entry at TP1.</i>",
        f"⚠️ <i>Max hold: 2-3hrs. Exit at close of London/NY if not hit.</i>",
        f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC  |  🏅 <b>SMC Gold Scalp</b>",
    ]
    return '\n'.join(l for l in lines if l is not None)

def build_gold_msg(sig):
    ib=sig['dir']=='BUY'; e=sig['entry']
    mode=('📋 PAPER' if PAPER_MODE else ('SCALP🎯' if SCALP_MODE else 'SWING📐'))
    emojis={'SWEEP_OB':'⚡','ICT_KZ':'🕯️','CHOCH':'🔄','EMA_PULL':'📊'}
    em=emojis.get(sig['setup'],'📡')
    paper_header = (
        f"\n📋 <b>PAPER TRADE — DO NOT ENTER YET</b>\n"
        f"<i>Observing {state['stats'].get('paper_count',0)+1}/{PAPER_TARGET} signals before going live.\n"
        f"Just watch if this would have worked.</i>\n"
    ) if PAPER_MODE else ""
    lines=[
        f"{'🟡' if ib else '🔴'} <b>GOLD {'BUY' if ib else 'SELL'} — XAU/USD [{mode}]</b>",
        paper_header if PAPER_MODE else None,
        f"{em} <b>{sig['setup_name']}</b>",
        "",
        f"📖 <b>Why this trade:</b>",
        sig['why'],
        "",
        f"💰 <b>Trade Levels</b>",
        f"  Entry:  <code>${e:.2f}</code>",
        f"  SL:     <code>${sig['sl']:.2f}</code>  <i>(-${sig['sl_dollar']:.2f}) ⚠️ use limit order</i>",
        f"  TP1:    <code>${sig['tp1']:.2f}</code>  <i>(+${sig['tp1_dollar']:.2f} — close 60%, SL→entry)</i>",
        f"  TP2:    <code>${sig['tp']:.2f}</code>   <i>(1:{sig['rr']} — close 30%)</i>",
        f"  TP3:    <code>${sig['tp3']:.2f}</code>  <i>(runner — 10%)</i>",
        "",
        f"📊 Score: {sig['score']}/10  |  R:R 1:{sig['rr']}  |  Conf: {sig['conf']}%",
        f"  Tags: {esc(' · '.join(sig['tags']))}",
        f"  Session: {sig['session']}  |  Weekly: {sig['weekly']}  |  RSI: {sig['rsi_val']}",
        f"  OB: ${sig['ob']['bot']:.2f} – ${sig['ob']['top']:.2f}" if sig.get('ob') else None,
        f"  Swept at: ${sig['wick_sl']:.2f}" if sig.get('wick_sl') else None,
        "",
        f"⚠️ <i>Gold scalp — SL=${sig['sl_dollar']:.2f}/oz. Check news before entry.</i>",
        f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC  |  🏅 <b>SMC Gold v1</b>",
    ]
    return '\n'.join(l for l in lines if l is not None)

def _win_g(t):
    lines=[]; tags=t.get('tags',[])
    if any('Sweep' in x for x in tags): lines.append("  ✅ Sweep cleared retail stops — clean reversal")
    if 'OB_Retest' in tags: lines.append("  ✅ OB zone defended by institutions")
    if any('Vol' in x for x in tags): lines.append("  ✅ Volume surge confirmed direction")
    if any('EMA' in x for x in tags): lines.append("  ✅ EMA stack aligned")
    if t.get('session') in ('London','New York'): lines.append(f"  ✅ {t['session']} session = high quality")
    return '\n'.join(lines) if lines else "  ✅ Confluence aligned"

def _loss_g(t, exit_p):
    lines=[]; ib=t['dir']=='BUY'; e=t['entry']
    weekly=t.get('weekly','neutral'); rsi_v=t.get('rsi_val',50)
    sess=t.get('session',''); score=t.get('score',0); mv=abs(exit_p-e)/e*100
    if ib and weekly=='bearish': lines.append("  ⚠️ BUY against weekly bearish trend")
    if not ib and weekly=='bullish': lines.append("  ⚠️ SELL against weekly bullish trend")
    if ib and rsi_v>62: lines.append(f"  ⚠️ RSI {rsi_v} — overbought for buy")
    if not ib and rsi_v<38: lines.append(f"  ⚠️ RSI {rsi_v} — oversold for sell")
    if sess=='Weekend': lines.append("  ⚠️ Weekend — thin gold market, fake moves")
    if sess=='Asian': lines.append("  ⚠️ Asian session — low gold volume")
    if score<=6.5: lines.append(f"  ⚠️ Low score ({score}/10)")
    if mv<0.2: lines.append("  ℹ️ Stopped immediately — news event or bad timing")
    if not lines: lines.append("  ℹ️ Valid setup — macro/news overrode technicals")
    lines.append(f"\n  🧠 Learning from this — adjusting weights")
    return '\n'.join(lines)

# ═══ PRICE MONITOR ══════════════════════════════════════════════════════════
def _check_scalp_trade():
    """Monitor scalp trade with faster exit logic"""
    trade = state['open_trades'].get('XAU_SCALP')
    if not trade: return
    price = fetch_gold_price()
    if not price: return
    ib=trade['dir']=='BUY'; en=trade['entry']
    sl_p=trade['sl']; tp1_p=trade['tp1']; tp2_p=trade['tp']

    # TP1 — close 70%, move SL to entry
    if not trade.get('be_triggered'):
        if (ib and price>=tp1_p) or (not ib and price<=tp1_p):
            trade['be_triggered']=True
            d1=round(abs(tp1_p-en),2); pnl1=round(abs(tp1_p-en)/en*100,3)
            send_tg(
                f"🎯 <b>GOLD SCALP TP1 +{pnl1}% (+${d1})</b>\n\n"
                f"Close 70% at <code>${price:.2f}</code>\n"
                f"Move SL to entry: <code>${en:.2f}</code>\n"
                f"Runner 30% → TP2: <code>${tp2_p:.2f}</code>\n"
                f"⚡ Scalp trade now risk-free!  |  🏅 SMC Gold Scalp"
            )
            log.info(f"  Scalp TP1 +{pnl1}%")

    # TP2 — WIN
    if not trade.get('tp2_hit'):
        if (ib and price>=tp2_p) or (not ib and price<=tp2_p):
            trade['tp2_hit']=True; pnl=round(abs(tp2_p-en)/en*100,3); d2=round(abs(tp2_p-en),2)
            send_tg(
                f"✅ <b>GOLD SCALP WIN +{pnl}% (+${d2})</b>\n\n"
                f"Setup: {trade.get('setup_name','—')}\n"
                f"Entry ${en:.2f} → Exit ${price:.2f}\n"
                f"Held: {_hours_held(trade)}\n\n"
                f"🤖 ML learning from this win...\n"
                f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC  |  🏅 SMC Gold Scalp"
            )
            # ML learning
            scalp_record_trade(trade, trade.get('session_name','Unknown'), 'win', pnl)
            lid=trade.get('learn_id')
            if lid: close_trade(lid,'win',price)
            journal_close_trade('XAU','win',price)
            state['stats']['wins']=state['stats'].get('wins',0)+1
            state['stats']['scalp_wins']=state['stats'].get('scalp_wins',0)+1
            log.info(f"  Scalp WIN +{pnl}%")
            del state['open_trades']['XAU_SCALP']; return

    # SL — LOSS
    if (ib and price<=sl_p) or (not ib and price>=sl_p):
        pnl=round(abs(sl_p-en)/en*100,3); dl=round(abs(sl_p-en),2)
        # ML failure analysis
        tags=trade.get('tags',[]); rsi_v=trade.get('rsi_val',50); weekly=trade.get('weekly','neutral')
        why_loss=[]
        if ib and weekly=='bearish': why_loss.append("BUY against weekly bearish")
        if not ib and weekly=='bullish': why_loss.append("SELL against weekly bullish")
        if ib and rsi_v>62: why_loss.append(f"RSI {rsi_v} overbought for buy")
        if not ib and rsi_v<38: why_loss.append(f"RSI {rsi_v} oversold for sell")
        mv=abs(price-en)/en*100
        if mv<0.15: why_loss.append("Stopped immediately — bad entry timing")
        reason='\n'.join(f"  ⚠️ {r}" for r in why_loss) if why_loss else "  Market conditions changed"
        send_tg(
            f"❌ <b>GOLD SCALP LOSS -{pnl}% (-${dl})</b>\n\n"
            f"Setup: {trade.get('setup_name','—')}  Score: {trade.get('score',0)}/10\n"
            f"Entry ${en:.2f} → SL ${price:.2f}\n"
            f"Held: {_hours_held(trade)}\n\n"
            f"🔍 <b>Why it failed:</b>\n{reason}\n\n"
            f"🤖 ML adjusting weights to avoid this...\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC  |  🏅 SMC Gold Scalp"
        )
        scalp_record_trade(trade, trade.get('session_name','Unknown'), 'loss', -pnl)
        lid=trade.get('learn_id')
        if lid: close_trade(lid,'loss',price)
        journal_close_trade('XAU','loss',price)
        state['stats']['losses']=state['stats'].get('losses',0)+1
        state['stats']['scalp_losses']=state['stats'].get('scalp_losses',0)+1
        log.info(f"  Scalp LOSS -{pnl}%")
        del state['open_trades']['XAU_SCALP']

def check_gold_prices():
    # Check scalp trades first (faster moving)
    if 'XAU_SCALP' in state['open_trades']:
        _check_scalp_trade()
    if 'XAU' not in state['open_trades']: return
    trade=state['open_trades']['XAU']
    price=fetch_gold_price()
    if not price: return
    ib=trade['dir']=='BUY'; en=trade['entry']
    sl_p=trade['sl']; tp1_p=trade['tp1']; tp2_p=trade['tp']; tp3_p=trade.get('tp3',tp2_p)

    if not trade.get('be_triggered'):
        if (ib and price>=tp1_p) or (not ib and price<=tp1_p):
            trade['be_triggered']=True
            pnl1=round(abs(tp1_p-en)/en*100,3); d1=round(abs(tp1_p-en),2)
            send_tg(
                f"🎯 <b>GOLD TP1 HIT +{pnl1}% (+${d1})</b>\n\n"
                f"✅ Close 60% at <code>${price:.2f}</code>\n"
                f"🔒 Move SL to entry: <code>${en:.2f}</code>\n\n"
                f"🎯 TP2: <code>${tp2_p:.2f}</code>  |  🚀 TP3: <code>${tp3_p:.2f}</code>\n"
                f"<i>Gold now risk-free. Let runner go.</i>\n"
                f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC  |  🏅 SMC Gold v1"
            )
            log.info(f"  🎯 GOLD TP1 +{pnl1}%")

    if not trade.get('tp2_hit'):
        if (ib and price>=tp2_p) or (not ib and price<=tp2_p):
            trade['tp2_hit']=True
            pnl=round(abs(tp2_p-en)/en*100,3); d2=round(abs(tp2_p-en),2)
            analysis=_win_g(trade)
            send_tg(
                f"✅ <b>GOLD WIN +{pnl}% (+${d2})</b>\n\n"
                f"🏅 {trade.get('setup_name','—')}\n"
                f"💰 Entry ${en:.2f} → Exit ${price:.2f}\n"
                f"⏱ Held: {_hours_held(trade)}\n\n"
                f"🏆 <b>Why it worked:</b>\n{analysis}\n\n"
                f"🚀 TP3 runner: <code>${tp3_p:.2f}</code>\n"
                f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC  |  🏅 SMC Gold v1"
            )
            lid=trade.get('learn_id')
            if lid: close_trade(lid,'win',price)
            try:
                kl_d=fetch_gold_candles(limit=100)
                if kl_d: learn_from_trade({**trade,'sym':'XAU','pnl':pnl},'win',kl_d)
            except: pass
            journal_close_trade('XAU','win',price)
            state['stats']['wins']=state['stats'].get('wins',0)+1
            paper_tag="[PAPER] " if PAPER_MODE else ""
            log.info(f"  ✅ {paper_tag}GOLD WIN +{pnl}%")
            del state['open_trades']['XAU']; return

    if trade.get('tp2_hit') and not trade.get('tp3_hit'):
        if (ib and price>=tp3_p) or (not ib and price<=tp3_p):
            trade['tp3_hit']=True; pnl=round(abs(tp3_p-en)/en*100,3); d3=round(abs(tp3_p-en),2)
            send_tg(f"🚀 <b>GOLD RUNNER +{pnl}% (+${d3})</b>\nFull exit. 🏅 SMC Gold v1")
            return

    if (ib and price<=sl_p) or (not ib and price>=sl_p):
        pnl=round(abs(sl_p-en)/en*100,3); dl=round(abs(sl_p-en),2)
        analysis=_loss_g(trade,price)
        send_tg(
            f"❌ <b>GOLD LOSS -{pnl}% (-${dl})</b>\n\n"
            f"📐 {trade.get('setup_name','—')}  Score: {trade.get('score',0)}/10\n"
            f"💰 Entry ${en:.2f} → SL ${price:.2f}\n"
            f"⏱ Held: {_hours_held(trade)}\n\n"
            f"🔍 <b>Failure Analysis:</b>\n{analysis}\n\n"
            f"📚 <i>Adjusting weights to avoid this next time.</i>\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC  |  🏅 SMC Gold v1"
        )
        lid=trade.get('learn_id')
        if lid: close_trade(lid,'loss',price)
        try:
            kl_d=fetch_gold_candles(limit=100)
            if kl_d: learn_from_trade({**trade,'sym':'XAU','pnl':-pnl},'loss',kl_d)
        except: pass
        journal_close_trade('XAU','loss',price)
        state['stats']['losses']=state['stats'].get('losses',0)+1
        log.info(f"  ❌ GOLD LOSS -{pnl}%")
        del state['open_trades']['XAU']

# ═══ HEALTH + COMMANDS + MAIN ════════════════════════════════════════════════
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        w=state['stats'].get('wins',0); l=state['stats'].get('losses',0)
        b=state['stats'].get('be',0); tot=w+l+b
        ot=', '.join(f"{s}: {v['dir']} {v.get('setup','?')} @${v['entry']:.2f}"
                     for s,v in state['open_trades'].items()) or 'none'
        body=(
            f"SMC Gold Engine v1\n{'='*40}\n"
            f"Started:   {state['started']}\n"
            f"Last scan: {state['last_scan']}\n"
            f"Scans:     {state['scans_done']}\n"
            f"Alerts:    {state['alerts_sent']}\n"
            f"Mode:      {'SCALP' if SCALP_MODE else 'SWING'}\n\n"
            f"W:{w} L:{l} BE:{b} WR:{round(w/tot*100) if tot else 0}%\n"
            f"Open: {ot}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        ).encode()
        self.send_response(200); self.send_header('Content-Type','text/plain')
        self.send_header('Content-Length',str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self,*a): pass

def run_gold_scan():
    state['scans_done']+=1
    state['last_scan']=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

    # ── Market hours check ─────────────────────────────────────
    is_open, sess_info = gold_market_open()
    if not is_open:
        log.info(f"Gold market closed: {sess_info} — skipping scan")
        return

    log.info(f"Gold scan #{state['scans_done']} — Session: {sess_info}")
    try:
        kl=fetch_gold_candles(limit=200)
        if not kl: log.warning("Gold: no data"); return

        # ── SCALP SIGNAL (score >= 8.5, session only) ──────────
        scalp_sig = None
        if is_scalp_session():
            # Compute signal then check if it qualifies as scalp
            raw_sig = compute_gold(kl)
            if raw_sig and raw_sig['score'] >= 8.5:
                # Apply ML-adjusted confidence
                ml_conf = scalp_adjusted_score(raw_sig, sess_info)
                if ml_conf >= 72:  # ML-weighted threshold
                    scalp_sig = {**raw_sig, 'trade_type':'SCALP',
                                 'ml_conf': ml_conf,
                                 # Scalp targets: faster, tighter
                                 'tp_scalp1': round(raw_sig['entry'] + abs(raw_sig['entry']-raw_sig['sl'])*1.5,2) if raw_sig['dir']=='BUY'
                                              else round(raw_sig['entry'] - abs(raw_sig['entry']-raw_sig['sl'])*1.5,2),
                                 'tp_scalp2': round(raw_sig['entry'] + abs(raw_sig['entry']-raw_sig['sl'])*2.0,2) if raw_sig['dir']=='BUY'
                                              else round(raw_sig['entry'] - abs(raw_sig['entry']-raw_sig['sl'])*2.0,2),
                                 }
                    log.info(f"  Scalp signal: {raw_sig['setup']} {raw_sig['dir']} score={raw_sig['score']} mlconf={ml_conf}")

        # ── MAIN SIGNAL (score >= 6) ────────────────────────────
        sig=compute_gold(kl)
        # Send scalp signal if found
        if scalp_sig:
            lf_s=last_fired.get('XAU_SCALP',{})
            cd_s=not lf_s or time.time()-lf_s.get('time',0)>COOLDOWN_M*60
            dc=state['stats'].get('scalp_today',0)  # daily cap
            today_key=datetime.now(timezone.utc).strftime('%Y-%m-%d')
            if today_key!=state['stats'].get('scalp_day'):
                state['stats']['scalp_today']=0; state['stats']['scalp_day']=today_key
            if cd_s and state['stats'].get('scalp_today',0)<3:
                ok_s=send_tg(build_scalp_msg(scalp_sig))
                if ok_s:
                    last_fired['XAU_SCALP']={'time':time.time(),'price':scalp_sig['price']}
                    state['stats']['scalp_today']=state['stats'].get('scalp_today',0)+1
                    state['stats']['alerts_sent_scalp']=state['stats'].get('alerts_sent_scalp',0)+1
                    state['open_trades']['XAU_SCALP']={
                        **scalp_sig,
                        'sym':'XAU_SCALP','pair':'XAU/USD',
                        'tp1':scalp_sig['tp_scalp1'],'tp':scalp_sig['tp_scalp2'],
                        'tp3':scalp_sig['tp_scalp2'],
                        'time':datetime.now(timezone.utc).isoformat(),
                        'be_triggered':False,'tp2_hit':False,'tp3_hit':False,
                        'session_name':sess_info,
                        'learn_id': log_signal(scalp_sig,{'sym':'XAU','cg':'','kr':''},sess_info),
                    }
                    log.info(f"  Scalp TG sent #{state['stats']['scalp_today']}/3 today")

        if not sig: log.info(f"  No main setup (price ${kl[-1]['c']:.2f})"); return
        lf=last_fired.get('XAU',{})
        pm=not lf or abs(sig['price']-lf.get('price',0))/sig['price']>0.003
        cd=not lf or time.time()-lf.get('time',0)>COOLDOWN_M*60
        if pm and cd:
            ok=send_tg(build_gold_msg(sig))
            if ok:
                last_fired['XAU']={'time':time.time(),'price':sig['price']}
                state['alerts_sent']+=1
                sess=get_gold_session()
                lid=log_signal(sig,{'sym':'XAU','cg':'','kr':''},sess)
                state['open_trades']['XAU']={
                    'sym':'XAU','name':'Gold','pair':'XAU/USD',
                    'dir':sig['dir'],'setup':sig['setup'],'setup_name':sig['setup_name'],
                    'entry':sig['entry'],'sl':sig['sl'],'tp':sig['tp'],
                    'tp1':sig['tp1'],'tp3':sig['tp3'],'score':sig['score'],'rr':sig['rr'],
                    'tags':sig['tags'],'weekly':sig['weekly'],'daily':sig['daily'],
                    'rsi_val':sig['rsi_val'],'session':sess,'session_name':sess,
                    'time':datetime.now(timezone.utc).isoformat(),
                    'be_triggered':False,'tp2_hit':False,'tp3_hit':False,
                    'why':sig['why'],'learn_id':lid,
                }
                journal_log_signal(sig,{'sym':'XAU'})
                if PAPER_MODE:
                    # Paper mode: track signal outcome without real position
                    state['stats']['paper_count'] = state['stats'].get('paper_count',0)+1
                    paper_count = state['stats']['paper_count']
                    log.info(f"  📋 PAPER #{paper_count} {sig['setup']} {sig['dir']} score={sig['score']}")
                    # Check if enough paper trades to evaluate
                    if paper_count >= PAPER_TARGET:
                        send_tg(
                            f"📊 <b>Gold Paper Trade Report — {paper_count} signals observed</b>\n\n"
                            + performance_report() +
                            f"\n\n💡 Set <b>PAPER_MODE=false</b> in Railway env vars if results look good.\n"
                            f"📡 SMC Gold v1"
                        )
                else:
                    log.info(f"  🏅 {sig['setup']} {sig['dir']} score={sig['score']} → LIVE ✓")
        else:
            log.info(f"  Signal found score={sig['score']} but cooldown/no move")
    except Exception as e:
        log.error(f"Gold scan error: {e}")

    # ── CRYPTO SCALP SCAN (same loop, separate signals) ──────
    try:
        run_crypto_scalp_scan()
    except Exception as e:
        log.error(f"Crypto scalp scan error: {e}")

def tg_commands():
    lid=[0]
    while True:
        try:
            r=requests.get(f'https://api.telegram.org/bot{TG_TOKEN}/getUpdates',
                params={'offset':lid[0]+1,'timeout':10},timeout=15)
            if r.ok:
                for upd in r.json().get('result',[]):
                    lid[0]=upd['update_id']
                    txt=upd.get('message',{}).get('text','').strip().lower()
                    if txt in ('/stats','/report'):
                        w=state['stats'].get('wins',0); l=state['stats'].get('losses',0)
                        b=state['stats'].get('be',0); tot=w+l+b
                        send_tg(
                            f"🏅 <b>Gold Engine Stats</b>\n\n"
                            f"✅ Wins: {w}  ❌ Losses: {l}  ➡️ BE: {b}\n"
                            f"WR: {round(w/tot*100) if tot else 0}%\n"
                            f"Open: {len(state['open_trades'])}\n"
                            f"Alerts: {state['alerts_sent']}\n"
                            f"Mode: {'SCALP 🎯' if SCALP_MODE else 'SWING 📐'}\n\n"
                            + journal_stats_report()
                        )
                    elif txt in ('/learn','/ml'): send_tg(performance_report())
                    elif txt in ('/deep','/analysis'): send_tg(deep_learning_report())
                    elif txt=='/weights':
                        db=load_db(); w_d=db['weights']
                        lines=["⚖️ <b>Gold Learned Weights</b>\n","<b>Setup scores:</b>"]
                        for s,v in w_d['setup_scores'].items(): lines.append(f"  {s}: {v:.2f}")
                        lines.append("\n<b>Sessions:</b>")
                        for s,v in w_d['session_weights'].items(): lines.append(f"  {s}: {v:.2f}x")
                        send_tg('\n'.join(lines))
                    elif txt=='/weekly': send_tg(weekly_learning_report())
                    elif txt=='/open':
                        msg="🔓 <b>Open gold trades:</b>\n"+'\n'.join(
                            f"  • {s}: {v['dir']} {v.get('setup','?')} @${v['entry']:.2f}"
                            for s,v in state['open_trades'].items()) if state['open_trades'] else "✅ No open trades"
                        send_tg(msg)
                    elif txt in ('/paper','/status'):
                        pc=state['stats'].get('paper_count',0)
                        remaining=max(0,PAPER_TARGET-pc)
                        send_tg(
                            f"📋 <b>Gold Engine Status</b>\n\n"
                            f"Mode: {'📋 PAPER (observing)' if PAPER_MODE else '🟢 LIVE'}\n"
                            f"Paper signals: {pc}/{PAPER_TARGET}\n"
                            f"Remaining to go live: {remaining}\n\n"
                            f"{'⏳ Still collecting data...' if remaining>0 else '✅ Ready to go live! Set PAPER_MODE=false'}\n\n"
                            + performance_report()
                        )
                    elif txt in ('/scalp','/scalps','/scalpml'):
                        send_tg(scalp_ml_report())
                    elif txt=='/help':
                        send_tg(
                            "🏅 <b>SMC Gold Commands</b>\n\n"
                            "/stats   — performance\n"
                            "/scalp   — scalp ML report\n"
                            "/paper   — paper trade status\n"
                            "/learn   — learning report\n"
                            "/deep    — chart analysis\n"
                            "/weights — learned weights\n"
                            "/weekly  — weekly summary\n"
                            "/open    — open trades\n"
                            "/help    — this menu"
                        )
        except Exception as e: log.debug(f"TG cmd: {e}")
        time.sleep(30)

def main():
    if not TG_TOKEN or not TG_CHAT:
        log.error("Need TG_TOKEN + TG_CHAT env vars"); raise SystemExit(1)
    log.info("="*55)
    log.info("SMC GOLD ENGINE v1 — 24/7 SELF-LEARNING")
    log.info(f"Mode: {'SCALP' if SCALP_MODE else 'SWING'} | SL max: {MAX_SL_PCT*100}% | "
             f"TP1:1:{TP1_MULT} TP2:1:{TP2_MULT} TP3:1:{TP3_MULT}")
    log.info(f"Scan:{SCAN_EVERY}min | Cooldown:{COOLDOWN_M}m | MinScore:{GOLD_MIN_SCORE}")
    log.info("="*55)
    threading.Thread(target=lambda:HTTPServer(('',PORT),Health).serve_forever(),daemon=True).start()
    log.info(f"Health on :{PORT}")
    send_tg(
        "🏅 <b>SMC Gold Engine v1 Started</b>\n\n"
        f"Mode: {'🎯 SCALP' if SCALP_MODE else '📐 SWING'}\n"
        f"Max SL: {MAX_SL_PCT*100}% (~${round(2000*MAX_SL_PCT)}/oz)\n"
        f"TP1: 1:{TP1_MULT}  TP2: 1:{TP2_MULT}  TP3: 1:{TP3_MULT}\n"
        f"Scan every {SCAN_EVERY} min\n\n"
        "📐 Setups: Sweep+OB · ICT Kill Zone · CHoCH · EMA Pull\n"
        "🧠 Self-learning: adjusts after every trade\n\n"
        "Alerts:\n"
        "🎯 TP1 → close 60%, SL to entry\n"
        "✅ TP2 WIN → why it worked\n"
        "❌ SL LOSS → failure analysis + learning\n"
        "📅 Monday 08 UTC → weekly report\n\n"
        "/stats /learn /deep /weights /open /help\n\n"
        "🏅 <b>SMC Gold v1 — Self Learning</b>"
    )
    threading.Thread(target=lambda:[
        (time.sleep(60), check_gold_prices()) and None
        for _ in iter(int, 1)
    ],daemon=True).start()

    def _monitor():
        while True:
            try:
                check_gold_prices()           # gold TP/SL
                check_crypto_scalp_prices()   # crypto scalp TP/SL
            except Exception as e: log.debug(f"Monitor: {e}")
            time.sleep(60)
    threading.Thread(target=_monitor,daemon=True).start()

    def _weekly():
        while True:
            n=datetime.now(timezone.utc)
            if n.weekday()==0 and n.hour==8 and n.minute<5:
                send_tg(weekly_learning_report()); time.sleep(300)
            time.sleep(60)
    threading.Thread(target=_weekly,daemon=True).start()
    threading.Thread(target=tg_commands,daemon=True).start()
    log.info("✓ All threads started")
    while True:
        try: run_gold_scan()
        except Exception as e: log.error(f"Scan: {e}")
        log.info(f"Next scan in {SCAN_EVERY}m...")
        time.sleep(SCAN_EVERY*60)

if __name__=='__main__':
    main()