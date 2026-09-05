# -*- coding: utf-8 -*-
"""
验证「通道质量」指标是否能区分朱哥的「像 / 不像」。

背景（2026-09-03）：
  旧 touch_score = min(1, touches/4) 恒为 1.0 → 几何分与人工判断无关。
  朱哥反馈 C 组（被误伤的 5 张楔形）「起码能看出来是通道」→
  说明他的判据是「边界可辨识 + 价格在通道内运行」，而不是收敛度。

做法：
  1. 临时放宽楔形/三角闸门（复现旧版宽松行为，否则发散楔形现在检不出）
  2. 对每条人工标注，加载本地 CSV、截断、检测、匹配形态
  3. 重算 channel_quality
  4. 对比「像」vs「不像」的分布 + 门槛敏感性

用法:
  python tools/verify_channel_quality.py \
      --manifest output/history_replay/manifest_before_p1wedge.json \
      --annotations output/history_replay/user_annotations.json
"""

import argparse
import collections
import json
import os
import statistics
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from patterns.base import (channel_quality, structure_orderliness,  # noqa: E402
                           PivotType)


def load_klines_csv(path):
    """与 tools/history_replay.py 保持一致的最小实现"""
    import csv as _csv
    from market_data import Kline
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        r = _csv.DictReader(f)
        for row in r:
            try:
                out.append(Kline(
                    openTime=int(row["open_time"]),
                    open=float(row["open"]), high=float(row["high"]),
                    low=float(row["low"]), close=float(row["close"]),
                    volume=float(row.get("volume", 0)),
                    closeTime=int(row.get("close_time", row["open_time"])),
                    quoteVolume=float(row.get("quote_volume", 0)),
                ))
            except Exception:
                continue
    out.sort(key=lambda k: k.openTime)
    return out


def key_of(r):
    return (r.get("symbol"), r.get("interval"),
            r.get("pattern_type"), r.get("end_ms"))


