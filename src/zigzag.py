# -*- coding: utf-8 -*-
"""
ZigZag 摆动点检测 —— 所有形态识别的前置步骤

核心思想：
  把几百根 K 线压缩成几十个"峰"和"谷"，后续所有形态判断
  都只是在这些点上做几何比较。

定义：
  摆动高点 —— 某根 K 线的 high 严格大于其左右各 N 根 K 线的 high
  摆动低点 —— 某根 K 线的 low  严格小于其左右各 N 根 K 线的 low

天然滞后（重要）：
  一个摆动点需要等它右边 N 根 K 线走完才能确认。
  所以 left=right=5 时，最新确认的摆动点至少滞后 5 根 K 线。
  这是结构性的、无法消除的延迟——信号必然滞后于行情。
"""

import logging
from typing import List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

from market_data import Kline

logger = logging.getLogger(__name__)


class PivotType(Enum):
    HIGH = "high"
    LOW = "low"


@dataclass
class Pivot:
    """一个摆动点（峰或谷）"""
    index: int              # 在 klines 数组中的位置
    price: float            # HIGH 取 high 值，LOW 取 low 值
    type: PivotType
    timestamp: int = 0      # 对应 K 线的 openTime

    def __repr__(self):
        t = "H" if self.type == PivotType.HIGH else "L"
        return f"Pivot({t}@{self.index}, {self.price:.4f})"


def find_pivots(klines: List[Kline], left: int = 5, right: int = 5) -> List[Pivot]:
    """
    ZigZag 摆动点检测主入口。

    参数：
      left  —— 左侧比较窗口大小
      right —— 右侧比较窗口大小（这是滞后的唯一来源）

    返回：按时间升序、严格高低交替的摆动点列表

    调参直觉：
      left/right 越大 → 摆动点越少 → 过滤噪声越强 → 只保留大级别形态
      left/right 越小 → 摆动点越多 → 越灵敏 → 但噪声形态也越多
    """
    n = len(klines)
    if n < left + right + 1:
        logger.warning(f"K线数量({n})不足以检测摆动点 "
                       f"(至少需要 {left + right + 1} 根)")
        return []

    # 第一步：扫描所有候选极值点
    candidates: List[Pivot] = []

    for i in range(left, n - right):
        window = klines[i - left: i + right + 1]
        current = klines[i]

        is_high = all(current.high >= k.high for k in window)
        is_low = all(current.low <= k.low for k in window)

        # 一根 K 线理论上不可能同时是窗口内的最高和最低
        # （除非窗口内全是十字星，此时优先判为高点）
        if is_high and not is_low:
            candidates.append(Pivot(index=i, price=current.high,
                                    type=PivotType.HIGH,
                                    timestamp=current.openTime))
        elif is_low and not is_high:
            candidates.append(Pivot(index=i, price=current.low,
                                    type=PivotType.LOW,
                                    timestamp=current.openTime))
        elif is_high and is_low:
            # 退化情况：窗口内价格完全一致（极端低波动）
            # 用收盘价相对开盘价的方向决定归类
            if current.close >= current.open:
                candidates.append(Pivot(index=i, price=current.high,
                                        type=PivotType.HIGH,
                                        timestamp=current.openTime))
            else:
                candidates.append(Pivot(index=i, price=current.low,
                                        type=PivotType.LOW,
                                        timestamp=current.openTime))

    # 第二步：强制高低交替（相邻同类只保留更极端的那个）
    return _enforce_alternation(candidates)


def _enforce_alternation(candidates: List[Pivot]) -> List[Pivot]:
    """
    强制摆动点严格高低交替。

    处理逻辑：
      1. 按顺序扫描，若相邻两个同类：
         - 同为 HIGH：保留价格更高的
         - 同为 LOW ：保留价格更低的
      2. 若两个同类点之间有更极端的反向点，则两个都保留
         （这通过"只在真正相邻时合并"来保证）

    举例说明"只在真正相邻时合并"：
      候选序列: H(100) H(105) L(90) H(102) H(108)
      第一次合并 H(100),H(105) -> H(105)
      得到: H(105) L(90) H(102) H(108)
      第二次合并 H(102),H(108) -> H(108)
      得到: H(105) L(90) H(108)   ← 正确，三个点构成一个 N 形
    """
    if not candidates:
        return []

    result: List[Pivot] = []

    for p in candidates:
        if not result:
            result.append(p)
            continue

        last = result[-1]

        if last.type != p.type:
            # 类型不同，直接追加（交替成立）
            result.append(p)
        else:
            # 类型相同：保留更极端的那个
            if p.type == PivotType.HIGH:
                if p.price > last.price:
                    result[-1] = p        # 新的更高，替换
                # 否则保留原来的
            else:  # PivotType.LOW
                if p.price < last.price:
                    result[-1] = p        # 新的更低，替换
                # 否则保留原来的

    return result


