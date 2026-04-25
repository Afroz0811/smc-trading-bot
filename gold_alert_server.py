#!/usr/bin/env python3
"""
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

logging.basicConfig(level=logging.INFO,
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

def save_db(db):
    try:
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
def main():
    if not TG_TOKEN or not TG_CHAT:
        log.error("Need TG_TOKEN + TG_CHAT"); raise SystemExit(1)

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

    send_tg(
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