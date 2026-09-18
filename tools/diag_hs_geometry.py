# -*- coding: utf-8 -*-
"""
诊断: 头肩形态的几何质量分布 —— 找它为什么回测最差。

背景 (2026-09-18 回测, 134 个 CONFIRMED 样本):
  descending_channel 期望 +0.554 / ascending_channel +0.378
  double_bottom +0.157 / double_top -0.102
  head_shoulders_top -0.134 / head_shoulders_bottom -0.145   <- 全场最差
  且"目标距离扫描"显示头肩在 0.5R~2.0R 全是负期望 -> 不是参数问题, 是真的弱。

  假设: 头肩的几何容差太松, 导致 morph 质量差。
    对比双顶: min_depth = 0.05 (中间谷深 >= 5%)
    头肩只有: head_prominence = 0.02 (头高过肩 >= 2%)
    2% 在币圈约等于一根 K 线的波动 —— 头和肩几乎一样高,
    那就不是头肩, 是震荡。

本脚本在全池上统计头肩的实际几何:
  - head_prominence 实际值 (头相对两肩均值的高度)
  - 两肩对称性 |LS-RS| / 均值
  - 颈线水平度 |N1-N2| / 均值
  - 形态高度 / ATR
并与双顶的 min_depth 做对照, 看收紧各阈值的代价。

用法: python tools/diag_hs_geometry.py [--intervals 15m,1h,4h,1d]
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


def pct(x):
    return 100.0 * x


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intervals", default="15m,1h,4h,1d")
    ap.add_argument("--json", default="output/diag_hs_geometry.json")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(os.path.join(_ROOT, "config.yaml"),
                              encoding="utf-8"))
    eng = PatternEngine(cfg)

    hs, dbl = [], []

    for iv in [s.strip() for s in args.intervals.split(",")]:
        d = os.path.join(_ROOT, "data", "klines_%s" % iv)
        if not os.path.isdir(d):
            continue
        for s in sorted(os.listdir(d)):
            f = os.path.join(d, s, "%s.csv" % s)
            if not os.path.isfile(f):
                continue
            try:
                ks = load_csv(f)
            except Exception:
                continue
            if len(ks) < 150:
                continue
            try:
                pats = eng.scan_multiscale(ks, symbol=s, interval=iv)
            except Exception:
                continue
            for p in pats:
                pv = sorted(p.pivots, key=lambda v: v.index)
                if p.pattern_type.startswith("head_shoulders"):
                    if len(pv) < 5:
                        continue
                    # 头肩顶: pivots 顺序 LS(H), N1(L), Head(H), N2(L), RS(H)
                    # 用价格高低反推角色，避免依赖 pivots 顺序
                    tops = [v for v in pv if v.price >=
                            sorted(x.price for x in pv)[len(pv) // 2]]
                    lows = [v for v in pv if v not in tops]
                    if len(tops) < 3 or len(lows) < 2:
                        continue
                    tops_s = sorted(tops, key=lambda v: v.index)
                    is_top = p.pattern_type.endswith("top")
                    if is_top:
                        head = max(tops_s, key=lambda v: v.price)
                    else:
                        head = min(tops_s, key=lambda v: v.price)
                    sh = [v for v in tops_s if v is not head]
                    if len(sh) != 2:
                        continue
                    sh_mean = (sh[0].price + sh[1].price) / 2.0
                    prom = abs(head.price - sh_mean) / abs(sh_mean)
                    sym = abs(sh[0].price - sh[1].price) / abs(sh_mean)
                    n1, n2 = lows[0], lows[1]
                    neck = abs(n1.price - n2.price) / \
                        ((abs(n1.price) + abs(n2.price)) / 2.0)
                    hs.append({
                        "symbol": s, "interval": iv, "type": p.pattern_type,
                        "prom": prom, "sym": sym, "neck": neck,
                        "span": pv[-1].index - pv[0].index,
                    })
                elif p.pattern_type.startswith("double"):
                    if len(pv) < 3:
                        continue
                    ps = sorted(pv, key=lambda v: v.index)
                    a, b, c = ps[0], ps[1], ps[2]
                    ends = (a.price + c.price) / 2.0
                    depth = abs(ends - b.price) / abs(ends)
                    dbl.append({"symbol": s, "interval": iv,
                                "type": p.pattern_type, "depth": depth})
        print("  %-4s 完成" % iv, flush=True)

    def dist(vals, name, unit="%"):
        if not vals:
            return
        vals = sorted(vals)
        n = len(vals)

        def q(f):
            return vals[min(n - 1, int(f * n))]
        print("\n=== %s (n=%d) ===" % (name, n))
        print("  min %.2f%s  中位 %.2f%s  p90 %.2f%s  max %.2f%s"
              % (vals[0], unit, q(0.5), unit, q(0.9), unit, vals[-1], unit))

    print("\n########## 头肩 ##########")
    dist([x["prom"] for x in hs], "head_prominence 头相对两肩的高出幅度")
    dist([x["sym"] for x in hs], "两肩不对称度")
    dist([x["neck"] for x in hs], "颈线两谷高度差（水平度）")

    print("\n########## 双顶/底对照 ##########")
    dist([x["depth"] for x in dbl], "min_depth 中间谷/峰深度")

    print("\n########## 若收紧 head_prominence 的代价 ##########")
    for t in (0.02, 0.03, 0.04, 0.05, 0.06, 0.08):
        keep = sum(1 for x in hs if x["prom"] >= t)
        print("  >= %.0f%%: 保留 %3d / %d (%.0f%%), 砍掉 %d"
              % (pct(t), keep, len(hs), pct(keep / max(1, len(hs))),
                 len(hs) - keep))

    print("\n########## 若收紧两肩对称性 ##########")
    for t in (0.03, 0.05, 0.08, 0.10):
        keep = sum(1 for x in hs if x["sym"] <= t)
        print("  <= %.0f%%: 保留 %3d / %d (%.0f%%)"
              % (pct(t), keep, len(hs), pct(keep / max(1, len(hs)))))

    os.makedirs(os.path.dirname(args.json), exist_ok=True)
    json.dump({"hs": hs, "double": dbl},
              open(args.json, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n明细 ->", args.json)


if __name__ == "__main__":
    main()
