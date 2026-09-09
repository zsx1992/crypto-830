# -*- coding: utf-8 -*-
"""
双顶 / 双底 检测器

可检测性: A 级（规则明确，可直接量化）

双顶 (Double Top / M顶)
  结构: (H, L, H) —— 两个高度相近的峰，中间夹一个谷
  含义: 看跌。两次冲击同一价位失败，说明该压力位有效
  失效: 价格重新站上两峰之上（则变成更高的高点，趋势可能延续）

双底 (Double Bottom / W底)
  结构: (L, H, L)
  含义: 看涨。两次探底成功，支撑位确认
  失效: 价格跌破两底之下

判定阈值（默认）:
  两峰价差容差      3%     —— 太松会把普通震荡当成双顶
  中间谷深度        5%     —— 太浅说明"两个峰"其实只是一个平台
  两峰间距        10~120 根 —— 太近是噪声，太远则形态已失效
  突破确认         连续 2 根收盘
  突破幅度         ≥ 0.5 × ATR
  量能确认         ≥ 1.5 × 20根均量
"""

import os
import sys
from typing import List

_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from zigzag import Pivot, PivotType
from market_data import Kline
from patterns.base import (
    BaseDetector, Pattern, Direction, PatternStatus, Line,
    horizontal_line, calc_trade_levels, find_breakout_index,
    check_breakout, calc_volume_ratio, prior_move,
)


