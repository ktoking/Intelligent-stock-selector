"""
根据综合分析结果列表，生成与参考同风格的 HTML 报告（技术面/消息面/财报、筛选、排序）。
"""
import re
import html as html_module
from datetime import datetime
from typing import List, Dict, Any, Optional


def _markdown_to_html(s: str) -> str:
    """将深度摘要中的简单 markdown（###、**、换行）转为 HTML，先转义防 XSS。"""
    if not s or not str(s).strip():
        return ""
    s = str(s).strip()
    s = html_module.escape(s)
    s = s.replace("\n", "<br>\n")
    # **粗体** -> <b>粗体</b>
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    # ### 标题 -> <h4>标题</h4>
    s = re.sub(r"(?m)^###\s*(.+)$", r"<h4 class=\"deep-heading\">\1</h4>", s)
    return s


# 板块英文→中文（yfinance sector/industry 常见值），未收录的保持原样
SECTOR_ZH = {
    "Technology": "科技",
    "Consumer Cyclical": "可选消费",
    "Consumer Defensive": "必选消费",
    "Healthcare": "医疗保健",
    "Financial Services": "金融",
    "Communication Services": "通信",
    "Industrials": "工业",
    "Basic Materials": "基础材料",
    "Energy": "能源",
    "Real Estate": "房地产",
    "Utilities": "公用事业",
}


def _sector_zh(s: str) -> str:
    """板块显示为中文，未收录则返回原值。"""
    if not s or not str(s).strip():
        return "—"
    key = (s or "").strip()
    return SECTOR_ZH.get(key, key)


def _action_class(action: str) -> str:
    """交易动作样式：买入=long，观察=hold，离场=short；兼容旧值多头/空头/观望等。"""
    a = (action or "").strip()
    if a == "买入" or "多头" in a or "加仓" in a or "轻仓" in a:
        return "long"
    if a == "离场" or "空头" in a or "减仓" in a or "禁止" in a:
        return "short"
    return "hold"


def _escape(s: Any) -> str:
    if s is None:
        return ""
    return html_module.escape(str(s).strip())


def _score_display(score: Any) -> str:
    try:
        f = float(score)
        if f == int(f):
            return str(int(f))
        return f"{f:.1f}"
    except Exception:
        return "—"


def _score_interpretation(score: Any) -> str:
    """将 10-1 评分映射为定性解读：10 最强、1 最弱"""
    try:
        f = float(score)
        if f >= 9:
            return "强烈看好"
        if f >= 7:
            return "偏多"
        if f >= 5:
            return "可配置"
        if f >= 3:
            return "关注"
        return "观望"
    except Exception:
        return "—"


