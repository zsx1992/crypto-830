# -*- coding: utf-8 -*-
"""
多周期交叉确认 + 信号强度评分

两个职责：
  1. SignalScorer    —— 七维加权打分（0~100），见 docs/01 1.4
  2. CrossTimeframe  —— 四周期共振加分 / 矛盾扣分，见 docs/02 2.2.4

设计要点：
  周期有大小关系（15m < 1h < 4h < 1d）。
  大周期确认小周期 = 共振加分；大周期与小周期反向 = 矛盾扣分；
  小周期先于大周期突破 = 领先确认加分。
"""

import os
import sys
import logging
from typing import List, Dict, Optional

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from patterns.base import Pattern, Direction, PatternStatus, validate_geometry

logger = logging.getLogger(__name__)


# 周期大小顺序（索引越小周期越短）
INTERVAL_ORDER = ["15m", "1h", "2h", "4h", "1d", "1w"]
# 周期对应的分钟数（用于计算形态持续时长）
INTERVAL_MINUTES = {"15m": 15, "1h": 60, "2h": 120, "4h": 240,
                    "1d": 1440, "1w": 10080}


def interval_rank(interval: str) -> int:
    """返回周期在序列中的位置，-1 表示未知"""
    try:
        return INTERVAL_ORDER.index(interval)
    except ValueError:
        return -1


# ============================================================
#  七维信号强度评分
# ============================================================

