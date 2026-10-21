"""Generated minute research factor; broker orders are never called here."""
from quant_agent.minute_factors import score

EXPRESSION = {'nodes': [{'a': None, 'b': None, 'field': 'returns', 'op': 'field', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': None, 'op': 'mean', 'window': 3, 'x': 0}, {'a': None, 'b': None, 'field': None, 'op': 'abs', 'window': None, 'x': 0}, {'a': None, 'b': None, 'field': None, 'op': 'mean', 'window': 3, 'x': 2}, {'a': 1, 'b': 3, 'field': None, 'op': 'div', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': 'close_pressure', 'op': 'field', 'window': None, 'x': None}, {'a': None, 'b': None, 'field': None, 'op': 'mean', 'window': 3, 'x': 5}, {'a': 4, 'b': 6, 'field': None, 'op': 'sub', 'window': None, 'x': None}], 'root': 7}

def calculate(fields):
    return score(EXPRESSION, fields)
