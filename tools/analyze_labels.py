# -*- coding: utf-8 -*-
"""
分析金标准标注结果，输出"砍哪些能提质量"的量化建议。

输入: golden_labels.json（标注页导出）
      charts_golden/manifest.json（形态元信息）
输出: 各维度交叉表 + 若干"若砍掉 X，瞎画率降到多少"的推演

用法: python tools/analyze_labels.py golden_labels.json
"""
import os
import sys
import json
import argparse
from collections import Counter, defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NAME = {"good": "像", "ok": "勉强", "bad": "瞎画"}


def pct(a, b):
    return 0.0 if not b else 100.0 * a / b


def line(label, c, n):
    g, o, b = c.get("good", 0), c.get("ok", 0), c.get("bad", 0)
    return "  %-24s %4d  像%3d(%3.0f%%)  勉强%3d(%3.0f%%)  瞎画%3d(%3.0f%%)" % (
        label, n, g, pct(g, n), o, pct(o, n), b, pct(b, n))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("labels")
    ap.add_argument("--manifest", default="charts_golden/manifest.json")
    args = ap.parse_args()

    lab = json.load(open(args.labels, encoding="utf-8"))
    labels = lab.get("labels", lab)

    man = {}
    mp = os.path.join(_ROOT, args.manifest)
    if os.path.isfile(mp):
        for i in json.load(open(mp, encoding="utf-8")).get("items", []):
            man[i["id"]] = i

    rows = []
    for k, v in labels.items():
        m = man.get(k, {})
        rows.append({
            "id": k, "label": v,
            "symbol": m.get("symbol", "?"),
            "interval": m.get("interval", "?"),
            "type": m.get("patternType", "?"),
            "direction": m.get("direction", "?"),
            "strength": m.get("strength", 0),
            "cat": "channel" if "channel" in (m.get("patternType") or "")
                   else "classic",
        })

    n = len(rows)
    if not n:
        print("没有标注数据")
        return

    tot = Counter(r["label"] for r in rows)
    print("=== 总览 (%d 条已标注) ===" % n)
    for v in ("good", "ok", "bad"):
        print("  %-4s %3d  %5.1f%%  %s" % (
            NAME[v], tot.get(v, 0), pct(tot.get(v, 0), n),
            "#" * int(pct(tot.get(v, 0), n) / 2)))
    print("  瞎画+勉强 = %.1f%%" % pct(tot.get("bad", 0) + tot.get("ok", 0), n))

    def by(field):
        d = defaultdict(Counter)
        c = Counter()
        for r in rows:
            d[r[field]][r["label"]] += 1
            c[r[field]] += 1
        return d, c

    for field, title in (("type", "按形态类型"), ("interval", "按周期"),
                         ("cat", "按形态大类"), ("direction", "按方向")):
        d, c = by(field)
        print("\n=== %s ===" % title)
        for k in sorted(c, key=lambda x: -pct(d[x].get("bad", 0), c[x])):
            print(line(k, d[k], c[k]))

    print("\n=== 按强度分档 ===")
    d, c = defaultdict(Counter), Counter()
    for r in rows:
        s = r["strength"] or 0
        b = "60-64" if s < 65 else "65-74" if s < 75 else "75+"
        d[b][r["label"]] += 1
        c[b] += 1
    for k in ("60-64", "65-74", "75+"):
        if c[k]:
            print(line(k, d[k], c[k]))

    # ---- 推演：砍掉某个维度后，瞎画率降到多少 ----
    print("\n=== 推演：砍掉 X 之后整体瞎画率 ===")
    base_bad = tot.get("bad", 0)
    print("  %-26s %5.1f%%  (基准, %d/%d)" % (
        "什么都不砍", pct(base_bad, n), base_bad, n))

    cands = []
    for field, title in (("cat", "形态大类"), ("interval", "周期"),
                         ("type", "形态类型")):
        d, c = by(field)
        for k in sorted(c):
            if c[k] < 5:
                continue
            nb = base_bad - d[k].get("bad", 0)
            nn = n - c[k]
            if nn <= 0:
                continue
            cands.append((pct(nb, nn), title, k, c[k], nb, nn))
    cands.sort()
    for rate, title, k, ck, nb, nn in cands[:10]:
        print("  砍 %-10s %-12s 剩%3d条  瞎画率 %5.1f%%  (%d/%d)" % (
            title, k, nn, rate, nb, nn))


if __name__ == "__main__":
    main()
