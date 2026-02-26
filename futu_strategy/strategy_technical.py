# -*- coding: utf-8 -*-
"""
富途量化策略（独立脚本，与主项目无依赖）

所有信号均由富途 API 获取并计算，不只用 ma，综合 MA、KDJ 等多类接口。

卖出条件（满足其一即提醒）：
  - MA：当前价 <= MA20/MA60、MA 空头排列、ATR 止损
  - KDJ：高位死叉 is_kdj_death_cross、顶背离 is_kdj_top_divergence、K>80 超买
  - 涨跌幅：单根 K 线涨幅>阈值(bar_chg_rate)、近 N 日涨幅>阈值(bar_custom) → 注意追高/止盈
  - 成交额/量比：放量下跌(量比>=1.5 且 单根跌) → 警惕
  - MACD：死叉 is_macd_death_cross、顶背离 is_macd_top_divergence、DIF<0 零轴下偏空
  - RSI：高位死叉 is_rsi_death_cross、顶背离 is_rsi_top_divergence、RSI>70 超买
  - MA：当前价 >= MA20、MA 多头排列
  - KDJ：低位金叉、底背离、K<20 超卖
  - MACD：金叉 is_macd_golden_cross、底背离 is_macd_bottom_divergence
  - RSI：低位金叉 is_rsi_golden_cross、底背离 is_rsi_bottom_divergence、RSI<30 超卖
  - 涨跌幅：单根大跌、近 N 日大跌(bar_chg_rate/bar_custom) → 关注超跌/超跌反弹
  - 成交额/量比：放量上涨(量比>=1.5 且 站上MA20或单根涨) → 放量突破可参考

依赖富途环境：ma, atr, is_ma_bullish_alignment, is_ma_bearish_alignment,
  is_kdj_golden_cross, is_kdj_death_cross, is_kdj_top_divergence, is_kdj_bottom_divergence,
  kdj_k, kdj_d, kdj_j, macd_dif, macd_dea, macd_macd,
  is_macd_golden_cross, is_macd_death_cross, is_macd_top_divergence, is_macd_bottom_divergence,
  rsi, is_rsi_golden_cross, is_rsi_death_cross, is_rsi_top_divergence, is_rsi_bottom_divergence,
  bar_chg_rate, bar_custom, bar_turnover, current_price, alert,
  BarType, DataType, THType, BarDataType, CustomType 等。
"""

# KDJ 参数（与项目技术文档 9,3,3 一致）
KDJ_K_PERIOD = 9
KDJ_D_PERIOD = 3
KDJ_SLOWING = 3
# 涨跌幅阈值（小数：0.05=5%）。单根大涨/近N日大涨→卖出参考；单根大跌/近N日大跌→买入参考
BAR_CHG_RATE_HIGH = 0.05   # 单根涨幅 > 5% 注意追高
BAR_CHG_RATE_LOW = -0.05   # 单根跌幅 < -5% 关注超跌
BAR_CUSTOM_DAYS = 8        # 近 N 日聚合
BAR_CUSTOM_CHG_HIGH = 0.15  # 近 N 日涨幅 > 15% 考虑止盈
BAR_CUSTOM_CHG_LOW = -0.10  # 近 N 日跌幅 < -10% 关注超跌反弹
# 量比：单根成交额 / 近 N 日均成交额，>= VOLUME_RATIO_BREAKOUT 视为放量
VOLUME_RATIO_DAYS = 20      # 近 20 日均额
VOLUME_RATIO_BREAKOUT = 1.5 # 量比 >= 1.5 放量（与项目技术文档一致）
# MACD 参数（与项目技术文档 12, 26, 9 一致）
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
# RSI 参数：金叉/死叉用 fast=6, slow=12；rsi 值与顶底背离用 period=12（项目文档为 RSI(14)，富途默认 12）
RSI_FAST = 6
RSI_SLOW = 12
RSI_PERIOD = 12
RSI_OVERBOUGHT = 70   # RSI > 70 超买
RSI_OVERSOLD = 30      # RSI < 30 超卖


