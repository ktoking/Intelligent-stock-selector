import pandas as pd
from research.watchlist_daily_trend import OUT,load,features,simulate


def test_saved_daily_order_ledger_reconciles():
    curve=pd.read_csv(OUT/'breakout55_final_10bps_curve.csv')
    orders=pd.read_csv(OUT/'breakout55_final_10bps_orders.csv')
    # The saved run used the original 2024+ universe. A later data extension
    # can change today's completeness filter, so reconcile against its raw
    # historical prices instead of rerunning universe selection.
    symbols=orders.symbol.unique()
    closes={s:pd.read_pickle(OUT/(s[3:]+'.pkl')).set_index('date')['close'] for s in symbols}
    cash=10000.;qty={}
    for row in curve.itertuples(index=False):
        day=str(row.date)[:10]
        for order in orders[orders.date==day].itertuples(index=False):
            if order.side=='BUY':
                cash-=order.qty*order.price
                qty[order.symbol]=qty.get(order.symbol,0)+order.qty
            else:
                cash+=order.qty*order.price
                qty[order.symbol]-=order.qty
                if qty[order.symbol]==0:del qty[order.symbol]
        key=int(day.replace('-',''))
        equity=cash+sum(n*float(closes[s].loc[key]) for s,n in qty.items())
        assert abs(equity-row.equity)<1e-6
        assert abs(cash-row.cash)<1e-6
        assert len(qty)==row.holdings


def test_early_orders_unchanged_when_later_data_is_removed():
    full=load()
    keep={'US.QQQ','US.NVDA','US.AAPL'}
    full={s:f for s,f in full.items() if s in keep}
    short={s:f.loc[:'2026-06-30'] for s,f in full.items()}
    window=[d for d in short['US.QQQ'].index if pd.Timestamp('2026-06-01')<=d<=pd.Timestamp('2026-06-30')]
    a=simulate(full,features(full),window,'momentum63')[3]
    b=simulate(short,features(short),window,'momentum63')[3]
    assert a==b
