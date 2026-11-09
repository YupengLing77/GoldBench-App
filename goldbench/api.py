import json, csv, io, os, hashlib
from datetime import date,datetime
from typing import Literal
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import Response,FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field,model_validator
from . import store,data,judges
from .engine import metrics,days,next_day
from .worker import MODELS,roster

app=FastAPI(title='GoldBench local',version='0.1.0')
store.init()

@app.exception_handler(ValueError)
async def bad_data(request,exc):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=422,content={'detail':str(exc)[:300]})

@app.exception_handler(RuntimeError)
async def unavailable(request,exc):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=409,content={'detail':str(exc)[:300]})

@app.middleware('http')
async def local_only(request:Request,call_next):
    # Cross-site requests may not create tasks, spend credits, or read credentials.
    origin=request.headers.get('origin')
    if origin and origin not in ('http://127.0.0.1:8765','http://localhost:8765','http://127.0.0.1:5173','http://localhost:5173'):
        return Response('Origin denied',status_code=403)
    host=request.headers.get('host','').split(':')[0]
    if host not in ('localhost','127.0.0.1','testserver'):return Response('Host denied',status_code=403)
    return await call_next(request)

class Config(BaseModel):
    name:str=Field(default='本地历史回测',min_length=1,max_length=100)
    start:date=date(2026,10,1)
    end:date=date(2026,10,31)
    dataset:Literal['fixture','audit','import','tushare']='fixture'
    driver:Literal['fixture','poe']='fixture'
    allow_short:bool=True
    spread:bool=True
    max_exposure:float=Field(default=1,gt=0,le=1)
    slippage_bps:float=Field(default=0,ge=0,le=100)
    commission_bps:float=Field(default=0,ge=0,le=100)
    monthly_budget:float=Field(default=10,gt=0,le=10)
    models:list[str]=Field(default_factory=lambda:MODELS.copy(),min_length=1,max_length=4)
    holidays:list[str]=Field(default_factory=list)
    prices:dict=Field(default_factory=dict)
    model_parameters:dict=Field(default_factory=dict)
    paid_enabled:bool=False
    compatibility_verified:bool=False
    rows:list[dict]=Field(default_factory=list,max_length=5000)
    research_data:dict=Field(default_factory=dict)
    @model_validator(mode='after')
    def validate_all(self):
        if self.start>self.end or (self.end-self.start).days>366:raise ValueError('date_range')
        if len(set(self.models))!=len(self.models) or any(x not in MODELS for x in self.models):raise ValueError('models')
        if any(date.fromisoformat(d).isoformat()!=d for d in self.holidays):raise ValueError('holidays')
        if not days(self.start.isoformat(),self.end.isoformat(),self.holidays):raise ValueError('empty_calendar')
        for m,p in self.prices.items():
            if m not in MODELS+['GPT-4.1-mini','Gemini-3.1-Flash-Lite'] or set(p)!={'input','output'} or any(not isinstance(x,(int,float)) or not 0<x<10000 for x in p.values()):raise ValueError('invalid_price')
        for model,p in self.model_parameters.items():
            if model not in MODELS or not isinstance(p,dict) or set(p)-{'reasoning_effort','thinking_budget'}:raise ValueError('model_parameters')
        return self

def build(config):
    c=config.model_dump(mode='json');raw=c.pop('rows');research=c.pop('research_data')
    rows=data.fixture(c['start'],c['end']) if c['dataset']=='fixture' else data.audit_rows() if c['dataset']=='audit' else data.historical_prices(c['start'],c['end']) if c['dataset']=='tushare' else data.normalize(raw)
    dataset={'prices':rows,**{k:v for k,v in research.items() if k in ('get_macro','get_positioning','read_webpage')}}
    report=data.coverage(rows,c)
    c['data_hash']=report['hash'];c['spec_version']='1.0';c['config_hash']=hashlib.sha256(store.dumps(c).encode()).hexdigest()
    return c,dataset,report

@app.get('/api/health')
def health():return {'ok':True,'poe_configured':bool(os.getenv('POE_API_KEY')),'tushare_configured':bool(os.getenv('TUSHARE_TOKEN')),'budget':store.budget(),'models':MODELS}

