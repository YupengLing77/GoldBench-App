"""Paid-call boundary tests use fake credentials and an in-memory HTTP stub."""
import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from goldbench import data, providers, store, worker
from goldbench.api import Config, build, preflight
from goldbench.trial import load_credentials


@pytest.fixture
def exp(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init()
    config=Config(driver='dashscope',dataset='import',models=[providers.QWEN],groups=['extended'],repeats=1,
                  token_limit=87000,monthly_budget=1,paid_enabled=True,start='2026-10-12',end='2026-10-14',
                  rows=data.fixture('2026-10-12','2026-10-14'),prices={providers.QWEN:{'input':1.65,'output':4.951}})
    frozen,dataset,_=build(config)
    ident=store.create(frozen,dataset,initial_status='prepared')
    monkeypatch.setenv('DASHSCOPE_API_KEY','fake-key-for-offline-test')
    return store.get(ident)


def fake_http(monkeypatch,responses):
    calls=[]
    class Response:
        def __init__(self,status=200,payload=None):self.status_code=status;self.payload=payload;self.headers={}
        def json(self):return self.payload
        def read(self):return b''
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def iter_lines(self):
            payload=self.payload
            chunks={'usage':payload.get('usage'),'choices':[{'delta':c.get('message',{}),'finish_reason':'tool_calls'} for c in payload.get('choices',[])]}
            yield 'data: '+json.dumps(chunks)
            yield 'data: [DONE]'
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def stream(self,method,url,headers,json):return self.post(url,headers,json)
        def post(self,url,headers,json):
            calls.append({'url':url,'body':__import__('json').loads(__import__('json').dumps(json))})
            item=responses[len(calls)-1]
            if isinstance(item,Exception):raise item
            return Response(*item)
    monkeypatch.setattr(providers.httpx,'Client',Client)
    return calls


def response(value=1,usage=None):
    return {'usage':usage if usage is not None else {'prompt_tokens':100,'completion_tokens':20,'total_tokens':120},
            'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[{'id':str(value),'type':'function',
                'function':{'name':'calibration_echo','arguments':json.dumps({'value':value})}}]}}]}


def body():return {'model':providers.QWEN,'messages':[{'role':'user','content':'test'}],'max_tokens':128}


def test_atomic_token_reservations_enforce_87k_under_concurrency(exp):
    def reserve(i):
        try:return store.reserve(exp['id'],str(i),'2026-10-12',.01,1,tokens=30000,token_limit=87000)
        except RuntimeError:return None
    with ThreadPoolExecutor(max_workers=4) as pool:rids=list(pool.map(reserve,range(4)))
    assert len([r for r in rids if r])==2
    budget=store.token_budget(exp['id'],87000)
    assert budget['reserved']==60000 and budget['remaining']==27000
    store.finish_request(next(r for r in rids if r),.001,{},tokens=500)
    assert store.token_budget(exp['id'],87000)=={'limit':87000,'used':500,'reserved':30000,'remaining':56500}


def test_dashscope_beijing_request_reports_actual_usage_without_search(exp,monkeypatch):
    calls=fake_http(monkeypatch,[(200,response())])
    result,cost=providers.request(exp,'a','2026-10-12',body())
    assert calls[0]['url']==providers.DASHSCOPE_URL
    assert calls[0]['body']['enable_thinking'] is True and calls[0]['body']['enable_search'] is False
    assert calls[0]['body']['max_completion_tokens']==128 and 'max_tokens' not in calls[0]['body']
    assert calls[0]['body']['parallel_tool_calls'] is False
    assert store.token_budget(exp['id'],87000)['used']==120
    assert store.token_budget(exp['id'],87000)['reserved']==0
    assert cost==pytest.approx((100*1.65+20*4.951)/1e6)
    with store.db() as con:payload=con.execute('SELECT payload FROM requests').fetchone()[0]
    assert 'fake-key-for-offline-test' not in payload


def test_budget_rejection_happens_before_http(exp,monkeypatch):
    calls=fake_http(monkeypatch,[])
    exp['config']['token_limit']=1
    with pytest.raises(RuntimeError,match='token_budget_exhausted'):providers.request(exp,'a','2026-10-12',body())
    assert not calls


@pytest.mark.parametrize('status',[400,401,403,404])
def test_definite_rejections_release_both_reservations(exp,monkeypatch,status):
    fake_http(monkeypatch,[(status,{})])
    with pytest.raises(RuntimeError,match=f'dashscope_http_{status}'):providers.request(exp,'a','2026-10-12',body())
    assert store.token_budget(exp['id'],87000)['reserved']==0
    assert store.budget()['reserved']==0


