#!/usr/bin/env python3
"""Build an importable moomoo ``.quant`` code-strategy container.

moomoo exports ``.quant`` as a protobuf message, not as a Python source file.
This module rewrites only the documented top-level Strategy fields while
preserving version metadata from a known-good, user-exported template.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory


@dataclass(frozen=True)
class WireField:
    number: int
    wire_type: int
    raw: bytes


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid protobuf varint")


def _encode_varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _iter_fields(data: bytes) -> list[WireField]:
    fields: list[WireField] = []
    offset = 0
    while offset < len(data):
        start = offset
        key, offset = _read_varint(data, offset)
        number, wire_type = key >> 3, key & 7
        if number == 0:
            raise ValueError("invalid protobuf field number 0")
        if wire_type == 0:
            _, offset = _read_varint(data, offset)
        elif wire_type == 1:
            offset += 8
        elif wire_type == 2:
            size, offset = _read_varint(data, offset)
            offset += size
        elif wire_type == 5:
            offset += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        if offset > len(data):
            raise ValueError("truncated protobuf field")
        fields.append(WireField(number, wire_type, data[start:offset]))
    return fields


def _field_varint(number: int, value: int) -> bytes:
    return _encode_varint((number << 3) | 0) + _encode_varint(value)


def _field_bytes(number: int, value: bytes) -> bytes:
    return _encode_varint((number << 3) | 2) + _encode_varint(len(value)) + value


def package_code_strategy(template: bytes, strategy_name: str, source: str) -> bytes:
    """Convert a valid canvas export into a valid code-strategy export.

    QuantCanvasProto.Strategy fields replaced here:
      2=startCardId, 8=canvasCardList, 9=canvasLineList, 12=strategyName,
      18=strategyType, 19=userCode.  All client-version metadata is retained.
    """
    if not strategy_name.strip():
        raise ValueError("strategy_name must not be empty")
    if "class Strategy(StrategyBase):" not in source:
        raise ValueError("source is not a moomoo code strategy")

    fields = _iter_fields(template)
    if not any(field.number == 8 for field in fields):
        raise ValueError("template has no canvas cards and is not a known-good canvas export")

    replaced = {2, 8, 9, 12, 18, 19}
    output = bytearray().join(field.raw for field in fields if field.number not in replaced)
    output += _field_varint(2, 0)
    output += _field_bytes(12, strategy_name.encode("utf-8"))
    output += _field_varint(18, 2)  # StrategyType_Code

    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines(keepends=True)
    if normalized and not lines:
        lines = [normalized]
    user_code = bytearray()
    for line in lines:
        if line.endswith("\n"):
            line = line[:-1] + "\r\n"
        user_code += _field_bytes(1, line.encode("utf-8"))
    output += _field_bytes(19, bytes(user_code))
    return bytes(output)


def _strategy_class(futu_app: Path = Path("/Applications/富途牛牛.app")):
    """Load QuantCanvasProto.Strategy from the schema embedded in moomoo."""
    dylib = futu_app / "Contents/Frameworks/libFTQuantServer.dylib"
    binary = dylib.read_bytes()
    marker = b"\x0a\x11QuantCanvas.proto\x12\x10QuantCanvasProto"
    start = 0
    while True:
        offset = binary.find(marker, start)
        if offset < 0:
            break
        candidate = binary[offset:offset + 12_000]
        best = None
        for size in range(1_000, len(candidate) + 1):
            descriptor = descriptor_pb2.FileDescriptorProto()
            try:
                descriptor.ParseFromString(candidate[:size])
            except Exception:
                continue
            names = {message.name for message in descriptor.message_type}
            if (
                descriptor.name == "QuantCanvas.proto"
                and descriptor.package == "QuantCanvasProto"
                and "Strategy" in names
                and "CanvasCard" in names
            ):
                best = descriptor
        if best is not None:
            pool = descriptor_pool.DescriptorPool()
            pool.Add(best)
            return message_factory.GetMessageClass(
                pool.FindMessageTypeByName("QuantCanvasProto.Strategy")
            )
        start = offset + len(marker)
    raise RuntimeError(f"QuantCanvas.proto descriptor not found in {dylib}")


def _code_lines(source: str) -> list[str]:
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines(keepends=True)
    result = []
    for line in lines:
        result.append(line[:-1] + "\r\n" if line.endswith("\n") else line)
    return result


def package_canvas_strategy(template: bytes, strategy_name: str, action_source: str) -> bytes:
    """Build a minimal native Canvas strategy from a known-good export.

    The client version table and indicator metadata stay byte-compatible with
    the user's export.  Only the strategy identity, graph, editable variables,
    and custom action body are changed.
    """
    if not strategy_name.strip():
        raise ValueError("strategy_name must not be empty")
    if "place_market" not in action_source or "Contract(\"US.SOXS\")" not in action_source:
        raise ValueError("action source does not contain the SOXL/SOXS switch implementation")

    Strategy = _strategy_class()
    strategy = Strategy.FromString(template)
    if strategy.strategyType != 1 or strategy.startCardId == 0:
        raise ValueError("template is not a native moomoo Canvas strategy")

    start_card = next(
        (card for card in strategy.canvasCardList if card.id == strategy.startCardId), None
    )
    action_card = next(
        (card for card in strategy.canvasCardList if card.isUserCode and card.canvasCardType == 3),
        None,
    )
    if start_card is None or action_card is None or not strategy.canvasLineList:
        raise ValueError("template does not contain the required Canvas nodes")

    start_copy = type(start_card)()
    start_copy.CopyFrom(start_card)
    action_copy = type(action_card)()
    action_copy.CopyFrom(action_card)
    line_copy = type(strategy.canvasLineList[0])()
    line_copy.CopyFrom(strategy.canvasLineList[0])

    variables = {
        "MOMENTUM_DAYS": (65400, "2", True, "QQQ completed-day momentum lookback"),
        "TREND_THRESHOLD": (65401, "0.02", True, "QQQ trend threshold"),
        "POSITION_RATIO": (65402, "0.95", True, "Maximum cash ratio"),
        "EXECUTION_ENABLED": (65403, "0", True, "0=shadow, 1=allow orders"),
        "LAST_RUN_DATE": (65404, "0", False, "Internal last execution date"),
        "CURRENT_SIGNAL": (65405, "0", False, "-1=SOXS, 0=cash, 1=SOXL"),
    }
    existing = {variable.name: variable for variable in start_copy.canvasCardStart.varList}
    for name, (var_id, value, explicit, desc) in variables.items():
        variable = existing.get(name)
        if variable is None:
            variable = start_copy.canvasCardStart.varList.add()
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
        variable.initDesc = desc
        variable.isPercent = False

    start_copy.positionX = 200.0
    start_copy.positionY = 200.0
    action_copy.id = 1006
    action_copy.name = "QQQ趋势切换 SOXL/SOXS（Shadow默认）"
    action_copy.positionX = 500.0
    action_copy.positionY = 200.0
    action_copy.isUserCode = True
    action_copy.aiChatId = 0
    action_copy.isGenByAiChat = False
    del action_copy.userCode.userCode[:]
    action_copy.userCode.userCode.extend(_code_lines(action_source))

    line_copy.id = 2001
    line_copy.startCardId = start_copy.id
    line_copy.endCardId = action_copy.id
    line_copy.condition = 0
    line_copy.startPositionX = start_copy.positionX + start_copy.width
    line_copy.startPositionY = start_copy.positionY + start_copy.height / 2
    line_copy.endPositionX = action_copy.positionX
    line_copy.endPositionY = action_copy.positionY + action_copy.height / 2

    del strategy.canvasCardList[:]
    strategy.canvasCardList.add().CopyFrom(start_copy)
    strategy.canvasCardList.add().CopyFrom(action_copy)
    del strategy.canvasLineList[:]
    strategy.canvasLineList.add().CopyFrom(line_copy)
    strategy.startCardId = start_copy.id
    strategy.strategyName = strategy_name
    strategy.strategyType = 1  # StrategyType_Canvas
    strategy.strategyId = 0
    strategy.cloudStrategyId = 0
    strategy.isAppSupplyQuantStrategy = False
    strategy.ClearField("userCode")
    strategy.width = 1_200.0
    strategy.height = 700.0
    strategy.viewOriginX = 0.0
    strategy.viewOriginY = 0.0
    strategy.aiChatID = 0
    strategy.isGenByAIChat = False
    strategy.aiContentID = 0
    return strategy.SerializeToString()


def inspect_top_level(data: bytes) -> dict[str, object]:
    """Return container-level facts used by tests and delivery validation."""
    fields = _iter_fields(data)
    numbers = [field.number for field in fields]
    strategy_types: list[int] = []
    for field in fields:
        if field.number == 18 and field.wire_type == 0:
            key_end = len(_encode_varint((18 << 3) | 0))
            value, _ = _read_varint(field.raw, key_end)
            strategy_types.append(value)
    return {
        "size": len(data),
        "has_canvas_cards": 8 in numbers,
        "has_canvas_lines": 9 in numbers,
        "has_user_code": 19 in numbers,
        "strategy_type": strategy_types[-1] if strategy_types else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--mode", choices=("canvas", "code"), default="canvas")
    args = parser.parse_args()
    packager = package_canvas_strategy if args.mode == "canvas" else package_code_strategy
    packaged = packager(args.template.read_bytes(), args.name, args.source.read_text(encoding="utf-8"))
    args.output.write_bytes(packaged)
    print(inspect_top_level(packaged))


if __name__ == "__main__":
    main()
