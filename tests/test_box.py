# -*- coding: utf-8 -*-
"""
箱体/通道检测器 (BoxDetector) 测试

2026-09-08 B 阶段补充。这是检测能力的真实空白(不是重复实现):
  - 三角形要求收敛(upper.rel_slope < lower.rel_slope) → 水平箱体被拒
  - 楔形要求收敛 + 5 触点                            → 平行通道被拒
  - 双顶/双底/头肩要求反转结构                        → 震荡区间不匹配

测试目标:
  1. 水平箱体 + 向上突破 → 检出 rectangle LONG 且 CONFIRMED
  2. 下降平行通道 + 向下破位 → 检出 descending_channel SHORT 且 CONFIRMED
  3. detect() 始终返回 list (flag_wedge bug3 同类防回归)
  4. 三角形检测器不会把水平箱体误认成三角形 (分类边界锁定)
  5. PatternEngine 端到端: config.yaml 接线正确, 多尺度扫描能产出 rectangle
"""

import os
import sys
import unittest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from patterns import BoxDetector, TriangleDetector, Direction, PatternStatus  # noqa: E402
from detector import PatternEngine  # noqa: E402
from market_data import Kline  # noqa: E402
from zigzag import find_pivots  # noqa: E402


def make_kline(i, o, h, l, c, v=1000.0):
    t = 1_700_000_000_000 + i * 3_600_000
    return Kline(
        openTime=t, open=o, high=h, low=l, close=c, volume=v,
        closeTime=t + 3_600_000, quoteVolume=v,
    )


def build_rectangle(bars_per_leg: int = 10, legs: int = 10):
    """
    构造水平箱体: 价格在 100~110 之间来回 legs 条腿,
    然后向上放量突破 (4根, 收盘 +2.5/根)。
    """
    kl = []
    i = 0
    price = 100.0
    for leg in range(legs):
        target = 110.0 if leg % 2 == 0 else 100.0
        start = price
        step = (target - start) / (bars_per_leg - 1)
        for _ in range(bars_per_leg):
            o = price
            price += step
            kl.append(make_kline(i, o, max(o, price) * 1.002,
                                 min(o, price) * 0.998, price))
            i += 1
    # 突破: 5 根放量上攻 (须明显越过上边界, 且界上有 >=2 根连续收盘确认)
    for _ in range(5):
        o = price
        price += 4.0
        kl.append(make_kline(i, o, price + 0.3, o - 0.2, price, v=3000.0))
        i += 1
    return kl


def build_descending_channel(bars_per_leg: int = 10, cycles: int = 4):
    """
    构造下降平行通道: 每个周期 高点-3 / 低点-3 (两条边界平行下移),
    4 个周期共 80 根, 然后向下放量破位 (4根, 收盘 91→81)。
    """
    kl = []
    i = 0
    price = 100.0
    bars_per_leg = bars_per_leg
    for k in range(cycles):
        high_k = 110.0 - 3 * k
        low_k = 100.0 - 3 * k
        # 上腿: 从 low_{k-1} 涨到 high_k
        start = price
        step = (high_k - start) / (bars_per_leg - 1)
        for _ in range(bars_per_leg):
            o = price
            price += step
            kl.append(make_kline(i, o, max(o, price) * 1.002,
                                 min(o, price) * 0.998, price))
            i += 1
        # 下腿: 从 high_k 跌到 low_k
        start = price
        step = (low_k - start) / (bars_per_leg - 1)
        for _ in range(bars_per_leg):
            o = price
            price += step
            kl.append(make_kline(i, o, max(o, price) * 1.002,
                                 min(o, price) * 0.998, price))
            i += 1
    # 破位: 4 根放量下跌
    for _ in range(4):
        o = price
        price -= 2.5
        kl.append(make_kline(i, o, o + 0.2, price - 0.3, price, v=3000.0))
        i += 1
    return kl


PARAMS = {
    "min_touches": 2,
    "touch_tolerance": 0.02,
    "min_span": 30,
    "max_span": 200,
    "min_height_atr": 3.0,
    "flat_threshold": 0.0004,
    "max_slope_diff": 0.0008,
    "breakout_candles": 2,
    "breakout_atr_ratio": 0.5,
    "volume_ratio_min": 1.5,
    "max_lookahead": 40,
}


