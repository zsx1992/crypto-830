# -*- coding: utf-8 -*-
"""
诊断: 双顶/双底的"峰"是否由长上影(插针)撑起来。

背景 (2026-09-11 朱哥 TAOUSDT 1h 金标准):
  右峰 269.78 是长上影, 实体顶只有 262.2 —— 影线比实体高 2.81%。
  而左峰(276.90) 实体 274.20, 影线仅高 0.97%。
  → 影线把"两峰价差"从实体的 4.4% 压到 2.57%, 刚好挤进 3% 容差。

本脚本量化: 若把"峰价"改用实体(收盘/开盘的较高者)计算, 有多少现存形态
会翻转(通过 → 不符), 以及影线占比的分布。

注意: 本脚本只做诊断, 不改检测逻辑。
用法: python tools/diag_peak_wick.py [--intervals 15m,1h,4h,1d]
"""
import os
import sys
import json
import argparse

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


def pct(v, q):
    if not v:
        return None
    return v[int(round(q * (len(v) - 1)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intervals", default="15m,1h,4h,1d")
    ap.add_argument("--json", default="output/diag_peak_wick.json")
    args = ap.parse_args()

    with open(os.path.join(_ROOT, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    eng = PatternEngine(cfg)
    tol = cfg.get("patterns", {}).get("tolerance", {})
    tol_min = tol.get("double_top_tol_min", tol.get("double_top_bottom_price", 0.03))
    tol_max = tol.get("double_top_tol_max", 0.05)
    span_lo = tol.get("double_top_tol_span_lo", 50)
    span_hi = tol.get("double_top_tol_span_hi", 200)

    def tol_for(span):
        if span <= span_lo:
            return tol_min
        if span >= span_hi:
            return tol_max
        return tol_min + (tol_max - tol_min) * (span - span_lo) / (span_hi - span_lo)

    records = []
    for iv in [s.strip() for s in args.intervals.split(",") if s.strip()]:
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
                if p.pattern_type not in ("double_top", "double_bottom"):
                    continue
                a, b, c = p.pivots
                is_top = p.pattern_type == "double_top"
                ka, kc = kl[a.index], kl[c.index]
                # 峰的"实体价": 高点看 max(o,c), 低点看 min(o,c)
                if is_top:
                    body_a = max(ka.open, ka.close)
                    body_c = max(kc.open, kc.close)
                    wick_a = (a.price - body_a) / a.price
                    wick_c = (c.price - body_c) / c.price
                else:
                    body_a = min(ka.open, ka.close)
                    body_c = min(kc.open, kc.close)
                    wick_a = (body_a - a.price) / a.price
                    wick_c = (body_c - c.price) / c.price
                span = c.index - a.index
                t = tol_for(span)
                diff_wick = abs(a.price - c.price) / max(a.price, c.price)
                diff_body = abs(body_a - body_c) / max(body_a, body_c)
                records.append({
                    "symbol": sym, "interval": iv, "type": p.pattern_type,
                    "span": span, "tol": round(t, 4),
                    "a": a.price, "c": c.price,
                    "body_a": round(body_a, 6), "body_c": round(body_c, 6),
                    "wick_a": round(wick_a, 4), "wick_c": round(wick_c, 4),
                    "wick_max": round(max(wick_a, wick_c), 4),
                    "diff_wick": round(diff_wick, 4),
                    "diff_body": round(diff_body, 4),
                    "flip": diff_body > t >= diff_wick,
                })

    print("=" * 78)
    print("样本 N = %d 个现存双顶/双底" % len(records))
    ws = sorted(r["wick_max"] for r in records)
    print("\n形态内'较大那侧影线占峰价比例'(wick_max) 分布:")
    print("  min=%.2f%% p25=%.2f%% p50=%.2f%% p75=%.2f%% p90=%.2f%% max=%.2f%%"
          % (ws[0] * 100, pct(ws, .25) * 100, pct(ws, .50) * 100,
             pct(ws, .75) * 100, pct(ws, .90) * 100, ws[-1] * 100))
    for thr in (0.01, 0.015, 0.02, 0.025, 0.03):
        n = sum(1 for w in ws if w > thr)
        print("    影线 > %.1f%% 的形态: %2d/%d (%.1f%%)"
              % (thr * 100, n, len(ws), 100.0 * n / len(ws)))

    flips = [r for r in records if r["flip"]]
    print("\n若把'峰价'改用实体价计算两峰价差:")
    print("  → 会从'通过'翻转为'不符容差'的形态: %d/%d (%.1f%%)"
          % (len(flips), len(records), 100.0 * len(flips) / len(records)))
    if flips:
        print("\n  %-12s %-4s %-13s %6s %7s %7s %8s"
              % ("symbol", "iv", "type", "span", "影线差", "实体差", "容差"))
        for r in sorted(flips, key=lambda z: -(z["diff_body"] - z["diff_wick"]))[:15]:
            print("  %-12s %-4s %-13s %6d %6.2f%% %6.2f%% %7.2f%%"
                  % (r["symbol"], r["interval"], r["type"], r["span"],
                     r["diff_wick"] * 100, r["diff_body"] * 100, r["tol"] * 100))

    print("\n影线最大的 12 个:")
    print("  %-12s %-4s %-13s %8s %8s %8s"
          % ("symbol", "iv", "type", "峰A影线", "峰C影线", "影线差"))
    for r in sorted(records, key=lambda z: -z["wick_max"])[:12]:
        print("  %-12s %-4s %-13s %7.2f%% %7.2f%% %7.2f%%"
              % (r["symbol"], r["interval"], r["type"],
                 r["wick_a"] * 100, r["wick_c"] * 100, r["diff_wick"] * 100))

    out = args.json if os.path.isabs(args.json) else os.path.join(_ROOT, args.json)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print("\n明细已落盘: %s" % out)


if __name__ == "__main__":
    main()
