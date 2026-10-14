"""Small causal factor DSL: cross-sectional rank blends, never eval/exec."""
from __future__ import annotations

from dataclasses import replace
import math

import numpy as np
import pandas as pd

from research.daily_factor_lab.core import Panel, Policy


FACTORS = ("momentum_risk","momentum_blend","residual_momentum","path_quality","near_high")


def validate_weights(weights):
    if not weights:
        return
    if not 1<=len(weights)<=4 or len({x["factor"] for x in weights})!=len(weights):
        raise ValueError("Factor blend must have 1-4 distinct supported factors")
    if any(x["factor"] not in FACTORS or not math.isfinite(x["weight"]) or not 0<x["weight"]<=1 for x in weights):
        raise ValueError("Invalid factor blend")
    if not math.isclose(sum(x["weight"] for x in weights),1,abs_tol=1e-6):
        raise ValueError("Factor weights must sum to one")


def compile_spec(panel, spec):
    weights=spec.get("factor_weights",[])
    validate_weights(weights)
    policy=Policy(**spec["policy"])
    if not weights:
        return panel,policy
    blended=np.zeros_like(panel.fields["close"])
    missing=np.zeros_like(blended,dtype=bool)
    for component in weights:
        source=panel.fields[component["factor"]].copy()
        source[:,~panel.stock_mask]=np.nan
        ranked=pd.DataFrame(source).rank(axis=1,pct=True).to_numpy()
        missing |= ~np.isfinite(ranked)
        blended += np.nan_to_num(ranked)*component["weight"]
    blended[missing]=np.nan
    fields=dict(panel.fields);fields["momentum_blend"]=blended
    return Panel(panel.dates,panel.symbols,panel.stock_mask,fields,panel.metadata),replace(policy,factor="momentum_blend")
