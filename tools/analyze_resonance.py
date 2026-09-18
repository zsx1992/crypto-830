# -*- coding: utf-8 -*-
"""
大小周期共振分析：顺势 vs 逆势 的胜率对比 + 多口径稳健性检验。

hctx 定义：低周期形态结束时，高周期收盘价相对 n 根前的方向
（n=10/20/50 三个口径；hctx20 是主口径，10/50 用于稳健性检验）。

顺势 = LONG 且高周期 up，或 SHORT 且高周期 down；反之逆势。

用法:
  python tools/analyze_resonance.py backtest/result_full_v5.json
"""
import sys
import json
import math
from collections import defaultdict


def wr(v):
    """1.0R 目标：返回 (胜率, 样本数)"""
    rs = [x["multi"].get("1.0") for x in v]
    rs = [r for r in rs if r]
    if not rs:
        return None, 0
    w = sum(1 for r in rs if r == "tp") / len(rs)
    return w, len(rs)


def ztest(w1, n1, w2, n2):
    if not n1 or not n2:
        return None
    pp = (round(w1 * n1) + round(w2 * n2)) / (n1 + n2)
    se = math.sqrt(pp * (1 - pp) * (1 / n1 + 1 / n2))
    return (w1 - w2) / se if se else 0.0


def split(samples, key):
    """按 key 字段分顺势/逆势"""
    a, r = [], []
    for x in samples:
        hc = x.get(key)
        if hc is None:
            continue
        same = (x["dir"] == "LONG") == (hc == "up")
        (a if same else r).append(x)
    return a, r


def report(samples, key, label):
    a, r = split(samples, key)
    wa, na = wr(a)
    wrev, nr = wr(r)
    line = "%-10s 顺势: " % label
    line += ("%3d样 %3.0f%%" % (na, 100 * wa)) if na else " 0样"
    line += " | 逆势: "
    line += ("%3d样 %3.0f%%" % (nr, 100 * wrev)) if nr else " 0样"
    z = ztest(wa or 0, na, wrev or 0, nr) if (na and nr) else None
    if z is not None:
        line += "  (z=%+.2f%s)" % (z, " *" if abs(z) >= 1.65 else "")
    print(line)
    return na, nr, z


def main():
    det = json.load(open(sys.argv[1], encoding="utf-8"))["detail"]
    cat = lambda t: "box" if ("channel" in t or "rectangle" in t) else "classic"
    print("样本总数: %d" % len(det))
    # 兼容旧字段名: v4 只有 hctx(=20根口径)
    main_key = "hctx20"
    if det and "hctx20" not in det[0] and "hctx" in det[0]:
        main_key = "hctx"
        print("(旧数据无三口径, 用 hctx 作为主口径)")
    for k in ("hctx10", "hctx20", "hctx50"):
        have = sum(1 for x in det if x.get(k))
        print("  %s 有值: %d" % (k, have))

    # 1. 三个口径的稳健性（全部形态）
    print("\n=== 口径稳健性（全部形态, 顺势/逆势 1.0R 胜率）===")
    counts = {}
    for k in ("hctx10", "hctx20", "hctx50"):
        counts[k] = report(det, k, k)
    valid = [c for c in counts.values() if c[2] is not None]
    if valid and all(c[2] > 0 for c in valid):
        print("  -> 三个口径方向一致: 逆势效应稳健")
    elif valid and any(c[2] > 0 for c in valid):
        print("  -> 口径间方向不一致: 效应可能是参数噪声")
    else:
        print("  -> 均不显著")

    # 2. 主口径 分大类
    print("\n=== 主口径 %s 分大类 ===" % main_key)
    for c in ("box", "classic"):
        v = [x for x in det if cat(x["type"]) == c]
        report(v, main_key, c)

    # 3. 主口径按低周期细分
    print("\n=== 主口径 %s 按低周期 ===" % main_key)
    for iv in ("1h", "15m"):
        v = [x for x in det if x["interval"] == iv]
        report(v, main_key, iv)

    # 4. 逆势样本池（供跨轮累计监控）
    a, r = split(det, main_key)
    print("\n逆势样本池: %d 个 (目标: 30+ 再下结论)" % len(r))
    if r:
        by = defaultdict(int)
        for x in r:
            by[(x["interval"], cat(x["type"]))] += 1
        for k in sorted(by):
            print("   %s %s: %d" % (k[0], k[1], by[k]))


if __name__ == "__main__":
    main()
