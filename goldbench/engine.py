from decimal import Decimal, ROUND_DOWN, getcontext
from datetime import date, datetime, timedelta, timezone
import math

getcontext().prec = 36
D = lambda x: Decimal(str(x))
STEP = D('0.0001')

def days(start, end, holidays=()):
    current, end = date.fromisoformat(start), date.fromisoformat(end)
    result = []
    while current <= end:
        if current.weekday() < 5 and current.isoformat() not in holidays:
            result.append(current.isoformat())
        current += timedelta(days=1)
    return result

def next_day(day, holidays=()):
    current = date.fromisoformat(day) + timedelta(days=1)
    while current.weekday() >= 5 or current.isoformat() in holidays:
        current += timedelta(days=1)
    return current.isoformat()

def cutoff(day):
    return datetime.fromisoformat(day+'T06:45:00+08:00')

def visible_bars(rows, day, holidays=()):
    cut = cutoff(day)
    return [r for r in rows if date.fromisoformat(r['date']).weekday() < 5
            and r['date'] not in holidays and
            datetime.fromisoformat(r['date']+'T00:00:00+00:00')+timedelta(days=1) <= cut]

def quote(row, spread=True):
    if not row:
        raise ValueError('missing_quote')
    b, a = D(row['bid_open']), D(row['ask_open'])
    if not b.is_finite() or not a.is_finite() or b <= 0 or a < b:
        raise ValueError('invalid_quote')
    return (b, a) if spread else ((b+a)/2, (b+a)/2)

def equity(cash, qty, b, a):
    return cash + qty*(b if qty >= 0 else a)

def execute(cash, qty, decision, row, config):
    cash, qty = D(cash), D(qty)
    b, a = quote(row, config.get('spread', True))
    mid = (a+b)/2
    slip = D(config.get('slippage_bps', 0))/10000
    commission = D(config.get('commission_bps', 0))/10000
    before = equity(cash, qty, b, a)
    bankrupt = before <= 0
    action = 'rebalance' if bankrupt else decision['action']
    weight = D(0 if bankrupt else (decision.get('target_weight') or 0))
    def trial(newq):
        delta = newq-qty
        price = a*(1+slip) if delta > 0 else b*(1-slip)
        fee = abs(delta)*price*commission
        newcash = cash-delta*price-fee
        return newcash, equity(newcash, newq, b, a), delta, price, fee
    if action == 'hold':
        newq = qty
    elif not weight:
        newq = D(0)
    else:
        # q*M = w*E_after; piecewise linear and monotone on a signed ray.
        sign = D(1) if weight > 0 else D(-1)
        lo, hi = D(0), abs(weight)*max(before, D(0))/mid
        for _ in range(110):
            amount = (lo+hi)/2
            _, nav, _, _, _ = trial(sign*amount)
            if amount*mid <= abs(weight)*nav:
                lo = amount
            else:
                hi = amount
        newq = sign*(lo/STEP).to_integral_value(rounding=ROUND_DOWN)*STEP
    newcash, nav, delta, price, fee = trial(newq)
    if action != 'hold' and nav > 0 and abs(newq)*mid > D(config.get('max_exposure',1))*nav+D('0.00000001'):
        raise ValueError('exposure_limit')
    return {'cash':str(newcash), 'qty':str(newq), 'equity':float(nav),
            'delta':float(delta), 'fill_price':float(price) if delta else None,
            'commission':float(fee), 'spread_cost':float(abs(delta)*(a-b)/2),
            'turnover':float(abs(delta)*price/before) if before > 0 else None,
            'exposure':float(abs(newq)*mid/nav) if nav>0 else None,
            'bankrupt':bankrupt or nav<=0, 'status':'filled' if delta else 'no_trade'}

def validate_decision(obj, config, evidence):
    required={'action','target_weight','expected_return','prob_up','rationale','evidence_ids'}
    if not isinstance(obj,dict) or set(obj)!=required:
        raise ValueError('exact_decision_fields_required')
    if obj['action'] not in ('hold','rebalance'):
        raise ValueError('action')
    for k in ('expected_return','prob_up'):
        if isinstance(obj[k],bool) or not isinstance(obj[k],(int,float)) or not math.isfinite(obj[k]):
            raise ValueError(k)
    if obj['expected_return'] < -1 or not 0 <= obj['prob_up'] <= 1:
        raise ValueError('prediction_range')
    if obj['action']=='hold':
        if obj['target_weight'] is not None: raise ValueError('hold_requires_null')
    else:
        w=obj['target_weight']
        if isinstance(w,bool) or not isinstance(w,(int,float)) or not math.isfinite(w): raise ValueError('weight')
        if abs(w)>config.get('max_exposure',1) or (w<0 and not config.get('allow_short',True)): raise ValueError('weight_limit')
    if not isinstance(obj['rationale'],str) or not obj['rationale'].strip(): raise ValueError('rationale')
    if not isinstance(obj['evidence_ids'],list) or any(not isinstance(x,str) or x not in evidence for x in obj['evidence_ids']):
        raise ValueError('unknown_evidence')
    return obj

def hold(reason='未提交有效决策'):
    return dict(action='hold',target_weight=None,expected_return=0.,prob_up=.5,rationale=reason,evidence_ids=[])

def sma(rows, day, holidays=()):
    past=visible_bars(rows,day,holidays)
    if not past: return None
    # Latest expected fully unlocked weekday; do not silently slide past gaps.
    latest=date.fromisoformat(day)-timedelta(days=2)
    while latest.weekday()>=5 or latest.isoformat() in holidays: latest-=timedelta(days=1)
    expected=[]
    while len(expected)<20:
        if latest.weekday()<5 and latest.isoformat() not in holidays: expected.append(latest.isoformat())
        latest-=timedelta(days=1)
    by={r['date']:r for r in past}
    if any(d not in by for d in expected): return None
    values=[(float(by[d]['bid_close'])+float(by[d]['ask_close']))/2 for d in expected]
    return 1 if values[0]>sum(values)/20 else -1 if values[0]<sum(values)/20 else 0

def metrics(events):
    points=[e for e in events if e.get('equity') is not None]
    peak=10000.; dd=0.
    for e in points:
        peak=max(peak,e['equity']);dd=max(dd,1-e['equity']/peak)
    decisions=[e for e in events if not e.get('terminal') and e.get('status')!='platform_paused']
    valid=[e for e in decisions if e.get('valid_submission')]
    labels=[e for e in valid if e.get('label') is not None]
    return {'return':points[-1]['equity']/10000-1 if points else None,'drawdown':dd if points else None,
            'submission_rate':len(valid)/len(decisions) if decisions else None,
            'mae_bp':sum(abs(e['decision']['expected_return']-e['label'])*10000 for e in labels)/len(labels) if labels else None,
            'brier':sum((e['decision']['prob_up']-int(e['label']>0))**2 for e in labels)/len(labels) if labels else None,
            'label_n':len(labels),'rounds':len(decisions),'cost':sum(e.get('cost',0) for e in events),
            'zero_return_mae_bp':sum(abs(e['label'])*10000 for e in labels)/len(labels) if labels else None,
            'half_probability_brier':.25 if labels else None,
            'input_tokens':sum(e.get('input_tokens',0) for e in events),'output_tokens':sum(e.get('output_tokens',0) for e in events),
            'tool_calls':sum(len(e.get('trace',[])) for e in events),
            'turnover':sum(e.get('turnover') or 0 for e in events)}
