# -*- coding: utf-8 -*-
"""
A/B: head_shoulders_min_gap = 0 (改动前) vs 4 (本次) ，全池 H&S 检出差异。

用法: python tools/ab_hs_min_gap.py [--intervals 15m,1h,4h,1d]
"""
import os
import sys
import csv
import argparse
import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

import yaml  # noqa: E402
from market_data import Kline  # noqa: E402
from detector import PatternEngine  # noqa: E402
import copy  # noqa: E402


def load_csv(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            p = ln.strip().split(",")
            if len(p) < 7:
                continue
            try:
                t = int(p[0])
            except ValueError:
                continue
            try:
                rows.append(Kline(
                    openTime=t, open=float(p[1]), high=float(p[2]),
                    low=float(p[3]), close=float(p[4]), volume=float(p[5]),
                    closeTime=int(p[6]),
                    quoteVolume=float(p[7]) if len(p) > 7 else 0.0))
            except (ValueError, IndexError):
                continue
    rows.sort(key=lambda k: k.openTime)
    return rows


def ts(ms):
    if not ms:
        return "-"
    return datetime.datetime.fromtimestamp(
        ms / 1000, datetime.timezone.utc).strftime("%m-%d %H:%M")


def key_of(p):
    pv = p.pivots
    return (p.symbol, p.interval, p.pattern_type,
            tuple(x.index for x in pv))


def run_pool(cfg, intervals):
    eng = PatternEngine(cfg)
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
                pats = eng.scan_multiscale(kl, symbol=sym, interval=iv)
            except Exception:  # noqa: BLE001
                continue
            for p in pats:
                if p.pattern_type in ("head_shoulders_top",
                                      "head_shoulders_bottom"):
                    out[key_of(p)] = p
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intervals", default="15m,1h,4h,1d")
    args = ap.parse_args()
    intervals = [s.strip() for s in args.intervals.split(",") if s.strip()]

    with open(os.path.join(_ROOT, "config.yaml"), encoding="utf-8") as f:
        base = yaml.safe_load(f)

    cfg_off = copy.deepcopy(base)
    cfg_off.setdefault("patterns", {}).setdefault("span", {})[
        "head_shoulders_min_gap"] = 0
    cfg_on = copy.deepcopy(base)
    cfg_on.setdefault("patterns", {}).setdefault("span", {})[
        "head_shoulders_min_gap"] = 4

    print("跑 A(改前, gap=0) ...")
    a = run_pool(cfg_off, intervals)
    print("跑 B(改后, gap=4) ...")
    b = run_pool(cfg_on, intervals)

    print("\n" + "=" * 78)
    print("A(改前) 检出 %d 个 H&S | B(改后) 检出 %d 个" % (len(a), len(b)))
    print("=" * 78)

    removed = [k for k in a if k not in b]
    added = [k for k in b if k not in a]

    print("\n被砍掉 %d 个:" % len(removed))
    if removed:
        print("%-12s %-4s %-22s %6s %-15s" %
              ("symbol", "iv", "type", "conf", "锚点idx"))
        for k in sorted(removed):
            p = a[k]
            pv = p.pivots
            idx = tuple(x.index for x in pv)
            gaps = (idx[1] - idx[0], idx[2] - idx[1],
                    idx[3] - idx[2], idx[4] - idx[3])
            print("%-12s %-4s %-22s %6.3f %-15s gaps=%s span=%d" % (
                k[0], k[1], k[2], p.confidence, str(idx), str(gaps),
                idx[4] - idx[0]))
    print("\n新增 %d 个 (应为 0)" % len(added))
    for k in sorted(added):
        print("  +", k)

    # 保留的形态里, 最接近阈值的
    print("\nB 中相邻间隔最小的 8 个 (最接近被砍边缘):")
    rows = []
    for k, p in b.items():
        idx = tuple(x.index for x in p.pivots)
        g = min(idx[1] - idx[0], idx[2] - idx[1],
                idx[3] - idx[2], idx[4] - idx[3])
        rows.append((g, idx, k))
    for g, idx, k in sorted(rows)[:8]:
        print("   min_gap=%2d  %-12s %-4s %-22s idx=%s" % (
            g, k[0], k[1], k[2], str(idx)))


if __name__ == "__main__":
    main()
