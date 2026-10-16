"""Generated factor candidate; live eligibility requires fresh validation."""
from quant_agent.evolution import expression_score

FACTOR_EXPRESSION = {'nodes': [{'a': None, 'b': None, 'field': 'residual_momentum', 'op': 'field', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': None, 'op': 'rank', 'window': None, 'x': 0}, {'a': None, 'b': None, 'field': 'turnover', 'op': 'field', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': None, 'op': 'std', 'window': 20, 'x': 2}, {'a': None, 'b': None, 'field': None, 'op': 'mean', 'window': 20, 'x': 2}, {'a': 3, 'b': 4, 'field': None, 'op': 'div', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': None, 'op': 'rank', 'window': None, 'x': 5}, {'a': 1, 'b': 6, 'field': None, 'op': 'sub', 'window': None, 'x': None}], 'root': 7}

def score(fields, stock_mask=None):
    return expression_score(FACTOR_EXPRESSION, fields, stock_mask)
