import json, math, hashlib, os
from datetime import date,timedelta,datetime,timezone
from pathlib import Path
import httpx
from . import store, policies
from .engine import days, next_day, cutoff

def normalize(rows):
    result={}
    for row in rows:
        r=dict(row);d=str(r.get('date') or r.get('trade_date',''))
        if len(d)==8:d=f'{d[:4]}-{d[4:6]}-{d[6:]}'
        date.fromisoformat(d);r['date']=d
        for k in ('bid_open','ask_open','bid_close','ask_close'):
            if k not in r or not math.isfinite(float(r[k])) or float(r[k])<=0:raise ValueError('invalid_price_field:'+k)
            r[k]=float(r[k])
        if r['bid_open']>r['ask_open'] or r['bid_close']>r['ask_close']:raise ValueError('crossed_quote')
        if d in result and result[d]!=r:raise ValueError('conflicting_date:'+d)
        result[d]=r
    return sorted(result.values(),key=lambda r:r['date'])

def fixture(start,end):
    first=(date.fromisoformat(start)-timedelta(days=160)).isoformat()
    rows=[]
    for i,d in enumerate(days(first,next_day(end))):
        mid=2600+1.4*i+35*math.sin(i/7)+11*math.sin(i/2.8)
        close=mid+4*math.sin(i/4)
        rows.append(dict(date=d,bid_open=round(mid-.3,4),ask_open=round(mid+.3,4),bid_close=round(close-.3,4),ask_close=round(close+.3,4),tick_qty=10000))
    return rows

def audit_rows():
    path=store.ROOT/'data/local/october-coverage/audit-20261109-130254.jsonl'
    if not path.exists(): return []
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        r=json.loads(line)
        if r.get('api')=='fx_daily' and r.get('params',{}).get('ts_code')=='XAUUSD.FXCM':return normalize(r['rows'])
    return []

def coverage(rows,config):
    by={r['date']:r for r in rows}; expected=days(config['start'],config['end'],config.get('holidays',[]))
    terminal=next_day(expected[-1],config.get('holidays',[])) if expected else None
    missing=[d for d in expected+([terminal] if terminal else []) if d not in by]
    return dict(expected_rounds=len(expected),missing_dates=missing,terminal=terminal,
                weekend_rows=sum(date.fromisoformat(r['date']).weekday()>4 for r in rows),
                low_tick_dates=[r['date'] for r in rows if r.get('tick_qty') is not None and r['tick_qty']<10],
                executable=bool(expected) and not missing,
                hash=hashlib.sha256(store.dumps(rows).encode()).hexdigest())

def tushare(api,params):
    token=os.environ.get('TUSHARE_TOKEN')
    if not token:raise RuntimeError('TUSHARE_TOKEN_not_configured')
    with httpx.Client(timeout=None if policies.GLOBAL_ONLY.get() else 30) as client:
        r=client.post('https://api.tushare.pro',json={'api_name':api,'token':token,'params':params,'fields':''})
        r.raise_for_status();obj=r.json()
    if obj.get('code')!=0:raise RuntimeError('tushare_api_error:'+str(obj.get('code')))
    data=obj.get('data') or {}
    return [dict(zip(data.get('fields',[]),row)) for row in data.get('items',[])]

def news_slice(start,end,source='sina',depth=0):
    key=hashlib.sha256(f'news:{source}:{start}:{end}'.encode()).hexdigest()
    with store.db() as c:r=c.execute('SELECT payload FROM news WHERE cache_key=?',(key,)).fetchone()
    if r:return json.loads(r[0])
    if source not in ('sina','cls','major_sina'):raise ValueError('news_source_not_allowed')
    limit=400 if source=='major_sina' else 1500
    rows=tushare('major_news' if source=='major_sina' else 'news',dict(src='新浪财经' if source=='major_sina' else source,start_date=start,end_date=end,limit=limit))
    if len(rows)>=limit:
        a,b=datetime.fromisoformat(start),datetime.fromisoformat(end)
        if (b-a).total_seconds()<=2 or (not policies.GLOBAL_ONLY.get() and depth>=24):raise RuntimeError('news_window_truncated')
        mid=(a+(b-a)/2).replace(microsecond=0).isoformat(sep=' ')
        rows=news_slice(start,mid,source,depth+1)+news_slice(mid,end,source,depth+1)
    unique={hashlib.sha256(store.dumps(r).encode()).hexdigest():r for r in rows}
    result=[dict(r,record_id=k,source=source) for k,r in unique.items()]
    with store.db() as c:c.execute('INSERT OR IGNORE INTO news VALUES(?,?,?)',(key,store.dumps(result),store.now()))
    # A concurrent fetch must use the winner's frozen bytes.
    with store.db() as c:return json.loads(c.execute('SELECT payload FROM news WHERE cache_key=?',(key,)).fetchone()[0])

def historical_prices(start,end):
    first=(date.fromisoformat(start)-timedelta(days=180)).strftime('%Y%m%d')
    last=next_day(end).replace('-','')
    key='prices:XAUUSD.FXCM:'+first+':'+last
    with store.db() as c:r=c.execute('SELECT payload FROM news WHERE cache_key=?',(key,)).fetchone()
    if r:return json.loads(r[0])
    rows=normalize(tushare('fx_daily',dict(ts_code='XAUUSD.FXCM',start_date=first,end_date=last,limit=1000)))
    if len(rows)>=1000:raise RuntimeError('price_request_truncated')
    with store.db() as c:c.execute('INSERT OR IGNORE INTO news VALUES(?,?,?)',(key,store.dumps(rows),store.now()))
    with store.db() as c:return json.loads(c.execute('SELECT payload FROM news WHERE cache_key=?',(key,)).fetchone()[0])
