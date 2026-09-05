# -*- coding: utf-8 -*-
"""
头肩顶 / 头肩底 检测器

可检测性:
  头肩顶 —— A 级（几何规则明确）
  头肩底 —— B 级（几何同样明确，但需"前置下跌趋势"这一额外条件，
                  而"什么是下跌趋势"本身又是一组参数）

头肩顶 (Head & Shoulders Top)
  结构: (H, L, H, L, H) —— 左肩、谷、头、谷、右肩
  含义: 看跌。三次上攻，中间最高但两肩无力，多头衰竭
  失效: 右肩高过头部（那就不是头肩，而是趋势延续）

头肩底 (Head & Shoulders Bottom)
  结构: (L, H, L, H, L)
  含义: 看涨，头肩顶的镜像
  额外: 需前置一段下跌趋势（跌幅 > 5%，至少 20 根 K 线）

判定阈值（默认）:
  两肩价差容差    3%
  头部突出度      ≥ 2%（头必须明显高过两肩，否则只是三重顶）
  两谷高度差      3%（颈线近似水平；太斜说明形态不标准）
  形态跨度        20~200 根
  颈线斜率        ±3%
"""

import os
import sys
from typing import List, Optional

_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from zigzag import Pivot, PivotType
from market_data import Kline
from patterns.base import (
    BaseDetector, Pattern, Direction, PatternStatus, Line,
    find_breakout_index, check_breakout, calc_volume_ratio, calc_trade_levels,
    prior_move,
)


