# -*- coding: utf-8 -*-
"""
三角形检测器：上升 / 下降 / 对称

可检测性: A 级（几何规则最明确的一类形态）

上升三角形 (Ascending Triangle)
  上边界水平，下边界抬升 → 看涨。每次回调低点都在抬高，买盘越来越急

下降三角形 (Descending Triangle)
  上边界下压，下边界水平 → 看跌。每次反弹高点都在降低，卖压越来越重

对称三角形 (Symmetrical Triangle)
  上边界下压 + 下边界抬升，双向收敛 → 方向取决于突破方向，需等待确认

判定阈值（默认）:
  水平判定阈值    |rel_slope| ≤ 0.0005（每根K线相对变化 0.05%）
  上下边界最少触点  各 2 个（合计 4）
  斜率比（对称）   0.5 ~ 2.0（两条边收敛速度不能差太多）
  形态跨度        15 ~ 150 根
  突破确认         连续 2 根收盘 + 0.5×ATR + 1.5 倍量

重要陷阱（见 docs/01）:
  形态高度必须在【左端最宽处】测量！
  若在突破点处测量，两条边界已经收敛，高度会接近 0，
  导致目标位算出来等于入场价——这是回测最常见的错误之一。
"""

import os
import sys
from typing import List, Optional, Tuple

_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from zigzag import Pivot, PivotType
from market_data import Kline
from patterns.base import (
    BaseDetector, Pattern, Direction, PatternStatus, Line,
    fit_trendline, is_flat, is_rising, is_falling, convergence,
    find_breakout_index, check_breakout, calc_volume_ratio, calc_trade_levels,
)


