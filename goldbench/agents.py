"""Provider-neutral loop; fixture driver is explicitly NOT an LLM result."""
import json, os, time, hashlib, re, subprocess,uuid
from datetime import datetime, timedelta
import httpx
from . import store, data, market_tools, tool_router, providers, prompts, policies
from .engine import hold, validate_decision, visible_bars, cutoff, sma,next_day

SUBMIT={'type':'function','function':{'name':'submit_decision','description':'Submit once; hold keeps quantity. Return is next scheduled open-to-open decimal return.',
 'parameters':{'type':'object','additionalProperties':False,'properties':{
 'action':{'type':'string','enum':['hold','rebalance']},'target_weight':{'type':['number','null']},
 'expected_return':{'type':'number','minimum':-1},'prob_up':{'type':'number','minimum':0,'maximum':1},
 'rationale':{'type':'string'},'evidence_ids':{'type':'array','items':{'type':'string'}}},
 'required':['action','target_weight','expected_return','prob_up','rationale','evidence_ids']}}}

def schema(name,description,properties):
    return {'type':'function','function':{'name':name,'description':description,'parameters':{'type':'object','properties':properties,'additionalProperties':False}}}
TOOLS=[
 schema('get_prices','Read unlocked XAU/USD daily bars, default 60, at most 500.',{'limit':{'type':'integer','minimum':1,'maximum':500}}),
 schema('search_news','Historical Tushare news search within frozen cutoff; 7 days default, 90 max.',{'source':{'type':'string','enum':['sina','cls','major_sina']},'query':{'type':'string','maxLength':200},'days':{'type':'integer','minimum':1,'maximum':90},'limit':{'type':'integer','minimum':1,'maximum':20}}),
 schema('read_news','Read one previously discovered article, paginated at 12000 characters.',{'record_id':{'type':'string'},'offset':{'type':'integer','minimum':0}}),
 schema('get_history','This account only; mature past labels only.',{'limit':{'type':'integer','minimum':1,'maximum':20}}),
 schema('read_webpage','Read authorized archived page only; current web fetching disabled.',{'record_id':{'type':'string'}}),
 schema('run_backtest','Run fixed SMA20 on acquired price handle only. No parameter optimization.',{'handle':{'type':'string'}}),
 schema('run_python','Isolated Docker Python; data variable is acquired price handle. No network.',{'code':{'type':'string','maxLength':8000},'handle':{'type':'string'}})
] + [schema(name, description, properties) for name, description, properties in market_tools.SPECS]

def unrestricted_tools():
    import copy
    tools=copy.deepcopy(TOOLS)
    def strip(value):
        if isinstance(value,dict):
            for key in ('maximum','maxLength','maxItems'):value.pop(key,None)
            for child in value.values():strip(child)
        elif isinstance(value,list):
            for child in value:strip(child)
    strip(tools)
    descriptions={'get_prices':'Read historical XAUUSD daily bars before cutoff; limit selects rows.',
                  'search_news':'Search historical news before cutoff; days is lookback, limit selects articles.',
                  'read_news':'Read a previously discovered article from offset to end.',
                  'get_history':'Read this account historical decisions with mature labels only.'}
    for tool in tools:
        fn=tool['function']
        if fn['name'] in descriptions:fn['description']=descriptions[fn['name']]
    return tools


