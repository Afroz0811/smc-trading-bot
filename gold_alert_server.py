#!/usr/bin/env python3
"""
SMC Gold Engine v1 — XAU/USD 24/7 Alert Server
FIXED: crypto logging, chop filter, ML thresholds, score thresholds
"""
import os, sys, json, time, logging, threading, requests, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
import math
from http.server import HTTPServer, BaseHTTPRequestHandler

TG_TOKEN   = os.environ.get('TG_TOKEN','')
TG_CHAT    = os.environ.get('TG_CHAT','')
PORT       = int(os.environ.get('PORT', 8080))
SCAN_EVERY = int(os.environ.get('SCAN_EVERY', 1))
COOLDOWN_M       = int(os.environ.get('COOLDOWN_M', 60))
SCALP_COOLDOWN_M = int(os.environ.get('SCALP_COOLDOWN_M', 90))
GOLD_YF    = 'https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF'

# Crypto pairs
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
CRYPTO_SCALP_MIN_SCORE = float(os.environ.get('CRYPTO_SCALP_MIN_SCORE', '7.5'))   # FIX: lowered from 8.5
CRYPTO_SCALP_DAILY_CAP = int(os.environ.get('CRYPTO_SCALP_DAILY_CAP',   '3'))
CRYPTO_SCALP_TP1       = float(os.environ.get('CRYPTO_SCALP_TP1',       '1.5'))
CRYPTO_SCALP_TP2       = float(os.environ.get('CRYPTO_SCALP_TP2',       '2.0'))
CRYPTO_SCALP_MAX_SL    = float(os.environ.get('CRYPTO_SCALP_MAX_SL',    '0.008'))

# FIX: lowered from 6.0 to 5.5 — prevents chop filter blocking everything
GOLD_MIN_SCORE = float(os.environ.get('GOLD_MIN_SCORE', 5.5))

SCALP_MODE   = os.environ.get('SCALP_MODE','true').lower()=='true'
PAPER_MODE   = os.environ.get('PAPER_MODE','true').lower()=='true'
PAPER_TARGET = int(os.environ.get('PAPER_TARGET','30'))

TP1_MULT = 1.5 if SCALP_MODE else 2.0
TP2_MULT = 2.0 if SCALP_MODE else 2.8
TP3_MULT = 2.5 if SCALP_MODE else 3.5
MAX_SL_PCT = 0.005 if SCALP_MODE else 0.008

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger('gold')

# ═══ FILE PATHS ══════════════════════════════════════════════════
import os as _os
_os.environ.setdefault('LEARN_FILE',       '/app/gold_learning.json')
_os.environ.setdefault('DEEP_LEARN_FILE',  '/app/gold_deep.json')
_os.environ.setdefault('JOURNAL_FILE',     '/app/gold_journal.json')
LEARN_FILE      = _os.environ['LEARN_FILE']
DEEP_LEARN_FILE = _os.environ['DEEP_LEARN_FILE']
JOURNAL_FILE    = _os.environ['JOURNAL_FILE']
SCALP_LEARN_FILE = os.environ.get('SCALP_LEARN_FILE', '/app/gold_scalp_ml.json')

# ═══ SELF-LEARNING ENGINE ═══════════════════════════════════════
DEFAULT_WEIGHTS = {
    'setup_scores': {
        'SWEEP_OB':        8.0,
        'HTF_CONFLUENCE':  8.0,
        'CHOCH':           8.0,
        'BOS':             7.0,
    },
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
    'session_weights': {
        'London':    1.2,
        'New York':  1.2,
        'Asian':     0.8,
        'Weekend':   0.5,
    },
    'rsi_zones': {
        '20-30': 1.3,
        '30-40': 1.1,
        '40-50': 1.0,
        '50-60': 0.9,
        '60-70': 1.1,
        '70-80': 1.3,
    },
    'weekly_bias_mult': {
        'bullish': 1.2,
        'neutral': 0.9,
        'bearish': 0.7,
    },
    'min_score_session': {
        'London':    5.5,   # FIX: lowered from 6.0
        'New York':  5.5,   # FIX: lowered from 6.0
        'Asian':     6.5,   # FIX: lowered from 7.0
        'Weekend':   8.0,
    }
}

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
        'signals': [],
        'outcomes': [],
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
        'learning_log': [],
        'last_learned': None,
    }

def save_db(db):
    try:
        with open(LEARN_FILE, 'w') as f:
            json.dump(db, f, indent=2)
    except Exception as e:
        log.warning(f"DB save error: {e}")

def log_signal(sig, pair, session):
    db = load_db()
    rsi_zone = get_rsi_zone(sig.get('rsi_val', 50))
    entry = {
        'id':        f"{pair['sym']}_{int(time.time())}",
        'sym':       pair['sym'],
        'setup':     sig['setup'],
        'dir':       sig['dir'],
        'score':     sig['score'],
        'raw_score': sig.get('raw_score', sig['score']),
        'conf':      sig['conf'],
        'entry':     sig['price'],
        'sl':        sig['sl'],
        'tp1':       sig['tp1'],
        'tp2':       sig['tp'],
        'tp3':       sig['tp3'],
        'rr':        sig['rr'],
        'risk_pct':  sig['risk_pct'],
        'tags':      sig.get('tags', []),
        'session':   session,
        'weekly':    sig.get('weekly', 'neutral'),
        'daily':     sig.get('daily', 'neutral'),
        'rsi_val':   sig.get('rsi_val', 50),
        'rsi_zone':  rsi_zone,
        'time':      datetime.now(timezone.utc).isoformat(),
        'status':    'open',
        'exit_price':None,
        'exit_time': None,
        'pnl':       None,
        'result':    None,
        'bars_held': None,
    }
    db['signals'].append(entry)
    db['stats']['total_signals'] += 1
    save_db(db)
    return entry['id']

def close_trade(trade_id, result, exit_price, bars_held=0):
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
    db['stats']['total_trades'] += 1
    db['stats']['total_pnl']    = round(db['stats']['total_pnl'] + pnl, 3)
    if result == 'win':    db['stats']['wins']   += 1
    elif result == 'loss': db['stats']['losses'] += 1
    else:                  db['stats']['be']     += 1
    _update_dimension(db, 'by_setup',    sig['setup'],    result, pnl)
    _update_dimension(db, 'by_session',  sig['session'],  result, pnl)
    _update_dimension(db, 'by_rsi_zone', sig['rsi_zone'], result, pnl)
    _update_dimension(db, 'by_weekly',   sig['weekly'],   result, pnl)
    score_bucket = f"{int(sig['score'])}-{int(sig['score'])+1}"
    _update_dimension(db, 'by_score_range', score_bucket, result, pnl)
    for tag in sig.get('tags', []):
        _update_dimension(db, 'by_tag', tag, result, pnl)
    db['outcomes'].append(sig)
    save_db(db)
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
    if result == 'win':    d['w'] += 1
    elif result == 'loss': d['l'] += 1
    else:                  d['be'] += 1

def learn(db):
    outcomes = [s for s in db['signals'] if s['result']]
    if len(outcomes) < 10:
        return
    lr = 0.15
    changes = []
    for setup, stats in db['stats']['by_setup'].items():
        if stats['total'] < 5: continue
        wr = stats['w'] / stats['total']
        avg_pnl = stats['pnl'] / stats['total']
        current_score = db['weights']['setup_scores'].get(setup, 7.0)
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
    for sess, stats in db['stats']['by_session'].items():
        if stats['total'] < 5: continue
        wr = stats['w'] / stats['total']
        current_m = db['weights']['session_weights'].get(sess, 1.0)
        target_m = 0.6 + wr * 1.2
        new_m = round(current_m + lr * (target_m - current_m), 2)
        new_m = max(0.3, min(1.5, new_m))
        if abs(new_m - current_m) > 0.05:
            db['weights']['session_weights'][sess] = new_m
            changes.append(f"{'↑' if new_m>current_m else '↓'} session '{sess}' mult {current_m:.2f}→{new_m:.2f} (WR:{wr:.0%})")
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

