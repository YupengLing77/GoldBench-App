import pytest
from fastapi.testclient import TestClient
from goldbench import store,data,worker
from goldbench.api import app

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path);store.init()
    return TestClient(app)

def create(client,**kwargs):
    r=client.post('/api/experiments',json={'start':'2026-10-01','end':'2026-10-09',**kwargs})
    assert r.status_code==200,r.text
    return r.json()['id']

def test_full_workflow(client):
    ident=create(client);worker.run(ident)
    detail=client.get('/api/experiments/'+ident).json()
    assert detail['status']=='completed_fixture';assert len(detail['accounts'])==19
    assert all(len(a['events'])==8 for a in detail['accounts'])
    assert all(a['events'][-1]['terminal'] and float(a['events'][-1]['qty'])==0 for a in detail['accounts'])
    assert not any(a['eligible'] for a in detail['accounts'])
    assert next(a for a in detail['accounts'] if a['model']=='cash')['metrics']['return']==0
    before=store.events(ident);worker.run(ident);assert before==store.events(ident)
    assert client.get(f'/api/experiments/{ident}/export?format=csv').status_code==200
    blind=client.get(f'/api/experiments/{ident}/blind').json()
    assert blind and all('model' not in r and 'equity' not in r and 'label' not in r for r in blind)
    r=client.post(f'/api/experiments/{ident}/reviews',json={'review_id':blind[0]['review_id'],'scores':[2,None,3],'rationale':'原始证据暂不可核查','reviewer':'test'})
    assert r.status_code==200
    assert client.get('/api/experiments/'+ident).json()['reviews'][-1]['total'] is None

def test_missing_quote(client):
    rows=[r for r in data.fixture('2026-10-01','2026-10-09') if r['date']!='2026-10-02']
    ident=create(client,dataset='import',rows=rows);worker.run(ident)
    d=client.get('/api/experiments/'+ident).json()
    assert d['status']=='incomplete'
    ev=d['accounts'][0]['events']
    assert ev[0]['label'] is None;assert ev[1]['status']=='data_unexecutable'
    assert ev[1]['equity'] is None;assert ev[2]['status']=='platform_paused'

def test_poe_no_fallback(client):
    assert client.post('/api/experiments',json={'driver':'poe','dataset':'fixture'}).status_code==422
    assert store.experiments()==[]

def test_budget(client):
    rid=store.reserve('test','a','2026-10-01',9,10)
    with pytest.raises(RuntimeError):store.reserve('test','b','2026-10-01',2,10)
    assert store.budget()['reserved']==9
    store.finish_request(rid,1,{'usage':{}})
    assert store.budget()['remaining']==9

def test_origin(client):
    assert client.post('/api/experiments',json={},headers={'origin':'https://other.example'}).status_code==403

def test_uncertain_resume(client):
    ident=create(client);store.status(ident,'blocked');store.reserve(ident,'x','2026-10-01',.01,10)
    assert client.post(f'/api/experiments/{ident}/resume').status_code==409

def test_invalid_config(client):
    for config in ({'start':'2026-11-10','end':'2026-10-01'},{'models':['unknown']},{'model_parameters':{'GPT-5-mini':{'tools':[]}}}):
        assert client.post('/api/experiments',json=config).status_code==422
