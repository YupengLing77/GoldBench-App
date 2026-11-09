"""GRAM additions: real source semantics, units, availability and evidence loop."""
import json

import pytest
from fastapi.testclient import TestClient
from goldbench import agents, data, market_tools, store
from goldbench.api import Config, app, build


@pytest.fixture
def research(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    store.init()
    config, dataset, _ = build(Config(start='2026-10-01', end='2026-10-02'))
    config['driver'] = 'poe'
    ident = store.create(config, dataset)
    return agents.Research(store.get(ident), 'A', '2026-10-16', [])


def provider(monkeypatch, responses):
    calls = []
    def fetch(api, params):
        calls.append((api, params))
        return responses.get(api, [])
    monkeypatch.setattr(data, 'tushare', fetch)
    return calls


def fxrow(day, code='EURUSD.FXCM'):
    return dict(ts_code=code, trade_date=day, bid_open=1.1, ask_open=1.2, bid_close=1.1, ask_close=1.2)


@pytest.mark.parametrize('code,base,quote', [('EURUSD.FXCM','EUR','USD'), ('USDJPY.FXCM','USD','JPY'),
                                           ('USDCNH.FXCM','USD','CNH'), ('AUDUSD.FXCM','AUD','USD')])
def test_fx_units_cutoff_cache_and_handle(research, monkeypatch, code, base, quote):
    calls = provider(monkeypatch, {'fx_daily': [fxrow('20261014', code), fxrow('20261015', code), fxrow('20261017', code)]})
    result = research.call('get_fx_rates', {'ts_code': code})['data']
    assert [r['data_date'] for r in result['rows']] == ['2026-10-14']
    row = result['rows'][0]
    assert row['base_currency'] == base and row['quote_currency'] == quote
    assert row['unit'] == f'{quote}_per_{base}'
    assert row['mid_close'] == pytest.approx(1.15)
    assert row['source_api'] == 'fx_daily'
    assert research.handles[result['handle']] == result['rows']
    assert research.call('run_backtest', {'handle': result['handle']})['data']['status'] == 'unavailable'
    research.call('get_fx_rates', {'ts_code': code})
    assert len(calls) == 1


def test_dollar_basket_code_migration_and_identity(research, monkeypatch):
    research.day = '2023-06-05'
    calls = []
    def fetch(api, params):
        calls.append(params)
        code = params['ts_code']
        return [fxrow('20230531' if code.endswith('.FXCM') else '20230601', code)]
    monkeypatch.setattr(data, 'tushare', fetch)
    result = research.call('get_fx_rates', {'ts_code': 'USDOLLAR', 'start_date': '2023-05-30'})['data']
    assert [r['provider_ts_code'] for r in result['rows']] == ['USDOLLAR.FXCM','USDOLLAR']
    assert all(r['ts_code'] == 'USDOLLAR' and r['unit'] == 'index_points' for r in result['rows'])
    assert calls[0]['end_date'] == '20230531' and calls[1]['start_date'] == '20230601'
    assert 'not ICE DXY' in result['note']


@pytest.mark.parametrize('api', ['fund_share','etf_share_size'])
def test_gold_etf_shares_and_size_are_distinct_from_cash_flows(research, monkeypatch, api):
    data_rows = [dict(ts_code='518880.SH', trade_date=day, fd_share=shares, total_share=shares, total_size=size)
                 for day, shares, size in [('20261012',100,500), ('20261014',105,540), ('20261015',999,900)]]
    calls = provider(monkeypatch, {api: data_rows})
    result = research.call('get_gold_etf_shares', {'source_api': api})['data']
    assert [r['data_date'] for r in result['rows']] == ['2026-10-12','2026-10-14']
    first, last = result['rows']
    assert first['shares'] == 1000000 and first['share_change_since_previous_observation'] is None
    assert last['shares'] == 1050000
    assert last['share_change_since_previous_observation'] == 50000
    assert last['previous_observation_date'] == '2026-10-12'
    assert last['availability_basis'] == 'assumed_next_beijing_day_12h'
    assert last['size_cny'] == (5400000 if api == 'etf_share_size' else None)
    assert last['size_change_cny_since_previous_observation'] == (400000 if api == 'etf_share_size' else None)
    assert 'not cash flows' in result['note']
    assert calls[0][0] == api


def test_etf_source_permission_failure_never_silently_switches(research, monkeypatch):
    calls = []
    def fail(api, params):
        calls.append(api)
        raise RuntimeError('permission_denied')
    monkeypatch.setattr(data, 'tushare', fail)
    result = research.call('get_gold_etf_shares', {'source_api':'etf_share_size'})['data']
    assert result['status'] == 'unavailable' and calls == ['etf_share_size']


def test_gold_etf_listing_and_nongold_validation(research, monkeypatch):
    calls = provider(monkeypatch, {})
    assert research.call('get_gold_etf_shares', {'ts_code':'510300.SH'})['data']['status'] == 'unavailable'
    research.day = '2013-07-01'
    assert research.call('get_gold_etf_shares', {})['data']['status'] == 'instrument_not_listed_at_cutoff'
    assert not calls


def test_vix_is_equity_volatility_with_numeric_string_normalization(research, monkeypatch):
    provider(monkeypatch, {'vix_index': [dict(trade_date='20261014', close='16.87', open='15.79', pct_change='10.48'),
                                       dict(trade_date='20261015', close='19')]})
    result = research.call('get_risk_index', {})['data']
    assert len(result['rows']) == 1
    assert result['rows'][0]['close'] == 16.87
    assert result['rows'][0]['gram_role'] == 'supplemental_equity_risk_proxy'
    assert 'not gold 3-month' in result['note']


def test_breakeven_matches_date_and_maturity_without_fill(research, monkeypatch):
    provider(monkeypatch, {
        'us_tycr': [dict(date='20261012', y10=5), dict(date='20261013', y5='4', y10=5.2, y30=None), dict(date='20261015', y10=99)],
        'us_trycr': [dict(date='20261013', y5='2', y10=2.8, y30=3), dict(date='20261014', y10=2)],
    })
    result = research.call('get_us_yields', {'kind':'breakeven'})['data']
    assert len(result['rows']) == 1
    row = result['rows'][0]
    assert row['data_date'] == '2026-10-13'
    assert row['y5'] == 2 and row['y10'] == pytest.approx(2.4) and row['y30'] is None
    assert row['unit'] == result['unit'] == 'percentage_points'
    assert row['inputs']['nominal']['y10'] == 5.2
    assert row['inputs']['real']['y10'] == 2.8


@pytest.mark.parametrize('tool,args', [('get_fx_rates', {'ts_code':'USDCNY.FXCM'}),
                                     ('get_gold_etf_shares', {'source_api':'unknown'}),
                                     ('get_fx_rates', {'limit':False}),
                                     ('get_risk_index', {'index':'gold_IV'})])
def test_bad_gram_arguments_do_not_fetch(research, monkeypatch, tool, args):
    calls = provider(monkeypatch, {})
    assert research.call(tool, args)['data']['status'] == 'unavailable'
    assert not calls


@pytest.mark.parametrize('tool,args,row', [
    ('get_fx_rates', {}, dict(ts_code='EURUSD.FXCM', bid_close=1.1, ask_close=1.2)),
    ('get_gold_etf_shares', {}, dict(ts_code='518880.SH', fd_share=100)),
    ('get_risk_index', {}, dict(index='VIX', close=15)),
])
def test_new_snapshots_are_preserved_and_never_fetch_in_fixture(research, monkeypatch, tool, args, row):
    research.exp['config']['driver'] = 'fixture'
    calls = provider(monkeypatch, {})
    assert research.call(tool, args)['data']['status'] == 'dataset_not_prepared'
    payload = {tool: [dict(row, available_at='2026-10-14T12:00:00+08:00'),
                      dict(row, available_at='2026-10-17T12:00:00+08:00')]}
    response = TestClient(app).post('/api/experiments', json={'research_data': payload})
    assert response.status_code == 200
    research.exp['dataset'].update(store.get(response.json()['id'])['dataset'])
    result = research.call(tool, args)['data']
    assert len(result['rows']) == 1 and result['source'] == 'import'
    assert not calls


def test_gap_list_exposes_original_missing_series_and_proxies():
    gaps = market_tools.GRAM_COVERAGE['gaps']
    assert len({r['id'] for r in gaps}) == 8
    assert {r['id'] for r in gaps} == set(market_tools.CATALOG['gram']['unmatched_original_inputs'])
    gold_iv = next(r for r in gaps if r['id'] == 'gold_3m_implied_volatility')
    assert gold_iv['status'] == 'not_found_in_review'
    assert gold_iv['proxy_tools'] == ['get_risk_index']
    names = {t['function']['name'] for t in agents.TOOLS}
    assert set(market_tools.CATALOG['gram']['dimensions']['opportunity_cost_fx']) <= names
    assert all(set(r['proxy_tools']) <= names for r in gaps)


@pytest.mark.parametrize('tool,args,row', [
    ('get_fx_rates', {}, dict(ts_code='EURUSD.FXCM', close=1.1)),
    ('get_gold_etf_shares', {}, dict(ts_code='518880.SH', shares=1000000)),
    ('get_risk_index', {}, dict(index='VIX', close=15)),
])
def test_model_can_choose_gram_tool_then_submit_evidence(research, monkeypatch, tool, args, row):
    research.exp['dataset'][tool] = [dict(row, available_at='2026-10-14T12:00:00+08:00')]
    calls = []
    def request(exp, agent, day, body, timeout=120):
        calls.append(body)
        if len(calls) == 1:
            assert 'tool_choice' not in body
            assert len(body['tools']) == 4
            state = json.loads(body['messages'][1]['content'])
            assert 'global_gold_etf_flows' in state['data_catalog']['tushare_research']['gram']['unmatched_original_inputs']
            assert any(tool in names for names in state['tool_directory']['categories'].values())
            name, arguments = 'select_tools', {'tools':[tool]}
        elif len(calls) == 2:
            assert tool in {t['function']['name'] for t in body['tools']}
            name, arguments = tool, args
        else:
            name = 'submit_decision'
            arguments = dict(action='hold', target_weight=None, expected_return=0, prob_up=.5,
                             rationale='基于本轮可见数据保持仓位', evidence_ids=list(research.evidence))
        return {'usage': {'prompt_tokens':100, 'completion_tokens':40}, 'choices':[{'message':{
            'role':'assistant', 'tool_calls':[{'id':str(len(calls)), 'type':'function', 'function':{
                'name':name, 'arguments':json.dumps(arguments)}}]}}]},0
    monkeypatch.setattr(agents, 'poe_request', request)
    decision, usage = agents.run_agent(research, 'GPT-5-mini', 1, 'extended', {'cash':'10000','qty':'0'})
    assert len(calls) == 3 and usage['calls'] == 1 and usage['routing_calls'] == 1
    assert decision['evidence_ids'] == list(research.evidence)


def test_forced_submission_drops_unused_schemas_to_preserve_budget(research, monkeypatch):
    research.exp['dataset']['get_risk_index'] = [dict(index='VIX', close=15, note='x'*2500, available_at='2026-10-14T12:00:00+08:00')]
    calls = []
    def request(exp, agent, day, body, timeout=120):
        calls.append(json.loads(json.dumps(body)))
        if len(calls) == 1:
            name, arguments = 'select_tools', {'tools':['get_risk_index']}
            tokens = 100
        elif len(calls) == 2:
            name, arguments = 'get_risk_index', {}
            tokens = 6000
        else:
            assert body['tool_choice']['function']['name'] == 'submit_decision'
            assert [t['function']['name'] for t in body['tools']] == ['submit_decision']
            name = 'submit_decision'
            arguments = dict(action='hold', target_weight=None, expected_return=0, prob_up=.5,
                             rationale='根据当轮风险数据保持数量', evidence_ids=list(research.evidence))
            tokens = 200
        return {'usage':{'prompt_tokens':tokens,'completion_tokens':40},'choices':[{'message':{
            'role':'assistant','tool_calls':[{'id':str(len(calls)),'type':'function','function':{
                'name':name,'arguments':json.dumps(arguments)}}]}}]},0
    monkeypatch.setattr(agents, 'poe_request', request)
    decision, usage = agents.run_agent(research, 'GPT-5-mini', 1, 'extended', {'cash':'10000','qty':'0'})
    assert decision is not None and len(calls) == 3 and usage['calls'] == 1


def test_shanghai_gold_etf_exposes_correct_benchmark(research, monkeypatch):
    provider(monkeypatch, {'fund_share':[dict(ts_code='518600.SH', trade_date='20261014', fd_share=100)]})
    result = research.call('get_gold_etf_shares', {'ts_code':'518600.SH'})['data']
    assert result['rows'][0]['benchmark'] == 'SHAU.SGE'
    assert result['rows'][0]['shares'] == 1000000


def test_mining_equity_etf_is_not_accepted_as_physical_gold_proxy(research, monkeypatch):
    calls = provider(monkeypatch, {})
    result = research.call('get_gold_etf_shares', {'ts_code':'159315.SZ'})['data']
    assert result['status'] == 'unavailable' and not calls
