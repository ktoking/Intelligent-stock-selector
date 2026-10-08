from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scripts.moomoo_quant_binary import _code_lines, _strategy_class


VARIABLES = {
    "TREND_POSITION": "0.25", "RANGE_POSITION": "0.25", "PANIC_POSITION": "0",
    "REGIME_ENTER": "0.005", "REGIME_EXIT": "0.0025", "REGIME_SLOPE": "0.0025",
    "RANGE_SPREAD": "0.006", "RANGE_SLOPE": "0.004", "RANGE_WIDTH": "0.10",
    "RANGE_RSI": "45", "RANGE_TP": "0.015", "RANGE_SL": "0.02",
    "PANIC_RSI": "35", "PANIC_DROP": "0.06", "PANIC_TP": "0.015", "PANIC_SL": "0.03",
    "TREND_STOP": "0.10", "EXECUTION_ENABLED": "0", "MAX_TRADES_DAY": "3",
    "TRADE_COUNT": "0", "POSITION_MODE": "0", "REGIME_CODE": "0",
    "CANDIDATE_REGIME": "0", "REGIME_CONFIRM": "0", "LAST_DAY_KEY": "0",
}


def action_source() -> str:
    return r'''# SOXL_REGIME_SWITCH_V1: completed bars only (select=2 is latest completed bar).
_now = device_time(TimeZone.MARKET_TIME_ZONE)
_day_key = _now.year * 10000 + _now.month * 100 + _now.day
if self.LAST_DAY_KEY != _day_key and (_now.hour > 9 or (_now.hour == 9 and _now.minute >= 30)):
    self.LAST_DAY_KEY = _day_key
    self.TRADE_COUNT = 0

_c30 = bar_close(symbol=self.trading_symbol, bar_type=BarType.K_30M, select=2, session_type=THType.RTH)
_ema20 = ema(symbol=self.trading_symbol, period=20, bar_type=BarType.K_30M, select=2, session_type=THType.RTH)
_ema60 = ema(symbol=self.trading_symbol, period=60, bar_type=BarType.K_30M, select=2, session_type=THType.RTH)
_ema20_old = ema(symbol=self.trading_symbol, period=20, bar_type=BarType.K_30M, select=8, session_type=THType.RTH)
_upper30 = boll_upper(symbol=self.trading_symbol, period=20, deviation=1.75, bar_type=BarType.K_30M, select=2, session_type=THType.RTH)
_lower30 = boll_lower(symbol=self.trading_symbol, period=20, deviation=1.75, bar_type=BarType.K_30M, select=2, session_type=THType.RTH)
_mid30 = boll_mid(symbol=self.trading_symbol, period=20, deviation=1.75, bar_type=BarType.K_30M, select=2, session_type=THType.RTH)
_spread = _ema20 / _ema60 - 1.0
_slope = _ema20 / _ema20_old - 1.0
_width30 = (_upper30 - _lower30) / _mid30 if _mid30 > 0 else 0.0
_candidate = 0
if _spread >= self.REGIME_ENTER and _slope > self.REGIME_SLOPE and _c30 > _ema20:
    _candidate = 1
elif _spread <= -self.REGIME_ENTER and _slope < -self.REGIME_SLOPE and _c30 < _ema20:
    _candidate = 3
elif abs(_spread) <= self.RANGE_SPREAD and abs(_slope) <= self.RANGE_SLOPE and _width30 <= self.RANGE_WIDTH:
    _candidate = 2
elif self.REGIME_CODE == 1 and _spread >= self.REGIME_EXIT:
    _candidate = 1
elif self.REGIME_CODE == 3 and _spread <= -self.REGIME_EXIT:
    _candidate = 3
if _candidate == self.REGIME_CODE:
    self.CANDIDATE_REGIME = _candidate
    self.REGIME_CONFIRM = 0
elif _candidate == self.CANDIDATE_REGIME:
    self.REGIME_CONFIRM = self.REGIME_CONFIRM + 1
else:
    self.CANDIDATE_REGIME = _candidate
    self.REGIME_CONFIRM = 1
if self.REGIME_CONFIRM >= 2:
    self.REGIME_CODE = self.CANDIDATE_REGIME
    self.REGIME_CONFIRM = 0

_close = bar_close(symbol=self.trading_symbol, bar_type=BarType.K_5M, select=2, session_type=THType.RTH)
_previous_close = bar_close(symbol=self.trading_symbol, bar_type=BarType.K_5M, select=3, session_type=THType.RTH)
_rsi14 = rsi(symbol=self.trading_symbol, period=14, bar_type=BarType.K_5M, select=2, session_type=THType.RTH)
_boll_mid = boll_mid(symbol=self.trading_symbol, period=20, deviation=1.75, bar_type=BarType.K_5M, select=2, session_type=THType.RTH)
_boll_lower = boll_lower(symbol=self.trading_symbol, period=20, deviation=1.75, bar_type=BarType.K_5M, select=2, session_type=THType.RTH)
_daily_close = bar_close(symbol=self.trading_symbol, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
_dema20 = ema(symbol=self.trading_symbol, period=20, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
_dema60 = ema(symbol=self.trading_symbol, period=60, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
_daily_upper = boll_upper(symbol=self.trading_symbol, period=20, deviation=1.5, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
_day_open = bar_open(symbol=self.trading_symbol, bar_type=BarType.K_DAY, select=1, session_type=THType.RTH)
_day_drop = 1.0 - _close / _day_open if _day_open > 0 else 0.0
_holding = position_holding_qty(symbol=self.trading_symbol)
_pnl = position_pl_ratio(symbol=self.trading_symbol, cost_price_model=CostPriceModel.AVG, currency=Currency.USD)
_force_flat = _now.hour > 15 or (_now.hour == 15 and _now.minute >= 45)
_exited = 0

# Exit is always evaluated before any new entry; POSITION_MODE is fixed while held.
if _holding > 0:
    _exit = 0
    if self.POSITION_MODE == 1 and (_daily_close < _dema20 or _dema20 < _dema60 or _pnl <= -self.TREND_STOP):
        _exit = 1
    elif self.POSITION_MODE == 2 and (_rsi14 >= 55 or _close >= _boll_mid or _pnl >= self.RANGE_TP or _pnl <= -self.RANGE_SL or _force_flat):
        _exit = 1
    elif self.POSITION_MODE == 3 and (_pnl >= self.PANIC_TP or _pnl <= -self.PANIC_SL or _force_flat):
        _exit = 1
    if _exit == 1:
        if self.EXECUTION_ENABLED >= 1:
            place_market(symbol=self.trading_symbol, qty=_holding, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
        alert(content='SOXL REGIME EXIT mode=' + str(self.POSITION_MODE) + ' pnl=' + str(_pnl))
        self.POSITION_MODE = 0
        _exited = 1

_entry_window = (_now.hour > 9 or (_now.hour == 9 and _now.minute >= 45)) and (_now.hour < 15 or (_now.hour == 15 and _now.minute <= 30))
if _holding <= 0 and _exited == 0 and _entry_window and self.TRADE_COUNT < self.MAX_TRADES_DAY:
    _mode = 0
    _ratio = 0.0
    if self.REGIME_CODE == 1 and _daily_close > _daily_upper and _dema20 > _dema60:
        _mode = 1
        _ratio = self.TREND_POSITION
    elif self.REGIME_CODE == 2 and _rsi14 < self.RANGE_RSI and _close < _boll_lower and _close > _previous_close:
        _mode = 2
        _ratio = self.RANGE_POSITION
    elif self.REGIME_CODE == 3 and _rsi14 < self.PANIC_RSI and _close < _boll_lower and _day_drop >= self.PANIC_DROP and _close > _previous_close:
        _mode = 3
        _ratio = self.PANIC_POSITION
    if _mode > 0:
        _net = net_asset(currency=Currency.USD)
        _price = current_price(symbol=self.trading_symbol, price_type=THType.RTH)
        _lot = lot_size(symbol=self.trading_symbol)
        _target_qty = floor(_net * _ratio / _price / _lot) * _lot
        _max_qty = max_qty_to_buy_on_cash(symbol=self.trading_symbol, order_type=OrdType.MKT, order_trade_session_type=TSType.RTH)
        if _target_qty > _max_qty:
            _target_qty = _max_qty
        if _target_qty > 0:
            if self.EXECUTION_ENABLED >= 1:
                place_market(symbol=self.trading_symbol, qty=_target_qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
            self.POSITION_MODE = _mode
            self.TRADE_COUNT = self.TRADE_COUNT + 1
            alert(content='SOXL REGIME ENTRY mode=' + str(_mode) + ' ratio=' + str(_ratio) + ' rsi14=' + str(_rsi14))
'''


