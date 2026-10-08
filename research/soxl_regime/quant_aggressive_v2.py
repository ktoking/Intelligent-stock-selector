from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scripts.moomoo_quant_binary import _code_lines, _strategy_class


VARIABLES: dict[str, tuple[str, bool, str]] = {
    "TREND_POSITION": ("0.90", True, "TREND_UP total-equity target"),
    "RANGE_POSITION": ("0.40", True, "RANGE total-equity target"),
    "PANIC_POSITION": ("0", True, "TREND_DOWN panic target; disabled after negative extension test"),
    "TREND_HARD_STOP": ("0.06", True, "Trend hard stop"),
    "TREND_TRAIL_STOP": ("0.08", True, "Trend trailing stop from completed 5m high"),
    "TREND_COOLDOWN_DAYS": ("5", True, "Days to pause after a trend stop"),
    "TREND_MAX_RSI": ("70", True, "Maximum completed daily RSI for a new trend entry"),
    "TREND_MAX_EXTENSION": ("0.04", True, "Maximum SOXL close extension above daily EMA20"),
    "TREND_MAX_INTRADAY_GAIN": ("0.01", True, "Do not chase after a gain above the session open"),
    "RANGE_RSI": ("35", True, "RANGE entry RSI"),
    "RANGE_TAKE_PROFIT": ("0.03", True, "RANGE take profit"),
    "RANGE_STOP_LOSS": ("0.02", True, "RANGE stop loss"),
    "PANIC_RSI": ("25", True, "TREND_DOWN panic entry RSI"),
    "PANIC_DAY_DROP": ("0.06", True, "TREND_DOWN intraday drop threshold"),
    "PANIC_TAKE_PROFIT": ("0.015", True, "TREND_DOWN panic take profit"),
    "PANIC_STOP_LOSS": ("0.03", True, "TREND_DOWN panic stop loss"),
    "MAX_TRADES_DAY": ("1", True, "Maximum entry cycles per day"),
    "EXECUTION_ENABLED": ("1", True, "1=orders enabled; intended for backtest"),
    "POSITION_MODE_V2": ("0", False, "0 cash, 1 trend, 2 range, 3 panic"),
    "PEAK_PRICE_V2": ("0", False, "Completed 5m peak while holding trend"),
    "COOLDOWN_LEFT_V2": ("0", False, "Remaining trend cooldown days"),
    "LAST_DAY_KEY_V2": ("0", False, "Internal session key"),
    "LAST_ENTRY_DAY_V2": ("0", False, "Prevents same-day trend re-entry"),
    "TRADE_COUNT_V2": ("0", False, "Entry cycles today"),
    "LEGACY_DISABLED_V2": ("0", False, "Permanent false gate for the connected legacy canvas"),
}


def risk_source() -> str:
    return r'''# SOXL_REGIME_ADAPTIVE_V3_RISK
# Time windows are native Futu cards; no unsupported TimeZone enum is used here.
_qqq = Contract("US.QQQ")
_q_close_d = bar_close(symbol=_qqq, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
_q_ema20_d = ema(symbol=_qqq, period=20, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
_q_ema60_d = ema(symbol=_qqq, period=60, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
_s_close_d = bar_close(symbol=self.trading_symbol, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
_s_ema20_d = ema(symbol=self.trading_symbol, period=20, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
_trend_up = _q_ema20_d > _q_ema60_d and _q_close_d > _q_ema20_d and _s_close_d > _s_ema20_d
_close = bar_close(symbol=self.trading_symbol, bar_type=BarType.K_5M, select=2, session_type=THType.RTH)
_high = bar_high(symbol=self.trading_symbol, bar_type=BarType.K_5M, select=2, session_type=THType.RTH)
_rsi14 = rsi(symbol=self.trading_symbol, period=14, bar_type=BarType.K_5M, select=2, session_type=THType.RTH)
_boll_mid = boll_mid(symbol=self.trading_symbol, period=20, deviation=1.75, bar_type=BarType.K_5M, select=2, session_type=THType.RTH)
_holding = position_holding_qty(symbol=self.trading_symbol)
_pnl = position_pl_ratio(symbol=self.trading_symbol, cost_price_model=CostPriceModel.AVG, currency=Currency.USD)
if _holding > 0:
    if _high > self.PEAK_PRICE_V2:
        self.PEAK_PRICE_V2 = _high
    _exit = 0
    if self.POSITION_MODE_V2 == 1:
        if not _trend_up or _pnl <= -self.TREND_HARD_STOP:
            _exit = 1
        elif self.PEAK_PRICE_V2 > 0 and _close <= self.PEAK_PRICE_V2 * (1.0 - self.TREND_TRAIL_STOP):
            _exit = 1
    elif self.POSITION_MODE_V2 == 2:
        if _rsi14 >= 55 or _close >= _boll_mid or _pnl >= self.RANGE_TAKE_PROFIT or _pnl <= -self.RANGE_STOP_LOSS:
            _exit = 1
    elif self.POSITION_MODE_V2 == 3:
        if _pnl >= self.PANIC_TAKE_PROFIT or _pnl <= -self.PANIC_STOP_LOSS:
            _exit = 1
    if _exit == 1:
        if self.EXECUTION_ENABLED >= 1:
            place_market(symbol=self.trading_symbol, qty=_holding, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
        alert(content='SOXL V3 EXIT mode=' + str(self.POSITION_MODE_V2) + ' pnl=' + str(_pnl))
        self.POSITION_MODE_V2 = 0
        self.PEAK_PRICE_V2 = 0
'''


