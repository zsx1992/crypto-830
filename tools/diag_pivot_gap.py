# -*- coding: utf-8 -*-
"""
诊断: 头肩顶/底的 5 个锚点 (LS, N1, Head, N2, RS) 相邻间隔分布。

背景 (2026-09-11):
  朱哥指出 CRVUSDT 1h 头肩顶"所选几根K线彼此距离太近, 缺乏参考意义"。
  像素反解: 右颈锚 N2 与右肩 RS 只隔 ~4px ≈ 2.25 根 @1h —— 右半形态被压扁。
  但现有约束只查"总跨度 span" (min_span=15/35), 不查相邻锚点间距。

本脚本用本地缓存跑真实 PatternEngine, 统计所有 H&S 形态的:
  - 4 个相邻间隔: LS-N1, N1-Head, Head-N2, N2-RS
  - 总跨度 RS-LS
输出分位数, 供选 min_gap 阈值时参考"会自动砍掉多少"。

用法:
  python tools/diag_pivot_gap.py [--intervals 1h,4h] [--json out.json]
"""
import os
import sys
import csv
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


def pct(sorted_vals, q):
    if not sorted_vals:
        return None
    k = int(round(q * (len(sorted_vals) - 1)))
    return sorted_vals[k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intervals", default="1h,4h")
    ap.add_argument("--json", default="output/diag_pivot_gap.json")
    args = ap.parse_args()

    with open(os.path.join(_ROOT, "config.yaml"), encoding="utf-8") as f:
        config = yaml.safe_load(f)

    engine = PatternEngine(config)
    intervals = [s.strip() for s in args.intervals.split(",") if s.strip()]

    records = []
    per_iv_stats = {}

    for iv in intervals:
        d = os.path.join(_ROOT, "data", "klines_%s" % iv)
        if not os.path.isdir(d):
            continue
        syms = sorted(os.listdir(d))
        n_sym = 0
        n_pat = 0
        for sym in syms:
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
                if p.pattern_type not in ("head_shoulders_top",
                                          "head_shoulders_bottom"):
                    continue
                pv = p.pivots
                if len(pv) != 5:
                    continue
                ls, n1, hd, n2, rs = pv
                g_ls_n1 = n1.index - ls.index
                g_n1_hd = hd.index - n1.index
                g_hd_n2 = n2.index - hd.index
                g_n2_rs = rs.index - n2.index
                span = rs.index - ls.index
                # 颈线两锚点间隔 (N1 <-> N2)
                g_neck = n2.index - n1.index
                rec = {
                    "symbol": sym, "interval": iv,
                    "type": p.pattern_type, "status": p.status.value
                    if hasattr(p.status, "value") else str(p.status),
                    "conf": p.confidence,
                    "ls": ts(ls.timestamp), "n1": ts(n1.timestamp),
                    "head": ts(hd.timestamp), "n2": ts(n2.timestamp),
                    "rs": ts(rs.timestamp),
                    "g_ls_n1": g_ls_n1, "g_n1_hd": g_n1_hd,
                    "g_hd_n2": g_hd_n2, "g_n2_rs": g_n2_rs,
                    "g_neck": g_neck, "span": span,
                }
                records.append(rec)
                n_pat += 1
        per_iv_stats[iv] = {"symbols": n_sym, "patterns": n_pat}

    # ---- 统计 ----
    print("\n" + "=" * 78)
    print("每周期统计")
    for iv, s in per_iv_stats.items():
        print("  %-4s 扫描 %3d 币 → 检出 H&S %3d 个" % (iv, s["symbols"],
                                                        s["patterns"]))

    print("\n" + "=" * 78)
    print("相邻锚点间隔分位数 (全周期合计, N=%d)" % len(records))
    print("%-12s %6s %6s %6s %6s %6s %6s" %
          ("gap", "min", "p05", "p10", "p25", "p50", "max"))
    for key in ("g_ls_n1", "g_n1_hd", "g_hd_n2", "g_n2_rs", "g_neck", "span"):
        vals = sorted(r[key] for r in records)
        print("%-12s %6s %6s %6s %6s %6s %6s" % (
            key,
            vals[0] if vals else "-",
            pct(vals, 0.05), pct(vals, 0.10), pct(vals, 0.25),
            pct(vals, 0.50), vals[-1] if vals else "-"))

    # 最短相邻间隔 (形态内部 4 个 gap 的最小值) 分布
    mins = sorted(min(r["g_ls_n1"], r["g_n1_hd"], r["g_hd_n2"],
                      r["g_n2_rs"]) for r in records)
    print("\n形态内部最短相邻间隔 分布 (min over 4 gaps):")
    print("  min=%s p05=%s p10=%s p25=%s p50=%s p75=%s max=%s" % (
        mins[0] if mins else "-", pct(mins, 0.05), pct(mins, 0.10),
        pct(mins, 0.25), pct(mins, 0.50), pct(mins, 0.75),
        mins[-1] if mins else "-"))

    for thr in (3, 5, 8, 10, 12, 15):
        hit = sum(1 for m in mins if m < thr)
        print("    若 min_gap=%2d  → 会砍掉 %3d/%d (%.1f%%)"
              % (thr, hit, len(mins), 100.0 * hit / max(1, len(mins))))

    # 最"挤"的 20 个案例
    print("\n" + "=" * 78)
    print("最拥挤的 20 个形态 (按内部最短间隔升序):")
    print("%-10s %-4s %-22s %5s %5s %5s %5s %6s" %
          ("symbol", "iv", "type", "LS-N1", "N1-HD", "HD-N2", "N2-RS", "span"))
    for r in sorted(records, key=lambda r: min(
            r["g_ls_n1"], r["g_n1_hd"], r["g_hd_n2"], r["g_n2_rs"]))[:20]:
        print("%-10s %-4s %-22s %5d %5d %5d %5d %6d" % (
            r["symbol"], r["interval"], r["type"], r["g_ls_n1"],
            r["g_n1_hd"], r["g_hd_n2"], r["g_n2_rs"], r["span"]))

    out = os.path.join(_ROOT, args.json) if not os.path.isabs(args.json) \
        else args.json
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"records": records, "per_interval": per_iv_stats},
                  f, ensure_ascii=False, indent=2)
    print("\n明细已落盘: %s" % out)


if __name__ == "__main__":
    main()
