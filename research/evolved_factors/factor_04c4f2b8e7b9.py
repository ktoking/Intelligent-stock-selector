"""Generated factor candidate; live eligibility requires fresh validation."""
from quant_agent.evolution import expression_score

FACTOR_EXPRESSION = {'op': 'lag', 'window': 1, 'x': {'op': 'rank', 'x': {'op': 'rank', 'x': {'op': 'rank', 'x': {'op': 'rank', 'x': {'op': 'rank', 'x': {'op': 'rank', 'x': {'op': 'rank', 'x': {'op': 'rank', 'x': {'op': 'rank', 'x': {'op': 'rank', 'x': {'op': 'rank', 'x': {'op': 'rank', 'x': {'field': 'residual_momentum'}}}}}}}}}}}}}}

def score(fields, stock_mask=None):
    return expression_score(FACTOR_EXPRESSION, fields, stock_mask)
