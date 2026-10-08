"""Package a SOXL main-wave candidate in the user's native Futu Canvas format."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from textwrap import indent

from scripts.moomoo_quant_binary import _code_lines, _strategy_class


NAME = "SOXL_MAIN_WAVE_TREND_V2_BACKTEST"
VARIABLES = {
    "WAVE_POSITION": "0.80",
    "WAVE_TRAIL": "0.12",
    "WAVE_STOP": "0.08",
    "WAVE_MAX_EXTENSION": "0.25",
    "WAVE_COOLDOWN_SESSIONS": "7",
    "WAVE_COOLDOWN_LEFT": "0",
    "WAVE_PEAK_CLOSE": "0",
    "WAVE_ENTRY_PRICE": "0",
    "WAVE_EXIT_PENDING": "0",
    "WAVE_EXIT_DAY_KEY": "0",
    "WAVE_LEGACY_DISABLED": "0",
    "WAVE_EXECUTION_ENABLED": "1",
}


def _regime_code() -> str:
    return '''_qqq_close = bar_close(symbol=self.ref_symbol, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
_qqq_e20 = ema(symbol=self.ref_symbol, period=20, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
_qqq_e60 = ema(symbol=self.ref_symbol, period=60, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
_soxl_close_d = bar_close(symbol=self.trading_symbol, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
_soxl_e20_d = ema(symbol=self.trading_symbol, period=20, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
_soxl_e60_d = ema(symbol=self.trading_symbol, period=60, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
_soxl_e20_old = ema(symbol=self.trading_symbol, period=20, bar_type=BarType.K_DAY, select=7, session_type=THType.RTH)
_wave_ready = _qqq_close is not None and _qqq_e20 is not None and _qqq_e60 is not None and _soxl_close_d is not None and _soxl_e20_d is not None and _soxl_e60_d is not None and _soxl_e20_old is not None and _qqq_close > 0 and _qqq_e20 > 0 and _qqq_e60 > 0 and _soxl_close_d > 0 and _soxl_e20_d > 0 and _soxl_e60_d > 0 and _soxl_e20_old > 0
_wave_up = _wave_ready and _qqq_e20 > _qqq_e60 and _qqq_close > _qqq_e20 and _soxl_close_d > _soxl_e20_d and _soxl_e20_d > _soxl_e60_d and _soxl_e20_d > _soxl_e20_old
'''


def risk_source() -> str:
    return '''# Main-wave hourly exit on a completed bar.
_holding = position_holding_qty(symbol=self.trading_symbol)
if _holding <= 0:
    self.WAVE_EXIT_PENDING = 0
    self.WAVE_PEAK_CLOSE = 0
else:
''' + indent(_regime_code(), "    ") + '''    _hour_close = bar_close(symbol=self.trading_symbol, bar_type=BarType.K_60M, select=2, session_type=THType.RTH)
    _hour_ready = _hour_close is not None and _hour_close > 0
    if _hour_ready and _hour_close > self.WAVE_PEAK_CLOSE:
        self.WAVE_PEAK_CLOSE = _hour_close
    _hard_stop = _hour_ready and self.WAVE_ENTRY_PRICE > 0 and _hour_close <= self.WAVE_ENTRY_PRICE * (1.0 - self.WAVE_STOP)
    _trail_stop = _hour_ready and self.WAVE_PEAK_CLOSE > 0 and _hour_close <= self.WAVE_PEAK_CLOSE * (1.0 - self.WAVE_TRAIL)
    _must_exit = (_wave_ready and not _wave_up) or _trail_stop or _hard_stop
    if _must_exit and self.WAVE_EXIT_PENDING == 0 and self.WAVE_EXECUTION_ENABLED >= 1:
        place_market(symbol=self.trading_symbol, qty=_holding, side=OrderSide.SELL)
        self.WAVE_EXIT_PENDING = 1
        self.WAVE_COOLDOWN_LEFT = self.WAVE_COOLDOWN_SESSIONS
        self.WAVE_EXIT_DAY_KEY = _soxl_close_d
        alert(content='SOXL主升浪退出：仓位=' + str(_holding))
'''


def entry_source() -> str:
    return "# Native time card gates new entries to 10:30-14:30 US Eastern.\n" + _regime_code() + '''_hour_close = bar_close(symbol=self.trading_symbol, bar_type=BarType.K_60M, select=2, session_type=THType.RTH)
_hour_e20 = ema(symbol=self.trading_symbol, period=20, bar_type=BarType.K_60M, select=2, session_type=THType.RTH)
_hour_e60 = ema(symbol=self.trading_symbol, period=60, bar_type=BarType.K_60M, select=2, session_type=THType.RTH)
_extension = _soxl_close_d / _soxl_e20_d - 1.0 if _wave_ready and _soxl_e20_d > 0 else 99.0
_hour_ready = _hour_close is not None and _hour_e20 is not None and _hour_e60 is not None and _hour_close > 0 and _hour_e20 > 0 and _hour_e60 > 0
_holding = position_holding_qty(symbol=self.trading_symbol)
if _holding <= 0 and self.TRADE_COUNT < 1 and self.WAVE_COOLDOWN_LEFT <= 0 and self.WAVE_EXIT_PENDING == 0:
    if _wave_up and _hour_ready and _extension <= self.WAVE_MAX_EXTENSION and _hour_close > _hour_e20 and _hour_e20 > _hour_e60:
        _net = net_asset(currency=Currency.USD)
        _price = current_price(symbol=self.trading_symbol, price_type=THType.RTH)
        _lot = lot_size(symbol=self.trading_symbol)
        if _price > 0 and _lot > 0:
            _target_qty = floor(_net * self.WAVE_POSITION / _price / _lot) * _lot
            _max_qty = max_qty_to_buy_on_cash(symbol=self.trading_symbol, order_type=OrdType.MKT, order_trade_session_type=TSType.RTH)
            if _target_qty > _max_qty:
                _target_qty = _max_qty
            if _target_qty > 0 and self.WAVE_EXECUTION_ENABLED >= 1:
                place_market(symbol=self.trading_symbol, qty=_target_qty, side=OrderSide.BUY)
                self.WAVE_ENTRY_PRICE = _price
                self.WAVE_PEAK_CLOSE = _hour_close
                self.TRADE_COUNT = self.TRADE_COUNT + 1
                alert(content='SOXL主升浪入场：目标比例=' + str(self.WAVE_POSITION))
'''


def reset_source() -> str:
    return '''# One native 09:30-09:35 time-card event per trading session.
_session_key = bar_close(symbol=self.trading_symbol, bar_type=BarType.K_DAY, select=2, session_type=THType.RTH)
if self.WAVE_COOLDOWN_LEFT > 0 and _session_key != self.WAVE_EXIT_DAY_KEY:
    self.WAVE_COOLDOWN_LEFT = self.WAVE_COOLDOWN_LEFT - 1
if self.WAVE_EXIT_PENDING > 0 and position_holding_qty(symbol=self.trading_symbol) > 0:
    self.WAVE_EXIT_PENDING = 0
'''


def build(template: Path, output: Path) -> dict:
    Strategy = _strategy_class()
    reference = template.read_bytes()
    strategy = Strategy.FromString(reference)
    if strategy.SerializeToString() != reference or strategy.strategyType != 1 or len(strategy.canvasCardList) != 92 or len(strategy.canvasLineList) != 91:
        raise ValueError("template is not the verified 92-card native Canvas export")
    by_id = {card.id: card for card in strategy.canvasCardList}
    start, action_template, rule_template, time_template = (by_id[i] for i in (strategy.startCardId, 1006, 1002, 1005))
    for variable in start.canvasCardStart.varList:
        if variable.isExplicit:
            variable.isExplicit = False
            variable.isCanModify = False
    for offset, (name, value) in enumerate(VARIABLES.items()):
        variable = start.canvasCardStart.varList.add()
        variable.id = 3030 + offset
        variable.name = name
        variable.varType = 2
        variable.initValue.enType = 0
        variable.initValue.value = value
        variable.assignmentType = 1
        variable.isAddByCode = True
        variable.isExplicit = name in {"WAVE_POSITION", "WAVE_TRAIL", "WAVE_STOP", "WAVE_MAX_EXTENSION", "WAVE_COOLDOWN_SESSIONS", "WAVE_EXECUTION_ENABLED"}
        variable.isCanModify = variable.isExplicit
        variable.codeType = 3 if "." in value else 2
        variable.initDesc = ""
        variable.isPercent = False

    def add_action(card_id: int, name: str, source: str, x: int, y: int):
        ast.parse(source)
        card = strategy.canvasCardList.add()
        card.CopyFrom(action_template)
        card.id, card.name, card.positionX, card.positionY = card_id, name, x, y
        del card.userCode.userCode[:]
        card.userCode.userCode.extend(_code_lines(source))
        return card

    risk = add_action(1092, "主升浪风险退出", risk_source(), 800, 40)
    entry_time = strategy.canvasCardList.add()
    entry_time.CopyFrom(time_template)
    entry_time.id, entry_time.name, entry_time.positionX, entry_time.positionY = 1093, "美东10:30至14:30开仓", 800, 240
    for condition in entry_time.canvasCardCondition.condGroup.conditionList:
        for value in condition.rightValue.paramValue:
            if value.value == "09:44:59":
                value.value = "10:29:59"
            elif value.value == "15:30:01":
                value.value = "14:30:01"
    entry = add_action(1094, "主升浪趋势入场", entry_source(), 1030, 240)
    legacy = strategy.canvasCardList.add()
    legacy.CopyFrom(rule_template)
    legacy.id, legacy.name, legacy.positionX, legacy.positionY = 1095, "旧V4分支关闭", 800, 490
    legacy.canvasCardCondition.condition.leftValue.enType = 1
    legacy.canvasCardCondition.condition.leftValue.varName = "WAVE_LEGACY_DISABLED"
    legacy.canvasCardCondition.condition.comparator = 1
    legacy.canvasCardCondition.condition.rightValue.enType = 0
    legacy.canvasCardCondition.condition.rightValue.value = "0"
    safe = add_action(1096, "安全终点", "_wave_safe_terminal = 1\n", 1030, 490)
    reset = add_action(1097, "冷却计数每日更新", reset_source(), 1030, 680)

    for line in strategy.canvasLineList:
        if line.id == 2004:
            line.startCardId, line.endCardId, line.condition = legacy.id, 1004, 1
            line.startPositionX = legacy.positionX + legacy.width
            line.startPositionY = legacy.positionY + legacy.height / 2
        elif line.id == 2087:
            line.startCardId, line.endCardId, line.condition = 1086, safe.id, 1
            line.endPositionX = safe.positionX
            line.endPositionY = safe.positionY + safe.height / 2
    line_template = strategy.canvasLineList[0]

    def connect(number: int, source, target, condition: int = 0):
        line = strategy.canvasLineList.add()
        line.CopyFrom(line_template)
        line.id, line.startCardId, line.endCardId, line.condition = number, source.id, target.id, condition
        line.startPositionX = source.positionX + source.width
        line.startPositionY = source.positionY + source.height / 2
        line.endPositionX = target.positionX
        line.endPositionY = target.positionY + target.height / 2

    connect(2092, start, risk)
    connect(2093, start, entry_time)
    connect(2094, entry_time, entry, 1)
    connect(2095, start, legacy)
    connect(2096, legacy, safe, 2)
    connect(2097, legacy, by_id[1087], 1)
    connect(2098, by_id[1001], reset, 1)

    strategy.strategyName = NAME
    strategy.strategyId = 0
    strategy.cloudStrategyId = 0
    strategy.isAppSupplyQuantStrategy = False
    strategy.aiChatID = 0
    strategy.isGenByAIChat = False
    strategy.aiContentID = 0
    strategy.ClearField("userCode")
    payload = strategy.SerializeToString()
    parsed = Strategy.FromString(payload)
    ids = [card.id for card in parsed.canvasCardList]
    edges = list(parsed.canvasLineList)
    reachable = {parsed.startCardId}
    frontier = [parsed.startCardId]
    while frontier:
        node = frontier.pop()
        for line in edges:
            if line.startCardId == node and line.endCardId not in reachable:
                reachable.add(line.endCardId)
                frontier.append(line.endCardId)
    codes = ["".join(card.userCode.userCode) for card in parsed.canvasCardList if card.isUserCode]
    start_variables = list(parsed.canvasCardList[0].canvasCardStart.varList)
    wave_parameters = [v for v in start_variables if v.name in VARIABLES]
    checks = {
        "native_canvas": parsed.strategyType == 1 and parsed.startCardId == 1,
        "protobuf_roundtrip": parsed.SerializeToString() == payload,
        "unique_cards": len(ids) == len(set(ids)),
        "unique_lines": len(edges) == len({line.id for line in edges}),
        "all_cards_reachable": reachable == set(ids),
        "no_dangling_lines": all(line.startCardId in ids and line.endCardId in ids for line in edges),
        "no_terminal_rule": all(card.canvasCardType != 2 or card.id in {line.startCardId for line in edges} for card in parsed.canvasCardList),
        "native_variable_id_range": len({v.id for v in start_variables}) == len(start_variables) and all(v.id <= 65535 for v in start_variables),
        "wave_parameters_match_native_numeric_format": len(wave_parameters) == len(VARIABLES)
        and all(v.initDesc == "" and v.initValue.enType == 0 and v.initValue.value == VARIABLES[v.name] for v in wave_parameters),
        "no_unsupported_time_api": all("device_time(" not in code and "TimeZone." not in code for code in codes),
        "known_good_order_call_shape": "time_in_force=" not in risk_source() + entry_source(),
        "reference_symbol_from_native_start": "ref_symbol" in str(parsed.canvasCardList[0].canvasCardStart),
        "buy_sell_present": any("OrderSide.BUY" in code for code in codes) and any("OrderSide.SELL" in code for code in codes),
        "entry_window": {"10:29:59", "14:30:01", "US_EASTERN"}.issubset({v.value for c in parsed.canvasCardList if c.id == 1093 for x in c.canvasCardCondition.condGroup.conditionList for v in x.rightValue.paramValue}),
        "execution_for_backtest": any(v.name == "WAVE_EXECUTION_ENABLED" and v.initValue.value == "1" for v in parsed.canvasCardList[0].canvasCardStart.varList),
    }
    for code in codes:
        ast.parse(code)
    if not all(checks.values()):
        raise ValueError(f"quant static validation failed: {checks}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(payload)
    return {"path": str(output), "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload),
            "cards": len(ids), "lines": len(edges), "template": str(template), "checks": checks}


if __name__ == "__main__":
    output = Path.home() / "Downloads/SOXL_MAIN_WAVE_TREND_V2_BACKTEST.quant"
    manifest = build(Path.home() / "Downloads/SOXL_NATIVE_V4_AGGRESSIVE_STABLE.quant", output)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
