# -*- coding: utf-8 -*-
"""
指标计算模块：ATR(14)、EMA 趋向、成交量均线

设计原则：
  - 全部基于【收盘价】计算，不用 typical price (H+L+C)/3，避免插针干扰
  - ATR 用 Wilder 平滑（alpha = 1/period），与 TradingView 默认一致
  - 成交量均线用算术平均（量能突发放大比平滑更重要）
"""

import logging
from typing import List, Optional
from dataclasses import dataclass

from market_data import Kline

logger = logging.getLogger(__name__)


def sma(values: List[float], period: int) -> List[Optional[float]]:
    """
    简单移动平均。前 period-1 个位置返回 None（数据不足）。

    返回列表长度与输入一致，便于按索引对齐。
    """
    if len(values) < period:
        return [None] * len(values)

    result: List[Optional[float]] = [None] * (period - 1)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1: i + 1]
        result.append(sum(window) / period)
    return result


def ema(values: List[float], period: int) -> List[Optional[float]]:
    """
    指数移动平均。前 period-1 个返回 None，之后用 EMA 递推。

    递推公式: EMA[i] = (value[i] - EMA[i-1]) * k + EMA[i-1]
    其中 k = 2 / (period + 1)
    """
    if len(values) < period:
        return [None] * len(values)

    k = 2.0 / (period + 1)
    result: List[Optional[float]] = [None] * (period - 1)

    # 第一个 EMA 值用 SMA 初始化
    first = sum(values[:period]) / period
    result.append(first)

    for i in range(period, len(values)):
        prev = result[-1]
        assert prev is not None
        result.append((values[i] - prev) * k + prev)

    return result


def true_range(klines: List[Kline]) -> List[float]:
    """
    真实波幅 TR

    TR = max(high - low,
             |high - prevClose|,
             |low  - prevClose|)

    第一根 K 线没有前收盘价，退化为 high - low。
    """
    if not klines:
        return []

    tr_list = []
    for i, k in enumerate(klines):
        if i == 0:
            tr_list.append(k.high - k.low)
        else:
            prev_close = klines[i - 1].close
            tr = max(
                k.high - k.low,
                abs(k.high - prev_close),
                abs(k.low - prev_close),
            )
            tr_list.append(tr)
    return tr_list


def atr(klines: List[Kline], period: int = 14) -> List[Optional[float]]:
    """
    ATR(14) — Wilder 平滑

    与简单 SMA 的区别：Wilder 用 alpha = 1/period 做平滑，
    对近期波动反应更慢但更稳定，这是 TradingView 和大多数平台的默认算法。

    返回列表与 klines 等长，前 period-1 个为 None。
    """
    if len(klines) < period:
        logger.warning(f"K线数量({len(klines)})不足 ATR 周期({period})")
        return [None] * len(klines)

    tr = true_range(klines)
    if not tr:
        return [None] * len(klines)

    result: List[Optional[float]] = [None] * (period - 1)

    # 第一个 ATR 用前 period 根 TR 的简单平均初始化
    first_atr = sum(tr[:period]) / period
    result.append(first_atr)

    # Wilder 平滑递推: ATR[i] = (ATR[i-1] * (period-1) + TR[i]) / period
    for i in range(period, len(tr)):
        prev = result[-1]
        assert prev is not None
        result.append((prev * (period - 1) + tr[i]) / period)

    return result


def _wilder_smooth(values: List[float], period: int) -> List[Optional[float]]:
    """
    Wilder 平滑（与 ATR 同算法），供 ADX / RSI 复用。

    values 为单根增量序列（TR / +DM / 涨跌），内部允许含 None（用 0 占位）。
    返回与输入等长的列表，前 period-1 个为 None。
    """
    if len(values) < period:
        return [None] * len(values)
    result: List[Optional[float]] = [None] * (period - 1)
    first = sum(v for v in values[:period]) / period
    result.append(first)
    for i in range(period, len(values)):
        prev = result[-1]
        assert prev is not None
        result.append((prev * (period - 1) + values[i]) / period)
    return result