def build_report_html(
    cards: List[Dict[str, Any]],
    title: str = None,
    gen_time: str = None,
    report_summary: str = None,
    backtest_rows: Optional[List[Dict[str, Any]]] = None,
    backtest_summary: Optional[Dict[str, Any]] = None,
) -> str:
    if not cards:
        cards = []
    title = title or "美股优秀资产分析"
    gen_time = gen_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary_block = ""
    if report_summary and str(report_summary).strip():
        summary_escaped = _escape(str(report_summary).strip()).replace("\n", "<br>\n")
        summary_block = f'<div class="report-summary"><div class="report-summary-title">报告总览</div><div class="report-summary-content">{summary_escaped}</div></div>'

    # 既往推荐表现（过去 N 天 9/10 分买入 → 多持有期、基准对比、风险指标）
    backtest_block = ""
    if backtest_rows and backtest_summary and backtest_summary.get("total_count", 0) > 0:
        since_days = backtest_summary.get("since_days", 30)
        total_count = backtest_summary.get("total_count", 0)
        win_count = backtest_summary.get("win_count", 0)
        win_rate_pct = backtest_summary.get("win_rate_pct", 0)
        avg_return_pct = backtest_summary.get("avg_return_pct", 0)
        # 多持有期
        total_1w = backtest_summary.get("total_1w", 0)
        win_rate_1w_pct = backtest_summary.get("win_rate_1w_pct", 0)
        avg_return_1w_pct = backtest_summary.get("avg_return_1w_pct", 0)
        total_1m = backtest_summary.get("total_1m", 0)
        win_rate_1m_pct = backtest_summary.get("win_rate_1m_pct", 0)
        avg_return_1m_pct = backtest_summary.get("avg_return_1m_pct", 0)
        total_2m = backtest_summary.get("total_2m", 0)
        win_rate_2m_pct = backtest_summary.get("win_rate_2m_pct", 0)
        avg_return_2m_pct = backtest_summary.get("avg_return_2m_pct", 0)
        total_3m = backtest_summary.get("total_3m", 0)
        win_rate_3m_pct = backtest_summary.get("win_rate_3m_pct", 0)
        avg_return_3m_pct = backtest_summary.get("avg_return_3m_pct", 0)
        # 基准
        bench_us = backtest_summary.get("benchmark_avg_us_pct")
        bench_cn = backtest_summary.get("benchmark_avg_cn_pct")
        bench_hk = backtest_summary.get("benchmark_avg_hk_pct")
        bench_hstech = backtest_summary.get("benchmark_avg_hstech_pct")
        bench_parts = []
        if bench_us is not None:
            bench_parts.append(f"标普500 同期 {bench_us:+.2f}%")
        if bench_cn is not None:
            bench_parts.append(f"沪深300 同期 {bench_cn:+.2f}%")
        if bench_hk is not None:
            bench_parts.append(f"恒生 同期 {bench_hk:+.2f}%")
        if bench_hstech is not None:
            bench_parts.append(f"恒科 同期 {bench_hstech:+.2f}%")
        bench_line = "；".join(bench_parts) if bench_parts else "—"
        # 风险与一眼看懂
        worst = backtest_summary.get("worst_return_pct")
        best = backtest_summary.get("best_return_pct")
        worst_str = f"{worst:+.2f}%" if worst is not None else "—"
        best_str = f"{best:+.2f}%" if best is not None else "—"
        triggered_exit_count = backtest_summary.get("triggered_exit_count", 0)
        recent_n = backtest_summary.get("recent_n", 0)
        recent_win_rate_pct = backtest_summary.get("recent_win_rate_pct", 0)
        d_up10 = backtest_summary.get("dist_up_10", 0)
        d_0_10 = backtest_summary.get("dist_0_10", 0)
        d_neg10_0 = backtest_summary.get("dist_neg10_0", 0)
        d_down10 = backtest_summary.get("dist_down_10", 0)
        dist_line = f"涨&gt;10% {d_up10} 只，0–10% {d_0_10} 只，-10%–0 {d_neg10_0} 只，&lt;-10% {d_down10} 只"

        summary_line = f"过去 {since_days} 天内共 {total_count} 条「9/10 分 买入」记录（同股只保留最早一条）。<br>至今：胜率 <b>{win_rate_pct}%</b>，平均收益 <b>{avg_return_pct:+.2f}%</b>；最近 {recent_n} 条胜率 <b>{recent_win_rate_pct}%</b>；触及减仓/离场价 <b>{triggered_exit_count}</b> 条。"
        if total_1w:
            summary_line += f" 持有1周（{total_1w} 条）：胜率 {win_rate_1w_pct}%，平均 {avg_return_1w_pct:+.2f}%。"
        if total_1m:
            summary_line += f" 持有1月（{total_1m} 条）：胜率 {win_rate_1m_pct}%，平均 {avg_return_1m_pct:+.2f}%。"
        if total_2m:
            summary_line += f" 持有2月（{total_2m} 条）：胜率 {win_rate_2m_pct}%，平均 {avg_return_2m_pct:+.2f}%。"
        if total_3m:
            summary_line += f" 持有3月（{total_3m} 条）：胜率 {win_rate_3m_pct}%，平均 {avg_return_3m_pct:+.2f}%。"
        summary_line += f"<br>基准同期：{bench_line}<br>最差/最佳单只：{worst_str} / {best_str}；收益分布：{dist_line}。"

        # 图表：胜率对比、收益分布（纯 CSS，无外部依赖）
        total_valid = max(total_count, 1)
        dist_total = d_up10 + d_0_10 + d_neg10_0 + d_down10
        dist_total = max(dist_total, 1)
        p_up10 = d_up10 / dist_total * 100
        p_0_10 = d_0_10 / dist_total * 100
        p_neg = d_neg10_0 / dist_total * 100
        p_down = d_down10 / dist_total * 100
        charts_html = f"""
            <div class="backtest-charts">
                <div class="backtest-chart-group">
                    <div class="backtest-chart-title">胜率对比</div>
                    <div class="backtest-chart-bars">
                        <div class="backtest-bar-row">
                            <span class="backtest-bar-label">至今</span>
                            <div class="backtest-bar-track"><div class="backtest-bar-fill {'positive' if win_rate_pct >= 50 else 'negative'}" style="width:{min(100, win_rate_pct)}%"></div></div>
                            <span class="backtest-bar-value {'positive' if win_rate_pct >= 50 else 'negative'}">{win_rate_pct}%</span>
                        </div>
                        <div class="backtest-bar-row">
                            <span class="backtest-bar-label">1周</span>
                            <div class="backtest-bar-track"><div class="backtest-bar-fill {'positive' if win_rate_1w_pct >= 50 else 'negative'}" style="width:{min(100, win_rate_1w_pct)}%"></div></div>
                            <span class="backtest-bar-value {'positive' if win_rate_1w_pct >= 50 else 'negative'}">{win_rate_1w_pct}%</span>
                        </div>
                        <div class="backtest-bar-row">
                            <span class="backtest-bar-label">1月</span>
                            <div class="backtest-bar-track"><div class="backtest-bar-fill {'positive' if win_rate_1m_pct >= 50 else 'negative'}" style="width:{min(100, win_rate_1m_pct)}%"></div></div>
                            <span class="backtest-bar-value {'positive' if win_rate_1m_pct >= 50 else 'negative'}">{win_rate_1m_pct}%</span>
                        </div>
                        <div class="backtest-bar-row">
                            <span class="backtest-bar-label">2月</span>
                            <div class="backtest-bar-track"><div class="backtest-bar-fill {'positive' if win_rate_2m_pct >= 50 else 'negative'}" style="width:{min(100, win_rate_2m_pct)}%"></div></div>
                            <span class="backtest-bar-value {'positive' if win_rate_2m_pct >= 50 else 'negative'}">{win_rate_2m_pct}%</span>
                        </div>
                        <div class="backtest-bar-row">
                            <span class="backtest-bar-label">3月</span>
                            <div class="backtest-bar-track"><div class="backtest-bar-fill {'positive' if win_rate_3m_pct >= 50 else 'negative'}" style="width:{min(100, win_rate_3m_pct)}%"></div></div>
                            <span class="backtest-bar-value {'positive' if win_rate_3m_pct >= 50 else 'negative'}">{win_rate_3m_pct}%</span>
                        </div>
                    </div>
                </div>
                <div class="backtest-chart-group">
                    <div class="backtest-chart-title">收益分布</div>
                    <div class="backtest-dist-bar">
                        <div class="backtest-dist-seg seg-up" style="width:{p_up10}%" title="涨&gt;10% {d_up10}只"></div>
                        <div class="backtest-dist-seg seg-0-10" style="width:{p_0_10}%" title="0-10% {d_0_10}只"></div>
                        <div class="backtest-dist-seg seg-neg" style="width:{p_neg}%" title="-10%-0 {d_neg10_0}只"></div>
                        <div class="backtest-dist-seg seg-down" style="width:{p_down}%" title="&lt;-10% {d_down10}只"></div>
                    </div>
                    <div class="backtest-dist-legend">
                        <span><i class="seg-dot seg-up"></i>涨&gt;10% {d_up10}</span>
                        <span><i class="seg-dot seg-0-10"></i>0-10% {d_0_10}</span>
                        <span><i class="seg-dot seg-neg"></i>-10%-0 {d_neg10_0}</span>
                        <span><i class="seg-dot seg-down"></i>&lt;-10% {d_down10}</span>
                    </div>
                </div>
                <div class="backtest-chart-group">
                    <div class="backtest-chart-title">基准同期</div>
                    <div class="backtest-bench-bars">
                        {"".join([
                            f'<div class="backtest-bench-row"><span class="backtest-bar-label">标普500</span><span class="backtest-bar-value {"positive" if bench_us >= 0 else "negative"}">{bench_us:+.2f}%</span></div>' if bench_us is not None else "",
                            f'<div class="backtest-bench-row"><span class="backtest-bar-label">沪深300</span><span class="backtest-bar-value {"positive" if bench_cn >= 0 else "negative"}">{bench_cn:+.2f}%</span></div>' if bench_cn is not None else "",
                            f'<div class="backtest-bench-row"><span class="backtest-bar-label">恒生</span><span class="backtest-bar-value {"positive" if bench_hk >= 0 else "negative"}">{bench_hk:+.2f}%</span></div>' if bench_hk is not None else "",
                            f'<div class="backtest-bench-row"><span class="backtest-bar-label">恒科</span><span class="backtest-bar-value {"positive" if bench_hstech >= 0 else "negative"}">{bench_hstech:+.2f}%</span></div>' if bench_hstech is not None else "",
                        ])}
                    </div>
                </div>
            </div>"""

        # 一眼看懂：核心指标卡片
        win_rate_class = "positive" if win_rate_pct >= 50 else "negative"
        recent_class = "positive" if recent_win_rate_pct >= 50 else "negative"
        at_a_glance = f"""
            <div class="backtest-at-a-glance">
                <div class="backtest-glance-item"><span class="backtest-glance-label">总条数</span><span class="backtest-glance-value">{total_count}</span></div>
                <div class="backtest-glance-item"><span class="backtest-glance-label">胜率</span><span class="backtest-glance-value {win_rate_class}">{win_rate_pct}%</span></div>
                <div class="backtest-glance-item"><span class="backtest-glance-label">平均收益</span><span class="backtest-glance-value {'positive' if avg_return_pct >= 0 else 'negative'}">{avg_return_pct:+.2f}%</span></div>
                <div class="backtest-glance-item"><span class="backtest-glance-label">最近{recent_n}条胜率</span><span class="backtest-glance-value {recent_class}">{recent_win_rate_pct}%</span></div>
                <div class="backtest-glance-item"><span class="backtest-glance-label">跌破卖出</span><span class="backtest-glance-value">{triggered_exit_count} 条</span></div>
                <div class="backtest-glance-item"><span class="backtest-glance-label">最差/最佳</span><span class="backtest-glance-value">{worst_str} / {best_str}</span></div>
            </div>"""

        # 表格行：前 BACKTEST_VISIBLE 条直接显示，其余折叠
        BACKTEST_VISIBLE = 15
        def _backtest_row(r):
            ticker = _escape(r.get("ticker") or "—")
            name = _escape(r.get("name") or ticker)
            rd = _escape((r.get("report_date") or "")[:10])
            score = _score_display(r.get("score"))
            price_then = r.get("price_at_report")
            price_now = r.get("current_price")
            ret = r.get("return_pct")
            ret_1w = r.get("return_1w_pct")
            ret_1m = r.get("return_1m_pct")
            ret_2m = r.get("return_2m_pct")
            ret_3m = r.get("return_3m_pct")
            bench_ret = r.get("benchmark_return_pct")
            days = r.get("holding_days")
            triggered = r.get("triggered_exit") is True
            price_then_str = f"{price_then:.2f}" if price_then is not None else "—"
            price_now_str = f"{price_now:.2f}" if price_now is not None else "—"
            def _ret_span(v):
                if v is None:
                    return "—"
                cls = "positive" if v >= 0 else "negative"
                return f'<span class="info-value {cls}">{v:+.2f}%</span>'
            ret_str = _ret_span(ret)
            ret_1w_str = _ret_span(ret_1w)
            ret_1m_str = _ret_span(ret_1m)
            ret_2m_str = _ret_span(ret_2m)
            ret_3m_str = _ret_span(ret_3m)
            bench_str = _ret_span(bench_ret) if bench_ret is not None else "—"
            days_str = str(days) if days is not None else "—"
            exit_badge = '<span class="triggered-exit-badge">卖出</span>' if triggered else "—"
            return f"<tr><td>{name}</td><td>{ticker}</td><td>{rd}</td><td>{score}</td><td>{price_then_str}</td><td>{price_now_str}</td><td>{ret_str}</td><td>{ret_1w_str}</td><td>{ret_1m_str}</td><td>{ret_2m_str}</td><td>{ret_3m_str}</td><td>{bench_str}</td><td>{days_str}</td><td>{exit_badge}</td></tr>"

        visible_rows = backtest_rows[:BACKTEST_VISIBLE]
        more_rows = backtest_rows[BACKTEST_VISIBLE:]
        table_body_visible = "\n".join(_backtest_row(r) for r in visible_rows)
        if more_rows:
            table_body_more = "\n".join(_backtest_row(r) for r in more_rows)
            thead = '<thead><tr><th>名称</th><th>代码</th><th>推荐日</th><th>评分</th><th>当时价</th><th>今日价</th><th>至今涨跌</th><th>1周</th><th>1月</th><th>2月</th><th>3月</th><th>基准同期</th><th>持有天数</th><th>跌破卖出</th></tr></thead>'
            table_inner = table_body_visible + f"""
            <tr class="backtest-expand-row"><td colspan="14" class="backtest-expand-cell">
                <details class="backtest-details">
                    <summary>展开更早 {len(more_rows)} 条</summary>
                    <table class="backtest-table backtest-table-nested">{thead}<tbody>{table_body_more}</tbody></table>
                </details>
            </tr>"""
        else:
            table_inner = table_body_visible

        # 历史股票表格：默认折叠，点击「查看历史股票明细」展开
        table_html = f"""
            <details class="backtest-table-details">
                <summary class="backtest-table-summary">查看历史股票明细（{total_count} 条）</summary>
                <table class="backtest-table">
                    <thead><tr><th>名称</th><th>代码</th><th>推荐日</th><th>评分</th><th>当时价</th><th>今日价</th><th>至今涨跌</th><th>1周</th><th>1月</th><th>2月</th><th>3月</th><th>基准同期</th><th>持有天数</th><th>跌破卖出</th></tr></thead>
                    <tbody>{table_inner}</tbody>
                </table>
            </details>"""

        backtest_block = f"""
        <div class="report-summary report-backtest">
            <div class="report-summary-title">既往推荐表现（过去 {since_days} 天 9/10 分 买入 → 多持有期、基准、风险）</div>
            <div class="report-summary-content">{summary_line}</div>
            {at_a_glance}
            {charts_html}
            {table_html}
        </div>"""

    # 收集筛选选项（交易动作已归一为 买入/观察/离场）
    scores = sorted(set(_score_display(c.get("score")) for c in cards), reverse=True)
    actions = sorted(set((c.get("action") or "观察").strip() for c in cards))
    markets = sorted(set((c.get("market") or "美股").strip() for c in cards)) or ["美股"]

    # 默认只勾选 9/10 分与买入，便于一眼看推荐标的
    score_options = "".join(
        f'<label class="filter-checkbox"><input type="checkbox" value="{s}" {"checked" if s in ("9", "10") else ""}><span>{s}</span></label>'
        for s in scores
    )
    action_options = "".join(
        f'<label class="filter-checkbox"><input type="checkbox" value="{_escape(a)}" {"checked" if a == "买入" else ""}><span>{_escape(a)}</span></label>'
        for a in actions
    )
    market_options = "".join(
        f'<label class="filter-checkbox"><input type="checkbox" value="{m}" checked><span>{m}</span></label>'
        for m in markets
    )
    has_direction_filter = any("direction_unchanged" in c for c in cards)
    direction_filter_html = ""
    if has_direction_filter:
        direction_filter_html = """
            <div class="control-group">
                <label>大方向筛选</label>
                <div class="filter-multi-select" id="directionFilter">
                    <label class="filter-checkbox"><input type="checkbox" id="directionUnchangedOnly" value="1"><span>仅显示大方向不变</span></label>
                </div>
            </div>"""

    card_html_list = []
    for c in cards:
        score_str = _score_display(c.get("score"))
        action = (c.get("action") or "观察").strip()
        market = (c.get("market") or "美股").strip()
        sector_raw = c.get("sector")
        sector_zh = _sector_zh(sector_raw)
        name = _escape(c.get("name") or c.get("ticker"))
        code = _escape(c.get("ticker"))
        price = _escape(c.get("current_price"))
        change_pct = c.get("change_pct") or "—"
        change_raw = c.get("change_pct_raw")
        if change_raw is not None:
            try:
                if float(change_raw) >= 0:
                    change_span = f'<span class="info-value positive">{_escape(change_pct)}</span>'
                else:
                    change_span = f'<span class="info-value negative">{_escape(change_pct)}</span>'
            except Exception:
                change_span = _escape(change_pct)
        else:
            change_span = _escape(change_pct)
        mcap = _escape(c.get("market_cap"))
        sector = _escape(sector_zh)
        add_price = _escape(c.get("add_price"))
        reduce_price = _escape(c.get("reduce_price"))
        tech_entry_note = _escape(c.get("tech_entry_note") or "—")
        tech_exit_note = _escape(c.get("tech_exit_note") or "—")
        trend = _escape(c.get("trend_structure"))
        macd = _escape(c.get("macd_status"))
        kdj = _escape(c.get("kdj_status"))
        tech_status_one_line = _escape(c.get("tech_status_one_line") or "—")
        atr_pct = c.get("atr_pct")
        atr_pct_str = f"{atr_pct:.2f}%" if atr_pct is not None else "—"
        reason = _escape(c.get("analysis_reason"))
        action_cls = _action_class(action)
        long_align = "是" if c.get("daily_long_align") else "否"
        pe = _escape(c.get("pe"))
        put_call = _escape(c.get("put_call"))
        core_conclusion = _escape(c.get("core_conclusion"))
        score_label = _score_interpretation(c.get("score"))
        score_reason = _escape(c.get("score_reason") or "—")
        last_date = _escape(c.get("last_date"))
        week52_high = c.get("week52_high")
        week52_low = c.get("week52_low")
        week52_str = "—"
        if week52_high is not None and week52_low is not None:
            week52_str = f"{week52_low:.2f} / {week52_high:.2f}"
        elif week52_high is not None:
            week52_str = f"— / {week52_high:.2f}"
        elif week52_low is not None:
            week52_str = f"{week52_low:.2f} / —"
        volume_ratio = c.get("volume_ratio")
        volume_ratio_str = f"{volume_ratio:.2f}" if volume_ratio is not None else "—"
        dividend_yield = c.get("dividend_yield")
        if dividend_yield is not None and dividend_yield > 0:
            div_str = f"{dividend_yield * 100:.2f}%" if dividend_yield < 0.1 else f"{dividend_yield:.2f}%"
        else:
            div_str = "—"
        recommendation = _escape(c.get("recommendation"))
        next_earnings = _escape(c.get("next_earnings"))
        interval_label = _escape(c.get("interval_label") or "日K")
        prepost_str = "是" if c.get("prepost") else "否"
        direction_unchanged = c.get("direction_unchanged", True)
        data_direction = "true" if direction_unchanged else "false"
        comp_reason = _escape(c.get("comparison_reason"))
        recent_trend = _escape(c.get("recent_trend"))
        fd_summary = _markdown_to_html(c.get("fundamental_deep_summary") or "")
        moat_summary = _markdown_to_html(c.get("moat_summary") or "")
        peers_summary = _markdown_to_html(c.get("peers_summary") or "")
        short_summary = _markdown_to_html(c.get("short_summary") or "")
        narrative_summary = _markdown_to_html(c.get("narrative_summary") or "")
        deep_disabled_reason = _escape(c.get("deep_disabled_reason"))
        deep_error = _escape(c.get("deep_error"))
        comp_reason_raw = (c.get("comparison_reason") or "").strip()
        recent_trend_raw = (c.get("recent_trend") or "").strip()
        no_history_note = (comp_reason_raw == "无历史对比" and (not recent_trend_raw or recent_trend_raw == "—"))
        has_deep = fd_summary or moat_summary or peers_summary or short_summary or narrative_summary or comp_reason or recent_trend or deep_disabled_reason or deep_error

        action_escaped = _escape(action)
        core_block = f'<div class="card-core-conclusion">{core_conclusion}</div>' if (core_conclusion and core_conclusion != "—") else ""
        last_date_val = last_date or "—"
        rec_val = recommendation or "—"
        next_earn_val = next_earnings or "—"
        deep_block = ""
        if has_deep:
            deep_hint = ""
            if deep_disabled_reason:
                deep_hint = f'<div class="card-section-content" style="background:#fff3cd;padding:8px 12px;border-radius:8px;margin-bottom:12px;">⚠️ 深度摘要未生成：{deep_disabled_reason}。请安装 <code>langchain-core</code>、<code>langchain-openai</code> 并配置 LLM 后使用 <code>?deep=1</code>。</div>'
            elif deep_error:
                deep_hint = f'<div class="card-section-content" style="background:#f8d7da;padding:8px 12px;border-radius:8px;margin-bottom:12px;">⚠️ 深度分析执行失败：{deep_error}</div>'
            deep_block = f"""
                {deep_hint}
                <div class="card-section">
                    <div class="card-section-title">基本面深度摘要</div>
                    <div class="card-section-content">{fd_summary}</div>
                </div>
                <div class="card-section">
                    <div class="card-section-title">护城河摘要</div>
                    <div class="card-section-content">{moat_summary}</div>
                </div>
                <div class="card-section">
                    <div class="card-section-title">同行对比摘要</div>
                    <div class="card-section-content">{peers_summary}</div>
                </div>
                <div class="card-section">
                    <div class="card-section-title">空头视角摘要</div>
                    <div class="card-section-content">{short_summary}</div>
                </div>
                <div class="card-section">
                    <div class="card-section-title">叙事变化摘要</div>
                    <div class="card-section-content">{narrative_summary}</div>
                </div>
                <div class="card-section">
                    <div class="card-section-title">与上次对比（大方向）</div>
                    <div class="card-section-content">{'<p class="deep-history-note" style="color:#6c757d;font-size:0.9em;margin-bottom:8px;">首次运行或尚无历史记录，再次运行 <code>?deep=1</code> 后将显示与上次对比。</p>' if no_history_note else ''}{comp_reason}</div>
                </div>
                <div class="card-section">
                    <div class="card-section-title">近期对比趋势</div>
                    <div class="card-section-content">{recent_trend}</div>
                </div>"""

        card_html_list.append(f'''
            <div class="card" data-score="{score_str}" data-action="{action_escaped}" data-market="{market}" data-name="{name}" data-code="{code}" data-direction-unchanged="{data_direction}">
                <div class="card-header">
                    <div class="card-title">
                        <h3>{name}</h3>
                        <div class="stock-code">{code}</div>
                    </div>
                    <div class="score-badge-wrap">
                        <div class="score-badge">{score_str}</div>
                        <div class="score-interpretation">{score_label}</div>
                        {f'<div class="score-reason">{score_reason}</div>' if score_reason and score_reason != "—" else ''}
                    </div>
                </div>
                {core_block}
                <div class="card-info">
                    <div class="info-item">
                        <div class="info-label">交易动作</div>
                        <div class="info-value"><span class="action-badge {action_cls}">{action_escaped}</span></div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">所属板块</div>
                        <div class="info-value">{sector}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">当前价格</div>
                        <div class="info-value">{price}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">涨跌幅</div>
                        <div class="info-value">{change_span}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">市值</div>
                        <div class="info-value">{mcap}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">市盈率PE</div>
                        <div class="info-value">{pe}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">日线多头排列</div>
                        <div class="info-value">{long_align}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">近日多空期权</div>
                        <div class="info-value">{put_call}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">市场</div>
                        <div class="info-value">{market}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">加仓价格</div>
                        <div class="info-value">{add_price}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">减仓价格</div>
                        <div class="info-value">{reduce_price}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">技术面入场参考</div>
                        <div class="info-value">{tech_entry_note}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">技术面离场参考</div>
                        <div class="info-value">{tech_exit_note}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">数据截至日</div>
                        <div class="info-value">{last_date_val}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">52周低/高</div>
                        <div class="info-value">{week52_str}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">量比</div>
                        <div class="info-value">{volume_ratio_str}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">ATR%</div>
                        <div class="info-value">{atr_pct_str}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">股息率</div>
                        <div class="info-value">{div_str}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">机构倾向</div>
                        <div class="info-value">{rec_val}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">下次财报日</div>
                        <div class="info-value">{next_earn_val}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">K线周期</div>
                        <div class="info-value">{interval_label}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">含盘前盘后</div>
                        <div class="info-value">{prepost_str}</div>
                    </div>
                </div>
                <div class="card-section">
                    <div class="card-section-title">技术面摘要</div>
                    <div class="card-section-content">{tech_status_one_line}</div>
                </div>
                <div class="card-section">
                    <div class="card-section-title">趋势结构</div>
                    <div class="card-section-content">{trend}</div>
                </div>
                <div class="card-section">
                    <div class="card-section-title">MACD状态</div>
                    <div class="card-section-content">{macd}</div>
                </div>
                <div class="card-section">
                    <div class="card-section-title">KDJ状态</div>
                    <div class="card-section-content">{kdj}</div>
                </div>
                <div class="card-section">
                    <div class="card-section-title">分析原因</div>
                    <div class="card-section-content">{reason}</div>
                </div>
                {deep_block}
            </div>''')

    cards_html = "\n".join(card_html_list)
    total = len(cards)

    # 内联 CSS 与参考一致（已精简保留关键样式）
    css = """* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 20px; min-height: 100vh; }
.container { max-width: 1600px; margin: 0 auto; }
.header { background: linear-gradient(135deg, #ffffff 0%, #f8f9fa 100%); border-radius: 16px; padding: 35px 40px; margin-bottom: 25px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12); border: 1px solid rgba(255, 255, 255, 0.8); }
.header h1 { color: #1a1a1a; font-size: 32px; margin-bottom: 12px; font-weight: 700; letter-spacing: -0.5px; }
.header-info { color: #6c757d; font-size: 15px; line-height: 1.8; }
.report-summary { background: linear-gradient(135deg, #eef2ff 0%, #e0e7ff 100%); border-radius: 16px; padding: 22px 28px; margin-bottom: 25px; border-left: 5px solid #667eea; box-shadow: 0 6px 20px rgba(102, 126, 234, 0.15); }
.report-summary-title { font-size: 15px; font-weight: 700; color: #3730a3; margin-bottom: 12px; letter-spacing: 0.3px; }
.report-summary-content { font-size: 15px; color: #4a5568; line-height: 1.75; }
.controls { background: linear-gradient(135deg, #ffffff 0%, #f8f9fa 100%); border-radius: 16px; padding: 25px 30px; margin-bottom: 25px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12); border: 1px solid rgba(255, 255, 255, 0.8); display: flex; flex-wrap: wrap; gap: 25px; align-items: flex-start; }
.control-group { display: flex; flex-direction: column; gap: 10px; min-width: 200px; }
.control-group label { font-weight: 700; color: #2d3748; font-size: 14px; text-transform: uppercase; letter-spacing: 0.5px; }
.filter-multi-select { display: flex; flex-wrap: wrap; gap: 10px; padding: 12px; border: 2px solid #e2e8f0; border-radius: 12px; background: #ffffff; min-width: 200px; max-width: 450px; transition: border-color 0.3s; }
.filter-multi-select:hover { border-color: #667eea; }
.filter-checkbox { display: flex; align-items: center; gap: 8px; padding: 8px 14px; background: #f7fafc; border-radius: 8px; cursor: pointer; transition: all 0.3s ease; border: 1px solid transparent; }
.filter-checkbox:hover { background: #edf2f7; transform: translateY(-2px); box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1); }
.filter-checkbox input[type="checkbox"] { cursor: pointer; width: 18px; height: 18px; accent-color: #667eea; }
.filter-checkbox input[type="checkbox"]:checked + span { font-weight: 700; color: #667eea; }
.filter-checkbox:has(input:checked) { background: #e6f2ff; border-color: #667eea; }
.sort-select { padding: 12px 18px; border: 2px solid #e2e8f0; border-radius: 12px; font-size: 14px; cursor: pointer; background: #ffffff; color: #2d3748; font-weight: 500; transition: all 0.3s; min-width: 180px; }
.sort-select:hover { border-color: #667eea; box-shadow: 0 4px 8px rgba(102, 126, 234, 0.15); }
.sort-select:focus { outline: none; border-color: #667eea; box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1); }
.stats { display: flex; gap: 15px; margin-left: auto; align-items: center; }
.stat-button { padding: 12px 24px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; border-radius: 12px; font-weight: 700; font-size: 15px; cursor: default; box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4); letter-spacing: 0.3px; }
.cards-container { display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 25px; }
@media (max-width: 1400px) { .cards-container { grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); } }
@media (max-width: 900px) { .cards-container { grid-template-columns: 1fr; } }
.card { background: linear-gradient(135deg, #ffffff 0%, #f8f9fa 100%); border-radius: 16px; padding: 25px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12); transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1); display: block; border: 1px solid rgba(255, 255, 255, 0.8); position: relative; overflow: hidden; }
.card::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 4px; background: linear-gradient(90deg, #667eea 0%, #764ba2 100%); transform: scaleX(0); transition: transform 0.4s; }
.card:hover::before { transform: scaleX(1); }
.card.hidden { display: none; }
.card:hover { transform: translateY(-8px) scale(1.02); box-shadow: 0 16px 40px rgba(0, 0, 0, 0.2); }
.card-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 20px; padding-bottom: 20px; border-bottom: 2px solid #e2e8f0; }
.card-title { flex: 1; }
.card-title h3 { color: #1a1a1a; font-size: 20px; margin-bottom: 8px; font-weight: 700; letter-spacing: -0.3px; }
.card-title .stock-code { color: #6c757d; font-size: 14px; font-weight: 500; background: #f7fafc; padding: 4px 10px; border-radius: 6px; display: inline-block; }
.score-badge-wrap { display: flex; flex-direction: column; align-items: center; gap: 6px; flex-shrink: 0; }
.score-badge { width: 60px; height: 60px; background: linear-gradient(135deg, #f6d365 0%, #fda085 100%); border-radius: 12px; display: flex; align-items: center; justify-content: center; font-size: 28px; font-weight: 800; color: white; box-shadow: 0 4px 12px rgba(246, 211, 101, 0.4); }
.score-interpretation { font-size: 12px; font-weight: 600; color: #667eea; letter-spacing: 0.5px; }
.score-reason { font-size: 11px; color: #6b7280; max-width: 180px; margin-top: 4px; line-height: 1.35; }
.card-core-conclusion { margin-bottom: 16px; padding: 12px 14px; background: linear-gradient(135deg, #eef2ff 0%, #e0e7ff 100%); border-radius: 10px; font-size: 14px; color: #3730a3; line-height: 1.5; border-left: 4px solid #667eea; }
.card-info { display: grid; grid-template-columns: repeat(2, 1fr); gap: 15px; margin-bottom: 20px; }
.info-item { display: flex; flex-direction: column; padding: 12px; background: #f7fafc; border-radius: 10px; transition: all 0.3s; }
.info-item:hover { background: #edf2f7; transform: translateY(-2px); }
.info-label { font-size: 11px; color: #718096; margin-bottom: 6px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
.info-value { font-size: 15px; color: #2d3748; font-weight: 600; }
.info-value.positive { color: #10b981; font-weight: 700; }
.info-value.negative { color: #ef4444; font-weight: 700; }
.action-badge { display: inline-block; padding: 6px 14px; border-radius: 20px; font-size: 12px; font-weight: 700; letter-spacing: 0.3px; }
.action-badge.long { background: linear-gradient(135deg, #dbeafe 0%, #bfdbfe 100%); color: #1e40af; box-shadow: 0 2px 8px rgba(30, 64, 175, 0.2); }
.action-badge.short { background: linear-gradient(135deg, #fee2e2 0%, #fecaca 100%); color: #991b1b; box-shadow: 0 2px 8px rgba(153, 27, 27, 0.2); }
.action-badge.hold { background: linear-gradient(135deg, #f3f4f6 0%, #e5e7eb 100%); color: #4b5563; box-shadow: 0 2px 8px rgba(75, 85, 99, 0.2); }
.card-section { margin-top: 18px; padding-top: 18px; border-top: 1px solid #e2e8f0; }
.card-section-title { font-size: 13px; font-weight: 700; color: #2d3748; margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.5px; }
.card-section-content { font-size: 14px; color: #4a5568; line-height: 1.7; background: #f7fafc; padding: 12px; border-radius: 8px; }
.card-section-content .deep-heading { font-size: 13px; font-weight: 700; color: #2d3748; margin: 12px 0 6px; display: block; }
.card-section-content .deep-heading:first-child { margin-top: 0; }
.no-results { text-align: center; padding: 80px 20px; color: #718096; font-size: 18px; background: linear-gradient(135deg, #ffffff 0%, #f8f9fa 100%); border-radius: 16px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12); font-weight: 500; }
.report-disclaimer { margin-top: 30px; padding: 20px 25px; background: rgba(255,255,255,0.9); border-radius: 12px; font-size: 13px; color: #6b7280; line-height: 1.6; text-align: center; border: 1px solid rgba(0,0,0,0.06); }
.report-backtest { border-left-color: #10b981; }
.backtest-at-a-glance { display: flex; flex-wrap: wrap; gap: 16px 24px; margin: 12px 0; padding: 12px 16px; background: #f8fafc; border-radius: 8px; border: 1px solid #e2e8f0; }
.backtest-glance-item { display: flex; flex-direction: column; gap: 2px; }
.backtest-glance-label { font-size: 12px; color: #64748b; }
.backtest-glance-value { font-size: 18px; font-weight: 700; color: #1e293b; }
.backtest-glance-value.positive { color: #10b981; }
.backtest-glance-value.negative { color: #ef4444; }
.triggered-exit-badge { display: inline-block; padding: 2px 8px; font-size: 12px; font-weight: 600; color: #fff; background: #dc2626; border-radius: 4px; }
.backtest-details { margin: 8px 0; }
.backtest-details summary { cursor: pointer; color: #3b82f6; font-size: 13px; }
.backtest-expand-row { vertical-align: top; }
.backtest-expand-cell { padding: 8px !important; border-bottom: none !important; }
.backtest-table-nested { margin-top: 8px; }
.backtest-table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 13px; }
.backtest-table th, .backtest-table td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #e2e8f0; }
.backtest-table th { background: #f7fafc; font-weight: 700; color: #2d3748; }
.backtest-table .info-value.positive { color: #10b981; }
.backtest-table .info-value.negative { color: #ef4444; }
.backtest-charts { display: flex; flex-wrap: wrap; gap: 24px; margin: 16px 0; }
.backtest-chart-group { flex: 1; min-width: 200px; padding: 12px 16px; background: #fff; border-radius: 10px; border: 1px solid #e2e8f0; }
.backtest-chart-title { font-size: 13px; font-weight: 700; color: #64748b; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
.backtest-chart-bars { display: flex; flex-direction: column; gap: 10px; }
.backtest-bar-row { display: flex; align-items: center; gap: 10px; }
.backtest-bar-label { font-size: 12px; color: #64748b; width: 36px; flex-shrink: 0; }
.backtest-bar-track { flex: 1; height: 10px; background: #e2e8f0; border-radius: 5px; overflow: hidden; }
.backtest-bar-fill { height: 100%; border-radius: 5px; transition: width 0.3s; }
.backtest-bar-fill.positive { background: linear-gradient(90deg, #34d399, #10b981); }
.backtest-bar-fill.negative { background: linear-gradient(90deg, #f87171, #ef4444); }
.backtest-bar-value { font-size: 13px; font-weight: 700; width: 44px; text-align: right; }
.backtest-dist-bar { display: flex; height: 24px; border-radius: 6px; overflow: hidden; margin-bottom: 8px; }
.backtest-dist-seg { min-width: 2px; transition: width 0.3s; }
.backtest-dist-seg.seg-up { background: #10b981; }
.backtest-dist-seg.seg-0-10 { background: #6ee7b7; }
.backtest-dist-seg.seg-neg { background: #fca5a5; }
.backtest-dist-seg.seg-down { background: #ef4444; }
.backtest-dist-legend { display: flex; flex-wrap: wrap; gap: 12px 16px; font-size: 12px; color: #64748b; }
.backtest-dist-legend .seg-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; vertical-align: middle; }
.backtest-dist-legend .seg-dot.seg-up { background: #10b981; }
.backtest-dist-legend .seg-dot.seg-0-10 { background: #6ee7b7; }
.backtest-dist-legend .seg-dot.seg-neg { background: #fca5a5; }
.backtest-dist-legend .seg-dot.seg-down { background: #ef4444; }
.backtest-bench-bars { display: flex; flex-direction: column; gap: 8px; }
.backtest-bench-row { display: flex; justify-content: space-between; align-items: center; }
.backtest-table-details { margin-top: 12px; }
.backtest-table-summary { cursor: pointer; padding: 10px 14px; background: #f1f5f9; border-radius: 8px; font-size: 14px; font-weight: 600; color: #475569; }
.backtest-table-summary:hover { background: #e2e8f0; color: #334155; }
.backtest-table-details[open] .backtest-table-summary { border-radius: 8px 8px 0 0; margin-bottom: 0; }
.backtest-table-details .backtest-table { margin-top: 0; }
"""

    script = f"""
        const cards = Array.from(document.querySelectorAll('.card'));
        const totalCount = {total};
        document.getElementById('totalCount').textContent = totalCount;
        function filterAndSort() {{
            const selectedScores = Array.from(document.querySelectorAll('#scoreFilter input:checked')).map(cb => cb.value);
            const selectedActions = Array.from(document.querySelectorAll('#actionFilter input:checked')).map(cb => cb.value);
            const selectedMarkets = Array.from(document.querySelectorAll('#marketFilter input:checked')).map(cb => cb.value);
            const sortBy = document.getElementById('sortSelect').value;
            let visibleCards = cards.filter(card => {{
                const score = card.getAttribute('data-score') || '';
                const action = card.getAttribute('data-action') || '';
                const market = card.getAttribute('data-market') || '';
                const dirOk = !document.getElementById('directionUnchangedOnly') || !document.getElementById('directionUnchangedOnly').checked || (card.getAttribute('data-direction-unchanged') === 'true');
                const matchScore = selectedScores.length === 0 || selectedScores.includes(score);
                const matchAction = selectedActions.length === 0 || selectedActions.includes(action);
                const matchMarket = selectedMarkets.length === 0 || selectedMarkets.includes(market);
                return matchScore && matchAction && matchMarket && dirOk;
            }});
            visibleCards.sort((a, b) => {{
                switch(sortBy) {{
                    case 'score-desc': return (parseFloat(b.getAttribute('data-score') || 0) - parseFloat(a.getAttribute('data-score') || 0));
                    case 'score-asc': return (parseFloat(a.getAttribute('data-score') || 0) - parseFloat(b.getAttribute('data-score') || 0));
                    case 'name-asc': return (a.getAttribute('data-name') || '').localeCompare(b.getAttribute('data-name') || '', 'zh-CN');
                    case 'name-desc': return (b.getAttribute('data-name') || '').localeCompare(a.getAttribute('data-name') || '', 'zh-CN');
                    case 'price-desc': return (parseFloat(b.querySelector('.info-item:nth-child(3) .info-value')?.textContent?.replace(/,/g,'') || 0) - parseFloat(a.querySelector('.info-item:nth-child(3) .info-value')?.textContent?.replace(/,/g,'') || 0));
                    case 'price-asc': return (parseFloat(a.querySelector('.info-item:nth-child(3) .info-value')?.textContent?.replace(/,/g,'') || 0) - parseFloat(b.querySelector('.info-item:nth-child(3) .info-value')?.textContent?.replace(/,/g,'') || 0));
                    default: return 0;
                }}
            }});
            cards.forEach(card => card.classList.add('hidden'));
            const container = document.getElementById('cardsContainer');
            const noResults = document.getElementById('noResults');
            if (visibleCards.length === 0) {{ noResults.style.display = 'block'; }} else {{
                noResults.style.display = 'none';
                visibleCards.forEach(card => {{ card.classList.remove('hidden'); container.appendChild(card); }});
            }}
            document.getElementById('displayCount').textContent = visibleCards.length;
        }}
        document.getElementById('sortSelect').addEventListener('change', filterAndSort);
        document.querySelectorAll('#scoreFilter input, #actionFilter input, #marketFilter input').forEach(cb => cb.addEventListener('change', filterAndSort));
        if (document.getElementById('directionUnchangedOnly')) document.getElementById('directionUnchangedOnly').addEventListener('change', filterAndSort);
        filterAndSort();  // 初始应用默认筛选（9/10分+买入）
    """

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>股票分析结果 - {_escape(title)}</title>
    <style>{css}</style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>{_escape(title)}</h1>
            <div class="header-info">
                <div>生成时间: {_escape(gen_time)}</div>
            </div>
        </div>
        {summary_block}
        {backtest_block}
        <div class="controls">
            <div class="control-group">
                <label>排序方式</label>
                <select class="sort-select" id="sortSelect">
                    <option value="score-desc">评分(高到低)</option>
                    <option value="score-asc">评分(低到高)</option>
                    <option value="name-asc">股票名称(A-Z)</option>
                    <option value="name-desc">股票名称(Z-A)</option>
                    <option value="price-desc">当前股价(高到低)</option>
                    <option value="price-asc">当前股价(低到高)</option>
                </select>
            </div>
            <div class="control-group">
                <label>评分筛选</label>
                <div class="filter-multi-select" id="scoreFilter">{score_options}</div>
            </div>
            <div class="control-group">
                <label>交易动作筛选</label>
                <div class="filter-multi-select" id="actionFilter">{action_options}</div>
            </div>
            <div class="control-group">
                <label>市场筛选</label>
                <div class="filter-multi-select" id="marketFilter">{market_options}</div>
            </div>
            {direction_filter_html}
            <div class="stats">
                <div class="stat-button">总计: <span id="totalCount">{total}</span></div>
                <div class="stat-button">显示: <span id="displayCount">{total}</span></div>
            </div>
        </div>
        <div class="cards-container" id="cardsContainer">
{cards_html}
        </div>
        <div class="no-results" id="noResults" style="display: none;">没有找到匹配的结果</div>
        <div class="report-disclaimer">
            本报告仅供参考，不构成任何投资建议。数据来源于公开信息，可能存在延迟或误差。投资有风险，决策需谨慎。
        </div>
    </div>
    <script>{script}</script>
</body>
</html>"""