def compute_learned_score(setup, tags, session, weekly, rsi_val, base_score):
    db = load_db()
    w = db['weights']
    score = w['setup_scores'].get(setup, base_score)
    for tag in tags:
        tag_clean = tag.split('RSI')[0].strip()
        score += w['tag_weights'].get(tag_clean, 0.3)
    score *= w['session_weights'].get(session, 1.0)
    zone = get_rsi_zone(rsi_val)
    score *= w['rsi_zones'].get(zone, 1.0)
    score *= w['weekly_bias_mult'].get(weekly, 1.0)
    return round(min(10, score), 1)

def get_min_score(session):
    db = load_db()
    return db['weights']['min_score_session'].get(session, 5.5)  # FIX: default 5.5

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

def performance_report():
    db = load_db()
    s = db['stats']
    total = s['total_trades']
    if total == 0:
        return "📊 No completed trades yet. Learning begins after first trade closes."
    wr = s['wins']/total*100 if total else 0
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
        lines.append(f"  {setup}: {st['total']}tr WR:{wr_s:.0f}% {bar}")
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
        for log_e in week_logs[-3:]:
            for ch in log_e['changes'][:3]:
                lines.append(f"  {ch}")
    lines.append(f"\n⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    lines.append("📡 <b>SMC Engine — Self Learning v3</b>")
    return '\n'.join(lines)

# ═══ DEEP LEARNING ENGINE ═══════════════════════════════════════
DEEP_LEARN_FILE = os.environ.get('DEEP_LEARN_FILE', '/app/gold_deep.json')

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
        'trades': [],
        'patterns': {},
        'thresholds': {
            'min_volume_ratio':  1.15,
            'min_rsi_buy_max':   62,
            'max_rsi_buy_min':   25,
            'min_sweep_size':    0.28,
            'max_bars_retest':   8,
            'min_atr_ratio':     0.30,   # FIX: lowered from 0.40
            'min_score':         5.5,    # FIX: lowered from 7.0
            'best_sessions':     ['London', 'New York'],
            'avoid_weekly':      [],
        },
        'condition_stats': {},
        'insights': [],
    }

def save_deep_db(db):
    try:
        with open(DEEP_LEARN_FILE, 'w') as f:
            json.dump(db, f, indent=2)
    except Exception as e:
        log.warning(f"Deep DB save error: {e}")

def fetch_candles_at_time(pair_cg, pair_kr, timestamp, limit=100):
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

def extract_metrics_from_chart(kl, trade):
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
    ema_aligned = (price > e20_a[i] > e50_a[i]) if is_buy else (price < e20_a[i] < e50_a[i])
    ema_stack   = (e9_a[i] > e20_a[i] > e50_a[i]) if (is_buy and e9_a[i]) else \
                  (e9_a[i] < e20_a[i] < e50_a[i]) if (not is_buy and e9_a[i]) else False
    macd_ok = (ht_a[i] > 0) if is_buy else (ht_a[i] < 0) if ht_a[i] else False
    rsi_zone = ('oversold' if rsi_val < 35 else
                'neutral'  if rsi_val < 65 else 'overbought')
    body    = abs(kl[i]['c'] - kl[i]['o'])
    rng     = kl[i]['h'] - kl[i]['l'] + 1e-10
    body_pct = round(body/rng*100, 1)
    sweep_size   = 0.0
    bars_retest  = 0
    ob_quality   = 'none'
    if trade.get('setup') == 'SWEEP_OB':
        swept_lvl = trade.get('swept_price', price)
        if swept_lvl and atr_a[i]:
            sweep_wick = abs(swept_lvl - min(kl[max(0,i-5):i+1], key=lambda x:x['l'])['l'])
            sweep_size = round(sweep_wick / atr_a[i], 2)
        bars_retest = trade.get('bars_since_sweep', 0)
        ob_top = trade.get('ob_top', 0)
        ob_bot = trade.get('ob_bot', 0)
        if ob_top and ob_bot and atr_a[i]:
            ob_size = ob_top - ob_bot
            ob_quality = 'clean' if ob_size > atr_a[i]*0.3 else 'small'
    recent6 = kl[max(0,i-6):i+1]
    bear_candles = sum(1 for x in recent6 if x['c'] < x['o'])
    bull_candles = len(recent6) - bear_candles
    trend_pressure = 'strong_bear' if bear_candles >= 5 else \
                     'strong_bull' if bull_candles >= 5 else 'mixed'
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

def learn_from_trade(trade, result, kl=None):
    db = load_deep_db()
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
    if metrics:
        _update_condition_stats(db, metrics, result)
    total = len([t for t in db['trades'] if t.get('result')])
    if total >= 10 and total % 5 == 0:
        insights = _find_patterns(db)
        if insights:
            db['insights'].extend(insights)
            _update_thresholds(db)
    save_deep_db(db)
    return record

def _update_condition_stats(db, metrics, result):
    conditions_to_track = {
        'session':        metrics.get('session'),
        'weekly_bias':    metrics.get('weekly_bias'),
        'rsi_zone':       metrics.get('rsi_zone'),
        'ema_aligned':    str(metrics.get('ema_aligned')),
        'macd_ok':        str(metrics.get('macd_ok')),
        'ob_quality':     metrics.get('ob_quality'),
        'trend_pressure': metrics.get('trend_pressure'),
        'high_volume':    str(metrics.get('volume_ratio', 0) >= 1.5),
        'very_high_vol':  str(metrics.get('volume_ratio', 0) >= 2.0),
        'clean_rsi_buy':  str(metrics.get('rsi', 50) < 45 and metrics.get('direction')=='BUY'),
        'no_trend_press': str(metrics.get('trend_pressure') == 'mixed'),
        'low_consec':     str(metrics.get('consec_against', 0) <= 2),
    }
    for key, val in conditions_to_track.items():
        if val is None: continue
        stat_key = f"{key}:{val}"
        if stat_key not in db['condition_stats']:
            db['condition_stats'][stat_key] = {'w':0,'l':0,'be':0,'total':0}
        s = db['condition_stats'][stat_key]
        s['total'] += 1
        if result == 'win':    s['w'] += 1
        elif result == 'loss': s['l'] += 1
        else:                  s['be'] += 1

def _find_patterns(db):
    insights = []
    stats = db['condition_stats']
    for key, s in stats.items():
        if s['total'] < 5: continue
        wr = s['w'] / s['total']
        if wr >= 0.65 and s['total'] >= 5:
            insights.append({
                'type': 'positive',
                'condition': key,
                'wr': round(wr*100),
                'trades': s['total'],
                'message': f"✅ When {key} → WR {round(wr*100)}% ({s['total']} trades)"
            })
        elif wr <= 0.30 and s['total'] >= 5:
            insights.append({
                'type': 'negative',
                'condition': key,
                'wr': round(wr*100),
                'trades': s['total'],
                'message': f"❌ When {key} → WR only {round(wr*100)}% — AVOID ({s['total']} trades)"
            })
    return insights[-10:] if insights else []

def _update_thresholds(db):
    stats  = db['condition_stats']
    thresh = db['thresholds']
    changes = []
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
    if changes:
        db['insights'].append({
            'type': 'threshold_update',
            'time': datetime.now(timezone.utc).isoformat(),
            'changes': changes
        })
    db['thresholds'] = thresh

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
    t = db['thresholds']
    lines += [
        "\n<b>⚙️ Learned Thresholds:</b>",
        f"  Min volume ratio: {t['min_volume_ratio']}x avg",
        f"  Best sessions: {', '.join(t['best_sessions'])}",
        f"  Avoid weekly: {', '.join(t['avoid_weekly']) or 'none'}",
        f"  Min score: {t['min_score']}",
    ]
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
    return load_deep_db()['thresholds']

# ═══ JOURNAL ═════════════════════════════════════════════════════
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
        log.warning(f"Journal save error: {e}")

