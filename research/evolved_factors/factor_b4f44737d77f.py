"""Generated factor candidate; live eligibility requires fresh validation."""
from quant_agent.evolution import expression_score

FACTOR_EXPRESSION = {'nodes': [{'a': None, 'b': None, 'field': 'residual_momentum', 'op': 'field', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': None, 'op': 'rank', 'window': None, 'x': 0}, {'a': None, 'b': None, 'field': None, 'op': 'std', 'window': 5, 'x': 1}, {'a': None, 'b': None, 'field': None, 'op': 'rank', 'window': None, 'x': 2}, {'a': 1, 'b': 3, 'field': None, 'op': 'sub', 'window': None, 'x': None}], 'root': 4}

def score(fields, stock_mask=None):
    return expression_score(FACTOR_EXPRESSION, fields, stock_mask)
