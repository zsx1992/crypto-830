# -*- coding: utf-8 -*-
"""
旗形/楔形检测器回归测试

2026-09-08 修复三个 bug 后补的锁定测试:
  bug1  detector.py 把 config 的 flag_pole_min_bars(3) 当成 pole_max_bars 传入,
        等于要求旗杆 3 根内涨完 + 只搜最近 9 根 → 旗形全线漏检
  bug2  flag_body_min/max 在 config 有值却从未传入, 一直用 DEFAULT 的 5/25
        (25 根在 1h 上仅 1 天, 教科书旗形 1~3 天整理装不下)
  bug3  FlagDetector/WedgeDetector.detect() 有 8 处 `return pattern`,
        与其余路径 `return results`(list) 类型不一致 → 调用方崩溃或漏形态

测试目标:
  1. 牛市旗形/熊市旗形能被检出, 方向正确
  2. detect() 始终返回 list (bug3 防回归)
  3. 旧参数(pole_max_bars=3, body_max=25)确实漏检 → 证明 bug1/2 真实存在
"""

import os
import sys
import math
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from patterns import FlagDetector, WedgeDetector, Direction, PatternStatus  # noqa: E402
from market_data import Kline  # noqa: E402
from zigzag import Pivot, PivotType, find_pivots  # noqa: E402


def make_kline(i, o, h, l, c, v=1000.0):
    t = 1_700_000_000_000 + i * 3_600_000
    return Kline(
        openTime=t, open=o, high=h, low=l, close=c, volume=v,
        closeTime=t + 3_600_000, quoteVolume=v,
    )


def _build_flag(direction_up: bool, body_n: int = 14):
    """
    构造旗形 (旗杆 + 通道内震荡的旗身 + 突破)。

    注意旗身必须在通道内【震荡】而不是单调递降 —— 单调序列 zigzag 只能
    找到首尾 2 个摆动点, 过不了 len(body_pivots) >= 4 这一闸。
    """
    kl = []
    i = 0
    price = 100.0
    # 旗杆: 10 根, 幅度 ~18%
    for _ in range(10):
        o = price
        price *= 1.017 if direction_up else 0.983
        kl.append(make_kline(i, o, max(o, price) * 1.002,
                             min(o, price) * 0.998, price))
        i += 1
    pole_end_price = price

    # 旗身: 通道整体与旗杆【反向】倾斜, 价格在通道内正弦震荡
    pole_top = max(100.0, pole_end_price)
    pole_bot = min(100.0, pole_end_price)
    if direction_up:
        # 牛市旗: 旗身通道【下倾】(与上涨旗杆反向)
        u0, l0 = pole_top, pole_top - 3.0
        u1, l1 = pole_top - 4.0, pole_top - 7.0
    else:
        # 熊市旗: 旗身通道【上倾】(与下跌旗杆反向)
        u0, l0 = pole_bot + 3.0, pole_bot
        u1, l1 = pole_bot + 7.0, pole_bot + 4.0
    for step in range(body_n):
        t = step / (body_n - 1)
        U = u0 + (u1 - u0) * t
        L = l0 + (l1 - l0) * t
        wave = (math.sin(2 * math.pi * step / 4.0) + 1) / 2   # 0..1
        mid = L + (U - L) * wave
        half = 0.35
        o = mid - half if step % 2 == 0 else mid + half
        c = mid + half if step % 2 == 0 else mid - half
        kl.append(make_kline(i, o, max(o, c) + 0.25, min(o, c) - 0.25, c))
        i += 1

    # 突破: 沿旗杆方向冲出通道
    if direction_up:
        br = kl[-1].high
        for _ in range(3):
            o = br
            br += 1.5
            kl.append(make_kline(i, o, br + 0.3, o - 0.2, br, v=3000.0))
            i += 1
    else:
        br = kl[-1].low
        for _ in range(3):
            o = br
            br -= 1.5
            kl.append(make_kline(i, o, o + 0.2, br - 0.3, br, v=3000.0))
            i += 1
    return kl


def build_bull_flag():
    """牛市旗形: 急涨 → 下倾平行通道 → 向上突破"""
    return _build_flag(direction_up=True)


