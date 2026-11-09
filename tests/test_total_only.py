"""The new policy must not silently inherit bounded-agent research quotas."""
import json
import pytest
from goldbench import agents, data, market_tools, policies, providers, store, worker
from goldbench.api import Config, build, preflight
from test_qwen_trial import fake_http, response


@pytest.fixture
def experiment(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init()
    monkeypatch.setenv('DASHSCOPE_API_KEY','offline-fake-key')
    c=Config(driver='dashscope',dataset='import',models=[providers.FLASH],groups=['extended'],repeats=1,
             token_limit=1000000,research_policy='total_tokens_only',paid_enabled=True,monthly_budget=.000001,
             start='2026-10-01',end='2026-10-30',rows=data.fixture('2026-10-01','2026-10-30'),
             prices={providers.FLASH:{'input':.35,'output':1.4}})
    frozen,dataset,_=build(c);ident=store.create(frozen,dataset,initial_status='prepared')
    return store.get(ident)


def test_total_only_preflight_does_not_require_budget_to_finish_month(experiment):
    exp=experiment
    with store.db() as con:
        rid=store.reserve(exp['id'],'prior','2026-10-01',0,None,tokens=999000,token_limit=1000000)
        store.finish_request(rid,0,{},tokens=999000)
    c=Config(**(exp['config']|{'rows':exp['dataset']['prices']}))
    assert preflight(c)['can_start']


def test_provider_uses_native_effort_capacity_and_no_money_or_time_quota(experiment,monkeypatch):
    calls=fake_http(monkeypatch,[(200,response())])
    providers.request(experiment,'a','2026-10-01',{'model':providers.FLASH,'messages':[],
                       'max_tokens':1,'reasoning_effort':'low','parallel_tool_calls':False},timeout=None)
    b=calls[0]['body']
    assert b['max_completion_tokens']==providers.FLASH_MAX_OUTPUT
    assert 'reasoning_effort' not in b and 'parallel_tool_calls' not in b and 'tool_choice' not in b
    assert b['enable_search'] is False
    assert store.token_budget(experiment['id'],1000000)['used']==120
    assert store.token_budget(experiment['id'],1000000)['reserved']==0


def test_global_remaining_dynamically_bounds_output_and_stops_before_http(experiment,monkeypatch):
    rid=store.reserve(experiment['id'],'prior','2026-10-01',0,None,tokens=900000,token_limit=1000000)
    store.finish_request(rid,0,{},tokens=900000)
    calls=fake_http(monkeypatch,[(200,response())])
    b={'model':providers.FLASH,'messages':[]}
    providers.request(experiment,'a','2026-10-01',b,timeout=None)
    assert 90000<calls[0]['body']['max_completion_tokens']<100000
    with pytest.raises(providers.TotalTokenBudgetExhausted):
        providers.request(experiment,'a','2026-10-01',b|{'messages':[{'role':'user','content':'x'*200000}]},timeout=None)
    assert len(calls)==1


def test_many_calls_large_cumulative_usage_and_late_clock_do_not_force_submit(experiment,monkeypatch):
    research=agents.Research(experiment,'A','2026-10-12',[])
    seen=[]
    from itertools import count
    monkeypatch.setattr(agents.time,'monotonic',count(0,10000).__next__)
    monkeypatch.setattr(research,'_call',lambda name,args:{'rows':[]})
    def request(e,a,d,b,timeout=120):
        seen.append(b)
        assert timeout is None and 'max_tokens' not in b and 'tool_choice' not in b
        assert 'get_prices' in {t['function']['name'] for t in b['tools']}
        if len(seen)<=12:
            # Direct calls to registered optional tools are allowed without a routing turn.
            name,args='get_us_yields',{}
        else:
            name,args='submit_decision',dict(action='hold',target_weight=None,expected_return=0,prob_up=.5,
                                           rationale='自主研究后保持数量',evidence_ids=list(research.evidence))
        return {'usage':{'prompt_tokens':40000,'completion_tokens':5000},'choices':[{'message':{'role':'assistant',
            'tool_calls':[{'id':str(len(seen)),'function':{'name':name,'arguments':json.dumps(args)}}]}}]},0
    monkeypatch.setattr(agents,'poe_request',request)
    decision,usage=agents.run_agent(research,providers.FLASH,1,'extended',{'cash':'10000','qty':'0'})
    assert decision and usage['calls']==12 and usage['input_tokens']==520000 and usage['output_tokens']==65000
    assert 'cumulative_input' not in seen[0]['messages'][1]['content']


def test_unbounded_results_and_query_size_still_enforce_cutoff(experiment):
    research=agents.Research(experiment,'A','2026-10-12',[])
    result=research.call('get_prices',{'limit':10000})['data']
    assert len(store.dumps(result).encode())>5500
    assert all(r['date']<'2026-10-12' for r in result['rows'])
    token=policies.GLOBAL_ONLY.set(True)
    try:
        start,end,limit=market_tools.window({'start_date':'2020-01-01','end_date':'2026-12-01','limit':1000},'2026-10-12')
        assert limit==1000 and end.isoformat()=='2026-10-12'
    finally:policies.GLOBAL_ONLY.reset(token)
    assert 'maximum' not in store.dumps(agents.unrestricted_tools())


def test_budget_stop_has_no_fake_trade_and_no_failure_hold(experiment,monkeypatch):
    def run(*args,**kwargs):return None,{'status':'total_token_budget_exhausted','calls':3,'cost':0}
    monkeypatch.setattr(agents,'run_agent',run)
    identity=(providers.FLASH+'|extended|1',providers.FLASH,'extended',1)
    worker.round_task(experiment,identity,'2026-10-01',False,[])
    event=store.events(experiment['id'])[0]
    assert event['status']=='total_token_budget_exhausted'
    assert event['decision'] is None and not event['valid_submission'] and event['equity'] is None
    assert event.get('delta') is None and event['qty']=='0'


def test_scheduler_stops_at_global_budget_without_fake_future_rounds(experiment,monkeypatch):
    monkeypatch.setattr(providers,'calibrate',lambda e:None)
    monkeypatch.setattr(agents,'run_agent',lambda *a,**k:(None,{'status':'total_token_budget_exhausted','cost':0,'calls':0}))
    worker.run(experiment['id'])
    assert store.get(experiment['id'])['status']=='token_budget_exhausted'
    events=[e for e in store.events(experiment['id']) if e['group']=='extended']
    assert len(events)==1 and events[0]['day']=='2026-10-01' and events[0]['equity'] is None


def test_streaming_assembles_fragmented_parallel_tool_calls():
    chunks=[{'choices':[{'delta':{'role':'assistant','reasoning_content':'private'}}]},
            {'choices':[{'delta':{'tool_calls':[{'index':0,'id':'a','function':{'name':'get_prices','arguments':'{"li'}},
                                                 {'index':1,'id':'b','function':{'name':'get_us_yields','arguments':'{'}}]}}]},
            {'choices':[{'delta':{'tool_calls':[{'index':0,'function':{'arguments':'mit":5}'}},
                                                 {'index':1,'function':{'arguments':'}'}}]},'finish_reason':'tool_calls'}]},
            {'choices':[],'usage':{'prompt_tokens':100,'completion_tokens':10,'total_tokens':110}}]
    class Stream:
        def iter_lines(self):
            for chunk in chunks:yield 'data: '+json.dumps(chunk)
            yield 'data: [DONE]'
    r=providers.collect_stream(Stream());calls=r['choices'][0]['message']['tool_calls']
    assert calls[0]['function']=={'name':'get_prices','arguments':'{"limit":5}'}
    assert calls[1]['function']=={'name':'get_us_yields','arguments':'{}'}
    assert r['usage']['total_tokens']==110


def test_interrupted_stream_retains_reservation_and_outgoing_checkpoint(experiment,monkeypatch):
    class Stream:
        status_code=200
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def iter_lines(self):
            yield 'data: '+json.dumps({'choices':[{'delta':{'reasoning_content':'private reasoning'}}]})
            raise ConnectionResetError('offline simulated reset')
    class Client:
        def __init__(self,**kw):assert kw['timeout'] is None
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def stream(self,*a,**kw):return Stream()
    monkeypatch.setattr(providers.httpx,'Client',Client)
    with pytest.raises(ConnectionResetError):providers.request(experiment,'a','2026-10-01',{'model':providers.FLASH,'messages':[]},timeout=None)
    b=store.token_budget(experiment['id'],1000000)
    assert b['used']==0 and b['reserved']>393216
    with store.db() as con:r=con.execute('SELECT status,payload FROM requests').fetchone()
    assert r['status']=='inflight' and json.loads(r['payload'])['request']['stream'] is True
    assert 'private reasoning' not in r['payload']
