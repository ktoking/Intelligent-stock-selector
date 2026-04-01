"""data.us_movers_scan.eval_us_daily_mover 单测（合成 K 线）。"""
import pandas as pd

from data.us_movers_scan import eval_us_daily_mover


def _make_uptrend_df():
    """22 根：前 20 根横盘略涨，昨 100 今放量大涨且突破高。"""
    n = 22
    closes = [100.0] * 18 + [101.0, 102.0, 103.0, 104.0]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    vols = [1_000_000] * 20 + [1_200_000, 3_000_000]
    return pd.DataFrame(
        {"Open": closes, "High": highs, "Low": lows, "Close": closes, "Volume": vols}
    )


def test_eval_us_daily_mover_hits_default_rules():
    df = _make_uptrend_df()
    # 最后一日：昨 104 -> 今需大涨：把最后一根 close 拉高
    df = df.copy()
    df.loc[df.index[-1], "Close"] = 110.0
    df.loc[df.index[-1], "High"] = 111.0
    df.loc[df.index[-1], "Volume"] = 5_000_000
    r = eval_us_daily_mover(df)
    assert r is not None
    assert r["daily_pct"] > 3
    assert r["vol_ratio"] >= 1.5
    assert r["breakout_20d"] is True


def test_eval_us_daily_mover_rejects_low_volume():
    df = _make_uptrend_df().copy()
    df.loc[df.index[-1], "Close"] = 110.0
    df.loc[df.index[-1], "High"] = 111.0
    df.loc[df.index[-1], "Volume"] = 500_000
    assert eval_us_daily_mover(df, min_volume_ratio=2.0) is None


def test_eval_us_daily_mover_rejects_no_breakout():
    rows = []
    for _ in range(21):
        rows.append({"Open": 100.0, "High": 110.0, "Low": 99.0, "Close": 100.0, "Volume": 1_000_000.0})
    rows.append({"Open": 100.0, "High": 110.0, "Low": 99.0, "Close": 100.0, "Volume": 1_000_000.0})
    # 今日涨 5%、放量，但未突破前 20 日高 110（收盘 105）
    rows.append({"Open": 100.0, "High": 105.0, "Low": 99.0, "Close": 105.0, "Volume": 3_000_000.0})
    df = pd.DataFrame(rows)
    assert eval_us_daily_mover(df, min_daily_pct=3.0) is None