def entry_source() -> str:
    return r'''# SOXL_REGIME_ADAPTIVE_V3_ENTRY
# The parent native card limits this action to 10:00:00-15:30:00 US Eastern.
_qqq = Contract("US.QQQ")
_q_close_d = bar_close(symbol=_qqq, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
_q_ema20_d = ema(symbol=_qqq, period=20, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
_q_ema60_d = ema(symbol=_qqq, period=60, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
_s_close_d = bar_close(symbol=self.trading_symbol, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
_s_ema20_d = ema(symbol=self.trading_symbol, period=20, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
_s_rsi_d = rsi(symbol=self.trading_symbol, period=14, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
_extension = _s_close_d / _s_ema20_d - 1.0 if _s_ema20_d > 0 else 99.0
_trend_up = _q_ema20_d > _q_ema60_d and _q_close_d > _q_ema20_d and _s_close_d > _s_ema20_d
_trend_down = _q_ema20_d < _q_ema60_d and _q_close_d < _q_ema20_d
_close = bar_close(symbol=self.trading_symbol, bar_type=BarType.K_5M, select=2, session_type=THType.RTH)
_high = bar_high(symbol=self.trading_symbol, bar_type=BarType.K_5M, select=2, session_type=THType.RTH)
_previous_close = bar_close(symbol=self.trading_symbol, bar_type=BarType.K_5M, select=3, session_type=THType.RTH)
_ema20_5m = ema(symbol=self.trading_symbol, period=20, bar_type=BarType.K_5M, select=2, session_type=THType.RTH)
_ema60_5m = ema(symbol=self.trading_symbol, period=60, bar_type=BarType.K_5M, select=2, session_type=THType.RTH)
_h3 = bar_high(symbol=self.trading_symbol, bar_type=BarType.K_5M, select=3, session_type=THType.RTH)
_h4 = bar_high(symbol=self.trading_symbol, bar_type=BarType.K_5M, select=4, session_type=THType.RTH)
_h5 = bar_high(symbol=self.trading_symbol, bar_type=BarType.K_5M, select=5, session_type=THType.RTH)
_h6 = bar_high(symbol=self.trading_symbol, bar_type=BarType.K_5M, select=6, session_type=THType.RTH)
_h7 = bar_high(symbol=self.trading_symbol, bar_type=BarType.K_5M, select=7, session_type=THType.RTH)
_h8 = bar_high(symbol=self.trading_symbol, bar_type=BarType.K_5M, select=8, session_type=THType.RTH)
_breakout_high = max(_h3, _h4, _h5, _h6, _h7, _h8)
_rsi14 = rsi(symbol=self.trading_symbol, period=14, bar_type=BarType.K_5M, select=2, session_type=THType.RTH)
_boll_lower = boll_lower(symbol=self.trading_symbol, period=20, deviation=1.75, bar_type=BarType.K_5M, select=2, session_type=THType.RTH)
_boll_lower_panic = boll_lower(symbol=self.trading_symbol, period=20, deviation=2.0, bar_type=BarType.K_5M, select=2, session_type=THType.RTH)
_day_open = bar_open(symbol=self.trading_symbol, bar_type=BarType.K_DAY, select=1, session_type=THType.RTH)
_day_drop = 1.0 - _close / _day_open if _day_open > 0 else 0.0
_intraday_gain = _close / _day_open - 1.0 if _day_open > 0 else 99.0
_holding = position_holding_qty(symbol=self.trading_symbol)
if _holding <= 0 and self.TRADE_COUNT < self.MAX_TRADES_DAY:
    _mode = 0
    _ratio = 0.0
    if _trend_up and _s_rsi_d <= self.TREND_MAX_RSI and _extension <= self.TREND_MAX_EXTENSION and _intraday_gain <= self.TREND_MAX_INTRADAY_GAIN and _close > _ema20_5m and _ema20_5m > _ema60_5m and _close > _breakout_high:
        _mode = 1
        _ratio = self.TREND_POSITION
    elif not _trend_up and not _trend_down and _rsi14 < self.RANGE_RSI and _close < _boll_lower and _close > _previous_close:
        _mode = 2
        _ratio = self.RANGE_POSITION
    elif _trend_down and self.PANIC_POSITION > 0 and _rsi14 < self.PANIC_RSI and _close < _boll_lower_panic and _day_drop >= self.PANIC_DAY_DROP and _close > _previous_close:
        _mode = 3
        _ratio = self.PANIC_POSITION
    if _ratio > 0:
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
            self.POSITION_MODE_V2 = _mode
            self.PEAK_PRICE_V2 = _high
            self.TRADE_COUNT = self.TRADE_COUNT + 1
            alert(content='SOXL V3 ENTRY mode=' + str(_mode) + ' ratio=' + str(_ratio))
'''