@pytest.mark.parametrize('payload',[{},response(usage={'prompt_tokens':100}),response(usage={'prompt_tokens':100,'completion_tokens':1,'total_tokens':10})])
def test_unknown_usage_keeps_reservation_and_stops(exp,monkeypatch,payload):
    fake_http(monkeypatch,[(200,payload)])
    with pytest.raises(RuntimeError):providers.request(exp,'a','2026-10-12',body())
    assert store.token_budget(exp['id'],87000)['reserved']>0
    with store.db() as con:assert con.execute('SELECT status FROM requests').fetchone()[0]=='inflight'


def test_server_error_is_not_retried_and_keeps_reservation(exp,monkeypatch):
    calls=fake_http(monkeypatch,[(500,{})])
    with pytest.raises(RuntimeError,match='dashscope_http_500'):providers.request(exp,'a','2026-10-12',body())
    assert len(calls)==1 and store.token_budget(exp['id'],87000)['reserved']>0


def test_rate_limit_retry_uses_same_experiment_budget(exp,monkeypatch):
    calls=fake_http(monkeypatch,[(429,{}),(200,response())])
    monkeypatch.setattr(providers.time,'sleep',lambda x:None)
    providers.request(exp,'a','2026-10-12',body())
    assert len(calls)==2 and store.token_budget(exp['id'],87000)['used']==120
    with store.db() as con:assert con.execute('SELECT COUNT(*) FROM request_tokens').fetchone()[0]==2


def test_calibration_checks_tool_result_continuation_and_is_checkpointed(exp,monkeypatch):
    calls=fake_http(monkeypatch,[(200,response(1)),(200,response(2))])
    event=providers.calibrate(exp)
    assert event['status']=='calibration_passed'
    assert any(m['role']=='tool' and json.loads(m['content'])=={'value':1} for m in calls[1]['body']['messages'])
    assert calls[1]['body']['tool_choice']=='auto'
    assert calls[1]['body']['max_completion_tokens']==768
    assert store.token_budget(exp['id'],87000)['used']==240
    providers.calibrate(exp)
    assert len(calls)==2
    with store.db() as con:
        orphans=con.execute('SELECT COUNT(*) FROM requests r LEFT JOIN events e ON r.experiment=e.experiment AND r.agent=e.agent AND r.day=e.day WHERE e.day IS NULL').fetchone()[0]
    assert orphans==0


def test_failed_calibration_blocks_worker_before_trade(exp,monkeypatch):
    calls=fake_http(monkeypatch,[(200,response(2))])
    worker.run(exp['id'])
    assert store.get(exp['id'])['status']=='blocked'
    assert len(calls)==1
    assert all(e['group']=='calibration' for e in store.events(exp['id']))


def test_single_account_roster_and_token_preflight(exp,monkeypatch):
    config=Config(**(exp['config']|{'rows':exp['dataset']['prices']}))
    assert len(worker.roster(config.model_dump()))==4
    assert len([r for r in worker.roster(config.model_dump()) if r[2]=='extended'])==1
    assert preflight(config)['can_start']
    config.end=__import__('datetime').date(2026,10,16)
    assert not preflight(config)['can_start']


def test_credentials_file_only_loads_allowed_keys_without_evaluation(tmp_path,monkeypatch):
    path=tmp_path/'local.env';path.write_text('DASHSCOPE_API_KEY="fake-key"\nTUSHARE_TOKEN=fake-token\n')
    monkeypatch.delenv('DASHSCOPE_API_KEY',raising=False);monkeypatch.delenv('TUSHARE_TOKEN',raising=False)
    load_credentials(path)
    assert __import__('os').environ['DASHSCOPE_API_KEY']=='fake-key'
    path.write_text('UNEXPECTED=secret-value\n')
    with pytest.raises(ValueError,match='invalid_line') as err:load_credentials(path)
    assert 'secret-value' not in str(err.value)


def test_followup_experiments_share_original_87000_cap(exp):
    rid=store.reserve(exp['id'],'a','2026-10-12',.01,1,tokens=30000,token_limit=87000)
    store.finish_request(rid,.01,{},tokens=27762)
    child=store.create(exp['config']|{'token_budget_parent':exp['id']},exp['dataset'],initial_status='prepared')
    assert store.token_budget(child,87000)['remaining']==59238
    store.reserve(child,'b','2026-10-12',.01,1,tokens=50000,token_limit=87000)
    with pytest.raises(RuntimeError,match='token_budget_exhausted'):
        store.reserve(exp['id'],'c','2026-10-13',.01,1,tokens=10000,token_limit=87000)
    assert store.token_budget(exp['id'],87000)['reserved']==50000


