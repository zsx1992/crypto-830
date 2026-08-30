# -*- coding: utf-8 -*-
"""
形态识别基础结构

提供三样东西：
  1. 数据结构 —— Line（趋势线）、Pattern（形态候选）
  2. 几何工具 —— 趋势线拟合、斜率计算
  3. 确认工具 —— 突破确认、量能确认、交易价位计算

所有检测器共用这些工具，保证同一套判定标准。
"""

import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from enum import Enum

# 允许 patterns 包内的模块直接 import src/ 下的兄弟模块
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from zigzag import Pivot, PivotType
from market_data import Kline


class Direction(Enum):
    LONG = "LONG"        # 看涨
    SHORT = "SHORT"      # 看跌


class PatternStatus(Enum):
    CANDIDATE = "CANDIDATE"    # 结构匹配，等待突破
    CONFIRMED = "CONFIRMED"    # 突破已确认 → 可推送
    FAILED = "FAILED"          # 价格回到形态内部，失效
    EXPIRED = "EXPIRED"        # 超过窗口未突破


@dataclass
class Line:
    """
    趋势线（由两个摆动点确定）

    slope      —— 每根 K 线的价格变化量（绝对值）
    rel_slope  —— 每根 K 线的相对变化率（斜率 / 均价），跨品种可比
    """
    p1: Pivot
    p2: Pivot

    @property
    def slope(self) -> float:
        dx = self.p2.index - self.p1.index
        if dx == 0:
            return 0.0
        return (self.p2.price - self.p1.price) / dx

    @property
    def rel_slope(self) -> float:
        """相对斜率：每根K线的价格变化占比。跨品种/跨价位可比。"""
        avg_price = (self.p1.price + self.p2.price) / 2
        if avg_price == 0:
            return 0.0
        return self.slope / avg_price

    def value_at(self, index: int) -> float:
        """该趋势线在第 index 根 K 线处的价格"""
        return self.p1.price + self.slope * (index - self.p1.index)

    @property
    def span(self) -> int:
        return abs(self.p2.index - self.p1.index)

    def __repr__(self):
        return (f"Line({self.p1.type.value[0].upper()}@{self.p1.index} "
                f"-> {self.p2.type.value[0].upper()}@{self.p2.index}, "
                f"rel_slope={self.rel_slope:+.5f})")


@dataclass
class Pattern:
    """形态候选 / 已确认信号"""
    symbol: str
    interval: str
    pattern_type: str
    direction: Direction
    status: PatternStatus

    # 几何
    pivots: List[Pivot] = field(default_factory=list)
    neckline: Optional[Line] = None
    upper_boundary: Optional[Line] = None
    lower_boundary: Optional[Line] = None
    height: float = 0.0

    # 突破
    breakout_index: int = -1
    breakout_price: float = 0.0
    breakout_magnitude_atr: float = 0.0      # 突破幅度 / ATR
    breakout_age: int = -1                   # 突破点距今多少根K线（-1=未突破）

    # 确认指标
    volume_ratio: float = 0.0                # 突破量 / 20根均量
    confirmed_candles: int = 0

    # 交易价位（由 calc_trade_levels 填充）
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0
    risk_reward: float = 0.0

    # 置信度 0~1（几何完整度，不含量能/共振等外部因素）
    confidence: float = 0.0

    # 综合信号强度 0~100（七维加权，含量能/共振/趋势一致性等）
    # 由 crosstf.SignalScorer 计算，见 docs/01 1.4
    strength_score: int = 0

    # 多周期共振信息
    resonant_with: List[str] = field(default_factory=list)
    conflict_with: List[str] = field(default_factory=list)

    # 单K线确认（TA-Lib 蜡烛图形态，如 "吞没形态(看涨)"）
    candle_confirmations: List[str] = field(default_factory=list)

    def boundary_at(self, index: int) -> Optional[float]:
        """
        返回该形态在指定 K 线处的突破判定边界。

        优先级：颈线（双顶/双底/头肩）> 突破方向对应的边界（三角形/旗形/楔形）
        """
        if self.neckline is not None:
            return self.neckline.value_at(index)
        if self.direction == Direction.LONG and self.upper_boundary is not None:
            return self.upper_boundary.value_at(index)
        if self.direction == Direction.SHORT and self.lower_boundary is not None:
            return self.lower_boundary.value_at(index)
        return None

    def is_breakout_intact(self, klines: List[Kline],
                           tolerance_atr: float = 0.0,
                           atr_value: float = 0.0) -> bool:
        """
        突破是否仍然有效——最新收盘价是否还在突破方向的正确一侧。

        为什么需要这个检查（实测发现的漏洞）：

        GIGGLEUSDT 4h 上升三角形，在第 115 根以 44.81 突破上边界，
        当时满足全部确认条件（幅度 2.97×ATR、量能 9.33×、连确认 5 根），
        系统判定 CONFIRMED 并生成了 R:R=1:2.7 的信号。
        但到最新一根价格已跌回 40.17——跌破入场价，是个失败的多头陷阱。

        如果不检查，扫描器会把"已经失败的突破"当成机会推送。
        这是假突破场景，必须拦截。
        """
        if not klines or self.breakout_index < 0:
            return False

        boundary = self.boundary_at(len(klines) - 1)
        if boundary is None:
            return False

        last_close = klines[-1].close
        slack = tolerance_atr * atr_value

        if self.direction == Direction.LONG:
            return last_close >= boundary - slack
        else:
            return last_close <= boundary + slack

    def __repr__(self):
        return (f"Pattern({self.symbol} {self.interval} {self.pattern_type} "
                f"{self.direction.value} {self.status.value} "
                f"conf={self.confidence:.2f})")


