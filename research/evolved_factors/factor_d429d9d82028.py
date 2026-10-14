"""Generated factor candidate; live eligibility requires fresh validation."""
from quant_agent.evolution import expression_score

FACTOR_EXPRESSION = {'nodes': [{'a': None, 'b': None, 'field': 'close', 'op': 'field', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': None, 'op': 'lag', 'window': 1, 'x': 0}, {'a': None, 'b': None, 'field': 'open', 'op': 'field', 'window': None, 'x': None}, {'a': 2, 'b': 1, 'field': None, 'op': 'sub', 'window': None, 'x': None}, {'a': 3, 'b': 1, 'field': None, 'op': 'div', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': None, 'op': 'std', 'window': 20, 'x': 4}, {'a': None, 'b': None, 'field': 'vol20', 'op': 'field', 'window': None, 'x': None}, {'a': 5, 'b': 6, 'field': None, 'op': 'div', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': None, 'op': 'rank', 'window': None, 'x': 7}, {'a': None, 'b': None, 'field': 'residual_momentum', 'op': 'field', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': None, 'op': 'rank', 'window': None, 'x': 9}, {'a': 10, 'b': 8, 'field': None, 'op': 'sub', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': None, 'op': 'lag', 'window': 1, 'x': 11}], 'root': 12}

def score(fields, stock_mask=None):
    return expression_score(FACTOR_EXPRESSION, fields, stock_mask)
