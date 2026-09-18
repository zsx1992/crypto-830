# -*- coding: utf-8 -*-
"""
形态胜率回测：把"质量"从主观的"像不像"，推进到客观的"有没有用"。

为什么要在云端跑:
  本机缓存长度 **正好等于一次扫描的窗口**（15m=600 / 1h=400 / 4h=300 / 1d=240），
  没有"窗口之后"的数据，无法验证形态后续是否走对。云端可以从 OKX 拉长历史
  （get_klines 已支持分页 + end_time_ms），才能滑动窗口回测。

方法:
  1. 拉 history 根历史 K 线
  2. 用正常 kline_counts 长度的窗口，按 step 滑动（模拟一次次历史扫描）
  3. 每次扫描取 CONFIRMED 形态，记录 entry / stop / tp1
  4. 在窗口【之后】的 hold 根 K 线里，看先触及 tp1 还是 stop
     （同一根内两者都触及 → 保守判为止损）
  5. 按形态类型 / 周期统计胜率、止损率、超时率、平均盈亏比

用法（云端）:
  python tools/backtest_patterns.py --intervals 4h,1h --history 2000 --symbols 25

本机冒烟（用缓存，窗口调小以留出验证区间）:
  python tools/backtest_patterns.py --cache-dir data --intervals 4h \
      --window 200 --hold 80 --step 20 --symbols 3
"""
import os
import sys
import json
import argparse
import datetime
from collections import Counter, defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

import yaml  # noqa: E402
from market_data import Kline, OkxClient  # noqa: E402
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


# 首轮回测发现"均R 越高、胜率越低"，说明问题可能不在形态识别，而在 TP1 设太远。
# 所以一次扫描同时评估多个目标距离，直接看出最优档位。
R_TARGETS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]


