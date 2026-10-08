"""Array implementation of the validated daily cash-account research engine."""
import numpy as np

from scripts.equity_factor_lab import metrics


def simulate(p, targets, start, end, *, cost_bps=7., rebalance=1,
             initial_equity=100_000., record_orders=True):
    if start < 1 or end <= start or end > len(targets):
        raise ValueError("invalid simulation interval")
    if list(p["open"].columns) != list(targets.columns) or not p["open"].index.equals(targets.index):
        raise ValueError("targets and prices must share ordered dates and symbols")
    opx, clx, w = p["open"].to_numpy(), p["close"].to_numpy(), targets.to_numpy()
    if not np.isfinite(w[start-1:end]).all() or w[start-1:end].min() < 0 or w[start-1:end].sum(axis=1).max() > 1+1e-10:
        raise ValueError("invalid cash-account target weights")
    fee = cost_bps/10_000
    cash, previous = initial_equity, initial_equity
    shares = np.zeros(w.shape[1])
    days, names = list(targets.index), list(targets.columns)
    daily, orders = [], []
    for i in range(start,end):
        op, cl = opx[i], clx[i]
        traded = costs = 0.
        if (i-start) % rebalance == 0:
            wanted = (cash + shares@op)*w[i-1]/op
            sell = np.maximum(shares-wanted,0)
            cash += (sell*op).sum()*(1-fee)
            shares -= sell
            buy = np.maximum(wanted-shares,0)
            needed = (buy*op).sum()*(1+fee)
            buy *= min(1.,max(0.,cash)/needed) if needed > 0 else 1.
            cash -= (buy*op).sum()*(1+fee)
            shares += buy
            for side, qty in (("SELL",sell),("BUY",buy)):
                indexes = np.flatnonzero(qty>1e-8)
                notional = qty[indexes]*op[indexes]
                traded += float(notional.sum())
                costs += float(notional.sum())*fee
                if record_orders:
                    orders.extend({"date":str(days[i]),"signal_date":str(days[i-1]),"symbol":names[j],
                                   "side":side,"quantity":float(qty[j]),"price":float(op[j]),
                                   "notional":float(qty[j]*op[j]),"cost":float(qty[j]*op[j]*fee),
                                   "fill":"next_open"} for j in indexes)
        if i == end-1:
            indexes=np.flatnonzero(shares>1e-8)
            notional=shares[indexes]*cl[indexes]
            traded += float(notional.sum())
            costs += float(notional.sum())*fee
            if record_orders:
                orders.extend({"date":str(days[i]),"symbol":names[j],"side":"SELL",
                               "quantity":float(shares[j]),"price":float(cl[j]),
                               "notional":float(shares[j]*cl[j]),"cost":float(shares[j]*cl[j]*fee),
                               "fill":"predeclared_final_close"} for j in indexes)
            cash += shares@cl*(1-fee)
            shares[:] = 0
        equity=float(cash+shares@cl)
        if cash < -1e-6 or equity <= 0:
            raise ValueError("cash-account insolvency")
        daily.append({"date":str(days[i]),"return":equity/previous-1,"equity":equity,
                      "cash":float(cash),"gross_weight":float(shares@cl/equity),
                      "turnover":traded/previous,"cost":costs})
        previous=equity
    return {"daily":daily,"orders":orders,"metrics":metrics(daily)}