class Research:
    def __init__(self, exp, agent, day, history):
        self.exp,self.agent,self.day,self.history=exp,agent,day,history
        self.evidence={};self.trace=[];self.handles={};self.articles={}
        self.handle_types={}

    def call(self,name,args):
        started=time.monotonic()
        token=policies.GLOBAL_ONLY.set(policies.total_only(self.exp['config']))
        try:
            try:result=self._call(name,args)
            except Exception as e:result={'status':'unavailable','reason':str(e)[:200]}
        finally:policies.GLOBAL_ONLY.reset(token)
        original=result
        result,context_limited=(original,False) if policies.total_only(self.exp['config']) else tool_router.bounded_result(original)
        eid=hashlib.sha256(store.dumps([self.agent,self.day,name,args,result]).encode()).hexdigest()[:20]
        result={'evidence_id':eid,'cutoff':cutoff(self.day).isoformat(),'data':result}
        self.evidence[eid]=result
        trace=dict(tool=name,args=args,result=result,seconds=round(time.monotonic()-started,3))
        if context_limited:trace['raw_result']=original
        self.trace.append(trace)
        return result

    def _call(self,name,args):
        exp=self.exp; rows=exp['dataset']['prices']; config=exp['config']
        if name=='get_prices':
            n=args.get('limit',60)
            if type(n) is not int or n<1 or (not policies.GLOBAL_ONLY.get() and n>500):raise ValueError('limit')
            bars=visible_bars(rows,self.day,config['holidays'])[-n:]
            handle=hashlib.sha256(store.dumps(bars).encode()).hexdigest()[:16];self.handles[handle]=bars
            self.handle_types[handle]='XAUUSD.FXCM'
            return {'rows':bars,'handle':handle,'symbol':'XAUUSD.FXCM','unit':'USD/troy_ounce'}
        if name=='get_history':return self.history[-(max(int(args.get('limit',5)),1) if policies.GLOBAL_ONLY.get() else min(max(int(args.get('limit',5)),1),20)):]
        if name=='search_news':
            lookback=int(args.get('days',7));limit=int(args.get('limit',10));q=str(args.get('query',''))
            if lookback<1 or limit<1 or (not policies.GLOBAL_ONLY.get() and (lookback>90 or limit>20 or len(q)>200)):raise ValueError('query_limit')
            if config['driver']=='fixture':return {'status':'fixture_has_no_news','rows':[]}
            cut=cutoff(self.day).replace(tzinfo=None)
            rows=data.news_slice((cut-timedelta(days=lookback)).isoformat(sep=' '),cut.isoformat(sep=' '),args.get('source','sina'))
            filtered=[]
            for r in rows:
                text=re.sub(r'<(script|style)\b[^>]*>.*?</\1>',' ',r.get('content',''),flags=re.S|re.I)
                text=re.sub('<[^>]*>',' ',text)
                if q and not any(w.lower() in (r.get('title','')+text).lower() for w in q.split()):continue
                stamp=r.get('datetime') or r.get('pub_time','')
                if not stamp or stamp>cut.isoformat(sep=' '):continue
                item=dict(r,content=text,datetime=stamp);self.articles[r['record_id']]=item;filtered.append(item)
            filtered.sort(key=lambda x:(x.get('datetime',''),x['record_id']),reverse=True)
            return [{'record_id':r['record_id'],'title':r.get('title',''),'published_at':r.get('datetime'),'snippet':r['content'][:300]} for r in filtered[:limit]]
        if name=='read_news':
            r=self.articles[args['record_id']];offset=int(args.get('offset',0))
            if offset<0:raise ValueError('offset')
            return dict(record_id=r['record_id'],content=r['content'][offset:] if policies.GLOBAL_ONLY.get() else r['content'][offset:offset+12000],offset=offset,total=len(r['content']))
        if name in market_tools.TOOL_NAMES:
            return market_tools.call(self, name, args)
        if name == 'read_webpage':
            records=exp['dataset'].get(name,[])
            cut=cutoff(self.day).isoformat()
            from datetime import datetime
            selected=[r for r in records if r.get('available_at') and datetime.fromisoformat(r['available_at'])<=cutoff(self.day)
                      and (r.get('dataset')==args.get('dataset') if name!='read_webpage' else r.get('record_id')==args.get('record_id'))]
            return {'status':'ok' if selected else 'dataset_not_prepared','rows':selected if policies.GLOBAL_ONLY.get() else selected[:min(int(args.get('limit',300)),300)]}
        if name=='run_backtest':
            from .engine import execute,days,metrics
            acquired=self.handles[args['handle']]
            if self.handle_types.get(args['handle']) != 'XAUUSD.FXCM' or any(not {'bid_open','ask_open','bid_close','ask_close'} <= r.keys() for r in acquired):
                raise ValueError('SMA20_backtest_requires_get_prices_XAUUSD_handle; use_run_python_for_other_markets')
            cash,qty='10000','0';events=[]
            for index,r in enumerate(acquired):
                signal=sma(acquired,r['date'],config['holidays'])
                dec=hold() if signal is None else hold()|{'action':'rebalance','target_weight':signal if config['allow_short'] else max(0,signal)}
                if index==len(acquired)-1:dec=hold('research_terminal_exit')|{'action':'rebalance','target_weight':0}
                e=execute(cash,qty,dec,r,config);cash,qty=e['cash'],e['qty'];events.append(e|{'day':r['date']})
            points=events if policies.GLOBAL_ONLY.get() or len(events)<=100 else [events[round(i*(len(events)-1)/99)] for i in range(100)]
            return {'strategy':'SMA20','status':'ok_last_acquired_open_terminal_exit','metrics':metrics(events),'points':points,'trades':[e for e in events if e['delta']] if policies.GLOBAL_ONLY.get() else [e for e in events if e['delta']][-20:]}
        if name=='run_python':
            code=args.get('code','')
            if not policies.GLOBAL_ONLY.get() and len(code)>8000:raise ValueError('code_limit')
            # Image must be explicitly installed locally; never implicitly download executable code.
            image='python:3.11-slim'
            subprocess.run(['docker','image','inspect',image],check=True,capture_output=True,timeout=None if policies.GLOBAL_ONLY.get() else 5)
            script='import json,sys,resource\nresource.setrlimit(resource.RLIMIT_FSIZE,(8192,8192))\np=json.load(sys.stdin)\ndata=p["data"]\nexec(p["code"])'
            if policies.GLOBAL_ONLY.get():
                script=script.replace('resource.setrlimit(resource.RLIMIT_FSIZE,(8192,8192))\n','')
            name='goldbench-'+uuid.uuid4().hex
            command=['docker','run','--name',name,'--rm','--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges',
                '--user=65534:65534','--memory=512m','--cpus=1','--pids-limit=32','--ulimit','nofile=64:64','--tmpfs','/tmp:rw,noexec,nosuid,size=16m',
                '-i',image,'sh','-c','python -I -c "$1" > /tmp/out 2>&1; '+('cat /tmp/out' if policies.GLOBAL_ONLY.get() else 'head -c 8000 /tmp/out'),'sh',script]
            try:
                result=subprocess.run(command,input=store.dumps({'code':code,'data':self.handles[args['handle']]}),text=True,capture_output=True,timeout=None if policies.GLOBAL_ONLY.get() else 20)
                return {'stdout':result.stdout if policies.GLOBAL_ONLY.get() else result.stdout[:8000],'stderr':result.stderr if policies.GLOBAL_ONLY.get() else result.stderr[:1000],'exit_code':result.returncode}
            finally:
                subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=5)
        raise ValueError('unknown_tool')