def build_bear_flag():
    """熊市旗形: 急跌 → 上倾平行通道 → 向下破位"""
    return _build_flag(direction_up=False)


# 修复后的参数 (与 config.yaml 一致)
FIXED = {
    "pole_min_move": 0.03,
    "pole_max_bars": 15,
    "flag_body_min": 5,
    "flag_body_max": 60,
    "parallel_tolerance": 0.0015,
    "min_touches": 2,
    "touch_tolerance": 0.02,
    "breakout_candles": 2,
    "breakout_atr_ratio": 0.5,
    "volume_ratio_min": 1.5,
    "max_lookahead": 20,
    "min_height_atr": 1.5,
}

# 修复前的参数 (bug 时期的实际生效值)
BROKEN = dict(FIXED)
BROKEN["pole_max_bars"] = 3      # bug1: 误用 flag_pole_min_bars
BROKEN["flag_body_max"] = 25     # bug2: config 值没传, 用 DEFAULT


class TestFlagDetector(unittest.TestCase):

    def setUp(self):
        self.kl_bull = build_bull_flag()
        self.kl_bear = build_bear_flag()

    def _run(self, kl, params):
        det = FlagDetector(params)
        out = []
        for s in (2, 3, 5):
            piv = find_pivots(kl, left=s, right=s)
            r = det.detect(kl, piv, 1.0, "TEST", "1h")
            if r:
                out.extend(r if isinstance(r, list) else [r])
        return out

    # ---- bug3: 返回值必须是 list ----
    def test_detect_returns_list(self):
        det = FlagDetector(FIXED)
        for s in (2, 3, 5):
            piv = find_pivots(self.kl_bull, left=s, right=s)
            r = det.detect(self.kl_bull, piv, 1.0, "TEST", "1h")
            self.assertIsInstance(r, list,
                                  f"detect() 必须返回 list, 实际 {type(r)}")

    def test_wedge_detect_returns_list(self):
        det = WedgeDetector({})
        for s in (2, 3, 5):
            piv = find_pivots(self.kl_bull, left=s, right=s)
            r = det.detect(self.kl_bull, piv, 1.0, "TEST", "1h")
            self.assertIsInstance(r, list,
                                  f"WedgeDetector.detect() 必须返回 list, 实际 {type(r)}")

    # ---- 牛市旗形 ----
    def test_bull_flag_detected(self):
        res = self._run(self.kl_bull, FIXED)
        self.assertTrue(res, "修复后应能检出牛市旗形")
        self.assertTrue(any(p.direction == Direction.LONG for p in res),
                        "牛市旗形方向应为 LONG")

    def test_bear_flag_detected(self):
        res = self._run(self.kl_bear, FIXED)
        self.assertTrue(res, "修复后应能检出熊市旗形")
        self.assertTrue(any(p.direction == Direction.SHORT for p in res),
                        "熊市旗形方向应为 SHORT")

    # ---- 证明 bug1/bug2 真实存在: 旧参数漏检 ----
    def test_old_params_missed_flag(self):
        """旧参数(pole_max_bars=3)下检不出旗形 —— 锁死 bug1 的存在证据"""
        res_old = self._run(self.kl_bull, BROKEN)
        res_new = self._run(self.kl_bull, FIXED)
        self.assertEqual(len(res_old), 0,
                         "旧参数 pole_max_bars=3 应漏检(证明 bug 真实)")
        self.assertGreater(len(res_new), 0,
                           "新参数 pole_max_bars=15 应能检出")

    def test_pole_max_bars_respected(self):
        """pole_max_bars 语义正确: 值越大, 搜索窗口越宽 (n - max_bars*3)"""
        wide = dict(FIXED, pole_max_bars=20)
        narrow = dict(FIXED, pole_max_bars=5)
        w = self._run(self.kl_bull, wide)
        n = self._run(self.kl_bull, narrow)
        self.assertGreaterEqual(len(w), len(n),
                                "放宽 pole_max_bars 不应减少检出")


if __name__ == "__main__":
    unittest.main(verbosity=2)