def journal_log_signal(sig, pair):
    try:
        j = load_journal()
        entry = {
            'id':         f"{pair['sym']}_{int(time.time())}",
            'sym':        pair['sym'],
            'setup':      sig['setup'],
            'setup_name': sig.get('setup_name', sig['setup']),
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
        log.warning(f"Journal log error: {e}")

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
        s = j['stats'].setdefault(sig['setup'],{'wins':0,'losses':0,'be':0,'total':0,'total_pnl':0})
        s['total']+=1; s['total_pnl']=round(s['total_pnl']+pnl,3)
        if result=='win': s['wins']+=1
        elif result=='loss': s['losses']+=1
        else: s['be']+=1
        save_journal(j)
    except Exception as e:
        log.warning(f"Journal close error: {e}")

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
            lines+=[
                f"🏆 Best:  {best['sym']} +{best.get('pnl',0):.2f}% ({best['setup']})",
                f"💔 Worst: {worst['sym']} {worst.get('pnl',0):.2f}% ({worst['setup']})"]
        if j['open']:
            lines.append(f"\n🔓 Open: {', '.join(j['open'].keys())}")
        lines.append(f"\n⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC  |  📡 SMC Engine Pro v3")
        return '\n'.join(lines)
    except Exception as e:
        return f"Journal error: {e}"

# ═══ DATA PERSISTENCE ════════════════════════════════════════════
import base64, zlib

<<<<<<< HEAD
def get_data_files():
    files = {
        'smc_learning': os.environ.get('LEARN_FILE',      '/app/gold_learning.json'),
        'smc_deep':     os.environ.get('DEEP_LEARN_FILE', '/app/gold_deep.json'),
        'smc_journal':  os.environ.get('JOURNAL_FILE',    '/app/gold_journal.json'),
    }
    scalp = os.environ.get('SCALP_LEARN_FILE', '/app/gold_scalp_ml.json')
=======
# All data files that must survive redeploys
def get_data_files():
    """Build data files dict at runtime — all vars guaranteed defined by then"""
    files = {
        'smc_learning': os.environ.get('LEARN_FILE',      '/data/smc_learning.json'),
        'smc_deep':     os.environ.get('DEEP_LEARN_FILE', '/data/smc_deep_learning.json'),
        'smc_journal':  os.environ.get('JOURNAL_FILE',    '/data/smc_journal.json'),
    }
    scalp = os.environ.get('SCALP_LEARN_FILE', '/data/gold_scalp_ml.json')
>>>>>>> d6b4f602b0e5ed08cf8c4f22c86398bcb286e865
    if scalp:
        files['gold_scalp'] = scalp
    return files

def _tg_send_file(filepath, caption):
    if not TG_TOKEN or not TG_CHAT: return False
    if not Path(filepath).exists(): return False
    try:
        with open(filepath, 'rb') as f:
            data = f.read()
        fname = Path(filepath).name
        r = requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendDocument',
            data={'chat_id': TG_CHAT, 'caption': caption},
            files={'document': (fname, data, 'application/json')},
            timeout=30
        )
        return r.ok
    except Exception as e:
        log.warning(f"TG file send error: {e}")
        return False

def _tg_get_latest_file(filename_pattern):
    if not TG_TOKEN or not TG_CHAT: return None
    try:
        r = requests.get(
            f'https://api.telegram.org/bot{TG_TOKEN}/getUpdates',
            params={'limit': 100, 'timeout': 5},
            timeout=10
        )
        if not r.ok: return None
        msgs = r.json().get('result', [])
        for upd in reversed(msgs):
            msg = upd.get('message', {})
            doc = msg.get('document', {})
            if doc.get('file_name', '').startswith(filename_pattern.replace('.json','')):
                file_id = doc['file_id']
                r2 = requests.get(
                    f'https://api.telegram.org/bot{TG_TOKEN}/getFile',
                    params={'file_id': file_id}, timeout=10
                )
                if not r2.ok: continue
                file_path = r2.json()['result']['file_path']
                r3 = requests.get(
                    f'https://api.telegram.org/file/bot{TG_TOKEN}/{file_path}',
                    timeout=15
                )
                if r3.ok: return r3.text
    except Exception as e:
        log.warning(f"TG file restore error: {e}")
    return None

def backup_data_to_tg(silent=False):
    backed_up = []
    for name, filepath in get_data_files().items():
        if not Path(filepath).exists(): continue
        try:
            size = Path(filepath).stat().st_size
            if size < 10: continue
            caption = (
                f"💾 <b>SMC Data Backup</b> — {name}\n"
                f"Size: {size:,} bytes\n"
                f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
            )
            ok = _tg_send_file(filepath, caption)
            if ok:
                backed_up.append(name)
                log.info(f"  💾 Backed up {name} ({size:,} bytes)")
        except Exception as e:
            log.warning(f"Backup {name}: {e}")
    if backed_up and not silent:
        send_tg(
            f"💾 <b>Data Backup Complete</b>\n\n"
            f"Backed up: {', '.join(backed_up)}\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
        )
    return backed_up

def restore_data_from_tg():
    restored = []; missing = []
<<<<<<< HEAD
=======

>>>>>>> d6b4f602b0e5ed08cf8c4f22c86398bcb286e865
    for name, filepath in get_data_files().items():
        if Path(filepath).exists():
            size = Path(filepath).stat().st_size
            if size > 10:
                log.info(f"  ✓ {name}: exists ({size:,} bytes)")
                continue
        log.info(f"  ⚠ {name}: missing — searching TG for backup...")
        fname = Path(filepath).name
        content = _tg_get_latest_file(fname)
        if content:
            try:
                parsed = json.loads(content)
                Path(filepath).parent.mkdir(parents=True, exist_ok=True)
                with open(filepath, 'w') as f:
                    json.dump(parsed, f, indent=2)
                size = len(content)
                log.info(f"  ✅ Restored {name} from TG ({size:,} bytes)")
                restored.append(name)
            except Exception as e:
                log.warning(f"  ✗ Restore {name} failed: {e}")
                missing.append(name)
        else:
            log.info(f"  ℹ No TG backup found for {name} — starting fresh")
            missing.append(name)
    return restored, missing

def start_backup_loop():
    def _loop():
        time.sleep(600)
        while True:
            try:
                backed = backup_data_to_tg(silent=True)
                if backed:
                    log.info(f"Auto-backup: {', '.join(backed)}")
            except Exception as e:
                log.warning(f"Auto-backup error: {e}")
            time.sleep(6 * 3600)
    threading.Thread(target=_loop, daemon=True).start()
    log.info("✓ Auto-backup loop started (every 6h)")

def check_circuit_breaker():
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

# ═══ SCALP ML ════════════════════════════════════════════════════
def _scalp_load():
    try:
        if Path(SCALP_LEARN_FILE).exists():
            with open(SCALP_LEARN_FILE) as f: return json.load(f)
    except: pass
    return {
        'version': 1,
        'trades': [],
        'total': 0, 'wins': 0, 'losses': 0, 'be': 0,
        'weights': {
            'session_london':    1.0,
            'session_ny':        1.0,
            'session_asian':     0.6,
            'rsi_oversold':      1.2,
            'rsi_neutral':       1.0,
            'rsi_overbought':    1.2,
            'vol_high':          1.2,
            'vol_normal':        1.0,
            'ema_aligned':       1.1,
            'ob_present':        1.2,
            'sweep_present':     1.2,
            'macd_aligned':      1.1,
            'weekly_aligned':    1.1,
            'weekly_against':    0.7,
            'base_score_8':      1.0,
            'base_score_85':     1.1,
            'base_score_9':      1.3,
        },
        'insights': [],
        'last_update': None,
        'update_count': 0,
    }

def _scalp_save(db):
    try:
        with open(SCALP_LEARN_FILE, 'w') as f: json.dump(db, f, indent=2)
    except Exception as e:
        log.warning(f"Scalp ML save: {e}")

