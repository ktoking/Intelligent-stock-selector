"""Minimal V2 Canvas edit: profit-dependent trail and confirmed trend exit."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

from scripts.moomoo_quant_binary import _code_lines, _strategy_class
from research.soxl_regime.quant_main_wave import risk_source


NAME = "SOXL_MAIN_WAVE_RUNNER_V3_RESEARCH"
PARAMETERS = {"WAVE_RUNNER_TRIGGER": "0.20", "WAVE_RUNNER_TRAIL": "0.16"}


def runner_risk_source() -> str:
    source = risk_source()
    old = "    _trail_stop = _hour_ready and self.WAVE_PEAK_CLOSE > 0 and _hour_close <= self.WAVE_PEAK_CLOSE * (1.0 - self.WAVE_TRAIL)\n"
    new = """    _effective_trail = self.WAVE_TRAIL
    if self.WAVE_ENTRY_PRICE > 0 and self.WAVE_PEAK_CLOSE >= self.WAVE_ENTRY_PRICE * (1.0 + self.WAVE_RUNNER_TRIGGER):
        _effective_trail = self.WAVE_RUNNER_TRAIL
    _trail_stop = _hour_ready and self.WAVE_PEAK_CLOSE > 0 and _hour_close <= self.WAVE_PEAK_CLOSE * (1.0 - _effective_trail)
    _older_close = bar_close(symbol=self.trading_symbol, bar_type=BarType.K_DAY, select=3, session_type=THType.RTH)
    _older_ema20 = ema(symbol=self.trading_symbol, period=20, bar_type=BarType.K_DAY, select=3, session_type=THType.RTH)
    _older_ready = _older_close is not None and _older_ema20 is not None and _older_close > 0 and _older_ema20 > 0
    _trend_exit = _wave_ready and (_qqq_close <= _qqq_e60 or (_older_ready and _soxl_close_d < _soxl_e20_d and _older_close < _older_ema20))
"""
    assert source.count(old) == 1
    source = source.replace(old, new)
    source = source.replace("(_wave_ready and not _wave_up) or _trail_stop or _hard_stop",
                            "_trend_exit or _trail_stop or _hard_stop")
    ast.parse(source)
    return source


def build(template: Path, output: Path) -> dict:
    Strategy = _strategy_class()
    original_bytes = template.read_bytes()
    original = Strategy.FromString(original_bytes)
    assert original.strategyName == "SOXL_MAIN_WAVE_TREND_V2_BACKTEST"
    assert original.strategyType == 1 and len(original.canvasCardList) == 98
    strategy = Strategy.FromString(original_bytes)
    cards = {card.id: card for card in strategy.canvasCardList}
    start = cards[strategy.startCardId].canvasCardStart
    variable_template = next(v for v in start.varList if v.name == "WAVE_TRAIL")
    next_id = max(v.id for v in start.varList) + 1
    for offset, (name, value) in enumerate(PARAMETERS.items()):
        variable = start.varList.add()
        variable.CopyFrom(variable_template)
        variable.id, variable.name = next_id + offset, name
        variable.initValue.value = value
        variable.initDesc = ""
    del cards[1092].userCode.userCode[:]
    cards[1092].userCode.userCode.extend(_code_lines(runner_risk_source()))
    cards[1092].name = "盈利后延长持仓／趋势确认退出"
    strategy.strategyName = NAME
    strategy.strategyId = 0
    strategy.cloudStrategyId = 0
    payload = strategy.SerializeToString()
    parsed = Strategy.FromString(payload)
    changed = [old.id for old, new in zip(original.canvasCardList, parsed.canvasCardList)
               if old.SerializeToString() != new.SerializeToString()]
    checks = {
        "only_start_and_risk_changed": changed == [strategy.startCardId, 1092],
        "all_lines_unchanged": [x.SerializeToString() for x in original.canvasLineList] == [x.SerializeToString() for x in parsed.canvasLineList],
        "roundtrip": payload == parsed.SerializeToString(),
        "parameters_numeric": all(v.initDesc == "" and v.initValue.value == PARAMETERS[v.name]
                                  for v in start.varList if v.name in PARAMETERS),
        "unique_variable_ids": len({v.id for v in start.varList}) == len(start.varList),
        "no_new_time_api": "device_time(" not in runner_risk_source(),
    }
    for card in parsed.canvasCardList:
        if card.isUserCode:
            ast.parse("".join(card.userCode.userCode))
    if not all(checks.values()):
        raise ValueError(checks)
    output.write_bytes(payload)
    return {"path": str(output), "sha256": hashlib.sha256(payload).hexdigest(),
            "cards": len(parsed.canvasCardList), "lines": len(parsed.canvasLineList),
            "checks": checks, "futu_client_backtest": "not_run"}


if __name__ == "__main__":
    root = Path.home() / "Downloads"
    print(json.dumps(build(root / "SOXL_MAIN_WAVE_TREND_V2_BACKTEST.quant",
                           root / f"{NAME}.quant"), ensure_ascii=False, indent=2))