def build(template_path: Path, output_path: Path, strategy_name: str = "SOXL_REGIME_SWITCH_V1_RESEARCH_CANDIDATE") -> dict:
    Strategy = _strategy_class()
    strategy = Strategy.FromString(template_path.read_bytes())
    start = next(card for card in strategy.canvasCardList if card.id == strategy.startCardId)
    action = next(card for card in strategy.canvasCardList if card.id == 1006 and card.isUserCode)
    line = next(edge for edge in strategy.canvasLineList if edge.startCardId == start.id)
    start_copy, action_copy, line_copy = type(start)(), type(action)(), type(line)()
    start_copy.CopyFrom(start); action_copy.CopyFrom(action); line_copy.CopyFrom(line)
    original_vars = list(start_copy.canvasCardStart.varList)
    canvas_version = next(variable for variable in original_vars if variable.name == "_canvasVersion")
    prototype = next(variable for variable in original_vars if variable.name == "POSITION_L1")
    del start_copy.canvasCardStart.varList[:]
    for offset, (name, value) in enumerate(VARIABLES.items(), start=3200):
        variable = start_copy.canvasCardStart.varList.add(); variable.CopyFrom(prototype)
        variable.id = offset; variable.name = name; variable.initValue.enType = 0; variable.initValue.value = value
        variable.isExplicit = name not in {"TRADE_COUNT", "POSITION_MODE", "REGIME_CODE", "CANDIDATE_REGIME", "REGIME_CONFIRM", "LAST_DAY_KEY"}
        variable.isCanModify = variable.isExplicit; variable.codeType = 3 if "." in value else 2
    start_copy.canvasCardStart.varList.add().CopyFrom(canvas_version)
    action_copy.id = 1006; action_copy.name = "SOXL Regime Switch V1（研究候选）"; action_copy.isUserCode = True
    action_copy.isGenByAiChat = False; action_copy.aiChatId = 0
    del action_copy.userCode.userCode[:]
    action_copy.userCode.userCode.extend(_code_lines(action_source()))
    line_copy.id = 2001; line_copy.startCardId = start_copy.id; line_copy.endCardId = action_copy.id; line_copy.condition = 0
    del strategy.canvasCardList[:]; strategy.canvasCardList.add().CopyFrom(start_copy); strategy.canvasCardList.add().CopyFrom(action_copy)
    del strategy.canvasLineList[:]; strategy.canvasLineList.add().CopyFrom(line_copy)
    strategy.strategyName = strategy_name; strategy.strategyType = 1; strategy.strategyId = 0; strategy.cloudStrategyId = 0
    strategy.isGenByAIChat = False; strategy.aiChatID = 0; strategy.aiContentID = 0
    strategy.ClearField("userCode")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = strategy.SerializeToString(); output_path.write_bytes(payload)
    return {"path": str(output_path), "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "nodes": 2, "edges": 1, "template_sha256": hashlib.sha256(template_path.read_bytes()).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, default=Path.home() / "Downloads/SOXL_QQQ_DYNAMIC_REVERSION_V4_FIXED.quant")
    parser.add_argument("--output", type=Path, default=Path("outputs/soxl_regime_switch_v1/SOXL_REGIME_SWITCH_V1_RESEARCH_CANDIDATE.quant"))
    args = parser.parse_args()
    print(json.dumps(build(args.template, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
