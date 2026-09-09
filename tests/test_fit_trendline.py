#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fit_trendline 触点统计区间修复回归测试 (2026-09-09)

背景（用户金标准 ZBTUSDT 4h）:
  08-07 插针线 0.20774@101 → 0.09385@145，拟合后外推到 idx>145 得到
  负价格。旧代码触点统计对区间外所有点执行
  abs(p.price - expected)/expected <= tolerance —— expected 为负数时
  该式恒为负数、恒成立，把区间外十几个点全部误计为"触点"。
  于是这条只真触 2 点的插针线拿到 touches=13，抢占真实收敛边界
  （真实上升三角上沿因此无人认领，形态漏检）。

修复:
  触点只统计线段两端界定的区间 [p1.index, p2.index] 内的点 ——
  趋势线端点之外不存在"触点"的几何意义。

测试:
  - 插针线(仅真触2点, 区间外外推为负) 不再靠误触取胜
  - 区间内多点共线的真实边界正常胜出
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from zigzag import Pivot, PivotType  # noqa: E402
from patterns.base import fit_trendline  # noqa: E402


def hp(idx, price):
    return Pivot(index=idx, price=price, type=PivotType.HIGH, timestamp=0)


class TestFitTrendlineTouchWindow(unittest.TestCase):
    """触点统计必须限定在线段区间内"""

    def test_extrapolated_negative_points_not_counted(self):
        """
        ZBT 复现场景:
          插针线 (0@1000 → 20@100), 外推到 idx>=30 价格变负。
          旧代码把 idx 30/40/50 全部误计为触点 → touches=5 胜出(错)。
          修复后区间外不计 → touches=2, 输给真实共线边界 (20→50, touches=4)。
        """
        highs = [
            hp(0, 1000.0),   # 插针: 极远高点
            hp(20, 100.0),   # 插针线终点
            hp(30, 99.0),    # 真实边界起点附近 (共线段内)
            hp(40, 98.0),
            hp(50, 97.0),
        ]
        # 真实边界 (20→50): slope=-0.1/根, 触点 = {20,30,40,50} 四个
        #   校验: @30 expected=99.0, @40 expected=98.0, @50 expected=97.0
        line, touches = fit_trendline(highs, PivotType.HIGH,
                                      min_touches=2, tolerance=0.02,
                                      min_span=5)
        self.assertIsNotNone(line, "应能拟合出边界")
        self.assertEqual(touches, 4,
                         "真实共线边界应有 4 个真触点(区间内)")
        self.assertEqual(line.p1.index, 20,
                         "胜出的应是真实边界(起点idx=20), 而非插针线(起点idx=0)")
        self.assertEqual(line.p2.index, 50)

    def test_short_real_line_beats_fake_long_touch_line(self):
        """
        陡降线 (0→60) 只真触 2 点, 外推区间外的点不贴线 → 不应虚增触点;
        真实缓升线 (70→90) 3 触点应胜出。
        """
        highs = [
            hp(0, 100.0),
            hp(60, 10.0),    # 陡降线端点 (slope=-1.5/根), 外推 idx>60 变负
            hp(70, 20.0),    # 价格跳回高位 → 不在陡降线外推路径上
            hp(80, 21.0),    # 真实缓升线 (70→90): slope≈+0.1/根
            hp(90, 22.0),
        ]
        # 真实线 (70→90) 触点 {70,80,90}=3; 陡降线 (0→60) 触点 {0,60}=2
        # 区间外点 (70/80/90) 若被陡降线误触(外推为负), 旧代码会给陡降线虚增到 4~5
        line, touches = fit_trendline(highs, PivotType.HIGH,
                                      min_touches=2, tolerance=0.02,
                                      min_span=5)
        self.assertIsNotNone(line)
        self.assertEqual(touches, 3, "真实缓升线应 3 触点胜出")
        self.assertEqual(line.p1.index, 70,
                         "陡降线仅真触 2 点, 不应因外推虚增触点而胜出")
        self.assertEqual(line.p2.index, 90)

    def test_out_of_window_pivot_ignored_even_if_close(self):
        """
        区间外点价格高 → 陡降线外推到该 idx 为负值。
        旧代码 abs(p.price - expected)/expected 除以负数恒成立 → 误触。
        修复后区间外不计, 陡降线 touches 恒为 2 (不虚增)。
        """
        # 线 (0→10): slope=-9/根, @20 expected=100-180=-80 (负!)
        highs = [
            hp(0, 100.0),
            hp(10, 10.0),
            hp(20, 200.0),   # 区间外, 外推 expected=-80 → 旧代码误触
        ]
        line, touches = fit_trendline(highs, PivotType.HIGH,
                                      min_touches=2, tolerance=0.02,
                                      min_span=5)
        self.assertIsNotNone(line)
        self.assertEqual(touches, 2, "区间外点(外推为负)不得虚增触点")
        # 不应选到被虚增到 3 触点的 (0→10); 三条候选全为真 2 触点时
        # tie-break 取跨度大者 → (0→20)
        self.assertEqual(line.p2.index, 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
