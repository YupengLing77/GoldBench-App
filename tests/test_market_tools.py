"""Research tool integration: source routing, historical isolation and tool loop."""
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
    return agents.Research(store.get(ident), 'A', '2026-10-15', [])


def mock_provider(monkeypatch, responses):
    calls = []
    def fetch(api, params):
        calls.append((api, params))
        return responses.get(api, [])
    monkeypatch.setattr(data, 'tushare', fetch)
    return calls


def test_daily_tools_route_filter_cache_and_compute(research, monkeypatch):
    day = '20261014'
    calls = mock_provider(monkeypatch, {
        'fut_mapping': [{'ts_code': 'AU.SHF', 'trade_date': day, 'mapping_ts_code': 'AU2612.SHF'}],
        'fut_daily': [dict(ts_code='AU.SHF', trade_date=d, close=900, settle=899, oi=100) for d in (day, '20261015', '20261016')],
        'fut_basic': [dict(ts_code='AU2612.SHF', name='沪金2612', list_date='20251118', delist_date='20261215'),
                      dict(ts_code='AU2812.SHF', list_date='20271118')],
        'sge_daily': [dict(ts_code='Au99.99', trade_date=day, close=99, change=-1, pct_change=-100)],
        'sge_basic': [dict(ts_code='Au99.99', list_date='20021030', t_unit=1000)],
        'fut_holding': [dict(symbol='AU', trade_date=day, broker='example', long_hld=20, short_hld=5)],
        'fut_wsr': [dict(symbol='AU', trade_date=day, vol=100, unit='千克')],
        'us_tycr': [dict(date=d, y10=5) for d in ('20261013', day)],
        'us_trycr': [dict(date=d, y10=2) for d in ('20261013', day)],
    })
    args = {'start_date': '2026-10-12', 'end_date': '2026-10-20'}
    # Day cutoff is 06:45; Chinese prior-day data unlocks at 09:00.
    assert research.call('get_gold_futures', args)['data']['rows'] == []
    research.day = '2026-10-16'
    futures = research.call('get_gold_futures', args)['data']
    assert [r['data_date'] for r in futures['rows']] == ['2026-10-14']
    assert futures['rows'][0]['underlying_contract'] == 'AU2612.SHF'
    assert futures['contracts'][0]['ts_code'] == 'AU2612.SHF'
    assert len(futures['contracts']) == 1
    assert research.handles[futures['handle']] == futures['rows']
    before = len(calls)
    research.call('get_gold_futures', args)
    assert len(calls) == before
    spot = research.call('get_gold_spot', args)['data']['rows'][0]
    assert spot['raw_pct_change'] == -100
    assert spot['computed_return'] == pytest.approx(-.01)
    assert spot['instrument_type'] == 'spot'
    assert research.call('get_positioning', args)['data']['rows'][0]['long_hld'] == 20
    assert research.call('get_gold_warehouse', args)['data']['rows'][0]['unit'] == '千克'
    yields = research.call('get_us_yields', args)['data']['rows']
    assert {r['kind'] for r in yields} == {'real', 'nominal'}
    assert all(r['unit'] == 'percent_per_annum' for r in yields)
    assert all('source_api' in r and 'available_at' in r for r in yields)
    assert all('end_date' not in p or p['end_date'] <= '20261016' for _, p in calls)
    assert research.trace and all(r['evidence_id'] in research.evidence for r in (research.call('get_us_yields', args),))


def test_us_session_date_is_not_beijing_publication_date(research, monkeypatch):
    mock_provider(monkeypatch, {'us_tycr': [{'date': '20261014', 'y10': 5}]})
    assert research.call('get_us_yields', {'kind': 'nominal'})['data']['rows'] == []
    research.day = '2026-10-16'
    assert research.call('get_us_yields', {'kind': 'nominal'})['data']['rows'][0]['y10'] == 5


