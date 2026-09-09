# -*- coding: utf-8 -*-
"""
箱体 / 通道检测器（B 阶段补充，2026-09-08）

这是现有检测器的真实空白（不是重复实现）：
  - 三角形要求收敛（upper.rel_slope < lower.rel_slope）→ 水平箱体被拒
  - 楔形要求收敛 + 5 触点                          → 平行通道被拒
  - 双顶/双底/头肩要求反转结构                      → 震荡区间不匹配
结果：市场里大量存在的"横盘箱体"和"斜平行通道"此前完全无人认领。

rectangle (箱体/矩形)
  上边界水平 + 下边界水平，价格在两条水平线之间震荡。
  突破方向不定：向上突破看涨、向下突破看跌，双向尝试。

ascending_channel (上升通道) / descending_channel (下降通道)
  两条边界近似平行且同向倾斜，价格沿通道运行。
  顺向突破=延续，反向突破=衰竭，双向尝试。

与三角检测器的关键实现差异（为什么不用 fit_trendline）:
  fit_trendline 在【全部】摆动点里枚举点对，优先选"触点最多+跨度最大"的线。
  箱体的高点/低点天然大量共线（震荡区间到处都是近似水平的高点），
  全局枚举会选出一个横跨 300 根的长线 → 被自己的 max_span 拒掉，
  而中间真实存在的好箱体反而被挤掉。因此这里改为
  【跨度受限的窗口拟合】：枚举时就要求 span 在 [min_span, max_span] 内，
  触点只统计落在 [p1.index, p2.index] 窗口内的摆动点。

判定阈值（默认，均可从 config.yaml 覆盖）:
  水平判定        |rel_slope| ≤ 0.0004（每根K线相对变化 0.04%）
  平行判定        |上斜率 - 下斜率| ≤ 0.0008
  每条边界最少触点 2 个（合计 4）
  形态跨度        30 ~ 200 根
  箱体高度        ≥ 3×ATR（左端最宽处测量）
  突破确认         连续 2 根收盘 + 0.5×ATR + 1.5 倍量
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
    find_breakout_index, check_breakout, calc_volume_ratio, calc_trade_levels,
)


def _fit_boundary_span(pivots: List[Pivot], use_type: PivotType,
                       min_touches: int, tolerance: float,
                       min_span: int, max_span: int
                       ) -> Tuple[Optional[Line], int]:
    """
    跨度受限的边界拟合（箱体专用，区别于全局 fit_trendline）。

    枚举所有同类摆动点对，要求:
      1. 两点间隔在 [min_span, max_span] 内
      2. 触点只统计落在 [p1.index, p2.index] 窗口内的摆动点
    优先级: 触点多 > 跨度大（与 fit_trendline 一致）。

    返回 (最佳线 或 None, 触点数)
    """
    pts = [p for p in pivots if p.type == use_type]
    if len(pts) < 2:
        return None, 0

    best_line: Optional[Line] = None
    best_touches = 0
    best_span = 0

    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            p1, p2 = pts[i], pts[j]
            span = p2.index - p1.index
            if not (min_span <= span <= max_span):
                continue

            line = Line(p1, p2)

            touches = 0
            for p in pts:
                if not (p1.index <= p.index <= p2.index):
                    continue
                expected = line.value_at(p.index)
                if expected == 0:
                    continue
                if abs(p.price - expected) / expected <= tolerance:
                    touches += 1

            if touches > best_touches or (touches == best_touches
                                          and touches >= min_touches
                                          and line.span > best_span):
                best_touches = touches
                best_span = line.span
                best_line = line

    if best_line is None or best_touches < min_touches:
        return None, best_touches

    return best_line, best_touches


class BoxDetector(BaseDetector):
    """箱体（矩形）/ 上升通道 / 下降通道"""

    name = "box"

    DEFAULT_PARAMS = {
        "min_touches": 2,              # 每条边界最少触点（合计≥4）
        "touch_tolerance": 0.02,       # 触点判定容差（相对价格）
        "min_span": 30,                # 形态最小跨度
        "max_span": 200,               # 形态最大跨度
        "min_height_atr": 3.0,         # 左端最宽处的高度下限（ATR倍数）
        "flat_threshold": 0.0004,      # 箱体判定: 边界 |rel_slope| ≤ 此值
        "max_slope_diff": 0.0008,      # 平行判定: |上斜率-下斜率| ≤ 此值
        "min_width_ratio": 0.75,       # 宽度收窄硬闸: 右端间距 ≥ 左端×此值
                                       # (2026-09-09 ZBT 4h: 收窄比0.44被拒)
        "breakout_candles": 2,
        "breakout_atr_ratio": 0.5,
        "volume_ratio_min": 1.5,
        "max_lookahead": 40,           # 从形态末端往后找突破
        # 价格包住硬校验 (2026-09-09, 用户金标准反馈):
        #   全量实测 23 个 CONFIRMED box 的边界穿透分布 —— 上/下边界各自
        #   [p1,p2] 窗口内统计 K 线相对边界线的穿透。用户点评"瞎画"的案例
        #   (CL 4h L线深刺19.6% / NEAR 10.4% / AAVE 38% / BNB 喇叭口等)
        #   无一例外穿透极大, 而看起来干净的(EDGE/DOT/XAG)全部小穿透。
        #   因此加三道硬闸, 任一超限直接拒掉, 杜绝"把孤立点连成线"的乱画:
        #     max_penetration: 单根K线影线相对边界最大穿透 (0.08 = 8%)
        #     max_close_escape: 收盘价跑出边界外的K线占比上限 (0.15 = 15%)
        #     max_deep_escape: 影线深刺(>2%)的K线占比上限 (0.15 = 15%)
        "contain_max_penetration": 0.08,
        "contain_max_close_escape": 0.15,
        "contain_max_deep_escape": 0.15,
        # 两边界时间重叠下限 (原0.5, 2026-09-09 收紧至0.85)。
        # 真箱体/通道两边界同起同止; 错位大=一边悬空=乱画。
        "overlap_min": 0.85,
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

        upper, upper_touches = _fit_boundary_span(
            pivots, PivotType.HIGH,
            min_touches=p["min_touches"],
            tolerance=p["touch_tolerance"],
            min_span=p["min_span"],
            max_span=p["max_span"],
        )
        lower, lower_touches = _fit_boundary_span(
            pivots, PivotType.LOW,
            min_touches=p["min_touches"],
            tolerance=p["touch_tolerance"],
            min_span=p["min_span"],
            max_span=p["max_span"],
        )

        if upper is None or lower is None:
            return results

        # 两条边界必须在时间上真实共存（重叠 ≥ 短边跨度的 overlap_min）。
        # 【2026-09-09 收紧 50%→85%(默认), 用户金标准 BNB/CL 案例】
        #   旧阈值允许两边界错位 50%: BNB U从idx63起/L从idx25起 → 窗口并集
        #   前 38 根只有下边界"悬空"; CL L从93起/U从131起 → 前段悬空。
        #   视觉上就是"一边线长一边线短、价格从没同时被两条线框住"的乱画。
        #   真箱体/通道的两条边界必然同起同止(重叠≈100%), 错位明显=假形态。
        ov = min(upper.p2.index, lower.p2.index) - max(upper.p1.index,
                                                       lower.p1.index)
        shorter = min(upper.span, lower.span)
        if ov <= 0 or ov < p["overlap_min"] * shorter:
            return results

        # 形态跨度取两条边界的【并集】（与三角检测器同口径）
        start_index = min(upper.p1.index, lower.p1.index)
        end_index = max(upper.p2.index, lower.p2.index)
        span = end_index - start_index
        if not (p["min_span"] <= span <= p["max_span"]):
            return results

        # 上边界必须在下边界之上（同一索引处比较）
        u_start = upper.value_at(start_index)
        l_start = lower.value_at(start_index)
        u_end = upper.value_at(end_index)
        l_end = lower.value_at(end_index)
        if u_start <= l_start or u_end <= l_end:
            return results

        # --- 高度必须在左端最宽处测量 ---
        height = u_start - l_start
        if height < p["min_height_atr"] * atr_value:
            return results

        # --- 平行判定：两条边界斜率差必须足够小 ---
        slope_diff = abs(upper.rel_slope - lower.rel_slope)
        if slope_diff > p["max_slope_diff"]:
            return results

        # --- 宽度收窄硬闸 (2026-09-09, 用户金标准 ZBT 4h) ---
        # 平行判定(斜率差)有个盲区: 长跨度下两条边界斜率差很小,
        # 但一缓一陡 → 右端间距相对左端显著收窄, 视觉是收敛三角/楔形,
        # 不是平行通道。ZBT 4h 实测: slope_diff=0.000475 < 0.0008 过闸,
        # 但 收窄比=0.44 (右端间距只有左端 44%) —— 用户判"勉强算上升三角"。
        # 真通道/箱体两端宽度应近似相等; 右端明显收窄 = 三角形/楔形的领地,
        # box 让路 (triangle 检测器收敛判定 ≥15% 收窄, 此处放更宽的边界)。
        right_gap = u_end - l_end
        if right_gap < p["min_width_ratio"] * height:
            return results

        # --- 价格包住硬校验 (2026-09-09) ---
        # 边界线必须真实"框住"中间行情: 在每条边界自己的 [p1,p2] 窗口内,
        # 统计 K 线影线相对边界线的穿透。任一超限即拒 —— 杜绝把孤立的
        # 两个点连成线、中间价格完全跑飞的"假箱体/假通道"。
        if not self._containment_ok(klines, upper, lower, p):
            return results

        # --- 分类 ---
        flat_thr = p["flat_threshold"]
        upper_flat = abs(upper.rel_slope) <= flat_thr
        lower_flat = abs(lower.rel_slope) <= flat_thr

        if upper_flat and lower_flat:
            kind = "rectangle"
        elif (upper.rel_slope > flat_thr and lower.rel_slope > flat_thr):
            kind = "ascending_channel"
        elif (upper.rel_slope < -flat_thr and lower.rel_slope < -flat_thr):
            kind = "descending_channel"
        else:
            return results   # 一平一斜 → 既非箱体也非通道，交给三角形

        # 双向尝试（箱体突破方向不定；通道顺向=延续、反向=衰竭）
        for direction in (Direction.LONG, Direction.SHORT):
            pat = self._build_and_confirm(
                kind, direction, upper, lower, upper_touches, lower_touches,
                start_index, end_index, height, klines, atr_value,
                symbol, interval
            )
            if pat:
                results.append(pat)

        return results

    # ---------- 价格包住硬校验 ----------

    @staticmethod
    def _containment_ok(klines: List[Kline], upper: Line, lower: Line,
                        p: dict) -> bool:
        """
        校验两条边界线是否真实包住中间行情（视觉常识硬闸）。

        对【每条边界各自】的 [p1, p2] 窗口内所有 K 线：
          max_penetration  : 影线相对边界的最大单根穿透（>8% 视为乱画）
          close_escape     : 收盘价跑出边界外的占比（>15% 视为没框住）
          deep_escape      : 影线深刺（>2%）的占比（>15% 视为大量刺穿）

        任一超限即拒。为什么按"每条边界自己的窗口"统计而不是形态并集：
          形态窗口取上/下边界并集，若上边界比下边界晚出现（如 CL 4h：
          L 从 93 根起、U 从 131 根才起），并集前段会"没有上边界"——
          收盘/影线天然全在"悬空上边界"之上，造成假性高穿透。
        """
        cap = p["contain_max_penetration"]
        ccap = p["contain_max_close_escape"]
        dcap = p["contain_max_deep_escape"]

        def _line_ok(line: Line, above: bool) -> bool:
            s, e = line.p1.index, line.p2.index
            n = e - s + 1
            if n <= 0:
                return False
            close_out = deep = 0
            max_pen = 0.0
            for i in range(s, e + 1):
                k = klines[i]
                v = line.value_at(i)
                if above:
                    if k.close > v:
                        close_out += 1
                    if k.high > v:
                        rel = (k.high - v) / v
                        max_pen = max(max_pen, rel)
                        if rel > 0.02:
                            deep += 1
                else:
                    if k.close < v:
                        close_out += 1
                    if k.low < v:
                        rel = (v - k.low) / v
                        max_pen = max(max_pen, rel)
                        if rel > 0.02:
                            deep += 1
            if max_pen > cap:
                return False
            if close_out / n > ccap:
                return False
            if deep / n > dcap:
                return False
            return True

        return _line_ok(upper, True) and _line_ok(lower, False)

    # ---------- 构建与确认（与三角检测器同流程） ----------

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

        # 突破方向决定用哪条边界（支持斜线边界）
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
        """几何完整度 0~1（口径与三角检测器一致）"""
        touch_score = min(1.0, (upper_touches + lower_touches) / 6.0)
        if 30 <= span <= 120:
            span_score = 1.0
        elif span < 30:
            span_score = span / 30
        else:
            span_score = max(0.3, 1.0 - (span - 120) / 200)
        return round(0.6 * touch_score + 0.4 * span_score, 3)