def action_source() -> str:
    return risk_source() + "\n" + entry_source()


def _set_variable(variable, var_id: int, name: str, value: str, explicit: bool, description: str) -> None:
    variable.id = var_id
    variable.name = name
    variable.varType = 2
    variable.initValue.enType = 0
    variable.initValue.value = value
    variable.assignmentType = 1
    variable.isAddByCode = True
    variable.isExplicit = explicit
    variable.isCanModify = explicit
    variable.codeType = 3 if "." in value else 2
    variable.initDesc = description
    variable.isPercent = False


def build(template_path: Path, output_path: Path) -> dict:
    Strategy = _strategy_class()
    template_payload = template_path.read_bytes()
    strategy = Strategy.FromString(template_payload)
    if strategy.SerializeToString() != template_payload:
        raise ValueError("template protobuf round-trip is not byte-identical")
    if len(strategy.canvasCardList) != 92 or len(strategy.canvasLineList) != 91:
        raise ValueError("expected the verified 92-card/91-line Futu graph")

    start = next(card for card in strategy.canvasCardList if card.id == strategy.startCardId)
    action_template = next(card for card in strategy.canvasCardList if card.id == 1006)
    by_name = {variable.name: variable for variable in start.canvasCardStart.varList}
    for offset, (name, (value, explicit, description)) in enumerate(VARIABLES.items()):
        variable = by_name.get(name)
        if variable is None:
            variable = start.canvasCardStart.varList.add()
        _set_variable(variable, 66000 + offset, name, value, explicit, description)

    gate_template = next(card for card in strategy.canvasCardList if card.id == 1002)
    time_template = next(card for card in strategy.canvasCardList if card.id == 1005)

    risk_action = strategy.canvasCardList.add()
    risk_action.CopyFrom(action_template)
    risk_action.id = 1092
    risk_action.name = "V3风险与退出（无时区代码）"
    risk_action.positionX = 781
    risk_action.positionY = 46
    del risk_action.userCode.userCode[:]
    risk_action.userCode.userCode.extend(_code_lines(risk_source()))

    entry_time = strategy.canvasCardList.add()
    entry_time.CopyFrom(time_template)
    entry_time.id = 1093
    entry_time.name = "V3美东10:00至15:30"
    entry_time.positionX = 781
    entry_time.positionY = 246
    for condition in entry_time.canvasCardCondition.condGroup.conditionList:
        for value in condition.rightValue.paramValue:
            if value.value == "09:44:59":
                value.value = "09:59:59"

    entry_action = strategy.canvasCardList.add()
    entry_action.CopyFrom(action_template)
    entry_action.id = 1094
    entry_action.name = "V3动态状态入场"
    entry_action.positionX = 1031
    entry_action.positionY = 246
    del entry_action.userCode.userCode[:]
    entry_action.userCode.userCode.extend(_code_lines(entry_source()))

    range_flat_gate = strategy.canvasCardList.add()
    range_flat_gate.CopyFrom(gate_template)
    range_flat_gate.id = 1095
    range_flat_gate.name = "非趋势仓尾盘平仓"
    range_flat_gate.positionX = 1031
    range_flat_gate.positionY = 446
    range_flat_gate.canvasCardCondition.condition.leftValue.enType = 1
    range_flat_gate.canvasCardCondition.condition.leftValue.varName = "POSITION_MODE_V2"
    range_flat_gate.canvasCardCondition.condition.comparator = 1
    range_flat_gate.canvasCardCondition.condition.rightValue.enType = 0
    range_flat_gate.canvasCardCondition.condition.rightValue.value = "1"

    legacy_gate = strategy.canvasCardList.add()
    legacy_gate.CopyFrom(gate_template)
    legacy_gate.id = 1096
    legacy_gate.name = "旧V4分支关闭（保持画布连接）"
    legacy_gate.positionX = 781
    legacy_gate.positionY = 646
    legacy_gate.canvasCardCondition.condition.leftValue.enType = 1
    legacy_gate.canvasCardCondition.condition.leftValue.varName = "LEGACY_DISABLED_V2"
    legacy_gate.canvasCardCondition.condition.comparator = 1
    legacy_gate.canvasCardCondition.condition.rightValue.enType = 0
    legacy_gate.canvasCardCondition.condition.rightValue.value = "0"

    safe_terminal = strategy.canvasCardList.add()
    safe_terminal.CopyFrom(action_template)
    safe_terminal.id = 1097
    safe_terminal.name = "安全终点（不下单）"
    safe_terminal.positionX = 1281
    safe_terminal.positionY = 646
    del safe_terminal.userCode.userCode[:]
    safe_terminal.userCode.userCode.extend(_code_lines("_safe_terminal = 1\n"))

    # Preserve the verified native 09:30 reset and 15:45 force-flat branches.
    # Only the original V4 trading root is placed behind a permanently-false
    # gate; the active entry window is a clone of the known-good native card.
    for line in strategy.canvasLineList:
        if line.id == 2004:
            line.startCardId = legacy_gate.id
            line.endCardId = 1004
            line.condition = 1
        elif line.id == 2087:
            line.startCardId = 1086
            line.endCardId = range_flat_gate.id
            line.condition = 1

    line_template = strategy.canvasLineList[0]

    def add_line(line_id: int, source_card, target_card, condition: int = 0) -> None:
        line = strategy.canvasLineList.add()
        line.CopyFrom(line_template)
        line.id = line_id
        line.startCardId = source_card.id
        line.endCardId = target_card.id
        line.condition = condition
        line.startPositionX = source_card.positionX + source_card.width
        line.startPositionY = source_card.positionY + source_card.height / 2
        line.endPositionX = target_card.positionX
        line.endPositionY = target_card.positionY + target_card.height / 2

    original_root = next(card for card in strategy.canvasCardList if card.id == 1004)
    force_flat_holding = next(card for card in strategy.canvasCardList if card.id == 1087)
    add_line(2092, start, risk_action)
    add_line(2093, start, entry_time)
    add_line(2094, entry_time, entry_action, 1)
    add_line(2095, start, legacy_gate)
    add_line(2096, legacy_gate, safe_terminal, 2)
    add_line(2097, range_flat_gate, force_flat_holding, 1)
    add_line(2098, range_flat_gate, safe_terminal, 2)

    strategy.strategyName = "SOXL_REGIME_ADAPTIVE_V3_AGGRESSIVE"
    strategy.strategyId = 0
    strategy.cloudStrategyId = 0
    strategy.isAppSupplyQuantStrategy = False
    strategy.aiChatID = 0
    strategy.isGenByAIChat = False
    strategy.aiContentID = 0
    strategy.ClearField("userCode")

    payload = strategy.SerializeToString()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(payload)

    parsed = Strategy.FromString(payload)
    card_ids = [card.id for card in parsed.canvasCardList]
    line_ids = [line.id for line in parsed.canvasLineList]
    node_set = set(card_ids)
    adjacency: dict[int, list[int]] = {}
    for line in parsed.canvasLineList:
        adjacency.setdefault(line.startCardId, []).append(line.endCardId)
    reachable = {parsed.startCardId}
    frontier = [parsed.startCardId]
    while frontier:
        current = frontier.pop()
        for target in adjacency.get(current, []):
            if target not in reachable:
                reachable.add(target)
                frontier.append(target)
    parsed_risk = next(card for card in parsed.canvasCardList if card.id == 1092)
    parsed_entry = next(card for card in parsed.canvasCardList if card.id == 1094)
    risk_code = "".join(parsed_risk.userCode.userCode)
    entry_code = "".join(parsed_entry.userCode.userCode)
    source = risk_code + "\n" + entry_code
    parsed_entry_time = next(card for card in parsed.canvasCardList if card.id == 1093)
    entry_time_values = [
        value.value
        for condition in parsed_entry_time.canvasCardCondition.condGroup.conditionList
        for value in condition.rightValue.paramValue
    ]
    parsed_variables = {v.name: v.initValue.value for v in next(card for card in parsed.canvasCardList if card.id == parsed.startCardId).canvasCardStart.varList}
    checks = {
        "output_roundtrip_byte_identical": parsed.SerializeToString() == payload,
        "card_count_98": len(card_ids) == 98,
        "line_count_98": len(line_ids) == 98,
        "card_ids_unique": len(card_ids) == len(set(card_ids)),
        "line_ids_unique": len(line_ids) == len(set(line_ids)),
        "no_dangling_lines": all(line.startCardId in node_set and line.endCardId in node_set for line in parsed.canvasLineList),
        "all_cards_connected_to_start": reachable == node_set,
        "no_rule_card_is_terminal": all(
            card.canvasCardType != 2 or card.id in adjacency
            for card in parsed.canvasCardList
        ),
        "legacy_gate_is_permanently_false": (
            next(card for card in parsed.canvasCardList if card.id == 1096).canvasCardCondition.condition.leftValue.varName == "LEGACY_DISABLED_V2"
            and next(card for card in parsed.canvasCardList if card.id == 1096).canvasCardCondition.condition.comparator == 1
            and next(card for card in parsed.canvasCardList if card.id == 1096).canvasCardCondition.condition.rightValue.value == "0"
            and parsed_variables.get("LEGACY_DISABLED_V2") == "0"
        ),
        "legacy_false_branch_has_safe_action": any(
            line.startCardId == 1096
            and line.endCardId == 1097
            and line.condition == 2
            for line in parsed.canvasLineList
        ) and next(card for card in parsed.canvasCardList if card.id == 1097).canvasCardType == 3,
        "original_rule_1005_keeps_action": any(
            line.id == 2006
            and line.startCardId == 1005
            and line.endCardId == 1006
            and line.condition == 1
            for line in parsed.canvasLineList
        ),
        "no_unsupported_timezone_code": "device_time(" not in source and "TimeZone." not in source,
        "native_entry_window_1000_1530_et": "09:59:59" in entry_time_values
        and "15:30:01" in entry_time_values
        and "US_EASTERN" in entry_time_values,
        "native_reset_branch_preserved": any(
            line.id == 2001 and line.startCardId == parsed.startCardId and line.endCardId == 1001
            for line in parsed.canvasLineList
        ),
        "native_force_flat_branch_preserved": any(
            line.id == 2086 and line.startCardId == parsed.startCardId and line.endCardId == 1086
            for line in parsed.canvasLineList
        ),
        "force_flat_excludes_trend_mode": any(
            line.startCardId == 1086 and line.endCardId == 1095 and line.condition == 1
            for line in parsed.canvasLineList
        ) and any(
            line.startCardId == 1095 and line.endCardId == 1087 and line.condition == 1
            for line in parsed.canvasLineList
        ),
        "completed_daily_bars": "BarType.K_DAY, select=2" in source,
        "completed_5m_bars": "BarType.K_5M, select=2" in source and "BarType.K_5M, select=3" in source,
        "qqq_reference": 'Contract("US.QQQ")' in source,
        "separate_risk_and_entry_actions": "if _holding > 0:" in risk_code
        and "if _holding <= 0" in entry_code,
        "buy_and_sell_paths": "OrderSide.BUY" in entry_code and "OrderSide.SELL" in risk_code,
        "execution_enabled_for_backtest": parsed_variables.get("EXECUTION_ENABLED") == "1",
        "selected_parameters_embedded": all(parsed_variables.get(name) == value for name, (value, _, _) in VARIABLES.items()),
    }
    if not all(checks.values()):
        raise ValueError(f"generated quant failed validation: {checks}")

    manifest = {
        "path": str(output_path.resolve()),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "strategy_name": parsed.strategyName,
        "cards": len(card_ids),
        "lines": len(line_ids),
        "connected_card_ids": sorted(reachable),
        "template_path": str(template_path.resolve()),
        "template_sha256": hashlib.sha256(template_payload).hexdigest(),
        "checks": checks,
        "passed": True,
    }
    (output_path.parent / "quant_validation.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, default=Path.home() / "Downloads/超跌反弹SOXL_QQQ_DYNAMIC_REVERSION_V4.quant")
    parser.add_argument("--output", type=Path, default=Path("outputs/soxl_regime_adaptive_v3/SOXL_REGIME_ADAPTIVE_V3_AGGRESSIVE.quant"))
    args = parser.parse_args()
    print(json.dumps(build(args.template, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
