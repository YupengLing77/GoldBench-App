from . import policies
"""Bounded Tushare research tools, separate from the account execution feed.

Daily bars use an explicit conservative publication assumption. Revisable macro
and calendar responses require an observed vintage or an imported as-of snapshot.
"""
import hashlib
import json
import re
import threading
import time
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import httpx

from . import data, store
from .engine import cutoff

CN = timezone(timedelta(hours=8))
CACHE_LOCK = threading.RLock()
FETCH_BUDGET = ContextVar('research_fetch_budget', default=None)
MACRO_APIS = ('cn_cpi', 'cn_ppi', 'cn_pmi', 'cn_gdp', 'cn_m', 'sf_month', 'shibor', 'shibor_lpr')
NEW_TOOLS = ('get_gold_futures', 'get_gold_spot', 'get_gold_warehouse', 'get_us_yields', 'get_economic_calendar',
             'get_fx_rates', 'get_gold_etf_shares', 'get_risk_index')
TOOL_NAMES = NEW_TOOLS + ('get_macro', 'get_positioning')
SNAPSHOT_TOOLS = TOOL_NAMES + ('read_webpage',)
FX_INSTRUMENTS = {
    'EURUSD.FXCM': ('EUR', 'USD'), 'USDJPY.FXCM': ('USD', 'JPY'),
    'USDCNH.FXCM': ('USD', 'CNH'), 'AUDUSD.FXCM': ('AUD', 'USD'),
    'GBPUSD.FXCM': ('GBP', 'USD'), 'USDCAD.FXCM': ('USD', 'CAD'),
    'USDCHF.FXCM': ('USD', 'CHF'), 'USDOLLAR': (None, None),
}
# Verified from etf_basic (2026-11-10), restricted to Au99.99/SHAU-tracking ETFs;
# gold-mining equity ETFs are deliberately excluded. Listing dates prevent
# offering a future fund to a historical agent.
GOLD_ETFS = {
    '159812.SZ': ('前海开源黄金ETF', '20200529'), '159934.SZ': ('易方达黄金ETF', '20131216'),
    '159937.SZ': ('博时黄金ETF', '20140901'), '518660.SH': ('工银瑞信黄金ETF', '20200529'),
    '518800.SH': ('国泰黄金ETF', '20130729'), '518850.SH': ('华夏黄金ETF', '20200605'),
    '518880.SH': ('华安易富黄金ETF', '20130729'),
    '159830.SZ': ('天弘上海金ETF', '20210719'), '159831.SZ': ('嘉实上海金ETF', '20220407'),
    '159834.SZ': ('南方上海金ETF', '20220316'), '518600.SH': ('广发上海金ETF', '20200805'),
    '518680.SH': ('富国上海金ETF', '20200728'), '518860.SH': ('建信上海金ETF', '20200907'),
    '518890.SH': ('中银上海金ETF', '20200928'),
}
SHAU_ETFS = {'159830.SZ', '159831.SZ', '159834.SZ', '518600.SH', '518680.SH', '518860.SH', '518890.SH'}
GRAM_COVERAGE = json.loads(Path(__file__).with_name('gram_coverage.json').read_text(encoding='utf-8'))

