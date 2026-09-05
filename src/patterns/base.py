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

    # 形态末端时间（最后一个 pivot 对应 K 线的 openTime, 毫秒）
    # 由 scanner / history_replay 在推送前填充。
    # 用途：判断"新检出的形态是不是上次推过的同一个"——
    # 同一形态冷却期过后仍挂在图上时不应重复推送。
    end_ms: int = 0

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

    # 几何质量分 0~1（借鉴 hunk77 对称性+结构质量加权思路，由 validate_geometry 计算）
    # 与 confidence 的区别：confidence 是检测器内部二值门槛后的完整度，
    # geometry_score 是【分级】的"画得标不标准"评分（价格对称+时间对称+结构质量）。
    geometry_score: float = 0.0
    geometry_reason: str = ""

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


def channel_quality(upper: "Line", lower: "Line", pivots: List[Pivot],
                    klines: List["Kline"], start_index: int, end_index: int
                    ) -> Tuple[float, str]:
    """
    通道质量 0~1：衡量「能不能一眼看出这是个通道」。

    这是朱哥判「像不像」的真实维度（2026-09-03 从 158 张标注反推）：
    他认可的图「起码能看出来是通道」，而不是靠收敛度/对称性。
    旧 touch_score = min(1, touches/4) 恒为 1.0（检测器已要求 min_touches），
    完全无区分度 —— 本函数用连续量替代它。

    两项各占一半：
      1. 贴合度 adherence —— 摆动点离对应边界线多远（相对通道宽度）。
         点都贴在线上 = 线清晰可辨；点散在线外 = 看不出线。
      2. 包容性 containment —— 区间内 K 线有多少落在两条边界之间。
         价格在通道里有秩序地来回 = 像；频繁穿越 = 只是震荡。

    返回 (score 0~1, reason)。

    ⚠️ 已验证无效，不要拿它当筛选器（2026-09-03，158 张人工标注）：
       实测 91 张里几乎全是 1.000（贴合1.00/包容1.00），
       朱哥判「像」均值 0.959 vs「不像」0.989，差 −0.030 ≈ 0。

       失效原因 = 循环论证：
       ① 贴合度用 pattern.pivots 去测由【同一批 pivot】拟合出的边界线，
          必然完美贴合（就像用考试答案去批改这份答卷）；
       ② 包容性同理——区间 [start,end] 本就由 pivot 位置界定，
          区间内 K 线天然落在极值连线之间。
       要评估「像不像通道」，必须用【独立于拟合过程】的样本
       （例如形态区间之外的 K 线、或收紧到 1~2% 的容差）。
       保留此函数仅为记录该思路，当前不在 validate_geometry 中使用。
    """
    if start_index >= end_index or not klines:
        return 0.0, "无效区间"

    # 基准宽度取区间起点处（收敛形态右端会趋近 0，不能拿来当分母）
    width0 = upper.value_at(start_index) - lower.value_at(start_index)
    if width0 <= 0:
        return 0.0, "通道宽度≤0"

    # ① 贴合度：摆动点到对应边界的平均偏离
    devs = []
    for p in pivots:
        if not (start_index <= p.index <= end_index):
            continue
        line = upper if p.type == PivotType.HIGH else lower
        expected = line.value_at(p.index)
        devs.append(abs(p.price - expected))
    if not devs:
        return 0.0, "区间内无摆动点"
    mean_dev = sum(devs) / len(devs)
    # 偏离达到 12% 通道宽度即视为「看不出线」
    adherence = max(0.0, min(1.0, 1.0 - mean_dev / (0.12 * width0)))

    # ② 包容性：K 线是否待在通道里（允许 8% 宽度的越界容差）
    inside = 0
    total = 0
    for i in range(start_index, min(end_index + 1, len(klines))):
        uv = upper.value_at(i)
        lv = lower.value_at(i)
        w = uv - lv
        if w <= 0:
            continue
        tol = 0.08 * w
        k = klines[i]
        total += 1
        if k.high <= uv + tol and k.low >= lv - tol:
            inside += 1
    containment = inside / total if total else 0.0

    score = 0.5 * adherence + 0.5 * containment
    reason = f"贴合{adherence:.2f}/包容{containment:.2f}"
    return round(max(0.0, min(1.0, score)), 3), reason


