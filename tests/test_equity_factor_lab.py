import numpy as np
import pandas as pd
import pytest

from scripts.equity_factor_lab import Config, daily_data, factor_scores, metrics, run, simulate, target_weights


def panel(open_, close):
    index = pd.date_range("2024-01-01", periods=len(open_)).strftime("%Y-%m-%d")
    return {"open": pd.DataFrame({"A": open_}, index=index),
            "close": pd.DataFrame({"A": close}, index=index)}


def test_close_signal_cannot_capture_overnight_gap_before_entry():
    p = panel([100, 200, 200], [100, 200, 220])
    target = pd.DataFrame(1., index=p["open"].index, columns=["A"])
    result = simulate(p, target, 1, 3, cost_bps=0, rebalance=5)
    assert result["metrics"]["return_pct"] == pytest.approx(10)
    assert result["orders"][0]["price"] == 200
    assert result["orders"][0]["signal_date"] < result["orders"][0]["date"]


def test_costs_and_terminal_open_position_are_included():
    p = panel([100, 100, 100], [100, 100, 100])
    target = pd.DataFrame(1., index=p["open"].index, columns=["A"])
    result = simulate(p, target, 1, 3, cost_bps=100)
    assert result["daily"][-1]["equity"] == pytest.approx(100_000 / 1.01 * .99)
    assert result["daily"][-1]["gross_weight"] == 0
    assert result["metrics"]["cost_usd"] == pytest.approx(100_000 - result["daily"][-1]["equity"])
    assert all(d["cash"] >= -1e-6 for d in result["daily"])


def test_cash_days_remain_in_annualization_and_initial_loss_in_drawdown():
    rows = [{"return": -.1, "gross_weight": 1, "turnover": 1, "cost": 0},
            {"return": 0, "gross_weight": 0, "turnover": 0, "cost": 0}]
    m = metrics(rows)
    assert m["sessions"] == 2
    assert m["max_drawdown_close_pct"] == pytest.approx(10)
    assert m["annualized_pct"] == pytest.approx((.9 ** 126 - 1) * 100)


def test_future_price_changes_cannot_change_prior_factors_or_weights():
    rng = np.random.default_rng(2)
    names = ["A", "B", "C", "SPY", "QQQ"]
    c = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(.002, .01, (160, 5)), axis=0)), columns=names)
    p = {"close": c, "volume": pd.DataFrame(1000., index=c.index, columns=names)}
    altered = {k: v.copy() for k, v in p.items()}
    altered["close"].iloc[145:] *= 100
    a, b = factor_scores(p), factor_scores(altered)
    for name in a:
        pd.testing.assert_frame_equal(a[name].iloc[:145], b[name].iloc[:145])
        cfg = Config(name)
        wa, wb = target_weights(p, a, cfg), target_weights(altered, b, cfg)
        pd.testing.assert_frame_equal(wa.iloc[:145], wb.iloc[:145])
        assert (wa.sum(axis=1) <= 1 + 1e-10).all()
        assert (wa.max(axis=1) <= 1 / 3 + 1e-10).all()


def test_positions_drift_without_hidden_daily_rebalancing():
    p = panel([100, 100, 110, 110], [100, 110, 110, 121])
    target = pd.DataFrame(.5, index=p["open"].index, columns=["A"])
    result = simulate(p, target, 1, 4, cost_bps=0, rebalance=5)
    assert len(result["orders"]) == 2  # One entry and predeclared terminal liquidation.
    assert result["metrics"]["return_pct"] == pytest.approx(10.5)
    assert result["daily"][0]["gross_weight"] == pytest.approx(55_000 / 105_000)


def test_daily_loader_adjusts_ohlc_and_refuses_missing_session(tmp_path):
    frame = pd.DataFrame({"open": [100., 100., 100.], "high": [110.] * 3,
                          "low": [90.] * 3, "close": [100.] * 3,
                          "adj close": [50.] * 3, "volume": [1000.] * 3},
                         index=pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]))
    path = tmp_path / "daily.pkl"
    pd.to_pickle({"A": frame}, path)
    p, provenance = daily_data(path)
    assert p["open"].A.tolist() == [50.] * 3
    assert p["high"].A.tolist() == [55.] * 3
    assert provenance["missing_sessions"] == []
    pd.to_pickle({"A": frame.iloc[[0, 2]]}, path)
    with pytest.raises(ValueError, match="incomplete equity session"):
        daily_data(path)


def test_rebalance_sells_fund_buys_without_negative_cash():
    p = {"open": pd.DataFrame({"A": [100.] * 4, "B": [100.] * 4}),
         "close": pd.DataFrame({"A": [100.] * 4, "B": [100.] * 4})}
    w = pd.DataFrame({"A": [1., 0., 0., 0.], "B": [0., 1., 1., 1.]})
    result = simulate(p, w, 1, 4, cost_bps=10, rebalance=1)
    assert all(d["cash"] >= -1e-6 for d in result["daily"])
    assert [r["side"] for r in result["orders"] if r["date"] == "2"] == ["SELL", "BUY"]
    assert result["daily"][-1]["equity"] == pytest.approx(100_000 * (.999 / 1.001) ** 2)


def test_completed_run_cannot_be_overwritten(tmp_path):
    (tmp_path / "report.json").write_text("frozen result")
    with pytest.raises(FileExistsError, match="immutable"):
        run(tmp_path)
    assert (tmp_path / "report.json").read_text() == "frozen result"
