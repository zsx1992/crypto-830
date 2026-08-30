# -*- coding: utf-8 -*-
"""
形态检测引擎（orchestrator）

职责：
  1. 组装所有检测器
  2. 对每个 (symbol, interval) 跑一遍全部检测器
  3. 同一标的多形态去重（如同时命中三角形和楔形，保留置信度高的）
  4. 统一过滤（量能、R:R、置信度）

当前阶段：只做单周期识别。多周期交叉确认（第二章 2.2.4）在阶段 3 接入。
"""

import os
import sys
import logging
from typing import List, Dict, Optional

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from market_data import Kline
from zigzag import Pivot, find_pivots
from indicators import calc_indicators
from patterns import (
    Pattern, Direction, PatternStatus,
    DoubleTopBottomDetector, HeadShouldersDetector,
    TriangleDetector, FlagDetector, WedgeDetector,
)

logger = logging.getLogger(__name__)


# 形态中文名（用于推送消息）
PATTERN_NAMES = {
    "double_top": "双顶",
    "double_bottom": "双底",
    "head_shoulders_top": "头肩顶",
    "head_shoulders_bottom": "头肩底",
    "ascending_triangle": "上升三角形",
    "descending_triangle": "下降三角形",
    "symmetrical_triangle": "对称三角形",
    "flag": "旗形",
    "rising_wedge": "上升楔形",
    "falling_wedge": "下降楔形",
}


