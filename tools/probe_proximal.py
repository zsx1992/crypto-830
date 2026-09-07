# -*- coding: utf-8 -*-
"""本地复现: 近端突破为什么没被确认?

用 data/klines_4h (Binance 12列 CSV) 对每标的跑 scan_multiscale,
按 status(CANDIDATE/CONFIRMED/FAILED) 统计 breakout age 分布,
重点看"近端(age<=20)有没有 CANDIDATE/FAILED 但 CONFIRMED 为零"——
区分: 市场没供给(近端连 CANDIDATE 都没有) vs 确认链吞近端
(CANDIDATE/FAILED 大量聚集近端, CONFIRMED 都偏老)。

用法: python tools/probe_proximal.py [--limit N] [--interval 4h]
"""
import os
import sys
import glob
import argparse
import logging

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from market_data import Kline
from detector import PatternEngine
from patterns.base import PatternStatus
import yaml

logging.basicConfig(level=logging.WARNING)


def load_binance_csv(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for i, ln in enumerate(f):
            if i == 0 and ln.startswith("open_time"):
                continue
            p = ln.strip().split(",")
            if len(p) < 7:
                continue
            try:
                rows.append(Kline(
                    openTime=int(p[0]), open=float(p[1]), high=float(p[2]),
                    low=float(p[3]), close=float(p[4]), volume=float(p[5]),
                    closeTime=int(p[6]),
                    quoteVolume=float(p[7]) if len(p) > 7 else 0.0))
            except ValueError:
                continue
    rows.sort(key=lambda k: k.openTime)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0=全部")
    ap.add_argument("--interval", default="4h")
    ap.add_argument("--min-age", type=int, default=0)
    ap.add_argument("--scales", default="", help="覆盖 multiscale, 如 2,3,5")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(os.path.join(_ROOT, "config.yaml"),
                              encoding="utf-8"))
    engine = PatternEngine(cfg)
    scales = cfg.get("zigzag", {}).get("multiscale", [5])
    if args.scales:
        scales = [int(x) for x in args.scales.split(",")]
    print(f"使用 scales={scales}", file=sys.stderr)

    data_dir = os.path.join(_ROOT, "data", f"klines_{args.interval}")
    files = sorted(glob.glob(os.path.join(data_dir, "*", "*.csv")))
    if not files:
        files = sorted(glob.glob(os.path.join(data_dir, "*.csv")))
    if args.limit:
        files = files[:args.limit]

    # 统计: 每个状态一个 age 桶 (0-10/11-20/21-50/51-100/100+)
    BUCKETS = [(0, 10), (11, 20), (21, 50), (51, 100), (101, 10**9)]
    from collections import defaultdict
    stats = defaultdict(lambda: defaultdict(int))  # status -> bucket -> count
    per_sym = []
    n_no_pivot = 0
    total_k = 0

    for fi, path in enumerate(files):
        sym = os.path.basename(os.path.dirname(path)) or \
            os.path.basename(path).split(".")[0]
        kl = load_binance_csv(path)
        if len(kl) < 100:
            continue
        total_k += len(kl)
        try:
            found = engine.scan_multiscale(
                kl, symbol=sym, interval=args.interval, scales=scales)
        except Exception as e:
            print(f"[{sym}] scan 异常: {e}")
            continue

        last = len(kl) - 1
        st_map = {"CANDIDATE": 0, "CONFIRMED": 0, "FAILED": 0}
        for p in found:
            st_map[p.status.value if hasattr(p.status, "value")
                   else str(p.status)] = st_map.get(
                p.status.value if hasattr(p.status, "value") else str(p.status),
                0) + 1
        # 按 age 分桶
        for p in found:
            st = p.status.value if hasattr(p.status, "value") else str(p.status)
            if p.breakout_index < 0:
                n_no_pivot += 1
                stats[st]["no_breakout"] += 1
                continue
            age = last - p.breakout_index
            for lo, hi in BUCKETS:
                if lo <= age <= hi:
                    stats[st][f"{lo}-{hi}"] += 1
                    break
        per_sym.append((sym, len(kl), st_map))
        if (fi + 1) % 10 == 0:
            print(f"  processed {fi+1}/{len(files)}", file=sys.stderr)

    # 输出
    print(f"\n标的: {len(per_sym)}  K线均值: {total_k // max(len(per_sym),1)}  "
          f"multiscale={scales} interval={args.interval}")
    print(f"{'status':<10} {'0-10':>6} {'11-20':>6} {'21-50':>6} "
          f"{'51-100':>7} {'100+':>6} {'no_break':>9}")
    for st in ("CANDIDATE", "CONFIRMED", "FAILED"):
        if not stats[st]:
            continue
        b = stats[st]
        row = [b.get(f"{lo}-{hi}", 0) for lo, hi in BUCKETS]
        print(f"{st:<10} {row[0]:>6} {row[1]:>6} {row[2]:>6} "
              f"{row[3]:>7} {row[4]:>6} {b.get('no_breakout', 0):>9}")
    # 近端供给检查: age<=20 的 CANDIDATE+CONFIRMED+FAILED(有突破点)
    near_c = sum(stats["CANDIDATE"].get(f"{lo}-{hi}", 0)
                 for lo, hi in BUCKETS[:2])
    near_f = sum(stats["FAILED"].get(f"{lo}-{hi}", 0)
                 for lo, hi in BUCKETS[:2])
    near_k = sum(stats["CONFIRMED"].get(f"{lo}-{hi}", 0)
                 for lo, hi in BUCKETS[:2])
    print(f"\n近端(age<=20): CANDIDATE={near_c}  CONFIRMED={near_k}  "
          f"FAILED(intact杀)={near_f}")


if __name__ == "__main__":
    main()
