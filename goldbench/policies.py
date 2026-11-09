"""Per-experiment research policy; temporal/privacy checks are independent."""
from contextvars import ContextVar
GLOBAL_ONLY = ContextVar('goldbench_global_token_only', default=False)


def total_only(config):
    return config.get('research_policy') == 'total_tokens_only'
