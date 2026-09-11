# -*- coding: utf-8 -*-
"""
A/B: 双顶/双底 min_pivot_gap 开关前后, 全池检出差异。

A = 关闭约束 (min_pivot_gap=0, 等价改动前)
B = 落地值  (min_pivot_gap=5)

输出: 被砍掉 / 新增 的形态清单, 用于确认没有误伤。

用法: python tools/ab_double_min_gap.py [--intervals 15m,1h,4h,1d]
"""
import os
import sys
import copy
import argparse
import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

import yaml  # noqa: E402
from market_data import Kline  # noqa: E402
from detector import PatternEngine  # noqa: E402


def load_csv(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            p = ln.strip().split(",")
            if len(p) < 7:
                continue
            try:
                int(p[0])
            except ValueError:
                continue
            try:
                rows.append(Kline(
                    openTime=int(p[0]), open=float(p[1]), high=float(p[2]),
                    low=float(p[3]), close=float(p[4]), volume=float(p[5]),
                    closeTime=int(p[6]),
                    quoteVolume=float(p[7]) if len(p) > 7 else 0.0))
            except (ValueError, IndexError):
                continue
    rows.sort(key=lambda k: k.openTime)
    return rows


def ts(ms):
    return datetime.datetime.fromtimestamp(
        ms / 1000, datetime.timezone.utc).strftime("%m-%d %H:%M")


def key_of(p, sym, iv):
    pv = p.pivots
    idxs = "-".join(str(x.index) for x in pv)
    return "%s|%s|%s|%s" % (sym, iv, p.pattern_type, idxs)


def collect(engine, intervals):
    out = {}
    for iv in intervals:
        d = os.path.join(_ROOT, "data", "klines_%s" % iv)
        if not os.path.isdir(d):
            continue
        for sym in sorted(os.listdir(d)):
            csvp = os.path.join(d, sym, "%s.csv" % sym)
            if not os.path.isfile(csvp):
                continue
            kl = load_csv(csvp)
            if len(kl) < 80:
                continue
            try:
                pats = engine.scan_multiscale(kl, symbol=sym, interval=iv)
            except Exception:  # noqa: BLE001
                continue
            for p in pats:
                if p.pattern_type not in ("double_top", "double_bottom"):
                    continue
                out[key_of(p, sym, iv)] = p
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intervals", default="15m,1h,4h,1d")
    args = ap.parse_args()

    with open(os.path.join(_ROOT, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg_off = copy.deepcopy(cfg)
    cfg_off["patterns"]["span"]["double_min_pivot_gap"] = 0

    intervals = [s.strip() for s in args.intervals.split(",") if s.strip()]
    print("跑 A (min_pivot_gap=0) ...")
    A = collect(PatternEngine(cfg_off), intervals)
    print("跑 B (min_pivot_gap=%s) ..."
          % cfg["patterns"]["span"].get("double_min_pivot_gap"))
    B = collect(PatternEngine(cfg), intervals)

    dropped = [A[k] for k in A if k not in B]
    added = [B[k] for k in B if k not in A]
    common = [k for k in A if k in B]

    print("\n" + "=" * 78)
    print("A(min_gap=0) = %d 个   B(min_gap=5) = %d 个   共同 = %d"
          % (len(A), len(B), len(common)))
    print("被砍掉 %d 个 / 新增 %d 个" % (len(dropped), len(added)))

    for lbl, lst in (("被砍掉", dropped), ("新增", added)):
        if not lst:
            print("\n%s: (无)" % lbl)
            continue
        print("\n%s (%d):" % (lbl, len(lst)))
        print("  %-12s %-4s %-14s %5s %5s %6s  %s"
              % ("symbol", "iv", "type", "A-B", "B-C", "span", "A→B→C"))
        for p in sorted(lst, key=lambda q: q.pivots[1].index - q.pivots[0].index):
            a, b, c = p.pivots
            print("  %-12s %-4s %-14s %5d %5d %6d  %s→%s→%s"
                  % (p.symbol, p.interval, p.pattern_type,
                     b.index - a.index, c.index - b.index, c.index - a.index,
                     ts(a.timestamp), ts(b.timestamp), ts(c.timestamp)))

    if A:
        print("\n保留率 = %.1f%%" % (100.0 * len(common) / len(A)))


if __name__ == "__main__":
    main()