def local_extrema(klines: List["Kline"], start_index: int, end_index: int,
                  order: int = 2):
    """
    找区间内的【局部极值】（比 zigzag pivot 密集得多）。

    这是评估形态质量的独立样本：检测器拟合边界用的是 zigzag pivot
    （left/right = 3~12，一个形态通常只有 4 个），而这里用 order=2
    能在一个 80 根的形态里找到 ~16 个极值点，且它们没有参与原拟合。

    返回 (highs, lows)，元素为 (index, price)。
    """
    highs, lows = [], []
    lo_i = max(1, start_index)
    hi_i = min(len(klines) - 2, end_index)
    for i in range(lo_i + order, hi_i - order + 1):
        h = klines[i].high
        l = klines[i].low
        if i + order >= len(klines) or i - order < 0:
            continue
        is_high = all(klines[j].high <= h
                      for j in range(i - order, i + order + 1) if j != i)
        is_low = all(klines[j].low >= l
                     for j in range(i - order, i + order + 1) if j != i)
        if is_high:
            highs.append((i, h))
        if is_low:
            lows.append((i, l))
    return highs, lows


def structure_orderliness(upper: "Line", lower: "Line",
                          klines: List["Kline"], start_index: int,
                          end_index: int, pattern_type: str = ""):
    """
    结构秩序感 0~1：用【独立于拟合过程】的样本评估形态。

    与 channel_quality（已验证无效）的区别：
      channel_quality 用参与拟合的那 4 个 pivot 去测拟合出的线 → 必然满分；
      本函数改用区间内 order=2 的密集局部极值（~16 个，未参与拟合）+ 全量 K 线。

    三项：
      ① 单调性(40%) —— 高点/低点是否朝形态声称的方向依次推进。
         真上升通道的高点应逐个抬高；震荡区间则忽高忽低。
      ② 贴合度(30%) —— 这些独立极值点离边界线多远（相对通道宽度）。
      ③ 穿越率(30%) —— 全量 K 线以 1% 宽度容差越界的比例（越界越多越不像）。

    返回 (score 0~1, reason)。
    """
    if start_index >= end_index or not klines:
        return 0.0, "无效区间"

    width0 = upper.value_at(start_index) - lower.value_at(start_index)
    if width0 <= 0:
        return 0.0, "通道宽度≤0"

    highs, lows = local_extrema(klines, start_index, end_index, order=2)
    if len(highs) < 2 or len(lows) < 2:
        return 0.0, "极值点不足"

    # ① 单调性：形态声称的方向
    pt = pattern_type
    if pt in ("rising_wedge", "ascending_triangle"):
        want_high, want_low = 1, 1          # 高点抬高、低点抬高
    elif pt in ("falling_wedge", "descending_triangle"):
        want_high, want_low = -1, -1        # 高点降低、低点降低
    elif pt == "symmetrical_triangle":
        want_high, want_low = -1, 1         # 高点降低、低点抬高（收敛）
    else:
        # 未知类型：取「更一致的那个方向」的秩序度（保守，不奖励随机）
        def ratio(seq, sign):
            return (sum(1 for a, b in zip(seq, seq[1:])
                        if (b[1] - a[1]) * sign > 0) / max(len(seq) - 1, 1))
        want_high = 1 if ratio(highs, 1) >= ratio(highs, -1) else -1
        want_low = 1 if ratio(lows, 1) >= ratio(lows, -1) else -1

    h_ratio = sum(1 for a, b in zip(highs, highs[1:])
                  if (b[1] - a[1]) * want_high > 0) / max(len(highs) - 1, 1)
    l_ratio = sum(1 for a, b in zip(lows, lows[1:])
                  if (b[1] - a[1]) * want_low > 0) / max(len(lows) - 1, 1)
    mono = 0.5 * h_ratio + 0.5 * l_ratio

    # ② 贴合度：独立极值点到边界线的偏离
    devs = []
    for i, p in highs:
        devs.append(abs(p - upper.value_at(i)))
    for i, p in lows:
        devs.append(abs(p - lower.value_at(i)))
    mean_dev = sum(devs) / len(devs)
    adherence = max(0.0, min(1.0, 1.0 - mean_dev / (0.15 * width0)))

    # ③ 穿越率：全量 K 线，严格容差 1% 通道宽度
    breach = 0
    total = 0
    for i in range(start_index, min(end_index + 1, len(klines))):
        uv = upper.value_at(i)
        lv = lower.value_at(i)
        w = uv - lv
        if w <= 0:
            continue
        tol = 0.01 * w
        k = klines[i]
        total += 1
        if k.high > uv + tol or k.low < lv - tol:
            breach += 1
    breach_rate = breach / total if total else 1.0
    containment = 1.0 - breach_rate

    score = 0.40 * mono + 0.30 * adherence + 0.30 * containment
    reason = (f"序{mono:.2f}/贴{adherence:.2f}/容{containment:.2f}"
              f"(点{len(highs) + len(lows)})")
    return round(max(0.0, min(1.0, score)), 3), reason


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
#  几何质量评估（借鉴 hunk77 的"对称性 + 结构质量"加权思路）
# ============================================================

