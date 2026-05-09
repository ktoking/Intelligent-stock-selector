import pandas as pd

from agents.full_analysis import _apply_quality_guards
from agents.news import filter_relevant_news
from agents.technical import _compute_entry_exit_levels
from report.build_html import build_report_html


def test_filter_relevant_news_keeps_direct_nvda_and_drops_generic_ai_news():
    news_items = [
        {
            "title": "Highlights from Meta's earnings call",
            "summary": "Meta raised capex guidance and discussed AI investments.",
        },
        {
            "title": "Why Is NVIDIA (NVDA) Among The Best Growth Stocks To Buy And Hold In 2026?",
            "summary": "NVIDIA Corporation heads into late April with robust analyst backing.",
        },
        {
            "title": "Why data storage stocks are a key AI play",
            "summary": "Analysts see gains for Seagate and Western Digital.",
        },
        {
            "title": "BofA reiterates Buy on chip leader",
            "summary": "The analysts called Nvidia their top semiconductor pick.",
        },
    ]

    relevant, excluded = filter_relevant_news(
        "NVDA",
        news_items,
        company_name="NVIDIA Corporation",
        max_items=10,
    )

    assert [n["title"] for n in relevant] == [
        "Why Is NVIDIA (NVDA) Among The Best Growth Stocks To Buy And Hold In 2026?",
        "BofA reiterates Buy on chip leader",
    ]
    assert len(excluded) == 2


def test_entry_note_changes_when_price_is_already_above_ma20():
    close = pd.Series([190 + i * 0.7 for i in range(30)])
    high = pd.Series([x + 2 for x in close])
    high.iloc[-1] = 216.83
    low = pd.Series([x - 2 for x in close])

    levels = _compute_entry_exit_levels(
        close=close,
        high=high,
        low=low,
        ma5=212.0,
        ma20=194.98,
        ma60=186.17,
        price=209.25,
        is_daily=True,
    )

    assert "已站上 MA20 约 194.98" in levels["entry_note"]
    assert "重新站回 MA5 约 212.00" in levels["entry_note"]
    assert "突破近期高点约 216.83" in levels["entry_note"]
    assert "突破/站稳 MA20" not in levels["entry_note"]


def test_observe_card_uses_trigger_and_invalid_labels_instead_of_trade_labels():
    html = build_report_html(
        [
            {
                "ticker": "NVDA",
                "name": "NVIDIA Corporation",
                "score": 7,
                "action": "观察",
                "market": "美股",
                "current_price": "209.25",
                "change_pct": "-1.84%",
                "change_pct_raw": -1.84,
                "market_cap": "5.09万亿",
                "sector": "Semiconductors",
                "pe": "42.70",
                "put_call": "偏多",
                "daily_long_align": False,
                "tech_entry_note": "已站上 MA20 约 194.98，回踩 MA20 不破可考虑低吸",
                "tech_exit_note": "跌破 MA20 约 194.98 考虑减仓",
                "tech_status_one_line": "非多头排列",
                "trend_structure": "高位震荡",
                "macd_status": "零轴上方",
                "kdj_status": "中性",
                "analysis_reason": "等待确认。",
            }
        ],
        title="test",
    )

    assert "观察触发条件" in html
    assert "观察失效条件" in html
    assert ">加仓价格<" not in html
    assert ">减仓价格<" not in html


def test_quality_guard_downgrades_incomplete_llm_output():
    parsed = {
        "core_conclusion": "",
        "trend_structure": "—",
        "macd_status": "—",
        "kdj_status": "—",
        "analysis_reason": "—",
        "score": 8,
        "score_reason": "原始高分",
        "action": "买入",
        "add_price": "突破 MA20",
        "reduce_price": "跌破 MA20",
    }

    guarded = _apply_quality_guards(parsed, {"ok": True})

    assert guarded["action"] == "观察"
    assert guarded["score"] == 4
    assert guarded["add_price"] == "—"
    assert guarded["reduce_price"] == "—"
    assert guarded["data_quality_issue"] is True
    assert "LLM输出不完整" in guarded["trend_structure"]

