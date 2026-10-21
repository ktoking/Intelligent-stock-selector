"""Generated minute research factor; broker orders are never called here."""
from quant_agent.minute_factors import score

EXPRESSION = {'nodes': [{'a': None, 'b': None, 'field': 'relative_return6', 'op': 'field', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': None, 'op': 'mean', 'window': 3, 'x': 0}, {'a': None, 'b': None, 'field': None, 'op': 'rank', 'window': None, 'x': 1}, {'a': None, 'b': None, 'field': 'vwap_distance', 'op': 'field', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': 'range_ratio', 'op': 'field', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': None, 'op': 'mean', 'window': 6, 'x': 4}, {'a': 3, 'b': 5, 'field': None, 'op': 'div', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': None, 'op': 'rank', 'window': None, 'x': 6}, {'a': 2, 'b': 7, 'field': None, 'op': 'sub', 'window': None, 'x': None}], 'root': 8}

def calculate(fields):
    return score(EXPRESSION, fields)