def stat_line(name, arr):
    if not arr:
        print(f"  {name:<18} 无")
        return
    a = sorted(arr)
    print(f"  {name:<16} n={len(a):<4} min={a[0]:.3f}  "
          f"中位={statistics.median(a):.3f}  max={a[-1]:.3f}  "
          f"均值={statistics.mean(a):.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest",
                    default="output/history_replay/manifest_before_p1wedge.json")
    ap.add_argument("--annotations",
                    default="output/history_replay/user_annotations.json")
    ap.add_argument("--interval", default="4h")
    ap.add_argument("--margin", type=int, default=12,
                    help="end_ms 之后再取多少根 K 线（pivot 需右侧确认）")
    args = ap.parse_args()

    man = json.load(open(os.path.join(_ROOT, args.manifest), encoding="utf-8"))
    mrows = man.get("rows", []) if isinstance(man, dict) else man
    ann = json.load(open(os.path.join(_ROOT, args.annotations), encoding="utf-8"))
    judged = [r for r in ann.get("rows", [])
              if r.get("verdict") in ("ok", "bad", "meh")]

    mmap = {os.path.basename(r["image"]): r for r in mrows}

    # 收集需要的形态
    need = []
    for j in judged:
        src = mmap.get(os.path.basename(j.get("chart", "")))
        if not src:
            continue
        need.append((src, j.get("verdict")))
    print(f"标注 {len(judged)} 张，可反查 manifest 的 {len(need)} 张\n")

    # 按 symbol 分组，减少 CSV 重复加载
    bysym = collections.defaultdict(list)
    for src, v in need:
        bysym[src["symbol"]].append((src, v))

    import yaml
    from detector import PatternEngine
    cfg = yaml.safe_load(open(os.path.join(_ROOT, "config.yaml"), encoding="utf-8"))

    # 临时放宽闸门，复现旧版宽松检出（否则发散楔形现在已被收敛闸拦掉）
    from patterns import flag_wedge, triangle
    flag_wedge.WedgeDetector.DEFAULT_PARAMS["converge_min"] = -1.0
    flag_wedge.WedgeDetector.DEFAULT_PARAMS["converge_ratio_min"] = 0.0
    for k in ("converge_ratio_min",):
        if k in triangle.TriangleDetector.DEFAULT_PARAMS:
            triangle.TriangleDetector.DEFAULT_PARAMS[k] = 0.0
    print("[放宽] converge_min=-1.0, converge_ratio_min=0.0 （复现旧版宽松检出）\n")

    engine = PatternEngine(cfg)
    results = []          # (verdict, quality, src, reason)
    matched = 0
    missed = 0

    for sym, items in bysym.items():
        csvp = os.path.join(_ROOT, "data", f"klines_{args.interval}",
                            f"{sym}USDT", f"{sym}USDT.csv")
        if not os.path.exists(csvp):
            print(f"  [跳过] {sym}: 无 CSV {csvp}")
            continue
        klines = load_klines_csv(csvp)
        times = [k.openTime for k in klines]
        import bisect
        for src, v in items:
            eidx = bisect.bisect_left(times, src["end_ms"])
            if eidx >= len(klines):
                missed += 1
                continue
            trunc = klines[:min(len(klines), eidx + args.margin)]
            try:
                found = engine.scan_multiscale(
                    trunc, sym + "USDT", args.interval)
            except Exception:
                missed += 1
                continue
            # 匹配：同类型 + end_ms 最接近
            cand = [p for p in found if p.pattern_type == src["pattern_type"]]
            if not cand:
                missed += 1
                continue
            best = min(cand, key=lambda p: abs(
                trunc[max(q.index for q in p.pivots)].openTime - src["end_ms"]))
            u, lo = best.upper_boundary, best.lower_boundary
            if u is None or lo is None:
                missed += 1
                continue
            s = min(u.p1.index, lo.p1.index)
            e = max(u.p2.index, lo.p2.index)
            q, reason = channel_quality(u, lo, best.pivots, trunc, s, e)
            q2, reason2 = structure_orderliness(
                u, lo, trunc, s, e, best.pattern_type)
            results.append((v, q, src, reason, best, q2, reason2))
            matched += 1
        print(f"  {sym}: 处理 {len(items)} 条")

    print(f"\n匹配成功 {matched} 条，未匹配 {missed} 条\n")

    ok = [q for v, q, *_ in results if v == "ok"]
    bad = [q for v, q, *_ in results if v == "bad"]
    meh = [q for v, q, *_ in results if v == "meh"]

    print("=" * 62)
    print("通道质量 分布（按朱哥判定分组）")
    print("=" * 62)
    stat_line("你判「像」", ok)
    stat_line("你判「不像」", bad)
    stat_line("拿不准", meh)

    if ok and bad:
        d = statistics.mean(ok) - statistics.mean(bad)
        print(f"\n  均值差 = {d:+.3f}  "
              f"（越大越好；旧几何分只有 −0.03，等于无效）")

    # 只看持续形态（楔形/三角）—— 朱哥的 C 组反馈就针对这两类
    cont = [(r[0], r[1], r[2], r[3]) for r in results
            if "wedge" in r[2]["pattern_type"] or "triangle" in r[2]["pattern_type"]]
    if cont:
        cok = [q for v, q, *_ in cont if v == "ok"]
        cbad = [q for v, q, *_ in cont if v == "bad"]
        print("\n" + "=" * 62)
        print("仅持续形态（楔形 / 三角）")
        print("=" * 62)
        stat_line("你判「像」", cok)
        stat_line("你判「不像」", cbad)
        if cok and cbad:
            print(f"\n  均值差 = {statistics.mean(cok) - statistics.mean(cbad):+.3f}")

        print("\n  逐张（持续形态 · 按通道质量降序）:")
        for v, q, s, r in sorted(cont, key=lambda x: -x[1])[:40]:
            flag = "★像" if v == "ok" else ("不像" if v == "bad" else "一般")
            print(f"    {q:.3f}  {flag}  {s['symbol']:<7}"
                  f"{s['pattern_type']:<22}{s.get('end_date')}  {r}")

    # ============ 新指标：结构秩序感（独立样本）============
    print("\n\n" + "=" * 66)
    print("★ 新指标：structure_orderliness（用未参与拟合的密集极值点）")
    print("=" * 66)
    ok2 = [r[5] for r in results if r[0] == "ok"]
    bad2 = [r[5] for r in results if r[0] == "bad"]
    meh2 = [r[5] for r in results if r[0] == "meh"]
    stat_line("你判「像」", ok2)
    stat_line("你判「不像」", bad2)
    stat_line("拿不准", meh2)
    if ok2 and bad2:
        d2 = statistics.mean(ok2) - statistics.mean(bad2)
        print(f"\n  均值差 = {d2:+.3f}   "
              f"（对比：旧几何分 −0.03 / 通道质量 −0.03）")
        if d2 > 0.10:
            print("  → 有区分度！可以作为筛选器")
        elif d2 > 0.03:
            print("  → 弱区分度，需结合其他特征")
        else:
            print("  → 仍然无区分度")

    print("\n  门槛敏感性（秩序感硬闸门）")
    print(f"  {'门槛':>6}{'保留像':>8}{'保留不像':>10}{'精确率':>9}")
    base_ok = sum(1 for r in results if r[0] == "ok")
    base_bad = sum(1 for r in results if r[0] == "bad")
    for th in (0.0, 0.30, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70):
        o = sum(1 for r in results if r[0] == "ok" and r[5] >= th)
        b = sum(1 for r in results if r[0] == "bad" and r[5] >= th)
        print(f"  {th:>6.2f}{o:>8}{b:>10}{o/max(o+b,1):>9.1%}"
              f"   (基线 {base_ok}/{base_ok+base_bad} "
              f"= {base_ok/max(base_ok+base_bad,1):.1%})")

    print("\n  逐张（按秩序感降序，前 30）:")
    for r in sorted(results, key=lambda x: -x[5])[:30]:
        v, src, q2, rsn2 = r[0], r[2], r[5], r[6]
        flag = "★像" if v == "ok" else ("不像" if v == "bad" else "一般")
        print(f"    {q2:.3f}  {flag}  {src['symbol']:<7}"
              f"{src['pattern_type']:<22}{src.get('end_date')}  {rsn2}")

    print("\n" + "=" * 62)
    print("门槛敏感性（若加通道质量硬闸门）")
    print("=" * 62)
    base_ok = sum(1 for v, *_ in results if v == "ok")
    base_bad = sum(1 for v, *_ in results if v == "bad")
    for th in (0.0, 0.30, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65):
        o = sum(1 for v, q, *_ in results if v == "ok" and q >= th)
        b = sum(1 for v, q, *_ in results if v == "bad" and q >= th)
        print(f"  {th:>6.2f}{o:>8}{b:>10}{o/max(o+b,1):>9.1%}"
              f"   (基线 {base_ok}/{base_ok+base_bad} "
              f"= {base_ok/max(base_ok+base_bad,1):.1%})")


if __name__ == "__main__":
    main()
