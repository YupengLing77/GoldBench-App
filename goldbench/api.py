import json, csv, io, os, hashlib
from datetime import date,datetime
from typing import Literal
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import Response,FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field,model_validator
from . import store,data,judges,market_tools,policies
from .engine import metrics,days,next_day
from .worker import MODELS,SUPPORTED_MODELS,roster
from .providers import QWEN, DASHSCOPE_MODELS

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
    driver:Literal['fixture','poe','dashscope']='fixture'
    allow_short:bool=True
    spread:bool=True
    max_exposure:float=Field(default=1,gt=0,le=1)
    slippage_bps:float=Field(default=0,ge=0,le=100)
    commission_bps:float=Field(default=0,ge=0,le=100)
    monthly_budget:float=Field(default=10,gt=0,le=10)
    models:list[str]=Field(default_factory=lambda:MODELS.copy(),min_length=1,max_length=4)
    groups:list[Literal['base','extended']]=Field(default_factory=lambda:['base','extended'],min_length=1,max_length=2)
    repeats:int=Field(default=2,ge=1,le=2)
    token_limit:int|None=Field(default=None,gt=0,le=1000000)
    research_policy:Literal['bounded','total_tokens_only']='bounded'
    token_budget_parent:str|None=Field(default=None,pattern='^[0-9a-f]{12}$')
    holidays:list[str]=Field(default_factory=list)
    prices:dict=Field(default_factory=dict)
    model_parameters:dict=Field(default_factory=dict)
    paid_enabled:bool=False
    compatibility_verified:bool=False
    rows:list[dict]=Field(default_factory=list,max_length=5000)
    research_data:dict=Field(default_factory=dict)
    @model_validator(mode='after')
    def validate_all(self):
        if policies.total_only(self.model_dump()) and (self.driver!='dashscope' or self.models!=['deepseek-v4.1-flash'] or self.groups!=['extended'] or self.repeats!=1 or self.token_limit!=1000000):raise ValueError('total_only_requires_single_flash_extended_and_1M')
        if self.start>self.end or (self.end-self.start).days>366:raise ValueError('date_range')
        if len(set(self.models))!=len(self.models) or any(x not in SUPPORTED_MODELS for x in self.models):raise ValueError('models')
        if len(set(self.groups))!=len(self.groups):raise ValueError('duplicate_groups')
        if self.driver=='dashscope' and (len(self.models)!=1 or self.models[0] not in DASHSCOPE_MODELS or self.token_limit is None):raise ValueError('dashscope_requires_supported_model_and_token_limit')
        if self.driver=='poe' and any(m in DASHSCOPE_MODELS for m in self.models):raise ValueError('model_requires_dashscope_driver')
        if any(date.fromisoformat(d).isoformat()!=d for d in self.holidays):raise ValueError('holidays')
        if not days(self.start.isoformat(),self.end.isoformat(),self.holidays):raise ValueError('empty_calendar')
        for m,p in self.prices.items():
            if m not in SUPPORTED_MODELS+['GPT-4.1-mini','Gemini-3.1-Flash-Lite'] or set(p)!={'input','output'} or any(not isinstance(x,(int,float)) or not 0<x<10000 for x in p.values()):raise ValueError('invalid_price')
        for model,p in self.model_parameters.items():
            if model not in MODELS or not isinstance(p,dict) or set(p)-{'reasoning_effort','thinking_budget'}:raise ValueError('model_parameters')
        unknown = set(self.research_data) - set(market_tools.SNAPSHOT_TOOLS)
        if unknown:raise ValueError('unknown_research_tools:' + ','.join(sorted(unknown)))
        for name, records in self.research_data.items():
            if not isinstance(records,list) or any(not isinstance(r,dict) for r in records):
                raise ValueError('research_data_requires_lists_of_records')
            for record in records:
                if not isinstance(record.get('available_at'),str):raise ValueError('research_record_requires_available_at')
                market_tools.aware(record['available_at'])
        return self