def fixture_decision(research,model,repeat,group):
    decision=hold('工程测试驱动输出；不是模型研究结果，不参与正式排行榜。')
    if group=='extended':
        result=research.call('get_prices',{'limit':25}); bars=result['data'].get('rows',[])
        if len(bars)>1:
            change=bars[-1]['bid_close']/bars[-2]['bid_close']-1
            direction=1 if change>0 else -1
            strength=[.25,.5,.75,1.][int(hashlib.sha256(model.encode()).hexdigest()[:2],16)%4]
            weight=direction*strength
            if not research.exp['config']['allow_short']:weight=max(0,weight)
            decision.update(action='rebalance',target_weight=weight,expected_return=change,prob_up=.6 if direction>0 else .4,evidence_ids=[result['evidence_id']])
    return decision, {'cost':0,'input_tokens':0,'output_tokens':0,'calls':0,'status':'fixture'}

def poe_request(exp,agent,day,body,timeout=120,retry=0):
    # Backward-compatible hook for tests/judges; dispatches by experiment driver.
    return providers.request(exp,agent,day,body,timeout,retry)

def incremental_input_upper(messages, tools, previous=None):
    """Reuse observed provider input count; bound every added context byte.

    Removed items give no credit. All added messages/schemas count at one token
    per UTF-8 byte, plus protocol slack. Provider reservations remain independent.
    """
    from collections import Counter
    current = [store.dumps(x) for x in messages + tools]
    if previous is None:
        return len(store.dumps([messages, tools]).encode()) + 1024
    old, observed = previous
    additions = Counter(current) - Counter(old)
    return observed + sum(len(item.encode()) * n for item, n in additions.items()) + 1024


