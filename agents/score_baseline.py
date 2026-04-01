"""
定量评分基准线（0–100）：规则引擎汇总技术/估值/期权/动能，供 LLM 综合评分对齐参考。
不替代 LLM 判断，仅降低评分漂移；可通过 ANALYSIS_QUANT_BASELINE_ENABLED=0 关闭注入。
"""
from typing import Any, Dict, List, Optional, Tuple


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def compute_quant_baseline(
    technical: Dict[str, Any],
    fundamental: Dict[str, Any],
    options_summary: Dict[str, Any],
) -> Tuple[int, str]:
    """
    返回 (0–100 分, 一行说明)。
    中性起点 50；技术偏多加分、偏空减分；期权与估值微调。
    """
    reasons: List[str] = []
    score = 50.0

    if technical.get("ok"):
        if technical.get("daily_long_align"):
            score += 12
            reasons.append("多头排列+12")
        ms = technical.get("macd_summary") or {}
        if ms.get("golden_cross"):
            score += 5
            reasons.append("MACD金叉+5")
        elif ms.get("above_zero"):
            score += 3
            reasons.append("MACD零轴上+3")
        ks = technical.get("kdj_summary") or {}
        if ks.get("oversold"):
            score += 3
            reasons.append("KDJ超卖+3")
        if ks.get("overbought"):
            score -= 4
            reasons.append("KDJ超买-4")
        rs = technical.get("rsi_summary") or {}
        if rs.get("oversold"):
            score += 3
            reasons.append("RSI超卖+3")
        if rs.get("overbought"):
            score -= 4
            reasons.append("RSI超买-4")
        div = technical.get("divergence_summary") or {}
        if div.get("macd_bottom") or div.get("rsi_bottom"):
            score += 5
            reasons.append("底背离+5")
        if div.get("macd_top") or div.get("rsi_top"):
            score -= 6
            reasons.append("顶背离-6")
        vc = technical.get("volume_context") or {}
        vr = vc.get("volume_ratio")
        if vr is not None:
            if vr >= 1.5:
                score += 4
                reasons.append(f"量比{vr:.2f}+4")
            elif vr < 0.7:
                score -= 3
                reasons.append(f"量比{vr:.2f}-3")
        mom = technical.get("momentum_summary") or {}
        r20 = mom.get("return_20d_pct")
        if r20 is not None:
            if r20 > 5:
                score += 4
                reasons.append(f"20日涨幅{r20:.1f}%+4")
            elif r20 < -8:
                score -= 5
                reasons.append(f"20日跌幅{r20:.1f}%-5")
        dh = mom.get("dist_to_52w_high_pct")
        if dh is not None:
            if dh > -8:
                score += 3
                reasons.append(f"距52周高{dh:.1f}%+3")
            elif dh < -35:
                score -= 4
                reasons.append(f"距52周高{dh:.1f}%-4")
    else:
        score -= 5
        reasons.append("技术数据不足-5")

    chg = fundamental.get("change_pct")
    if chg is not None:
        if chg > 2:
            score += 3
            reasons.append(f"当日涨{chg:.1f}%+3")
        elif chg < -2:
            score -= 4
            reasons.append(f"当日跌{chg:.1f}%-4")

    tpe = fundamental.get("trailing_pe")
    if tpe is not None and tpe > 0:
        if tpe < 15:
            score += 4
            reasons.append(f"PE{tpe:.1f}偏低+4")
        elif tpe > 45:
            score -= 4
            reasons.append(f"PE{tpe:.1f}偏高-4")

    rec = (fundamental.get("recommendation") or "").strip()
    if rec in ("强烈买入", "买入"):
        score += 3
        reasons.append("机构偏多+3")
    elif rec in ("卖出", "强烈卖出"):
        score -= 4
        reasons.append("机构偏空-4")

    desc = (options_summary.get("description") or "").strip()
    if "偏多" in desc or "看涨" in desc:
        score += 4
        reasons.append("期权偏多+4")
    elif "偏空" in desc or "看跌" in desc:
        score -= 4
        reasons.append("期权偏空-4")

    final = int(round(_clamp(score, 0, 100)))
    note = "；".join(reasons) if reasons else "默认中性50"
    return final, note


def baseline_to_score10_hint(baseline_100: int) -> str:
    """将 0–100 映射到 1–10 档提示（供 Prompt 文字）。"""
    # 线性映射：0->1, 100->10
    s = max(1, min(10, round(baseline_100 / 100 * 9 + 1)))
    return f"定量参考对应约 {s}/10 档（仅供对齐，非强制）"