def adx(klines: List[Kline], period: int = 14) -> List[Optional[float]]:
    """
    ADX(14) —— Wilder 平滑（TradingView 默认算法）。

    用途：判定"趋势是否有意义"。ADX < 20 视为无趋势（横盘），
   此时形态识别不应被趋势过滤误杀；ADX >= 25 趋势明确。
    这是 fantomluck 共振引擎的门部件——只有趋势"够强"才让方向投票生效。

    返回与 klines 等长，前 2*period-1 个为 None。
    """
    n = len(klines)
    if n < period + 1:
        return [None] * n

    pdm = [0.0] * n
    mdm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        prev = klines[i - 1]
        up = klines[i].high - prev.high
        dn = prev.low - klines[i].low
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(klines[i].high - klines[i].low,
                    abs(klines[i].high - prev.close),
                    abs(klines[i].low - prev.close))

    atr_s = _wilder_smooth(tr, period)
    pdm_s = _wilder_smooth(pdm, period)
    mdm_s = _wilder_smooth(mdm, period)

    dx = [0.0] * n
    for i in range(period, n):
        if atr_s[i] is None or atr_s[i] <= 0:
            continue
        pdi = 100.0 * pdm_s[i] / atr_s[i]
        mdi = 100.0 * mdm_s[i] / atr_s[i]
        denom = pdi + mdi
        dx[i] = 100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0

    dx_s = _wilder_smooth(dx, period)
    adx_list: List[Optional[float]] = [None] * n
    start = 2 * period - 1
    for i in range(start, n):
        adx_list[i] = dx_s[i]
    return adx_list


def rsi(klines: List[Kline], period: int = 14) -> List[Optional[float]]:
    """
    RSI(14) —— Wilder 平滑（TradingView 默认）。

    用途：动量方向判定。> 50 偏多、< 50 偏空；超买 > 70 / 超卖 < 30。
    用于共振引擎里"RSI 与形态方向同向才加分"。
    """
    n = len(klines)
    if n < period + 1:
        return [None] * n

    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        d = klines[i].close - klines[i - 1].close
        gains[i] = d if d > 0 else 0.0
        losses[i] = -d if d < 0 else 0.0

    ag = _wilder_smooth(gains, period)
    al = _wilder_smooth(losses, period)
    out: List[Optional[float]] = [None] * n
    for i in range(period, n):
        if ag[i] is None:
            continue
        if al[i] is not None and al[i] > 0:
            rs = ag[i] / al[i]
            out[i] = 100.0 - 100.0 / (1.0 + rs)
        elif ag[i] > 0:
            out[i] = 100.0
        else:
            out[i] = 50.0
    return out


def macd(klines: List[Kline], fast: int = 12, slow: int = 26,
         signal: int = 9):
    """
    MACD(12,26,9)。

    返回三元组 (macd_line, signal_line, hist)，三者均与 klines 等长（前段为 None）。
    hist = macd_line - signal_line，柱体符号即动能方向：> 0 多头动能、< 0 空头动能。
    """
    closes = [k.close for k in klines]
    ef = ema(closes, fast)
    es = ema(closes, slow)
    n = len(closes)
    macd_line = [None if (ef[i] is None or es[i] is None) else ef[i] - es[i]
                 for i in range(n)]
    clean = [v if v is not None else 0.0 for v in macd_line]
    sig = ema(clean, signal)
    hist = [None if (macd_line[i] is None or sig[i] is None)
            else macd_line[i] - sig[i] for i in range(n)]
    return macd_line, sig, hist


def volume_ma(klines: List[Kline], period: int = 20) -> List[Optional[float]]:
    """成交量均线（算术平均）—— 用于突破量能确认"""
    volumes = [k.volume for k in klines]
    return sma(volumes, period)


def detect_trend(klines: List[Kline], fast: int = 20, slow: int = 50) -> str:
    """
    趋势方向判定：EMA(fast) 与 EMA(slow) 的位置关系

    返回: "up" / "down" / "sideways"

    注意：sideways 的判定用 ATR 归一化，避免在低波动率标的上误判。
    """
    if len(klines) < slow:
        return "unknown"

    closes = [k.close for k in klines]
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)

    f = ema_fast[-1]
    s = ema_slow[-1]
    if f is None or s is None:
        return "unknown"

    diff_pct = (f - s) / s if s != 0 else 0

    # 用 ATR 判断"多近算贴近"
    atr_list = atr(klines, 14)
    current_atr = atr_list[-1] if atr_list and atr_list[-1] else 0
    last_close = closes[-1]
    atr_pct = current_atr / last_close if last_close else 0

    # 若两线差距小于半个 ATR 的相对幅度，视为横盘
    if abs(diff_pct) < 0.5 * atr_pct:
        return "sideways"

    return "up" if diff_pct > 0 else "down"


