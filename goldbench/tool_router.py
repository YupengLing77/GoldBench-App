"""Local per-round tool selection; no extra model/provider call is made here."""
import copy
import json

CORE = ('get_prices', 'get_history')
GROUPS = {
    'gold_market': ('get_gold_futures', 'get_gold_spot', 'get_gold_warehouse'),
    'macro': ('get_macro', 'get_economic_calendar'),
    'rates_fx': ('get_us_yields', 'get_fx_rates'),
    'risk_news': ('get_risk_index', 'search_news', 'read_news'),
    'positioning': ('get_positioning', 'get_gold_etf_shares'),
    'analysis': ('run_backtest', 'run_python'),
    'archives': ('read_webpage',),
}
SUMMARIES = {
    'get_prices': 'Frozen XAUUSD daily bars and Python handle.',
    'get_history': 'This account past decisions and mature outcomes.',
    'get_gold_futures': 'SHFE gold futures prices and main mapping.',
    'get_gold_spot': 'SGE gold spot/deferred daily prices.',
    'get_gold_warehouse': 'SHFE gold registered receipts.',
    'get_macro': 'China growth, inflation, liquidity; as-of vintages.',
    'get_economic_calendar': 'Global events/China releases; as-of vintages.',
    'get_us_yields': 'US nominal/real yields and breakeven proxy.',
    'get_fx_rates': 'Major FXCM currency pairs/dollar basket.',
    'get_risk_index': 'VIX equity risk proxy, not gold IV.',
    'search_news': 'Search historical Tushare news.',
    'read_news': 'Read discovered news text by ID/offset.',
    'get_positioning': 'SHFE gold member position ranks.',
    'get_gold_etf_shares': 'China gold ETF shares/size, not global flows.',
    'run_backtest': 'Fixed SMA20 on XAUUSD handle.',
    'run_python': 'Docker computation on an acquired handle.',
    'read_webpage': 'Imported archived page by record ID.',
}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


class ToolRouter:
    def __init__(self, tools, submit):
        self.registry = {t['function']['name']: t for t in tools}
        if set(self.registry) != set(SUMMARIES) or set(self.registry) != set(CORE) | {n for names in GROUPS.values() for n in names}:
            raise ValueError('router_registry_catalog_mismatch')
        self.submit = submit
        self.selected = ()
        self.attempts = 0
        self.schema = {'type': 'function', 'function': {
            'name': 'select_tools',
            'description': 'Replace optional tools for the NEXT model request. Select any tools or categories from tool_directory. Core price/history and submit remain. Empty tools clears selection. No count cap; token, cost and time budgets still apply. No market data is fetched.',
            'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {
                'tools': {'type': 'array', 'items': {'type': 'string', 'enum': list(self.registry)}},
                'categories': {'type': 'array', 'items': {'type': 'string', 'enum': list(GROUPS)}},
            }}}}

    def directory(self):
        return {'core': {name: SUMMARIES[name] for name in CORE},
                'categories': {group: {name: SUMMARIES[name] for name in names} for group, names in GROUPS.items()},
                'selection_policy': 'No count cap; keep useful schemas and respect token/cost/time budgets.',
                'mode': 'replace optional selection; effects start on next model request'}

    def schemas(self):
        return [self.submit, self.schema] + [self.registry[n] for n in CORE + self.selected]

    def select(self, args):
        self.attempts += 1
        if not isinstance(args, dict) or not args or set(args)-{'tools', 'categories'}:
            raise ValueError('select_tools_requires_tools_or_categories')
        chosen = []
        for key in ('categories', 'tools'):
            values = args.get(key, [])
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                raise ValueError('invalid_router_selection')
            for value in values:
                if key == 'categories':
                    if value not in GROUPS:
                        raise ValueError('unknown_tool_category')
                    chosen.extend(GROUPS[value])
                else:
                    if value not in self.registry:
                        raise ValueError('unknown_research_tool')
                    chosen.append(value)
        selected = tuple(dict.fromkeys(n for n in chosen if n not in CORE))
        # Validate everything before changing the current selection.
        self.selected = selected
        return {'status': 'selected', 'active_research_tools': list(CORE+self.selected),
                'effective': 'next_model_request'}


def bounded_result(raw, max_bytes=5500):
    """Bound what the model sees; caller archives original output separately.

    The bound is in UTF-8 bytes. It preserves valid JSON, handles and IDs, and
    clearly marks sampling. Evidence must be generated from this visible result.
    """
    result = copy.deepcopy(raw)
    truncated = False
    while len(dumps(result).encode()) > max_bytes-200:
        candidates = []
        def visit(value, parent=None, key=None):
            if isinstance(value, list):
                if len(value) > 1:
                    candidates.append((len(dumps(value).encode()), 'list', parent, key, value))
                for i, child in enumerate(value):
                    visit(child, value, i)
            elif isinstance(value, dict):
                for field, child in value.items():
                    visit(child, value, field)
            elif isinstance(value, str) and len(value.encode()) > 256:
                candidates.append((len(value.encode()), 'text', parent, key, value))
        visit(result)
        if not candidates:
            return {'status': 'response_too_large', 'hint': 'Reduce limit or paginate; no oversized evidence was shown.'}, True
        _, kind, parent, key, value = max(candidates, key=lambda c: c[0])
        if kind == 'list':
            size = max(1, len(value)//2)
            # Price/history rows are chronological; top-level news lists are newest first.
            replacement = value[-size:] if key in ('rows', 'points', 'trades') else value[:size]
        else:
            replacement = value.encode()[:max(128, len(value.encode())//2)].decode('utf-8', errors='ignore')
        if parent is None:
            result = replacement
        else:
            parent[key] = replacement
        truncated = True
    if truncated:
        if not isinstance(result, dict):
            result = {'items': result}
        if isinstance(result.get('rows'), list) and 'total_visible' in result:
            count = len(result['rows'])
            offset = result.get('offset', 0)
            result.update(returned=count, omitted=max(0, result['total_visible']-count),
                          next_offset=offset+count if count and offset+count < result['total_visible'] else None)
        result['context_limited'] = True
        result['context_hint'] = 'Partial output. Reduce limit or paginate; only shown content is evidence. Handles may retain the full acquired data.'
    return result, truncated