class TriangleDetector(BaseDetector):
    """上升 / 下降 / 对称三角形"""

    name = "triangle"

    DEFAULT_PARAMS = {
        "flat_ratio": 0.35,            # 一侧斜率 < 另一侧的 35% 即视为"水平"
        "min_touches": 2,              # 每条边界最少触点
        "touch_tolerance": 0.02,       # 触点判定容差
        "min_span": 15,                # 形态最小跨度
        "max_span": 150,               # 形态最大跨度
        "slope_ratio_min": 0.5,        # 对称三角形两条边斜率比下限
        "slope_ratio_max": 2.0,        # 上限
        "converge_ratio_min": 0.15,    # 几何收窄下限：右端间距至少比左端窄 15%
        "breakout_candles": 2,
        "breakout_atr_ratio": 0.5,
        "volume_ratio_min": 1.5,
        "max_lookahead": 40,           # 从形态末端往后找突破
        "min_height_atr": 2.0,         # 三角形高度至少 2×ATR
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

        p = self.params

        # 分别拟合上下边界
        upper, upper_touches = fit_trendline(
            pivots, PivotType.HIGH,
            min_touches=p["min_touches"],
            tolerance=p["touch_tolerance"],
            min_span=5,
        )
        lower, lower_touches = fit_trendline(
            pivots, PivotType.LOW,
            min_touches=p["min_touches"],
            tolerance=p["touch_tolerance"],
            min_span=5,
        )

        if upper is None or lower is None:
            return results

        # 形态跨度取两条边界的【并集】
        #
        # 曾经用交集（max(p1.index), min(p2.index)），结果严重错误：
        #   上边覆盖 [10,100]、下边覆盖 [50,110] → 交集算成 span=50（实际应为 100）
        #   两条边不重叠时甚至算出负数
        start_index = min(upper.p1.index, lower.p1.index)
        end_index = max(upper.p2.index, lower.p2.index)
        span = end_index - start_index
        if span <= 0:
            return results
        if not (p["min_span"] <= span <= p["max_span"]):
            return results

        # 上边界必须在下边界之上（在同一索引处比较，不能直接比 p1.price）
        if upper.value_at(start_index) <= lower.value_at(start_index):
            return results

        # 必须收敛：上边界斜率 < 下边界斜率
        if upper.rel_slope >= lower.rel_slope:
            return results

        # --- 高度必须在左端最宽处测量 ---
        upper_at_start = upper.value_at(start_index)
        lower_at_start = lower.value_at(start_index)
        height = upper_at_start - lower_at_start
        if height < p["min_height_atr"] * atr_value:
            return results

        # 几何收窄硬闸门：斜率收敛（upper.rel_slope < lower.rel_slope）
        # 只保证"方向对"，不保证收窄幅度够。必须在同一索引上量左右端间距。
        if convergence(upper, lower, start_index, end_index) \
                < p["converge_ratio_min"]:
            return results

        # --- 分类 ---
        kind = self._classify(upper, lower, p)
        if kind is None:
            return results

        # 对称三角形双向尝试；上升/下降三角形单向
        if kind == "symmetrical_triangle":
            directions = [Direction.LONG, Direction.SHORT]
        elif kind == "ascending_triangle":
            directions = [Direction.LONG]
        else:
            directions = [Direction.SHORT]

        for direction in directions:
            pat = self._build_and_confirm(
                kind, direction, upper, lower, upper_touches, lower_touches,
                start_index, end_index, height, klines, atr_value,
                symbol, interval
            )
            if pat:
                results.append(pat)

        return results

    # ---------- 分类 ----------

    def _classify(self, upper: Line, lower: Line, p: dict) -> Optional[str]:
        """
        按两条边界的【相对斜率比例】分类三角形。

        为什么不用绝对阈值（如 |rel_slope| <= 0.0005 算水平）：
          每根K线的斜率强烈依赖跨度。同一个 2% 的收敛幅度，
          20 根的三角形是 0.001/根，100 根的只有 0.0002/根。
          用绝对阈值会导致长跨度三角形的两边都被判成"水平"，
          于是不属于任何类型而被误杀——实测 ETH 1h 就是这样漏掉的。

        改用相对比例（与跨度无关）：
          su = -upper.rel_slope   上边下压速度（正=在下降）
          sl =  lower.rel_slope   下边抬升速度（正=在上升）

          su 远小于 sl（<35%）→ 上升三角（上边基本水平，下边抬升）
          sl 远小于 su（<35%）→ 下降三角（上边下压，下边基本水平）
          两者相当             → 对称三角

        实测验证：
          ETH 1h  su=0.000169 sl=0.000184 → 比例0.92 → 对称三角 ✅
                  （旧逻辑两边都判"水平"，直接漏检）
          ETH 4h  su=0.000113 sl=0.000439 → 比例0.26 → 上升三角 ✅
          XRP 1h  su=0.000713 sl=-0.000607（下边在下降）
                  → 净收敛仅0.000106，且 sl 反向过大 → 正确拒绝 ✅
        """
        su = -upper.rel_slope           # 上边下压速度
        sl = lower.rel_slope            # 下边抬升速度
        net = su + sl                   # 净收敛速度

        # 必须收敛
        if net <= 0:
            return None

        # 允许一侧轻微反向，但不能超过净收敛的 30%
        # （如下降三角的支撑位略微下倾是常见的，但若下倾过快就不是三角形了）
        if su < -0.3 * net or sl < -0.3 * net:
            return None

        m = max(su, sl)
        if m <= 0:
            return None

        ratio_threshold = p.get("flat_ratio", 0.35)

        if su < ratio_threshold * m:
            return "ascending_triangle"
        if sl < ratio_threshold * m:
            return "descending_triangle"

        # 对称三角形：两条边收敛速度不能差太多
        if sl != 0:
            ratio = abs(su) / abs(sl)
            if not (p["slope_ratio_min"] <= ratio <= p["slope_ratio_max"]):
                return None
        return "symmetrical_triangle"

    # ---------- 构建与确认 ----------

    def _build_and_confirm(self, kind: str, direction: Direction,
                           upper: Line, lower: Line,
                           upper_touches: int, lower_touches: int,
                           start_index: int, end_index: int, height: float,
                           klines: List[Kline], atr_value: float,
                           symbol: str, interval: str) -> Optional[Pattern]:
        p = self.params

        pattern = Pattern(
            symbol=symbol, interval=interval,
            pattern_type=kind,
            direction=direction,
            status=PatternStatus.CANDIDATE,
            pivots=[upper.p1, upper.p2, lower.p1, lower.p2],
            upper_boundary=upper,
            lower_boundary=lower,
            height=height,
            confidence=self._confidence(upper_touches, lower_touches,
                                        end_index - start_index),
        )

        # 突破方向决定用哪条边界
        if direction == Direction.LONG:
            def boundary_fn(idx):
                return upper.value_at(idx)
        else:
            def boundary_fn(idx):
                return lower.value_at(idx)

        idx = find_breakout_index(klines, end_index + 1, boundary_fn,
                                  direction, p["max_lookahead"])
        if idx < 0:
            return pattern

        boundary_at = boundary_fn(idx)
        ok, confirmed, magnitude = check_breakout(
            klines, idx, boundary_at, direction, atr_value,
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

    @staticmethod
    def _confidence(upper_touches: int, lower_touches: int, span: int) -> float:
        """几何完整度 0~1"""
        # 触点越多越可信（4个满分）
        touch_score = min(1.0, (upper_touches + lower_touches) / 6.0)
        if 20 <= span <= 80:
            span_score = 1.0
        elif span < 20:
            span_score = span / 20
        else:
            span_score = max(0.3, 1.0 - (span - 80) / 150)
        return round(0.6 * touch_score + 0.4 * span_score, 3)