WINDOW = {
    'start_date': {'type': 'string'},
    'end_date': {'type': 'string'},
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
    'offset': {'type': 'integer', 'minimum': 0, 'maximum': 10000},
}
SPECS = [
    ('get_gold_futures', 'Read SHFE gold futures daily OHLC, settlement, volume, open interest and historical main-contract mapping. CNY/gram; research only, no COMEX. Monthly contracts or AU.SHF main series.',
     WINDOW | {'ts_code': {'type': 'string', 'description': 'AU.SHF (default), or e.g. AU2612.SHF.'}}),
    ('get_gold_spot', 'Read SGE gold daily OHLC, volume and delivery data. CNY/gram; Au(T+D) is deferred delivery, not cash spot. Raw provider percentage is retained separately from computed return.',
     WINDOW | {'ts_code': {'type': 'string', 'enum': ['Au99.99', 'Au99.95', 'Au100g', 'iAu99.99', 'Au(T+D)', 'mAu(T+D)']}}),
    ('get_gold_warehouse', 'Read SHFE AU registered warehouse receipts and changes, including units. These are receipts, not total gold inventories.', WINDOW),
    ('get_us_yields', 'Read US nominal/real Treasury yield curves, percent per annum. kind=breakeven joins matching dates/maturities and subtracts real from nominal, in percentage points; includes risk/liquidity premia, not a CPI forecast.',
     WINDOW | {'kind': {'type': 'string', 'enum': ['nominal', 'real', 'both', 'breakeven']}}),
    ('get_economic_calendar', 'Read global economic events (eco_cal) or Chinese release schedule (cn_schedule). Historical values, forecasts and schedules require a previously observed or imported vintage. Unverified time zones are not guessed.',
     WINDOW | {'calendar': {'type': 'string', 'enum': ['global', 'china']}, 'country': {'type': 'string', 'maxLength': 40}, 'query': {'type': 'string', 'maxLength': 100}}),
    ('get_macro', 'Read China macro series from Tushare or imported as-of snapshots. API datasets: cn_cpi, cn_ppi, cn_pmi, cn_gdp, cn_m, sf_month, shibor, shibor_lpr. Monthly/quarterly historical values require a verified vintage; unknown legacy datasets remain snapshot-only.',
     WINDOW | {'start_date': {'type': 'string', 'description': 'YYYY-MM-DD reference-period start, default last 366 days.'}, 'dataset': {'type': 'string', 'description': 'Tushare API name or imported dataset name.'}}),
    ('get_positioning', 'Read SHFE gold member/broker position rankings via fut_holding, or an imported legacy dataset. Reported member ranks are not CFTC COT or complete investor exposure; absent ranks are not zero.',
     WINDOW | {'symbol': {'type': 'string', 'description': 'AU (default), or AU2612; no .SHF suffix.'}, 'dataset': {'type': 'string', 'description': 'Optional imported legacy dataset name.'}}),
    ('get_fx_rates', 'Read FXCM daily bid/ask FX rates or USDOLLAR basket. Explicit base/quote currency; CNH is offshore yuan, USDOLLAR is FXCM basket (not ICE DXY or GRAM FX indices). Returns a Python data handle.',
     WINDOW | {'ts_code': {'type': 'string', 'enum': list(FX_INSTRUMENTS)}}),
    ('get_gold_etf_shares', 'Read selected China Au99.99/SHAU gold ETF shares via fund_share (default) or etf_share_size. Normalized shares/CNY size and changes since previous observation; not global gold ETF flows. No silent source fallback.',
     WINDOW | {'ts_code': {'type': 'string', 'enum': list(GOLD_ETFS)}, 'source_api': {'type': 'string', 'enum': ['fund_share', 'etf_share_size']}}),
    ('get_risk_index', 'Read VIX via vix_index, SP500 option-implied 30-day equity volatility, percent annualized. Supplemental risk proxy; not gold 3-month implied volatility or geopolitical risk index.', WINDOW),
]
CATALOG = {
    'window_parameters': 'YYYY-MM-DD start_date/end_date; default last 30 days (macro 366), maximum span 366 days. End clamped to research day. limit default 20, max 100; offset skips newest rows; follow next_offset.',
    'gold_futures': {'tool': 'get_gold_futures', 'market': 'SHFE', 'default': 'AU.SHF', 'unit': 'CNY/gram'},
    'gold_spot': {'tool': 'get_gold_spot', 'market': 'SGE', 'default': 'Au99.99', 'unit': 'CNY/gram'},
    'gold_positions': {'tool': 'get_positioning', 'dataset': 'fut_holding', 'symbol': 'AU'},
    'gold_receipts': {'tool': 'get_gold_warehouse', 'symbol': 'AU'},
    'us_yields': {'tool': 'get_us_yields', 'kinds': ['nominal', 'real', 'both', 'breakeven']},
    'china_macro': {'tool': 'get_macro', 'datasets': list(MACRO_APIS)},
    'calendar': {'tool': 'get_economic_calendar', 'calendars': ['global', 'china']},
    'fx_rates': {'tool': 'get_fx_rates', 'codes': list(FX_INSTRUMENTS)},
    'gold_etf_shares': {'tool': 'get_gold_etf_shares', 'default': '518880.SH', 'scope': 'China Au99.99/SHAU gold ETFs'},
    'risk_index': {'tool': 'get_risk_index', 'index': 'VIX', 'role': 'supplemental_equity_risk_proxy'},
    'gram': {'dimensions': GRAM_COVERAGE['dimensions'],
             'unmatched_original_inputs': [r['id'] for r in GRAM_COVERAGE['gaps']],
             'note': 'Tools support GRAM themes; proxy series do not reproduce original GRAM regressors.'},
    'history_policy': 'Daily publication times are conservative assumptions. Revised macro/calendar data are visible only from observed_at or explicit imported available_at; missing historical vintages are reported, never backdated.',
}