class Strategy(StrategyBase):

    def initialize(self):
        declare_strategy_type(AlgoStrategyType.SECURITY)
        self.trigger_symbols()
        self.custom_indicator()
        self.global_variables()

    def trigger_symbols(self):
        self.驱动标的1 = declare_trig_symbol()

    def global_variables(self):
        self.K线周期 = BarType.D1
        self.ATR倍数 = 1.5
        self._select = 1  # 取倒数第 1 根 K 线

    def custom_indicator(self):
        pass

    # ---------- MA 类 API ----------
    def _get_ma(self, period, select=1):
        try:
            return ma(
                symbol=self.驱动标的1,
                period=period,
                bar_type=self.K线周期,
                data_type=DataType.CLOSE,
                select=select,
                session_type=THType.ALL,
            )
        except Exception:
            return None

    def _get_atr(self, period=14, select=1):
        try:
            return atr(
                symbol=self.驱动标的1,
                period=period,
                bar_type=self.K线周期,
                select=select,
                session_type=THType.ALL,
            )
        except Exception:
            return None

    def _is_ma_bullish(self):
        try:
            return is_ma_bullish_alignment(
                symbol=self.驱动标的1,
                bar_type=self.K线周期,
                data_type=DataType.CLOSE,
                session_type=THType.ALL,
                select=self._select,
            )
        except Exception:
            return False

    def _is_ma_bearish(self):
        try:
            return is_ma_bearish_alignment(
                symbol=self.驱动标的1,
                bar_type=self.K线周期,
                data_type=DataType.CLOSE,
                session_type=THType.ALL,
                select=self._select,
            )
        except Exception:
            return False

    # ---------- KDJ 类 API ----------
    def _kdj_golden_cross(self):
        """低位金叉，偏多"""
        try:
            return is_kdj_golden_cross(
                symbol=self.驱动标的1,
                k_period=KDJ_K_PERIOD,
                d_period=KDJ_D_PERIOD,
                slowing=KDJ_SLOWING,
                bar_type=self.K线周期,
                session_type=THType.ALL,
                select=self._select,
            )
        except Exception:
            return False

    def _kdj_death_cross(self):
        """高位死叉，偏空"""
        try:
            return is_kdj_death_cross(
                symbol=self.驱动标的1,
                k_period=KDJ_K_PERIOD,
                d_period=KDJ_D_PERIOD,
                slowing=KDJ_SLOWING,
                bar_type=self.K线周期,
                session_type=THType.ALL,
                select=self._select,
            )
        except Exception:
            return False

    def _kdj_top_divergence(self):
        """顶背离，偏空"""
        try:
            return is_kdj_top_divergence(
                symbol=self.驱动标的1,
                k_period=KDJ_K_PERIOD,
                d_period=KDJ_D_PERIOD,
                slowing=KDJ_SLOWING,
                bar_type=self.K线周期,
                session_type=THType.ALL,
                select=self._select,
            )
        except Exception:
            return False

    def _kdj_bottom_divergence(self):
        """底背离，偏多"""
        try:
            return is_kdj_bottom_divergence(
                symbol=self.驱动标的1,
                k_period=KDJ_K_PERIOD,
                d_period=KDJ_D_PERIOD,
                slowing=KDJ_SLOWING,
                bar_type=self.K线周期,
                session_type=THType.ALL,
                select=self._select,
            )
        except Exception:
            return False

    def _get_kdj_k(self):
        try:
            return kdj_k(
                symbol=self.驱动标的1,
                k_period=KDJ_K_PERIOD,
                d_period=KDJ_D_PERIOD,
                slowing=KDJ_SLOWING,
                bar_type=self.K线周期,
                select=self._select,
                session_type=THType.ALL,
            )
        except Exception:
            return None

    def _get_kdj_d(self):
        try:
            return kdj_d(
                symbol=self.驱动标的1,
                k_period=KDJ_K_PERIOD,
                d_period=KDJ_D_PERIOD,
                slowing=KDJ_SLOWING,
                bar_type=self.K线周期,
                select=self._select,
                session_type=THType.ALL,
            )
        except Exception:
            return None

    def _get_kdj_j(self):
        try:
            return kdj_j(
                symbol=self.驱动标的1,
                k_period=KDJ_K_PERIOD,
                d_period=KDJ_D_PERIOD,
                slowing=KDJ_SLOWING,
                bar_type=self.K线周期,
                select=self._select,
                session_type=THType.ALL,
            )
        except Exception:
            return None

    # ---------- MACD 类 API ----------
    def _get_macd_dif(self, select=1):
        try:
            return macd_dif(
                symbol=self.驱动标的1,
                fast_period=MACD_FAST,
                slow_period=MACD_SLOW,
                signal_period=MACD_SIGNAL,
                bar_type=self.K线周期,
                select=select,
                session_type=THType.ALL,
            )
        except Exception:
            return None

    def _get_macd_dea(self, select=1):
        try:
            return macd_dea(
                symbol=self.驱动标的1,
                fast_period=MACD_FAST,
                slow_period=MACD_SLOW,
                signal_period=MACD_SIGNAL,
                bar_type=self.K线周期,
                select=select,
                session_type=THType.ALL,
            )
        except Exception:
            return None

    def _get_macd_macd(self, select=1):
        """MACD 柱 = DIF - DEA。"""
        try:
            return macd_macd(
                symbol=self.驱动标的1,
                fast_period=MACD_FAST,
                slow_period=MACD_SLOW,
                signal_period=MACD_SIGNAL,
                bar_type=self.K线周期,
                select=select,
                session_type=THType.ALL,
            )
        except Exception:
            return None

    def _macd_golden_cross(self):
        try:
            return is_macd_golden_cross(
                symbol=self.驱动标的1,
                fast_period=MACD_FAST,
                slow_period=MACD_SLOW,
                signal_period=MACD_SIGNAL,
                bar_type=self.K线周期,
                session_type=THType.ALL,
                select=self._select,
            )
        except Exception:
            return False

    def _macd_death_cross(self):
        try:
            return is_macd_death_cross(
                symbol=self.驱动标的1,
                fast_period=MACD_FAST,
                slow_period=MACD_SLOW,
                signal_period=MACD_SIGNAL,
                bar_type=self.K线周期,
                session_type=THType.ALL,
                select=self._select,
            )
        except Exception:
            return False

    def _macd_top_divergence(self):
        try:
            return is_macd_top_divergence(
                symbol=self.驱动标的1,
                fast_period=MACD_FAST,
                slow_period=MACD_SLOW,
                signal_period=MACD_SIGNAL,
                bar_type=self.K线周期,
                session_type=THType.ALL,
                select=self._select,
            )
        except Exception:
            return False

    def _macd_bottom_divergence(self):
        try:
            return is_macd_bottom_divergence(
                symbol=self.驱动标的1,
                fast_period=MACD_FAST,
                slow_period=MACD_SLOW,
                signal_period=MACD_SIGNAL,
                bar_type=self.K线周期,
                session_type=THType.ALL,
                select=self._select,
            )
        except Exception:
            return False

    # ---------- RSI 类 API ----------
    def _get_rsi(self, select=1):
        try:
            return rsi(
                symbol=self.驱动标的1,
                period=RSI_PERIOD,
                bar_type=self.K线周期,
                select=select,
                session_type=THType.ALL,
            )
        except Exception:
            return None

    def _rsi_golden_cross(self):
        """RSI 低位金叉，偏多"""
        try:
            return is_rsi_golden_cross(
                symbol=self.驱动标的1,
                fast_period=RSI_FAST,
                slow_period=RSI_SLOW,
                bar_type=self.K线周期,
                session_type=THType.ALL,
                select=self._select,
            )
        except Exception:
            return False

    def _rsi_death_cross(self):
        """RSI 高位死叉，偏空"""
        try:
            return is_rsi_death_cross(
                symbol=self.驱动标的1,
                fast_period=RSI_FAST,
                slow_period=RSI_SLOW,
                bar_type=self.K线周期,
                session_type=THType.ALL,
                select=self._select,
            )
        except Exception:
            return False

    def _rsi_top_divergence(self):
        try:
            return is_rsi_top_divergence(
                symbol=self.驱动标的1,
                period=RSI_PERIOD,
                bar_type=self.K线周期,
                session_type=THType.ALL,
                select=self._select,
            )
        except Exception:
            return False

    def _rsi_bottom_divergence(self):
        try:
            return is_rsi_bottom_divergence(
                symbol=self.驱动标的1,
                period=RSI_PERIOD,
                bar_type=self.K线周期,
                session_type=THType.ALL,
                select=self._select,
            )
        except Exception:
            return False

    # ---------- K 线涨跌幅 API ----------
    def _get_bar_chg_rate(self, select=1):
        """单根 K 线涨跌幅（前复权），返回值如 0.05 表示 5%，-0.01 表示 -1%。"""
        try:
            return bar_chg_rate(
                symbol=self.驱动标的1,
                bar_type=self.K线周期,
                select=select,
                session_type=THType.ALL,
            )
        except Exception:
            return None

    def _get_bar_custom_chg_rate(self, custom_num=8, custom_type=CustomType.D1, select=1):
        """多根 K 线聚合涨跌幅，如近 8 日涨跌幅。custom_type 用 CustomType.D1 表示按日聚合。"""
        try:
            return bar_custom(
                symbol=self.驱动标的1,
                data_type=BarDataType.CHG_RATE,
                custom_num=custom_num,
                custom_type=custom_type,
                select=select,
                session_type=THType.ALL,
            )
        except Exception:
            return None

    # ---------- K 线成交额 / 量比 API ----------
    def _get_bar_turnover(self, select=1):
        """单根 K 线成交额（前复权）。"""
        try:
            return bar_turnover(
                symbol=self.驱动标的1,
                bar_type=self.K线周期,
                select=select,
                session_type=THType.ALL,
            )
        except Exception:
            return None

    def _get_bar_custom_turnover(self, custom_num=20, custom_type=CustomType.D1, select=1):
        """多根 K 线聚合成交额，如近 20 日总成交额（聚合后一根的 TURNOVER 为这 N 根之和）。"""
        try:
            return bar_custom(
                symbol=self.驱动标的1,
                data_type=BarDataType.TURNOVER,
                custom_num=custom_num,
                custom_type=custom_type,
                select=select,
                session_type=THType.ALL,
            )
        except Exception:
            return None

    def _get_volume_ratio(self):
        """量比 = 单根成交额 / 近 N 日均成交额。N=VOLUME_RATIO_DAYS。无数据返回 None。"""
        single = self._get_bar_turnover()
        total = self._get_bar_custom_turnover(custom_num=VOLUME_RATIO_DAYS, custom_type=CustomType.D1)
        if single is None or total is None or total <= 0:
            return None
        avg = total / float(VOLUME_RATIO_DAYS)
        if avg <= 0:
            return None
        return single / avg

    def handle_data(self):
        self.condition_sell_invoke()
        self.condition_buy_invoke()

    # ---------- 卖出侧 ----------
    def condition_sell_invoke(self):
        msg = self.condition_sell()
        if msg:
            self.action_alert_sell(msg)

    def condition_sell(self):
        """卖出原因：MA + KDJ 等，全部来自 API。"""
        v_price = current_price(symbol=self.驱动标的1, price_type=THType.ALL)
        if v_price is None:
            return None
        reasons = []

        # MA
        ma20 = self._get_ma(20)
        ma60 = self._get_ma(60)
        if ma20 is not None and ma20 > 0 and v_price <= ma20:
            reasons.append("当前价 %.2f 已跌破 MA20(%.2f)，考虑减仓" % (v_price, ma20))
        if ma60 is not None and ma60 > 0 and v_price <= ma60:
            reasons.append("当前价 %.2f 已跌破 MA60(%.2f)，考虑离场" % (v_price, ma60))
        if self._is_ma_bearish():
            reasons.append("MA 空头排列，偏空考虑减仓/离场")

        atr_val = self._get_atr(14)
        if ma20 is not None and ma20 > 0 and atr_val is not None and atr_val > 0:
            stop_price = ma20 - self.ATR倍数 * atr_val
            if v_price <= stop_price:
                reasons.append("当前价 %.2f 已跌破 ATR 止损位约 %.2f，建议离场" % (v_price, stop_price))

        # KDJ
        if self._kdj_death_cross():
            reasons.append("KDJ 高位死叉，偏空考虑减仓/离场")
        if self._kdj_top_divergence():
            reasons.append("KDJ 顶背离，偏空考虑减仓/离场")
        k_val = self._get_kdj_k()
        if k_val is not None and k_val > 80:
            reasons.append("KDJ K 值 %.2f 超买(>80)，注意回调风险" % k_val)

        # MACD
        if self._macd_death_cross():
            reasons.append("MACD 死叉，偏空考虑减仓/离场")
        if self._macd_top_divergence():
            reasons.append("MACD 顶背离，偏空考虑减仓/离场")

        # RSI
        if self._rsi_death_cross():
            reasons.append("RSI 高位死叉，偏空考虑减仓/离场")
        if self._rsi_top_divergence():
            reasons.append("RSI 顶背离，偏空考虑减仓/离场")
        rsi_val = self._get_rsi()
        if rsi_val is not None and rsi_val > RSI_OVERBOUGHT:
            reasons.append("RSI 值 %.2f 超买(>%d)，注意回调风险" % (rsi_val, RSI_OVERBOUGHT))

        # K 线涨跌幅：单根大涨 / 近 N 日大涨 → 注意追高或止盈
        bar_chg = self._get_bar_chg_rate()
        if bar_chg is not None and bar_chg >= BAR_CHG_RATE_HIGH:
            reasons.append("单根 K 线涨幅 %.2f%%，注意追高风险" % (bar_chg * 100))
        bar_custom_chg = self._get_bar_custom_chg_rate(custom_num=BAR_CUSTOM_DAYS, custom_type=CustomType.D1)
        if bar_custom_chg is not None and bar_custom_chg >= BAR_CUSTOM_CHG_HIGH:
            reasons.append("近 %d 日涨幅 %.2f%%，考虑止盈" % (BAR_CUSTOM_DAYS, bar_custom_chg * 100))

        # 成交额/量比：放量下跌 → 警惕
        vol_ratio = self._get_volume_ratio()
        bar_chg = self._get_bar_chg_rate()
        if vol_ratio is not None and vol_ratio >= VOLUME_RATIO_BREAKOUT and bar_chg is not None and bar_chg < 0:
            reasons.append("量比 %.2f 放量下跌(%.2f%%)，需警惕" % (vol_ratio, bar_chg * 100))

        if not reasons:
            return None
        return "；".join(reasons)

    def action_alert_sell(self, content):
        alert(content="【卖出】" + content)

    # ---------- 买入侧 ----------
    def condition_buy_invoke(self):
        if self.condition_buy():
            self.action_alert_buy()

    def condition_buy(self):
        """买入：MA 站上/多头排列 或 KDJ 低位金叉/底背离/超卖 或 单根/近N日大跌超跌"""
        v_price = current_price(symbol=self.驱动标的1, price_type=THType.ALL)
        if v_price is None:
            return False
        if self._is_ma_bullish():
            return True
        ma20 = self._get_ma(20)
        if ma20 is not None and ma20 > 0 and v_price >= ma20:
            return True
        if self._kdj_golden_cross():
            return True
        if self._kdj_bottom_divergence():
            return True
        k_val = self._get_kdj_k()
        if k_val is not None and k_val < 20:
            return True
        if self._macd_golden_cross():
            return True
        if self._macd_bottom_divergence():
            return True
        if self._rsi_golden_cross():
            return True
        if self._rsi_bottom_divergence():
            return True
        rsi_val = self._get_rsi()
        if rsi_val is not None and rsi_val < RSI_OVERSOLD:
            return True
        bar_chg = self._get_bar_chg_rate()
        if bar_chg is not None and bar_chg <= BAR_CHG_RATE_LOW:
            return True
        bar_custom_chg = self._get_bar_custom_chg_rate(custom_num=BAR_CUSTOM_DAYS, custom_type=CustomType.D1)
        if bar_custom_chg is not None and bar_custom_chg <= BAR_CUSTOM_CHG_LOW:
            return True
        # 放量突破/放量上涨：量比>=1.5 且 (站上 MA20 或 单根涨)
        vol_ratio = self._get_volume_ratio()
        if vol_ratio is not None and vol_ratio >= VOLUME_RATIO_BREAKOUT:
            if ma20 is not None and ma20 > 0 and v_price is not None and v_price >= ma20:
                return True
            bar_chg = self._get_bar_chg_rate()
            if bar_chg is not None and bar_chg > 0:
                return True
        return False

    def action_alert_buy(self):
        v_price = current_price(symbol=self.驱动标的1, price_type=THType.ALL)
        p_str = "%.2f" % v_price if v_price is not None else "--"
        parts = []
        if self._is_ma_bullish():
            parts.append("MA 多头排列")
        ma20 = self._get_ma(20)
        if ma20 is not None and ma20 > 0 and v_price is not None and v_price >= ma20:
            parts.append("站上 MA20(%.2f)" % ma20)
        if self._kdj_golden_cross():
            parts.append("KDJ 低位金叉")
        if self._kdj_bottom_divergence():
            parts.append("KDJ 底背离")
        k_val = self._get_kdj_k()
        if k_val is not None and k_val < 20:
            parts.append("KDJ 超卖(K=%.1f)" % k_val)
        if self._macd_golden_cross():
            parts.append("MACD 金叉")
        if self._macd_bottom_divergence():
            parts.append("MACD 底背离")
        if self._rsi_golden_cross():
            parts.append("RSI 低位金叉")
        if self._rsi_bottom_divergence():
            parts.append("RSI 底背离")
        rsi_val = self._get_rsi()
        if rsi_val is not None and rsi_val < RSI_OVERSOLD:
            parts.append("RSI 超卖(%.1f)" % rsi_val)
        bar_chg = self._get_bar_chg_rate()
        if bar_chg is not None and bar_chg <= BAR_CHG_RATE_LOW:
            parts.append("单根跌幅 %.2f%% 超跌" % (bar_chg * 100))
        bar_custom_chg = self._get_bar_custom_chg_rate(custom_num=BAR_CUSTOM_DAYS, custom_type=CustomType.D1)
        if bar_custom_chg is not None and bar_custom_chg <= BAR_CUSTOM_CHG_LOW:
            parts.append("近%d日跌幅 %.2f%% 超跌反弹" % (BAR_CUSTOM_DAYS, bar_custom_chg * 100))
        vol_ratio = self._get_volume_ratio()
        bar_chg = self._get_bar_chg_rate()
        if vol_ratio is not None and vol_ratio >= VOLUME_RATIO_BREAKOUT and bar_chg is not None and bar_chg > 0:
            parts.append("量比%.2f放量上涨" % vol_ratio)
        elif vol_ratio is not None and vol_ratio >= VOLUME_RATIO_BREAKOUT and ma20 is not None and ma20 > 0 and v_price is not None and v_price >= ma20:
            parts.append("量比%.2f放量站上MA20" % vol_ratio)
        reason = "；".join(parts) if parts else "可考虑入场"
        alert(content="【买入】当前价 %s，%s。" % (p_str, reason))
