# -*- coding: utf-8 -*-
"""
双顶 / 双底 检测器

可检测性: A 级（规则明确，可直接量化）

双顶 (Double Top / M顶)
  结构: (H, L, H) —— 两个高度相近的峰，中间夹一个谷
  含义: 看跌。两次冲击同一价位失败，说明该压力位有效
  失效: 价格重新站上两峰之上（则变成更高的高点，趋势可能延续）

双底 (Double Bottom / W底)
  结构: (L, H, L)
  含义: 看涨。两次探底成功，支撑位确认
  失效: 价格跌破两底之下

判定阈值（默认）:
  两峰价差容差      3%     —— 太松会把普通震荡当成双顶
  中间谷深度        5%     —— 太浅说明"两个峰"其实只是一个平台
  两峰间距        10~120 根 —— 太近是噪声，太远则形态已失效
  突破确认         连续 2 根收盘
  突破幅度         ≥ 0.5 × ATR
  量能确认         ≥ 1.5 × 20根均量
"""

import os
import sys
from typing import List

_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from zigzag import Pivot, PivotType
from market_data import Kline
from patterns.base import (
    BaseDetector, Pattern, Direction, PatternStatus, Line,
    horizontal_line, calc_trade_levels, find_breakout_index,
    check_breakout, calc_volume_ratio,
)


