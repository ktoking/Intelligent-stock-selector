"""Generated factor candidate; live eligibility requires fresh validation."""
from quant_agent.evolution import expression_score

FACTOR_EXPRESSION = {'nodes': [{'a': None, 'b': None, 'field': 'returns', 'op': 'field', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': 'turnover', 'op': 'field', 'window': None, 'x': None}, {'a': 0, 'b': 1, 'field': None, 'op': 'mul', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': None, 'op': 'mean', 'window': 20, 'x': 2}, {'a': None, 'b': None, 'field': None, 'op': 'mean', 'window': 20, 'x': 0}, {'a': None, 'b': None, 'field': None, 'op': 'mean', 'window': 20, 'x': 1}, {'a': 4, 'b': 5, 'field': None, 'op': 'mul', 'window': None, 'x': None}, {'a': 3, 'b': 6, 'field': None, 'op': 'sub', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': 'vol20', 'op': 'field', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': None, 'op': 'std', 'window': 20, 'x': 1}, {'a': 8, 'b': 9, 'field': None, 'op': 'mul', 'window': None, 'x': None}, {'a': 7, 'b': 10, 'field': None, 'op': 'div', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': None, 'op': 'lag', 'window': 1, 'x': 11}, {'a': None, 'b': None, 'field': None, 'op': 'rank', 'window': None, 'x': 12}], 'root': 13}

def score(fields, stock_mask=None):
    return expression_score(FACTOR_EXPRESSION, fields, stock_mask)
