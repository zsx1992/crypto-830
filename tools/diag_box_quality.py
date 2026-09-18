# -*- coding: utf-8 -*-
"""
诊断: box 类形态(rectangle / ascending_channel / descending_channel) 的两大质量问题。

背景 (2026-09-18 朱哥 "时隔这么久推送质量还是一般"):
  查云端 state 近 8 天 68 条正式推送, 形态构成:
    double_bottom LONG 16 / head_shoulders_bottom LONG 11
    descending_channel LONG 11 / double_top SHORT 9 / head_shoulders_top SHORT 7
    ascending_channel SHORT 6 / descending_channel SHORT 5 / ascending_channel LONG 3
  两个可疑点:
   ① channel 合计 25/68 = 37% —— 占推送比重最大, 但 channel 是 box.py 里
      "矩形判定失败后的兜底分类"(两边都斜且同向 -> 通道), 不是独立识别的形态。
   ② 同一形态出现双向: descending_channel LONG 11 / SHORT 5,
      ascending_channel SHORT 6 / LONG 3 —— 源于 box.py 第 262 行
      `for direction in (LONG, SHORT)` 双向尝试。

本脚本量化:
  - box 类形态的上/下边界触点数分布 (min_touches=2 是否过松)
  - 双向推送占比 (同一形态是否既推多又推空)
  - 若把 min_touches 提到 3 / 4, 会砍掉多少 box 形态

用法: python tools/diag_box_quality.py [--intervals 15m,1h,4h,1d]
"""
import os
import sys
import json
import argparse
from collections import Counter

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

import yaml  # noqa: E402
from market_data import Kline  # noqa: E402
from detector import PatternEngine  # noqa: E402
import patterns.box as boxmod  # noqa: E402


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


# ---- monkey-patch: 记录每条边界的触点数 ----
RECORDS = []
_orig_build = boxmod.BoxDetector._build_and_confirm


def _patched_build(self, kind, direction, upper, lower,
                   upper_touches, lower_touches, *a, **kw):
    pat = _orig_build(self, kind, direction, upper, lower,
                      upper_touches, lower_touches, *a, **kw)
    # 记录每一次尝试(含未确认的), 用 pattern 对象做关联
    if pat is not None:
        try:
            pat.__dict__["_ut"] = upper_touches
            pat.__dict__["_lt"] = lower_touches
            pat.__dict__["_kind"] = kind
        except Exception:
            pass
    return pat


boxmod.BoxDetector._build_and_confirm = _patched_build


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intervals", default="15m,1h,4h,1d")
    ap.add_argument("--json", default="output/diag_box_quality.json")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(os.path.join(_ROOT, "config.yaml"),
                              encoding="utf-8"))
    eng = PatternEngine(cfg)
    intervals = [s.strip() for s in args.intervals.split(",") if s.strip()]

    recs = []
    for iv in intervals:
        d = os.path.join(_ROOT, "data", "klines_%s" % iv)
        if not os.path.isdir(d):
            print("跳过(无缓存):", iv)
            continue
        syms = sorted(os.listdir(d))
        n = 0
        for s in syms:
            f = os.path.join(d, s, "%s.csv" % s)
            if not os.path.isfile(f):
                f2 = os.path.join(d, s)
                if os.path.isfile(f2):
                    f = f2
                else:
                    continue
            try:
                ks = load_csv(f)
            except Exception:
                continue
            if len(ks) < 120:
                continue
            try:
                pats = eng.scan_multiscale(ks, symbol=s, interval=iv)
            except Exception as e:
                print("  ERR", s, iv, type(e).__name__, str(e)[:60])
                continue
            for p in pats:
                if p.pattern_type not in ("rectangle", "ascending_channel",
                                          "descending_channel"):
                    continue
                ut = getattr(p, "_ut", None)
                lt = getattr(p, "_lt", None)
                if ut is None:
                    continue
                recs.append({
                    "symbol": s, "interval": iv, "type": p.pattern_type,
                    "direction": str(p.direction).replace("Direction.", ""),
                    "ut": ut, "lt": lt, "touches": ut + lt,
                    "status": str(p.status).replace("PatternStatus.", ""),
                    "confidence": getattr(p, "confidence", None),
                    "span": (p.pivots[-1].index - p.pivots[0].index)
                            if p.pivots else None,
                })
                n += 1
        print("  %-4s 扫描 %d 个标的, box 类 %d 个" % (iv, len(syms), n))

    if not recs:
        print("无 box 类形态")
        return

    # ---- 触点分布 ----
    print("\n=== 触点数分布 (全池 %d 个 box 形态) ===" % len(recs))
    tc = Counter(r["touches"] for r in recs)
    for k in sorted(tc):
        print("  合计触点 %2d : %3d 个  %s" % (k, tc[k], "#" * tc[k]))
    print("  单边最小(ut/lt) 分布:",
          dict(Counter(min(r["ut"], r["lt"]) for r in recs)))

    # ---- 按形态+方向 ----
    print("\n=== 形态 × 方向 ===")
    cd = Counter((r["type"], r["direction"]) for r in recs)
    for (t, dr), n in cd.most_common():
        print("  %-22s %-6s %3d" % (t, dr, n))

    print("\n=== 同一 symbol+interval 是否双向推送 ===")
    bykey = {}
    for r in recs:
        bykey.setdefault((r["symbol"], r["interval"], r["type"]), set()).add(
            r["direction"])
    both = {k: v for k, v in bykey.items() if len(v) > 1}
    print("  双向组合 %d / %d (%.1f%%)" %
          (len(both), len(bykey), 100.0 * len(both) / max(1, len(bykey))))
    for k in list(both)[:12]:
        print("   ", k)

    # ---- 提阈值的代价 ----
    print("\n=== 若提高 min_touches 的代价 ===")
    for t in (2, 3, 4):
        keep = [r for r in recs
                if min(r["ut"], r["lt"]) >= t]
        print("  每条边界 >=%d 触点: 保留 %3d / %d (%.1f%%), 砍掉 %d"
              % (t, len(keep), len(recs), 100.0 * len(keep) / len(recs),
                 len(recs) - len(keep)))
    print("  注: 当前 min_touches=2 表示每条边界至少 2 点(合计>=4)")

    os.makedirs(os.path.dirname(args.json), exist_ok=True)
    json.dump({"records": recs}, open(args.json, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n明细 ->", args.json)


if __name__ == "__main__":
    main()