def _price_symmetry(a: float, b: float, tol: float = 0.05) -> float:
    """
    价格对称性 0~1：a、b 越接近越高；相对差达到 tol 时得 0。

    hunk77 把对称性作为置信度的主要权重（~32%）：
    双顶两峰、双底两谷、头肩两肩，价格必须接近才算标准形态。
    """
    denom = max(abs(a), abs(b), 1e-9)
    return max(0.0, min(1.0, 1.0 - abs(a - b) / denom / tol))


def prior_move(klines: List[Kline], before_index: int,
               bars: int) -> Optional[float]:
    """
    形态起点之前 close 的相对涨跌幅（正=涨，负=跌）。
    返回 None 表示窗口不足。

    用于「反转形态必须有前置趋势」的结构性闸门：
    双顶/头肩顶要求 prior_move > 0（前面涨上来），
    双底/头肩底要求 prior_move < 0（前面跌下来）。

    实测动机（2026-09-03 朱哥 158 张人工标注）：现存漏网误报里
    AAVE 双顶 / ENA 头肩顶 / ARB 双底 全是「下跌中继里的小 H-L-H」，
    形态内部结构满足但前面根本没趋势。本闸门就是为了把这批假信号杀掉。
    """
    start = max(0, before_index - bars)
    if before_index - start < 5:
        return None
    start_price = klines[start].close
    if start_price <= 0:
        return None
    return (klines[before_index].close - start_price) / start_price


def _time_symmetry(idx_left: int, idx_mid: int, idx_right: int,
                   tol: float = 0.5) -> float:
    """
    时间对称性 0~1：左臂(idx_mid-idx_left)与右臂(idx_right-idx_mid)越接近越高。

    crypto-830 原检测器【只查价格对称、不查时间对称】——这正是很多
    "几何过了但图上看着歪"的根因：两臂时长差很多照样过。hunk77 的
    结构质量里隐含了对形态左右均衡的要求，这里显式补上。
    """
    arm_l = abs(idx_mid - idx_left)
    arm_r = abs(idx_right - idx_mid)
    span = max(arm_l + arm_r, 1)
    return max(0.0, min(1.0, 1.0 - abs(arm_l - arm_r) / span / tol))