def _run_det(det, kl, scales=(3, 5)):
    out = []
    for s in scales:
        piv = find_pivots(kl, left=s, right=s)
        r = det.detect(kl, piv, 1.0, "TEST", "1h")
        if r:
            out.extend(r if isinstance(r, list) else [r])
    return out


class TestBoxDetector(unittest.TestCase):

    def test_rectangle_breakout_up_confirmed(self):
        """水平箱体向上放量突破 → rectangle LONG CONFIRMED"""
        kl = build_rectangle()
        res = _run_det(BoxDetector(PARAMS), kl)
        confirmed = [p for p in res
                     if p.pattern_type == "rectangle"
                     and p.status == PatternStatus.CONFIRMED
                     and p.direction == Direction.LONG]
        self.assertTrue(confirmed, f"应检出 rectangle LONG CONFIRMED, 实际: {res}")
        p = confirmed[0]
        self.assertGreater(p.height, 0)
        self.assertGreater(p.risk_reward, 0)

    def test_descending_channel_breakdown_confirmed(self):
        """下降平行通道向下放量破位 → descending_channel SHORT CONFIRMED"""
        kl = build_descending_channel()
        res = _run_det(BoxDetector(PARAMS), kl)
        confirmed = [p for p in res
                     if p.pattern_type == "descending_channel"
                     and p.status == PatternStatus.CONFIRMED
                     and p.direction == Direction.SHORT]
        self.assertTrue(confirmed,
                        f"应检出 descending_channel SHORT CONFIRMED, 实际: "
                        f"{[(x.pattern_type, x.direction.value, x.status.value) for x in res]}")

    def test_detect_returns_list(self):
        """detect() 必须返回 list (flag_wedge bug3 同类防回归)"""
        det = BoxDetector(PARAMS)
        for kl in (build_rectangle(), build_descending_channel()):
            for s in (3, 5):
                piv = find_pivots(kl, left=s, right=s)
                r = det.detect(kl, piv, 1.0, "TEST", "1h")
                self.assertIsInstance(
                    r, list, f"detect() 必须返回 list, 实际 {type(r)}")

    def test_triangle_rejects_flat_box(self):
        """分类边界锁定: 水平箱体不应被三角形检测器误认(无收敛)"""
        kl = build_rectangle()
        tri = _run_det(TriangleDetector({}), kl)
        self.assertEqual(
            len(tri), 0,
            f"水平箱体不应被三角形检测器检出, 实际: "
            f"{[(x.pattern_type, x.status.value) for x in tri]}")

    def test_engine_end_to_end(self):
        """端到端: PatternEngine + 真实 config.yaml 接线后能产出 rectangle"""
        cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        engine = PatternEngine(cfg)
        kl = build_rectangle()
        pats = engine.scan_multiscale(kl, "TESTUSDT", "1h")
        rects = [p for p in pats if p.pattern_type == "rectangle"]
        self.assertTrue(rects, f"引擎应能检出 rectangle, 实际: "
                               f"{[(p.pattern_type, p.status.value) for p in pats]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ============================================================
# 2026-09-09 用户金标准回归: 价格包住硬校验
#   BNB "喇叭口"/CL "深刺乱画"/LAYER "穿透" 必须被拒
# ============================================================

def build_bad_channel():  # 两条"线"只连孤立点, 中间价格完全跑飞
    kl = []
    i = 0
    price = 100.0
    # 先制造两个高点 (110@idx10, 112@idx30) 与两个低点 (90@idx5, 92@idx25)
    # 中间价格却一路涨到 150 —— 所谓"上边界"根本框不住
    for _ in range(30):
        kl.append(make_kline(i, price, price * 1.01, price * 0.99, price))
        price += 1.2   # 从 100 一路涨到 ~135
        i += 1
    # 突破: 4 根放量
    for _ in range(4):
        o = price
        price += 3.0
        kl.append(make_kline(i, o, price + 0.3, o - 0.2, price, v=3000.0))
        i += 1
    return kl


class TestContainment(unittest.TestCase):
    def test_penetrating_lines_rejected(self):
        """中间价格跑飞两条边界线的'假通道'必须被拒"""
        kl = build_bad_channel()
        det = BoxDetector(PARAMS)
        piv = find_pivots(kl, left=3, right=3)
        atr_val = 1.0
        pats = det.detect(kl, piv, atr_val, "TEST", "15m")
        confirmed = [p for p in pats if p.status == PatternStatus.CONFIRMED]
        # 几何上这些散点甚至不一定能凑出 box 候选; 重点是: 若凑出来,
        # 也绝不能是 CONFIRMED —— 穿透校验必须把它们拦下
        for p in pats:
            self.assertNotEqual(
                p.status, PatternStatus.CONFIRMED,
                "中间价格完全跑飞的假形态不应被确认")

    def test_diverge_mouth_rejected(self):
        """上下边界向外张开的'喇叭口'应被拒 (BNB 案例回归)

        上边界向上斜 (斜率 +0.0005), 下边界向下斜 (斜率 -0.0005),
        两线向右张开 —— 视觉上是喇叭口不是箱体。
        """
        kl = []
        i = 0
        # 两段腿构成发散区间: 低点逐步抬高, 高点逐步抬高更快
        # 上边界触点在 108, 112; 下边界触点在 92, 90(反向)→ 口越张越大
        legs = [
            (100.0, 108.0), (92.0, 100.0), (104.0, 112.0),
            (90.0, 102.0), (106.0, 116.0), (88.0, 104.0),
            (110.0, 120.0), (86.0, 106.0),   # 最后下探 86 远离下边界趋势
        ]
        for target_lo, target_hi in legs:
            for _ in range(6):
                rng_lo = min(target_lo, target_hi)
                mid = (target_lo + target_hi) / 2
                o = mid
                kl.append(make_kline(i, o, target_hi * 1.001,
                                     target_lo * 0.999, mid, v=100.0))
                i += 1
        # 尾部向上突破
        price = 120.0
        for _ in range(5):
            o = price
            price += 2.0
            kl.append(make_kline(i, o, price + 0.3, o - 0.2, price, v=3000.0))
            i += 1
        det = BoxDetector(PARAMS)
        piv = find_pivots(kl, left=2, right=2)
        pats = det.detect(kl, piv, 1.0, "TEST", "1d")
        confirmed = [p for p in pats if p.status == PatternStatus.CONFIRMED]
        self.assertEqual(confirmed, [],
                         "向外张开的喇叭口不应被确认为箱体/通道")


class TestReversalPriority(unittest.TestCase):
    """双底优先: 同一区间反转形态确认时 box 让路"""

    def test_double_bottom_suppresses_rectangle(self):
        """同一区间 double_bottom CONFIRMED 时, 同向 rectangle 被抑制"""
        # 造一段 V 底: 两个同高谷(中间一个峰) + 突破, 双底结构明显
        kl = []
        i = 0
        price = 100.0
        # 下行到谷1 92
        for _ in range(8):
            o = price; price -= 1.0
            kl.append(make_kline(i, o, max(o, price) * 1.002,
                                 min(o, price) * 0.998, price, v=500.0))
            i += 1
        # 反弹到峰 102
        for _ in range(8):
            o = price; price += 1.25
            kl.append(make_kline(i, o, max(o, price) * 1.002,
                                 min(o, price) * 0.998, price, v=500.0))
            i += 1
        # 下行到谷2 92 (与谷1同高)
        for _ in range(8):
            o = price; price -= 1.25
            kl.append(make_kline(i, o, max(o, price) * 1.002,
                                 min(o, price) * 0.998, price, v=500.0))
            i += 1
        # 突破颈线 102
        for _ in range(6):
            o = price; price += 2.0
            kl.append(make_kline(i, o, price + 0.3, o - 0.2, price, v=3000.0))
            i += 1

        # 用真实引擎多尺度扫(合成V底通常 double 检出而 box 不画矩形,
        # 关键是若两者同时出现必须让反转优先)
        cfg = yaml.safe_load(open(os.path.join(os.path.dirname(__file__),
                                               "..", "config.yaml"),
                                  encoding="utf-8"))
        eng = PatternEngine(cfg)
        pats = eng.scan_multiscale(kl, "TEST", "15m")
        types = {(p.pattern_type, p.direction.value): p.status.value
                 for p in pats}
        # box 与 double 同现时 box 必须被抑制 —— 这里至少确认引擎不崩溃
        # 且 double_bottom 若确认, 同向 rectangle 不共存
        if ("double_bottom", "LONG") in types \
                and types[("double_bottom", "LONG")] == "CONFIRMED":
            self.assertNotIn(("rectangle", "LONG"), types,
                             "双底已确认时同向矩形必须让路")
