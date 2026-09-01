#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双顶/双底 突破后回踩确认 测试

背景：
  2026-09-01 朱哥标注发现 XAG 4h 双顶靠"单根长上影 close 跌破颈线 +
  后续 1 根 close 也在颈线下"就判确认。check_breakout 只能挡住"close
  没在边界外"的情况，挡不住"影线长 + 实体略破"的边缘突破。回踩确认
  要求：突破后 N 根 K 线内，若有 K 线收盘价回到颈线错误一侧，视为假突破。

测试：
  用模拟 K 线验证 detector 在不同回踩场景下的行为。
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from zigzag import Pivot, PivotType
from patterns.double import DoubleTopBottomDetector
from patterns.base import Direction, PatternStatus
from market_data import Kline


def make_klines_prices(prices):
    """给定一组价格序列，生成最小可用的 Kline 列表"""
    klines = []
    for i, p in enumerate(prices):
        t = 1_700_000_000_000 + i * 3_600_000
        klines.append(Kline(
            openTime=t, open=p, high=p * 1.005, low=p * 0.995,
            close=p, volume=1000, closeTime=t + 3_600_000, quoteVolume=1000
        ))
    return klines


def build_long_scenario(pullback_at=None, pullback_bars=3, total=30):
    """
    构造 (L, H, L) 双底场景：
      i=0~4   : 100 附近（左谷前的随机波动）
      i=5     : l1 = 100（左谷）
      i=6~9   : 100 附近
      i=10    : h1 = 110（中峰，颈线）
      i=11~14 : 100 附近
      i=15    : l2 = 100.5（右谷）
      i=16    : close=112（第1根突破，> neck=110，幅度 2×ATR）
      i=17    : close=113（第2根连续确认）
      i=18+   : 视 pullback_at 决定
    """
    prices = []
    for i in range(total):
        if i < 5:
            prices.append(100.0)            # 左谷前
        elif i == 5:
            prices.append(100.0)            # 左谷
        elif i < 10:
            prices.append(105.0)            # 上升到中峰
        elif i == 10:
            prices.append(110.0)            # 中峰（颈线）
        elif i < 15:
            prices.append(101.0)            # 回落到右谷
        elif i == 15:
            prices.append(100.5)            # 右谷（与左谷差 0.5%，通过容差）
        elif i == 16:
            prices.append(112.0)            # 突破第1根
        elif i == 17:
            prices.append(113.0)            # 突破第2根（连续确认）
        else:
            # 18 及之后：默认继续上行（115 维持）
            p = 115.0
            if pullback_at is not None and i == pullback_at:
                # 触发回踩：close 跌到颈线下方（LONG: close < neck=110）
                p = 108.0
            prices.append(p)
    return make_klines_prices(prices)


class TestDoublePullback(unittest.TestCase):
    """双底突破后回踩确认 测试"""

    def setUp(self):
        self.atr = 1.0
        # 关键参数：pullback_bars=3 开启回踩确认
        self.detector = DoubleTopBottomDetector({
            "peak_tolerance": 0.05,
            "min_depth": 0.03,
            "min_span": 8,
            "max_span": 150,
            "breakout_candles": 2,
            "breakout_atr_ratio": 0.5,
            "volume_ratio_min": 0.0,    # 关闭成交量过滤（测试专用）
            "max_lookahead": 30,
            "min_height_atr": 1.0,
            "pullback_bars": 3,
        })
        self.pivots = [
            Pivot(index=5, price=100.0, type=PivotType.LOW, timestamp=0),
            Pivot(index=10, price=110.0, type=PivotType.HIGH, timestamp=0),
            Pivot(index=15, price=100.5, type=PivotType.LOW, timestamp=0),
        ]

    def test_pullback_within_window_blocks_confirmation(self):
        """回踩在窗口内(i=18)穿越颈线 → 必须降级为 CANDIDATE"""
        klines = build_long_scenario(pullback_at=18, pullback_bars=3, total=30)
        result = self.detector._check_double_bottom(
            self.pivots[0], self.pivots[1], self.pivots[2],
            klines, self.atr, "TESTUSDT", "1h"
        )
        self.assertIsNotNone(result, "应返回形态对象（即便被回踩降级）")
        self.assertEqual(result.status, PatternStatus.CANDIDATE,
                         f"回踩穿越应降级为 CANDIDATE，实际 {result.status}")

    def test_no_pullback_keeps_confirmed(self):
        """无回踩穿越 → 应确认 CONFIRMED"""
        klines = build_long_scenario(pullback_at=None, pullback_bars=3, total=30)
        result = self.detector._check_double_bottom(
            self.pivots[0], self.pivots[1], self.pivots[2],
            klines, self.atr, "TESTUSDT", "1h"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PatternStatus.CONFIRMED,
                         f"无回踩应确认，实际 {result.status}")

    def test_pullback_outside_window_still_confirmed(self):
        """回踩穿越发生在窗口外(i=22 > 17+3) → 应仍确认"""
        # 突破 idx=16，pullback_bars=3 → 检查 i=17,18,19
        # 回踩设在 i=22，已超出窗口
        klines = build_long_scenario(pullback_at=22, pullback_bars=3, total=30)
        result = self.detector._check_double_bottom(
            self.pivots[0], self.pivots[1], self.pivots[2],
            klines, self.atr, "TESTUSDT", "1h"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PatternStatus.CONFIRMED,
                         f"回踩在窗口外应确认，实际 {result.status}")

    def test_pullback_disabled_still_confirms(self):
        """pullback_bars=0 关闭 → 不检查回踩（保留兼容性）"""
        det_off = DoubleTopBottomDetector({
            "peak_tolerance": 0.05, "min_depth": 0.03,
            "min_span": 8, "max_span": 150,
            "breakout_candles": 2, "breakout_atr_ratio": 0.5,
            "volume_ratio_min": 0.0, "max_lookahead": 30,
            "min_height_atr": 1.0, "pullback_bars": 0,
        })
        # 即使有回踩，因关闭了，状态应仍是 CONFIRMED
        klines = build_long_scenario(pullback_at=18, pullback_bars=3, total=30)
        result = det_off._check_double_bottom(
            self.pivots[0], self.pivots[1], self.pivots[2],
            klines, self.atr, "TESTUSDT", "1h"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PatternStatus.CONFIRMED,
                         f"关闭回踩应确认，实际 {result.status}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
