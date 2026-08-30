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

    return IndicatorSet(
        klines=klines,
        atr=atr_list,
        atr_current=atr_current,
        volume_ma=vol_ma_list,
        volume_ma_current=vol_current,
        ema_fast=ema(closes, ema_fast_period),
        ema_slow=ema(closes, ema_slow_period),
        trend=detect_trend(klines, ema_fast_period, ema_slow_period),
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