# ============================================================
#  聚合入口
# ============================================================

@dataclass
class IndicatorSet:
    """一次计算完成的全部指标，供形态识别消费"""
    klines: List[Kline]
    atr: List[Optional[float]]
    atr_current: float
    volume_ma: List[Optional[float]]
    volume_ma_current: float
    ema_fast: List[Optional[float]]
    ema_slow: List[Optional[float]]
    trend: str
    # 共振引擎门部件（对应 fantomluck 的多源投票）
    adx: List[Optional[float]]            # ADX 序列
    adx_current: float                    # 最新 ADX（趋势强度）
    rsi_current: float                    # 最新 RSI（动量）
    macd_hist_current: float              # 最新 MACD 柱（动能方向）

    @property
    def last_close(self) -> float:
        return self.klines[-1].close if self.klines else 0.0


def calc_indicators(klines: List[Kline],
                    atr_period: int = 14,
                    vol_ma_period: int = 20,
                    ema_fast_period: int = 20,
                    ema_slow_period: int = 50) -> IndicatorSet:
    """
    一次性算好所有指标。

    ATR 是形态识别的核心依赖：
      - 突破幅度确认: |close - boundary| / ATR >= 0.5
      - 止损距离: 颈线外 1.5 × ATR
      - 噪音过滤: ATR / price < 0.005 视为横盘
    """
    atr_list = atr(klines, atr_period)
    vol_ma_list = volume_ma(klines, vol_ma_period)
    closes = [k.close for k in klines]

    atr_current = atr_list[-1] if atr_list and atr_list[-1] is not None else 0.0
    vol_current = vol_ma_list[-1] if vol_ma_list and vol_ma_list[-1] is not None else 0.0

    # 共振门部件：ADX / RSI / MACD
    adx_list = adx(klines, atr_period)
    rsi_list = rsi(klines, atr_period)
    _, _, macd_hist = macd(klines)

    def _last_valid(seq):
        for v in reversed(seq):
            if v is not None:
                return v
        return 0.0

    adx_current = _last_valid(adx_list)
    rsi_current = _last_valid(rsi_list)
    macd_hist_current = macd_hist[-1] if macd_hist and macd_hist[-1] is not None \
        else 0.0

    return IndicatorSet(
        klines=klines,
        atr=atr_list,
        atr_current=atr_current,
        volume_ma=vol_ma_list,
        volume_ma_current=vol_current,
        ema_fast=ema(closes, ema_fast_period),
        ema_slow=ema(closes, ema_slow_period),
        trend=detect_trend(klines, ema_fast_period, ema_slow_period),
        adx=adx_list,
        adx_current=adx_current,
        rsi_current=rsi_current,
        macd_hist_current=macd_hist_current,
    )


# ============================================================
#  自测：验证 ATR 与 TradingView 一致
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # 构造一段已知数据，手工验证 ATR 计算
    test_klines = [
        Kline(openTime=i * 60000, open=100 + i, high=102 + i, low=98 + i,
              close=101 + i, volume=1000, closeTime=i * 60000 + 59999,
              quoteVolume=100000)
        for i in range(30)
    ]

    atr_vals = atr(test_klines, 14)
    print("ATR(14) 最后 5 个值:")
    for i, v in enumerate(atr_vals[-5:], start=len(atr_vals) - 5):
        print(f"  idx={i}: {v:.4f}" if v else f"  idx={i}: None")

    ind = calc_indicators(test_klines)
    print(f"\n当前 ATR: {ind.atr_current:.4f}")
    print(f"当前成交量均线: {ind.volume_ma_current:.2f}")
    print(f"趋势: {ind.trend}")
    print(f"最后收盘: {ind.last_close:.2f}")
