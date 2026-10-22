import pytest

from foundry_runtime._foundry_cost import estimate


def metrics(**values):
    return {key: {"observed_sum": value} for key, value in values.items()}


def test_codex_cache_and_reasoning_are_not_double_counted():
    result = estimate('gpt-6.1-sol', metrics(input_tokens=1000,
        cached_input_tokens=700, cache_write_input_tokens=100,
        output_tokens=200, reasoning_output_tokens=150), backend='codex')
    tokens = result['token_breakdown']
    assert tokens['ordinary_input_tokens'] == 200
    assert tokens['total_input_tokens'] == 1000
    assert tokens['output_tokens'] == 200
    assert result['usd_min'] == pytest.approx(.00272)


def test_unknown_claude_model_retains_disjoint_observed_counters():
    result = estimate('future-model', metrics(input_tokens=100,
        cache_read_input_tokens=700, cache_creation_input_tokens=200,
        output_tokens=50), backend='claude')
    assert result['usd_min'] is None
    assert result['token_breakdown']['total_input_tokens'] == 1000
    assert result['token_breakdown']['ordinary_input_tokens'] == 100


@pytest.mark.parametrize('cached', [None, -1, True, 2000])
def test_incomplete_or_inconsistent_counters_are_not_reported_as_zero(cached):
    result = estimate('gpt-6.1-sol', metrics(input_tokens=1000,
        cached_input_tokens=cached, cache_write_input_tokens=0,
        output_tokens=50), backend='codex')
    assert result['token_breakdown'] is None
    assert result['usd_min'] is None


def test_partial_cache_observation_is_not_complete_breakdown():
    values = metrics(input_tokens=1000, cached_input_tokens=700,
                     cache_write_input_tokens=0, output_tokens=50)
    values['cached_input_tokens']['missing_call_ids'] = ['missing']
    assert estimate('gpt-6.1-sol', values, backend='codex')['token_breakdown'] is None
