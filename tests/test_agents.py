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