class HeadShouldersDetector(BaseDetector):
    """头肩顶 + 头肩底"""

    name = "head_shoulders"

    DEFAULT_PARAMS = {
        "shoulder_tolerance": 0.03,     # 两肩价差容差
        "head_prominence": 0.02,        # 头必须高过两肩至少 2%
        "neck_tolerance": 0.03,         # 两谷高度差（颈线近似水平）
        "min_span": 20,                 # 形态最小跨度
        "max_span": 200,                # 形态最大跨度
        "breakout_candles": 2,
        "breakout_atr_ratio": 0.5,
        "volume_ratio_min": 1.5,
        "max_lookahead": 30,            # 右肩之后最多看多少根找突破
        "min_height_atr": 1.0,
        # 反转形态结构性闸门：形态前必须有显著趋势（2026-09-03 共享给头肩顶）
        # 实测：158 张人工标注的漏网误报里 ENA 头肩顶 / CRV 头肩底 全是
        # 「横盘里的小 H-L-H-L-H」，形态内部结构满足但前面没趋势。
        # 顶要求前面涨上来，底要求前面跌下来。prior_move 来自 base.py 共享。
        "require_prior_trend": True,
        "prior_trend_bars": 20,
        "prior_trend_min_move": 0.05,
    }

    def __init__(self, params=None):
        merged = dict(self.DEFAULT_PARAMS)
        if params:
            merged.update(params)
        super().__init__(merged)

    def detect(self, klines: List[Kline], pivots: List[Pivot],
               atr_value: float, symbol: str = "",
               interval: str = "") -> List[Pattern]:
        results = []
        if len(pivots) < 5 or not klines or atr_value <= 0:
            return results

        # 扫描所有连续五点窗口
        for i in range(len(pivots) - 4):
            window = pivots[i: i + 5]
            types = [p.type for p in window]

            # --- 头肩顶: 高-低-高-低-高 ---
            if types == [PivotType.HIGH, PivotType.LOW, PivotType.HIGH,
                         PivotType.LOW, PivotType.HIGH]:
                pat = self._check_hs_top(window, klines, atr_value,
                                         symbol, interval)
                if pat:
                    results.append(pat)

            # --- 头肩底: 低-高-低-高-低 ---
            elif types == [PivotType.LOW, PivotType.HIGH, PivotType.LOW,
                           PivotType.HIGH, PivotType.LOW]:
                pat = self._check_hs_bottom(window, klines, pivots, i,
                                            atr_value, symbol, interval)
                if pat:
                    results.append(pat)

        return results

    # ---------- 头肩顶 ----------

    def _check_hs_top(self, w: List[Pivot], klines: List[Kline],
                      atr_value: float, symbol: str,
                      interval: str) -> Optional[Pattern]:
        p = self.params
        ls, n1, head, n2, rs = w      # left shoulder, neck1, head, neck2, right shoulder

        # ① 头必须高过两肩
        if head.price <= max(ls.price, rs.price):
            return None
        prominence = (head.price - max(ls.price, rs.price)) / max(ls.price, rs.price)
        if prominence < p["head_prominence"]:
            return None

        # ② 两肩高度接近
        shoulder_diff = abs(ls.price - rs.price) / ls.price
        if shoulder_diff > p["shoulder_tolerance"]:
            return None

        # ③ 两个谷高度接近（颈线近似水平）
        neck_diff = abs(n1.price - n2.price) / n1.price
        if neck_diff > p["neck_tolerance"]:
            return None

        # ④ 形态跨度合理
        span = rs.index - ls.index
        if not (p["min_span"] <= span <= p["max_span"]):
            return None

        # ⑤ 形态高度有交易价值
        neck_at_head = self._interp(n1, n2, head.index)
        height = head.price - neck_at_head
        if height < p["min_height_atr"] * atr_value:
            return None

        # ⑤b 前置趋势（结构性闸门，2026-09-03 补齐）：头肩顶是反转形态，
        # 前面必须有一段显著上涨。否则横盘里的"H-L-H-L-H"会过其它所有检查
        # 但根本不是反转。
        if p["require_prior_trend"]:
            mv = prior_move(klines, ls.index, p["prior_trend_bars"])
            if mv is None or mv < p["prior_trend_min_move"]:
                return None

        # ⑥ 右肩之后不能创新高超过头部（否则形态破坏）
        highest_after = max((k.high for k in klines[rs.index:]), default=rs.price)
        if highest_after > head.price * 1.005:
            return None

        neckline = Line(n1, n2)
        pattern = Pattern(
            symbol=symbol, interval=interval,
            pattern_type="head_shoulders_top",
            direction=Direction.SHORT,
            status=PatternStatus.CANDIDATE,
            pivots=[ls, n1, head, n2, rs],
            neckline=neckline,
            height=height,
            confidence=self._confidence(shoulder_diff, neck_diff,
                                        prominence, span),
        )

        # ⑦ 突破确认（颈线是斜线，每根K线取该位置的颈线值）
        def boundary_fn(idx):
            return neckline.value_at(idx)

        idx = find_breakout_index(klines, rs.index + 1, boundary_fn,
                                  Direction.SHORT, p["max_lookahead"])
        if idx < 0:
            return pattern

        ok, confirmed, magnitude = check_breakout(
            klines, idx, neckline.value_at(idx), Direction.SHORT, atr_value,
            required_candles=p["breakout_candles"],
            min_magnitude_atr=p["breakout_atr_ratio"],
        )

        pattern.breakout_index = idx
        pattern.breakout_price = klines[idx].close
        pattern.breakout_magnitude_atr = magnitude
        pattern.confirmed_candles = confirmed
        pattern.volume_ratio = calc_volume_ratio(klines, idx)

        if pattern.volume_ratio < p["volume_ratio_min"]:
            return pattern
        if not ok:
            return pattern

        pattern.status = PatternStatus.CONFIRMED
        calc_trade_levels(pattern, klines, atr_value)
        return pattern

    # ---------- 头肩底 ----------

    def _check_hs_bottom(self, w: List[Pivot], klines: List[Kline],
                         all_pivots: List[Pivot], start_idx: int,
                         atr_value: float, symbol: str,
                         interval: str) -> Optional[Pattern]:
        p = self.params
        ls, n1, head, n2, rs = w

        # ① 头必须低过两肩
        if head.price >= min(ls.price, rs.price):
            return None
        prominence = (min(ls.price, rs.price) - head.price) / head.price
        if prominence < p["head_prominence"]:
            return None

        # ② 两肩高度接近
        shoulder_diff = abs(ls.price - rs.price) / ls.price
        if shoulder_diff > p["shoulder_tolerance"]:
            return None

        # ③ 两个峰高度接近（颈线近似水平）
        neck_diff = abs(n1.price - n2.price) / n1.price
        if neck_diff > p["neck_tolerance"]:
            return None

        # ④ 形态跨度
        span = rs.index - ls.index
        if not (p["min_span"] <= span <= p["max_span"]):
            return None

        # ⑤ 形态高度
        neck_at_head = self._interp(n1, n2, head.index)
        height = neck_at_head - head.price
        if height < p["min_height_atr"] * atr_value:
            return None

        # ⑥ 额外条件：前置下跌趋势（结构性闸门，2026-09-03 改用 base.prior_move）
        if p["require_prior_trend"]:
            mv = prior_move(klines, ls.index, p["prior_trend_bars"])
            if mv is None or mv > -p["prior_trend_min_move"]:
                return None

        # ⑦ 右肩之后不能创新低超过头部
        lowest_after = min((k.low for k in klines[rs.index:]), default=rs.price)
        if lowest_after < head.price * 0.995:
            return None

        neckline = Line(n1, n2)
        pattern = Pattern(
            symbol=symbol, interval=interval,
            pattern_type="head_shoulders_bottom",
            direction=Direction.LONG,
            status=PatternStatus.CANDIDATE,
            pivots=[ls, n1, head, n2, rs],
            neckline=neckline,
            height=height,
            confidence=self._confidence(shoulder_diff, neck_diff,
                                        prominence, span),
        )

        def boundary_fn(idx):
            return neckline.value_at(idx)

        idx = find_breakout_index(klines, rs.index + 1, boundary_fn,
                                  Direction.LONG, p["max_lookahead"])
        if idx < 0:
            return pattern

        ok, confirmed, magnitude = check_breakout(
            klines, idx, neckline.value_at(idx), Direction.LONG, atr_value,
            required_candles=p["breakout_candles"],
            min_magnitude_atr=p["breakout_atr_ratio"],
        )

        pattern.breakout_index = idx
        pattern.breakout_price = klines[idx].close
        pattern.breakout_magnitude_atr = magnitude
        pattern.confirmed_candles = confirmed
        pattern.volume_ratio = calc_volume_ratio(klines, idx)

        if pattern.volume_ratio < p["volume_ratio_min"]:
            return pattern
        if not ok:
            return pattern

        pattern.status = PatternStatus.CONFIRMED
        calc_trade_levels(pattern, klines, atr_value)
        return pattern

    # ---------- 辅助 ----------

    @staticmethod
    def _interp(p1: Pivot, p2: Pivot, index: int) -> float:
        """在 p1、p2 连线上取第 index 根 K 线处的值"""
        dx = p2.index - p1.index
        if dx == 0:
            return p1.price
        return p1.price + (p2.price - p1.price) * (index - p1.index) / dx

    @staticmethod
    def _confidence(shoulder_diff: float, neck_diff: float,
                    prominence: float, span: int) -> float:
        """几何完整度 0~1"""
        shoulder_score = max(0.0, 1.0 - shoulder_diff / 0.03)      # 0.30 权重
        neck_score = max(0.0, 1.0 - neck_diff / 0.03)              # 0.25 权重
        prom_score = min(1.0, prominence / 0.08)                   # 0.25 权重
        if 30 <= span <= 120:
            span_score = 1.0                                       # 0.20 权重
        elif span < 30:
            span_score = span / 30
        else:
            span_score = max(0.3, 1.0 - (span - 120) / 200)

        return round(0.30 * shoulder_score + 0.25 * neck_score
                     + 0.25 * prom_score + 0.20 * span_score, 3)
