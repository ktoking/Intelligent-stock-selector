_now = device_time(TimeZone.ET)
_day_key = _now.year * 10000 + _now.month * 100 + _now.day
if self.LAST_RUN_DATE != _day_key and (_now.hour > 9 or (_now.hour == 9 and _now.minute >= 35)):
    self.LAST_RUN_DATE = _day_key
    _qqq_recent = bar_close(symbol=self.ref_symbol, bar_type=BarType.D1, select=2, session_type=THType.RTH)
    _qqq_old = bar_close(symbol=self.ref_symbol, bar_type=BarType.D1, select=int(self.MOMENTUM_DAYS) + 2, session_type=THType.RTH)
    _change = _qqq_recent / _qqq_old - 1 if _qqq_old > 0 else 0
    _signal = 0
    if _change > self.TREND_THRESHOLD:
        _signal = 1
    elif _change < -self.TREND_THRESHOLD:
        _signal = -1
    self.CURRENT_SIGNAL = _signal
    alert(content='QQQ 2D trend=' + str(_change) + ' signal=' + str(_signal) + ' (1=SOXL,-1=SOXS,0=CASH)')
    if self.EXECUTION_ENABLED >= 1:
        _soxl = self.trading_symbol
        _soxs = Contract("US.SOXS")
        _soxl_qty = position_holding_qty(symbol=_soxl)
        _soxs_qty = position_holding_qty(symbol=_soxs)
        if _signal > 0:
            if _soxs_qty > 0:
                place_market(symbol=_soxs, qty=_soxs_qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
            elif _soxl_qty <= 0:
                _lot = lot_size(symbol=_soxl)
                _max_qty = max_qty_to_buy_on_cash(symbol=_soxl, order_type=OrdType.MKT, order_trade_session_type=TSType.RTH)
                _buy_qty = floor(_max_qty * self.POSITION_RATIO / _lot) * _lot
                if _buy_qty > 0:
                    place_market(symbol=_soxl, qty=_buy_qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        elif _signal < 0:
            if _soxl_qty > 0:
                place_market(symbol=_soxl, qty=_soxl_qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
            elif _soxs_qty <= 0:
                _lot = lot_size(symbol=_soxs)
                _max_qty = max_qty_to_buy_on_cash(symbol=_soxs, order_type=OrdType.MKT, order_trade_session_type=TSType.RTH)
                _buy_qty = floor(_max_qty * self.POSITION_RATIO / _lot) * _lot
                if _buy_qty > 0:
                    place_market(symbol=_soxs, qty=_buy_qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        else:
            if _soxl_qty > 0:
                place_market(symbol=_soxl, qty=_soxl_qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
            if _soxs_qty > 0:
                place_market(symbol=_soxs, qty=_soxs_qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