def scalp_adjusted_score(sig, session):
    db = _scalp_load()
    w  = db['weights']
    base = sig['score']
    sess_w = w.get(f'session_{session.lower().replace(" ","_").replace("new_york","ny")}',
                   w.get('session_london', 1.0))
    ri = sig.get('rsi_val', 50)
    is_buy = sig['dir'] == 'BUY'
    if is_buy:
        rsi_w = w['rsi_oversold'] if ri<35 else (w['rsi_neutral'] if ri<55 else 0.8)
    else:
        rsi_w = w['rsi_overbought'] if ri>65 else (w['rsi_neutral'] if ri>45 else 0.8)
    tags = sig.get('tags', [])
    vol_w   = w['vol_high']    if any('Vol++' in t for t in tags) else w['vol_normal']
    ema_w   = w['ema_aligned'] if any('EMA' in t for t in tags) else 1.0
    ob_w    = w['ob_present']  if 'OB_Retest' in tags else 1.0
    sw_w    = w['sweep_present'] if any('Sweep' in t for t in tags) else 1.0
    macd_w  = w['macd_aligned'] if any('MACD' in t for t in tags) else 1.0
    weekly = sig.get('weekly', 'neutral')
    wk_w = w['weekly_aligned'] if (
        (is_buy and weekly=='bullish') or (not is_buy and weekly=='bearish')
    ) else (w['weekly_against'] if (
        (is_buy and weekly=='bearish') or (not is_buy and weekly=='bullish')
    ) else 1.0)
    sc_w = w['base_score_9'] if base>=9 else (w['base_score_85'] if base>=8.5 else w['base_score_8'])
    multiplier = (sess_w * rsi_w * vol_w * ema_w * ob_w * sw_w * macd_w * wk_w * sc_w) ** (1/9)
    adjusted = round(min(100, base * multiplier * 10), 1)
    log.info(f"  ML score: raw={base} sess_w={sess_w:.2f} mult={multiplier:.3f} adj={adjusted:.1f}")
    return adjusted

def scalp_record_trade(sig, session, result, pnl, kl=None):
    db = _scalp_load()
    tags = sig.get('tags', [])
    ri = sig.get('rsi_val', 50)
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
        'result':   result,
        'pnl':      pnl,
    }
    db['trades'].append(record)
    db['total']  += 1
    if result=='win':    db['wins']   += 1
    elif result=='loss': db['losses'] += 1
    else:                db['be']     += 1
    if db['total'] >= 5 and db['total'] % 5 == 0:
        _update_scalp_weights(db)
    _scalp_save(db)
    return record

def _update_scalp_weights(db):
    trades = db['trades']
    if len(trades) < 5: return
    lr = 0.12
    w  = db['weights']
    changes = []

    def wr_for(condition_fn):
        subset = [t for t in trades if condition_fn(t)]
        if len(subset) < 3: return None
        wins = sum(1 for t in subset if t['result']=='win')
        return wins / len(subset), len(subset)

    for sess_key, condition in [
        ('session_london',  lambda t: t['session']=='London'),
        ('session_ny',      lambda t: t['session']=='New York'),
        ('session_asian',   lambda t: t['session']=='Asian'),
    ]:
        result = wr_for(condition)
        if result:
            wr, n = result
            target = 0.5 + wr * 1.0
            old = w[sess_key]
            w[sess_key] = round(max(0.3, min(1.8, old + lr*(target-old))), 3)
            if abs(w[sess_key]-old) > 0.02:
                changes.append(f"{sess_key}: {old:.2f}→{w[sess_key]:.2f} (WR:{wr:.0%} n={n})")

    for rsi_key, condition in [
        ('rsi_oversold', lambda t: (t['dir']=='BUY' and t['rsi']<35) or (t['dir']=='SELL' and t['rsi']>65)),
        ('rsi_neutral',  lambda t: 35<=t['rsi']<=65),
    ]:
        result = wr_for(condition)
        if result:
            wr, n = result
            target = 0.5 + wr
            old = w[rsi_key]
            w[rsi_key] = round(max(0.5, min(2.0, old + lr*(target-old))), 3)
            if abs(w[rsi_key]-old) > 0.02:
                changes.append(f"{rsi_key}: {old:.2f}→{w[rsi_key]:.2f} (WR:{wr:.0%})")

    result_hvol = wr_for(lambda t: any('Vol++' in x for x in t.get('tags',[])))
    if result_hvol:
        wr, n = result_hvol
        old = w['vol_high']
        w['vol_high'] = round(max(0.5, min(2.0, old + lr*(0.5+wr-old))), 3)
        if abs(w['vol_high']-old) > 0.02:
            changes.append(f"vol_high: {old:.2f}→{w['vol_high']:.2f} (WR:{wr:.0%})")

    result_ob = wr_for(lambda t: 'OB_Retest' in t.get('tags',[]))
    if result_ob:
        wr, n = result_ob
        old = w['ob_present']
        w['ob_present'] = round(max(0.5, min(2.0, old + lr*(0.5+wr-old))), 3)
        if abs(w['ob_present']-old) > 0.02:
            changes.append(f"ob_present: {old:.2f}→{w['ob_present']:.2f} (WR:{wr:.0%})")

    result_sw = wr_for(lambda t: any('Sweep' in x for x in t.get('tags',[])))
    if result_sw:
        wr, n = result_sw
        old = w['sweep_present']
        w['sweep_present'] = round(max(0.5, min(2.0, old + lr*(0.5+wr-old))), 3)
        if abs(w['sweep_present']-old) > 0.02:
            changes.append(f"sweep_present: {old:.2f}→{w['sweep_present']:.2f} (WR:{wr:.0%})")

    for sc_key, condition in [
        ('base_score_9',  lambda t: t['score']>=9.0),
        ('base_score_85', lambda t: 8.5<=t['score']<9.0),
        ('base_score_8',  lambda t: 8.0<=t['score']<8.5),
    ]:
        result = wr_for(condition)
        if result:
            wr, n = result
            old = w[sc_key]
            w[sc_key] = round(max(0.5, min(2.0, old + lr*(0.5+wr-old))), 3)
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
        f"  London:      {weights['session_london']:.2f}x",
        f"  New York:    {weights['session_ny']:.2f}x",
        f"  Asian:       {weights['session_asian']:.2f}x",
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

