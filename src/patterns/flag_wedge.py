# -*- coding: utf-8 -*-
"""
旗形 + 楔形 检测器

样本集中出现 35 次（通道14 + 楔形9 + 旗形7 + 下降楔3 + 上升楔2），属 P1 优先级。

旗形 (Flag)
  可检测性: A 级
  结构: 旗杆（急涨/急跌）+ 旗身（两条近似平行、且与旗杆反向倾斜的边界）
  含义: 持续形态 —— 方向【与旗杆同向】。上涨趋势中的旗形向下倾斜，是暂停而非反转
  失效: 旗身倾斜方向与旗杆同向（那就不是旗形，可能是通道）

  关键陷阱（见 docs/01）:
    旗形的"形态高度"取【旗杆】高度，不是旗身高度！
    用旗身高度算目标位会严重低估——这是回测第二大常见错误。

楔形 (Wedge)
  可检测性: B 级
  结构: 两条边界【同向】倾斜但收敛
  上升楔形: 两条边都向上，上边更缓 → 看跌（反转信号！不是持续）
  下降楔形: 两条边都向下，下边更陡 → 看涨
  注意: 楔形通常是【反转】形态，与旗形（持续）相反，这点极易混淆
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
    fit_trendline, is_flat, is_rising, is_falling,
    find_breakout_index, check_breakout, calc_volume_ratio, calc_trade_levels,
)


class FlagDetector(BaseDetector):
    """旗形：急涨/急跌后的短暂整理，随后延续原方向"""

    name = "flag"

    DEFAULT_PARAMS = {
        "pole_min_move": 0.05,          # 旗杆最小涨幅/跌幅
        "pole_max_bars": 15,            # 旗杆最多用多少根 K 线完成
        "flag_body_min": 5,             # 旗身最少 K 线
        "flag_body_max": 25,            # 旗身最多 K 线
        "parallel_tolerance": 0.0015,   # 两条边斜率差上限（近似平行）
        "min_touches": 2,
        "touch_tolerance": 0.02,
        "breakout_candles": 2,
        "breakout_atr_ratio": 0.5,
        "volume_ratio_min": 1.5,
        "max_lookahead": 20,
        "min_height_atr": 1.5,
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
        if len(pivots) < 4 or not klines or atr_value <= 0:
            return results

        p = self.params

        # ① 找旗杆：在最近一段内寻找急促的单向运动
        pole = self._find_flagpole(klines, p)
        if pole is None:
            return results

        pole_dir, pole_start, pole_end = pole

        # ② 旗身：旗杆结束之后的 K 线区间
        body_start = pole_end
        body_end = min(body_start + p["flag_body_max"], len(klines) - 1)
        if body_end - body_start < p["flag_body_min"]:
            return results

        # ③ 在旗身区间内的摆动点
        body_pivots = [pv for pv in pivots
                       if body_start <= pv.index <= body_end]
        if len(body_pivots) < 4:
            return results

        upper, ut = fit_trendline(body_pivots, PivotType.HIGH,
                                  min_touches=p["min_touches"],
                                  tolerance=p["touch_tolerance"], min_span=3)
        lower, lt = fit_trendline(body_pivots, PivotType.LOW,
                                  min_touches=p["min_touches"],
                                  tolerance=p["touch_tolerance"], min_span=3)
        if upper is None or lower is None:
            return results

        # ④ 两条边近似平行
        slope_diff = abs(upper.rel_slope - lower.rel_slope)
        if slope_diff > p["parallel_tolerance"]:
            return results

        # ⑤ 旗身倾斜方向必须与旗杆【相反】
        flag_slope = (upper.rel_slope + lower.rel_slope) / 2
        if pole_dir == Direction.LONG and flag_slope >= 0:
            return results      # 上涨旗杆后，旗身应向下倾斜
        if pole_dir == Direction.SHORT and flag_slope <= 0:
            return results

        # ⑥ 高度取【旗杆】，不是旗身
        if pole_dir == Direction.LONG:
            height = max(k.high for k in klines[pole_start:pole_end + 1]) \
                     - min(k.low for k in klines[pole_start:pole_end + 1])
        else:
            height = max(k.high for k in klines[pole_start:pole_end + 1]) \
                     - min(k.low for k in klines[pole_start:pole_end + 1])

        if height < p["min_height_atr"] * atr_value:
            return results

        pattern = Pattern(
            symbol=symbol, interval=interval,
            pattern_type="flag",
            direction=pole_dir,           # 方向 = 旗杆方向
            status=PatternStatus.CANDIDATE,
            pivots=body_pivots[:6],
            upper_boundary=upper,
            lower_boundary=lower,
            height=height,
            confidence=self._confidence(slope_diff, len(body_pivots)),
        )

        # ⑦ 突破：沿旗杆方向突破旗身边界
        if pole_dir == Direction.LONG:
            def boundary_fn(idx):
                return upper.value_at(idx)
        else:
            def boundary_fn(idx):
                return lower.value_at(idx)

        idx = find_breakout_index(klines, body_end, boundary_fn,
                                  pole_dir, p["max_lookahead"])
        if idx < 0:
            return pattern

        ok, confirmed, magnitude = check_breakout(
            klines, idx, boundary_fn(idx), pole_dir, atr_value,
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
    def _find_flagpole(klines: List[Kline], p: dict):
        """
        找旗杆：在最近 max_pole_bars 根内，价格单向移动超过 min_move。

        返回 (方向, 起点索引, 终点索引) 或 None
        """
        n = len(klines)
        max_bars = p["pole_max_bars"]
        min_move = p["pole_min_move"]

        # 只看最近 2 倍窗口，避免找到太远的"旗杆"
        search_start = max(0, n - max_bars * 3)

        best = None
        best_move = 0.0

        for start in range(search_start, n - 2):
            for end in range(start + 2, min(start + max_bars, n)):
                start_price = klines[start].close
                if start_price <= 0:
                    continue

                seg_high = max(k.high for k in klines[start:end + 1])
                seg_low = min(k.low for k in klines[start:end + 1])

                up_move = (seg_high - start_price) / start_price
                down_move = (start_price - seg_low) / start_price

                move = max(up_move, down_move)
                if move >= min_move and move > best_move:
                    best_move = move
                    direction = Direction.LONG if up_move >= down_move \
                        else Direction.SHORT
                    best = (direction, start, end)

        return best

    @staticmethod
    def _confidence(slope_diff: float, pivot_count: int) -> float:
        # 越平行越好
        parallel_score = max(0.0, 1.0 - slope_diff / 0.0015)
        # 摆动点越多越可信
        count_score = min(1.0, pivot_count / 6.0)
        return round(0.6 * parallel_score + 0.4 * count_score, 3)


class WedgeDetector(BaseDetector):
    """楔形：两条边界同向倾斜并收敛，通常是【反转】信号"""

    name = "wedge"

    DEFAULT_PARAMS = {
        "min_touches": 2,
        "touch_tolerance": 0.02,
        "min_span": 15,
        "max_span": 150,
        "converge_min": 0.0002,        # 两条边斜率差下限（必须真的在收敛）
        "flat_threshold": 0.0005,
        "breakout_candles": 2,
        "breakout_atr_ratio": 0.5,
        "volume_ratio_min": 1.5,
        "max_lookahead": 40,
        "min_height_atr": 2.0,
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

        upper, ut = fit_trendline(pivots, PivotType.HIGH,
                                  min_touches=p["min_touches"],
                                  tolerance=p["touch_tolerance"], min_span=5)
        lower, lt = fit_trendline(pivots, PivotType.LOW,
                                  min_touches=p["min_touches"],
                                  tolerance=p["touch_tolerance"], min_span=5)
        if upper is None or lower is None:
            return results

        # 跨度取【并集】（与 TriangleDetector 同样的修正）
        start_index = min(upper.p1.index, lower.p1.index)
        end_index = max(upper.p2.index, lower.p2.index)
        span = end_index - start_index
        if span <= 0:
            return results
        if not (p["min_span"] <= span <= p["max_span"]):
            return results

        # 上边界必须在下边界之上（同一索引处比较）
        if upper.value_at(start_index) <= lower.value_at(start_index):
            return results

        # 高度在左端最宽处测量
        height = upper.value_at(start_index) - lower.value_at(start_index)
        if height < p["min_height_atr"] * atr_value:
            return results

        us, ls = upper.rel_slope, lower.rel_slope
        flat_t = p["flat_threshold"]

        # --- 上升楔形：两条边都向上，上边更缓（收敛）→ 看跌 ---
        if us > flat_t and ls > flat_t and (us - ls) >= p["converge_min"]:
            pat = self._build("rising_wedge", Direction.SHORT, upper, lower,
                              ut, lt, start_index, end_index, height,
                              klines, atr_value, symbol, interval)
            if pat:
                results.append(pat)

        # --- 下降楔形：两条边都向下，下边更陡（收敛）→ 看涨 ---
        elif us < -flat_t and ls < -flat_t and (us - ls) >= p["converge_min"]:
            pat = self._build("falling_wedge", Direction.LONG, upper, lower,
                              ut, lt, start_index, end_index, height,
                              klines, atr_value, symbol, interval)
            if pat:
                results.append(pat)

        return results

    def _build(self, kind: str, direction: Direction,
               upper: Line, lower: Line, ut: int, lt: int,
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
            confidence=min(1.0, (ut + lt) / 6.0),
        )

        # 上升楔形跌破下边界；下降楔形突破上边界
        if direction == Direction.SHORT:
            def boundary_fn(idx):
                return lower.value_at(idx)
        else:
            def boundary_fn(idx):
                return upper.value_at(idx)

        idx = find_breakout_index(klines, end_index + 1, boundary_fn,
                                  direction, p["max_lookahead"])
        if idx < 0:
            return pattern

        ok, confirmed, magnitude = check_breakout(
            klines, idx, boundary_fn(idx), direction, atr_value,
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
