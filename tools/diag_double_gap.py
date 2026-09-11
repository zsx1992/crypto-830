# -*- coding: utf-8 -*-
"""
诊断: 双顶/双底 3 个锚点 (A, B, C) 的相邻间隔分布。

背景 (2026-09-11 朱哥 TAOUSDT 1h 金标准):
  "仅凭这几根K线不足以构成有效的双顶"。
  像素反解: 谷(249.2) → 右峰(269.78, 长上影插针) 只隔 ~4px ≈ 2~4 根 @1h。
  与 CRV 头肩顶 (右颈锚-右肩隔 2 根) 同构 —— 右半形态被压扁。
  现有约束只查总跨度 span (double_top_min/max), 不查相邻锚点间距。

本脚本统计所有现存双顶/双底的:
  - 2 个相邻间隔: A-B, B-C
  - 总跨度 C-A
输出分位数与"若设阈值会砍掉多少"。

用法: python tools/diag_double_gap.py [--intervals 15m,1h,4h,1d]
"""
import os
import sys
import json
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
    if not ms:
        return "-"
    return datetime.datetime.fromtimestamp(
        ms / 1000, datetime.timezone.utc).strftime("%m-%d %H:%M")


def pct(v, q):
    if not v:
        return None
    return v[int(round(q * (len(v) - 1)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intervals", default="15m,1h,4h,1d")
    ap.add_argument("--json", default="output/diag_double_gap.json")
    args = ap.parse_args()

    with open(os.path.join(_ROOT, "config.yaml"), encoding="utf-8") as f:
        config = yaml.safe_load(f)
    engine = PatternEngine(config)
    intervals = [s.strip() for s in args.intervals.split(",") if s.strip()]

    records, per_iv = [], {}
    for iv in intervals:
        d = os.path.join(_ROOT, "data", "klines_%s" % iv)
        if not os.path.isdir(d):
            continue
        n_sym = n_pat = 0
        for sym in sorted(os.listdir(d)):
            csvp = os.path.join(d, sym, "%s.csv" % sym)
            if not os.path.isfile(csvp):
                continue
            kl = load_csv(csvp)
            if len(kl) < 80:
                continue
            n_sym += 1
            try:
                pats = engine.scan_multiscale(kl, symbol=sym, interval=iv)
            except Exception as e:  # noqa: BLE001
                print("  [skip] %s %s: %s" % (iv, sym, e))
                continue
            for p in pats:
                if p.pattern_type not in ("double_top", "double_bottom"):
                    continue
                pv = p.pivots
                if len(pv) != 3:
                    continue
                a, b, c = pv
                g_ab = b.index - a.index
                g_bc = c.index - b.index
                span = c.index - a.index
                records.append({
                    "symbol": sym, "interval": iv, "type": p.pattern_type,
                    "status": p.status.value if hasattr(p.status, "value")
                    else str(p.status), "conf": p.confidence,
                    "a": ts(a.timestamp), "b": ts(b.timestamp), "c": ts(c.timestamp),
                    "pa": a.price, "pb": b.price, "pc": c.price,
                    "g_ab": g_ab, "g_bc": g_bc, "span": span,
                    "dir": p.direction.value if hasattr(p.direction, "value")
                    else str(p.direction),
                })
                n_pat += 1
        per_iv[iv] = {"symbols": n_sym, "patterns": n_pat}

    print("\n" + "=" * 74)
    print("每周期统计")
    for iv, s in per_iv.items():
        print("  %-4s 扫描 %3d 币 → 检出双顶/双底 %3d 个" % (iv, s["symbols"], s["patterns"]))

    print("\n" + "=" * 74)
    print("相邻锚点间隔分位数 (N=%d)" % len(records))
    print("%-10s %6s %6s %6s %6s %6s %6s" % ("gap", "min", "p05", "p10", "p25", "p50", "max"))
    for key in ("g_ab", "g_bc", "span"):
        v = sorted(r[key] for r in records)
        print("%-10s %6s %6s %6s %6s %6s %6s" % (
            key, v[0] if v else "-", pct(v, .05), pct(v, .10), pct(v, .25),
            pct(v, .50), v[-1] if v else "-"))

    mins = sorted(min(r["g_ab"], r["g_bc"]) for r in records)
    print("\n形态内部最短相邻间隔 (min of A-B, B-C):")
    print("  min=%s p05=%s p10=%s p25=%s p50=%s max=%s" % (
        mins[0] if mins else "-", pct(mins, .05), pct(mins, .10),
        pct(mins, .25), pct(mins, .50), mins[-1] if mins else "-"))
    for thr in (2, 3, 4, 5, 6, 8, 10):
        hit = sum(1 for m in mins if m < thr)
        print("    若 min_pivot_gap=%2d → 砍掉 %3d/%d (%.1f%%)"
              % (thr, hit, len(mins), 100.0 * hit / max(1, len(mins))))

    print("\n" + "=" * 74)
    print("最拥挤的 25 个 (按内部最短间隔升序):")
    print("%-10s %-4s %-24s %5s %5s %6s  %s" %
          ("symbol", "iv", "type", "A-B", "B-C", "span", "A→B→C 时间"))
    for r in sorted(records, key=lambda r: min(r["g_ab"], r["g_bc"]))[:25]:
        print("%-10s %-4s %-24s %5d %5d %6d  %s→%s→%s" % (
            r["symbol"], r["interval"], r["type"], r["g_ab"], r["g_bc"],
            r["span"], r["a"], r["b"], r["c"]))

    out = args.json if os.path.isabs(args.json) else os.path.join(_ROOT, args.json)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"records": records, "per_interval": per_iv}, f,
                  ensure_ascii=False, indent=2)
    print("\n明细已落盘: %s" % out)


if __name__ == "__main__":
    main()