def htf_bias_fn(kl, i, f=5):
    if i<f*25: return 'neutral'
    htf=[kl[j*f+f-1]['c'] for j in range((i+1)//f)]
    if len(htf)<25: return 'neutral'
    e20=ema(htf,20); e50=ema(htf,50); n=len(htf)-1
    if not e20[n] or not e50[n]: return 'neutral'
    if htf[n]>e20[n]>e50[n]: return 'bullish'
    if htf[n]<e20[n]<e50[n]: return 'bearish'
    return 'neutral'

def swings(kl, lb=5):
    sh = []; sl = []
    for i in range(lb, len(kl)-lb):
        if all(kl[i]['h'] >= kl[j]['h'] for j in range(i-lb, i+lb+1) if j != i):
            sh.append((i, kl[i]['h']))
        if all(kl[i]['l'] <= kl[j]['l'] for j in range(i-lb, i+lb+1) if j != i):
            sl.append((i, kl[i]['l']))
    return sh, sl

# ═══ GOLD DATA FETCH ═════════════════════════════════════════════
def fetch_gold_candles(limit=200):
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
            v=ohlcv.get('volume',[0]*len(ts))[j] or 50.0
            if None in (o,h,l,c_): continue
            kl.append({'t':int(ts[j]),'o':float(o),'h':float(h),
                       'l':float(l),'c':float(c_),'v':float(v)})
        return kl[-limit:] if len(kl)>=30 else None
    except Exception as e:
        log.warning(f"Gold fetch error: {e}")
        return None

def fetch_gold_price():
    try:
        url = f'{GOLD_YF}?interval=1m&range=1d'
        req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
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
    now = datetime.now(timezone.utc)
    d   = now.weekday()
    h   = now.hour
    if d == 5: return False, 'Closed (Saturday)'
    if d == 6 and h < 23: return False, f'Closed (Sunday — opens at 23:00 UTC)'
    if d == 4 and h >= 21: return False, 'Closed (Friday close)'
    sess = get_gold_session()
    return True, sess

def is_scalp_session():
    open_, sess = gold_market_open()
    return open_ and sess in ('London', 'New York')

# ═══ GOLD CHOP FILTER — FIXED ════════════════════════════════════
def gold_chop(atr_a, i, thresh=0.25):
    """
    FIX: threshold lowered from 0.40 to 0.25.
    Gold at ATH has smaller ATR relative to avg — 0.40 was too aggressive.
    """
<<<<<<< HEAD
=======
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

def build_crypto_scalp_msg(sig, count_today):
    """Crypto scalp TG message — clearly labelled SCALP with full TP/SL"""
    ib   = sig['dir'] == 'BUY'
    e    = sig['entry']
    sl   = sig['sl']
    tp1  = sig['tp1']
    tp2  = sig['tp']
    tp3  = sig['tp3']
    risk = abs(e - sl)
    tp1p = round(abs(tp1-e)/e*100, 2)
    tp2p = round(abs(tp2-e)/e*100, 2)
    tp3p = round(abs(tp3-e)/e*100, 2)
    slp  = round(abs(sl-e)/e*100, 2)
    em   = '⚡' if sig['setup']=='SWEEP_OB' else '🔄'
    return '\n'.join(filter(None, [
        f"{'🟢' if ib else '🔴'} <b>⚡ SCALP {'BUY' if ib else 'SELL'} — {sig['sym']}/USD</b>  <code>[SCALP #{count_today}/{CRYPTO_SCALP_DAILY_CAP}]</code>",
        f"{em} <b>{sig.get('setup_name','Scalp Setup')}</b>  |  Score: {sig['score']}/10  |  ML: {sig.get('ml_conf',0):.0f}%",
        f"📅 Session: {sig.get('session','—')}  |  RSI: {sig.get('rsi_val','—')}  |  Weekly: {sig.get('weekly','—')}",
        "",
        "💰 <b>Scalp Levels</b>",
        f"  Entry:  <code>{fp_crypto(e)}</code>",
        f"  SL:     <code>{fp_crypto(sl)}</code>  <i>(-{slp}% — below wick)</i>",
        f"  TP1:    <code>{fp_crypto(tp1)}</code>  <i>(+{tp1p}% — 1:1.5 — close 70%)</i>",
        f"  TP2:    <code>{fp_crypto(tp2)}</code>  <i>(+{tp2p}% — 1:2.0 — runner 30%)</i>",
        f"  TP3:    <code>{fp_crypto(tp3)}</code>  <i>(+{tp3p}% — 1:2.5 — let go)</i>",
        "",
        f"📖 <b>Why this scalp:</b>",
        sig.get('why', '  SMC confluence setup'),
        "",
        f"🔍 {esc(' · '.join(sig.get('tags',[])))}" if sig.get('tags') else None,
        sig.get('ob') and f"  OB: {fp_crypto(sig['ob']['bot'])} – {fp_crypto(sig['ob']['top'])}",
        sig.get('wick_sl') and f"  Swept at: {fp_crypto(sig['wick_sl'])}",
        "",
        "⚡ <i>SCALP — aim TP1 first. Move SL to entry at TP1. Exit at session close if not hit.</i>",
        f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC  |  📡 <b>SMC Crypto Scalp</b>",
    ]))


def run_crypto_scalp_scan():
    """
    Scan all 10 crypto pairs for scalp signals.
    Only fires during London/NY session.
    Max 3 scalps/coin/day.
    Tracks each in open_trades['SCALP_BTC'] etc.
    """
    # No hard session block — ML handles session weighting
    # is_scalp_session() result passed to ML for score adjustment
    current_session = get_gold_session()
    log.debug(f"Crypto scalp scan — session: {current_session}")

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if today != state.get('scalp_crypto_day'):
        state['scalp_crypto_day']   = today
        state['scalp_crypto_count'] = {}  # {sym: count}
        state['scalp_crypto_total'] = 0   # total across all coins today

    # Global daily cap: max 6 crypto scalps per day across ALL coins
    GLOBAL_DAILY_CAP = int(os.environ.get('CRYPTO_SCALP_GLOBAL_CAP', 6))
    if state.get('scalp_crypto_total', 0) >= GLOBAL_DAILY_CAP:
        log.debug(f"Global scalp cap hit ({GLOBAL_DAILY_CAP}/day) — skip crypto scan")
        return

    signals_sent = 0
    for pair in CRYPTO_PAIRS:
        sym = pair['sym']
        # Daily cap check
        if state['scalp_crypto_count'].get(sym, 0) >= CRYPTO_SCALP_DAILY_CAP:
            continue
        # Cooldown: don't fire same coin twice within COOLDOWN_M
        lf = last_fired.get(f'SCALP_{sym}', {})
        if lf and time.time()-lf.get('time',0) < SCALP_COOLDOWN_M*60:
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
            # ML conf gate — threshold lower for active sessions, higher for weak ones
            sess_now = get_gold_session()
            ml_threshold = 60 if sess_now in ('London', 'New York') else 72
            if sig['ml_conf'] < ml_threshold:
                log.debug(f"  {sym}: low ML conf {sig['ml_conf']:.0f}% < {ml_threshold} ({sess_now}) — skip")
                continue
            count = state['scalp_crypto_count'].get(sym, 0) + 1
            total = sum(state['scalp_crypto_count'].values()) + 1
            msg = build_crypto_scalp_msg(sig, count)
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
>>>>>>> d6b4f602b0e5ed08cf8c4f22c86398bcb286e865
    r=[x for x in atr_a[max(0,i-20):i] if x]
    if not r: return True
    avg_atr = sum(r)/len(r)
    current = atr_a[i]
    is_choppy = current < avg_atr * thresh
    if is_choppy:
        log.info(f"  Chop filter: ATR {current:.2f} < {avg_atr*thresh:.2f} (avg={avg_atr:.2f} thresh={thresh})")
    return is_choppy

# ═══ GOLD SIGNAL ENGINE ══════════════════════════════════════════
def compute_gold(kl):
    if not kl or len(kl)<60: return None
    n=len(kl); i=n-1
    closes=[k['c'] for k in kl]; vols=[k['v'] for k in kl]
    ri_a=rsi(closes); e9_a=ema(closes,9); e20_a=ema(closes,20); e50_a=ema(closes,50)
    ht_a=macd_hist(closes); at_a=calc_atr(kl); va_a=vol_avg(vols)
    if not all([ri_a[i],e9_a[i],e20_a[i],e50_a[i],at_a[i],va_a[i]]):
        log.info("  Gold: missing indicator values")
        return None
    if gold_chop(at_a,i):
        return None
    price=closes[i]; k=kl[i]
    at=float(at_a[i]); va=float(va_a[i])
    ri_v=float(ri_a[i]); e9=float(e9_a[i]); e20=float(e20_a[i]); e50=float(e50_a[i])
    ht=ht_a[i]
    sh,sl_sw=swings(kl[:i+1],5)
    weekly=htf_bias_fn(kl,i,21); daily=htf_bias_fn(kl,i,5)
    sess=get_gold_session()
    is_buy=None; score=0.0; tags=[]; setup=None; wick_sl=None; ob_hit=None

    log.info(f"  Gold engine: price={price:.2f} RSI={ri_v:.1f} ATR={at:.2f} "
             f"vol={k['v']:.0f}/avg={va:.0f} sess={sess} weekly={weekly} daily={daily}")

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

    if is_buy is None:
        log.info(f"  Gold: no setup triggered (swings={len(sh)}h/{len(sl_sw)}l)")
        return None

    log.info(f"  Gold setup found: {setup} {'BUY' if is_buy else 'SELL'} base_score={score:.1f}")

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

    log.info(f"  Gold final score={score:.1f} (min={GOLD_MIN_SCORE}) tags={tags}")

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

# ═══ CRYPTO SCALP ENGINE — FIXED ═════════════════════════════════
def fetch_crypto_candles(pair, limit=200):
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

    # FIX: chop filter threshold lowered from 0.40 to 0.25
    r_ = [x for x in at_a[max(0,i-20):i] if x]
    if not r_ or at < sum(r_)/len(r_)*0.25: return None

    sh_, sl_ = swings(kl[:i+1], 5)
    weekly = htf_bias_fn(kl, i, 21)
    daily  = htf_bias_fn(kl, i, 5)

    is_buy = None; score = 0.0; tags = []; setup = None; wick_sl = None; ob_hit = None

    # SETUP 1: Sweep + OB
    for li, lvl in [(ix,float(p)) for ix,p in sl_ if ix<i-1 and ix>i-50][-4:]:
        if not(k['l']<lvl<price) or lvl-k['l']<at*0.25 or k['v']<va*1.1: continue
        # FIX: removed strict daily=='bullish' requirement, only block strong bearish
        if weekly == 'bearish': continue
        if not(25<ri_v<65): continue
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
        # FIX: removed strict daily=='bearish' requirement
        if weekly == 'bullish': continue
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

    # SETUP 2: CHoCH
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

    # Confluences
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

    # SL placement
    if wick_sl is not None:
        sl_p = wick_sl - at*0.08 if is_buy else wick_sl + at*0.08
    else:
        rl2 = [(ix,float(p)) for ix,p in sl_ if ix<=i][-2:]
        rh2 = [(ix,float(p)) for ix,p in sh_ if ix<=i][-2:]
        sl_p = (min(p for _,p in rl2)-at*0.08) if (is_buy and rl2) else \
               (max(p for _,p in rh2)+at*0.08) if (not is_buy and rh2) else \
               (price-at*1.5 if is_buy else price+at*1.5)

    if is_buy  and (price-sl_p)>price*CRYPTO_SCALP_MAX_SL: sl_p=price-price*CRYPTO_SCALP_MAX_SL
    if not is_buy and (sl_p-price)>price*CRYPTO_SCALP_MAX_SL: sl_p=price+price*CRYPTO_SCALP_MAX_SL
    risk = abs(price-sl_p)
    if risk <= 0 or risk < price*0.001: return None

    tp1  = price+risk*CRYPTO_SCALP_TP1 if is_buy else price-risk*CRYPTO_SCALP_TP1
    tp2  = price+risk*CRYPTO_SCALP_TP2 if is_buy else price-risk*CRYPTO_SCALP_TP2
    tp3  = price+risk*2.5 if is_buy else price-risk*2.5
    if abs(tp2-price)/risk < 1.8: return None

    sess = get_gold_session()
    sig_tmp = {'dir':'BUY' if is_buy else 'SELL','score':score,'tags':tags,
               'rsi_val':round(ri_v,1),'weekly':weekly,'session':sess}
    ml_conf = scalp_adjusted_score(sig_tmp, sess)

    setup_names = {'SWEEP_OB':'⚡ Sweep+OB Retest','CHOCH':'🔄 CHoCH Reversal'}
    why_map = {
        'SWEEP_OB': (f"  1️⃣ Retail stops swept at {fp_crypto(wick_sl)}\n"
                     f"  2️⃣ Closed back above — stop hunt done\n"
                     (f"  3️⃣ OB zone: {fp_crypto(ob_hit['bot'])} – {fp_crypto(ob_hit['top'])}\n" if ob_hit else "")+
                     f"  4️⃣ SL below wick — tight risk\n"
                     f"  💡 Scalp: target TP1 fast, trail to TP2"),
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

def build_crypto_scalp_msg(sig, count_today):
    ib   = sig['dir'] == 'BUY'
    e    = sig['entry']
    sl   = sig['sl']
    tp1  = sig['tp1']
    tp2  = sig['tp']
    tp3  = sig['tp3']
    tp1p = round(abs(tp1-e)/e*100, 2)
    tp2p = round(abs(tp2-e)/e*100, 2)
    tp3p = round(abs(tp3-e)/e*100, 2)
    slp  = round(abs(sl-e)/e*100, 2)
    em   = '⚡' if sig['setup']=='SWEEP_OB' else '🔄'
    return '\n'.join(filter(None, [
        f"{'🟢' if ib else '🔴'} <b>⚡ SCALP {'BUY' if ib else 'SELL'} — {sig['sym']}/USD</b>  <code>[SCALP #{count_today}/{CRYPTO_SCALP_DAILY_CAP}]</code>",
        f"{em} <b>{sig.get('setup_name','Scalp Setup')}</b>  |  Score: {sig['score']}/10  |  ML: {sig.get('ml_conf',0):.0f}%",
        f"📅 Session: {sig.get('session','—')}  |  RSI: {sig.get('rsi_val','—')}  |  Weekly: {sig.get('weekly','—')}",
        "",
        "💰 <b>Scalp Levels</b>",
        f"  Entry:  <code>{fp_crypto(e)}</code>",
        f"  SL:     <code>{fp_crypto(sl)}</code>  <i>(-{slp}% — below wick)</i>",
        f"  TP1:    <code>{fp_crypto(tp1)}</code>  <i>(+{tp1p}% — 1:1.5 — close 70%)</i>",
        f"  TP2:    <code>{fp_crypto(tp2)}</code>  <i>(+{tp2p}% — 1:2.0 — runner 30%)</i>",
        f"  TP3:    <code>{fp_crypto(tp3)}</code>  <i>(+{tp3p}% — 1:2.5 — let go)</i>",
        "",
        f"📖 <b>Why this scalp:</b>",
        sig.get('why', '  SMC confluence setup'),
        "",
        f"🔍 {esc(' · '.join(sig.get('tags',[])))}" if sig.get('tags') else None,
        (f"  OB: {fp_crypto(sig['ob']['bot'])} – {fp_crypto(sig['ob']['top'])}") if sig.get('ob') else None,
        (f"  Swept at: {fp_crypto(sig['wick_sl'])}") if sig.get('wick_sl') else None,
        "",
        "⚡ <i>SCALP — aim TP1 first. Move SL to entry at TP1. Exit at session close if not hit.</i>",
        f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC  |  📡 <b>SMC Crypto Scalp</b>",
    ]))

# ═══ CRYPTO SCALP SCAN — FIXED LOGGING ═══════════════════════════
def run_crypto_scalp_scan():
    current_session = get_gold_session()
    # FIX: changed all debug → info so crypto activity shows in logs
    log.info(f"  [Crypto] Scanning {len(CRYPTO_PAIRS)} pairs — session: {current_session}")

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if today != state.get('scalp_crypto_day'):
        state['scalp_crypto_day']   = today
        state['scalp_crypto_count'] = {}
        state['scalp_crypto_total'] = 0

    GLOBAL_DAILY_CAP = int(os.environ.get('CRYPTO_SCALP_GLOBAL_CAP', 6))
    if state.get('scalp_crypto_total', 0) >= GLOBAL_DAILY_CAP:
        log.info(f"  [Crypto] Global daily cap hit ({GLOBAL_DAILY_CAP}) — skipping")
        return

    signals_sent = 0
    for pair in CRYPTO_PAIRS:
        sym = pair['sym']
        if state['scalp_crypto_count'].get(sym, 0) >= CRYPTO_SCALP_DAILY_CAP:
            continue
        lf = last_fired.get(f'SCALP_{sym}', {})
        if lf and time.time()-lf.get('time',0) < SCALP_COOLDOWN_M*60:
            remain = int((SCALP_COOLDOWN_M*60 - (time.time()-lf.get('time',0))) / 60)
            log.info(f"  [Crypto] {sym}: cooldown {remain}m remaining")
            continue
        if f'SCALP_{sym}' in state['open_trades']:
            log.info(f"  [Crypto] {sym}: already in open trade")
            continue
        try:
            kl = fetch_crypto_candles(pair, limit=200)
            if not kl or len(kl)<60:
                log.info(f"  [Crypto] {sym}: insufficient data ({len(kl) if kl else 0} candles)")
                continue
            sig = compute_crypto_scalp(kl, pair)
            if not sig:
                log.info(f"  [Crypto] {sym}: no setup (price={kl[-1]['c']:.4f})")
                continue

            # FIX: lowered ML confidence gate from 60 to 50 for active sessions, 60 for weak
            sess_now = get_gold_session()
            ml_threshold = 50 if sess_now in ('London', 'New York') else 60
            if sig['ml_conf'] < ml_threshold:
                log.info(f"  [Crypto] {sym}: ML conf {sig['ml_conf']:.0f}% < {ml_threshold} ({sess_now}) — skip")
                continue

            log.info(f"  [Crypto] {sym}: SIGNAL! {sig['setup']} {sig['dir']} score={sig['score']} ml={sig['ml_conf']:.0f}%")

            count = state['scalp_crypto_count'].get(sym, 0) + 1
            msg = build_crypto_scalp_msg(sig, count)
            ok  = send_tg(msg)
            if ok:
                last_fired[f'SCALP_{sym}'] = {'time':time.time(),'price':sig['price']}
                state['scalp_crypto_count'][sym] = count
                state['scalp_crypto_total'] = state.get('scalp_crypto_total', 0) + 1
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
                log.info(f"  [Crypto] {sym}: TG sent ✓ (#{count} today)")
        except Exception as e:
            log.error(f"  [Crypto] {sym} error: {e}")
        time.sleep(0.5)

    log.info(f"  [Crypto] Scan done: {signals_sent} signals sent, {len(state['open_trades'])} open trades")

def check_crypto_scalp_prices():
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

        if trade.get('tp2_hit') and not trade.get('tp3_hit'):
            if (ib and price>=tp3_p) or (not ib and price<=tp3_p):
                trade['tp3_hit']=True; pnl=round(abs(tp3_p-en)/en*100,3)
                send_tg(f"🚀 <b>SCALP RUNNER — {real_sym}/USD +{pnl}%</b>\nFull exit.  📡 SMC Crypto Scalp")
                continue

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

# ═══ HELPERS + TELEGRAM ══════════════════════════════════════════
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
        r2 = requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id':TG_CHAT,'text':msg[:4000],'disable_web_page_preview':True},
            timeout=10)
        return r2.ok
    except Exception as e:
        log.error(f"TG exception: {e}"); return False

state = {
    'started':     datetime.now(timezone.utc).isoformat(),
    'last_scan':   'Never',
    'scans_done':  0,
    'alerts_sent': 0,
    'open_trades': {},
    'stats':       {'wins':0,'losses':0,'be':0,'by_setup':{},'paper_count':0},
    'scalp_crypto_day':   '',
    'scalp_crypto_count': {},
    'scalp_crypto_total': 0,
}
last_fired = {}

def _hours_held(trade):
    try:
        secs=time.time()-datetime.fromisoformat(trade['time']).timestamp()
        return f"{int(secs//3600)}h {int((secs%3600)//60)}m"
    except: return '—'

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

def build_scalp_msg(sig):
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

# ═══ PRICE MONITORS ══════════════════════════════════════════════
def _check_scalp_trade():
    trade = state['open_trades'].get('XAU_SCALP')
    if not trade: return
    price = fetch_gold_price()
    if not price: return
    ib=trade['dir']=='BUY'; en=trade['entry']
    sl_p=trade['sl']; tp1_p=trade['tp1']; tp2_p=trade['tp']

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
            scalp_record_trade(trade, trade.get('session_name','Unknown'), 'win', pnl)
            lid=trade.get('learn_id')
            if lid: close_trade(lid,'win',price)
            journal_close_trade('XAU','win',price)
            state['stats']['wins']=state['stats'].get('wins',0)+1
            del state['open_trades']['XAU_SCALP']; return

    if (ib and price<=sl_p) or (not ib and price>=sl_p):
        pnl=round(abs(sl_p-en)/en*100,3); dl=round(abs(sl_p-en),2)
        tags=trade.get('tags',[]); rsi_v=trade.get('rsi_val',50); weekly=trade.get('weekly','neutral')
        why_loss=[]
        if ib and weekly=='bearish': why_loss.append("BUY against weekly bearish")
        if not ib and weekly=='bullish': why_loss.append("SELL against weekly bullish")
        if ib and rsi_v>62: why_loss.append(f"RSI {rsi_v} overbought for buy")
        if not ib and rsi_v<38: why_loss.append(f"RSI {rsi_v} oversold for sell")
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
        del state['open_trades']['XAU_SCALP']

def check_gold_prices():
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
            log.info(f"  ✅ GOLD WIN +{pnl}%")
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

# ═══ MAIN SCAN ═══════════════════════════════════════════════════
def run_gold_scan():
    """Gold XAU/USD scan — only runs when gold market is open"""
    state['scans_done']+=1
    state['last_scan']=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

<<<<<<< HEAD
    # Crypto runs every scan, no session restriction
=======
    # ── Crypto scalp scan runs 24/7 REGARDLESS of gold hours ──
>>>>>>> d6b4f602b0e5ed08cf8c4f22c86398bcb286e865
    try:
        run_crypto_scalp_scan()
    except Exception as e:
        log.error(f"Crypto scalp scan error: {e}")

<<<<<<< HEAD
    is_open, sess_info = gold_market_open()
    if not is_open:
        log.info(f"Gold market: {sess_info}")
=======
    # ── Gold market hours check ────────────────────────────────
    is_open, sess_info = gold_market_open()
    if not is_open:
        log.info(f"Gold market closed: {sess_info} — crypto only scan done")
>>>>>>> d6b4f602b0e5ed08cf8c4f22c86398bcb286e865
        return

    log.info(f"Gold scan #{state['scans_done']} — Session: {sess_info}")
    try:
        kl=fetch_gold_candles(limit=200)
        if not kl: log.warning("Gold: no data"); return

        # Gold scalp signal
        scalp_sig = None
        if is_scalp_session():
            raw_sig = compute_gold(kl)
            if raw_sig and raw_sig['score'] >= 8.5:
                ml_conf = scalp_adjusted_score(raw_sig, sess_info)
                if ml_conf >= 60:  # FIX: lowered from 72
                    scalp_sig = {**raw_sig, 'trade_type':'SCALP',
                                 'ml_conf': ml_conf,
                                 'tp_scalp1': round(raw_sig['entry'] + abs(raw_sig['entry']-raw_sig['sl'])*1.5,2) if raw_sig['dir']=='BUY'
                                              else round(raw_sig['entry'] - abs(raw_sig['entry']-raw_sig['sl'])*1.5,2),
                                 'tp_scalp2': round(raw_sig['entry'] + abs(raw_sig['entry']-raw_sig['sl'])*2.0,2) if raw_sig['dir']=='BUY'
                                              else round(raw_sig['entry'] - abs(raw_sig['entry']-raw_sig['sl'])*2.0,2),
                                 }
                    log.info(f"  Gold scalp: {raw_sig['setup']} {raw_sig['dir']} score={raw_sig['score']} mlconf={ml_conf:.0f}%")

        sig=compute_gold(kl)

        if scalp_sig:
            lf_s=last_fired.get('XAU_SCALP',{})
            cd_s=not lf_s or time.time()-lf_s.get('time',0)>COOLDOWN_M*60
            today_key=datetime.now(timezone.utc).strftime('%Y-%m-%d')
            if today_key!=state['stats'].get('scalp_day'):
                state['stats']['scalp_today']=0; state['stats']['scalp_day']=today_key
            if cd_s and state['stats'].get('scalp_today',0)<3:
                ok_s=send_tg(build_scalp_msg(scalp_sig))
                if ok_s:
                    last_fired['XAU_SCALP']={'time':time.time(),'price':scalp_sig['price']}
                    state['stats']['scalp_today']=state['stats'].get('scalp_today',0)+1
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
                    log.info(f"  Gold scalp TG sent #{state['stats']['scalp_today']}/3 today")

        if not sig:
            log.info(f"  Gold: no qualifying setup (price=${kl[-1]['c']:.2f})")
            return
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
                    state['stats']['paper_count'] = state['stats'].get('paper_count',0)+1
                    paper_count = state['stats']['paper_count']
                    log.info(f"  📋 PAPER #{paper_count} {sig['setup']} {sig['dir']} score={sig['score']}")
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
            log.info(f"  Gold signal score={sig['score']} blocked: pm={pm} cd={cd}")
    except Exception as e:
        log.error(f"Gold scan error: {e}")

<<<<<<< HEAD
# ═══ HEALTH SERVER ════════════════════════════════════════════════
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        w=state['stats'].get('wins',0); l=state['stats'].get('losses',0)
        b=state['stats'].get('be',0); tot=w+l+b
        ot=', '.join(f"{s}: {v['dir']} {v.get('setup','?')} @{v['entry']:.2f}"
                     for s,v in state['open_trades'].items()) or 'none'
        crypto_scanned = ', '.join(p['sym'] for p in CRYPTO_PAIRS)
        body=(
            f"SMC Gold+Crypto Engine v1 FIXED\n{'='*45}\n"
            f"Started:   {state['started']}\n"
            f"Last scan: {state['last_scan']}\n"
            f"Scans:     {state['scans_done']}\n"
            f"Alerts:    {state['alerts_sent']}\n"
            f"Mode:      {'SCALP' if SCALP_MODE else 'SWING'}\n"
            f"Gold min score: {GOLD_MIN_SCORE}\n"
            f"Crypto min score: {CRYPTO_SCALP_MIN_SCORE}\n\n"
            f"W:{w} L:{l} BE:{b} WR:{round(w/tot*100) if tot else 0}%\n"
            f"Open: {ot}\n\n"
            f"Crypto pairs: {crypto_scanned}\n"
            f"Crypto today: {state.get('scalp_crypto_count',{})}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        ).encode()
        self.send_response(200); self.send_header('Content-Type','text/plain')
        self.send_header('Content-Length',str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self,*a): pass
=======
    # Crypto scalp scan already called at top of run_gold_scan
>>>>>>> d6b4f602b0e5ed08cf8c4f22c86398bcb286e865

# ═══ TG COMMANDS ═════════════════════════════════════════════════
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
                            f"🏅 <b>Gold+Crypto Engine Stats</b>\n\n"
                            f"✅ Wins: {w}  ❌ Losses: {l}  ➡️ BE: {b}\n"
                            f"WR: {round(w/tot*100) if tot else 0}%\n"
                            f"Open: {len(state['open_trades'])}\n"
                            f"Alerts: {state['alerts_sent']}\n"
                            f"Mode: {'SCALP 🎯' if SCALP_MODE else 'SWING 📐'}\n\n"
                            + journal_stats_report()
                        )
                    elif txt in ('/learn','/ml'):   send_tg(performance_report())
                    elif txt in ('/deep','/analysis'): send_tg(deep_learning_report())
                    elif txt=='/scalp':             send_tg(scalp_ml_report())
                    elif txt=='/weekly':            send_tg(weekly_learning_report())
                    elif txt in ('/backup','/save'):backup_data_to_tg()
                    elif txt=='/open':
                        msg=("🔓 <b>Open trades:</b>\n"+'\n'.join(
                            f"  • {s}: {v['dir']} {v.get('setup','?')} @{v['entry']:.4f}"
                            for s,v in state['open_trades'].items())) if state['open_trades'] else "✅ No open trades"
                        send_tg(msg)
                    elif txt in ('/paper','/status'):
                        pc=state['stats'].get('paper_count',0)
                        remaining=max(0,PAPER_TARGET-pc)
                        send_tg(
                            f"📋 <b>Gold Engine Status</b>\n\n"
                            f"Mode: {'📋 PAPER (observing)' if PAPER_MODE else '🟢 LIVE'}\n"
                            f"Paper signals: {pc}/{PAPER_TARGET}\n"
                            f"Remaining: {remaining}\n\n"
                            f"Gold min score: {GOLD_MIN_SCORE}\n"
                            f"Crypto min score: {CRYPTO_SCALP_MIN_SCORE}\n"
                            f"Chop filter: 0.25 (relaxed)\n\n"
                            + performance_report()
                        )
                    elif txt=='/weights':
                        db=load_db(); w_d=db['weights']
                        lines=["⚖️ <b>Learned Weights</b>\n","<b>Setup scores:</b>"]
                        for s,v in w_d['setup_scores'].items(): lines.append(f"  {s}: {v:.2f}")
                        lines.append("\n<b>Sessions:</b>")
                        for s,v in w_d['session_weights'].items(): lines.append(f"  {s}: {v:.2f}x")
                        send_tg('\n'.join(lines))
                    elif txt=='/crypto':
                        cc = state.get('scalp_crypto_count', {})
                        oc = {k:v for k,v in state['open_trades'].items() if k.startswith('SCALP_')}
                        lines = [
                            "📡 <b>Crypto Scalp Status</b>",
                            f"Session: {get_gold_session()}",
                            f"Pairs tracked: {len(CRYPTO_PAIRS)}",
                            f"Today's count: {cc or 'none'}",
                            f"Open trades: {len(oc)}",
                            f"Min score: {CRYPTO_SCALP_MIN_SCORE}",
                            f"ML threshold: 50% (London/NY) / 60% (other)",
                            f"Chop filter: 0.25 (relaxed)",
                        ]
                        if oc:
                            lines.append("\n<b>Open scalps:</b>")
                            for k,v in oc.items():
                                lines.append(f"  {k}: {v['dir']} @{fp_crypto(v['entry'])} SL={fp_crypto(v['sl'])}")
                        send_tg('\n'.join(lines))
                    elif txt=='/help':
                        send_tg(
                            "🏅 <b>SMC Gold+Crypto Commands</b>\n\n"
                            "/stats   — performance\n"
                            "/crypto  — crypto scalp status\n"
                            "/scalp   — scalp ML weights\n"
                            "/paper   — paper trade status\n"
                            "/learn   — learning report\n"
                            "/deep    — chart analysis\n"
                            "/weights — learned weights\n"
                            "/weekly  — weekly summary\n"
                            "/open    — open trades\n"
                            "/backup  — backup data to TG\n"
                            "/help    — this menu"
                        )
        except Exception as e:
            log.warning(f"TG cmd: {e}")
        time.sleep(30)

# ═══ MAIN ════════════════════════════════════════════════════════
def main():
    if not TG_TOKEN or not TG_CHAT:
        log.error("Need TG_TOKEN + TG_CHAT env vars"); raise SystemExit(1)

    log.info("="*60)
    log.info("SMC GOLD+CRYPTO ENGINE v1 — FIXED BUILD")
    log.info(f"Gold min score:   {GOLD_MIN_SCORE} (was 6.0)")
    log.info(f"Crypto min score: {CRYPTO_SCALP_MIN_SCORE} (was 8.5)")
    log.info(f"Chop threshold:   0.25 (was 0.40)")
    log.info(f"ML threshold:     50% London/NY, 60% other (was 60/72)")
    log.info(f"Mode: {'SCALP' if SCALP_MODE else 'SWING'} | Paper: {PAPER_MODE}")
    log.info(f"Crypto pairs: {[p['sym'] for p in CRYPTO_PAIRS]}")
    log.info("="*60)

    log.info("Checking data files...")
    restored, missing = restore_data_from_tg()
    if restored:
        send_tg(f"✅ <b>Data Restored</b>\nRestored: {', '.join(restored)}")

    threading.Thread(target=lambda:HTTPServer(('',PORT),Health).serve_forever(),daemon=True).start()
    log.info(f"✓ Health server on :{PORT}")

    send_tg(
        "🏅 <b>SMC Gold+Crypto Engine — FIXED BUILD</b>\n\n"
        f"✅ Fixes applied:\n"
        f"  • Crypto logging: now visible in logs\n"
        f"  • Chop filter: 0.40 → 0.25 (less restrictive)\n"
        f"  • Gold min score: 6.0 → {GOLD_MIN_SCORE}\n"
        f"  • Crypto min score: 8.5 → {CRYPTO_SCALP_MIN_SCORE}\n"
        f"  • ML threshold: 60 → 50% for London/NY\n"
        f"  • Crypto setup: removed strict daily bias block\n\n"
        f"Mode: {'🎯 SCALP' if SCALP_MODE else '📐 SWING'} | Paper: {PAPER_MODE}\n"
        f"Pairs: {', '.join(p['sym'] for p in CRYPTO_PAIRS)}\n\n"
        "/crypto — scalp status  |  /help — all commands\n"
        "🏅 <b>SMC Engine v1 Fixed</b>"
    )

    start_backup_loop()

    def _monitor():
        while True:
            try:
                check_gold_prices()
                check_crypto_scalp_prices()
            except Exception as e:
                log.warning(f"Monitor: {e}")
            time.sleep(60)
    threading.Thread(target=_monitor, daemon=True).start()

    def _weekly():
        while True:
            n=datetime.now(timezone.utc)
            if n.weekday()==0 and n.hour==8 and n.minute<5:
                send_tg(weekly_learning_report()); time.sleep(300)
            time.sleep(60)
    threading.Thread(target=_weekly, daemon=True).start()

    threading.Thread(target=tg_commands, daemon=True).start()
    log.info("✓ All threads started — scanning every %dm" % SCAN_EVERY)

    while True:
        try:
            run_gold_scan()
        except Exception as e:
            log.error(f"Scan: {e}")
        log.info(f"Next scan in {SCAN_EVERY}m...")
        time.sleep(SCAN_EVERY*60)

if __name__=='__main__':
    main()
