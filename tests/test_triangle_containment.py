# -*- coding: utf-8 -*-
"""
三角形检测器"价格包住"硬校验回归测试

金标准背景 (2026-09-09):
  DOSUSDT 4h 被画成 descending_triangle 并推送, 朱哥判:
  "中间涨了那么多, 有那么多根K线在你画的线之外, 而且不是插针,
   怎么能算三角呢?" —— 完全不是三角。
  量化: 上边界窗口 144 根里收盘越界 20.1% / 深刺(>2%) 18.1% /
  最大穿透 28.6% —— 视觉是一段独立上涨行情, 不是边界内收敛震荡。

根因: triangle 只做触点校验(每条边有 >=2 个摆动点贴线), 从不检查
中间 K 线是否被两条边界真实框住。box 已有同款硬闸(BNB/CL/LAYER
金标准后补), triangle 漏加。修复: 提取共享 base.containment_ok,
在 classify 前接入。

本测试构造"名义下降三角 + 中段大涨跑飞"的合成数据:
  修复前: descending_triangle CONFIRMED (复现 DOS 误判)
  修复后: 被拒, 无检出
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from market_data import Kline  # noqa: E402
from zigzag import find_pivots  # noqa: E402
from patterns.triangle import TriangleDetector  # noqa: E402
from patterns.box import BoxDetector  # noqa: E402
from patterns.base import containment_ok, Line, PivotType  # noqa: E402
from test_box import make_kline, PARAMS  # noqa: E402


class _P:
    """极简 Pivot 桩 (仅 containment_ok 单测用)"""
    def __init__(self, idx, price):
        self.index = idx
        self.price = price


def build_dos_like():
    """
    DOS 型伪下降三角:
      上边界 110@10 -> 108@40 -> 106@70 -> 104@100 (名义下压)
      下边界 95 水平 (idx20/50/80/110)
      但 idx55-69 中段插入一波大涨到 125 —— 远超出上边界(106-107),
      且不是单根插针而是连续多根收盘在线上 (DOS 08-30 真实形态)。
      尾部向下突破。
    """
    kl = []
    i = 0
    highs = [(10, 110.0), (40, 108.0), (70, 106.0), (100, 104.0)]
    lows = [(20, 95.0), (50, 95.0), (80, 95.0), (110, 95.0)]
    seq = sorted(highs + lows, key=lambda x: x[0])
    price = 100.0
    for idx, target in seq:
        step = (target - price) / (idx - i)
        for _ in range(idx - i):
            o = price
            price += step
            kl.append(make_kline(i, o, max(o, price) + 0.5,
                                 min(o, price) - 0.5, price))
            i += 1
    # 中段大涨跑飞: 连续 15 根 K 线 push 到 125, 收盘 124 远超上边界
    for j in range(55, 70):
        kl[j] = make_kline(j, kl[j].open, 125.0, kl[j].low, 124.0,
                           v=3000.0)
    # 尾部向下突破
    price = kl[-1].close
    for _ in range(5):
        o = price
        price -= 2.0
        kl.append(make_kline(i, o, o + 0.2, price - 0.3, price,
                             v=3000.0))
        i += 1
    return kl


class TestTriangleContainment(unittest.TestCase):
    def test_dos_like_mid_rally_rejected(self):
        """中段大涨跑飞(非插针)的伪下降三角必须被拒 (DOSUSDT 4h 回归)"""
        kl = build_dos_like()
        det = TriangleDetector()
        piv = find_pivots(kl, left=3, right=3)
        pats = det.detect(kl, piv, 1.0, "TEST", "1h")
        for p in pats:
            self.assertNotEqual(
                p.status.value, "CONFIRMED",
                f"{p.pattern_type} 中间价格整体跑飞不应被确认")
        self.assertEqual(
            pats, [],
            "中段独立大涨的伪三角应整体拒掉, 实际检出: "
            f"{[(p.pattern_type, p.status.value) for p in pats]}")

    def test_true_triangle_still_detected(self):
        """边界真正框住价格的真三角不受影响 (防误杀)"""
        kl = []
        i = 0
        # 上边界水平 110, 下边界抬升 95->104, 价格在两条线内震荡收敛
        highs = [(15, 110.0), (45, 110.0), (75, 110.0), (100, 110.0)]
        lows = [(25, 95.0), (55, 100.0), (80, 103.0), (105, 104.5)]
        seq = sorted(highs + lows, key=lambda x: x[0])
        price = 100.0
        for idx, target in seq:
            step = (target - price) / (idx - i)
            for _ in range(idx - i):
                o = price
                price += step
                # 影线不超过上下边界的名义位置 → 干净被框住
                kl.append(make_kline(i, o, max(o, price) + 0.4,
                                     min(o, price) - 0.4, price))
                i += 1
        # 向上突破 110
        price = kl[-1].close
        for _ in range(6):
            o = price
            price += 3.0
            kl.append(make_kline(i, o, price + 0.3, o - 0.2, price,
                                 v=3000.0))
            i += 1
        det = TriangleDetector()
        piv = find_pivots(kl, left=3, right=3)
        pats = det.detect(kl, piv, 1.0, "TEST", "1h")
        asc = [p for p in pats if p.pattern_type == "ascending_triangle"]
        self.assertTrue(
            asc,
            "边界框住价格的真上升三角不应被误杀, 实际: "
            f"{[(p.pattern_type, p.status.value) for p in pats]}")


class TestContainmentOkShared(unittest.TestCase):
    """base.containment_ok 共享函数的行为锁死 (box 已用它, 防迁移回归)"""

    def _klines_flat(self, n=30, high_above=0.0):
        """n 根 K 线, 价格 100; high_above>0 时全部 K 线 high 抬高该量"""
        kl = []
        for i in range(n):
            h = 100.0 + high_above
            kl.append(make_kline(i, 99.0, h, 98.0, 99.5))
        return kl

    def _line(self, start, end, price=100.0):
        return Line(_P(start, price), _P(end, price))

    def test_clean_line_ok(self):
        # K 线 [98, 100] 被夹在 lower=97 与 upper=101 之间, 无越界 → ok
        kl = []
        for i in range(30):
            kl.append(make_kline(i, 99.0, 100.0, 98.0, 99.5))
        p = {"contain_max_penetration": 0.08,
             "contain_max_close_escape": 0.15,
             "contain_max_deep_escape": 0.15}
        upper = self._line(0, 29, 101.0)
        lower = self._line(0, 29, 97.0)
        self.assertTrue(containment_ok(kl, upper, lower, p))

    def test_single_wick_penetration_rejected(self):
        """单根深插针(>8%) 必须拒 —— box/triangle 同口径"""
        kl = []
        for i in range(30):
            kl.append(make_kline(i, 99.0, 100.0, 98.0, 99.5))
        # 把 idx15 的 high 拉到 115 (上边界101, 穿透 ~14%)
        kl[15] = make_kline(15, 99.0, 115.0, 98.0, 99.5)
        p = {"contain_max_penetration": 0.08,
             "contain_max_close_escape": 0.15,
             "contain_max_deep_escape": 0.15}
        upper = self._line(0, 29, 101.0)
        lower = self._line(0, 29, 97.0)
        self.assertFalse(containment_ok(kl, upper, lower, p),
                         "单根深刺 >8% 应被拒")

    def test_close_escape_ratio_rejected(self):
        """大量收盘跑出边界外(占比超 15%) 必须拒 —— DOS 主因"""
        kl = []
        n = 30
        for i in range(n):
            # 前 10 根正常, 后 20 根收盘 105 (上边界101, 越界 20/30=67%)
            c = 105.0 if i >= 10 else 99.5
            h = 105.5 if i >= 10 else 100.0
            kl.append(make_kline(i, 99.0, h, 98.0, c))
        p = {"contain_max_penetration": 0.08,
             "contain_max_close_escape": 0.15,
             "contain_max_deep_escape": 0.15}
        upper = self._line(0, 29, 101.0)
        lower = self._line(0, 29, 97.0)
        self.assertFalse(containment_ok(kl, upper, lower, p),
                         "67% 收盘在线外应被拒")


if __name__ == "__main__":
    unittest.main(verbosity=2)