def test_qwen_final_submission_has_reasoning_room_without_replaying_reasoning(exp,monkeypatch):
    from goldbench import agents
    research=agents.Research(exp,'A','2026-10-12',[])
    seen=[]
    def request(e,a,d,body,timeout=120):
        seen.append(body)
        if len(seen)==1:
            fn,args='get_prices',{'limit':1}
            inp=9000
        else:
            assert body['max_tokens']==1400
            assert all('reasoning_content' not in m for m in body['messages'])
            fn='submit_decision'
            args={'action':'hold','target_weight':None,'expected_return':0,'prob_up':.5,'rationale':'已有可见证据不足以调整仓位','evidence_ids':list(research.evidence)}
            inp=100
        return {'usage':{'prompt_tokens':inp,'completion_tokens':50},'choices':[{'message':{
            'role':'assistant','reasoning_content':'private reasoning','tool_calls':[{'id':str(len(seen)),'type':'function','function':{'name':fn,'arguments':json.dumps(args)}}]}}]},0
    monkeypatch.setattr(agents,'poe_request',request)
    result,usage=agents.run_agent(research,providers.QWEN,1,'extended',{'cash':'10000','qty':'0'})
    assert result and len(seen)==2


def test_deepseek_beijing_uses_same_key_and_independent_million_cap(exp, monkeypatch):
    calls = fake_http(monkeypatch, [(200,response())])
    exp['config'].update(models=[providers.DEEPSEEK], token_limit=1000000,
                         prices={providers.DEEPSEEK:{'input':1.5,'output':4.5}})
    result, cost = providers.request(exp, 'a', '2026-10-12', body() | {'model':providers.DEEPSEEK})
    assert calls[0]['url'] == providers.DASHSCOPE_URL
    assert calls[0]['body']['model'] == providers.DEEPSEEK
    assert calls[0]['body']['enable_search'] is False
    assert store.token_budget(exp['id'],1000000)['used'] == 120
    assert Config(**(exp['config'] | {'token_limit':1000000})).token_limit == 1000000


def test_deepseek_larger_budget_still_masks_future_prices_and_dataset(exp, monkeypatch):
    from goldbench import agents
    exp['config']['models'] = [providers.DEEPSEEK]
    exp['dataset']['read_webpage'] = [dict(record_id='future-private-marker',available_at='2026-11-01T00:00:00+08:00')]
    research = agents.Research(exp, 'A', '2026-10-12', [])
    seen = []
    def request(e,a,d,b,timeout=120):
        seen.append(b)
        assert b['max_tokens'] == 3000
        assert 'bid_open' not in b['messages'][1]['content']
        if len(seen)==1:
            name, args = 'get_prices', {'limit':1}
        else:
            name, args = 'submit_decision', dict(action='hold',target_weight=None,expected_return=0,prob_up=.5,
                                               rationale='保持数量',evidence_ids=list(research.evidence))
        return {'usage':{'prompt_tokens':100,'completion_tokens':30},'choices':[{'message':{'role':'assistant',
            'tool_calls':[{'id':str(len(seen)),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}]},0
    monkeypatch.setattr(agents,'poe_request',request)
    decision,usage = agents.run_agent(research,providers.DEEPSEEK,1,'extended',{'cash':'10000','qty':'0'})
    assert decision and usage['calls']==1
    assert research.trace[0]['result']['data']['rows'][-1]['date'] <= '2026-10-10'
    assert 'future-private-marker' not in store.dumps(seen)


def test_incremental_input_bound_counts_additions_without_crediting_removals():
    from goldbench.agents import incremental_input_upper
    old = [{'role':'system','content':'金'*1000}]
    current = old + [{'role':'tool','content':'known result'}]
    previous = ([store.dumps(x) for x in old], 1100)
    assert incremental_input_upper(current, [], previous) == 1100 + len(store.dumps(current[-1]).encode()) + 1024
    assert incremental_input_upper([], [], previous) == 2124


def test_final_mode_rejects_research_with_correct_reason_and_submission_guidance(exp, monkeypatch):
    from goldbench import agents
    research=agents.Research(exp,'A','2026-10-12',[])
    seen=[]
    def request(e,a,d,b,timeout=120):
        seen.append(json.loads(json.dumps(b)))
        if len(seen)==1:
            name,args='get_prices',{'limit':1};inp=9000
        elif len(seen)==2:
            assert [t['function']['name'] for t in b['tools']]==['submit_decision']
            assert any('研究已结束' in m.get('content','') for m in b['messages'] if m['role']=='user')
            name,args='get_risk_index',{};inp=10
        else:
            replies=[json.loads(m['content']) for m in b['messages'] if m['role']=='tool']
            assert replies[-1]['error'].startswith('research_closed_for_submission')
            name,args='submit_decision',dict(action='hold',target_weight=None,expected_return=0,prob_up=.5,
                                           rationale='保持数量',evidence_ids=list(research.evidence));inp=10
        return {'usage':{'prompt_tokens':inp,'completion_tokens':20},'choices':[{'message':{'role':'assistant','tool_calls':[
            {'id':str(len(seen)),'function':{'name':name,'arguments':json.dumps(args)}}]}}]},0
    monkeypatch.setattr(agents,'poe_request',request)
    decision,usage=agents.run_agent(research,providers.QWEN,1,'extended',{'cash':'10000','qty':'0'})
    assert decision and usage['calls']==1 and len(seen)==3
    assert [t['tool'] for t in research.trace]==['get_prices']