# ============================================================
#  几何工具：趋势线拟合
# ============================================================

def fit_trendline(pivots: List[Pivot], use_type: PivotType,
                  min_touches: int = 2,
                  tolerance: float = 0.02,
                  min_span: int = 5) -> Tuple[Optional[Line], int]:
    """
    在指定类型的摆动点中拟合一条趋势线。

    算法：
      1. 枚举所有摆动点对 (i, j)，j > i
      2. 对每对，计算直线，统计有多少个摆动点落在该线附近（容差内）
      3. 优先取【触点最多】的线；触点相同时取【跨度更大】的（更稳定）

    参数：
      use_type    —— HIGH 拟合上边界（压力线），LOW 拟合下边界（支撑线）
      min_touches —— 最少触点数（三角形通常要求 ≥2，复杂形态要求 ≥3）
      tolerance   —— 触点判定容差（相对价格）
      min_span    —— 两点最小间隔（防止相邻摆动点连出无意义的陡线）

    返回: (最佳线 或 None, 触点数)
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
            if p2.index - p1.index < min_span:
                continue

            line = Line(p1, p2)

            # 统计落在这条线附近的摆动点数量
            touches = 0
            for p in pts:
                expected = line.value_at(p.index)
                if expected == 0:
                    continue
                if abs(p.price - expected) / expected <= tolerance:
                    touches += 1

            # 优先级：触点数 > 跨度
            if touches > best_touches or (touches == best_touches
                                          and touches >= min_touches
                                          and line.span > best_span):
                best_touches = touches
                best_span = line.span
                best_line = line

    if best_line is None or best_touches < min_touches:
        return None, best_touches

    return best_line, best_touches


def count_touches(line: Line, pivots: List[Pivot], tolerance: float = 0.02) -> int:
    """统计有多少摆动点落在该趋势线附近"""
    count = 0
    for p in pivots:
        expected = line.value_at(p.index)
        if expected == 0:
            continue
        if abs(p.price - expected) / expected <= tolerance:
            count += 1
    return count


def horizontal_line(price: float, start_index: int,
                    span: int = 10, ptype: PivotType = PivotType.LOW) -> Line:
    """
    构造一条水平线（颈线、箱体边界等常用）。

    Line 需要两个端点，这里用"同一价格、相隔 span 根 K 线"的两个虚拟点构造，
    这样 slope=0、value_at() 恒等于该价格。
    """
    p1 = Pivot(index=start_index, price=price, type=ptype, timestamp=0)
    p2 = Pivot(index=start_index + span, price=price, type=ptype, timestamp=0)
    return Line(p1, p2)


def is_flat(line: Line, threshold: float = 0.0005) -> bool:
    """趋势线是否近似水平（相对斜率绝对值 ≤ 阈值）"""
    return abs(line.rel_slope) <= threshold


def is_rising(line: Line, threshold: float = 0.0005) -> bool:
    return line.rel_slope > threshold


def is_falling(line: Line, threshold: float = 0.0005) -> bool:
    return line.rel_slope < -threshold


# ============================================================
#  确认工具
# ============================================================

def calc_volume_ratio(klines: List[Kline], index: int,
                      lookback: int = 20) -> float:
    """
    突破 K 线的成交量 / 前 N 根均量。

    同时考虑前一根（避免单根插针导致误判）。
    """
    if index <= 0 or index >= len(klines):
        return 0.0

    start = max(0, index - lookback)
    if index - start < 2:
        return 0.0

    avg_vol = sum(k.volume for k in klines[start:index]) / (index - start)
    if avg_vol <= 0:
        return 0.0

    current = klines[index].volume
    prev = klines[index - 1].volume if index >= 1 else current

    # 取两根中较大者，但前一根打折计入（0.7权重）
    return max(current / avg_vol, (prev / avg_vol) * 0.7)


def check_breakout(klines: List[Kline],
                   breakout_index: int,
                   boundary_price_at_index: float,
                   direction: Direction,
                   atr_value: float,
                   required_candles: int = 2,
                   min_magnitude_atr: float = 0.5,
                   max_check: int = 5) -> Tuple[bool, int, float]:
    """
    突破确认：连续 N 根收盘价站在边界外，且幅度足够。

    返回: (是否确认, 连续确认根数, 突破幅度/ATR)

    设计要点：
      - 用【收盘价】而非最高/最低价，避免影线刺穿造成假信号
      - 边界是斜线时，每根 K 线要用该位置的边界值（boundary_fn），
        这里简化为传入一个函数
    """
    if breakout_index < 0 or breakout_index >= len(klines):
        return False, 0, 0.0

    confirmed = 0
    for i in range(breakout_index, min(breakout_index + max_check, len(klines))):
        close = klines[i].close
        if direction == Direction.SHORT and close < boundary_price_at_index:
            confirmed += 1
        elif direction == Direction.LONG and close > boundary_price_at_index:
            confirmed += 1
        else:
            break       # 中断即视为未持续确认

    magnitude = 0.0
    if atr_value > 0:
        magnitude = abs(klines[breakout_index].close - boundary_price_at_index) / atr_value

    ok = (confirmed >= required_candles) and (magnitude >= min_magnitude_atr)
    return ok, confirmed, magnitude


def find_breakout_index(klines: List[Kline],
                        start_index: int,
                        boundary_fn,
                        direction: Direction,
                        max_lookahead: int = 30) -> int:
    """
    从 start_index 往后找第一根突破边界的 K 线。

    boundary_fn: 接受 K 线索引，返回该位置的边界价格（支持斜线）
    """
    end = min(start_index + max_lookahead, len(klines))
    for i in range(start_index, end):
        boundary = boundary_fn(i)
        close = klines[i].close
        if direction == Direction.SHORT and close < boundary:
            return i
        if direction == Direction.LONG and close > boundary:
            return i
    return -1


# ============================================================
#  交易价位计算
# ============================================================

def calc_trade_levels(pattern: Pattern,
                      klines: List[Kline],
                      atr_value: float,
                      atr_stop_multiplier: float = 1.5,
                      tp1_ratio: float = 1.0,
                      tp2_ratio: float = 1.618) -> None:
    """
    计算入场/止损/止盈，直接写入 pattern 对象。

    规则（见 docs/04-notification.md）：
      入场 = 突破确认 K 线的收盘价
      止损 = 颈线（或边界）外侧 1.5×ATR
      止盈1 = 入场 ± 形态高度 × 1.0
      止盈2 = 入场 ± 形态高度 × 1.618
    """
    # 入场价取【最新收盘价】，而不是突破K线的收盘价。
    #
    # 实测案例：GIGGLEUSDT 4h 上升三角形在第115根以 44.81 突破，
    # 到第119根价格已回踩至 40.00（但仍在边界 38.58 上方，突破有效）。
    # 若按突破价 44.81 报入场，等于让用户比现价高买 12%——完全不可执行。
    #
    # 用最新收盘价还有一个额外好处：突破后的回踩本身就是更好的入场位
    # （距边界更近 → 止损更紧 → 风险回报比更高），即经典的"回踩买入"。
    entry = klines[-1].close if klines else pattern.breakout_price
    if entry <= 0:
        entry = pattern.breakout_price
    if entry <= 0:
        return

    # --- 止损候选 ---
    candidates = []

    if pattern.neckline is not None:
        neck_at = pattern.neckline.value_at(pattern.breakout_index)
        if pattern.direction == Direction.SHORT:
            candidates.append(neck_at + atr_stop_multiplier * atr_value)
        else:
            candidates.append(neck_at - atr_stop_multiplier * atr_value)

    if pattern.lower_boundary is not None:
        b = pattern.lower_boundary.value_at(pattern.breakout_index)
        if pattern.direction == Direction.SHORT:
            candidates.append(b + 0.5 * atr_value)
        else:
            candidates.append(b - 0.5 * atr_value)

    if pattern.upper_boundary is not None:
        b = pattern.upper_boundary.value_at(pattern.breakout_index)
        if pattern.direction == Direction.SHORT:
            candidates.append(b + 0.5 * atr_value)
        else:
            candidates.append(b - 0.5 * atr_value)

    # ATR 固定倍数兜底
    if pattern.direction == Direction.SHORT:
        candidates.append(entry + 2.0 * atr_value)
    else:
        candidates.append(entry - 2.0 * atr_value)

    # 取最靠近入场价的有效止损（亏损最小）
    if pattern.direction == Direction.SHORT:
        valid = [c for c in candidates if c > entry]
        stop = min(valid) if valid else entry + 2.0 * atr_value
    else:
        valid = [c for c in candidates if c < entry]
        stop = max(valid) if valid else max(entry - 2.0 * atr_value, entry * 0.5)

    # --- 止盈 ---
    h = pattern.height
    if pattern.direction == Direction.SHORT:
        tp1 = entry - h * tp1_ratio
        tp2 = entry - h * tp2_ratio
    else:
        tp1 = entry + h * tp1_ratio
        tp2 = entry + h * tp2_ratio

    # 价格不能为负
    tp1 = max(tp1, entry * 0.1)
    tp2 = max(tp2, entry * 0.1)

    risk = abs(entry - stop)
    reward = abs(tp1 - entry)
    rr = reward / risk if risk > 0 else 0.0

    pattern.entry_price = entry
    pattern.stop_loss = stop
    pattern.take_profit_1 = tp1
    pattern.take_profit_2 = tp2
    pattern.risk_reward = rr


# ============================================================
#  检测器基类
# ============================================================

class BaseDetector:
    """
    所有形态检测器的基类。

    子类只需实现 detect()，基类负责：
      - 参数注入
      - 突破确认
      - 交易价位计算
    """

    name = "base"
    # 该检测器产出的形态类型名（子类覆盖）
    pattern_type = "unknown"

    def __init__(self, params: Optional[dict] = None):
        self.params = params or {}

    def detect(self, klines: List[Kline], pivots: List[Pivot],
               atr_value: float, symbol: str = "",
               interval: str = "") -> List[Pattern]:
        raise NotImplementedError

    # --- 通用确认流程 ---
    def _confirm(self, pattern: Pattern, klines: List[Kline],
                 atr_value: float, boundary_fn,
                 start_search_index: int) -> bool:
        """
        统一的突破确认 + 价位计算流程。

        返回 True 表示形态已确认（CONFIRMED），False 表示仍为候选或失败。
        """
        idx = find_breakout_index(
            klines, start_search_index, boundary_fn, pattern.direction
        )
        if idx < 0:
            pattern.status = PatternStatus.CANDIDATE
            return False

        boundary_at = boundary_fn(idx)
        ok, confirmed, magnitude = check_breakout(
            klines, idx, boundary_at, pattern.direction, atr_value,
            required_candles=self.params.get("breakout_candles", 2),
            min_magnitude_atr=self.params.get("breakout_atr_ratio", 0.5),
        )

        pattern.breakout_index = idx
        pattern.breakout_price = klines[idx].close
        pattern.breakout_magnitude_atr = magnitude
        pattern.confirmed_candles = confirmed
        pattern.volume_ratio = calc_volume_ratio(klines, idx)

        # 量能门槛
        min_vol = self.params.get("volume_ratio_min", 1.5)
        if pattern.volume_ratio < min_vol:
            pattern.status = PatternStatus.CANDIDATE
            return False

        if not ok:
            pattern.status = PatternStatus.CANDIDATE
            return False

        pattern.status = PatternStatus.CONFIRMED
        calc_trade_levels(pattern, klines, atr_value)
        return True