def convergence(upper: "Line", lower: "Line",
                start_index: int, end_index: int) -> float:
    """
    边界收敛度 0~1：两条边界在【同一个索引处】比较间距，看右端比左端窄了多少。
    =1 完全收拢到一点，=0 完全没收窄（或在发散）。

    为什么必须对齐到同一索引：
      旧实现写作
          left_gap  = |u.value_at(u.p1.index)  - lo.value_at(lo.p1.index)|
          right_gap = |u.value_at(u.p2.index)  - lo.value_at(lo.p2.index)|
      两条边界的 p1/p2 索引常常不同，等于在两个不同的 x 位置比价格 ——
      算出来的"收敛度"没有几何意义。实测大量真楔形被算成 0.00，
      导致几何分失去区分度（人工标注：像 0.38 vs 不像 0.41）。

    调用方应传入两条边界的共同跨度（通常取并集
    start=min(u.p1.index, lo.p1.index), end=max(u.p2.index, lo.p2.index)）。
    """
    if end_index <= start_index:
        return 0.0
    left_gap = abs(upper.value_at(start_index) - lower.value_at(start_index))
    right_gap = abs(upper.value_at(end_index) - lower.value_at(end_index))
    if left_gap <= 1e-9:
        return 0.0
    return max(0.0, min(1.0, (left_gap - right_gap) / left_gap))


def validate_geometry(pattern: "Pattern", atr_value: float = 0.0):
    """
    评估形态"画得标不标准"的几何质量分 0~1（分级，非二值）。

    返回 (score 0~1, reason: str)。

    设计（对应 hunk77 的 symmetry + structure quality）：
      - 反转形态（双顶/双底/头肩）：
          价格对称(两峰/两肩接近) + 时间对称(左右臂等长)
          + 结构(深度/头部突出度/颈线水平)
      - 持续形态（三角/楔形/旗形）：
          边界收敛度(三角/楔形) 或 平行度(旗形) + 触点数量

    这是【分级】质量分，用于把"勉强过关"的形态强度分压低，
    而不是直接丢弃——丢弃由扫描器的 min_strength 闸门决定。
    """
    pt = pattern.pattern_type
    pv = pattern.pivots
    if not pv:
        return 0.5, "无pivot"

    reversal = pt in ("double_top", "double_bottom",
                      "head_shoulders_top", "head_shoulders_bottom")
    if reversal:
        if pt in ("double_top", "double_bottom"):
            # 结构: [翼1, 中, 翼2]（峰-谷-峰 / 谷-峰-谷）
            w1, mid, w2 = pv[0], pv[1], pv[2]
            price_sym = _price_symmetry(w1.price, w2.price, tol=0.05)
            time_sym = _time_symmetry(w1.index, mid.index, w2.index, tol=0.5)
            # 2026-09-05: 剔除「深」子分。实测 209 张人工标注（v4）：
            #   深分 ok 均值 0.957 vs bad 0.983，差值 -0.026 ≈ 0，无区分度。
            #   公式 min(1, depth/0.10) 深度超 10% 即满分，而形态深度普遍 >10%
            #   → 0.20 权重纯送分，把几何总分虚高（"标准"样本 68% 被人否）。
            #   几何分只保留有信号的价对(+0.094)与时对(+0.157)，权重重归一。
            score = 0.55 * price_sym + 0.45 * time_sym
            reason = (f"价对{price_sym:.2f}/时对{time_sym:.2f}")
        else:
            # 头肩: [左肩, 颈, 头, 颈, 右肩]
            ls, n1, head, n2, rs = pv[0], pv[1], pv[2], pv[3], pv[4]
            sh_sym = _price_symmetry(ls.price, rs.price, tol=0.05)
            # 头部突出度：理想 5~15%（太突出像三重顶变体，太平像震荡）
            if pt == "head_shoulders_top":
                prom = (head.price - max(ls.price, rs.price)) / max(ls.price, rs.price)
            else:
                prom = (min(ls.price, rs.price) - head.price) / head.price
            prom_score = max(0.0, min(1.0, 1.0 - abs(prom - 0.10) / 0.10))
            time_sym = _time_symmetry(ls.index, head.index, rs.index, tol=0.5)
            neck_flat = _price_symmetry(n1.price, n2.price, tol=0.05)
            score = (0.30 * sh_sym + 0.20 * prom_score
                     + 0.30 * time_sym + 0.20 * neck_flat)
            reason = (f"肩对{sh_sym:.2f}/头突{prom_score:.2f}"
                      f"/时对{time_sym:.2f}/颈平{neck_flat:.2f}")
        return round(max(0.0, min(1.0, score)), 3), reason

    # 持续形态：三角 / 楔形 / 旗形
    if pattern.upper_boundary is not None and pattern.lower_boundary is not None:
        u, lo = pattern.upper_boundary, pattern.lower_boundary
        # 收敛度：右端间距 < 左端间距 → 收敛（三角/楔形都成立）
        # 注意：必须在【同一索引】上比较（用并集跨度），错位比较会让收敛度失真
        start_index = min(u.p1.index, lo.p1.index)
        end_index = max(u.p2.index, lo.p2.index)
        conv = convergence(u, lo, start_index, end_index)
        is_tri = "triangle" in pt
        is_wedge = "wedge" in pt
        if is_tri or is_wedge:
            structure = conv                       # 收敛（收窄）才标准
        else:                                      # 旗形：近似平行通道
            slope_diff = abs(u.rel_slope - lo.rel_slope)
            structure = max(0.0, min(1.0, 1.0 - slope_diff / 0.01))
        touches = count_touches(u, pv) + count_touches(lo, pv)
        touch_score = min(1.0, touches / 4.0)
        # 2026-09-05: 触分不再计入几何总分（仅保留在 reason 供诊断）。
        # 检测器已要求 min_touches（三角/楔形上下沿合计 ≥4），touches≥4 时
        # min(1, touches/4) 恒为 1.0 → 0.4 权重纯送分。实测 209 张人工标注：
        # 触分 ok 1.000 vs bad 1.000 零区分度，把三角/楔形总分虚高顶过"标准线"。
        score = structure
        reason = f"收敛{conv:.2f}/触{touch_score:.2f}"
        return round(max(0.0, min(1.0, score)), 3), reason

    return 0.5, "持续形态缺边界"


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

