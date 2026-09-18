# -*- coding: utf-8 -*-
"""
诊断: 形态的"窗口滑动稳定性" —— 量化"瞎画"的通用指标。

背景 (2026-09-18):
  复盘渲染 68 条推送时发现, 7 天后重跑检测器, **找不到当初那个形态**的比例:
    通道 25 条中 8 条 = 32%
    经典形态 43 条中 3 条 = 7%
  怀疑根因: 通道的两条斜线是"拟合"出来的 —— 边界画在哪全看喂进去的数据窗口,
  没有真正的几何锚点支撑。经典形态(双顶/头肩)锚在真实的峰谷上, 所以稳定。

本脚本把这个猜想做成可量化指标:
  保持窗口长度不变, 把窗口起点向后滑动 k 根(模拟"时间推进了 k 根"),
  看基准窗口检出的形态, 滑动后**是否还在、且位置是否只是平移**。

  真形态  → 对窗口微扰稳定(多一根少一根 K 线, 形态还在)
  拟合物  → 剧烈变形/消失(线跟着窗口跑)

判据: 同 type + 同 direction, 且形态末端索引偏差 <= k + tol 视为"同一个"。

用法: python tools/diag_window_stability.py [--intervals 1h,4h] [--slide 5]
"""
import os
import sys
import json
import argparse
from collections import Counter, defaultdict

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


def sig(p):
    """形态签名：(类型, 方向)"""
    return (p.pattern_type,
            str(p.direction).replace("Direction.", ""))


def end_idx(p):
    if not p.pivots:
        return None
    return max(v.index for v in p.pivots)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intervals", default="1h,4h")
    ap.add_argument("--slide", type=int, default=5, help="向后滑动几根")
    ap.add_argument("--tol", type=int, default=2, help="末端索引容差")
    ap.add_argument("--max-symbols", type=int, default=60)
    ap.add_argument("--json", default="output/diag_window_stability.json")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(os.path.join(_ROOT, "config.yaml"),
                              encoding="utf-8"))
    eng = PatternEngine(cfg)

    # 按形态类别聚合：滑动 k 根后仍匹配的数量 / 基准数量
    stat = defaultdict(lambda: {"base": 0, "hit": Counter()})
    detail = []

    for iv in [s.strip() for s in args.intervals.split(",")]:
        d = os.path.join(_ROOT, "data", "klines_%s" % iv)
        if not os.path.isdir(d):
            continue
        syms = sorted(os.listdir(d))[:args.max_symbols]
        n_sym = 0
        for s in syms:
            f = os.path.join(d, s, "%s.csv" % s)
            if not os.path.isfile(f):
                continue
            try:
                ks = load_csv(f)
            except Exception:
                continue
            if len(ks) < 200:
                continue

            # 基准窗口：最后 len 根；滑动窗口：起点前移 k 根（长度不变）
            N = len(ks)
            try:
                base = eng.scan_multiscale(ks, symbol=s, interval=iv)
            except Exception:
                continue
            if not base:
                continue
            n_sym += 1

            # 预跑各滑动窗口
            shifted = {}
            for k in range(1, args.slide + 1):
                sub = ks[:N - k]          # 砍掉末尾 k 根 = 时间倒退 k 根
                if len(sub) < 150:
                    continue
                try:
                    shifted[k] = eng.scan_multiscale(sub, symbol=s,
                                                     interval=iv)
                except Exception:
                    shifted[k] = []

            for p in base:
                cat = "channel" if "channel" in p.pattern_type else "classic"
                e0 = end_idx(p)
                if e0 is None:
                    continue
                stat[cat]["base"] += 1
                stat[(cat, p.pattern_type)]["base"] += 1
                for k, pats in shifted.items():
                    ok = False
                    for q in pats:
                        if sig(q) != sig(p):
                            continue
                        e1 = end_idx(q)
                        if e1 is None:
                            continue
                        # 窗口砍掉末尾 k 根后, 同一形态末端索引应【几乎不变】
                        # (因为形态在窗口内部, 不受末尾影响)
                        if abs(e1 - e0) <= args.tol:
                            ok = True
                            break
                    if ok:
                        stat[cat]["hit"][k] += 1
                        stat[(cat, p.pattern_type)]["hit"][k] += 1
                detail.append({
                    "symbol": s, "interval": iv, "type": p.pattern_type,
                    "cat": cat, "end": e0,
                    "hits": [k for k in shifted
                             if any(sig(q) == sig(p) and
                                    end_idx(q) is not None and
                                    abs(end_idx(q) - e0) <= args.tol
                                    for q in shifted[k])],
                })
        print("  %-4s 有形态的标的 %d 个" % (iv, n_sym))

    print("\n=== 窗口滑动稳定性（滑动 k 根后形态仍在的比例）===")
    print("%-10s %6s  %s" % ("类别", "基准数",
                             "  ".join("k=%d" % k
                                       for k in range(1, args.slide + 1))))
    for cat in ("classic", "channel"):
        if cat not in stat or not stat[cat]["base"]:
            continue
        b = stat[cat]["base"]
        row = []
        for k in range(1, args.slide + 1):
            c = stat[cat]["hit"][k]
            row.append("%4.0f%%" % (100.0 * c / b))
        print("%-10s %6d  %s" % (cat, b, "  ".join(row)))

    print("\n=== 细分到形态类型 ===")
    keys = [k for k in stat if isinstance(k, tuple)]
    for (cat, t) in sorted(keys, key=lambda x: -stat[x]["base"]):
        b = stat[(cat, t)]["base"]
        if b < 3:
            continue
        row = ["%4.0f%%" % (100.0 * stat[(cat, t)]["hit"][k] / b)
               for k in range(1, args.slide + 1)]
        print("  %-22s %-8s %4d  %s" % (t, cat, b, "  ".join(row)))

    os.makedirs(os.path.dirname(args.json), exist_ok=True)
    json.dump({"detail": detail}, open(args.json, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n明细 ->", args.json)


if __name__ == "__main__":
    main()