@app.post('/api/preflight')
def preflight(config:Config):
    c,d,r=build(config)
    issues=[]
    if not r['executable']:issues.append('行情缺少预定轮次或期末端点；不可启动真实模型实验。')
    if config.driver=='poe':
        if config.dataset=='fixture':issues.append('真实模型不能使用工程测试行情。')
        if not config.paid_enabled:issues.append('尚未启用付费运行。')
        if not os.getenv('POE_API_KEY'):issues.append('未配置POE_API_KEY。')
        if not config.compatibility_verified:issues.append('需先完成独立Poe兼容/计费校准并明确标记。')
        if any(m not in config.prices for m in config.models):issues.append('需提供各模型当前每百万token输入/输出单价。')
        # Worst case study allocation plus judge reserve. Never promise the original estimate as a cap.
        estimated=r['expected_rounds']*4*sum((20000*p['input']+2000*p['output'])/1e6 for m,p in config.prices.items() if m in config.models)+3.5
        if estimated>min(config.monthly_budget,store.budget()['remaining']):issues.append('全实验最坏研究预算及3.5美元裁判预留超过剩余额度；请缩短试跑区间或调整正式方案。')
    return {'coverage':r,'issues':issues,'can_start':not issues or config.driver=='fixture','config':c}

@app.get('/api/experiments')
def list_experiments():return store.experiments()

@app.post('/api/experiments')
def create(config:Config):
    report=preflight(config)
    if not report['can_start']:raise HTTPException(422,report['issues'])
    c,d,r=build(config);ident=store.create(c,d)
    return {'id':ident}

def existing(ident):
    try:return store.get(ident)
    except KeyError:raise HTTPException(404,'experiment_not_found')

@app.get('/api/experiments/{ident}')
def detail(ident:str):
    exp=existing(ident);events=store.events(ident);groups=[]
    for aid,model,group,repeat in roster(exp['config']):
        ev=[e for e in events if e['agent']==aid];m=metrics(ev)
        final=next((e for e in ev if e.get('terminal')),None)
        eligible=bool(final and final.get('equity') is not None and (m['submission_rate'] or 0)>=.9 and exp['config']['driver']=='poe' and exp['status']=='awaiting_review')
        groups.append(dict(agent=aid,model=model,group=group,repeat=repeat,metrics=m,eligible=eligible,events=ev))
    with store.db() as c:reviews=[json.loads(r[0]) for r in c.execute('SELECT payload FROM reviews WHERE experiment=? ORDER BY created',(ident,))]
    quality=judges.summary(ident)
    for a in groups:
        scored=[j for j in quality if j['agent']==a['agent'] and j['final'] and j['total'] is not None]
        a['metrics']['quality']=sum(j['total'] for j in scored)/len(scored) if scored else None
        a['metrics']['quality_n']=len(scored)
    common={}
    for group in ('base','extended'):
        members=[a for a in groups if a['group']==group]
        eligible=[{e['day'] for e in a['events'] if e.get('valid_submission') and e.get('label') is not None and not e['terminal']} for a in members]
        dates=set.intersection(*eligible) if eligible else set()
        common[group]={'dates':sorted(dates),'n':len(dates),'accounts':{a['agent']:metrics([e for e in a['events'] if e['day'] in dates]) for a in members}}
    return {k:v for k,v in exp.items() if k!='dataset'}|{'coverage':data.coverage(exp['dataset']['prices'],exp['config']),'accounts':groups,'reviews':reviews,'judgments':quality,'common_prediction':common,'budget':store.budget()}

@app.post('/api/experiments/{ident}/judge')
def judge(ident:str):
    exp=existing(ident)
    if exp['config']['driver']!='poe' or exp['status']!='awaiting_review':raise HTTPException(409,'仅完整真实模型回测可启动付费裁判')
    if any(j not in exp['config']['prices'] for j in judges.JUDGES):raise HTTPException(422,'创建实验时需冻结两裁判的当前单价')
    store.status(ident,'judge_queued');return {'ok':True}