def build(config):
    c=config.model_dump(mode='json');raw=c.pop('rows');research=c.pop('research_data')
    rows=data.fixture(c['start'],c['end']) if c['dataset']=='fixture' else data.audit_rows() if c['dataset']=='audit' else data.historical_prices(c['start'],c['end']) if c['dataset']=='tushare' else data.normalize(raw)
    dataset={'prices':rows,**{k:v for k,v in research.items() if k in market_tools.SNAPSHOT_TOOLS}}
    report=data.coverage(rows,c)
    c['data_hash']=report['hash'];c['spec_version']='1.0';c['config_hash']=hashlib.sha256(store.dumps(c).encode()).hexdigest()
    return c,dataset,report

@app.get('/api/health')
def health():return {'ok':True,'poe_configured':bool(os.getenv('POE_API_KEY')),'dashscope_configured':bool(os.getenv('DASHSCOPE_API_KEY')),'tushare_configured':bool(os.getenv('TUSHARE_TOKEN')),'budget':store.budget(),'models':SUPPORTED_MODELS}

@app.post('/api/preflight')
def preflight(config:Config):
    c,d,r=build(config)
    issues=[]
    if not r['executable']:issues.append('行情缺少预定轮次或期末端点；不可启动真实模型实验。')
    if config.driver!='fixture':
        if config.dataset=='fixture':issues.append('真实模型不能使用工程测试行情。')
        if not config.paid_enabled:issues.append('尚未启用付费运行。')
        credential='DASHSCOPE_API_KEY' if config.driver=='dashscope' else 'POE_API_KEY'
        if not os.getenv(credential):issues.append('未配置'+credential+'。')
        if config.driver=='poe' and not config.compatibility_verified:issues.append('需先完成独立Poe兼容/计费校准并明确标记。')
        if any(m not in config.prices for m in config.models):issues.append('需提供各模型当前每百万token输入/输出单价。')
        # Worst case study allocation plus judge reserve. Never promise the original estimate as a cap.
        paths=len(config.groups)*config.repeats
        estimated=r['expected_rounds']*paths*sum(((30000 if m=='deepseek-v4-pro-0813' else 20000)*p['input']+(10000 if m=='deepseek-v4-pro-0813' else 2000)*p['output'])/1e6 for m,p in config.prices.items() if m in config.models)+(3.5 if config.driver=='poe' else .05)
        if not policies.total_only(c) and estimated>min(config.monthly_budget,store.budget()['remaining']):issues.append('全实验最坏研究预算与校准/裁判预留超过剩余额度；请缩短日期或减少账户。')
        remaining_tokens=config.token_limit
        if config.token_budget_parent:
            parent=store.get(config.token_budget_parent)
            if parent['config'].get('token_budget_parent') or parent['config'].get('token_limit')!=config.token_limit:raise ValueError('invalid_token_budget_parent')
            remaining_tokens=store.token_budget(config.token_budget_parent,config.token_limit)['remaining']
        if not policies.total_only(c) and remaining_tokens is not None and r['expected_rounds']*paths*len(config.models)*(40000 if config.models==['deepseek-v4-pro-0813'] else 22000)+(8000 if config.driver=='dashscope' else 0)>remaining_tokens:
            issues.append('研究轮次的最坏token用量及兼容性校准预留超过实验token上限；请缩短日期或减少账户。')
    return {'coverage':r,'issues':issues,'can_start':not issues or config.driver=='fixture','config':c,'calibration_required':config.driver=='dashscope','token_limit':config.token_limit}

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
    return {k:v for k,v in exp.items() if k!='dataset'}|{'coverage':data.coverage(exp['dataset']['prices'],exp['config']),'accounts':groups,'reviews':reviews,'judgments':quality,'common_prediction':common,'budget':store.budget(),'token_budget':store.token_budget(ident,exp['config'].get('token_limit'))}

