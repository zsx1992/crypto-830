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


def judge(p, future):
    """判定一段形态的后续结果：'tp' 达标 / 'sl' 止损 / 'none' 超时"""
    entry = getattr(p, "entry_price", 0) or 0
    sl = getattr(p, "stop_loss", 0) or 0
    tp = getattr(p, "take_profit_1", 0) or 0
    if not (entry > 0 and sl > 0 and tp > 0):
        return None
    long = str(p.direction).replace("Direction.", "") == "LONG"
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    for k in future:
        if long:
            hit_sl = k.low <= sl
            hit_tp = k.high >= tp
        else:
            hit_sl = k.high >= sl
            hit_tp = k.low <= tp
        if hit_sl and hit_tp:
            return ("sl", -1.0)      # 同根双触，保守判负
        if hit_sl:
            return ("sl", -1.0)
        if hit_tp:
            return ("tp", abs(tp - entry) / risk)
    return ("none", 0.0)


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
                    r = judge(p, fut)
                    if not r:
                        continue
                    res, rmult = r
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

    os.makedirs(os.path.dirname(args.json), exist_ok=True)
    json.dump({"detail": detail}, open(args.json, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n明细 ->", args.json)


if __name__ == "__main__":
    main()
