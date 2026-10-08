from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scripts.moomoo_quant_binary import _strategy_class


# These are existing, user-modifiable variables in the known-good 92-card graph.
# No card payload, line, icon/layout field, version field, or unknown field is
# invented here.
RESEARCH_BALANCED_OVERRIDES = {
    "POSITION_L1": "0.20",
    "POSITION_L2": "0.25",
    "POSITION_L3": "0.25",
    "POSITION_L4": "0.25",
    "RSI_L1": "45",
    "RSI_L2": "35",
    "RSI_L3": "30",
    "RSI_L4": "25",
    "RSI_EXIT": "55",
    "BOLL_PERIOD": "20",
    "BOLL_L1_STD": "1.5",
    "BOLL_L2_STD": "1.75",
    "BOLL_L3_STD": "1.75",
    "BOLL_L4_STD": "2.0",
    "VOLUME_PERIOD": "20",
    "VOLUME_MULTIPLIER": "1.0",
    "LEVEL4_DAY_DROP": "0.06",
    "TAKE_PROFIT": "0.015",
    "STOP_LOSS": "0.02",
    "MAX_TRADES_DAY": "3",
}


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _graph_fingerprint(strategy) -> str:
    digest = hashlib.sha256()
    for card in strategy.canvasCardList:
        digest.update(card.SerializeToString())
    for line in strategy.canvasLineList:
        digest.update(line.SerializeToString())
    return digest.hexdigest()


def _variables(strategy) -> dict[str, str]:
    start = next(card for card in strategy.canvasCardList if card.id == strategy.startCardId)
    return {variable.name: variable.initValue.value for variable in start.canvasCardStart.varList}


def build_graph_variant(
    template_path: Path,
    output_path: Path,
    strategy_name: str,
    variable_overrides: dict[str, str] | None = None,
) -> dict:
    Strategy = _strategy_class()
    template_payload = template_path.read_bytes()
    template = Strategy.FromString(template_payload)

    # This is a hard safety gate: the local descriptor must preserve every byte
    # in this exact Futu export before it is allowed to write a derivative.
    if template.SerializeToString() != template_payload:
        raise ValueError("template protobuf round-trip is not byte-identical")
    if len(template.canvasCardList) != 92 or len(template.canvasLineList) != 91:
        raise ValueError("expected the verified 92-card/91-line Futu graph")

    strategy = Strategy.FromString(template_payload)
    original_card_ids = [card.id for card in strategy.canvasCardList]
    original_line_ids = [line.id for line in strategy.canvasLineList]
    original_lines = [line.SerializeToString() for line in strategy.canvasLineList]
    original_cards = {card.id: card.SerializeToString() for card in strategy.canvasCardList}
    original_variables = _variables(strategy)

    strategy.strategyName = strategy_name
    overrides = variable_overrides or {}
    start = next(card for card in strategy.canvasCardList if card.id == strategy.startCardId)
    by_name = {variable.name: variable for variable in start.canvasCardStart.varList}
    missing = sorted(set(overrides) - set(by_name))
    if missing:
        raise ValueError(f"template is missing variables: {missing}")
    for name, value in overrides.items():
        by_name[name].initValue.value = value

    payload = strategy.SerializeToString()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(payload)

    reparsed = Strategy.FromString(payload)
    card_ids = [card.id for card in reparsed.canvasCardList]
    line_ids = [line.id for line in reparsed.canvasLineList]
    node_set = set(card_ids)
    dangling = [
        line.id
        for line in reparsed.canvasLineList
        if line.startCardId not in node_set or line.endCardId not in node_set
    ]
    changed_card_ids = [
        card.id
        for card in reparsed.canvasCardList
        if card.SerializeToString() != original_cards[card.id]
    ]
    new_variables = _variables(reparsed)
    variable_diffs = {
        name: {"before": original_variables[name], "after": new_variables[name]}
        for name in original_variables
        if original_variables[name] != new_variables[name]
    }

    checks = {
        "template_roundtrip_byte_identical": True,
        "output_roundtrip_byte_identical": reparsed.SerializeToString() == payload,
        "card_count_92": len(card_ids) == 92,
        "line_count_91": len(line_ids) == 91,
        "card_ids_preserved": card_ids == original_card_ids,
        "line_ids_preserved": line_ids == original_line_ids,
        "all_lines_byte_identical": [line.SerializeToString() for line in reparsed.canvasLineList] == original_lines,
        "no_dangling_lines": not dangling,
        "card_ids_unique": len(card_ids) == len(set(card_ids)),
        "line_ids_unique": len(line_ids) == len(set(line_ids)),
        "only_start_card_changed": changed_card_ids in ([], [template.startCardId]),
        "only_requested_variables_changed": (
            set(variable_diffs).issubset(overrides)
            and all(new_variables[name] == value for name, value in overrides.items())
        ),
        "strategy_name_updated": reparsed.strategyName == strategy_name,
    }
    if not all(checks.values()):
        raise ValueError(f"generated graph failed validation: {checks}")

    return {
        "path": str(output_path.resolve()),
        "size": len(payload),
        "sha256": _sha256(payload),
        "template_path": str(template_path.resolve()),
        "template_sha256": _sha256(template_payload),
        "strategy_name": reparsed.strategyName,
        "cards": len(card_ids),
        "lines": len(line_ids),
        "indicators": [item.name for item in reparsed.indicatorInfoCollection.indicatorInfoList],
        "graph_fingerprint": _graph_fingerprint(reparsed),
        "changed_card_ids": changed_card_ids,
        "variable_diffs": variable_diffs,
        "checks": checks,
        "passed": True,
    }


def build_outputs(template_path: Path, output_dir: Path) -> dict:
    import_test = build_graph_variant(
        template_path=template_path,
        output_path=output_dir / "SOXL_GRAPH_IMPORT_TEST.quant",
        strategy_name="SOXL_GRAPH_IMPORT_TEST",
    )
    research_baseline = build_graph_variant(
        template_path=template_path,
        output_path=output_dir / "SOXL_DYNAMIC_REVERSION_V4_RESEARCH_BALANCED.quant",
        strategy_name="SOXL_DYNAMIC_REVERSION_V4_RESEARCH_BALANCED",
        variable_overrides=RESEARCH_BALANCED_OVERRIDES,
    )
    recent_best = build_graph_variant(
        template_path=template_path,
        output_path=output_dir / "SOXL_DYNAMIC_REVERSION_V4_RECENT_BEST.quant",
        strategy_name="SOXL_DYNAMIC_REVERSION_V4_RECENT_BEST",
    )
    manifest = {
        "warning": (
            "These files preserve the verified V4 graphical strategy. They are not a falsely-labelled "
            "implementation of the rejected SOXL_REGIME_SWITCH_V1 research model."
        ),
        "import_test": import_test,
        "research_baseline": research_baseline,
        "recent_best": recent_best,
    }
    manifest_path = output_dir / "graph_quant_validation.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--template",
        type=Path,
        default=Path.home() / "Downloads/超跌反弹SOXL_QQQ_DYNAMIC_REVERSION_V4.quant",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs/soxl_regime_switch_v1_graph"),
    )
    args = parser.parse_args()
    print(json.dumps(build_outputs(args.template, args.output_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
