#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双底/双顶容差回归测试

背景：
  2026-09-01 朱哥人工标注 4 张双底图：2 张 ok（DASH 1h / DOS 1h meh）+ 2 张 bad（DASH 4h / ZKP 1h）
  bad 样本两谷价差分别是 12.3% / 7%，远超 8% 通用容差——修复方向是给双底/双顶单独设 5% 严容差。

测试：
  用模拟拐点验证 detector 在 5% 严容差下：
    - 12.3% 差 → 必须被过滤
    - 7% 差 → 必须被过滤
    - 0.8% 差（DASH 1h OK）→ 必须通过
    - 5% 差（边界）→ 必须通过（恰好等于阈值）
    - 5.1% 差（边界外）→ 必须被过滤
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from zigzag import Pivot, PivotType
from patterns.double import DoubleTopBottomDetector
from patterns.base import Direction
from market_data import Kline


def make_klines_prices(prices):
    """给定一组价格序列，生成最小可用的 Kline 列表（仅需 openTime/close）"""
    klines = []
    for i, p in enumerate(prices):
        t = 1_700_000_000_000 + i * 3_600_000  # 1h 间隔
        klines.append(Kline(
            openTime=t, open=p, high=p * 1.005, low=p * 0.995,
            close=p, volume=1000, closeTime=t + 3_600_000, quoteVolume=1000
        ))
    return klines


class TestDoubleTolerance(unittest.TestCase):
    """双底两谷价差容差边界测试"""

    def setUp(self):
        # 模拟 K 线：30 根在 110 附近波动，保证右谷后所有价格 > 100 (避免触发"右谷后创新低"过滤)
        self.klines = make_klines_prices([110 + (i % 5) - 2 for i in range(30)])
        self.atr = 1.0
        self.detector = DoubleTopBottomDetector({
            "peak_tolerance": 0.05,   # 修复后的 5% 严容差
            "min_depth": 0.03,
            "min_span": 8,
            "max_span": 150,
            "breakout_candles": 2,
            "breakout_atr_ratio": 0.5,
            "volume_ratio_min": 1.5,
            "max_lookahead": 30,
            "min_height_atr": 1.0,
        })

    def _make_pivots(self, low1_price, h_price, low2_price):
        """构造 (L, H, L) 三点滑窗，间距 8 根"""
        return [
            Pivot(index=5, price=low1_price, type=PivotType.LOW, timestamp=0),
            Pivot(index=10, price=h_price, type=PivotType.HIGH, timestamp=0),
            Pivot(index=15, price=low2_price, type=PivotType.LOW, timestamp=0),
        ]

    def _check_double_bottom(self, l1, h, l2):
        return self.detector._check_double_bottom(
            l1, h, l2, self.klines, self.atr, "TESTUSDT", "1h"
        )

    def test_diff_12_3pct_should_be_filtered(self):
        """DASH 4h bad 样本：两谷差 12.3% → 必须被过滤"""
        # l1=32.5, l2=36.5, diff=|32.5-36.5|/36.5 = 10.96%
        # 若用 max(l1,l2) 即 36.5 当分母：12.3% (用 l1=32.5 当分母)
        # 代码用 max(l1,l2) 当分母（double.py line 181）
        pivots = self._make_pivots(32.5, 40.0, 36.5)
        # diff = 4/36.5 = 10.96% > 5% → 应被过滤
        result = self._check_double_bottom(pivots[0], pivots[1], pivots[2])
        self.assertIsNone(result, f"差 10.96% 应被过滤，却返回 {result}")

    def test_diff_7pct_should_be_filtered(self):
        """ZKP 1h bad 样本：两谷差约 7% → 必须被过滤"""
        # l1=100, l2=107, diff=7/107 = 6.54% > 5%
        pivots = self._make_pivots(100.0, 110.0, 107.0)
        result = self._check_double_bottom(pivots[0], pivots[1], pivots[2])
        self.assertIsNone(result, f"差 6.54% 应被过滤，却返回 {result}")

    def test_diff_0_8pct_should_pass(self):
        """DASH 1h ok 样本：两谷差 0.8% → 必须通过价差检查"""
        # l1=100, l2=100.8, diff=0.8/100.8 = 0.79% < 5%
        # 但需要 depth>=3% min_depth → 100.8 -> 105 (4.36%) 通过
        pivots = self._make_pivots(100.0, 105.0, 100.8)
        result = self._check_double_bottom(pivots[0], pivots[1], pivots[2])
        # 可能因为其他条件（如 volume/breakout）返回 CANDIDATE 形态
        # 但至少不应因为价差被过滤
        # 真正会因 volume_ratio 0 (无成交量变化) 返回 pattern 但 status=CANDIDATE
        self.assertIsNotNone(result, "差 0.79% 应通过价差检查")

    def test_diff_exactly_5pct_passes(self):
        """边界：两谷差恰好 5% → 必须通过（不超过阈值）"""
        # l1=100, l2=105, diff=5/105 = 4.76% < 5%
        # 真正 5% 边界：l1=100, l2=100/0.95 = 105.26, diff=5.26/105.26 = 5% (恰好)
        # 验证代码用 max(l1,l2) 做分母：5/105.26 = 4.75% < 5% → 通过
        pivots = self._make_pivots(100.0, 110.0, 105.26)
        result = self._check_double_bottom(pivots[0], pivots[1], pivots[2])
        self.assertIsNotNone(result, "差 4.75% 应通过边界")

    def test_diff_5_1pct_filtered(self):
        """边界外：两谷差 5.1% → 必须被过滤"""
        # l1=100, l2=105.4, diff=5.4/105.4 = 5.12% > 5%
        pivots = self._make_pivots(100.0, 110.0, 105.4)
        result = self._check_double_bottom(pivots[0], pivots[1], pivots[2])
        self.assertIsNone(result, "差 5.12% 应被过滤")


if __name__ == "__main__":
    unittest.main(verbosity=2)