class SignalScorer:
    """
    按七个维度加权打分，满分 100。

    权重（来自 docs/01 1.4）：
      形态完整度      25%
      突破幅度/ATR    20%
      成交量确认      15%
      多周期共振      15%
      形态时长合理性  10%
      大趋势一致性    10%
      距目标位远近     5%

    分档：
      >= 75  强信号，立即推送
      60~74  中等信号，推送并标注"待进一步确认"
      <  60  弱信号，仅记录不推送
    """

    DEFAULT_WEIGHTS = {
        "completeness": 0.10,          # 形态完整度（检测器二值门槛后的几何分）
        "symmetry": 0.15,              # 几何对称/结构质量（P2，借鉴 hunk77）
        "breakout_magnitude": 0.20,
        "volume_confirmation": 0.15,
        "multi_tf_resonance": 0.15,
        "duration_reasonableness": 0.10,
        "trend_consistency": 0.10,
        "proximity_to_target": 0.05,
    }

    def __init__(self, config: Optional[dict] = None):
        cfg = config or {}
        scoring = cfg.get("scoring", {})
        self.weights = dict(self.DEFAULT_WEIGHTS)
        self.weights.update(scoring.get("weights", {}))

        self.threshold_strong = scoring.get("threshold_strong", 75)
        self.threshold_medium = scoring.get("threshold_medium", 60)
        # ADX 阈值：低于此值视为"无显著趋势"（横盘），方向投票打折
        self.adx_threshold = scoring.get("adx_threshold", 20)

    # ---------- 各维度打分函数 ----------

    @staticmethod
    def score_completeness(p: Pattern) -> float:
        """
        形态完整度 —— 直接用识别阶段算出的 confidence（0~1）。

        confidence 已包含：峰谷价差吻合度、形态深度、跨度合理性等几何因素。
        """
        return max(0.0, min(1.0, p.confidence))

    @staticmethod
    def score_symmetry(p: Pattern) -> float:
        """
        几何对称/结构质量（P2，借鉴 hunk77 的 symmetry + structure quality）。

        直接用 validate_geometry() 算出的 geometry_score（0~1）：
          - 反转形态：价格对称(两峰/两肩接近) + 时间对称(左右臂等长) + 结构
          - 持续形态：边界收敛度/平行度 + 触点数量

        这是【分级】分：检测器内部是二值门槛（差>3% 直接丢弃），
        但"差 2.9%"和"差 0.1%"都算过——这里把"勉强过关"的压低，
        让强度分真正反映"画得标不标准"。
        """
        g = getattr(p, "geometry_score", 0.0)
        if g <= 0.0:
            return 0.5          # 未计算时给中性，避免误杀
        return max(0.0, min(1.0, g))

    @staticmethod
    def score_breakout_magnitude(p: Pattern) -> float:
        """
        突破幅度（以 ATR 归一化）。

        < 0.5×ATR  → 0 分（连最低确认门槛都没过）
        0.5×ATR    → 0.3
        1.0×ATR    → 0.6
        1.5×ATR    → 0.85
        >= 2.5×ATR → 1.0
        """
        m = p.breakout_magnitude_atr
        if m < 0.5:
            return 0.0
        if m >= 2.5:
            return 1.0
        # 分段线性插值
        points = [(0.5, 0.3), (1.0, 0.6), (1.5, 0.85), (2.5, 1.0)]
        for i in range(len(points) - 1):
            x0, y0 = points[i]
            x1, y1 = points[i + 1]
            if x0 <= m <= x1:
                return y0 + (y1 - y0) * (m - x0) / (x1 - x0)
        return 0.3

    @staticmethod
    def score_volume(p: Pattern) -> float:
        """
        量能确认。

        < 1.0×  → 0（缩量突破不可信）
        1.5×    → 0.6（达到最低门槛）
        2.5×    → 0.85
        >= 4.0× → 1.0
        """
        v = p.volume_ratio
        if v < 1.0:
            return 0.0
        if v >= 4.0:
            return 1.0
        points = [(1.0, 0.15), (1.5, 0.6), (2.5, 0.85), (4.0, 1.0)]
        for i in range(len(points) - 1):
            x0, y0 = points[i]
            x1, y1 = points[i + 1]
            if x0 <= v <= x1:
                return y0 + (y1 - y0) * (v - x0) / (x1 - x0)
        return 0.15

    @staticmethod
    def score_resonance(p: Pattern) -> float:
        """
        多周期共振。

        有共振 → 按数量给分；有矛盾 → 扣分（下限 0）。
        """
        base = 0.4                                   # 无共振无矛盾给中间分
        bonus = min(1.0, len(p.resonant_with) * 0.35)
        penalty = min(1.0, len(p.conflict_with) * 0.45)
        return max(0.0, min(1.0, base + bonus - penalty))

    @staticmethod
    def score_duration(p: Pattern) -> float:
        """
        形态时长合理性。

        太短（<10根）是噪声，太长（>200根）形态已松散。
        理想区间 20~120 根。
        """
        if not p.pivots:
            return 0.5
        span = max(q.index for q in p.pivots) - min(q.index for q in p.pivots)
        if span < 10:
            return max(0.0, span / 10 * 0.4)
        if span <= 120:
            # 20~120 根最理想
            if span < 20:
                return 0.4 + (span - 10) / 10 * 0.6
            return 1.0
        # 120 根之后逐渐衰减
        return max(0.2, 1.0 - (span - 120) / 200)

    def score_trend_consistency(self, p: Pattern, trend: Optional[str] = None,
                                momentum: Optional[dict] = None) -> float:
        """
        与大趋势的一致性（fantomluck 风格：趋势强度 + 动量同向投票）。

        关键改进（对比原版只看 EMA 方向）：
          - ADX 判定趋势是否"有效"：ADX < adx_threshold（默认20）视为横盘，
            此时方向投票打折，避免把无趋势区间里的噪声形态误判。
          - RSI / MACD 柱动能同向才给满分，否则略减——多源同意才信。
          - 反转形态（头肩/双顶双底）顺势回调反转成功率高，略加分；
            持续形态（三角/旗形/楔形）必须顺势。

        若趋势未知，给中性分 0.5。
        """
        if not trend or trend == "unknown":
            return 0.5

        adx = momentum.get("adx", 0.0) if momentum else 0.0
        rsi = momentum.get("rsi", 50.0) if momentum else 50.0
        macd_hist = momentum.get("macd_hist", 0.0) if momentum else 0.0

        reversal_types = {
            "head_shoulders_top", "head_shoulders_bottom",
            "double_top", "double_bottom",
        }
        is_reversal = p.pattern_type in reversal_types

        if p.direction == Direction.LONG:
            aligned = (trend == "up")
        else:
            aligned = (trend == "down")

        # 趋势不显著（横盘）：方向投票打折
        if adx < self.adx_threshold:
            # 横盘里反转形态（区间边界反转）相对合理，给中性偏上；
            # 持续形态在横盘里偏弱。
            return 0.6 if is_reversal else 0.4

        # 趋势显著：方向对齐给基础分，动量同向再加成
        base = 0.75 if aligned else 0.45
        if momentum:
            mom_aligned = (
                (p.direction == Direction.LONG and rsi > 50 and macd_hist >= 0)
                or (p.direction == Direction.SHORT and rsi < 50 and macd_hist <= 0)
            )
            if mom_aligned:
                base = min(1.0, base + 0.15)
            else:
                base = max(0.2, base - 0.10)
        if is_reversal and aligned:
            base = min(1.0, base + 0.05)
        return base

    @staticmethod
    def score_proximity(p: Pattern) -> float:
        """
        距目标位的远近 —— 剩余空间越大越好（没走完）。

        已走完全程（价格已到目标位）→ 0 分
        刚突破（离目标最远）        → 1 分
        """
        if p.entry_price <= 0 or p.take_profit_1 <= 0:
            return 0.5
        total = abs(p.take_profit_1 - p.entry_price)
        if total <= 0:
            return 0.5
        # 用风险距离归一化剩余空间
        risk = abs(p.entry_price - p.stop_loss)
        if risk <= 0:
            return 0.5
        # 剩余空间 / 风险 = R 倍数，1~3 之间最理想
        rr = p.risk_reward
        if rr >= 3:
            return 1.0
        if rr <= 0.5:
            return 0.0
        return min(1.0, (rr - 0.5) / 2.5)

    # ---------- 综合 ----------

    def score(self, p: Pattern, trend: Optional[str] = None,
              momentum: Optional[dict] = None) -> int:
        """计算综合强度分 0~100"""
        dims = {
            "completeness": self.score_completeness(p),
            "symmetry": self.score_symmetry(p),
            "breakout_magnitude": self.score_breakout_magnitude(p),
            "volume_confirmation": self.score_volume(p),
            "multi_tf_resonance": self.score_resonance(p),
            "duration_reasonableness": self.score_duration(p),
            "trend_consistency": self.score_trend_consistency(p, trend, momentum),
            "proximity_to_target": self.score_proximity(p),
        }

        total = 0.0
        for dim, val in dims.items():
            total += val * self.weights.get(dim, 0.0)

        # 单K线确认加成（TA-Lib 蜡烛图形态，最多 ±8 分）
        total += self._score_candle_confirmation(p)

        # 封顶 100
        p.strength_score = int(round(min(total, 1.0) * 100))
        return p.strength_score

    @staticmethod
    def _score_candle_confirmation(p: Pattern) -> float:
        """
        突破K线的蜡烛图形态与突破方向是否一致。

        方向一致（如做多 + 看涨吞没）→ +0.08（8分）
        方向冲突（如做多 + 流星线）   → -0.06
        无确认                          → 0
        """
        candles = getattr(p, "candle_confirmations", [])
        if not candles:
            return 0.0
        bullish = any("看涨" in c for c in candles)
        bearish = any("看跌" in c for c in candles)
        if p.direction == Direction.LONG:
            if bullish:
                return 0.08
            if bearish:
                return -0.06
        else:
            if bearish:
                return 0.08
            if bullish:
                return -0.06
        return 0.0

    def grade(self, score: int) -> str:
        """分档"""
        if score >= self.threshold_strong:
            return "强"
        if score >= self.threshold_medium:
            return "中"
        return "弱"