@app.post('/api/experiments/{ident}/pause')
def pause(ident:str):
    exp=existing(ident)
    if exp['status'] not in ('running','queued'):raise HTTPException(409,'not_running')
    store.status(ident,'paused' if exp['status']=='queued' else 'pause_requested');return {'ok':True}

@app.post('/api/experiments/{ident}/resume')
def resume(ident:str):
    exp=existing(ident)
    if exp['status'] not in ('paused','blocked'):raise HTTPException(409,'not_paused')
    with store.db() as c:
        outstanding=c.execute("SELECT COUNT(*) FROM requests WHERE experiment=? AND status!='complete'",(ident,)).fetchone()[0]
        orphan=c.execute('SELECT COUNT(*) FROM requests r LEFT JOIN events e ON r.experiment=e.experiment AND r.agent=e.agent AND r.day=e.day WHERE r.experiment=? AND e.day IS NULL',(ident,)).fetchone()[0]
    if outstanding or orphan:raise HTTPException(409,'存在未知或未提交到检查点的付费请求；需人工核账，不自动重复生成。')
    store.status(ident,'queued');return {'ok':True}

class Review(BaseModel):
    agent:str
    day:str
    scores:list[int|None]=Field(min_length=3,max_length=3)
    rationale:str=Field(min_length=5,max_length=4000)
    reviewer:str=Field(min_length=1,max_length=100)
    @model_validator(mode='after')
    def valid(self):
        if any(x is not None and not 0<=x<=4 for x in self.scores):raise ValueError('score_range')
        return self

@app.get('/api/experiments/{ident}/blind')
def blind(ident:str):
    existing(ident)
    ev=[e for e in store.events(ident) if e.get('valid_submission') and e['group']!='baseline' and not e['terminal']]
    return [{'review_id':hashlib.sha256((ident+e['agent']+e['day']).encode()).hexdigest()[:16], 'day':e['day'],'decision':e['decision'],'evidence':e.get('evidence',{}),'cutoff':e['cutoff']} for e in ev]

class BlindReview(BaseModel):
    review_id:str
    scores:list[int|None]=Field(min_length=3,max_length=3)
    rationale:str=Field(min_length=5,max_length=4000)
    reviewer:str=Field(min_length=1,max_length=100)

@app.post('/api/experiments/{ident}/reviews')
def review(ident:str,r:BlindReview):
    exp=existing(ident)
    target=next((e for e in store.events(ident) if hashlib.sha256((ident+e['agent']+e['day']).encode()).hexdigest()[:16]==r.review_id and e.get('valid_submission')),None)
    if not target:raise HTTPException(404,'review_record_not_found')
    try:validated=Review(agent=target['agent'],day=target['day'],scores=r.scores,rationale=r.rationale,reviewer=r.reviewer)
    except ValueError:raise HTTPException(422,'invalid_scores')
    payload=validated.model_dump()|{'review_id':r.review_id,'type':'human_blind','total':None if None in r.scores else sum(r.scores)/12*100,'created':store.now()}
    with store.db() as c:c.execute('INSERT INTO reviews VALUES(?,?,?,?,?)',(ident,target['agent'],target['day'],store.dumps(payload),store.now()))
    return {'ok':True}

@app.get('/api/experiments/{ident}/export')
def export(ident:str,format:Literal['json','csv']='json'):
    result=detail(ident)
    if format=='json':return Response(store.dumps(result),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="goldbench-{ident}.json"'})
    out=io.StringIO();writer=csv.writer(out);writer.writerow(['agent','date','status','cash','qty','equity','delta','fill_price','commission','label','cost'])
    for acc in result['accounts']:
        for e in acc['events']:writer.writerow([e.get(k,'') for k in ('agent','day','status','cash','qty','equity','delta','fill_price','commission','label','cost')])
    return Response('\ufeff'+out.getvalue(),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="goldbench-{ident}.csv"'})

dist=store.ROOT/'frontend/dist'
if dist.exists():
    app.mount('/assets',StaticFiles(directory=dist/'assets'),name='assets')
    @app.get('/')
    def index():return FileResponse(dist/'index.html')
