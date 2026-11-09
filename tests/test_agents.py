import json
import pytest
from goldbench import agents,store,data,worker,judges
from goldbench.api import Config,build

@pytest.fixture
def exp(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init()
    c,d,r=build(Config(start='2026-10-01',end='2026-10-02'))
    ident=store.create(c,d)
    return store.get(ident)

def test_tool_future_boundary_and_capability_errors(exp):
    research=agents.Research(exp,'A','2026-10-08',[])
    p=research.call('get_prices',{'limit':5})
    assert p['data']['rows'][-1]['date']<='2026-10-06'
    assert research.call('get_macro',{'dataset':'cn_pmi'})['data']['status']=='dataset_not_prepared'
    assert research.call('read_news',{'record_id':'invented'})['data']['status']=='unavailable'
    assert research.call('get_prices',{'limit':1000})['data']['status']=='unavailable'

def test_poe_tool_loop_mocked_no_network(exp,monkeypatch):
    exp['config']['driver']='poe'
    research=agents.Research(exp,'A','2026-10-01',[])
    seen=[]
    def mocked(exp,agent,day,body,timeout=120):
        seen.append(body)
        dec={'action':'hold','target_weight':None,'expected_return':0,'prob_up':.5,'rationale':'当前没有充分的交易证据，保留原数量。','evidence_ids':[]}
        return {'usage':{'prompt_tokens':100,'completion_tokens':50},'choices':[{'message':{'role':'assistant','tool_calls':[{'id':'x','type':'function','function':{'name':'submit_decision','arguments':json.dumps(dec)}}]}}]},.001
    monkeypatch.setattr(agents,'poe_request',mocked)
    decision,usage=agents.run_agent(research,'GPT-5-mini',1,'base',{'cash':'10000','qty':'0'})
    assert decision['action']=='hold';assert usage['input_tokens']==100
    assert len(seen[0]['tools'])==1

def test_blind_dual_judge_disagreement_and_human_override(exp,monkeypatch):
    worker.run(exp['id']);captured=[]
    def mock(exp,aid,day,body,timeout=120):
        package=json.loads(body['messages'][1]['content']);captured.append(package)
        assert 'label' not in package and 'model' not in package and 'equity' not in package
        score=4 if body['model']==judges.JUDGES[0] else 1
        obj={'scores':[score]*3,'reasons':['依据对应正确']*3,'evidence_ids':[],'critical_dispute':False}
        return {'choices':[{'message':{'content':json.dumps(obj)}}]},.001
    monkeypatch.setattr(judges,'poe_request',mock)
    judges.run(exp['id']);result=judges.summary(exp['id'])
    assert captured and all(r['review_required'] and not r['final'] for r in result)
    target=result[0]
    review={'agent':target['agent'],'day':target['day'],'scores':[3,3,3]}
    with store.db() as c:c.execute('INSERT INTO reviews VALUES(?,?,?,?,?)',(exp['id'],target['agent'],target['day'],store.dumps(review),store.now()))
    updated=judges.summary(exp['id'])[0]
    assert updated['final'];assert updated['total']==75

def test_judge_invalid_output_is_not_zero():
    with pytest.raises(ValueError):judges.validate({'scores':[5,1,2],'reasons':['a']*3,'evidence_ids':[],'critical_dispute':False},{})


def test_live_monitor_reads_calls_without_prompts_or_private_reasoning(exp):
    from goldbench.api import live
    rid=store.reserve(exp['id'],'agent','2026-10-01',.001,10,tokens=100)
    store.finish_request(rid,.001,{'request':{'messages':[{'role':'system','content':'PRIVATE_PROMPT_MARKER'}]},
        'response':{'usage':{'prompt_tokens':10,'completion_tokens':5},'choices':[{'message':{
            'reasoning_content':'PRIVATE_REASONING_MARKER','tool_calls':[{'id':'call-1','function':{
                'name':'get_prices','arguments':'{"limit": 5}'}}]}}]}},tokens=15)
    result=live(exp['id']);serialized=store.dumps(result)
    assert 'PRIVATE_PROMPT_MARKER' not in serialized and 'PRIVATE_REASONING_MARKER' not in serialized
    assert 'dataset' not in result and result['activity'][0]['name']=='get_prices'
    assert result['activity'][0]['arguments']=={'limit':5}
    assert result['token_budget']['used']==15 and result['requests_completed']==1
    assert result['policy']['search_enabled'] is False


def test_live_monitor_distinguishes_final_stage_rejection_from_execution(exp):
    from goldbench.api import live
    def saved(day, request, response):
        rid=store.reserve(exp['id'],'agent',day,.001,10,tokens=100)
        store.finish_request(rid,.001,{'request':request,'response':{'choices':[{'message':response}]}},tokens=10)
    tool_call={'id':'same-call-id','function':{'name':'get_risk_index','arguments':'{}'}}
    saved('2026-10-01',{'tools':[agents.SUBMIT]}, {'tool_calls':[tool_call]})
    saved('2026-10-01',{'messages':[{'role':'tool','tool_call_id':'same-call-id','content':json.dumps({'error':'tool_not_active; select_tools_then_call_on_next_request'})}]},{})
    saved('2026-10-02',{'tools':agents.TOOLS}, {'tool_calls':[tool_call]})
    saved('2026-10-02',{'messages':[{'role':'tool','tool_call_id':'same-call-id','content':json.dumps({'evidence_id':'visible','data':{'status':'ok','rows':[]}})}]},{})
    result=live(exp['id'])
    blocked,executed=result['activity']
    assert blocked['outcome']=='rejected' and blocked['reason']=='final_submission_only'
    assert '未读取数据' in blocked['explanation']
    assert executed['outcome']=='succeeded'
    assert 'evidence_id' not in blocked['result']