def run_agent(research,model,repeat,group,account):
    if research.exp['config']['driver']=='fixture':return fixture_decision(research,model,repeat,group)
    total_only = policies.total_only(research.exp['config'])
    registry = unrestricted_tools() if total_only else TOOLS
    router = tool_router.ToolRouter(registry, SUBMIT) if group == 'extended' else None
    system=prompts.system_prompt(extended=router is not None)
    deepseek = model == providers.DEEPSEEK
    input_limit, output_limit = (30000, 10000) if deepseek else (20000, 2000)
    output_reserve = 3000 if deepseek else 600
    def visible_catalog(name, field):
        return [r.get(field) for r in research.exp['dataset'].get(name, [])
                if r.get('available_at') and market_tools.aware(r['available_at']) <= cutoff(research.day)]
    state={'cutoff':cutoff(research.day).isoformat(),'account':account,'rules':{k:research.exp['config'][k] for k in ('allow_short','spread','max_exposure')},'history':(research.history if total_only else research.history[-5:]) if group=='extended' else []}
    state.update(round_date=research.day,nominal_execution=research.day+'T07:00:00+08:00',
        prediction_end=next_day(research.day,research.exp['config']['holidays'])+'T07:00:00+08:00',
        budget={'cumulative_input':input_limit,'cumulative_output':output_limit,'seconds':600},
        data_catalog={'prices':'XAUUSD.FXCM','macro':visible_catalog('get_macro', 'dataset'),
                      'positioning':visible_catalog('get_positioning', 'dataset'),
                      'tushare_research':{k:market_tools.CATALOG[k] for k in ('window_parameters','history_policy','gram')},
                      'webpages':visible_catalog('read_webpage', 'record_id')})
    if total_only:
        state['data_catalog']['tushare_research']['window_parameters']='YYYY-MM-DD dates; end clamped to research day. Caller chooses lookback, limit and offset. Provider pagination and historical availability still apply; no framework window/row cap.'
        state['budget']={'experiment_total_token_limit':research.exp['config']['token_limit'],'scope':'input+cached_input+reasoning+output+calibration; all days share one cap','per_round_limits':None}
        state['research_policy']='仅全实验1M token硬上限；不分配每日额度，不限制研究次数、输出、思考力度或研究时间，不强制收尾。可用完预算而未完成整月。自行决定研究路径、何时提交和仓位；注册目录中的工具可以直接调用，router仅供按需加载定义。'
    if router:
        state['tool_directory'] = router.directory()
        state['prompt_version'] = prompts.PROMPT_VERSION
    messages=[{'role':'system','content':system},{'role':'user','content':store.dumps(state)}]
    usage={'cost':0,'input_tokens':0,'output_tokens':0,'calls':0,'routing_calls':0,'routing':[]}
    start=time.monotonic()
    previous_input = None
    while total_only or time.monotonic()-start<600:
        tools = router.schemas() if router else [SUBMIT]
        if total_only:
            messages.append({'role':'user','content':store.dumps({'experiment_token_budget':store.token_budget(research.exp['id'],research.exp['config']['token_limit'])})})
        if usage['input_tokens'] and not total_only:
            budget_state={'remaining_input':input_limit-usage['input_tokens'],'remaining_output':output_limit-usage['output_tokens'],
                          'research_calls_used':usage['calls'],'remaining_seconds':round(600-(time.monotonic()-start))}
            if router:budget_state['routing_selections_used']=router.attempts
            messages.append({'role':'user','content':store.dumps(budget_state)})
        remaining=output_limit-usage['output_tokens']; upper=incremental_input_upper(messages, tools, previous_input) if deepseek else len(store.dumps([messages,tools]).encode())+1024
        if remaining<=0 and not total_only:break
        final=not total_only and (time.monotonic()-start>=480 or usage['output_tokens']>=output_limit-output_reserve or usage['input_tokens']+upper>=input_limit-4000)
        request_tools=[SUBMIT] if final else tools
        if final:
            messages.append({'role':'user','content':'本轮研究已结束，剩余预算预留给最终提交。现在只允许调用 submit_decision；研究工具和 select_tools 均已关闭，不能重新加载。请基于已有证据提交，证据不足可保持持仓或空仓并说明原因。'})
        upper=incremental_input_upper(messages, request_tools, previous_input) if deepseek else len(store.dumps([messages,request_tools]).encode())+1024
        if not total_only and usage['input_tokens']+upper>input_limit and not (deepseek and final):break
        final_limit=3000 if deepseek else 1400 if research.exp['config']['driver']=='dashscope' else 600
        body={'model':model,'messages':messages,'tools':request_tools,'max_tokens':min(final_limit if final else 3000 if deepseek else 800,remaining)}
        if total_only:body.pop('max_tokens',None)
        if final:body['tool_choice']={'type':'function','function':{'name':'submit_decision'}}
        body.update(research.exp['config'].get('model_parameters',{}).get(model,{}))
        request_items = [store.dumps(x) for x in messages + request_tools]
        try:
            result,cost=poe_request(research.exp,research.agent,research.day,body,None if total_only else 600-(time.monotonic()-start))
        except providers.TotalTokenBudgetExhausted:
            return None, usage|{'status':'total_token_budget_exhausted','messages':messages}
        u=result['usage'];previous_input=(request_items, u['prompt_tokens']);usage['cost']+=cost;usage['input_tokens']+=u['prompt_tokens'];usage['output_tokens']+=u['completion_tokens']
        message=result['choices'][0]['message']
        if research.exp['config']['driver']=='dashscope':
            # Provider usage still includes reasoning; archive the original response
            # in requests, but don't replay private reasoning as conversation input.
            message={k:v for k,v in message.items() if k!='reasoning_content'}
        messages.append(message)
        requests=message.get('tool_calls') or []
        if not requests:
            messages.append({'role':'user','content':'普通文字不能代替决策；可以继续研究，决定结束本轮时请调用submit_decision提交已有证据支持的操作与主要风险。'});continue
        # Freeze permissions to exactly the schema set advertised for this response.
        # A select_tools call cannot authorize new calls in the same assistant batch.
        advertised = {tool['function']['name'] for tool in request_tools}
        for request in requests:
            name=request['function']['name']
            args = None
            if name == 'select_tools' and router and name in advertised:
                began = time.monotonic()
                try:
                    try:args=json.loads(request['function']['arguments'])
                    except (ValueError,TypeError):pass
                    reply=router.select(args)
                except ValueError as exc:
                    reply={'error':str(exc),'active_research_tools':list(tool_router.CORE+router.selected)}
                usage['routing_calls']=router.attempts
                usage['routing'].append({'tool':name,'args':args,'result':reply,'seconds':round(time.monotonic()-began,3)})
            else:
                try:
                    if final and name != 'submit_decision':
                        raise ValueError('research_closed_for_submission; call_submit_decision_with_existing_evidence')
                    if name not in advertised and not (total_only and group=='extended' and name in {t['function']['name'] for t in registry}):
                        raise ValueError('tool_not_active; select_tools_then_call_on_next_request')
                    args=json.loads(request['function']['arguments'])
                    if name=='submit_decision':
                        decision=validate_decision(args,research.exp['config'],research.evidence)
                        if not total_only and time.monotonic()-start>600:raise ValueError('deadline_exceeded')
                        usage['messages']=messages
                        return decision,usage
                    if group!='extended' or final:reply={'error':'research_budget_exhausted'}
                    else:usage['calls']+=1;reply=research.call(name,args)
                except (ValueError,KeyError,TypeError) as exc:
                    reply={'error':str(exc)}
            messages.append({'role':'tool','tool_call_id':request['id'],'content':store.dumps(reply)})
    return None,usage|{'status':'budget_or_timeout','messages':messages}