# ============================================================
#  多周期交叉确认
# ============================================================

class CrossTimeframeConfirm:
    """
    四周期交叉确认。

    规则表（docs/02 2.2.4）：
      共振加分：
        15m ← 1h 同向      +15
        15m ← 4h 同向      +10
        1h  ← 4h 同向      +15
        1h  ← 1d 同向      +10
        4h  ← 1d 同向      +15
        任意 ← 更小周期领先  +8
      矛盾扣分：
        1h 与 4h 反向      -20
        4h 与 1d 反向      -25
        三周期两两矛盾      -40
    """

    DEFAULT_RESONANCE = {
        ("15m", "1h"): 15,
        ("15m", "4h"): 10,
        ("1h", "4h"): 15,
        ("1h", "1d"): 10,
        ("4h", "1d"): 15,
    }
    LEAD_BONUS = 8

    DEFAULT_CONFLICT = {
        ("1h", "4h"): -20,
        ("4h", "1d"): -25,
    }
    TRIPLE_CONFLICT_PENALTY = -40

    def __init__(self, config: Optional[dict] = None):
        cfg = config or {}
        res = cfg.get("scoring", {}).get("resonance_bonus", {})
        con = cfg.get("scoring", {}).get("conflict_penalty", {})

        self.resonance = dict(self.DEFAULT_RESONANCE)
        self.conflict = dict(self.DEFAULT_CONFLICT)
        self.lead_bonus = self.LEAD_BONUS

        # 从配置覆盖（配置里用的是字符串键，如 "1h_from_15m"）
        for key, val in (res or {}).items():
            if "_from_" in key:
                larger, smaller = key.split("_from_")
                self.resonance[(smaller, larger)] = val
            elif key == "smaller_lead":
                self.lead_bonus = val

        for key, val in (con or {}).items():
            if "_vs_" in key:
                a, b = key.split("_vs_")
                self.conflict[(a, b)] = val

        self.scorer = SignalScorer(cfg)

    def confirm(self, signals: List[Pattern],
                trends: Optional[Dict[str, str]] = None,
                momentum: Optional[Dict] = None) -> List[Pattern]:
        """
        对所有信号做交叉确认 + 打分。

        参数：
          signals —— 所有 (symbol, interval) 上检出的已确认信号
          trends  —— {(symbol, interval): "up"/"down"/"sideways"}，可选

        返回：按强度分降序排列的信号列表
        """
        if not signals:
            return []

        trends = trends or {}

        # 按 symbol 分组
        by_symbol: Dict[str, List[Pattern]] = {}
        for s in signals:
            by_symbol.setdefault(s.symbol, []).append(s)

        for symbol, group in by_symbol.items():
            # 按周期建立索引
            by_interval: Dict[str, List[Pattern]] = {}
            for s in group:
                by_interval.setdefault(s.interval, []).append(s)

            for s in group:
                self._apply_resonance(s, by_interval)

                trend = trends.get((s.symbol, s.interval))
                mom = momentum.get((s.symbol, s.interval)) if momentum else None
                # P2：先算几何质量分（对称+结构），供 symmetry 维度使用
                try:
                    s.geometry_score, s.geometry_reason = validate_geometry(s)
                except Exception:
                    s.geometry_score, s.geometry_reason = 0.5, "几何评估异常"
                self.scorer.score(s, trend, mom)

        # 过滤掉过弱的和矛盾的
        result = []
        for s in signals:
            # 三周期两两矛盾直接丢弃
            if len(s.conflict_with) >= 2:
                logger.info(f"{s.symbol} {s.interval} {s.pattern_type} "
                            f"多周期矛盾过多，丢弃")
                continue
            result.append(s)

        result.sort(key=lambda x: (-x.strength_score, -x.confidence))
        return result

    def _apply_resonance(self, s: Pattern,
                         by_interval: Dict[str, List[Pattern]]):
        """计算单个信号的共振/矛盾关系"""
        rank = interval_rank(s.interval)
        if rank < 0:
            return

        s.resonant_with = []
        s.conflict_with = []

        for other_iv, others in by_interval.items():
            if other_iv == s.interval:
                continue
            other_rank = interval_rank(other_iv)
            if other_rank < 0:
                continue

            for o in others:
                same_dir = (o.direction == s.direction)

                if same_dir:
                    if other_rank > rank:
                        # 更大周期同向 → 共振加分
                        bonus = self.resonance.get((s.interval, other_iv), 0)
                        if bonus:
                            s.resonant_with.append(other_iv)
                            s.strength_score += bonus
                    else:
                        # 更小周期同向 → 领先确认
                        s.resonant_with.append(f"{other_iv}(领先)")
                        s.strength_score += self.lead_bonus
                else:
                    # 方向相反
                    pair = tuple(sorted(
                        [s.interval, other_iv], key=interval_rank))
                    penalty = self.conflict.get(pair, 0)
                    if penalty:
                        s.conflict_with.append(other_iv)
                        s.strength_score += penalty

        # 去重（同一周期可能多次出现）
        s.resonant_with = sorted(set(s.resonant_with))
        s.conflict_with = sorted(set(s.conflict_with))
