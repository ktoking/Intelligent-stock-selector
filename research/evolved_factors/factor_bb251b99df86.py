"""Generated factor candidate; live eligibility requires fresh validation."""
from quant_agent.evolution import expression_score

FACTOR_EXPRESSION = {'nodes': [{'a': None, 'b': None, 'field': 'residual_momentum', 'op': 'field', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': None, 'op': 'rank', 'window': None, 'x': 0}, {'a': None, 'b': None, 'field': 'returns', 'op': 'field', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': None, 'op': 'abs', 'window': None, 'x': 2}, {'a': 3, 'b': 2, 'field': None, 'op': 'sub', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': 'turnover', 'op': 'field', 'window': None, 'x': None}, {'a': 5, 'b': 4, 'field': None, 'op': 'mul', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': None, 'op': 'mean', 'window': 20, 'x': 6}, {'a': None, 'b': None, 'field': None, 'op': 'mean', 'window': 20, 'x': 5}, {'a': None, 'b': None, 'field': 'vol20', 'op': 'field', 'window': None, 'x': None}, {'a': 8, 'b': 9, 'field': None, 'op': 'mul', 'window': None, 'x': None}, {'a': 7, 'b': 10, 'field': None, 'op': 'div', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': None, 'op': 'rank', 'window': None, 'x': 11}, {'a': 1, 'b': 12, 'field': None, 'op': 'sub', 'window': None, 'x': None}], 'root': 13}

def score(fields, stock_mask=None):
    return expression_score(FACTOR_EXPRESSION, fields, stock_mask)