class PatternEngine:
    """形态检测引擎"""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        pattern_cfg = self.config.get("patterns", {})

        # 各检测器共享的确认参数
        common = {
            "breakout_candles": self._get(pattern_cfg,
                                          ["confirmation", "breakout_candles"], 2),
            "breakout_atr_ratio": self._get(pattern_cfg,
                                            ["confirmation", "breakout_atr_ratio"], 0.5),
            "volume_ratio_min": self._get(pattern_cfg,
                                          ["confirmation", "volume_ratio_min"], 1.5),
        }
        tol = pattern_cfg.get("tolerance", {})
        span = pattern_cfg.get("span", {})

        # --- 组装检测器 ---
        self.detectors = [
            DoubleTopBottomDetector({
                **common,
                "peak_tolerance": tol.get("peak_price", 0.08),
                "min_depth": tol.get("shoulder_ratio", 0.03),
                "min_span": span.get("double_top_min", 8),
                "max_span": span.get("double_top_max", 150),
            }),
            HeadShouldersDetector({
                **common,
                "shoulder_tolerance": tol.get("peak_price", 0.08),
                "neck_tolerance": tol.get("neck_slope_max", 0.08),
                "head_prominence": tol.get("head_prominence", 0.01),
                "min_span": span.get("head_shoulders_min", 15),
                "max_span": span.get("head_shoulders_max", 220),
            }),
            TriangleDetector({
                **common,
                "touch_tolerance": tol.get("touch_penetration", 0.02),
                "min_touches": 2,
                "min_span": span.get("triangle_min_span", 12),
                "max_span": span.get("triangle_max_span", 160),
                "min_height_atr": span.get("triangle_min_height_atr", 1.0),
            }),
            FlagDetector({
                **common,
                "pole_min_move": span.get("flag_pole_min_move", 0.03),
                "pole_max_bars": span.get("flag_pole_min_bars", 3),
            }),
            WedgeDetector({
                **common,
                "touch_tolerance": tol.get("touch_penetration", 0.02),
            }),
        ]

        # 过滤阈值
        filt = self.config.get("filter", {})
        self.min_confidence = filt.get("min_confidence", 0.4)
        self.min_rr = filt.get("min_rr", 1.5)
        self.min_volume = self._get(pattern_cfg,
                                    ["confirmation", "volume_ratio_min"], 1.5)

        # 新鲜度窗口（按周期）
        self.freshness_bars = filt.get("freshness_bars", {})
        self.freshness_default = filt.get("freshness_default", 6)

        # 多尺度扫描的 ZigZag 窗口列表
        self.multiscale_scales = self.config.get(
            "zigzag", {}).get("multiscale", [5])

        # 是否要求突破在最新K线仍然有效（拦截假突破）
        self.require_intact_breakout = filt.get("require_intact_breakout", True)

    def freshness_for(self, interval: str) -> int:
        """取该周期的新鲜度窗口（根）"""
        return self.freshness_bars.get(interval, self.freshness_default)

    @staticmethod
    def _get(d: dict, path: list, default):
        cur = d
        for k in path:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(k)
            if cur is None:
                return default
        return cur

    # ---------- 主入口 ----------

    def scan(self, klines: List[Kline], pivots: List[Pivot],
             atr_value: float, symbol: str = "",
             interval: str = "") -> List[Pattern]:
        """
        对单个 (symbol, interval) 跑全部检测器。

        返回去重后的形态列表（含 CANDIDATE 和 CONFIRMED）。
        """
        if not klines or not pivots or atr_value <= 0:
            return []

        candidates: List[Pattern] = []

        for det in self.detectors:
            try:
                found = det.detect(klines, pivots, atr_value, symbol, interval)
                if found:
                    candidates.extend(found)
            except Exception as e:
                logger.warning(f"[{det.name}] 检测异常 {symbol} {interval}: {e}")

        # 计算突破点新鲜度（距今多少根K线）
        last_index = len(klines) - 1
        for p in candidates:
            if p.breakout_index >= 0:
                p.breakout_age = last_index - p.breakout_index

        # 检查突破是否仍然有效（防止推送已经失败的假突破）
        #
        # 实测案例：GIGGLEUSDT 4h 上升三角形在 #115 以 44.81 突破，
        # 当时满足全部确认条件，但到最新一根价格已跌回 40.17（跌破入场价）。
        # 这是典型的假突破/多头陷阱，若不检查就会被当作机会推送。
        if self.require_intact_breakout:
            for p in candidates:
                if p.status == PatternStatus.CONFIRMED:
                    if not p.is_breakout_intact(klines, atr_value=atr_value):
                        p.status = PatternStatus.FAILED

        # 去重 + 排序
        return self._deduplicate(candidates)

    def scan_symbol(self, klines: List[Kline], symbol: str = "",
                    interval: str = "15m",
                    zigzag_params: Optional[tuple] = None) -> List[Pattern]:
        """
        便捷入口：从原始 K 线直接完成 指标→摆动点→识别 全流程。
        """
        if not klines:
            return []

        indicators = calc_indicators(klines)
        left, right = zigzag_params or (5, 5)
        pivots = find_pivots(klines, left=left, right=right)

        return self.scan(klines, pivots, indicators.atr_current,
                         symbol, interval)

    # ---------- 多尺度扫描 ----------

    def scan_multiscale(self, klines: List[Kline], symbol: str = "",
                        interval: str = "",
                        scales: Optional[List[int]] = None) -> List[Pattern]:
        """
        在多个 ZigZag 尺度上分别识别，然后合并结果。

        为什么需要多尺度（实测结论）：

        人眼看图时会自动做"视觉平滑"——放大看是小波动，缩小看就融进趋势了。
        而固定 left/right 的 ZigZag 只有一种尺度，必然会与人的视角错位。

        实测 82 个标注样本（判定窗口：形态末端距截图时刻 ≤25 根K线）：
            单尺度 (5,5) 精确类型命中率 :  8.5%
            多尺度 [3,5,8,12] 精确类型   : 15.9%   ← 接近翻倍
            多尺度 同族匹配             : 23.2%
            多尺度 任意类型             : 65.9%   ← 说明"这里有结构"能感知到

        结论：多尺度主要解决"漏检"，但"归类不一致"依然存在——
        这是图表形态识别的固有难题，人工标注之间的一致率本身也只有 50~70%。
        """
        if not klines:
            return []

        scales = scales or self.multiscale_scales
        indicators = calc_indicators(klines)
        atr = indicators.atr_current
        if atr <= 0:
            return []

        all_found: List[Pattern] = []
        for s in scales:
            pivots = find_pivots(klines, left=s, right=s)
            if len(pivots) < 3:
                continue
            try:
                found = self.scan(klines, pivots, atr, symbol, interval)
                # 记录该形态是在哪个尺度下检出的（便于诊断）
                for p in found:
                    p.confidence = round(p.confidence, 3)
                all_found.extend(found)
            except Exception as e:
                logger.warning(f"[multiscale s={s}] {symbol} {interval}: {e}")

        return self._deduplicate(all_found)

    # ---------- 去重 ----------

    def _deduplicate(self, patterns: List[Pattern]) -> List[Pattern]:
        """
        同一标的的形态去重。

        规则：
          1. 同一 pattern_type 只保留置信度最高 + 状态最好的那个
          2. 若不同形态的突破点非常接近（±3根K线）且方向相同，
             只保留置信度高的（避免"三角形"和"楔形"重复报同一个结构）
        """
        if not patterns:
            return []

        # ① 同类型去重
        by_type: Dict[str, Pattern] = {}
        for p in patterns:
            key = f"{p.pattern_type}_{p.direction.value}"
            existing = by_type.get(key)
            if existing is None:
                by_type[key] = p
            else:
                if self._better(p, existing):
                    by_type[key] = p

        kept = list(by_type.values())

        # ② 跨类型去重：突破点接近且同方向
        kept.sort(key=lambda x: -x.confidence)
        final: List[Pattern] = []
        for p in kept:
            duplicate = False
            for q in final:
                if (q.direction == p.direction
                        and p.breakout_index > 0 and q.breakout_index > 0
                        and abs(p.breakout_index - q.breakout_index) <= 3):
                    duplicate = True
                    break
            if not duplicate:
                final.append(p)

        return final

    @staticmethod
    def _better(a: Pattern, b: Pattern) -> bool:
        """a 是否比 b 更值得保留"""
        # 已确认 > 候选
        if a.status == PatternStatus.CONFIRMED and b.status != PatternStatus.CONFIRMED:
            return True
        if b.status == PatternStatus.CONFIRMED and a.status != PatternStatus.CONFIRMED:
            return False
        # 同状态下比置信度
        return a.confidence > b.confidence

    # ---------- 过滤 ----------

    def filter_signals(self, patterns: List[Pattern],
                       max_age: Optional[int] = None) -> List[Pattern]:
        """
        统一过滤，只保留值得推送的信号。

        过滤条件：
          1. 必须已确认（CONFIRMED）
          2. 置信度 >= min_confidence
          3. 量能比 >= min_volume
          4. 风险回报比 >= min_rr
          5. 突破点足够新鲜（max_age 为 None 时不限制）

        关于第 5 条（新鲜度）——这是实测发现的严重问题：

        不加新鲜度约束时，实测 20 标的×3 周期 = 60 次扫描产出的
        "已确认信号"，其突破点距今 age = [63, 74, 79, 88, 105, 132, 164] 根 K 线。
        也就是说系统推送的全是 2.6~6.8 天前的旧突破，毫无交易价值。

        原因：检测器扫描整段历史，会找到"早期形成、早已突破"的形态。
        这些形态【在回测中是有价值的样本】（用于统计胜率），
        但【在实盘扫描中必须剔除】。

        因此：
          - 回测模式：max_age=None，保留全部（用于统计历史胜率）
          - 实盘模式：max_age=按周期设定的新鲜度窗口，只报最近的突破
        """
        passed = []
        for p in patterns:
            if p.status != PatternStatus.CONFIRMED:
                continue
            if p.confidence < self.min_confidence:
                continue
            if p.volume_ratio < self.min_volume:
                continue
            if p.risk_reward < self.min_rr:
                continue
            if max_age is not None and p.breakout_age > max_age:
                continue
            passed.append(p)

        passed.sort(key=lambda x: (-x.confidence, -x.risk_reward))
        return passed
