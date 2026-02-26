# -*- coding: utf-8 -*-
"""
富途量化策略（独立脚本，与主项目无依赖）

精简版：仅做固定价位提醒。
- 当前价 <= 减仓价 → 推送卖出
- 当前价 <= 买入参考价（且>0）→ 推送可考虑买入

适合只盯一两个固定价位、不需 MA/ATR 等时使用。技术面多条件版见 strategy_technical.py。
"""


class Strategy(StrategyBase):

    def initialize(self):
        declare_strategy_type(AlgoStrategyType.SECURITY)
        self.trigger_symbols()
        self.custom_indicator()
        self.global_variables()

    def trigger_symbols(self):
        """驱动标的：填报告里关注的股票代码（如 AAPL.US、00700.HK）"""
        self.驱动标的1 = declare_trig_symbol()
        # 若平台支持多标的，可增加：self.驱动标的2 = declare_trig_symbol()

    def global_variables(self):
        """减仓价：从报告「减仓/离场价」填入；当前价<=此价时推送卖出提醒。
        若平台仅支持整数，改为 GlobalType.INT 并填 元*100（如 18550 表 185.50 元），比较处用 self.减仓价/100.0"""
        self.减仓价 = show_variable(100.0, GlobalType.FLOAT)
        """买入参考价（可选）：想逢低买入时填；当前价<=此价时推送买入提醒，不需要则填 0 或极大值"""
        self.买入参考价 = show_variable(0.0, GlobalType.FLOAT)

    def custom_indicator(self):
        # 本策略仅用行情价与全局变量比较，无需自编指标
        pass

    def handle_data(self):
        self.condition_break_reduce_invoke()
        self.condition_touch_buy_invoke()

    # ----- 跌破减仓价：建议卖出 -----
    def condition_break_reduce_invoke(self):
        if self.condition_break_reduce():
            self.action_alert_sell()
        else:
            pass

    def condition_break_reduce(self):
        """当前价 <= 减仓价 则视为跌破，应考虑卖出"""
        v_price = current_price(symbol=self.驱动标的1, price_type=THType.ALL)
        if v_price is None:
            return False
        reduce_p = self.减仓价
        if reduce_p is None or reduce_p <= 0:
            return False
        if v_price <= reduce_p:
            return True
        return False

    def action_alert_sell(self):
        v_price = current_price(symbol=self.驱动标的1, price_type=THType.ALL)
        p_str = "%.2f" % v_price if v_price is not None else "--"
        alert(content="【卖出】当前价 %s 已跌破减仓价 %.2f，建议考虑卖出。" % (p_str, self.减仓价))

    # ----- 触及买入参考价：可考虑买入（可选） -----
    def condition_touch_buy_invoke(self):
        if self.condition_touch_buy():
            self.action_alert_buy()
        else:
            pass

    def condition_touch_buy(self):
        """当前价 <= 买入参考价 且 买入参考价 > 0 时提醒可考虑买入"""
        buy_ref = self.买入参考价
        if buy_ref is None or buy_ref <= 0:
            return False
        v_price = current_price(symbol=self.驱动标的1, price_type=THType.ALL)
        if v_price is None:
            return False
        if v_price <= buy_ref:
            return True
        return False

    def action_alert_buy(self):
        v_price = current_price(symbol=self.驱动标的1, price_type=THType.ALL)
        p_str = "%.2f" % v_price if v_price is not None else "--"
        alert(content="【买入】当前价 %s 已触及参考买入价 %.2f，可考虑买入。" % (p_str, self.买入参考价))
