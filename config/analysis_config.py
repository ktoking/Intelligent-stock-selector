"""
分析可调参数：技术指标、评分阈值、入场离场规则、诊断条件等。
通过环境变量覆盖，便于微调与 A/B 测试。
"""
import os


def _float_env(key: str, default: float) -> float:
    try:
        v = os.environ.get(key, "").strip()
        return float(v) if v else default
    except (TypeError, ValueError):
        return default


def _int_env(key: str, default: int) -> int:
    try:
        v = os.environ.get(key, "").strip()
        return int(v) if v else default
    except (TypeError, ValueError):
        return default


# ---------- 技术指标参数 ----------
# 均线周期
MA_PERIODS = {
    "ma5": _int_env("TECH_MA5", 5),
    "ma10": _int_env("TECH_MA10", 10),
    "ma20": _int_env("TECH_MA20", 20),
    "ma60": _int_env("TECH_MA60", 60),
}

# MACD
MACD_FAST = _int_env("TECH_MACD_FAST", 12)
MACD_SLOW = _int_env("TECH_MACD_SLOW", 26)
MACD_SIGNAL = _int_env("TECH_MACD_SIGNAL", 9)

# RSI
RSI_PERIOD = _int_env("TECH_RSI_PERIOD", 14)
RSI_OVERBOUGHT = _float_env("TECH_RSI_OVERBOUGHT", 70)
RSI_OVERSOLD = _float_env("TECH_RSI_OVERSOLD", 30)

# KDJ
KDJ_N = _int_env("TECH_KDJ_N", 9)
KDJ_OVERBOUGHT = _float_env("TECH_KDJ_OVERBOUGHT", 80)
KDJ_OVERSOLD = _float_env("TECH_KDJ_OVERSOLD", 20)

# 布林带
BB_PERIOD = _int_env("TECH_BB_PERIOD", 20)
BB_STD_MULT = _float_env("TECH_BB_STD_MULT", 2.0)

# ATR
ATR_PERIOD = _int_env("TECH_ATR_PERIOD", 14)

# 量能
VOLUME_MA_PERIOD = _int_env("TECH_VOLUME_MA_PERIOD", 20)

# 背离检测：回溯 K 线数（用于寻找近期高低点）
DIVERGENCE_LOOKBACK = _int_env("TECH_DIVERGENCE_LOOKBACK", 30)
# 背离检测：至少需要多少根 K 线
DIVERGENCE_MIN_BARS = _int_env("TECH_DIVERGENCE_MIN_BARS", 20)

# ---------- 入场/离场规则 ----------
# ATR 止损倍数（离场）
ATR_STOP_MULT = _float_env("TECH_ATR_STOP_MULT", 1.5)
# 放量突破量比阈值
VOLUME_BREAKOUT_RATIO = _float_env("TECH_VOLUME_BREAKOUT_RATIO", 1.5)

# ---------- 推荐记录条件 ----------
# 写入 recommendations.jsonl 的最低评分（若胜率偏低可改为 10 仅记录最强信号）
RECOMMEND_MIN_SCORE = _float_env("RECOMMEND_MIN_SCORE", 9)
# 写入 recommendations.jsonl 的动作
RECOMMEND_ACTION = os.environ.get("RECOMMEND_ACTION", "买入").strip()
# 震荡市（基准 N 日涨跌幅在熊牛阈值之间）时，仅记录不低于该分的买入；设为 9 表示与平时一致，设为 10 则震荡市只记 10 分（若几乎没有标的达 10 分可保持 9）
RECOMMEND_SIDEWAYS_MIN_SCORE = _float_env("RECOMMEND_SIDEWAYS_MIN_SCORE", 9.0)

# ---------- 诊断脚本 ----------
# 默认诊断回溯天数
DIAGNOSE_SINCE_DAYS = _int_env("DIAGNOSE_SINCE_DAYS", 90)
# 基准「牛市」判定：同期涨幅 >= 该值视为牛市环境
DIAGNOSE_BULL_THRESHOLD_PCT = _float_env("DIAGNOSE_BULL_THRESHOLD_PCT", 5.0)
# 基准「熊市」判定：同期涨幅 <= 该值视为熊市环境
DIAGNOSE_BEAR_THRESHOLD_PCT = _float_env("DIAGNOSE_BEAR_THRESHOLD_PCT", -5.0)

# ---------- 自我进化：回测胜率反哺评分 ----------
# 是否启用：当近期推荐胜率低于阈值时，在综合分析 prompt 中注入「请更保守给 9/10 分」
EVOLVE_ENABLED = os.environ.get("EVOLVE_ENABLED", "1").strip() in ("1", "true", "yes")
# 近期胜率低于该值（%）时触发收紧，默认 45
EVOLVE_WIN_RATE_THRESHOLD = _float_env("EVOLVE_WIN_RATE_THRESHOLD", 45.0)
