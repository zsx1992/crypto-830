#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
C方案 (2026-09-08): peak_tolerance 随跨度缩放回归测试

背景:
  9-07 A+B 把双底/双顶两谷价差容差收严到 3%, 误报少了但把大 W 底也全拒了
  (PIEVERSE 真实谷差 7.2% / ENAUSDT 7.4% = higher-low 结构非噪声)。
  C方案: 容差随形态跨度缩放 — 近距小形态保持严值(3%), 大跨度放宽(5% cap)。

本测试锁定:
  1. 同一谷差 4.31%: span=10(近距) 拒 / span=210(大跨度) 收 → 缩放语义核心
  2. 大跨度 cap 5%: 谷差 5.21% 仍拒 (7%+ higher-low 不算标准双底)
  3. 线性过渡区 span=100: tol≈3.67%, 3.57% 收 / 4.31% 拒
  4. 旧接口兼容: 只传 peak_tolerance=0.05 → 恒定 0.05 (不做缩放, 旧行为)
  5. 双顶对称: 近距价差 4.31% 拒 / 大跨度收
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from zigzag import Pivot, PivotType
from patterns.double import DoubleTopBottomDetector
from market_data import Kline


def make_klines(n=320, price=150.0):
    """全 close=price 的平坦 K 线。检测只用 pivot.price, K 线仅服务
    prior_move/突破查找; 平坦高价保证右谷后无新低、突破即时找到。"""
    klines = []
    for i in range(n):
        t = 1_700_000_000_000 + i * 3_600_000
        klines.append(Kline(
            openTime=t, open=price, high=price * 1.005, low=price * 0.995,
            close=price, volume=1000, closeTime=t + 3_600_000, quoteVolume=1000
        ))
    return klines


def make_detector(**over):
    """容差测试专用 detector: 关掉与容差无关的闸, 聚焦 ① 容差闸。"""
    base = dict(
        min_depth=0.05,
        min_span=8,
        max_span=400,
        breakout_candles=2,
        breakout_atr_ratio=0.5,
        volume_ratio_min=0.0,       # 关量能
        max_lookahead=None,
        min_height_atr=0.0,         # 关高度闸
        require_prior_trend=False,  # 关前置趋势闸
    )
    base.update(over)
    return DoubleTopBottomDetector(base)


def piv_l(low_idx, price):
    return Pivot(index=low_idx, price=price, type=PivotType.LOW, timestamp=0)


def piv_h(hi_idx, price):
    return Pivot(index=hi_idx, price=price, type=PivotType.HIGH, timestamp=0)