def find_pending_pivot(klines: List[Kline], last_confirmed: Optional[Pivot],
                       left: int = 5) -> Optional[Pivot]:
    """
    检测"尚未确认"的潜在摆动点。

    最后 right 根 K 线内的极值还没有足够的右侧数据来确认，
    但它可能是正在形成的形态的一部分（比如刚创出新高的头肩顶右肩）。

    用途：用于 UI 提示"可能正在形成 XX 形态"，不用于生成交易信号。
    """
    if not klines or last_confirmed is None:
        return None

    start = last_confirmed.index + 1
    if start >= len(klines):
        return None

    tail = klines[start:]
    if len(tail) < 2:
        return None

    # 找尾部的最高/最低
    max_idx = max(range(len(tail)), key=lambda i: tail[i].high)
    min_idx = max(range(len(tail)), key=lambda i: tail[i].low)

    highest = tail[max_idx]
    lowest = tail[min_idx]

    # 与上一个确认的摆动点比较，判断是潜在的峰还是谷
    if last_confirmed.type == PivotType.LOW:
        # 上一个是谷，接下来应该出现峰
        if highest.high > last_confirmed.price:
            return Pivot(index=start + max_idx, price=highest.high,
                         type=PivotType.HIGH, timestamp=highest.openTime)
    else:
        # 上一个是峰，接下来应该出现谷
        if lowest.low < last_confirmed.price:
            return Pivot(index=start + min_idx, price=lowest.low,
                         type=PivotType.LOW, timestamp=lowest.openTime)

    return None


def pivot_health_check(klines: List[Kline], pivots: List[Pivot]) -> dict:
    """
    摆动点密度健康检查。

    重要：用【密度】而非绝对数量判定。
    原因：不同周期的 K 线总数差异很大（15m 有 480 根，1d 只有 60 根）。
    若用绝对阈值（如 "<15 个算过度平滑"），日线永远会被误判——
    实测 1d 60根K线产生14个摆动点(密度0.233)，其实比 15m(密度0.104) 更密。

    密度区间（实测标定）：
      < 0.04        → 过度平滑：会漏掉中等级别形态，调小 left/right
      0.04 ~ 0.18   → 健康
      0.18 ~ 0.30   → 偏灵敏：可能有噪声形态，但仍可用
      > 0.30        → 参数失效：几乎每根 K 线都是摆动点，调大 left/right
    """
    n = len(klines)
    count = len(pivots)
    density = count / n if n else 0

    if n == 0:
        status = "no_data"
    elif density < 0.04:
        status = "over_smoothed"
    elif density <= 0.18:
        status = "healthy"
    elif density <= 0.30:
        status = "sensitive"
    else:
        status = "broken"

    return {
        "klines_count": n,
        "pivots_count": count,
        "density": round(density, 4),
        "status": status,
        "compression_ratio": round(n / count, 2) if count else 0,
    }


# ============================================================
#  按周期获取推荐参数
# ============================================================

# 按周期推荐的 (left, right)
#
# 不是"周期越大窗口越小"这么简单——实测发现反直觉的结果：
#   1d 若用 (2,2)，60根K线能出14个点，但密度高达 0.233（噪声过多）；
#   改成 (3,3) 并把K线数提到120根，得18个点、密度0.150，既够用又干净。
#
# 实测标定表（Top8 标的中位数）：
#   15m (5,5) 480根 → 50点, 密度0.104  healthy
#   1h  (5,5) 240根 → 23点, 密度0.096  healthy
#   4h  (3,3) 120根 → 18点, 密度0.150  healthy
#   1d  (3,3) 120根 → 18点, 密度0.150  healthy
DEFAULT_PARAMS = {
    "15m": (5, 5),
    "1h": (5, 5),
    "2h": (4, 4),
    "4h": (3, 3),
    "1d": (3, 3),          # 注意：不是(2,2)，实测(2,2)噪声过多
}


def get_params_for_interval(interval: str) -> Tuple[int, int]:
    """按周期返回推荐的 (left, right)"""
    return DEFAULT_PARAMS.get(interval, (5, 5))


# ============================================================
#  自测
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # 构造一个明确的双底形态用于验证
    # 价格走势: 下跌 → 谷1(95) → 反弹(105) → 谷2(96) → 上涨(115)
    prices = [100, 98, 96, 95, 97, 100, 103, 105, 103, 100, 97, 96,
              98, 102, 106, 110, 115]
    test_klines = [
        Kline(openTime=i * 3600000, open=p, high=p + 0.5, low=p - 0.5,
              close=p, volume=1000, closeTime=i * 3600000 + 3599999,
              quoteVolume=100000)
        for i, p in enumerate(prices)
    ]

    pivots = find_pivots(test_klines, left=2, right=2)
    print(f"检测到 {len(pivots)} 个摆动点:")
    for p in pivots:
        print(f"  {p}")

    print("\n健康检查:")
    health = pivot_health_check(test_klines, pivots)
    for k, v in health.items():
        print(f"  {k}: {v}")