class DoubleTopBottomDetector(BaseDetector):
    """同时检测双顶和双底（结构对称，逻辑共用）"""

    name = "double_top_bottom"

    DEFAULT_PARAMS = {
        "peak_tolerance": 0.03,        # [兼容] 两峰/两谷价差容差 (基准值)
        # 【2026-09-08 C方案: 边界灵活】peak_tolerance 随形态跨度缩放:
        #   span <= tol_span_lo(50)   → peak_tolerance_min (3%)  严值
        #   span >= tol_span_hi(200)  → peak_tolerance_max (5%)  宽值
        #   中间线性插值。动机: 大 W 底两谷价差天然大 (PIEVERSE 真实
        #   谷差 7.2% = higher-low 结构非噪声), 固定 3% 把它们全拒;
        #   但近距小形态 4~5% 价差是 9-07 误报重灾区 (A+B 收严成果),
        #   不能整体放宽。故按跨度分层: 短形态严、长形态宽。
        #   None = 未启用缩放: 全程用 peak_tolerance (旧接口/旧测试行为)。
        "peak_tolerance_min": None,
        "peak_tolerance_max": None,
        "tol_span_lo": 50,
        "tol_span_hi": 200,
        "min_depth": 0.05,             # 中间谷/峰的最小深度
        "min_span": 10,                # 两峰最小间距（根）
        "max_span": 120,               # 两峰最大间距（根）
        # 方向性硬闸 (2026-09-09, 用户金标准 CBRS 1h / RAY 1d / BNB):
        #   peak_overshoot_max    — 双顶右峰最多比左峰高 2% (higher-high=趋势延续)
        #   trough_undershoot_max — 双底右谷最多比左谷低 3% (lower-low=下跌延续)
        # 注意: 这是"谁高谁低"的方向约束, 与 peak_tolerance(|差|≤tol) 正交——
        # CBRS 右顶高 2.54% 在 3% 容差内通过价差检查, 但方向上是 higher-high。
        # 两阈值不对称的依据 (真实金标准):
        #   RAY 1d 双底右谷低 1.95% 是线上已推的真信号 (突破量48x) → 必须放行;
        #   BNB 右谷低 5.7% 用户判"非教科书W底" → 必须拒绝。
        #   双底右侧最后一跌挖坑(1~3%)常见, 双顶右侧更高(>2%)罕见且危险。
        "peak_overshoot_max": 0.02,
        "trough_undershoot_max": 0.03,
        "breakout_candles": 2,         # 连续确认根数
        "breakout_atr_ratio": 0.5,     # 突破幅度 / ATR
        "volume_ratio_min": 1.5,       # 量能确认
        # 突破等待窗口。None = 从右谷/右峰搜索到数据末端。
        # 【2026-09-08 朱哥金标准反馈】旧值 30 根只够 30 小时@1h，大 W 底
        # (右谷后 ~100-200 根才破颈线, 如 ENAUSDT 右谷 08-31 破颈 09-08)
        # 永远确认不了 → CANDIDATE 静默丢弃。放宽后由 freshness +
        # require_intact_breakout + pullback_bars 兜底过滤陈年/假突破。
        "max_lookahead": None,
        "min_height_atr": 1.0,         # 形态高度至少 1×ATR，否则无交易价值
        "pullback_bars": 0,            # 突破后回踩确认窗口（0=关闭，2026-09-01 新增）
        # 反转形态结构性闸门：形态前必须有显著趋势（2026-09-03 新增）
        # 实测：158 张人工标注的漏网误报里 AAVE 双顶 / ARB 双底 全是
        # 「下跌中继里的小 H-L-H」，形态内部结构满足但前面没趋势。
        # 头肩底早就有这道闸门（require_prior_trend=True），双底/双顶/头肩顶补齐。
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
        if len(pivots) < 3 or not klines or atr_value <= 0:
            return results

        p = self.params

        # 扫描所有连续三点窗口
        for i in range(len(pivots) - 2):
            a, b, c = pivots[i], pivots[i + 1], pivots[i + 2]

            # --- 双顶: 高-低-高 ---
            if (a.type == PivotType.HIGH and b.type == PivotType.LOW
                    and c.type == PivotType.HIGH):
                pat = self._check_double_top(a, b, c, klines, atr_value,
                                             symbol, interval)
                if pat:
                    results.append(pat)

            # --- 双底: 低-高-低 ---
            elif (a.type == PivotType.LOW and b.type == PivotType.HIGH
                  and c.type == PivotType.LOW):
                pat = self._check_double_bottom(a, b, c, klines, atr_value,
                                                symbol, interval)
                if pat:
                    results.append(pat)

        return results

    # ---------- 双顶 ----------

    def _tolerance_for_span(self, span_bars: int) -> float:
        """两峰/两谷价差容差随形态跨度缩放 (2026-09-08 C方案: 边界灵活)。

        近距小形态 (span 小) 容差严 —— 保留 9-07 A+B 防误报成果:
            (5% 容差时 4~5% 价差的速成双底灌进管线全误报, 收严 3%)
        大跨度形态 (span 大) 容差宽 —— 大 W 两谷天然有差价:
            (PIEVERSE 0.918/0.990 差 7.2% 是 higher-low 结构而非噪声)
        50~200 根之间线性过渡。
        兼容: 调用方只传 peak_tolerance (旧接口) 时, 它作为 tol_min 兜底。
        """
        p = self.params
        tol_min = p.get("peak_tolerance_min")
        if tol_min is None:
            tol_min = p.get("peak_tolerance", 0.03)
        tol_max = p.get("peak_tolerance_max")
        if tol_max is None:
            tol_max = tol_min
        span_lo = p.get("tol_span_lo", 50)
        span_hi = p.get("tol_span_hi", 200)
        if span_hi <= span_lo:
            return tol_max
        if span_bars <= span_lo:
            return tol_min
        if span_bars >= span_hi:
            return tol_max
        frac = (span_bars - span_lo) / (span_hi - span_lo)
        return tol_min + (tol_max - tol_min) * frac

    def _check_double_top(self, h1: Pivot, l1: Pivot, h2: Pivot,
                          klines: List[Kline], atr_value: float,
                          symbol: str, interval: str) -> "Pattern | None":
        p = self.params

        # ① 两峰高度接近（用 max 做分母，避免左右顺序导致容差偏移）
        peak_diff = abs(h1.price - h2.price) / max(h1.price, h2.price)
        tol = self._tolerance_for_span(h2.index - h1.index)
        if peak_diff > tol:
            return None

        # ①a 方向性硬闸：右峰不得显著高于左峰 (2026-09-09 用户金标准 CBRS 1h)
        # 双顶的本质是"两次冲击同一压力位失败"。若右峰明显更高 (higher-high),
        # 说明多方仍在推动 → 是趋势延续不是反转双顶。
        # CBRSUSDT 1h 实测: 左顶216.65 / 右顶222.16 (右高2.54%) 被判双顶推送,
        # 用户评"明显不是双顶, 什么也不是"——右峰冲高后单根长上影大阴线回落,
        # 实际是趋势末端的插针而非 M 顶。
        # 教科书允许右峰略高于左峰 (常见 ≤1~2%), 超限即拒。
        peak_overshoot = p.get("peak_overshoot_max", 0.02)
        if h2.price > h1.price * (1 + peak_overshoot):
            return None

        # ①b 前置趋势：双顶是反转形态，前面必须有一段显著上涨。
        # 否则下跌中继里"两个相邻小反弹"会过 ①③⑤ 但根本不是反转。
        if p["require_prior_trend"]:
            mv = prior_move(klines, h1.index, p["prior_trend_bars"])
            if mv is None or mv < p["prior_trend_min_move"]:
                return None

        # ② 中间谷足够深
        depth = (min(h1.price, h2.price) - l1.price) / min(h1.price, h2.price)
        if depth < p["min_depth"]:
            return None

        # ③ 两峰间距合理
        span = h2.index - h1.index
        if not (p["min_span"] <= span <= p["max_span"]):
            return None

        # ④ 形态高度要有交易价值
        height = min(h1.price, h2.price) - l1.price
        if height < p["min_height_atr"] * atr_value:
            return None

        # ⑤ 右峰之后不能已经创新高（否则形态破坏）
        highest_after = max((k.high for k in klines[h2.index:]), default=h2.price)
        if highest_after > max(h1.price, h2.price) * 1.005:
            return None

        neck_price = l1.price
        pattern = Pattern(
            symbol=symbol,
            interval=interval,
            pattern_type="double_top",
            direction=Direction.SHORT,
            status=PatternStatus.CANDIDATE,
            pivots=[h1, l1, h2],
            neckline=horizontal_line(neck_price, l1.index,
                                     span=max(10, span), ptype=PivotType.LOW),
            height=height,
            confidence=self._confidence(peak_diff, depth, span, tol),
        )

        # ⑥ 突破确认
        def boundary_fn(idx):
            return neck_price

        idx = find_breakout_index(klines, h2.index + 1, boundary_fn,
                                  Direction.SHORT, p["max_lookahead"])
        if idx < 0:
            return pattern          # 仍是候选

        ok, confirmed, magnitude = check_breakout(
            klines, idx, neck_price, Direction.SHORT, atr_value,
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

        # ⑦ 突破后回踩确认（拦截"影线插针"式假突破）
        #
        # 实测问题（2026-09-01）：XAG 4h 双顶靠单根长上影 close 跌破
        # 颈线 + 后续 1 根也收在颈线下就判确认，朱哥人眼判"不像"。
        # check_breakout 用 close，挡住了影线插针 close 的情况，但挡不住
        # "上影长 + 实体略破" 这类边缘突破。回踩确认要求：突破后 N 根
        # 内若有 K 线收盘价回到颈线错误一侧（SHORT: close > neck；
        # LONG: close < neck），视为假突破，恢复 CANDIDATE 不推送。
        if p.get("pullback_bars", 0) > 0:
            pb_end = min(idx + 1 + p["pullback_bars"], len(klines))
            pullback_failed = False
            for j in range(idx + 1, pb_end):
                if (Direction.SHORT == pattern.direction
                        and klines[j].close > neck_price):
                    pullback_failed = True
                    break
                if (Direction.LONG == pattern.direction
                        and klines[j].close < neck_price):
                    pullback_failed = True
                    break
            if pullback_failed:
                pattern.status = PatternStatus.CANDIDATE
                return pattern

        pattern.status = PatternStatus.CONFIRMED
        calc_trade_levels(pattern, klines, atr_value)
        return pattern

    # ---------- 双底 ----------

    def _check_double_bottom(self, l1: Pivot, h1: Pivot, l2: Pivot,
                             klines: List[Kline], atr_value: float,
                             symbol: str, interval: str) -> "Pattern | None":
        p = self.params

        # ① 两谷高度接近（用 max 做分母，避免左右顺序导致容差偏移）
        trough_diff = abs(l1.price - l2.price) / max(l1.price, l2.price)
        tol = self._tolerance_for_span(l2.index - l1.index)
        if trough_diff > tol:
            return None

        # ①a 方向性硬闸：右谷不得显著低于左谷 (2026-09-09, 与双顶 peak_overshoot
        # 镜像)。双底本质是"两次探底同一支撑成功"。若右谷明显更低 (lower-low),
        # 说明空方仍在打压 → 是下跌延续不是反转 W 底。
        # (对称案例: BNB 谷1 569 / 谷2 537 低 5.7%, 用户判非教科书 W 底)
        trough_undershoot = p.get("trough_undershoot_max", 0.03)
        if l2.price < l1.price * (1 - trough_undershoot):
            return None

        # ①b 前置趋势：双底是反转形态，前面必须有一段显著下跌。
        if p["require_prior_trend"]:
            mv = prior_move(klines, l1.index, p["prior_trend_bars"])
            if mv is None or mv > -p["prior_trend_min_move"]:
                return None

        # ② 中间峰足够高
        peak_height = (h1.price - max(l1.price, l2.price)) / max(l1.price, l2.price)
        if peak_height < p["min_depth"]:
            return None

        # ③ 两谷间距合理
        span = l2.index - l1.index
        if not (p["min_span"] <= span <= p["max_span"]):
            return None

        # ④ 形态高度有交易价值
        height = h1.price - max(l1.price, l2.price)
        if height < p["min_height_atr"] * atr_value:
            return None

        # ⑤ 右谷之后不能已经创新低
        lowest_after = min((k.low for k in klines[l2.index:]), default=l2.price)
        if lowest_after < min(l1.price, l2.price) * 0.995:
            return None

        neck_price = h1.price
        pattern = Pattern(
            symbol=symbol,
            interval=interval,
            pattern_type="double_bottom",
            direction=Direction.LONG,
            status=PatternStatus.CANDIDATE,
            pivots=[l1, h1, l2],
            neckline=horizontal_line(neck_price, h1.index,
                                     span=max(10, span), ptype=PivotType.HIGH),
            height=height,
            confidence=self._confidence(trough_diff, peak_height, span, tol),
        )

        def boundary_fn(idx):
            return neck_price

        idx = find_breakout_index(klines, l2.index + 1, boundary_fn,
                                  Direction.LONG, p["max_lookahead"])
        if idx < 0:
            return pattern

        ok, confirmed, magnitude = check_breakout(
            klines, idx, neck_price, Direction.LONG, atr_value,
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

        # ⑦ 突破后回踩确认（逻辑同 _check_double_top，详见那里注释）
        if p.get("pullback_bars", 0) > 0:
            pb_end = min(idx + 1 + p["pullback_bars"], len(klines))
            pullback_failed = False
            for j in range(idx + 1, pb_end):
                if (Direction.LONG == pattern.direction
                        and klines[j].close < neck_price):
                    pullback_failed = True
                    break
                if (Direction.SHORT == pattern.direction
                        and klines[j].close > neck_price):
                    pullback_failed = True
                    break
            if pullback_failed:
                pattern.status = PatternStatus.CANDIDATE
                return pattern

        pattern.status = PatternStatus.CONFIRMED
        calc_trade_levels(pattern, klines, atr_value)
        return pattern

    # ---------- 置信度 ----------

    @staticmethod
    def _confidence(price_diff: float, depth: float, span: int,
                    tol: float = 0.03) -> float:
        """
        几何完整度评分 0~1（只评价"长得像不像"，不含量能/共振）

        三项各占一部分：
          价差越小越像   —— 0% 得满分，超过容差(tol)得 0
          深度越深越像   —— 15% 以上得满分，5% 得 0.5
          间距适中       —— 20~60 根最理想
        """
        # 价差项（0.4 权重）：按实际容差归一（大跨度形态容差宽）
        diff_score = max(0.0, 1.0 - price_diff / max(tol, 1e-9))
        # 深度项（0.35 权重）
        depth_score = min(1.0, depth / 0.15)
        # 间距项（0.25 权重）：20~60 根最佳
        if 20 <= span <= 60:
            span_score = 1.0
        elif span < 20:
            span_score = span / 20
        else:
            span_score = max(0.3, 1.0 - (span - 60) / 100)

        return round(0.4 * diff_score + 0.35 * depth_score + 0.25 * span_score, 3)
