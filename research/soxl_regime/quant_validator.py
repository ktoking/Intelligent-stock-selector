from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scripts.moomoo_quant_binary import _iter_fields, _strategy_class


def validate(path: Path) -> dict:
    payload = path.read_bytes()
    wire = _iter_fields(payload)
    Strategy = _strategy_class()
    strategy = Strategy.FromString(payload)
    node_ids = [card.id for card in strategy.canvasCardList]
    edge_ids = [edge.id for edge in strategy.canvasLineList]
    node_set = set(node_ids)
    dangling = [edge.id for edge in strategy.canvasLineList if edge.startCardId not in node_set or edge.endCardId not in node_set]
    action = next((card for card in strategy.canvasCardList if card.isUserCode), None)
    source = "".join(action.userCode.userCode) if action else ""
    api_binary = Path("/Applications/富途牛牛.app/Contents/Frameworks/libFTQuantCommon.dylib")
    api_bytes = api_binary.read_bytes() if api_binary.exists() else b""
    checks = {
        "wire_parse_to_eof": bool(wire), "strategy_name": strategy.strategyName,
        "strategy_type_canvas": strategy.strategyType == 1, "start_node_exists": strategy.startCardId in node_set,
        "node_ids_unique": len(node_ids) == len(node_set), "edge_ids_unique": len(edge_ids) == len(set(edge_ids)),
        "dangling_edges": dangling, "has_buy": "OrderSide.BUY" in source, "has_sell": "OrderSide.SELL" in source,
        "completed_5m_select_2": "BarType.K_5M, select=2" in source and "BarType.K_5M, select=3" in source,
        "rsi14_implemented": "rsi(symbol=self.trading_symbol, period=14" in source,
        "reset_includes_0930": "_now.minute >= 30" in source, "entry_includes_0945": "_now.minute >= 45" in source,
        "entry_includes_1530": "_now.minute <= 30" in source, "force_flat_includes_1545": "_now.minute >= 45" in source,
        "exit_before_entry": source.find("if _holding > 0:") < source.find("if _holding <= 0 and _exited == 0"),
        "execution_disabled_default": any(v.name == "EXECUTION_ENABLED" and v.initValue.value == "0" for v in strategy.canvasCardList[0].canvasCardStart.varList),
        "local_futu_api_symbols": all(token in api_bytes for token in [b"K_5M", b"K_30M", b"K_DAY", b"position_pl_ratio", b"rsi", b"boll_upper"]),
    }
    passed = all(value is True or (key == "dangling_edges" and value == []) or key == "strategy_name" for key, value in checks.items())
    template = Path.home() / "Downloads/SOXL_QQQ_DYNAMIC_REVERSION_V4_FIXED.quant"
    return {"path": str(path), "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload), "nodes": len(node_ids), "edges": len(edge_ids), "template_sha256": hashlib.sha256(template.read_bytes()).hexdigest() if template.exists() else None, "checks": checks, "passed": passed}


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("path", type=Path); args = parser.parse_args()
    print(json.dumps(validate(args.path), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