@pytest.mark.parametrize('api', market_tools.MACRO_APIS)
def test_macro_endpoints_do_not_backdate_revised_values(research, monkeypatch, api):
    calls = mock_provider(monkeypatch, {api: [{'month': '202609', 'quarter': '2026Q3', 'value': 123}]})
    result = research.call('get_macro', {'dataset': api})['data']
    assert calls[0][0] == api
    if api not in ('shibor', 'shibor_lpr'):
        assert result['status'] == 'historical_vintage_unavailable'
        assert result['rows'] == []
        assert calls[0][1].get('start_m') or calls[0][1].get('start_q')


def test_preobserved_macro_vintage_and_field_selection(research, monkeypatch):
    monkeypatch.setattr(store, 'now', lambda: '2026-10-14T00:00:00+00:00')
    mock_provider(monkeypatch, {'cn_pmi': [dict(month='202609', pmi010000=50, pmi010100=49), dict(month='202610', pmi010000=51)]})
    result = research.call('get_macro', {'dataset': 'cn_pmi'})['data']
    assert len(result['rows']) == 1
    assert result['rows'][0]['pmi010000'] == 50
    assert 'pmi010100' not in result['rows'][0]
    assert result['rows'][0]['available_at'] == '2026-10-14T00:00:00+00:00'


@pytest.mark.parametrize('which,api,row', [
    ('global', 'eco_cal', dict(date='20261014', time='20:30', country='美国', event='CPI', value='3%', fore_value='2%')),
    ('china', 'cn_schedule', dict(month='202610', publish_date='20261014', title='CPI', data_api='cn_cpi')),
])
def test_calendars_require_observed_vintage(research, monkeypatch, which, api, row):
    calls = mock_provider(monkeypatch, {api: [row]})
    result = research.call('get_economic_calendar', {'calendar': which})['data']
    assert result['status'] == 'historical_vintage_unavailable'
    assert result['rows'] == []
    assert calls[0][0] == api


def test_calendar_preobserved_version_and_imported_release_boundary(research, monkeypatch):
    monkeypatch.setattr(store, 'now', lambda: '2026-10-14T00:00:00+00:00')
    mock_provider(monkeypatch, {'eco_cal': [dict(date='20261014', time='20:30', country='美国', event='CPI', value='3%')]})
    result = research.call('get_economic_calendar', {'query': 'CPI'})['data']
    assert result['rows'][0]['event_timezone'] == 'unverified'
    assert result['rows'][0]['available_at'] == '2026-10-14T00:00:00+00:00'
    research.day = '2026-10-14'
    research.exp['dataset']['get_economic_calendar'] = [
        dict(calendar='global', date='20261014', event='CPI', available_at='2026-10-14T20:30:00+08:00', value='3%'),
        dict(calendar='global', date='20261014', event='CPI schedule', available_at='2026-10-13T12:00:00+08:00', value=None),
    ]
    result = research.call('get_economic_calendar', {})['data']
    assert len(result['rows']) == 1
    assert result['rows'][0]['value'] is None


def test_all_tools_work_with_imported_fixture_without_network(research, monkeypatch):
    research.exp['config']['driver'] = 'fixture'
    def no_network(*a, **k):
        raise AssertionError('fixture must not fetch')
    monkeypatch.setattr(data, 'tushare', no_network)
    for tool in market_tools.TOOL_NAMES:
        args = {'dataset': 'cn_cpi'} if tool == 'get_macro' else {}
        assert research.call(tool, args)['data']['status'] == 'dataset_not_prepared'
    row = dict(ts_code='AU2612.SHF', trade_date='20261013', close=900, available_at='2026-10-14T09:00:00+08:00')
    research.exp['dataset']['get_gold_futures'] = [row, dict(row, available_at='2026-10-16T09:00:00+08:00', close=999)]
    result = research.call('get_gold_futures', {'ts_code': 'AU2612.SHF'})['data']
    assert len(result['rows']) == 1 and result['rows'][0]['close'] == 900
    research.exp['dataset']['get_macro'] = [dict(dataset='cn_cpi', available_at='2026-10-14T09:00:00+08:00', month='202609', nt_yoy=1)]
    assert research.call('get_macro', {'dataset': 'cn_cpi'})['data']['rows'][0]['nt_yoy'] == 1