def parse_date(value):
    text = str(value)
    if re.fullmatch(r'\d{8}', text):
        text = f'{text[:4]}-{text[4:6]}-{text[6:]}'
    return date.fromisoformat(text)


def aware(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError('available_at_requires_timezone')
    return stamp


def window(args, day):
    end = min(parse_date(args.get('end_date', day)), parse_date(day))
    start = parse_date(args['start_date']) if args.get('start_date') else end - timedelta(days=30)
    limit = args.get('limit', 20)
    if type(limit) is not int or (limit<1 or (not policies.GLOBAL_ONLY.get() and limit>100)):
        raise ValueError('limit_must_be_1_to_100')
    if start > end or (not policies.GLOBAL_ONLY.get() and (end-start).days > 366):
        raise ValueError('research_window_must_be_0_to_366_days')
    return start, end, limit


def cached(exp, api, params, cap):
    key = hashlib.sha256(store.dumps([exp['id'], api, params]).encode()).hexdigest()
    with CACHE_LOCK:
        with store.db() as con:
            row = con.execute('SELECT payload,received FROM research_cache WHERE cache_key=?', (key,)).fetchone()
        if row:
            return json.loads(row['payload']), row['received']
        budget = FETCH_BUDGET.get()
        if budget is not None:
            if time.monotonic() >= budget['deadline']:
                raise RuntimeError('research_fetch_budget_exhausted:reduce_date_window')
            budget['calls'] += 1
        rows = data.tushare(api, params)
        if len(rows) >= cap:
            raise RuntimeError('research_response_truncated:' + api)
        received = store.now()
        with store.db() as con:
            con.execute('INSERT OR IGNORE INTO research_cache VALUES(?,?,?,?)', (key, api, store.dumps(rows), received))
        with store.db() as con:
            row = con.execute('SELECT payload,received FROM research_cache WHERE cache_key=?', (key,)).fetchone()
        return json.loads(row['payload']), row['received']


def fetch_range(exp, api, params, start, end, cap=2000):
    """Split capped responses by date; never silently return a truncated dataset."""
    query = params | {'start_date': start.strftime('%Y%m%d'), 'end_date': end.strftime('%Y%m%d')}
    try:
        rows, received = cached(exp, api, query, cap)
        return [(row, received) for row in rows]
    except RuntimeError as exc:
        if not str(exc).startswith('research_response_truncated:') or start == end:
            raise
        mid = start + (end-start)//2
        return fetch_range(exp, api, params, start, mid, cap) + fetch_range(exp, api, params, mid+timedelta(days=1), end, cap)


def contract_definitions(exp, api, params, cap):
    # Basic-info and daily-data permissions differ (notably SGE). Missing optional
    # definitions must not discard valid prices, but must remain visible to callers.
    try:
        rows, _ = cached(exp, api, params, cap)
        return rows, 'ok'
    except (RuntimeError, httpx.HTTPError):
        return [], 'unavailable_check_basic_info_permission'


def daily_record(row, api, received, us=False, hour=None):
    day = parse_date(row.get('trade_date') or row.get('date'))
    # US date denotes a US session: wait through the next Beijing day.
    hour = hour if hour is not None else 18 if us else 9
    available = datetime.combine(day+timedelta(days=1), datetime.min.time(), CN).replace(hour=hour)
    return dict(row, source='tushare', source_api=api, data_date=day.isoformat(),
                available_at=available.isoformat(), observed_at=received,
                availability_basis=f'assumed_next_beijing_day_{hour:02}h')


def envelope(rows, limit, offset=0, **meta):
    rows = sorted(rows, key=lambda r: (r.get('data_date', ''), r['available_at'], r.get('record_id', ''), str(r.get('broker', ''))))
    stop = max(0, len(rows)-offset)
    selected = rows[max(0, stop-limit):stop]
    result = dict(status='ok' if selected else 'no_visible_data', rows=selected, total_visible=len(rows), offset=offset, **meta)
    # Reserve context for a subsequent model decision. Pagination counts actual
    # returned records, including when byte limits reduce the requested limit.
    while selected and not policies.GLOBAL_ONLY.get() and len(store.dumps(result).encode()) > 5500:
        selected.pop(0)
    count = len(selected)
    result.update(returned=count, omitted=max(0, len(rows)-count),
                  next_offset=offset+count if count and offset+count < len(rows) else None)
    if not count and stop:
        result['status'] = 'record_too_large'
    return result


def snapshot(research, name, args, start, end, limit):
    records = research.exp['dataset'].get(name, [])
    if not records:
        return None
    selected = []
    for row in records:
        stamp = aware(row['available_at'])
        if stamp > cutoff(research.day):
            continue
        if name == 'get_macro' and row.get('dataset') != args.get('dataset'):
            continue
        if name == 'get_positioning' and args.get('dataset') and row.get('dataset') != args['dataset']:
            continue
        if name in ('get_gold_futures', 'get_gold_spot', 'get_fx_rates', 'get_gold_etf_shares') and row.get('ts_code') != args['ts_code']:
            continue
        if name == 'get_gold_etf_shares' and row.get('source_api') not in (None, args['source_api']):
            continue
        if name == 'get_positioning' and not args.get('dataset') and row.get('symbol') != args['symbol']:
            continue
        if name == 'get_us_yields' and args.get('kind', 'both') != 'both' and row.get('kind') != args['kind']:
            continue
        if name == 'get_economic_calendar':
            if row.get('calendar', 'global') != args.get('calendar', 'global'):
                continue
            if args.get('country') and row.get('country') != args['country']:
                continue
            if args.get('query', '').lower() not in str(row.get('event', row.get('title', ''))).lower():
                continue
        raw_day = row.get('data_date') or row.get('trade_date') or row.get('date')
        if raw_day and not start <= parse_date(raw_day) <= end:
            continue
        selected.append(dict(row, source=row.get('source', 'import'), availability_basis='imported_available_at'))
    return envelope(selected, limit, offset=args.get('offset', 0), source='import', vintage_policy='importer_verified_as_of')


def call(research, name, args):
    args = dict(args)
    for field, maximum in (('query', 100), ('country', 40)):
        if field in args and (not isinstance(args[field], str) or (not policies.GLOBAL_ONLY.get() and len(args[field]) > maximum)):
            raise ValueError(field + '_length_or_type')
    offset = args.get('offset', 0)
    if type(offset) is not int or (offset<0 or (not policies.GLOBAL_ONLY.get() and offset>10000)):
        raise ValueError('offset_must_be_0_to_10000')
    if set(args)-set(next(properties for tool, _, properties in SPECS if tool == name)):
        raise ValueError('unknown_tool_arguments')
    if name == 'get_gold_futures':
        code = args.setdefault('ts_code', 'AU.SHF')
        if not isinstance(code, str) or not re.fullmatch(r'AU(?:\d{4})?\.SHF', code):
            raise ValueError('only_SHFE_gold_contracts_supported')
    if name == 'get_gold_spot':
        if args.setdefault('ts_code', 'Au99.99') not in SPECS[1][2]['ts_code']['enum']:
            raise ValueError('unsupported_SGE_gold_contract')
    if name == 'get_positioning':
        if not re.fullmatch(r'AU(?:\d{4})?', args.setdefault('symbol', 'AU')):
            raise ValueError('only_AU_member_positions_supported')
    if name == 'get_us_yields' and args.setdefault('kind', 'both') not in ('nominal', 'real', 'both', 'breakeven'):
        raise ValueError('yield_kind')
    if name == 'get_fx_rates' and args.setdefault('ts_code', 'EURUSD.FXCM') not in FX_INSTRUMENTS:
        raise ValueError('unsupported_FX_instrument')
    if name == 'get_gold_etf_shares':
        if args.setdefault('ts_code', '518880.SH') not in GOLD_ETFS:
            raise ValueError('unsupported_gold_ETF')
        if args.setdefault('source_api', 'fund_share') not in ('fund_share', 'etf_share_size'):
            raise ValueError('ETF_source_api')
    if name == 'get_economic_calendar' and args.setdefault('calendar', 'global') not in ('global', 'china'):
        raise ValueError('calendar')
    if name == 'get_macro' and not isinstance(args.get('dataset'), str):
        raise ValueError('dataset_required')
    if name == 'get_macro' and 'start_date' not in args:
        end_day = min(parse_date(args.get('end_date', research.day)), parse_date(research.day))
        args['start_date'] = (end_day-timedelta(days=366)).isoformat()
    start, end, limit = window(args, research.day)
    if name == 'get_gold_etf_shares' and parse_date(GOLD_ETFS[args['ts_code']][1]) > cutoff(research.day).date():
        return {'status': 'instrument_not_listed_at_cutoff', 'rows': []}
    result = snapshot(research, name, args, start, end, limit)
    if result is not None:
        return attach_handle(research, name, result)
    if research.exp['config']['driver'] == 'fixture':
        return {'status': 'dataset_not_prepared', 'rows': [], 'reason': 'Fixture tools never fetch external data; import as-of research_data to exercise this tool.'}
    if name == 'get_macro' and args['dataset'] not in MACRO_APIS:
        return {'status': 'dataset_not_prepared', 'rows': []}
    if name == 'get_positioning' and args.get('dataset') not in (None, 'fut_holding'):
        return {'status': 'dataset_not_prepared', 'rows': []}
    token = FETCH_BUDGET.set({'calls': 0, 'deadline': float('inf') if policies.GLOBAL_ONLY.get() else time.monotonic()+120})
    try:
        result = provider(research, name, args, start, end, limit)
    finally:
        FETCH_BUDGET.reset(token)
    return attach_handle(research, name, result)


def attach_handle(research, name, result):
    if name in ('get_gold_futures', 'get_gold_spot', 'get_fx_rates') and result['rows']:
        handle = hashlib.sha256(store.dumps(result['rows']).encode()).hexdigest()[:16]
        research.handles[handle] = result['rows']
        research.handle_types[handle] = name
        result['handle'] = handle
    return result


def provider(research, name, args, start, end, limit):
    exp = research.exp
    cut = cutoff(research.day)
    rows = []
    meta = {'source': 'tushare', 'cutoff': cut.isoformat()}
    if name == 'get_gold_futures':
        code = args['ts_code']
        mapping = {}
        if code == 'AU.SHF':
            mapping = {r['trade_date']: r['mapping_ts_code'] for r, _ in fetch_range(exp, 'fut_mapping', {'ts_code': code}, start, end)}
        for row, received in fetch_range(exp, 'fut_daily', {'ts_code': code}, start, end):
            if row.get('ts_code') != code:
                continue
            item = daily_record(row, 'fut_daily', received)
            if item['available_at'] and aware(item['available_at']) <= cut and start <= parse_date(item['data_date']) <= end:
                item.update(unit='CNY/gram', volume_unit='contracts', amount_unit='CNY_10000', open_interest_unit='contracts',
                            underlying_contract=mapping.get(row['trade_date']) if code == 'AU.SHF' else code)
                rows.append(item)
        contract_codes = {r['underlying_contract'] for r in rows if r.get('underlying_contract')}
        definitions, definition_status = contract_definitions(exp, 'fut_basic', {'exchange': 'SHFE', 'fut_code': 'AU', 'fut_type': '1'}, 10000)
        # Only include the requested/returned gold contracts and stable contract
        # definitions, never future listings or current margin/ranking fields.
        contracts = [{k: r.get(k) for k in ('ts_code', 'name', 'list_date', 'delist_date', 'trade_unit', 'per_unit', 'quote_unit')}
                     for r in definitions if r.get('ts_code') in contract_codes
                     and r.get('list_date') and parse_date(r['list_date']) <= cut.date()]
        meta.update(market='SHFE', ts_code=code, unit='CNY/gram', series='unadjusted_main' if code == 'AU.SHF' else 'monthly_contract',
                    contracts=contracts, contract_metadata_status=definition_status,
                    contract_metadata_basis='current_provider_static_definitions_not_historical_spec_changes',
                    note='Main-contract roll gaps are not investment returns. No COMEX data. Same-day mapping is historical context, not a contract selection known at that day opening.')
    elif name == 'get_gold_spot':
        code = args['ts_code']
        for row, received in fetch_range(exp, 'sge_daily', {'ts_code': code}, start, end):
            if row.get('ts_code') != code:
                continue
            item = daily_record(row, 'sge_daily', received)
            if aware(item['available_at']) <= cut and start <= parse_date(item['data_date']) <= end:
                change, close = row.get('change'), row.get('close')
                previous = close-change if isinstance(change, (int, float)) and isinstance(close, (int, float)) else None
                item['raw_pct_change'] = item.pop('pct_change', None)
                item['computed_return'] = change/previous if previous and previous > 0 else None
                item.update(unit='CNY/gram', volume_unit='kg', amount_unit='CNY',
                            instrument_type='deferred_delivery' if '(T+D)' in code else 'spot')
                rows.append(item)
        definitions, definition_status = contract_definitions(exp, 'sge_basic', {'ts_code': code}, 100)
        contracts = [{k: r.get(k) for k in ('ts_code', 'ts_name', 'trade_type', 't_unit', 'p_unit', 'list_date')}
                     for r in definitions if r.get('ts_code') == code
                     and r.get('list_date') and parse_date(r['list_date']) <= cut.date()]
        meta.update(market='SGE', ts_code=code, unit='CNY/gram', contracts=contracts, contract_metadata_status=definition_status,
                    contract_metadata_basis='current_provider_static_definitions_not_historical_spec_changes',
                    note='Raw provider pct_change is not assumed to be percent; computed_return is decimal change/(close-change).')
    elif name in ('get_positioning', 'get_gold_warehouse'):
        api = 'fut_holding' if name == 'get_positioning' else 'fut_wsr'
        symbol = args.get('symbol', 'AU')
        for row, received in fetch_range(exp, api, {'symbol': symbol, 'exchange': 'SHFE'}, start, end, cap=2000 if api == 'fut_holding' else 1000):
            if row.get('symbol') != symbol:
                continue
            item = daily_record(row, api, received)
            if aware(item['available_at']) <= cut and start <= parse_date(item['data_date']) <= end:
                item['unit'] = 'contracts' if api == 'fut_holding' else row.get('unit', 'provider_unspecified')
                rows.append(item)
        meta.update(symbol=symbol, note='Member ranking coverage is partial; zero/absent rank is not proof of no position.' if api == 'fut_holding' else 'Registered receipts, not total gold inventories. Preserve provider units.')
    elif name == 'get_us_yields':
        kinds = ('nominal', 'real') if args['kind'] in ('both', 'breakeven') else (args['kind'],)
        for kind in kinds:
            api = 'us_tycr' if kind == 'nominal' else 'us_trycr'
            for row, received in fetch_range(exp, api, {}, start, end):
                item = daily_record(row, api, received, us=True)
                if aware(item['available_at']) <= cut and start <= parse_date(item['data_date']) <= end:
                    item.update(kind=kind, unit='percent_per_annum')
                    rows.append(item)
        if args['kind'] == 'breakeven':
            rows = breakeven(rows)
        meta.update(unit='percent_per_annum', note='Next Beijing day 18:00 is an assumed publication delay, not a verified vendor arrival timestamp.')
        if args['kind'] == 'breakeven':
            meta.update(unit='percentage_points', note='Nominal minus real yield at matching dates and maturities; includes inflation risk and liquidity premia, not a pure inflation forecast.')
    elif name == 'get_fx_rates':
        return fx_rates(research, args, start, end, limit)
    elif name == 'get_gold_etf_shares':
        return etf_shares(research, args, start, end, limit)
    elif name == 'get_risk_index':
        for row, received in fetch_range(exp, 'vix_index', {}, start, end, cap=300):
            item = daily_record(row, 'vix_index', received, us=True)
            if aware(item['available_at']) <= cut and start <= parse_date(item['data_date']) <= end:
                for field in ('open', 'high', 'low', 'close', 'pct_change'):
                    item[field] = finite_number(row.get(field))
                item.update(index='VIX', unit='annualized_volatility_percent', gram_role='supplemental_equity_risk_proxy')
                rows.append(item)
        meta.update(index='VIX', note='SP500 30-day equity implied volatility; not gold 3-month implied volatility or GPR.')
    elif name == 'get_macro':
        return macro(research, args['dataset'], start, end, limit, args.get('offset', 0))
    else:
        return calendar(research, args, start, end, limit)
    return envelope(rows, limit, offset=args.get('offset', 0), **meta)


def macro(research, api, start, end, limit, offset=0):
    cut = cutoff(research.day)
    if api in ('shibor', 'shibor_lpr'):
        rows = []
        for row, received in fetch_range(research.exp, api, {}, start, end):
            item = daily_record(row, api, received)
            if aware(item['available_at']) <= cut and start <= parse_date(item['data_date']) <= end:
                item.update(dataset=api, unit='percent_per_annum')
                rows.append(item)
        return envelope(rows, limit, offset=offset, source='tushare', dataset=api)
    if api == 'cn_gdp':
        params = {'start_q': f'{start.year}Q{(start.month-1)//3+1}', 'end_q': f'{end.year}Q{(end.month-1)//3+1}'}
    else:
        params = {'start_m': start.strftime('%Y%m'), 'end_m': end.strftime('%Y%m')}
    raw, received = cached(research.exp, api, params, 2000)
    # These endpoints expose latest values, not historical revision vintages.
    # A release calendar alone cannot turn a revised value into a historical value.
    if aware(received) > cut:
        return dict(status='historical_vintage_unavailable', rows=[], dataset=api, source='tushare',
                    reason='Latest monthly/quarterly values were observed after cutoff. Import verified available_at snapshots; do not backdate revised data.')
    # At first observation these reference periods must already be complete.
    rows = []
    for row in raw:
        period = row.get('month') or row.get('quarter')
        if api == 'cn_gdp':
            match = re.fullmatch(r'(\d{4})Q([1-4])', str(period))
            if not match:
                continue
            year, quarter = map(int, match.groups())
            month = quarter*3
        else:
            if not re.fullmatch(r'\d{6}', str(period)):
                continue
            year, month = int(str(period)[:4]), int(str(period)[4:])
        after = date(year+1, 1, 1) if month == 12 else date(year, month+1, 1)
        if after > cut.date():
            continue
        item = dict(row, source='tushare', source_api=api, dataset=api, data_date=(after-timedelta(days=1)).isoformat(),
                    available_at=received, observed_at=received, availability_basis='first_observed_latest_vintage',
                    units={'growth_rates': 'percent', 'price_indices': 'provider_index_base', 'pmi': 'index_points',
                           'gdp_components': 'CNY_100_million', 'money_supply_levels': 'CNY_100_million',
                           'inc_month': 'CNY_100_million', 'inc_cumval': 'CNY_100_million', 'stk_endval': 'CNY_trillion'})
        # PMI has many subindices: select the main manufacturing/non-manufacturing/composite series.
        if api == 'cn_pmi':
            item = {k: v for k, v in item.items() if not k.startswith('pmi') or k in ('pmi010000', 'pmi020100', 'pmi030000')}
        rows.append(item)
    return envelope(rows, limit, offset=offset, source='tushare', dataset=api, vintage_policy='first_observed_latest_vintage')


def calendar(research, args, start, end, limit):
    rows = []
    cut = cutoff(research.day)
    china = args['calendar'] == 'china'
    batches = []
    if china:
        cursor = start.replace(day=1)
        while cursor <= end:
            raw, received = cached(research.exp, 'cn_schedule', {'m': cursor.strftime('%Y%m')}, 3000)
            batches.extend((r, received) for r in raw)
            cursor = (cursor.replace(day=28)+timedelta(days=4)).replace(day=1)
    else:
        params = {}
        country = args.get('country', '美国')
        if len(country) > 40:
            raise ValueError('country_length')
        params['country'] = country
        batches = fetch_range(research.exp, 'eco_cal', params, start, end, cap=100)
    hidden = False
    query = args.get('query', '')
    if not isinstance(query, str) or len(query) > 100:
        raise ValueError('query_length')
    for row, received in batches:
        if aware(received) > cut:
            hidden = True
            continue
        text = str(row.get('title' if china else 'event', ''))
        if query.lower() not in text.lower():
            continue
        day = parse_date(row.get('publish_date') if china else row['date'])
        if not start <= day <= end:
            continue
        # Provider global calendar does not specify a reliable timezone. Only expose
        # the complete stored response from its observation timestamp, never infer a
        # historical publication timestamp from date + time.
        item = dict(row, source='tushare', source_api='cn_schedule' if china else 'eco_cal', calendar=args['calendar'],
                    data_date=day.isoformat(), available_at=received, observed_at=received,
                    availability_basis='first_observed_calendar_vintage', event_timezone='unverified')
        rows.append(item)
    result = envelope(rows, limit, offset=args.get('offset', 0), source='tushare', vintage_policy='first_observed_or_imported',
                      note='No assumption that historical forecasts/pre-values are unrevised. Global event clock timezone unverified; imported snapshots must specify availability with timezone.')
    if not rows and hidden:
        result['status'] = 'historical_vintage_unavailable'
    return result


def finite_number(value):
    """Normalize providers returning numeric strings; never emit NaN/Infinity."""
    import math
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def breakeven(rows):
    curves = {}
    for row in rows:
        curves.setdefault(row['data_date'], {})[row['kind']] = row
    result = []
    for day, pair in sorted(curves.items()):
        if set(pair) != {'nominal', 'real'}:
            continue
        nominal, real = pair['nominal'], pair['real']
        values = {}
        for maturity in ('y5', 'y7', 'y10', 'y20', 'y30'):
            n, r = finite_number(nominal.get(maturity)), finite_number(real.get(maturity))
            values[maturity] = n-r if n is not None and r is not None else None
        if not any(v is not None for v in values.values()):
            continue
        result.append(dict(values, kind='breakeven', source='tushare_derived', source_api='us_tycr+us_trycr',
                           data_date=day, available_at=max((nominal['available_at'], real['available_at']), key=aware),
                           observed_at=max((nominal['observed_at'], real['observed_at']), key=aware),
                           availability_basis=nominal['availability_basis'], unit='percentage_points',
                           formula='matching nominal yield minus real yield',
                           inputs={'nominal': {k: nominal.get(k) for k in values}, 'real': {k: real.get(k) for k in values}}))
    return result


def fx_rates(research, args, start, end, limit):
    code = args['ts_code']
    base, quote = FX_INSTRUMENTS[code]
    spans = [(start, end, code)]
    if code == 'USDOLLAR':
        # Official fx_daily documentation changes non-metal basket codes on this date.
        migration = date(2023, 6, 1)
        spans = []
        if start < migration:
            spans.append((start, min(end, migration-timedelta(days=1)), 'USDOLLAR.FXCM'))
        if end >= migration:
            spans.append((max(start, migration), end, 'USDOLLAR'))
    rows = []
    for begin, stop, provider_code in spans:
        for row, received in fetch_range(research.exp, 'fx_daily', {'ts_code': provider_code}, begin, stop, cap=1000):
            if row.get('ts_code') not in (code, provider_code):
                continue
            item = daily_record(row, 'fx_daily', received)
            if aware(item['available_at']) > cutoff(research.day) or not start <= parse_date(item['data_date']) <= end:
                continue
            for field in ('bid_open', 'ask_open', 'bid_close', 'ask_close', 'bid_high', 'ask_high', 'bid_low', 'ask_low'):
                item[field] = finite_number(row.get(field))
            for b, a in (('bid_open', 'ask_open'), ('bid_close', 'ask_close')):
                if item[b] is None or item[a] is None or not 0 < item[b] <= item[a]:
                    raise ValueError('invalid_FX_bid_ask_quote')
            item.update(ts_code=code, provider_ts_code=row['ts_code'], base_currency=base, quote_currency=quote,
                        mid_close=(item['bid_close']+item['ask_close'])/2,
                        unit='index_points' if code == 'USDOLLAR' else f'{quote}_per_{base}',
                        instrument_type='FXCM_dollar_basket' if code == 'USDOLLAR' else 'currency_pair')
            rows.append(item)
    return envelope(rows, limit, offset=args.get('offset', 0), source='tushare', ts_code=code,
                    base_currency=base, quote_currency=quote, gram_dimension='opportunity_cost_fx',
                    note='FXCM quotes, GMT session dates; CNH is offshore yuan. USDOLLAR is a provider basket, not ICE DXY or the original GRAM FX indices.')


def etf_shares(research, args, start, end, limit):
    code, api = args['ts_code'], args['source_api']
    rows = []
    for row, received in fetch_range(research.exp, api, {'ts_code': code}, start, end, cap=2000 if api == 'fund_share' else 5000):
        if row.get('ts_code') != code:
            continue
        item = daily_record(row, api, received, hour=12)
        if aware(item['available_at']) > cutoff(research.day) or not start <= parse_date(item['data_date']) <= end:
            continue
        if parse_date(item['data_date']) < parse_date(GOLD_ETFS[code][1]):
            continue
        value = finite_number(row.get('fd_share' if api == 'fund_share' else 'total_share'))
        if value is None or value < 0:
            raise ValueError('invalid_gold_ETF_share_count')
        size = finite_number(row.get('total_size')) if api == 'etf_share_size' else None
        if size is not None and size < 0:
            raise ValueError('invalid_gold_ETF_size')
        item.update(shares=value*10000, shares_unit='shares', size_cny=size*10000 if size is not None else None,
                    raw_units={'fd_share': '10000_shares'} if api == 'fund_share' else {'total_share': '10000_shares', 'total_size': 'CNY_10000'},
                    etf_name=GOLD_ETFS[code][0], benchmark='SHAU.SGE' if code in SHAU_ETFS else 'Au99.99.SGE',
                    gram_role='China_gold_ETF_share_proxy')
        rows.append(item)
    # Never compute changes using a hidden/future observation or mix sources.
    rows.sort(key=lambda r: r['data_date'])
    prior = None
    for row in rows:
        row['previous_observation_date'] = prior['data_date'] if prior else None
        row['share_change_since_previous_observation'] = row['shares']-prior['shares'] if prior else None
        row['size_change_cny_since_previous_observation'] = (row['size_cny']-prior['size_cny']
            if prior and row['size_cny'] is not None and prior['size_cny'] is not None else None)
        prior = row
    return envelope(rows, limit, offset=args.get('offset', 0), source='tushare', source_api=api, ts_code=code,
                    gram_dimension='momentum', scope='selected_China_gold_ETFs',
                    note='Changes are between available observations, not necessarily consecutive sessions. ETF splits are not adjusted. NAV-valued size changes are not cash flows; this is not global gold ETF flows.')
