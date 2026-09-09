#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双顶/双底方向性硬闸回归测试 (2026-09-09, 用户金标准 CBRS 1h / BNB)

背景：
  CBRSUSDT 1h 被判 double_top 推送：左顶 216.65 / 右顶 222.16，
  右峰比左峰高 2.54% —— |价差| 在 3% 容差内通过，但方向是 higher-high
  （多方仍在推动 = 趋势延续），不是"M 顶两次冲高失败"。用户判"明显不是双顶"。
  BNB 镜像：谷2 537 比谷1 569 低 5.7%（lower-low = 下跌延续），不是 W 底。

测试：
  - 双顶右峰比左峰高 >1.5% → 必须被拒
  - 双顶右峰略低（经典 M 顶）→ 必须放行
  - 双底右谷比左谷低 >1.5% → 必须被拒
  - 双底右谷略高（经典 W 底）→ 必须放行
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from zigzag import Pivot, PivotType  # noqa: E402
from patterns.double import DoubleTopBottomDetector  # noqa: E402
from market_data import Kline  # noqa: E402


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


class TestDoubleDirection(unittest.TestCase):
    """方向性硬闸：higher-high / lower-low 不是反转形态"""

    def setUp(self):
        self.klines = make_klines_prices([110 + (i % 5) - 2 for i in range(40)])
        self.atr = 1.0
        self.detector = DoubleTopBottomDetector({
            "peak_tolerance": 0.05,
            "peak_overshoot_max": 0.02,      # 右峰最多比左峰高 2%
            "trough_undershoot_max": 0.03,   # 右谷最多比左谷低 3%
            "min_depth": 0.03,
            "min_span": 8,
            "max_span": 150,
            "breakout_candles": 2,
            "breakout_atr_ratio": 0.5,
            "volume_ratio_min": 1.5,
            "max_lookahead": 30,
            "min_height_atr": 1.0,
            "require_prior_trend": False,      # 本测试聚焦方向闸
        })

    # ---- 双顶 ----

    def _check_top(self, h1, l1, h2):
        return self.detector._check_double_top(
            Pivot(index=5, price=h1, type=PivotType.HIGH, timestamp=0),
            Pivot(index=10, price=l1, type=PivotType.LOW, timestamp=0),
            Pivot(index=15, price=h2, type=PivotType.HIGH, timestamp=0),
            self.klines, self.atr, "TESTUSDT", "1h",
        )

    def test_higher_second_peak_rejected(self):
        """CBRS 场景: 右顶比左顶高 2.54% → 必须被拒"""
        # h1=216.65, h2=222.16 (高 2.54% > 2%) → None
        result = self._check_top(216.65, 205.0, 222.16)
        self.assertIsNone(result, "右峰高 2.54% 的 higher-high 不应判为双顶")

    def test_classic_m_top_passes(self):
        """经典 M 顶: 右顶比左顶低 (两次冲高失败) → 放行"""
        # pivot 价格远高于 K 线(110附近): 右峰后价格更低 → 不触发"峰后创新高"。
        # h1=150, l1=140, h2=149 (右顶略低, 方向闸放行; 后续闸另判)
        result = self._check_top(150.0, 140.0, 149.0)
        self.assertIsNotNone(result, "经典 M 顶 (右顶略低) 应通过方向闸")

    def test_slightly_higher_peak_passes(self):
        """右顶仅高 1% (< 2% 容差) → 仍放行"""
        result = self._check_top(150.0, 140.0, 151.5)
        self.assertIsNotNone(result, "右顶高 1% 在容差内应放行")

    # ---- 双底 ----

    def _check_bottom(self, l1, h1, l2):
        return self.detector._check_double_bottom(
            Pivot(index=5, price=l1, type=PivotType.LOW, timestamp=0),
            Pivot(index=10, price=h1, type=PivotType.HIGH, timestamp=0),
            Pivot(index=15, price=l2, type=PivotType.LOW, timestamp=0),
            self.klines, self.atr, "TESTUSDT", "1h",
        )

    def test_lower_second_trough_rejected(self):
        """BNB 场景: 右谷比左谷低 6% (> 3%) → 必须被拒"""
        result = self._check_bottom(100.0, 110.0, 94.0)   # 低 6%
        self.assertIsNone(result, "右谷低 6% 的 lower-low 不应判为 W 底")

    def test_ray_like_slightly_lower_trough_passes(self):
        """RAY 场景: 右谷低 1.95% (< 3% 容差) → 放行 (线上已推真信号)"""
        # RAYUSDT 1d 谷1 0.5422 / 谷2 0.5316 (低 1.95%) 是突破量 48x 的真 W 底
        result = self._check_bottom(100.0, 110.0, 98.05)   # 低 1.95%
        self.assertIsNotNone(result, "右谷低 1.95% 在容差内应放行 (RAY 场景)")

    def test_classic_w_bottom_passes(self):
        """经典 W 底: 右谷比左谷高 (两次探底成功) → 放行"""
        # pivot 低于 K 线(110附近)= 右谷后价格不创新低, 类似 tolerance 测试。
        # l1=100, h=105, l2=100.5 (右谷略高, 方向闸放行; 后续闸另判)
        result = self._check_bottom(100.0, 105.0, 100.5)
        self.assertIsNotNone(result, "经典 W 底应通过方向闸")


if __name__ == "__main__":
    unittest.main(verbosity=2)
