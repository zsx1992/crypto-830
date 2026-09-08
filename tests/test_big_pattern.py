#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大形态回归测试 (2026-09-08 朱哥金标准反馈)

两张观察流图暴露扫描器看不见"大 W 底"的两个结构性死因：
  1. PIEVERSE 1h: 大 W 底两谷相距 ~168 根 > 旧 max_span 150 → 检测器直接拒绝
     （右侧 ~1.005 局部小 W 底被推，真正的大 W 底 颈1.15→1.32 反而是主信号）
  2. ENAUSDT 1h: 大 W 底跨度 ~100 根能过 span 闸，但右谷后 ~190 根才破颈线
     0.190 → 旧 max_lookahead=30 只搜 30 根 → 永远 CANDIDATE → 静默丢弃

修复：config span.double_top_max 150→350 / head_shoulders_max 220→420；
      double/head_shoulders 的 max_lookahead 30→None（搜到数据末端，
      由 freshness + require_intact_breakout + pullback_bars 兜底）。

本测试用合成 K 线锁死这两个行为，防止未来参数被改回去。
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from zigzag import Pivot, PivotType
from patterns.double import DoubleTopBottomDetector
from patterns.base import PatternStatus
from market_data import Kline


def make_klines_prices(prices):
    """价格序列 → Kline 列表（1h 间隔，volume 恒定）"""
    klines = []
    for i, p in enumerate(prices):
        t = 1_700_000_000_000 + i * 3_600_000
        klines.append(Kline(
            openTime=t, open=p, high=p * 1.005, low=p * 0.995,
            close=p, volume=1000, closeTime=t + 3_600_000, quoteVolume=1000
        ))
    return klines


def build_wide_w_scenario(breakout_at=None, l2_index=80, total=320):
    """
    大 W 底合成场景（ENAUSDT 形态骨架）：
      i<30     : 下跌 130→101（前置趋势，满足 require_prior_trend）
      i=40     : l1 = 100（左谷）
      i=60     : h1 = 110（中峰 = 颈线）
      i=80     : l2 = 101（右谷，与左谷差 1%，通过容差）
      i=81~N   : 长期横盘 100~108（右谷后 N 根内不破颈线 110）
      breakout_at: 若指定，则该根 close 跳 112 突破；不指定则永不突破
    """
    prices = []
    for i in range(total):
        if i < 30:
            prices.append(130 - i)              # 130 → 101 下跌
        elif i == 40:
            prices.append(100.0)                # 左谷
        elif i == 60:
            prices.append(110.0)                # 中峰颈线
        elif i == 80:
            prices.append(101.0)                # 右谷
        elif breakout_at is not None and i >= breakout_at:
            prices.append(112.0 if i < breakout_at + 2 else 113.0)
        else:
            prices.append(105.0 if i % 2 else 104.0)   # 横盘 104~105，不破 110
    return make_klines_prices(prices)


def make_detector(max_span=350, max_lookahead=None, pullback_bars=0):
    return DoubleTopBottomDetector({
        "peak_tolerance": 0.03,
        "min_depth": 0.03,
        "min_span": 30,
        "max_span": max_span,
        "breakout_candles": 2,
        "breakout_atr_ratio": 0.5,
        "volume_ratio_min": 0.0,     # 测试聚焦结构/突破语义，关成交量
        "max_lookahead": max_lookahead,
        "min_height_atr": 1.0,
        "pullback_bars": pullback_bars,
        "require_prior_trend": True,   # 保留真实闸门：前面有下跌段才有效
    })


class TestBigPattern(unittest.TestCase):
    """大跨度/晚突破 W 底：修复后必须能进管线"""

    def test_wide_span_passes_with_max_span_350(self):
        """两谷相距 180 根（PIEVERSE 168 根同类）：max_span=350 必须放行"""
        # l1@40, l2=80 → span=40；这里再造一个 l2 更远的场景
        klines = make_klines_prices([100.0] * 400)
        # 直接用 _check_double_bottom 验证 span 闸：l1 idx=40, l2 idx=220 → span 180
        det = make_detector(max_span=350)
        l1 = Pivot(index=40, price=100.0, type=PivotType.LOW, timestamp=0)
        h1 = Pivot(index=120, price=112.0, type=PivotType.HIGH, timestamp=0)
        l2 = Pivot(index=220, price=101.0, type=PivotType.LOW, timestamp=0)
        # 前置趋势：K线全 100，prior_move 需要 l1 前下跌段 → 关掉 trend 闸单独测 span
        det.params["require_prior_trend"] = False
        result = det._check_double_bottom(l1, h1, l2, klines, 1.0,
                                          "TESTUSDT", "1h")
        # span=180 ≤ 350 应返回形态（哪怕 CANDIDATE/无突破）
        self.assertIsNotNone(result,
                             "span=180 在 max_span=350 下应通过 span 闸")

    def test_wide_span_rejected_with_old_max_span_150(self):
        """对照组：同样的 180 根间距在旧 max_span=150 下必须被拒（回归锁）"""
        klines = make_klines_prices([100.0] * 400)
        det = make_detector(max_span=150)
        det.params["require_prior_trend"] = False
        l1 = Pivot(index=40, price=100.0, type=PivotType.LOW, timestamp=0)
        h1 = Pivot(index=120, price=112.0, type=PivotType.HIGH, timestamp=0)
        l2 = Pivot(index=220, price=101.0, type=PivotType.LOW, timestamp=0)
        result = det._check_double_bottom(l1, h1, l2, klines, 1.0,
                                          "TESTUSDT", "1h")
        self.assertIsNone(result, "span=180 > 150 应被旧上限拒绝")

    def test_late_breakout_confirms_with_unlimited_lookahead(self):
        """右谷后 ~190 根才突破（ENAUSDT 场景）：max_lookahead=None 必须 CONFIRMED"""
        klines = build_wide_w_scenario(breakout_at=270, l2_index=80, total=320)
        det = make_detector(max_span=350, max_lookahead=None)
        l1 = Pivot(index=40, price=100.0, type=PivotType.LOW, timestamp=0)
        h1 = Pivot(index=60, price=110.0, type=PivotType.HIGH, timestamp=0)
        l2 = Pivot(index=80, price=101.0, type=PivotType.LOW, timestamp=0)
        result = det._check_double_bottom(l1, h1, l2, klines, 1.0,
                                          "TESTUSDT", "1h")
        self.assertIsNotNone(result, "晚突破在无限 lookahead 下应找到突破")
        self.assertEqual(result.status, PatternStatus.CONFIRMED,
                         f"右谷后 190 根突破应 CONFIRMED，实际 {result.status}")

    def test_late_breakout_stays_candidate_with_old_lookahead_30(self):
        """对照组：同样场景 max_lookahead=30 → CANDIDATE（旧行为回归锁）"""
        klines = build_wide_w_scenario(breakout_at=270, l2_index=80, total=320)
        det = make_detector(max_span=350, max_lookahead=30)
        l1 = Pivot(index=40, price=100.0, type=PivotType.LOW, timestamp=0)
        h1 = Pivot(index=60, price=110.0, type=PivotType.HIGH, timestamp=0)
        l2 = Pivot(index=80, price=101.0, type=PivotType.LOW, timestamp=0)
        result = det._check_double_bottom(l1, h1, l2, klines, 1.0,
                                          "TESTUSDT", "1h")
        self.assertIsNotNone(result)
        self.assertEqual(result.status, PatternStatus.CANDIDATE,
                         "30 根内无突破应停留 CANDIDATE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