# 交易价位默认参数（全局，启动时由 PatternEngine 从 config 覆盖）
# 回测实测（49标的×2100根, 2026-09-01）：
#   tp1_ratio=1.0 → -0.09R    0.75 → +0.26R    0.5 → +0.56R
# 1×形态高度的目标太远，经常先回踩打止损；0.5x 更早落袋，胜率 30%→48.5%。
_tp1_ratio = 1.0
_tp2_ratio = 1.618
_atr_stop_multiplier = 1.5


def set_trade_level_params(tp1_ratio: float = None,
                           tp2_ratio: float = None,
                           atr_stop_multiplier: float = None) -> None:
    """由 PatternEngine 启动时调用，用 config.yaml 的 patterns.trade_levels 覆盖默认值。"""
    global _tp1_ratio, _tp2_ratio, _atr_stop_multiplier
    if tp1_ratio is not None:
        _tp1_ratio = tp1_ratio
    if tp2_ratio is not None:
        _tp2_ratio = tp2_ratio
    if atr_stop_multiplier is not None:
        _atr_stop_multiplier = atr_stop_multiplier


def calc_trade_levels(pattern: Pattern,
                      klines: List[Kline],
                      atr_value: float,
                      atr_stop_multiplier: float = None,
                      tp1_ratio: float = None,
                      tp2_ratio: float = None) -> None:
    """
    计算入场/止损/止盈，直接写入 pattern 对象。

    规则（见 docs/04-notification.md）：
      入场 = 突破确认 K 线的收盘价
      止损 = 颈线（或边界）外侧 1.5×ATR
      止盈1 = 入场 ± 形态高度 × tp1_ratio（config 可调，回测推荐 0.5）
      止盈2 = 入场 ± 形态高度 × 1.618
    """
    if atr_stop_multiplier is None:
        atr_stop_multiplier = _atr_stop_multiplier
    if tp1_ratio is None:
        tp1_ratio = _tp1_ratio
    if tp2_ratio is None:
        tp2_ratio = _tp2_ratio
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