def prominence(p):
    """
    形态显著性：头肩 = 头相对两肩均值的高出幅度；双顶/底 = 中间谷/峰深度。
    其余形态返回 None。

    动机：全池诊断发现头肩 prominence 中位 6.8%，而双顶/底 depth 中位 11.3%
    —— 头肩的"形态显著性"只有双顶的一半，可能是它回测最差的原因。
    """
    pv = sorted(p.pivots or [], key=lambda v: v.index)
    if len(pv) < 3:
        return None
    t = p.pattern_type
    if t.startswith("head_shoulders") and len(pv) >= 5:
        mid = sorted(v.price for v in pv)[len(pv) // 2]
        tops = [v for v in pv if v.price >= mid]
        if len(tops) != 3:
            return None
        head = max(tops, key=lambda v: v.price) if t.endswith("top") \
            else min(tops, key=lambda v: v.price)
        sh = [v for v in tops if v is not head]
        m = (sh[0].price + sh[1].price) / 2.0
        return abs(head.price - m) / abs(m) if m else None
    if t.startswith("double") and len(pv) >= 3:
        a, b, c = pv[0], pv[1], pv[2]
        m = (a.price + c.price) / 2.0
        return abs(m - b.price) / abs(m) if m else None
    return None


def judge(p, future):
    """判定一段形态的后续结果：'tp' 达标 / 'sl' 止损 / 'none' 超时"""
    r = judge_multi(p, future)
    return (r[100], r[100])


def judge_multi(p, future):
    """
    同时评估多个目标距离（单位 R = 风险倍数）。
    返回 {R: 'tp'|'sl'|'none'}；key=100 表示用形态自带的 tp1。
    """
    entry = getattr(p, "entry_price", 0) or 0
    sl = getattr(p, "stop_loss", 0) or 0
    if not (entry > 0 and sl > 0):
        return {R: None for R in R_TARGETS + [100]}
    long = str(p.direction).replace("Direction.", "") == "LONG"
    risk = abs(entry - sl)
    if risk <= 0:
        return {R: None for R in R_TARGETS + [100]}

    tp1 = getattr(p, "take_profit_1", 0) or 0
    own_r = abs(tp1 - entry) / risk if tp1 > 0 else None

    out = {R: "none" for R in R_TARGETS}
    out[100] = "none"

    for k in future:
        adv = (entry - k.low) / risk if long else (k.high - entry) / risk
        fav = (k.high - entry) / risk if long else (entry - k.low) / risk
        hit_sl = adv >= 1.0
        for R in R_TARGETS:
            if out[R] == "none" and fav >= R:
                out[R] = "sl" if hit_sl else "tp"
        if own_r is not None and out[100] == "none" and fav >= own_r:
            out[100] = "sl" if hit_sl else "tp"
        if hit_sl:
            # 已止损：所有尚未达标的目标都判负
            for R in R_TARGETS:
                if out[R] == "none":
                    out[R] = "sl"
            if out[100] == "none":
                out[100] = "sl"
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intervals", default="4h,1h")
    ap.add_argument("--history", type=int, default=2000)
    ap.add_argument("--window", type=int, default=0, help="0=用配置的 kline_counts")
    ap.add_argument("--hold", type=int, default=100, help="最多持有多少根看结果")
    ap.add_argument("--step", type=int, default=40, help="滑动步长")
    ap.add_argument("--symbols", type=int, default=25)
    ap.add_argument("--min-n", type=int, default=5,
                    help="样本数低于此值不打印（CONFIRMED 很稀有）")
    ap.add_argument("--cache-dir", default="", help="离线模式: 读本地缓存")
    ap.add_argument("--json", default="output/backtest.json")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(os.path.join(_ROOT, "config.yaml"),
                              encoding="utf-8"))
    kc = cfg.get("data", {}).get("kline_counts", {}) or \
        cfg.get("kline_counts", {})
    eng = PatternEngine(cfg)
    client = None if args.cache_dir else OkxClient(timeout=30)

    stat = defaultdict(Counter)
    rr = defaultdict(list)
    detail = []

    for iv in [s.strip() for s in args.intervals.split(",")]:
        W = args.window or int(kc.get(iv, 300))
        if args.cache_dir:
            d = os.path.join(args.cache_dir, "klines_%s" % iv)
            syms = sorted(os.listdir(d))[:args.symbols] \
                if os.path.isdir(d) else []
        else:
            # OkxClient.get_top_symbols(top_n, min_volume_usdt) -> List[str]
            try:
                syms = client.get_top_symbols(top_n=args.symbols)
            except Exception as e:
                print("取标的列表失败:", str(e)[:80])
                syms = []

        n_sym = 0
        for s in syms:
            if args.cache_dir:
                ks = load_csv(os.path.join(args.cache_dir, "klines_%s" % iv,
                                           s, "%s.csv" % s))
            else:
                try:
                    ks = client.get_klines(s, iv, args.history)
                except Exception as e:
                    continue
            if len(ks) < W + args.hold + 20:
                continue
            n_sym += 1
            seen = {}      # 去重：(type, 绝对末端位置) -> 已计入
            starts = range(0, len(ks) - W - args.hold, args.step)
            for st in starts:
                win = ks[st:st + W]
                fut = ks[st + W:st + W + args.hold]
                try:
                    pats = eng.scan_multiscale(win, symbol=s, interval=iv)
                except Exception:
                    continue
                for p in pats:
                    if str(p.status).replace("PatternStatus.", "") \
                            != "CONFIRMED":
                        continue
                    # 【去重】相邻滑动窗口高度重叠（窗口300根、步长40 → 87%重叠），
                    # 同一形态会被反复检出，导致样本不独立、夸大样本量、低估方差。
                    # 实测：4h 的 HS_bottom 2.7% 竟出现 6 次，实为同一形态。
                    # 判据：同 type 且绝对末端位置相差 <= step+tol 视为同一个。
                    e_abs = st + max((v.index for v in p.pivots),
                                     default=0)
                    dup = False
                    for (tt, ee) in seen:
                        if tt == p.pattern_type and abs(ee - e_abs) <= \
                                args.step + 5:
                            dup = True
                            break
                    if dup:
                        continue
                    seen[(p.pattern_type, e_abs)] = True

                    multi = judge_multi(p, fut)
                    res = multi.get(100)
                    if res is None:
                        continue
                    tp1 = getattr(p, "take_profit_1", 0) or 0
                    ent = getattr(p, "entry_price", 0) or 0
                    slp = getattr(p, "stop_loss", 0) or 0
                    rmult = (abs(tp1 - ent) / abs(ent - slp)
                             if (tp1 > 0 and abs(ent - slp) > 0) else 0)
                    cat = ("channel" if "channel" in p.pattern_type
                           else "classic")
                    for key in ((iv, p.pattern_type), (iv, cat),
                                ("ALL", p.pattern_type), ("ALL", cat)):
                        stat[key][res] += 1
                        if res == "tp":
                            rr[key].append(rmult)
                    detail.append({
                        "symbol": s, "interval": iv, "type": p.pattern_type,
                        "dir": str(p.direction).replace("Direction.", ""),
                        "res": res, "rr": round(rmult, 2),
                        "multi": {str(k): v for k, v in multi.items()
                                  if v is not None},
                        "prom": (round(prominence(p), 4)
                                 if prominence(p) is not None else None),
                        "strength": getattr(p, "score", None),
                    })
        print("  %-4s 回测 %d 个标的 (窗口%d 持有%d 步长%d)"
              % (iv, n_sym, W, args.hold, args.step), flush=True)

    print("\n=== 形态胜率（先触及 TP1 = 胜，先触及止损 = 负）===")
    print("%-6s %-22s %5s %6s %6s %6s %7s"
          % ("周期", "形态", "样本", "胜率", "止损", "超时", "均R"))
    for key in sorted(stat, key=lambda k: -sum(stat[k].values())):
        iv, name = key
        c = stat[key]
        n = sum(c.values())
        if n < args.min_n:
            continue
        avg = (sum(rr[key]) / len(rr[key])) if rr[key] else 0
        print("%-6s %-22s %5d %5.0f%% %5.0f%% %5.0f%% %6.2f"
              % (iv, name, n,
                 100.0 * c["tp"] / n, 100.0 * c["sl"] / n,
                 100.0 * c["none"] / n, avg))

    # ---- 目标距离扫描：同一批样本，TP1 设在不同 R 档位的表现 ----
    print("\n=== 目标距离扫描（TP1 设在 N×R 处的表现）===")
    print("  期望 = 胜率×R − 止损率×1；越大越好")
    print("  %-22s %5s  %s" % ("分组", "样本",
                               "  ".join("%5.2fR" % R for R in R_TARGETS)))
    groups = defaultdict(list)
    for x in detail:
        m = x.get("multi", {})
        if not m:
            continue
        groups[("ALL", "全部")].append(x)
        groups[("ALL", x["type"])].append(x)
        groups[(x["interval"], "全部")].append(x)
        cat = "channel" if "channel" in x["type"] else "classic"
        groups[("ALL", cat)].append(x)
    for (iv, name) in sorted(groups, key=lambda k: (k[0] != "ALL", k[0], k[1])):
        v = groups[(iv, name)]
        if len(v) < args.min_n or name == "全部":
            continue
        cells = []
        for R in R_TARGETS:
            rs = [x["multi"].get(str(R)) for x in v]
            rs = [r for r in rs if r]
            if not rs:
                cells.append("    -")
                continue
            n = len(rs)
            w = sum(1 for r in rs if r == "tp") / n
            cells.append("%+5.2f" % (w * R - (1 - w)))
        print("  %-22s %5d  %s" % (name, len(v), "  ".join(cells)))

    os.makedirs(os.path.dirname(args.json), exist_ok=True)
    json.dump({"detail": detail}, open(args.json, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n明细 ->", args.json)


if __name__ == "__main__":
    main()