class DoubleTopBottomDetector(BaseDetector):
    """同时检测双顶和双底（结构对称，逻辑共用）"""

    name = "double_top_bottom"

    DEFAULT_PARAMS = {
        "peak_tolerance": 0.03,        # 两峰/两谷价差容差
        "min_depth": 0.05,             # 中间谷/峰的最小深度
        "min_span": 10,                # 两峰最小间距（根）
        "max_span": 120,               # 两峰最大间距（根）
        "breakout_candles": 2,         # 连续确认根数
        "breakout_atr_ratio": 0.5,     # 突破幅度 / ATR
        "volume_ratio_min": 1.5,       # 量能确认
        "max_lookahead": 30,           # 从右峰开始最多往后看多少根找突破
        "min_height_atr": 1.0,         # 形态高度至少 1×ATR，否则无交易价值
    }

    def __init__(self, params=None):
        merged = dict(self.DEFAULT_PARAMS)
        if params:
            merged.update(params)
        super().__init__(merged)

    def detect(self, klines: List[Kline], pivots: List[Pivot],
               atr_value: float, symbol: str = "",
               interval: str = "") -> List[Pattern]:
        results = []
        if len(pivots) < 3 or not klines or atr_value <= 0:
            return results

        p = self.params

        # 扫描所有连续三点窗口
        for i in range(len(pivots) - 2):
            a, b, c = pivots[i], pivots[i + 1], pivots[i + 2]

            # --- 双顶: 高-低-高 ---
            if (a.type == PivotType.HIGH and b.type == PivotType.LOW
                    and c.type == PivotType.HIGH):
                pat = self._check_double_top(a, b, c, klines, atr_value,
                                             symbol, interval)
                if pat:
                    results.append(pat)

            # --- 双底: 低-高-低 ---
            elif (a.type == PivotType.LOW and b.type == PivotType.HIGH
                  and c.type == PivotType.LOW):
                pat = self._check_double_bottom(a, b, c, klines, atr_value,
                                                symbol, interval)
                if pat:
                    results.append(pat)

        return results

    # ---------- 双顶 ----------

    def _check_double_top(self, h1: Pivot, l1: Pivot, h2: Pivot,
                          klines: List[Kline], atr_value: float,
                          symbol: str, interval: str) -> "Pattern | None":
        p = self.params

        # ① 两峰高度接近
        peak_diff = abs(h1.price - h2.price) / h1.price
        if peak_diff > p["peak_tolerance"]:
            return None

        # ② 中间谷足够深
        depth = (min(h1.price, h2.price) - l1.price) / min(h1.price, h2.price)
        if depth < p["min_depth"]:
            return None

        # ③ 两峰间距合理
        span = h2.index - h1.index
        if not (p["min_span"] <= span <= p["max_span"]):
            return None

        # ④ 形态高度要有交易价值
        height = min(h1.price, h2.price) - l1.price
        if height < p["min_height_atr"] * atr_value:
            return None

        # ⑤ 右峰之后不能已经创新高（否则形态破坏）
        highest_after = max((k.high for k in klines[h2.index:]), default=h2.price)
        if highest_after > max(h1.price, h2.price) * 1.005:
            return None

        neck_price = l1.price
        pattern = Pattern(
            symbol=symbol,
            interval=interval,
            pattern_type="double_top",
            direction=Direction.SHORT,
            status=PatternStatus.CANDIDATE,
            pivots=[h1, l1, h2],
            neckline=horizontal_line(neck_price, l1.index,
                                     span=max(10, span), ptype=PivotType.LOW),
            height=height,
            confidence=self._confidence(peak_diff, depth, span),
        )

        # ⑥ 突破确认
        def boundary_fn(idx):
            return neck_price

        idx = find_breakout_index(klines, h2.index + 1, boundary_fn,
                                  Direction.SHORT, p["max_lookahead"])
        if idx < 0:
            return pattern          # 仍是候选

        ok, confirmed, magnitude = check_breakout(
            klines, idx, neck_price, Direction.SHORT, atr_value,
            required_candles=p["breakout_candles"],
            min_magnitude_atr=p["breakout_atr_ratio"],
        )

        pattern.breakout_index = idx
        pattern.breakout_price = klines[idx].close
        pattern.breakout_magnitude_atr = magnitude
        pattern.confirmed_candles = confirmed
        pattern.volume_ratio = calc_volume_ratio(klines, idx)

        if pattern.volume_ratio < p["volume_ratio_min"]:
            return pattern
        if not ok:
            return pattern

        pattern.status = PatternStatus.CONFIRMED
        calc_trade_levels(pattern, klines, atr_value)
        return pattern

    # ---------- 双底 ----------

    def _check_double_bottom(self, l1: Pivot, h1: Pivot, l2: Pivot,
                             klines: List[Kline], atr_value: float,
                             symbol: str, interval: str) -> "Pattern | None":
        p = self.params

        # ① 两谷高度接近
        trough_diff = abs(l1.price - l2.price) / l1.price
        if trough_diff > p["peak_tolerance"]:
            return None

        # ② 中间峰足够高
        peak_height = (h1.price - max(l1.price, l2.price)) / max(l1.price, l2.price)
        if peak_height < p["min_depth"]:
            return None

        # ③ 两谷间距合理
        span = l2.index - l1.index
        if not (p["min_span"] <= span <= p["max_span"]):
            return None

        # ④ 形态高度有交易价值
        height = h1.price - max(l1.price, l2.price)
        if height < p["min_height_atr"] * atr_value:
            return None

        # ⑤ 右谷之后不能已经创新低
        lowest_after = min((k.low for k in klines[l2.index:]), default=l2.price)
        if lowest_after < min(l1.price, l2.price) * 0.995:
            return None

        neck_price = h1.price
        pattern = Pattern(
            symbol=symbol,
            interval=interval,
            pattern_type="double_bottom",
            direction=Direction.LONG,
            status=PatternStatus.CANDIDATE,
            pivots=[l1, h1, l2],
            neckline=horizontal_line(neck_price, h1.index,
                                     span=max(10, span), ptype=PivotType.HIGH),
            height=height,
            confidence=self._confidence(trough_diff, peak_height, span),
        )

        def boundary_fn(idx):
            return neck_price

        idx = find_breakout_index(klines, l2.index + 1, boundary_fn,
                                  Direction.LONG, p["max_lookahead"])
        if idx < 0:
            return pattern

        ok, confirmed, magnitude = check_breakout(
            klines, idx, neck_price, Direction.LONG, atr_value,
            required_candles=p["breakout_candles"],
            min_magnitude_atr=p["breakout_atr_ratio"],
        )

        pattern.breakout_index = idx
        pattern.breakout_price = klines[idx].close
        pattern.breakout_magnitude_atr = magnitude
        pattern.confirmed_candles = confirmed
        pattern.volume_ratio = calc_volume_ratio(klines, idx)

        if pattern.volume_ratio < p["volume_ratio_min"]:
            return pattern
        if not ok:
            return pattern

        pattern.status = PatternStatus.CONFIRMED
        calc_trade_levels(pattern, klines, atr_value)
        return pattern

    # ---------- 置信度 ----------

    @staticmethod
    def _confidence(price_diff: float, depth: float, span: int) -> float:
        """
        几何完整度评分 0~1（只评价"长得像不像"，不含量能/共振）

        三项各占一部分：
          价差越小越像   —— 0% 得满分，超过容差(3%)得 0
          深度越深越像   —— 15% 以上得满分，5% 得 0.5
          间距适中       —— 20~60 根最理想
        """
        # 价差项（0.4 权重）
        diff_score = max(0.0, 1.0 - price_diff / 0.03)
        # 深度项（0.35 权重）
        depth_score = min(1.0, depth / 0.15)
        # 间距项（0.25 权重）：20~60 根最佳
        if 20 <= span <= 60:
            span_score = 1.0
        elif span < 20:
            span_score = span / 20
        else:
            span_score = max(0.3, 1.0 - (span - 60) / 100)

        return round(0.4 * diff_score + 0.35 * depth_score + 0.25 * span_score, 3)