def test_market_handles_can_be_used_by_python_but_not_fx_backtest(research):
    research.exp['dataset']['get_gold_spot'] = [dict(ts_code='Au99.99', close=900, available_at='2026-10-13T09:00:00+08:00')]
    result = research.call('get_gold_spot', {})['data']
    assert research.handles[result['handle']][0]['close'] == 900
    backtest = research.call('run_backtest', {'handle': result['handle']})['data']
    assert backtest['status'] == 'unavailable'
    assert 'get_prices_XAUUSD_handle' in backtest['reason']


@pytest.mark.parametrize('tool,args', [
    ('get_gold_futures', {'ts_code': 'GC.COMEX'}),
    ('get_gold_spot', {'ts_code': 'not-gold'}),
    ('get_positioning', {'symbol': 'CU'}),
    ('get_us_yields', {'kind': 'not-a-kind'}),
    ('get_economic_calendar', {'calendar': 'unknown'}),
    ('get_macro', {}),
    ('get_gold_warehouse', {'start_date': '2000-01-01'}),
    ('get_gold_warehouse', {'limit': 1000}),
])
def test_invalid_arguments_never_fetch(research, monkeypatch, tool, args):
    calls = mock_provider(monkeypatch, {})
    result = research.call(tool, args)['data']
    assert result['status'] == 'unavailable'
    assert not calls


def test_truncated_data_splits_and_single_day_fails_closed(research, monkeypatch):
    calls = []
    def fetch(api, params):
        calls.append(params)
        if params['start_date'] != params['end_date']:
            return [{}]*100
        return [dict(date=params['start_date'])]
    monkeypatch.setattr(data, 'tushare', fetch)
    start, end = map(market_tools.parse_date, ('20261012', '20261013'))
    result = market_tools.fetch_range(research.exp, 'eco_cal', {}, start, end, cap=100)
    assert len(result) == 2 and len(calls) == 3
    monkeypatch.setattr(data, 'tushare', lambda *a, **k: [{}]*100)
    with pytest.raises(RuntimeError, match='truncated'):
        market_tools.fetch_range(research.exp, 'eco_cal', {'country': 'new'}, start, start, cap=100)


def test_snapshot_api_preserves_new_tools_and_validates_timezone(research):
    client = TestClient(app)
    record = dict(ts_code='Au99.99', close=900, available_at='2026-10-01T09:00:00+08:00')
    response = client.post('/api/experiments', json={'research_data': {'get_gold_spot': [record]}})
    assert response.status_code == 200
    assert store.get(response.json()['id'])['dataset']['get_gold_spot'] == [record]
    for bad in ({'get_gold_spot': [dict(record, available_at='2026-10-01T09:00:00')]}, {'unknown_tool': []}, {'get_macro': 'invalid'}):
        assert client.post('/api/experiments', json={'research_data': bad}).status_code == 422


