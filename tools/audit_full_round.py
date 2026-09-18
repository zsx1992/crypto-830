# -*- coding: utf-8 -*-
"""
全周期完整轮回测的审计脚本。

回答三个问题:
  1. 新轮(60标的/含conf)是否完全覆盖旧轮?  -> 决定旧数据能否直接作废
  2. confidence(几何吻合度)是否预测胜负?   -> 决定线上推送排序是否有效
  3. 头肩样本扩到多少了?

用法:
  python tools/audit_full_round.py backtest/result_full_v3.json \
      backtest/result.json backtest/result_15m1d_v2.json
  (第1个=新轮, 其余=旧轮, 用于覆盖校验)
"""
import sys
import json
from collections import defaultdict


def key(x):
    """跨轮匹配键: 同标的+周期+类型+方向+几何(rr) ≈ 同一形态"""
    return (x["symbol"], x["interval"], x["type"], x["dir"], x["rr"])


def main():
    new = json.load(open(sys.argv[1], encoding="utf-8"))["detail"]
    olds = []
    for p in sys.argv[2:]:
        olds += json.load(open(p, encoding="utf-8"))["detail"]
    print("新轮样本: %d, 旧轮合计: %d" % (len(new), len(olds)))

    # ---- 1. 覆盖校验 ----
    kn = {key(x) for x in new}
    hit = sum(1 for x in olds if key(x) in kn)
    print("\n=== 1. 覆盖校验 ===")
    print("旧轮样本被新轮精确匹配: %d/%d (%.0f%%)"
          % (hit, len(olds), 100.0 * hit / max(len(olds), 1)))
    if hit == len(olds):
        print("-> 新轮完全覆盖旧轮, 分析只用新轮数据 (且新轮带 conf/end_ts)")
    else:
        print("-> 有 %d 条旧轮样本不在新轮, 保留合并但注意口径" %
              (len(olds) - hit))

    # ---- 2. conf 是否预测胜负 ----
    print("\n=== 2. confidence 分档 vs 自带TP1胜负 (新轮) ===")
    buckets = defaultdict(lambda: [0, 0])  # 档 -> [n, tp]
    for x in new:
        c = x.get("conf")
        if not c:
            continue
        b = round(c, 1)  # 0.1 一档
        buckets[b][0] += 1
        if x["res"] == "tp":
            buckets[b][1] += 1
    nc = sum(v[0] for v in buckets.values())
    print("(有 conf 的样本: %d/%d)" % (nc, len(new)))
    for b in sorted(buckets):
        n, tp = buckets[b]
        print("  conf %.1f: %3d样  胜率 %3.0f%%" % (b, n, 100.0 * tp / n))
    if nc >= 20:
        lo = [x for x in new if x.get("conf") and x["conf"] < 0.5]
        hi = [x for x in new if x.get("conf") and x["conf"] >= 0.5]
        if lo and hi:
            import math
            wl = sum(1 for x in lo if x["res"] == "tp") / len(lo)
            wh = sum(1 for x in hi if x["res"] == "tp") / len(hi)
            print("  低conf(<0.5) %.0f%% vs 高conf(>=0.5) %.0f%%"
                  % (100 * wl, 100 * wh))
            # 两比例 z 检验: 差距不够显著就明说无区分度, 别给虚假安慰
            pp = (sum(1 for x in lo if x["res"] == "tp")
                  + sum(1 for x in hi if x["res"] == "tp")) / (len(lo) + len(hi))
            se = math.sqrt(pp * (1 - pp) * (1 / len(lo) + 1 / len(hi)))
            z = (wh - wl) / se if se else 0
            verdict = ("高conf更好, 排序有依据" if z >= 1.65 else
                       "无统计学区分度 — 排序依据存疑, 不能拿conf当质量分")
            print("  -> z=%.2f: %s" % (z, verdict))

    # ---- 3. 头肩样本量 ----
    hs = [x for x in new if x["type"].startswith("head_shoulders")]
    print("\n=== 3. 头肩样本 (新轮) ===")
    for t in ("head_shoulders_top", "head_shoulders_bottom"):
        v = [x for x in hs if x["type"] == t]
        if v:
            w = sum(1 for x in v if x["res"] == "tp")
            print("  %-24s %3d样 胜率 %.0f%%" % (t, len(v), 100.0 * w / len(v)))
    print("  总计: %d (旧轮 15)" % len(hs))


if __name__ == "__main__":
    main()