class TestSpanScaledTolerance(unittest.TestCase):
    """双底/双顶容差随跨度缩放边界"""

    def setUp(self):
        self.klines = make_klines()
        self.atr = 1.0
        # 启用缩放: 3% (span<=50) ~ 5% (span>=200)
        self.det = make_detector(
            peak_tolerance=0.03,
            peak_tolerance_min=0.03,
            peak_tolerance_max=0.05,
            tol_span_lo=50,
            tol_span_hi=200,
        )

    # ---------- 核心: 同一谷差, 跨度决定收拒 ----------

    def test_same_diff_short_span_reject_long_span_accept(self):
        """谷差 4.31% (100 vs 104.5): span=10 拒(3%严) / span=210 收(5%宽)"""
        # span=10: 近距小形态, 4.31% > 3% → 拒
        short = self.det._check_double_bottom(
            piv_l(5, 100.0), piv_h(10, 130.0), piv_l(15, 104.5),
            self.klines, self.atr, "T1", "1h")
        self.assertIsNone(short, "span=10 谷差 4.31% 应被 3% 严容差拒绝")
        # span=210: 大跨度, 4.31% < 5% → 收
        long = self.det._check_double_bottom(
            piv_l(10, 100.0), piv_h(110, 130.0), piv_l(220, 104.5),
            self.klines, self.atr, "T1", "1h")
        self.assertIsNotNone(long, "span=210 谷差 4.31% 应被 5% 宽容差接受")

    def test_long_span_cap_5pct(self):
        """大跨度 cap 5%: 谷差 5.21% (100 vs 105.5) 仍拒"""
        r = self.det._check_double_bottom(
            piv_l(10, 100.0), piv_h(110, 130.0), piv_l(220, 105.5),
            self.klines, self.atr, "T2", "1h")
        self.assertIsNone(r, "span=210 谷差 5.21% > cap 5% 应拒绝 (higher-low 不算双底)")

    def test_linear_mid_span(self):
        """线性过渡区 span=100 (tol≈3.67%): 3.57% 收 / 4.31% 拒"""
        tol = self.det._tolerance_for_span(100)
        self.assertAlmostEqual(tol, 0.0367, places=3)
        # 3.57% (103.7) < 3.67% → 收
        ok = self.det._check_double_bottom(
            piv_l(10, 100.0), piv_h(60, 130.0), piv_l(110, 103.7),
            self.klines, self.atr, "T3", "1h")
        self.assertIsNotNone(ok, "span=100 谷差 3.57% < tol 3.67% 应接受")
        # 4.31% > 3.67% → 拒
        bad = self.det._check_double_bottom(
            piv_l(10, 100.0), piv_h(60, 130.0), piv_l(110, 104.5),
            self.klines, self.atr, "T3", "1h")
        self.assertIsNone(bad, "span=100 谷差 4.31% > tol 3.67% 应拒绝")

    # ---------- 旧接口兼容 ----------

    def test_legacy_peak_tolerance_constant(self):
        """只传 peak_tolerance=0.05 (旧接口): 全程恒定 5%, 不做缩放"""
        det = make_detector(peak_tolerance=0.05)
        # span=10 近距 + 谷差 4.31% → 旧行为 5% 接受
        r = det._check_double_bottom(
            piv_l(5, 100.0), piv_h(10, 130.0), piv_l(15, 104.5),
            self.klines, self.atr, "T4", "1h")
        self.assertIsNotNone(r, "旧接口 peak_tolerance=0.05 应恒定 5%, span=10 谷差 4.31% 应接受")
        # 谷差 6% 仍拒 (超 5%)
        r2 = det._check_double_bottom(
            piv_l(5, 100.0), piv_h(10, 130.0), piv_l(15, 106.0),
            self.klines, self.atr, "T4", "1h")
        self.assertIsNone(r2, "旧接口 5% 恒定容差下谷差 5.66% 应拒绝")

    # ---------- 双顶对称 ----------

    def test_double_top_symmetry(self):
        """双顶对称: 峰差 4.31% span=10 拒 / span=210 收"""
        # 双顶的 ⑤ 闸要求"右峰后不创新高", 平坦高价 K 线会误拦,
        # 故双顶用"前高后低"序列: 220 根前 close=150(高), 220 后 close=95(低)。
        kl_high_low = make_klines(320, 150.0)
        for i in range(220, 320):
            t = kl_high_low[i].openTime
            kl_high_low[i] = Kline(
                openTime=t, open=95.0, high=95.8, low=94.2,
                close=95.0, volume=1000, closeTime=t + 3_600_000, quoteVolume=1000)
        # span=10 近距 → 拒
        short = self.det._check_double_top(
            piv_h(5, 130.0), piv_l(10, 100.0), piv_h(15, 124.4),
            kl_high_low, self.atr, "T5", "1h")
        self.assertIsNone(short, "双顶 span=10 峰差 4.31% 应被拒")
        # span=210 大跨度 → 收
        long = self.det._check_double_top(
            piv_h(10, 130.0), piv_l(110, 100.0), piv_h(220, 124.4),
            kl_high_low, self.atr, "T5", "1h")
        self.assertIsNotNone(long, "双顶 span=210 峰差 4.31% 应被接受")


if __name__ == "__main__":
    unittest.main(verbosity=2)
