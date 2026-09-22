#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
规则3 降级闸回归测试: LONG 信号逆 1d 趋势 → 观察流 (2026-09-22 上线)

背景：
  实盘两天 14 条 LONG 全推进下跌行情、CASHCAT −31%。348 样本回测按 1d 趋势分层:
    LONG 逆 1d 胜率 23.1%(26样) vs 顺势 50.4%(137样), z=+2.56
    15m/1h/4h 三周期方向一致: 逆势 15.4% / 25.0% / 33.3%
    SHORT 侧顺逆无差异(39.4% vs 39.1%, z=0.02) → 只拦 LONG, 不做对称规则

测试目标（把口径钉死, 防止以后被"顺手改对称"或改错比较符）：
  1. LONG 15m/1h/4h + 1d 下跌 → 拦（True）
  2. LONG 15m/1h/4h + 1d 上涨 → 放行（False）
  3. SHORT 任何周期任何趋势 → 不适用（None, 放行）
  4. 1d 形态自身 → 不适用（None）
  5. 1d 数据不足（含回测同款 n+5 下限）→ 不适用（None），宁放勿杀
  6. 边界: 收盘价正好相等 → 归 down（与回测 trend_ctx 的 `>` 判据一致）
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from market_data import Kline          # noqa: E402
from patterns.base import Direction    # noqa: E402
from scanner import counter_1d_long_gate  # noqa: E402


def make_klines(closes):
    """按收盘价序列造 K 线（开高低用收盘价占位, 本闸只看 close）"""
    out = []
    for i, c in enumerate(closes):
        out.append(Kline(
            openTime=i * 86400000, open=c, high=c, low=c, close=c,
            volume=1.0, closeTime=i * 86400000 + 86399999, quoteVolume=1.0,
        ))
    return out


def flat_then(n_now, n_ago, base=100.0, n_total=60):
    """造一段横盘 + 末尾位移: 使 closes[-1] 与 closes[-n_ago] 有指定关系"""
    closes = [base] * n_total
    closes[-n_ago] = base
    closes[-1] = n_now
    return closes


class TestCounter1dLongGate(unittest.TestCase):

    def test_long_down_trend_is_demoted(self):
        """LONG 三周期 + 1d 下跌 → 必须拦"""
        k = make_klines(flat_then(90.0, 20))          # 现价 90 < 20 根前 100
        for iv in ("15m", "1h", "4h"):
            self.assertIs(counter_1d_long_gate(k, Direction.LONG, iv), True,
                          f"LONG {iv} 逆 1d 应被拦")

    def test_long_up_trend_passes(self):
        """LONG 三周期 + 1d 上涨 → 必须放行"""
        k = make_klines(flat_then(110.0, 20))         # 现价 110 > 20 根前 100
        for iv in ("15m", "1h", "4h"):
            self.assertIs(counter_1d_long_gate(k, Direction.LONG, iv), False,
                          f"LONG {iv} 顺 1d 应放行")

    def test_short_never_applies(self):
        """SHORT 侧刻意不做对称规则（回测 z=0.02）"""
        k_down = make_klines(flat_then(90.0, 20))
        k_up = make_klines(flat_then(110.0, 20))
        for iv in ("15m", "1h", "4h"):
            for k in (k_down, k_up):
                self.assertIsNone(
                    counter_1d_long_gate(k, Direction.SHORT, iv),
                    f"SHORT {iv} 不该被这条规则碰")

    def test_1d_interval_not_applicable(self):
        """1d 形态自身未在回测里验证过 → 不动刀"""
        k = make_klines(flat_then(90.0, 20))
        self.assertIsNone(counter_1d_long_gate(k, Direction.LONG, "1d"))

    def test_insufficient_data_passes(self):
        """1d 数据不足 → 放行（宁放勿杀）。下限对齐回测 n+5=25"""
        k24 = make_klines([100.0] * 23 + [90.0])
        k25 = make_klines([100.0] * 4 + [100.0] + [100.0] * 19 + [90.0])
        self.assertIsNone(counter_1d_long_gate(k24, Direction.LONG, "1h"),
                          "24 根应判不适用")
        self.assertEqual(len(k25), 25)
        self.assertIs(counter_1d_long_gate(k25, Direction.LONG, "1h"), True,
                      "25 根且逆势应被拦")
        self.assertIsNone(counter_1d_long_gate(None, Direction.LONG, "1h"))
        self.assertIsNone(counter_1d_long_gate([], Direction.LONG, "1h"))

    def test_equal_close_counts_as_down(self):
        """收盘价相等归 down —— 与回测 trend_ctx 的 `closes[-1] > closes[-n]` 一致"""
        k = make_klines([100.0] * 60)
        self.assertIs(counter_1d_long_gate(k, Direction.LONG, "4h"), True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