def test_model_can_select_new_tool_and_submit_citable_evidence(research, monkeypatch):
    research.exp['dataset']['get_gold_warehouse'] = [dict(symbol='AU', vol=100, unit='kg', available_at='2026-10-13T09:00:00+08:00')]
    seen = []
    def request(exp, agent, day, body, timeout=120):
        seen.append(json.loads(json.dumps(body)))
        if len(seen) == 1:
            assert 'tool_choice' not in body, 'Tool descriptions must fit before forced submission threshold'
            tool_names = {x['function']['name'] for x in body['tools']}
            assert tool_names == {'submit_decision','select_tools','get_prices','get_history'}
            name, args = 'select_tools', {'tools':['get_gold_warehouse']}
        elif len(seen) == 2:
            assert 'get_gold_warehouse' in {x['function']['name'] for x in body['tools']}
            name, args = 'get_gold_warehouse', {}
        else:
            assert body['messages'][-2]['role'] == 'tool'
            evidence_id = next(iter(research.evidence))
            name, args = 'submit_decision', dict(action='hold', target_weight=None, expected_return=0, prob_up=.5,
                                                rationale='仓单数据支持暂时观察', evidence_ids=[evidence_id])
        return {'usage': {'prompt_tokens': 100, 'completion_tokens': 40}, 'choices': [{'message': {'role': 'assistant', 'tool_calls': [
            {'id': str(len(seen)), 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}}]}, 0
    monkeypatch.setattr(agents, 'poe_request', request)
    decision, usage = agents.run_agent(research, 'GPT-5-mini', 1, 'extended', {'cash': '10000', 'qty': '0'})
    assert decision['evidence_ids'] == list(research.evidence)
    assert usage['calls'] == 1 and usage['routing_calls'] == 1 and len(seen) == 3


def test_large_member_rankings_paginate_without_losing_rows(research, monkeypatch):
    records = [dict(symbol='AU', trade_date='20261013', broker=f'broker-{i:03}', long_hld=i, note='x'*200) for i in range(40)]
    calls = mock_provider(monkeypatch, {'fut_holding': records})
    seen, offset = set(), 0
    while True:
        result = research.call('get_positioning', {'limit': 100, 'offset': offset})['data']
        assert result['status'] == 'ok'
        assert len(store.dumps(result).encode()) < 6000
        ids = {r['broker'] for r in result['rows']}
        assert not seen & ids
        seen.update(ids)
        if result['next_offset'] is None:
            break
        assert result['next_offset'] > offset
        offset = result['next_offset']
    assert len(seen) == 40
    assert len(calls) == 1


def test_basic_info_permission_does_not_hide_daily_data(research, monkeypatch):
    def fetch(api, params):
        if api == 'sge_basic':
            raise RuntimeError('tushare_api_error:permission')
        assert api == 'sge_daily'
        return [dict(ts_code='Au99.99', trade_date='20261013', close=900, change=1)]
    monkeypatch.setattr(data, 'tushare', fetch)
    result = research.call('get_gold_spot', {})['data']
    assert result['rows'][0]['close'] == 900
    assert result['contract_metadata_status'] == 'unavailable_check_basic_info_permission'


def test_provider_failure_is_explicit_and_never_fixture_fallback(research, monkeypatch):
    def fail(*a, **k):
        raise RuntimeError('TUSHARE_TOKEN_not_configured')
    monkeypatch.setattr(data, 'tushare', fail)
    result = research.call('get_gold_warehouse', {})['data']
    assert result['status'] == 'unavailable'
    assert result['reason'] == 'TUSHARE_TOKEN_not_configured'
    assert 'rows' not in result


def test_fetch_budget_stops_after_deadline(research, monkeypatch):
    monkeypatch.setattr(data, 'tushare', lambda *a, **k: [{}]*100)
    token = market_tools.FETCH_BUDGET.set({'calls': 16, 'deadline': 0})
    try:
        with pytest.raises(RuntimeError, match='budget_exhausted'):
            market_tools.cached(research.exp, 'eco_cal', {}, 100)
    finally:
        market_tools.FETCH_BUDGET.reset(token)


def test_fetch_budget_allows_more_than_sixteen_requests(research, monkeypatch):
    monkeypatch.setattr(data, 'tushare', lambda *a, **k: [])
    budget = {'calls': 0, 'deadline': float('inf')}
    token = market_tools.FETCH_BUDGET.set(budget)
    try:
        for i in range(17):
            market_tools.cached(research.exp, 'eco_cal', {'page': i}, 100)
        assert budget['calls'] == 17
    finally:
        market_tools.FETCH_BUDGET.reset(token)
