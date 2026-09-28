"""回测结论稳定性检验 — 判断一个"达标"的结论是真的稳, 还是窗口幸运值。

动机 (2026-09-28 复盘):
  规则3 (LONG 逆 1d 趋势 -> 观察流) 上线依据是 z=+2.56 (09-22 回测)。
  一周后重跑同口径 z 掉到 +1.83, 跌破门槛。查下去发现两个陷阱:

  陷阱1 — "重跑回测"不产生独立验证。
    三次回测的样本池按 (symbol,interval,type) 算重叠高达 67-71%, 是同一批
    币种/形态在不同时间窗口的重复观测; 按带 end_ts 的严格 key 算只重叠 6%
    (窗口一移, 同一形态的端点就变)。结论: 重跑多少次都不等于多次独立验证。

  陷阱2 — 小样本下结论对 ±几条样本的胜负极度敏感。
    逆势组 27 条, 实际 TP=9 (33%) 时 z=+1.83; 若 TP=5 z=+3.24, TP=13 z=+0.43。
    4 条样本的差别就能让结论在"远超门槛"和"完全不显著"之间横跳。

  用法: python tools/check_stability.py
  输出: 样本重叠度 / 各次 z 汇总 / 逆势组敏感性表
"""
import json
import math
import os
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 按时间顺序归档的回测快照（新跑完回测后手动追加到这里）
SNAPSHOTS = [
    ("09-20", "backtest/result_ctx_0920.json"),
    ("09-22", "backtest/result_ctx_0922.json"),
    ("09-28", "backtest/result_ctx_0928.json"),
]

# 被检验的规则: 只看 LONG 信号, 按 1d(hctx20) 趋势分顺/逆
SCOPE_DIR = "LONG"
OPPOSITE = "down"   # LONG 的逆势 = 1d down


def tp(rows):
    return sum(1 for x in rows if x["res"] == "tp")


def zstat(w1, n1, w2, n2):
    """两比例 z 检验 (顺 vs 逆)。"""
    if min(n1, n2) == 0:
        return 0.0
    p1, p2 = w1 / n1, w2 / n2
    p = (w1 + w2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return (p1 - p2) / se if se else 0.0


def load(path):
    with open(os.path.join(BASE, path), encoding="utf-8") as f:
        return json.load(f)["detail"]


def keys(det, use_ts=True):
    if use_ts:
        return {(x["symbol"], x["interval"], x["type"], x.get("end_ts")) for x in det}
    return {(x["symbol"], x["interval"], x["type"]) for x in det}


def main():
    sets = [(lab, load(p)) for lab, p in SNAPSHOTS if os.path.exists(os.path.join(BASE, p))]

    print("### 1) 样本重叠度 —— 重跑回测到底有没有独立验证价值? ###")
    print("     (严格 key = symbol+interval+type+end_ts; 宽松 key 忽略 end_ts)")
    for i in range(len(sets) - 1):
        (la, a), (lb, b) = sets[i], sets[i + 1]
        for tight in (True, False):
            k1, k2 = keys(a, tight), keys(b, tight)
            tag = "严格" if tight else "宽松"
            print("  %s %s->%s : %3d vs %3d 条, 相同 %3d (新样本的 %.0f%%)" % (
                tag, la, lb, len(k1), len(k2), len(k1 & k2), 100 * len(k1 & k2) / len(k2)))
    print()

    print("### 2) 各次 z 值 (LONG 逆 1d 趋势) ###")
    for lab, det in sets:
        for scope in ["4h", "ALL"]:
            L = [x for x in det if x.get("hctx20") and x["dir"] == SCOPE_DIR
                 and (scope == "ALL" or x["interval"] == scope)]
            wt = [x for x in L if x["hctx20"] != OPPOSITE]
            ct = [x for x in L if x["hctx20"] == OPPOSITE]
            n1, n2 = len(wt), len(ct)
            print("  %s %-4s 顺 %3d/%3d=%5.1f%%  逆 %2d/%2d=%5.1f%%  z=%+.2f" % (
                lab, scope, tp(wt), n1, 100 * tp(wt) / n1 if n1 else 0,
                tp(ct), n2, 100 * tp(ct) / n2 if n2 else 0,
                zstat(tp(wt), n1, tp(ct), n2)))
    print()

    lab, det = sets[-1]
    L = [x for x in det if x.get("hctx20") and x["dir"] == SCOPE_DIR]
    wt = [x for x in L if x["hctx20"] != OPPOSITE]
    ct = [x for x in L if x["hctx20"] == OPPOSITE]
    n1, w1, n2, w2 = len(wt), tp(wt), len(ct), tp(ct)
    base = zstat(w1, n1, w2, n2)

    print("### 3) 敏感性: 逆势组(%d条) TP 数每变 1 条, z 变多少? ###" % n2)
    print("  实际: 顺 %d/%d=%.1f%%  逆 %d/%d=%.1f%%  z=%+.2f" % (
        w1, n1, 100 * w1 / n1, w2, n2, 100 * w2 / n2, base))
    for tw in range(max(0, w2 - 4), min(n2, w2 + 4) + 1):
        z = zstat(w1, n1, tw, n2)
        mark = ""
        if abs(z) >= 2 > abs(base):
            mark = "  <- 跨过门槛"
        elif abs(z) < 2 <= abs(base):
            mark = "  <- 跌破门槛"
        print("    TP=%2d (%4.1f%%)  z=%+.2f%s" % (tw, 100 * tw / n2, z, mark))
    print()

    print("### 4) 各次逆势组构成 ###")
    for lab, det in sets:
        ct = [x for x in det if x.get("hctx20") and x["dir"] == SCOPE_DIR and x["hctx20"] == OPPOSITE]
        print("  %s n=%2d 周期=%s" % (lab, len(ct), dict(Counter(x["interval"] for x in ct))))
    print()
    print("判读: z 在门槛线上下浮动 + 重叠度高 => 结论是窗口幸运值, 不应据此上/撤规则;")
    print("      真正的独立验证只能来自实盘样本累积 (tools/eval_demoted.py 的周度复盘)。")


if __name__ == "__main__":
    main()
