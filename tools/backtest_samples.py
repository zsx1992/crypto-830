#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
金标准样本回放验证

用你手绘标注的样本集验证检测器是否真的能识别出来。

原理（防前视偏差）：
  每个样本的文件名里都有截图时刻（如 [10 20] 2023年7月27日 → 2023-07-27T10:20:00）
  回测时【只喂该时刻之前的数据】，绝不能让检测器看到标注之后的行情。
  否则就是"看着答案做题"，指标会虚高。

用法:
  python tools/backtest_samples.py --limit 40
  python tools/backtest_samples.py --limit 97 --tz-offset 8

输出:
  命中率按形态分类，以及漏判样本明细（用于针对性调参）
"""

import os
import sys
import json
import argparse
import logging
from datetime import datetime, timedelta
from collections import Counter

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, _SRC)

from market_data import MarketDataClient
from zigzag import find_pivots
from indicators import calc_indicators
from detector import PatternEngine
from patterns.base import PatternStatus

logging.basicConfig(level=logging.WARNING,
                    format="[%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# 样本标签 -> 检测器输出的形态类型
# 一个样本标签可能对应多个检测器输出（如"头肩"未指明顶/底）
LABEL_TO_PATTERNS = {
    "double_top": {"double_top"},
    "double_bottom": {"double_bottom"},
    "head_shoulders_top": {"head_shoulders_top"},
    "head_shoulders_bottom": {"head_shoulders_bottom"},
    "head_shoulders_unspecified": {"head_shoulders_top",
                                   "head_shoulders_bottom"},
    "ascending_triangle": {"ascending_triangle"},
    "descending_triangle": {"descending_triangle"},
    "symmetrical_triangle": {"symmetrical_triangle"},
    "triangle_unspecified": {"ascending_triangle", "descending_triangle",
                             "symmetrical_triangle"},
    "flag": {"flag"},
    "rising_wedge": {"rising_wedge"},
    "falling_wedge": {"falling_wedge"},
    "wedge_unspecified": {"rising_wedge", "falling_wedge"},
    # 以下为 C 级，检测器未实现，标注为"未覆盖"
    "rounding_bottom": set(),
    "rounding_top": set(),
    "v_reversal": set(),
    "cup_and_handle": set(),
    "channel": set(),
}

# 各周期的 K 线数量
KLINE_COUNTS = {"15m": 480, "1h": 240, "2h": 180, "4h": 120, "1d": 120}
ZIGZAG_PARAMS = {"15m": (5, 5), "1h": (5, 5), "2h": (4, 4),
                 "4h": (3, 3), "1d": (3, 3)}

# 我们实现了哪些形态（用于统计有效覆盖率）
IMPLEMENTED = {"double_top", "double_bottom", "head_shoulders_top",
               "head_shoulders_bottom", "ascending_triangle",
               "descending_triangle", "symmetrical_triangle",
               "flag", "rising_wedge", "falling_wedge"}


def load_gold_samples(path: str):
    """读取完整金标准样本（形态+时间+周期+标的齐全）"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    gold = []
    for s in data["samples"]:
        if not s.get("patterns"):
            continue
        if not s.get("detected_at"):
            continue
        if not s.get("timeframe") or not s.get("symbol"):
            continue
        gold.append(s)
    return gold


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", default="samples/labels.json")
    ap.add_argument("--limit", type=int, default=40, help="最多测多少个样本")
    ap.add_argument("--tz-offset", type=int, default=8,
                    help="样本时间戳的时区偏移(小时)，默认+8")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    gold = load_gold_samples(args.samples)
    print(f"\n金标准样本总数: {len(gold)}")

    # 只测我们已实现检测器覆盖的形态
    testable = [s for s in gold
                if any(p in IMPLEMENTED for p in s["patterns"])]
    skipped = [s for s in gold
               if not any(p in IMPLEMENTED for p in s["patterns"])]

    print(f"其中检测器已覆盖形态: {len(testable)}")
    print(f"未覆盖(C级/通道等):   {len(skipped)}")

    if args.limit:
        testable = testable[:args.limit]
        print(f"本次测试: {len(testable)} 个")

    client = MarketDataClient()
    engine = PatternEngine()

    hits = []            # 严格命中：形态末端贴近截图时刻
    loose_hits = []      # 宽松命中：窗口内任何位置检出期望形态
    recent_hits = []
    confirmed_hits = []
    misses = []
    fetched_fail = 0
    by_type = Counter()
    by_type_total = Counter()

    print("\n开始回放...\n")

    for i, s in enumerate(testable, 1):
        symbol = s["symbol"] + "USDT"
        interval = s["timeframe"]
        label = s["patterns"][0]
        expected = LABEL_TO_PATTERNS.get(label, set())

        # 样本时间 -> 毫秒时间戳（考虑时区）
        try:
            dt = datetime.strptime(s["detected_at"], "%Y-%m-%dT%H:%M:%S")
            dt = dt - timedelta(hours=args.tz_offset)   # 转 UTC
            end_ms = int(dt.timestamp() * 1000)
        except Exception as e:
            logger.warning(f"时间解析失败 {s['detected_at']}: {e}")
            continue

        limit = KLINE_COUNTS.get(interval, 240)
        left, right = ZIGZAG_PARAMS.get(interval, (5, 5))

        try:
            klines, src = client.get_klines(symbol, interval, limit,
                                            end_time_ms=end_ms)
        except Exception as e:
            logger.warning(f"{symbol} 请求异常: {e}")
            fetched_fail += 1
            continue

        if len(klines) < 30:
            logger.info(f"{symbol} {interval} 数据不足({len(klines)}根)，跳过")
            fetched_fail += 1
            continue

        indicators = calc_indicators(klines)
        if indicators.atr_current <= 0:
            fetched_fail += 1
            continue

        pivots = find_pivots(klines, left=left, right=right)
        if len(pivots) < 3:
            fetched_fail += 1
            continue

        # 多尺度扫描（实测比单尺度命中率接近翻倍）
        found = engine.scan_multiscale(klines, symbol, interval)

        by_type_total[label] += 1

        # 命中判定分两档：
        #   宽松 —— 窗口内任何位置检出期望形态（可能是巧合匹配）
        #   严格 —— 形态末端距截图时刻 <=25 根K线（大概率就是截图的那个）
        RECENCY = 25
        matched = [p for p in found if p.pattern_type in expected]
        matched_recent = [
            p for p in matched
            if p.pivots and (len(klines) - 1
                             - max(q.index for q in p.pivots)) <= RECENCY
        ]
        matched_conf = [p for p in matched_recent
                        if p.status == PatternStatus.CONFIRMED]
        loose_hit = bool(matched)
        if loose_hit:
            loose_hits.append(s)
        if matched_recent:
            recent_hits.append(s)

        if matched_recent:
            hits.append((s, matched_recent[0]))
            by_type[label] += 1
            if matched_conf:
                confirmed_hits.append((s, matched_conf[0]))
            dist = len(klines) - 1 - max(q.index for q in matched_recent[0].pivots)
            print(f"  [{i:>3}] ✓ {symbol:<11}{interval:<5}"
                  f"期望={label:<26} 检出={matched_recent[0].pattern_type:<22}"
                  f"距={dist:<3}根 {'已确认' if matched_conf else '未突破'}"
                  f" 置信={matched_recent[0].confidence:.2f}")
        else:
            misses.append(s)
            got = ", ".join(sorted({p.pattern_type for p in found})) or "无"
            extra = f" (窗口内检出但位置远: {', '.join(sorted({p.pattern_type for p in matched}))})" if matched else ""
            print(f"  [{i:>3}] ✗ {symbol:<11}{interval:<5}"
                  f"期望={label:<26} 实际=[{got}]{extra}")

    # ---------- 汇总 ----------
    total = len(hits) + len(misses)
    print("\n" + "=" * 70)
    print("  回放结果")
    print("=" * 70)
    print(f"  有效测试      : {total}")
    print(f"  数据获取失败  : {fetched_fail}")
    if total:
        print(f"  严格命中(末端≤25根): {len(hits):>3}  ({len(hits) / total * 100:.1f}%)")
        print(f"  宽松命中(任意位置)  : {len(loose_hits):>3}  "
              f"({len(loose_hits) / total * 100:.1f}%)")
        print(f"  其中已确认突破      : {len(confirmed_hits):>3}  "
              f"({len(confirmed_hits) / total * 100:.1f}%)")
        print(f"  漏判                : {len(misses):>3}  "
              f"({len(misses) / total * 100:.1f}%)")
        print()
        print("  两档差异说明：宽松命中包含大量'数据别处的偶然匹配'，")
        print("  只有严格命中才大概率对应截图里的那个形态。")

    print()
    print("  按形态分类:")
    for label, cnt in by_type_total.most_common():
        h = by_type.get(label, 0)
        bar = "#" * round(h / max(1, cnt) * 20)
        print(f"    {label:<28} {h:>2}/{cnt:<3} {bar}")

    if misses:
        print()
        print("  漏判样本明细（前15个，用于针对性调参）:")
        for s in misses[:15]:
            print(f"    {s['symbol']:<8}{s['timeframe']:<5}"
                  f"{s['patterns'][0]:<28}{s['detected_at']}")

    print()
    print("  说明:")
    print("   · '命中(检出形态)' = 几何结构识别成功，是最核心的指标")
    print("   · '已确认突破' 偏低是正常的——截图时形态可能尚未突破，")
    print("     或突破未满足量能/幅度门槛")
    print("   · 漏判需逐个分析：是参数太严，还是该形态本身不该自动化")
    print()


if __name__ == "__main__":
    main()
