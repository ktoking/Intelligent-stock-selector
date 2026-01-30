"""
根据综合分析结果列表，生成与参考同风格的 HTML 报告（技术面/消息面/财报、筛选、排序）。
"""
import html
from datetime import datetime
from typing import List, Dict, Any


def _action_class(action: str) -> str:
    a = (action or "").strip()
    if "多头" in a or "加仓" in a:
        return "long"
    if "空头" in a or "减仓" in a or "禁止" in a:
        return "short"
    return "hold"


def _escape(s: Any) -> str:
    if s is None:
        return ""
    return html.escape(str(s).strip())


def _score_display(score: Any) -> str:
    try:
        f = float(score)
        if f == int(f):
            return str(int(f))
        return f"{f:.1f}"
    except Exception:
        return "—"


def _score_interpretation(score: Any) -> str:
    """将 1-5 评分映射为定性解读：观望/关注/可配置/偏多/强烈看好"""
    try:
        f = float(score)
        if f < 1.5:
            return "观望"
        if f < 2.5:
            return "关注"
        if f < 3.5:
            return "可配置"
        if f < 4.5:
            return "偏多"
        return "强烈看好"
    except Exception:
        return "—"


def build_report_html(cards: List[Dict[str, Any]], title: str = None, gen_time: str = None) -> str:
    if not cards:
        cards = []
    title = title or "美股优秀资产分析"
    gen_time = gen_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 收集筛选选项
    scores = sorted(set(_score_display(c.get("score")) for c in cards), reverse=True)
    actions = sorted(set((c.get("action") or "观望").strip() for c in cards))
    markets = sorted(set((c.get("market") or "美股").strip() for c in cards)) or ["美股"]

    score_options = "".join(
        f'<label class="filter-checkbox"><input type="checkbox" value="{s}" checked><span>{s}</span></label>'
        for s in scores
    )
    action_options = "".join(
        f'<label class="filter-checkbox"><input type="checkbox" value="{_escape(a)}" checked><span>{_escape(a)}</span></label>'
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
        action = (c.get("action") or "观望").strip()
        market = (c.get("market") or "美股").strip()
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
        sector = _escape(c.get("sector"))
        add_price = _escape(c.get("add_price"))
        reduce_price = _escape(c.get("reduce_price"))
        tech_entry_note = _escape(c.get("tech_entry_note") or "—")
        tech_exit_note = _escape(c.get("tech_exit_note") or "—")
        trend = _escape(c.get("trend_structure"))
        macd = _escape(c.get("macd_status"))
        kdj = _escape(c.get("kdj_status"))
        reason = _escape(c.get("analysis_reason"))
        action_cls = _action_class(action)
        long_align = "是" if c.get("daily_long_align") else "否"
        pe = _escape(c.get("pe"))
        put_call = _escape(c.get("put_call"))
        core_conclusion = _escape(c.get("core_conclusion"))
        score_label = _score_interpretation(c.get("score"))
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
        fd_summary = _escape(c.get("fundamental_deep_summary"))
        moat_summary = _escape(c.get("moat_summary"))
        peers_summary = _escape(c.get("peers_summary"))
        short_summary = _escape(c.get("short_summary"))
        narrative_summary = _escape(c.get("narrative_summary"))
        has_deep = fd_summary or moat_summary or comp_reason or recent_trend

        action_escaped = _escape(action)
        core_block = f'<div class="card-core-conclusion">{core_conclusion}</div>' if (core_conclusion and core_conclusion != "—") else ""
        last_date_val = last_date or "—"
        rec_val = recommendation or "—"
        next_earn_val = next_earnings or "—"
        deep_block = ""
        if has_deep:
            deep_block = f"""
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
                    <div class="card-section-content">{comp_reason}</div>
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
.no-results { text-align: center; padding: 80px 20px; color: #718096; font-size: 18px; background: linear-gradient(135deg, #ffffff 0%, #f8f9fa 100%); border-radius: 16px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12); font-weight: 500; }
.report-disclaimer { margin-top: 30px; padding: 20px 25px; background: rgba(255,255,255,0.9); border-radius: 12px; font-size: 13px; color: #6b7280; line-height: 1.6; text-align: center; border: 1px solid rgba(0,0,0,0.06); }"""

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
        document.getElementById('displayCount').textContent = totalCount;
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
