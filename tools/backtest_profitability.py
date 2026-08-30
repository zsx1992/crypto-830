#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交易盈利回测 —— 模拟"突破信号发出后价格先到止盈还是先到止损"

与检测一致率（tools/backtest_samples.py）互补：
  一致率回答"系统看形态和你看得像不像"
  本脚本回答"系统报的形态按它的价位操作，能不能赚钱"

方法（walk-forward，防前视偏差）：
  1. 对每个标的取 1000 根 4h K线
  2. 从第 150 根开始，每 50 根设一个"检查点"
  3. 在检查点只喂【检查点之前】的数据做识别（绝不看未来）
  4. 若产出已确认信号且突破在 8 根内（新鲜），记录 入场/止损/止盈
  5. 向前模拟 120 根：用高低价判断 先触止盈1 还是 先触止损
  6. 统计：胜率、平均盈亏（R倍数）、期望值、按形态/方向分组

说明：
  - 单尺度 ZigZag(5,5)（回测跑多尺度太慢，精度损失可接受）
  - 止盈1 计为全仓平仓（保守），止盈2 只统计命中率
"""

import os
import sys
import time
import logging
import argparse
from collections import Counter

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, _SRC)

from market_data import MarketDataClient
from indicators import calc_indicators
from zigzag import find_pivots
from detector import PatternEngine
from patterns.base import Pattern, PatternStatus, Direction
from crosstf import SignalScorer

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ---- 回测参数（可用环境变量覆盖：BT_TOP_N / BT_BARS / BT_STEP / BT_LOOKAHEAD）----
INTERVAL = os.environ.get("BT_INTERVAL", "4h")
BARS = int(os.environ.get("BT_BARS", 1000))
MIN_BARS = 150
WINDOW_STEP = int(os.environ.get("BT_STEP", 50))
LOOKAHEAD = int(os.environ.get("BT_LOOKAHEAD", 120))   # 入场后最多看多少根（4h×120=20天）
FRESH_MAX = 8            # 突破距今<=8根才算"新鲜信号"
ZIGZAG = 5               # 单尺度

TOP_N = int(os.environ.get("BT_TOP_N", 20))   # 回测的标的数
TIME_BUDGET_SEC = int(os.environ.get("BT_BUDGET", 400))   # 时间预算，防止跑太久

# 实盘过滤条件（与 scanner.run 保持一致，保证回测=实盘）
MIN_STRENGTH = 60
MIN_RR = 1.5


def simulate(p: Pattern, klines, entry_bar: int,
             tp_scale: float = 1.0):
    """
    从【入场检查点】向后模拟（严格防前视：入场=检查点收盘价）。

    tp_scale: 止盈缩放系数。1.0 = 形态高度（默认），0.5 = 半程止盈。
    """
    direction = p.direction
    entry = p.entry_price
    sl = p.stop_loss
    risk = abs(entry - sl)
    if risk <= 0:
        return None

    # 按缩放系数计算调整后的止盈位
    if direction == Direction.LONG:
        tp1 = entry + (p.take_profit_1 - entry) * tp_scale
        tp2 = entry + (p.take_profit_2 - entry) * tp_scale
    else:
        tp1 = entry - (entry - p.take_profit_1) * tp_scale
        tp2 = entry - (entry - p.take_profit_2) * tp_scale

    start = entry_bar
    end = min(start + LOOKAHEAD, len(klines))

    for i in range(start, end):
        k = klines[i]
        if direction == Direction.LONG:
            if k.low <= sl:
                return {"result": "SL", "r": -1.0}
            if k.high >= tp1:
                # 已触止盈1；再继续看是否触止盈2
                for j in range(i, end):
                    if klines[j].low <= sl:
                        return {"result": "TP1_then_SL", "r": p.risk_reward}
                    if klines[j].high >= tp2:
                        return {"result": "TP2", "r": p.risk_reward}
                return {"result": "TP1", "r": p.risk_reward}
        else:
            if k.high >= sl:
                return {"result": "SL", "r": -1.0}
            if k.low <= tp1:
                for j in range(i, end):
                    if klines[j].high >= sl:
                        return {"result": "TP1_then_SL", "r": p.risk_reward}
                    if klines[j].low <= tp2:
                        return {"result": "TP2", "r": p.risk_reward}
                return {"result": "TP1", "r": p.risk_reward}

    # 超时：按最后收盘价结算（用R衡量）
    last = klines[min(end, len(klines)) - 1].close
    if direction == Direction.LONG:
        r = (last - entry) / risk
    else:
        r = (entry - last) / risk
    return {"result": "TIMEOUT", "r": r}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-trend-filter", action="store_true",
                    help="关闭趋势同向过滤（对比用）")
    ap.add_argument("--tp-scale", type=float, default=1.0,
                    help="止盈缩放系数 (默认1.0=形态高度, 可试0.5)")
    args = ap.parse_args()

    client = MarketDataClient()
    engine = PatternEngine()
    scorer = SignalScorer(engine.config)
    # 单尺度模式：临时禁用多尺度
    engine.multiscale_scales = [ZIGZAG]

    print(f"\n参数: 标的={TOP_N} 周期={INTERVAL} "
          f"趋势过滤={'关' if args.no_trend_filter else '开'} "
          f"止盈缩放={args.tp_scale}x")
    print("拉取标的与历史数据...")
    syms = client.get_top_symbols(top_n=TOP_N)
    print(f"标的: {len(syms)} 个, 周期: {INTERVAL}, "
          f"每标的 {BARS} 根历史\n")

    t0 = time.time()
    all_trades = []
    per_pattern = Counter()
    per_dir = Counter()

    for si, sym in enumerate(syms):
        if time.time() - t0 > TIME_BUDGET_SEC:
            print(f"  时间预算耗尽，提前结束（已完成 {si} 个标的）")
            break
        try:
            klines, _ = client.get_klines(sym, INTERVAL, BARS)
        except Exception as e:
            print(f"  {sym}: 数据获取失败 {e}")
            continue
        if len(klines) < MIN_BARS + 50:
            continue

        n_signal = 0
        for t in range(MIN_BARS, min(len(klines), BARS), WINDOW_STEP):
            window = klines[:t]
            ind = calc_indicators(window)
            if ind.atr_current <= 0:
                continue
            pivots = find_pivots(window, ZIGZAG, ZIGZAG)
            if len(pivots) < 3:
                continue

            sigs = engine.scan(window, pivots, ind.atr_current, sym, INTERVAL)
            for p in sigs:
                if p.status != PatternStatus.CONFIRMED:
                    continue
                if p.breakout_age > FRESH_MAX:
                    continue
                # 实盘过滤：评分 + 风险回报比（与 scanner.run 一致）
                scorer.score(p, ind.trend)
                if p.strength_score < MIN_STRENGTH:
                    continue
                if p.risk_reward < MIN_RR:
                    continue
                # 趋势同向过滤（与实盘 require_trend_alignment 一致）
                if not args.no_trend_filter:
                    aligned = (
                        (p.direction == Direction.LONG and ind.trend == "up")
                        or (p.direction == Direction.SHORT
                            and ind.trend == "down")
                    )
                    if not aligned:
                        continue
                trade = simulate(p, klines, t, tp_scale=args.tp_scale)
                if trade is None:
                    continue
                n_signal += 1
                all_trades.append((p, trade))
                per_pattern[p.pattern_type] += 1
                per_dir[p.direction.value] += 1

        print(f"  {sym:<12} 信号数: {n_signal}  "
              f"({time.time() - t0:.0f}s)")

    # ---- 汇总 ----
    total = len(all_trades)
    print("\n" + "=" * 62)
    print("  交易盈利回测结果")
    print("=" * 62)
    print(f"  信号总数      : {total}")

    if total == 0:
        print("\n  无有效信号，无法统计")
        return

    wins = [tr for _, tr in all_trades
            if tr["result"] in ("TP1", "TP1_then_SL", "TP2")]
    losses = [tr for _, tr in all_trades if tr["result"] == "SL"]
    timeouts = [tr for _, tr in all_trades if tr["result"] == "TIMEOUT"]
    tp2_hits = [tr for _, tr in all_trades if tr["result"] == "TP2"]

    win_rate = len(wins) / total * 100
    sl_rate = len(losses) / total * 100

    # 平均R（止盈按 TP1 计，止损按 -1R，超时按实际）
    avg_r = sum(tr["r"] for _, tr in all_trades) / total
    # 胜者平均R
    avg_win_r = (sum(tr["r"] for _, tr in all_trades
                     if tr["result"] in ("TP1", "TP1_then_SL", "TP2"))
                 / len(wins)) if wins else 0
    avg_loss_r = -1.0

    print(f"  止盈1命中(胜) : {len(wins)}  ({win_rate:.1f}%)")
    print(f"  其中触止盈2   : {len(tp2_hits)}  ({len(tp2_hits) / total * 100:.1f}%)")
    print(f"  止损触发(负)  : {len(losses)}  ({sl_rate:.1f}%)")
    print(f"  超时未达目标  : {len(timeouts)}  ({len(timeouts) / total * 100:.1f}%)")
    print()
    print(f"  平均盈亏      : {avg_r:+.2f} R/笔")
    print(f"  胜方平均      : +{avg_win_r:.2f} R")
    print(f"  负方平均      : {avg_loss_r:.2f} R")
    print(f"  期望值        : {avg_r:+.2f} R  "
          f"({'正期望✓' if avg_r > 0 else '负期望✗'})")
    print()

    # 按形态分组
    print("  按形态分组（样本>=3）:")
    grouped = {}
    for p, tr in all_trades:
        grouped.setdefault(p.pattern_type, []).append(tr["r"])
    for pt, rs in sorted(grouped.items(), key=lambda x: -len(x[1])):
        if len(rs) < 3:
            continue
        wr = sum(1 for r in rs if r > 0) / len(rs) * 100
        print(f"    {pt:<26} n={len(rs):<4} 胜率={wr:.0f}%  "
              f"均值={sum(rs) / len(rs):+.2f}R")

    print()
    print("  按方向:")
    for d in ("LONG", "SHORT"):
        rs = [tr["r"] for p, tr in all_trades
              if p.direction.value == d]
        if not rs:
            continue
        wr = sum(1 for r in rs if r > 0) / len(rs) * 100
        print(f"    {d:<6} n={len(rs):<4} 胜率={wr:.0f}%  "
              f"均值={sum(rs) / len(rs):+.2f}R")
    print()


if __name__ == "__main__":
    main()
