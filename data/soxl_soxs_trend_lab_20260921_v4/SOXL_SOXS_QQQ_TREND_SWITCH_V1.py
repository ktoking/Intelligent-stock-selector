# SOXL_SOXS_QQQ_TREND_SWITCH_V1
# Generated from causal hourly research. Import into moomoo Algo as a code strategy.
# Bind the sole trigger symbol to US.QQQ. For D1 parameters, trigger at 09:35 ET;
# completed daily bars are read with select=2 so the forming day is excluded.
class Strategy(StrategyBase):
    def initialize(self):
        declare_strategy_type(strategy_type=AlgoStrategyType.SECURITY)
        self.trigger_symbols()
        self.custom_indicator()
        self.global_variables()

    def trigger_symbols(self):
        self.qqq = declare_trig_symbol()

    def custom_indicator(self):
        pass

    def global_variables(self):
        self.soxl = Contract("US.SOXL")
        self.soxs = Contract("US.SOXS")
        self.family = show_variable(2, GlobalType.INT, "1=EMA 2=Momentum 3=EMA+Momentum")
        self.fast = show_variable(0, GlobalType.INT, "QQQ EMA fast")
        self.slow = show_variable(0, GlobalType.INT, "QQQ EMA slow")
        self.horizon = show_variable(2, GlobalType.INT, "QQQ momentum hours")
        self.buffer = show_variable(0.020000, GlobalType.FLOAT, "Signal hysteresis")
        self.persistence = show_variable(1, GlobalType.INT, "Confirmation bars")
        self.neutral_cash = show_variable(True, GlobalType.BOOL, "Exit to cash in neutral regime")
        self.max_position_ratio = show_variable(0.95, GlobalType.FLOAT, "Maximum cash ratio")
        self.execution_enabled = show_variable(False, GlobalType.BOOL, "Enable orders; keep False for shadow")
        self.current_signal = 0
        self.candidate_signal = 0
        self.candidate_count = 0
        self.last_daily_signal_date = ""

    def _completed_daily_ema(self, period):
        lookback = max(period * 5, period + 10)
        value = bar_close(self.qqq, bar_type=BarType.D1, select=lookback + 1)
        alpha = 2.0 / (period + 1.0)
        for offset in range(lookback, 1, -1):
            price = bar_close(self.qqq, bar_type=BarType.D1, select=offset)
            value = alpha * price + (1.0 - alpha) * value
        return value

    def _raw_signal(self):
        select_now = 1 if False else 2
        current = bar_close(self.qqq, bar_type=BarType.D1, select=select_now)
        ema_signal = 0
        momentum_signal = 0
        if self.family == 1 or self.family == 3:
            if True:
                fast_value = self._completed_daily_ema(self.fast)
                slow_value = self._completed_daily_ema(self.slow)
            else:
                fast_value = ema(symbol=self.qqq, period=self.fast, bar_type=BarType.H1, session_type=THType.RTH)
                slow_value = ema(symbol=self.qqq, period=self.slow, bar_type=BarType.H1, session_type=THType.RTH)
            gap = fast_value / slow_value - 1 if slow_value else 0
            if gap > self.buffer:
                ema_signal = 1
            elif gap < -self.buffer:
                ema_signal = -1
        if self.family == 2 or self.family == 3:
            old = bar_close(self.qqq, bar_type=BarType.D1, select=self.horizon + select_now)
            change = current / old - 1 if old else 0
            if change > self.buffer:
                momentum_signal = 1
            elif change < -self.buffer:
                momentum_signal = -1
        if self.family == 1:
            return ema_signal
        if self.family == 2:
            return momentum_signal
        if ema_signal == momentum_signal:
            return ema_signal
        return 0

    def _update_signal(self, raw):
        if raw == 0 and not self.neutral_cash:
            self.candidate_signal = 0
            self.candidate_count = 0
            return
        if raw == self.current_signal:
            self.candidate_signal = raw
            self.candidate_count = 0
            return
        if raw == self.candidate_signal:
            self.candidate_count += 1
        else:
            self.candidate_signal = raw
            self.candidate_count = 1
        if self.candidate_count >= self.persistence:
            self.current_signal = raw
            self.candidate_count = 0

    def _sell_all(self, symbol):
        qty = position_holding_qty(symbol=symbol)
        if qty > 0:
            order_id = place_market(
                symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY
            )
            import time
            for _ in range(10):
                if order_status(order_id) == OrderStatus.FILLED_ALL:
                    return False
                time.sleep(0.2)
            return True
        return False

    def _buy_target(self, symbol):
        max_qty = max_qty_to_buy_on_cash(
            symbol=symbol, order_type=OrdType.MKT, order_trade_session_type=TSType.RTH
        )
        qty = int(max_qty * self.max_position_ratio)
        if qty >= 1:
            place_market(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)

    def handle_data(self):
        if True:
            now = device_time(TimeZone.DEVICE_TIME_ZONE)
            today = now.strftime("%Y-%m-%d")
            if self.last_daily_signal_date == today:
                return
            if now.hour < 9 or (now.hour == 9 and now.minute < 35) or now.hour >= 10:
                return
            self.last_daily_signal_date = today
        raw = self._raw_signal()
        self._update_signal(raw)
        if self.current_signal == 0:
            if self.execution_enabled:
                self._sell_all(self.soxl)
                self._sell_all(self.soxs)
            return
        target = self.soxl if self.current_signal > 0 else self.soxs
        other = self.soxs if self.current_signal > 0 else self.soxl
        print("QQQ trend signal=", self.current_signal, " target=", target)
        if not self.execution_enabled:
            return
        # Mutual exclusion: remove the opposite ETF before opening the target.
        if self._sell_all(other):
            return
        if position_holding_qty(symbol=target) <= 0:
            self._buy_target(target)
