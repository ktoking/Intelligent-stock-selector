"""Causal daily portfolio engine. Local data only; no broker dependency."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd


DEFAULT_DATA = Path("outputs/watchlist_daily_trend_20260923")


@dataclass(frozen=True)
class Policy:
    name: str
    factor: str = "momentum_risk"
    trend: str = "slow"
    schedule: str = "twice"
    positions: int = 5
    weight_cap: float = .30
    annual_vol_target: float = .20
    rank_buffer: int = 10
    regime: str = "gate"
    trail: float = .15
    turnover_band: float = .025
    cooldown: int = 2

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("Policy requires a name")
        if self.factor not in ("momentum_risk", "momentum_blend", "residual_momentum",
                               "path_quality", "near_high"):
            raise ValueError("Unknown factor")
        if self.trend not in ("slow", "fast", "breakout"):
            raise ValueError("Unknown trend")
        if self.schedule not in ("weekly", "twice", "daily"):
            raise ValueError("Unknown schedule")
        if self.regime not in ("gate", "reduce"):
            raise ValueError("Unknown regime")
        if not (0 <= self.positions <= 20 and isinstance(self.positions, int)
                and self.rank_buffer >= self.positions and isinstance(self.rank_buffer, int)
                and isinstance(self.cooldown, int) and self.cooldown >= 1):
            raise ValueError("Invalid position count, hold rank, or cooldown")
        if not (0 < self.weight_cap <= 1 and 0 < self.annual_vol_target <= 10
                and 0 < self.trail < 1 and 0 <= self.turnover_band < 1):
            raise ValueError("Invalid risk parameters")


def catalog():
    """Hypotheses fixed before execution; no parameter optimizer."""
    configs = [Policy("trend_weekly20", schedule="weekly", weight_cap=.20, rank_buffer=5),
               Policy("trend_heavy28", annual_vol_target=.28, rank_buffer=5),
               Policy("trend_buffer28", annual_vol_target=.28),
               Policy("trend_buffer20"),
               Policy("trend_buffer15", annual_vol_target=.15)]
    for factor in ("momentum_blend", "residual_momentum", "path_quality", "near_high"):
        configs.append(Policy(factor + "20", factor=factor))
    configs.extend([Policy("breakout20", trend="breakout", factor="momentum_blend"),
                    Policy("fast_trend20", trend="fast", factor="momentum_blend"),
                    Policy("trend_reduce20", regime="reduce"),
                    Policy("blend_reduce20", factor="momentum_blend", regime="reduce"),
                    Policy("blend_weekly20", factor="momentum_blend", schedule="weekly"),
                    Policy("blend_buffer28", factor="momentum_blend", annual_vol_target=.28),
                    Policy("trend_daily20", schedule="daily")])
    return {p.name: p for p in configs}


@dataclass
class Panel:
    dates: pd.DatetimeIndex
    symbols: list[str]
    stock_mask: np.ndarray
    fields: dict[str, np.ndarray]
    metadata: dict

    @property
    def qqq(self):
        return self.symbols.index("US.QQQ")


def load_panel(directory=DEFAULT_DATA, asof=None):
    directory = Path(directory)
    universe = json.loads((directory / "universe.json").read_text())
    types = json.loads((directory / "security_types.json").read_text())
    frames, audits = {}, []
    for symbol in sorted(universe["symbols"]):
        path = directory / (symbol[3:].replace("/", "_") + ".pkl")
        if not path.exists():
            audits.append({"symbol": symbol, "reason": "missing_file"})
            continue
        frame = pd.read_pickle(path).copy()
        if frame.empty:
            audits.append({"symbol": symbol, "reason": "empty_file"})
            continue
        frame.index = pd.to_datetime(frame.date.astype(str), format="%Y%m%d")
        frame = frame.sort_index()
        if asof is not None:
            frame = frame.loc[:pd.Timestamp(asof)]
        if frame.empty:
            continue
        if frame.index.has_duplicates:
            raise ValueError(f"Duplicate bars: {symbol}")
        cols = ["open", "high", "low", "close", "volume", "turnover"]
        frame = frame[cols].astype(float)
        prices = frame[["open", "high", "low", "close"]]
        if (not np.isfinite(frame.to_numpy()).all() or (prices <= 0).any().any()
                or (frame[["volume", "turnover"]] < 0).any().any()
                or (frame.high < prices.max(axis=1)).any()
                or (frame.low > prices.min(axis=1)).any()):
            raise ValueError(f"Invalid OHLCV: {symbol}")
        frames[symbol] = frame
    if "US.QQQ" not in frames:
        raise ValueError("QQQ benchmark/calendar missing")
    dates, symbols = frames["US.QQQ"].index, sorted(frames)
    wide = {key: pd.DataFrame({s: f[key] for s, f in frames.items()}, index=dates)
            .reindex(columns=symbols) for key in ("open", "high", "low", "close", "volume", "turnover")}
    # No end-of-sample completeness filter. Eligibility is decided bar by bar.
    close, high = wide["close"], wide["high"]
    returns = close.pct_change(fill_method=None)
    features = dict(wide)
    features["returns"] = returns
    for span in (20, 50, 60, 100, 150, 200):
        features[f"ema{span}"] = close.ewm(span=span, adjust=False, min_periods=span).mean().where(close.notna())
    for lag in (21, 63, 126):
        features[f"mom{lag}"] = close / close.shift(lag) - 1
    features["vol20"] = returns.rolling(20, min_periods=20).std()
    features["liquid20"] = wide["turnover"].rolling(20, min_periods=20).mean()
    features["momentum_risk"] = features["mom63"] / features["vol20"]
    features["momentum_blend"] = (.5 * features["mom63"] + .5 * features["mom126"]) / features["vol20"]
    qret = returns["US.QQQ"]
    beta = returns.rolling(63, min_periods=63).cov(qret).div(qret.rolling(63).var(), axis=0)
    features["residual_momentum"] = (features["mom63"] - beta.mul(features["mom63"]["US.QQQ"], axis=0)) / features["vol20"]
    efficiency = (close - close.shift(63)).abs() / close.diff().abs().rolling(63).sum()
    features["path_quality"] = features["momentum_risk"] * efficiency
    features["near_high"] = close / high.rolling(252, min_periods=150).max()
    prior55 = high.shift().rolling(55, min_periods=55).max()
    features["breakout_recent"] = (close > prior55).rolling(10, min_periods=10).max()
    true_range = np.maximum(high - wide["low"], np.maximum((high - close.shift()).abs(),
                                                          (wide["low"] - close.shift()).abs()))
    features["atr14"] = true_range.rolling(14).mean()
    digest = sha256()
    for key in ("open", "high", "low", "close", "volume", "turnover"):
        digest.update(wide[key].to_csv(float_format="%.12g").encode())
    stock_mask = np.array([types.get(s) == "STOCK" for s in symbols])
    digest.update(json.dumps({s: types.get(s) for s in symbols}, sort_keys=True).encode())
    metadata = {"source": "Futunn REST autype=1 cached daily OHLCV and turnover",
                "start": str(dates[0].date()), "end": str(dates[-1].date()),
                "symbols": len(symbols), "ordinary_stocks": int(stock_mask.sum()),
                "input_sha256": digest.hexdigest(), "missing": audits,
                "limitations": ["current watchlist survivorship and selection bias",
                                "price returns; dividend cashflows excluded",
                                "all historical periods already inspected; retrospective research"]}
    return Panel(dates, symbols, stock_mask,
                 {key: frame.to_numpy(dtype=float) for key, frame in features.items()}, metadata)


def rank(panel: Panel, i: int, policy: Policy):
    f = panel.fields
    c, v = f["close"][i], f["vol20"][i]
    valid = panel.stock_mask & np.isfinite(v) & (v > 0) & (f["liquid20"][i] >= 5e6)
    if policy.trend == "fast":
        valid &= (c > f["ema20"][i]) & (f["ema20"][i] > f["ema60"][i])
    else:
        valid &= (c > f["ema50"][i]) & (f["ema50"][i] > f["ema150"][i])
    valid &= f["mom63"][i] > 0
    if policy.trend == "breakout":
        valid &= f["breakout_recent"][i] > 0
    score = f[policy.factor][i]
    valid &= np.isfinite(score)
    candidates = np.flatnonzero(valid)
    return sorted(candidates, key=lambda j: (-float(score[j]), panel.symbols[j]))


def scheduled(dates, i, schedule):
    if schedule == "daily":
        return True
    day = dates[i]
    previous = dates[i-1] if i else day - pd.Timedelta(days=7)
    first_of_week = day.isocalendar()[:2] != previous.isocalendar()[:2]
    if schedule == "weekly":
        return first_of_week
    # A Monday/Thursday holiday is moved to the next available session.
    return first_of_week or (day.weekday() >= 3 and previous.weekday() < 3)


def market_exposure(panel, previous, policy):
    f, q = panel.fields, panel.qqq
    close = f["close"][previous, q]
    risk_on = close > f["ema100"][previous, q]
    if policy.regime == "reduce":
        return 1.0 if risk_on else (.5 if close > f["ema200"][previous, q] else 0.0)
    return 1.0 if risk_on else 0.0


def weights_for(panel, previous, selected, policy, gross=1.0):
    """Inverse-vol capped weights, scaled by trailing covariance portfolio risk."""
    weights = np.zeros(len(panel.symbols))
    if not selected or gross <= 0:
        return weights
    inverse = 1 / panel.fields["vol20"][previous, selected]
    remaining, free = min(gross, len(selected) * policy.weight_cap), list(range(len(selected)))
    local = np.zeros(len(selected))
    while free and remaining > 1e-12:
        proposed = remaining * inverse[free] / inverse[free].sum()
        capped = [k for k, amount in zip(free, proposed) if amount > policy.weight_cap]
        if not capped:
            local[free] = proposed
            break
        for k in capped:
            local[k] = policy.weight_cap
            remaining -= policy.weight_cap
            free.remove(k)
    history = panel.fields["returns"][max(0, previous-59):previous+1, selected]
    if history.shape[0] < 20 or not np.isfinite(history).all():
        return weights
    covariance = np.atleast_2d(np.cov(history, rowvar=False)) * 252
    risk = float(np.sqrt(max(0.0, local @ covariance @ local)))
    if risk > policy.annual_vol_target:
        local *= policy.annual_vol_target / risk
    weights[selected] = local
    return weights


def metrics(curve, orders, capital=10000.):
    values = np.r_[capital, curve.equity.to_numpy()]
    returns = pd.Series(values).pct_change().dropna()
    vol = float(returns.std() * np.sqrt(252))
    return {"return_pct": float((values[-1]/capital-1)*100),
            "max_drawdown_pct": float((1-values/np.maximum.accumulate(values)).max()*100),
            "annualized_vol_pct": vol*100,
            "sharpe_zero_rf": float(returns.mean()*252/vol) if vol > 0 else None,
            "orders": len(orders), "cost_dollars": float(sum(o["cost"] for o in orders)),
            "avg_exposure_pct": float((1-curve.cash/curve.equity).mean()*100),
            "trading_days": len(curve)}


def simulate(panel: Panel, policy: Policy, start, end, bps=25, policy_changes=None,
             signal_lag=1):
    """Close decisions/next-open fills; cash-only; mark open positions at end.

    Policy changes are precomputed from past-only training periods. Daily stops
    block same-open rebuy; scheduled weights rebalance caps and retain winners
    within the wider hold rank. Exact execution prices remain model assumptions.
    """
    if not 0 <= bps < 1000 or signal_lag not in (1, 2):
        raise ValueError("Invalid friction or signal lag")
    indices = np.flatnonzero((panel.dates >= pd.Timestamp(start)) & (panel.dates <= pd.Timestamp(end)))
    if not len(indices) or indices[0] < signal_lag:
        raise ValueError("Need a non-empty interval with prior history")
    n = len(panel.symbols)
    qty = np.zeros(n, dtype=int)
    peak = np.zeros(n)
    blocked_until = np.zeros(n, dtype=int)
    last_marks = np.zeros(n)
    cash, fee, orders, curve, decisions = 10000., bps/10000., [], [], []
    prior_policy = policy.name
    prior_peak = peak.copy()
    stale_bars = 0
    f = panel.fields
    for i in indices:
        date, previous = panel.dates[i], i-signal_lag
        if policy_changes and str(date.date()) in policy_changes:
            policy = policy_changes[str(date.date())]
        change = policy.name != prior_policy
        prior_policy = policy.name
        opens, closes, prior_close = f["open"][i], f["close"][i], f["close"][previous]
        tradable = np.isfinite(opens) & (opens > 0)
        held = qty > 0
        close_valid = np.isfinite(closes)
        missing_held = held & (~tradable | ~close_valid)
        if missing_held.any():
            names = [panel.symbols[j] for j in np.flatnonzero(missing_held)]
            raise ValueError(f"Held asset has missing execution/valuation bar on {date.date()}: {names}; supply corporate-action/delisting or missing-data treatment before replay")
        stale_bars += int(np.sum(held & ~close_valid))
        marks_open = np.where(tradable, opens, last_marks)
        equity_open = float(cash + qty @ marks_open)
        signal_peak = peak if signal_lag == 1 else prior_peak
        stop = held & np.isfinite(prior_close) & (
            (prior_close < f["ema20" if policy.trend == "fast" else "ema50"][previous])
            | (prior_close < signal_peak * (1-policy.trail)))
        rebalance = change or scheduled(panel.dates, i, policy.schedule)
        regime_gross = market_exposure(panel, previous, policy)
        defensive = policy.regime == "reduce" and regime_gross < 1
        target_qty = qty.copy()
        target_qty[stop] = 0
        blocked_until[stop] = i + policy.cooldown
        target_weights = None
        selected = []
        if rebalance or defensive:
            ranks = rank(panel, previous, policy)
            # Stops and cooldown override ranks. Keep an existing valid winner
            # while it remains inside the hold buffer, then fill vacant slots.
            retained = [j for j in ranks[:policy.rank_buffer] if held[j] and not stop[j]]
            selected = retained[:policy.positions]
            if regime_gross > 0 and rebalance:
                selected += [j for j in ranks[:policy.positions] if j not in selected and not stop[j]
                             and blocked_until[j] <= i][:policy.positions-len(selected)]
            if policy.regime == "gate" and regime_gross == 0:
                # Gate blocks new buys; existing trend-valid holdings keep their
                # cash exposure until individual exits or market recovery.
                selected = [j for j in selected if held[j]]
                target_weights = weights_for(panel, previous, selected, policy)
                target_weights = np.minimum(target_weights, qty * marks_open / equity_open)
            else:
                target_weights = weights_for(panel, previous, selected, policy, regime_gross)
            if defensive and not rebalance:
                target_weights = np.minimum(target_weights, qty * marks_open / equity_open)
            for j in range(n):
                if not tradable[j]:
                    continue
                desired = int(equity_open * target_weights[j] / (opens[j]*(1+fee)))
                deviation = abs(desired-qty[j])*opens[j]/equity_open
                # Enforce caps at scheduled open. No trading just for rounding.
                cap_breach = qty[j]*opens[j]/equity_open > policy.weight_cap + .001
                if deviation >= policy.turnover_band or desired == 0 or qty[j] == 0 or cap_breach:
                    target_qty[j] = desired
            target_qty[stop] = 0
        for j in np.flatnonzero(tradable & (target_qty < qty)):
            sold = int(qty[j]-target_qty[j]); raw = float(opens[j]); cost = sold*raw*fee
            cash += sold*raw-cost
            qty[j] -= sold
            reason = "trend_or_trailing_exit" if stop[j] else "rebalance_or_risk"
            orders.append({"date":str(date.date()),"symbol":panel.symbols[j],"side":"SELL",
                           "qty":sold,"raw_price":raw,"price":raw*(1-fee),"cost":cost,
                           "reason":reason,"policy":policy.name})
            if qty[j] == 0:
                peak[j] = 0
        # Stable rank order determines who receives cash after sells.
        for j in selected:
            if not tradable[j] or target_qty[j] <= qty[j] or stop[j] or blocked_until[j] > i:
                continue
            raw = float(opens[j]); fill = raw*(1+fee)
            bought = min(int(target_qty[j]-qty[j]), int((cash+1e-9)/fill))
            if bought <= 0:
                continue
            if qty[j] == 0:
                peak[j] = raw
            qty[j] += bought
            cash -= bought*fill
            orders.append({"date":str(date.date()),"symbol":panel.symbols[j],"side":"BUY",
                           "qty":bought,"raw_price":raw,"price":fill,"cost":bought*raw*fee,
                           "reason":"rank_and_risk","policy":policy.name})
        if cash < -1e-7 or (qty < 0).any():
            raise AssertionError("Portfolio accounting violated")
        last_marks = np.where(close_valid, closes, last_marks)
        prior_peak = peak.copy()
        peak = np.where(qty > 0, np.maximum(peak, last_marks), 0)
        equity = float(cash + qty @ last_marks)
        curve.append({"date":str(date.date()),"equity":equity,"cash":cash,
                      "holdings":int((qty>0).sum()),"policy":policy.name,
                      "max_weight":float((qty*last_marks/equity).max())})
        decisions.append({"date":str(date.date()),"signal_date":str(panel.dates[previous].date()),
                          "policy":policy.name,"rebalance":bool(rebalance),
                          "regime_exposure":regime_gross})
    frame = pd.DataFrame(curve)
    stats = metrics(frame, orders)
    stats["stale_held_bars"] = stale_bars
    positions = [{"symbol":panel.symbols[j],"qty":int(qty[j]),"last_close":float(last_marks[j]),
                  "peak_close":float(peak[j]),"weight":float(qty[j]*last_marks[j]/frame.equity.iloc[-1])}
                 for j in np.flatnonzero(qty)]
    return {"metrics":stats,"curve":frame,"orders":orders,"positions":positions,"decisions":decisions}


def benchmark(panel, start, end, bps=25, symbol="US.QQQ"):
    indices = np.flatnonzero((panel.dates >= pd.Timestamp(start)) & (panel.dates <= pd.Timestamp(end)))
    j = panel.symbols.index(symbol)
    raw = float(panel.fields["open"][indices[0],j])
    fill = raw*(1+bps/10000)
    quantity = int(10000/fill)
    cash = 10000-quantity*fill
    curve = pd.DataFrame({"date":[str(panel.dates[i].date()) for i in indices],
                          "equity":cash+quantity*panel.fields["close"][indices,j],"cash":cash})
    orders = [{"date":curve.date.iloc[0],"symbol":symbol,"side":"BUY","qty":quantity,
               "raw_price":raw,"price":fill,"cost":quantity*(fill-raw)}]
    return {"metrics":metrics(curve, orders),"curve":curve,"orders":orders}
