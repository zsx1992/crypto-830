# -*- coding: utf-8 -*-
"""
合并多轮回测明细，出跨周期对照分析。

背景：
  第 4 轮回测（4h+1h, 去重后 61 样本）唯一显著结论是"通道优于经典形态"
  （1R 胜率 73% vs 44%, z=2.19, p≈0.028）。但头肩去重后仅 11 个、
  1d/15m 两个周期完全没覆盖——本脚本合并 15m+1d 扩样本结果后统一重算。

用法:
  python tools/merge_backtest.py backtest/result.json backtest/result_15m_1d.json
"""
import sys
import json
import math
from collections import defaultdict, Counter

R_TARGETS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
IV_ORDER = {"15m": 0, "1h": 1, "4h": 2, "1d": 3}


def load(paths):
    det = []
    for p in paths:
        d = json.load(open(p, encoding="utf-8"))
        det.extend(d.get("detail", []))
        print("载入 %s: %d 条" % (p, len(d.get("detail", []))))
    return det


def cat(t):
    # 与第 4 轮报告口径一致: 通道+矩形同属 box 类（兜底桶）
    return "box" if ("channel" in t or "rectangle" in t) else "classic"


def fmt_cell(v):
    return "%+5.2f" % v if v is not None else "    -"


def ztest(w1, n1, w2, n2):
    """两比例 z 检验（胜率差）。返回 (z, p)。"""
    if n1 == 0 or n2 == 0:
        return None, None
    p1, p2 = w1 / n1, w2 / n2
    pp = (w1 + w2) / (n1 + n2)
    se = math.sqrt(pp * (1 - pp) * (1 / n1 + 1 / n2))
    if se == 0:
        return None, None
    z = (p1 - p2) / se
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    return z, p


def main():
    det = load(sys.argv[1:])
    print("\n合并后样本: %d" % len(det))
    print("按周期:", dict(Counter(x["interval"] for x in det)))
    print("按大类:", dict(Counter(cat(x["type"]) for x in det)))

    # ---- 1. 1.0R 目标胜率（与第 4 轮报告口径一致, 可直接对比）----
    print("\n=== 1.0R 目标胜率（TP1 设在 1.0×风险处）: 大类 × 周期 ===")
    print("%-6s %-8s %5s %6s %7s" % ("周期", "大类", "样本", "胜率", "期望"))
    grp = defaultdict(list)
    for x in det:
        grp[(x["interval"], cat(x["type"]))].append(x)

    def wr_1r(v):
        rs = [x["multi"].get("1.0") for x in v]
        rs = [r for r in rs if r]
        if not rs:
            return None, None
        w = sum(1 for r in rs if r == "tp") / len(rs)
        return w, w - (1 - w)

    for iv in sorted({k[0] for k in grp}, key=lambda v: IV_ORDER.get(v, 9)):
        for c in ("box", "classic"):
            v = grp.get((iv, c), [])
            if not v:
                continue
            w, e = wr_1r(v)
            if w is None:
                continue
            print("%-6s %-8s %5d %5.0f%% %+6.2f"
                  % (iv, c, len(v), 100.0 * w, e))

    # 大类合计 + 检验
    for c in ("box", "classic"):
        v = [x for x in det if cat(x["type"]) == c]
        w, e = wr_1r(v)
        if w is not None:
            print("%-6s %-8s %5d %5.0f%% %+6.2f"
                  % ("合计", c, len(v), 100.0 * w, e))
    bx = [x for x in det if cat(x["type"]) == "box"]
    cl = [x for x in det if cat(x["type"]) == "classic"]
    wb, _ = wr_1r(bx)
    wc, _ = wr_1r(cl)
    z, p = ztest(round(wb * len(bx)), len(bx),
                 round(wc * len(cl)), len(cl))
    if z is not None:
        print("box类 vs 经典: z=%.2f, p=%.3f" % (z, p))

    # ---- 2. 目标距离扫描: 大类 × 周期 ----
    print("\n=== 期望扫描（TP1 设 N×R）===")
    print("  %-14s %5s  %s" % ("周期/大类", "样本",
                               "  ".join("%5.2fR" % R for R in R_TARGETS)))
    grp2 = defaultdict(list)
    for x in det:
        grp2[("全部", cat(x["type"]))].append(x)
        grp2[(x["interval"], cat(x["type"]))].append(x)
        grp2[(x["interval"], "all")].append(x)
    rows = sorted(grp2.items(),
                  key=lambda kv: (kv[0][0] != "全部",
                                  IV_ORDER.get(kv[0][0], 9), kv[0][1]))
    for (iv, c), v in rows:
        if len(v) < 5:
            continue
        cells = []
        for R in R_TARGETS:
            rs = [x["multi"].get(str(R)) for x in v]
            rs = [r for r in rs if r]
            if not rs:
                cells.append("    -")
                continue
            w = sum(1 for r in rs if r == "tp") / len(rs)
            cells.append("%+5.2f" % (w * R - (1 - w)))
        print("  %-14s %5d  %s" % ("%s/%s" % (iv, c), len(v),
                                   "  ".join(cells)))

    # ---- 3. 分形态明细（合并后样本>=8 才列出）----
    print("\n=== 分形态（样本>=8）===")
    print("%-6s %-22s %5s %6s %7s" % ("周期", "形态", "样本", "胜率", "期望"))
    bytype = defaultdict(list)
    for x in det:
        bytype[(x["interval"], x["type"])].append(x)
    for (iv, t), v in sorted(bytype.items(),
                             key=lambda kv: (-len(kv[1]), kv[0])):
        n = len(v)
        if n < 8:
            continue
        w = sum(1 for x in v if x["res"] == "tp")
        print("%-6s %-22s %5d %5.0f%% %+6.2f"
              % (iv, t, n, 100.0 * w / n, w / n - (n - w) / n))

    # ---- 4. 头肩专项：扩样后能否下结论 ----
    hs = [x for x in det if x["type"].startswith("head_shoulders")]
    print("\n=== 头肩专项 ===")
    print("头肩总样本: %d" % len(hs))
    if len(hs) >= 10:
        for t in ("head_shoulders_top", "head_shoulders_bottom"):
            v = [x for x in hs if x["type"] == t]
            if v:
                w = sum(1 for x in v if x["res"] == "tp")
                print("  %-22s %3d 胜率 %.0f%%"
                      % (t, len(v), 100.0 * w / len(v)))
    else:
        print("  样本仍不足 10，结论继续挂机等数据")


if __name__ == "__main__":
    main()
