"""Router permissions and bounded prompts, tested through the actual agent loop."""
import json

import pytest
from goldbench import agents, store, tool_router
from goldbench.api import Config, build


@pytest.fixture
def research(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    store.init()
    config, dataset, _ = build(Config(start='2026-10-01', end='2026-10-02'))
    config['driver'] = 'poe'
    ident = store.create(config, dataset)
    item = agents.Research(store.get(ident), 'A', '2026-10-16', [])
    item.exp['dataset']['get_fx_rates'] = [dict(ts_code='EURUSD.FXCM', mid_close=1.1, available_at='2026-10-14T09:00:00+08:00')]
    item.exp['dataset']['get_gold_etf_shares'] = [dict(ts_code='518880.SH', shares=100, available_at='2026-10-14T09:00:00+08:00')]
    return item


def names(schemas):
    return {s['function']['name'] for s in schemas}


def submit(research):
    return dict(action='hold', target_weight=None, expected_return=0, prob_up=.5,
                rationale='根据可见研究证据保持数量', evidence_ids=list(research.evidence))


def scripted_run(research, monkeypatch, steps, group='extended'):
    seen = []
    def request(exp, agent, day, body, timeout=120):
        seen.append(json.loads(json.dumps(body)))
        assert len(seen) <= len(steps)
        step = steps[len(seen)-1]
        if callable(step):
            step = step(body)
        requests = step if isinstance(step, list) else [step]
        return {'usage':{'prompt_tokens':100,'completion_tokens':20},'choices':[{'message':{
            'role':'assistant','tool_calls':[{'id':f'{len(seen)}-{i}','type':'function', 'function':{
                'name':name,'arguments':arguments if isinstance(arguments,str) else json.dumps(arguments)}}
                for i,(name,arguments) in enumerate(requests)]}}]},0
    monkeypatch.setattr(agents, 'poe_request', request)
    decision, usage = agents.run_agent(research, 'GPT-5-mini', 1, group, {'cash':'10000','qty':'0'})
    return decision, usage, seen


def test_all_17_tools_are_discoverable_before_only_four_schemas_are_sent():
    router = tool_router.ToolRouter(agents.TOOLS, agents.SUBMIT)
    directory = router.directory()
    catalog = set(directory['core']) | {name for tools in directory['categories'].values() for name in tools}
    assert len(catalog) == 17
    assert catalog == names(agents.TOOLS)
    assert names(router.schemas()) == {'submit_decision','select_tools','get_prices','get_history'}
    assert len(store.dumps(router.schemas()).encode()) < .4*len(store.dumps([agents.SUBMIT]+agents.TOOLS).encode())


def test_category_selection_replacement_capacity_and_fresh_round():
    router = tool_router.ToolRouter(agents.TOOLS, agents.SUBMIT)
    router.select({'categories':['risk_news']})
    assert len(router.schemas()) == 7
    assert names(router.schemas()) >= {'search_news','read_news','get_risk_index'}
    router.select({'categories':['rates_fx']})
    assert 'search_news' not in names(router.schemas())
    assert names(router.schemas()) >= {'get_us_yields','get_fx_rates'}
    router.select({'tools':[]})
    assert names(router.schemas()) == {'submit_decision','select_tools','get_prices','get_history'}
    router.select({'tools':['run_python']})
    assert 'select_tools' in names(router.schemas())
    for _ in range(6):
        router.select({'tools':['read_webpage']})
    router.select({'categories':list(tool_router.GROUPS)})
    assert names(router.schemas()) == names(agents.TOOLS) | {'select_tools','submit_decision'}
    assert 'run_python' not in names(tool_router.ToolRouter(agents.TOOLS, agents.SUBMIT).schemas())


@pytest.mark.parametrize('selection', [{'categories':True}, {'tools':['unknown']},
                                       {'tools':'get_fx_rates'}, {'tools':[True]}, {'categories':['unknown']},
                                       {'tool':['get_fx_rates']}, {}, None])
def test_invalid_selection_is_atomic_and_counts_attempt(selection):
    router = tool_router.ToolRouter(agents.TOOLS, agents.SUBMIT)
    router.select({'tools':['get_fx_rates']})
    before = names(router.schemas())
    with pytest.raises(ValueError):
        router.select(selection)
    assert router.attempts == 2
    assert names(router.schemas()) == before


def test_selection_cannot_authorize_another_call_in_same_response(research, monkeypatch):
    decision, usage, seen = scripted_run(research, monkeypatch, [
        [('select_tools',{'tools':['get_fx_rates']}), ('get_fx_rates',{})],
        ('get_fx_rates',{}),
        lambda body: ('submit_decision',submit(research)),
    ])
    assert decision is not None
    assert usage['calls'] == 1 and usage['routing_calls'] == 1
    replies = [json.loads(m['content']) for m in seen[1]['messages'] if m['role']=='tool']
    assert replies[0]['status'] == 'selected'
    assert replies[1]['error'].startswith('tool_not_active')
    assert len(research.evidence) == 1 and research.trace[0]['tool'] == 'get_fx_rates'


def test_pivot_preserves_existing_evidence_but_revokes_old_tool(research, monkeypatch):
    decision, usage, seen = scripted_run(research, monkeypatch, [
        ('select_tools',{'tools':['get_fx_rates']}),
        ('get_fx_rates',{}),
        ('select_tools',{'tools':['get_gold_etf_shares']}),
        [('get_fx_rates',{}),('get_gold_etf_shares',{})],
        lambda body: ('submit_decision',submit(research)),
    ])
    assert decision['evidence_ids'] == list(research.evidence)
    assert len(research.evidence) == 2
    assert usage['calls'] == 2 and usage['routing_calls'] == 2
    assert len(usage['routing']) == 2
    assert 'get_fx_rates' not in names(seen[3]['tools'])
    assert 'get_gold_etf_shares' in names(seen[3]['tools'])


def test_malformed_router_arguments_consume_attempts_without_evidence(research, monkeypatch):
    decision, usage, seen = scripted_run(research, monkeypatch, [
        ('select_tools','{invalid json'),
        ('select_tools',{'tools':['bad']}),
        ('select_tools',{'categories':['unknown']}),
        ('select_tools',{'tools':[]}),
        lambda body: ('submit_decision',submit(research)),
    ])
    assert decision is not None
    assert usage['routing_calls'] == 4 and len(usage['routing']) == 4
    assert usage['calls'] == 0 and not research.evidence and not research.trace
    assert 'select_tools' in names(seen[-1]['tools'])


def test_base_group_never_gets_router_or_executes_research(research, monkeypatch):
    def must_not_execute(*a, **k):
        raise AssertionError('base group cannot execute research')
    monkeypatch.setattr(research, 'call', must_not_execute)
    decision, usage, seen = scripted_run(research, monkeypatch, [
        [('select_tools',{'tools':['get_fx_rates']}),('get_prices',{})],
        lambda body: ('submit_decision',submit(research)),
    ], group='base')
    assert decision is not None and usage['calls'] == usage['routing_calls'] == 0
    assert all(names(body['tools']) == {'submit_decision'} for body in seen)


def test_research_can_exceed_six_calls(research, monkeypatch):
    steps = [('select_tools',{'tools':['get_fx_rates']}), [('get_history',{'limit':1})]*8]
    def finish(body):
        # Token/time completion may still activate; call count itself is uncapped.
        assert len(research.trace) == 8
        return ('submit_decision',submit(research))
    decision, usage, seen = scripted_run(research, monkeypatch, steps+[finish])
    assert decision is not None and usage['calls'] == 8 and usage['routing_calls'] == 1
    assert len(research.trace) == 8


def test_large_utf8_results_are_visible_evidence_and_raw_is_archived(research, monkeypatch):
    original = {'content':'黄金风险分析'*4000,'offset':0,'total':24000,'record_id':'news-1'}
    monkeypatch.setattr(research, '_call', lambda *a: original)
    result = research.call('read_news',{'record_id':'news-1'})
    shown = result['data']
    assert len(store.dumps(result).encode()) < 6000
    assert shown['context_limited'] is True and shown['content']
    assert original['content'].startswith(shown['content'])
    assert research.evidence[result['evidence_id']] == result
    assert research.trace[0]['result'] == result
    assert research.trace[0]['raw_result'] == original
    assert len(original['content']) == 24000


def test_bounded_rows_keep_pagination_consistent():
    original = {'rows':[{'day':i,'content':'金'*1000} for i in range(20)],
                'total_visible':100,'offset':10,'returned':20,'next_offset':30,'handle':'handle-1'}
    result, changed = tool_router.bounded_result(original)
    assert changed and len(result['rows']) < 20
    assert result['returned'] == len(result['rows'])
    assert result['next_offset'] == 10+len(result['rows'])
    assert result['rows'][-1]['day'] == 19
    assert result['handle'] == 'handle-1'
    assert len(original['rows']) == 20


def test_worker_checkpoints_route_audit_with_decision(research, monkeypatch):
    from goldbench import worker
    route_events = [{'tool':'select_tools','args':{'tools':['get_fx_rates']},'result':{'status':'selected'}}]
    def run(*args, **kwargs):
        return submit(research), {'cost':0,'calls':0,'routing_calls':1,'routing':route_events}
    monkeypatch.setattr(agents, 'run_agent', run)
    worker.round_task(research.exp, ('A','GPT-5-mini','extended',1),'2026-10-01',False,[])
    event = store.events(research.exp['id'])[0]
    assert event['routing'] == route_events and event['routing_calls'] == 1
    assert event['valid_submission']


def test_approved_prompt_guides_without_tool_walkthrough():
    from goldbench import prompts
    text = prompts.system_prompt()
    assert '世界黄金协会' in text and '不是固定的预测公式' in text
    assert 'select_tools' in text and '下一次模型请求生效' in text
    assert 'submit_decision' in text and '交易成本' in text
    assert 'get_us_yields' not in text and 'get_prices' not in text
    base = prompts.system_prompt(False)
    assert '没有研究工具' in base and 'select_tools' not in base
