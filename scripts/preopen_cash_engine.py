"""Strict pre-open sizing stress: fixed quantities, cash reservation, explicit distributions.

Prices and distributions are in split-normalized units, not dividend-adjusted units.
This remains an idealized opening-auction fill model, not broker integration.
"""
import numpy as np

from scripts.equity_factor_lab import metrics


def plan_orders(prior_close, shares, cash, receivables, target, gap_limit, cost_bps):
    prior_close = np.asarray(prior_close, dtype=float)
    shares = np.asarray(shares, dtype=float)
    target = np.asarray(target, dtype=float)
    if not np.isfinite(target).all() or target.min() < 0 or target.sum() > 1+1e-10:
        raise ValueError("invalid target allocation")
    if gap_limit < 0 or cost_bps < 0 or cash < -1e-6 or receivables < 0:
        raise ValueError("invalid funding or cost assumptions")
    wanted = (cash+receivables+shares@prior_close)*target/prior_close
    sells = np.maximum(shares-wanted, 0)
    buys = np.maximum(wanted-shares, 0)
    limits = prior_close*(1+gap_limit)
    fee = cost_bps/10_000
    reserve = float((buys*limits).sum()*(1+fee))
    if reserve > 0:
        buys *= min(1., max(cash, 0.)/reserve)
    return sells, buys, limits


def settlement_sessions(day):
    # Trading-session approximation; banking-only holidays are not modeled.
    return 1 if day >= "2024-05-28" else 2 if day >= "2017-09-05" else 3


def simulate(p, targets, dividends, start, end, *, gap_limit=.05, cost_bps=7.,
             distribution_delay=20, initial_equity=100_000.):
    if start < 1 or end > len(targets) or end <= start or distribution_delay < 1:
        raise ValueError("invalid interval or distribution delay")
    for panel in (p["open"], p["close"], dividends):
        if not panel.index.equals(targets.index) or list(panel.columns) != list(targets.columns):
            raise ValueError("price/target/distribution alignment mismatch")
        if not np.isfinite(panel.to_numpy()).all():
            raise ValueError("invalid input prices/distributions")
    if (p["open"] <= 0).any().any() or (p["close"] <= 0).any().any() or (dividends < 0).any().any():
        raise ValueError("invalid input prices/distributions")
    opens, closes = p["open"].to_numpy(), p["close"].to_numpy()
    weights, distributions = targets.to_numpy(), dividends.to_numpy()
    symbols, dates = list(targets.columns), list(targets.index)
    shares = np.zeros(len(symbols))
    cash = previous = initial_equity
    pending, daily, orders = [], [], []
    fee = cost_bps/10_000
    dividend_total = rejected = planned_buys = 0
    for i in range(start, end):
        # Cash due today was already known before today's auction.
        cash += sum(amount for due, amount in pending if due <= i)
        pending = [(due, amount) for due, amount in pending if due > i]
        receivables = sum(amount for _, amount in pending)
        sell, buy, limit = plan_orders(closes[i-1], shares, cash, receivables,
                                      weights[i-1], gap_limit, cost_bps)
        entitlement = float(shares@distributions[i])
        if entitlement:
            pending.append((i+distribution_delay, entitlement))
            dividend_total += entitlement
        traded = costs = 0.
        for j, qty in enumerate(sell):
            if qty <= 1e-8:
                continue
            value = float(qty*opens[i,j])
            shares[j] -= qty
            pending.append((i+settlement_sessions(str(dates[i])), value*(1-fee)))
            traded += value
            costs += value*fee
            orders.append({"date": str(dates[i]), "signal_date": str(dates[i-1]),
                           "symbol": symbols[j], "side": "SELL", "quantity": float(qty),
                           "price": float(opens[i,j]), "cost": value*fee,
                           "status": "filled", "fill": "preplanned_market_on_open"})
        for j, qty in enumerate(buy):
            if qty <= 1e-8:
                continue
            planned_buys += 1
            filled = opens[i,j] <= limit[j]
            if filled:
                value = float(qty*opens[i,j])
                shares[j] += qty
                cash -= value*(1+fee)
                traded += value
                costs += value*fee
            else:
                rejected += 1
            orders.append({"date": str(dates[i]), "signal_date": str(dates[i-1]),
                           "symbol": symbols[j], "side": "BUY", "quantity": float(qty),
                           "limit": float(limit[j]), "price": float(opens[i,j]) if filled else None,
                           "cost": float(qty*opens[i,j]*fee) if filled else 0.,
                           "status": "filled" if filled else "expired_open_above_limit",
                           "fill": "preplanned_limit_on_open"})
        if i == end-1:
            for j, qty in enumerate(shares):
                if qty <= 1e-8:
                    continue
                value = float(qty*closes[i,j])
                pending.append((i+settlement_sessions(str(dates[i])), value*(1-fee)))
                traded += value
                costs += value*fee
                orders.append({"date": str(dates[i]), "symbol": symbols[j], "side": "SELL",
                    "quantity": float(qty), "price": float(closes[i,j]), "cost": value*fee,
                    "status": "filled", "fill": "predeclared_final_close"})
            shares[:] = 0
        receivables = sum(amount for _, amount in pending)
        equity = float(cash+receivables+shares@closes[i])
        if cash < -1e-6 or shares.min() < -1e-8 or equity <= 0:
            raise ValueError("cash or inventory constraint violated")
        daily.append({"date": str(dates[i]), "return": equity/previous-1, "equity": equity,
                      "cash": float(cash), "receivables": float(receivables),
                      "gross_weight": float(shares@closes[i]/equity),
                      "turnover": traded/previous, "cost": costs})
        previous = equity
    return {"metrics": metrics(daily), "daily": daily, "orders": orders,
            "planned_buy_orders": planned_buys, "expired_buy_orders": rejected,
            "dividend_entitlement_usd": dividend_total}
