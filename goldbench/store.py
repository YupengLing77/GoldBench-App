import sqlite3, json, os, uuid
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[1]
DATA=Path(os.environ.get('GOLDBENCH_DATA',ROOT/'runtime'))

def now(): return datetime.now(timezone.utc).isoformat()
def dumps(x): return json.dumps(x,ensure_ascii=False,allow_nan=False)

@contextmanager
def db():
    DATA.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(DATA/'bench.sqlite',timeout=30)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA foreign_keys=ON')
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback(); raise
    finally: con.close()

def init():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS experiments(id TEXT PRIMARY KEY, created TEXT, status TEXT, config TEXT, dataset TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS events(experiment TEXT, agent TEXT, day TEXT, payload TEXT, PRIMARY KEY(experiment,agent,day));
        CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, experiment TEXT, agent TEXT, day TEXT, month TEXT, status TEXT, reserved REAL, cost REAL, payload TEXT);
        CREATE TABLE IF NOT EXISTS reviews(experiment TEXT, agent TEXT, day TEXT, payload TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS news(cache_key TEXT PRIMARY KEY, payload TEXT, received TEXT);
        CREATE TABLE IF NOT EXISTS judgments(experiment TEXT, agent TEXT, day TEXT, judge TEXT, payload TEXT, PRIMARY KEY(experiment,agent,day,judge));
        ''')

def create(config,dataset):
    ident=uuid.uuid4().hex[:12]
    with db() as c:c.execute('INSERT INTO experiments VALUES(?,?,?,?,?,NULL)',(ident,now(),'queued',dumps(config),dumps(dataset)))
    return ident

def experiments():
    with db() as c:rows=c.execute('SELECT id,created,status,config,error FROM experiments ORDER BY created DESC').fetchall()
    return [dict(r)|{'config':json.loads(r['config'])} for r in rows]

def get(ident):
    with db() as c:r=c.execute('SELECT * FROM experiments WHERE id=?',(ident,)).fetchone()
    if not r: raise KeyError(ident)
    return dict(r)|{'config':json.loads(r['config']),'dataset':json.loads(r['dataset'])}

def status(ident,state,error=None):
    with db() as c:c.execute('UPDATE experiments SET status=?,error=? WHERE id=?',(state,error,ident))

def events(ident):
    with db() as c:rows=c.execute('SELECT payload FROM events WHERE experiment=? ORDER BY day,agent',(ident,)).fetchall()
    return [json.loads(r[0]) for r in rows]

def save_event(ident,event):
    with db() as c:c.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?)',(ident,event['agent'],event['day'],dumps(event)))

def reserve(ident,agent,day,amount,limit):
    rid=uuid.uuid4().hex; month=now()[:7]
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        used=c.execute("SELECT COALESCE(SUM(CASE WHEN status='complete' THEN cost ELSE reserved END),0) FROM requests WHERE month=?",(month,)).fetchone()[0]
        if used+amount>min(limit,10): raise RuntimeError('monthly_budget_exhausted')
        c.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)',(rid,ident,agent,day,month,'inflight',amount,None,'{}'))
    return rid

def finish_request(rid,cost,payload):
    with db() as c:c.execute("UPDATE requests SET status='complete',cost=?,payload=? WHERE id=?",(cost,dumps(payload),rid))

def budget():
    with db() as c:
        rows=c.execute('SELECT status,reserved,cost FROM requests WHERE month=?',(now()[:7],)).fetchall()
    spent=sum(r['cost'] or 0 for r in rows if r['status']=='complete')
    reserved=sum(r['reserved'] for r in rows if r['status']!='complete')
    return dict(month=now()[:7],spent=spent,reserved=reserved,remaining=max(0,10-spent-reserved))