@app.get('/api/experiments/{ident}/live')
def live(ident:str):
    """Read-only monitor: curated calls/usage, never prompts, keys or reasoning."""
    result=detail(ident)
    with store.db() as con:
        requests=con.execute('SELECT id,agent,day,status,payload FROM requests WHERE experiment=? ORDER BY rowid',(ident,)).fetchall()
    activity=[]; replies={}; completed=0
    for row in requests:
        payload=json.loads(row['payload'])
        for message in payload.get('request',{}).get('messages',[]):
            if message.get('role')=='tool':
                try:replies[(row['agent'],row['day'],message.get('tool_call_id'))]=json.loads(message.get('content','null'))
                except (ValueError,TypeError):pass
        response=payload.get('response',{})
        choices=response.get('choices') or [{}]
        calls=choices[0].get('message',{}).get('tool_calls') or []
        for call in calls:
            fn=call.get('function',{})
            try:arguments=json.loads(fn.get('arguments','{}'))
            except (ValueError,TypeError):arguments={'parse_error':True}
            activity.append({'id':row['id']+':'+call.get('id',''), 'call_id':call.get('id'),
                             'day':row['day'],'agent':row['agent'],'name':fn.get('name'),
                             'arguments':arguments,'status':'complete','advertised':fn.get('name') in [t['function']['name'] for t in payload.get('request',{}).get('tools',[])],
                             'submission_only':[t['function']['name'] for t in payload.get('request',{}).get('tools',[])]==['submit_decision'],
                             'usage':response.get('usage',{})})
        if row['status']!='complete':
            activity.append({'id':row['id'],'day':row['day'],'agent':row['agent'],'name':'model_request',
                             'arguments':{},'status':row['status'],'usage':{}})
        else:completed+=1
    decisions={}
    for account in result['accounts']:
        for event in account['events']:
            key=(account['agent'],event['day'])
            if event.get('valid_submission'):decisions[key]=event.get('decision')
            for message in event.get('messages',[]):
                if message.get('role')=='tool':
                    try:replies[key+(message.get('tool_call_id'),)]=json.loads(message.get('content','null'))
                    except (ValueError,TypeError):pass
    for item in activity:
        key=(item['agent'],item['day'])
        reply=replies.get(key+(item.get('call_id'),))
        if reply is not None:item['result']=reply
        item['outcome']='awaiting_result'
        if item['status']!='complete':item['outcome']='inflight'
        elif isinstance(reply,dict) and reply.get('error'):
            item['outcome']='rejected'
            error=str(reply['error'])
            if item.get('submission_only') and item['name']!='submit_decision':
                item.update(reason='final_submission_only',explanation='本轮已进入最终提交阶段，研究工具关闭；请求被拦截，未读取数据。')
            elif error.startswith('tool_not_active'):
                item.update(reason='tool_not_loaded',explanation='该工具不在本次请求的已加载列表中；应先加载，再在下一次请求中调用。')
            else:item.update(reason='invalid_call',explanation='请求被拒绝：'+error[:200])
        elif item['name']=='submit_decision' and decisions.get(key)==item['arguments']:
            item['outcome']='submitted'
        elif isinstance(reply,dict) and reply.get('evidence_id'):
            data=reply.get('data')
            state=data.get('status') if isinstance(data,dict) else None
            item['outcome']='data_unavailable' if state in ('unavailable','dataset_not_prepared','historical_vintage_unavailable','no_visible_data','fixture_has_no_news','record_too_large') else 'succeeded'
        elif isinstance(reply,dict) and reply.get('status')=='selected':item['outcome']='succeeded'
    config=result['config']
    safe_accounts=[]
    event_fields=('day','status','equity','terminal','decision','qty','cash','fill_price','delta','commission','valid_submission','cutoff','cost')
    for account in result['accounts']:
        safe_accounts.append({k:account[k] for k in ('agent','model','group','repeat','metrics')} |
                             {'events':[{k:event.get(k) for k in event_fields} for event in account['events']]})
    return {'id':ident,'status':result['status'],'error':result.get('error'),'updated_at':store.now(),
            'config':{k:config.get(k) for k in ('name','start','end','models','driver','allow_short','spread','holidays','prompt_version','excluded_dates_reason','token_budget_parent','research_policy')},
            'coverage':result['coverage'],'token_budget':result['token_budget'],'accounts':safe_accounts,
            'requests_completed':completed,'requests_inflight':sum(r['status']!='complete' for r in requests),
            'activity':activity,'policy':{'cutoff':'06:45 Asia/Shanghai','execution':'07:00 Asia/Shanghai',
                                        'search_enabled':False,'local_only':True,'historical_vintages_required':True}}


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
    if (dist/'fonts').exists():
        app.mount('/fonts',StaticFiles(directory=dist/'fonts'),name='fonts')
    app.mount('/assets',StaticFiles(directory=dist/'assets'),name='assets')
    @app.get('/monitor')
    @app.get('/monitor/')
    @app.get('/')
    def index():return FileResponse(dist/'index.html')
