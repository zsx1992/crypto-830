# -*- coding: utf-8 -*-
"""离线复刻线上完整扫描（含观察流），用于定位"几天没推送"的主闸。

与 tools/diag_no_push.py 的区别:
  diag_no_push 只手写了 6 道闸的子集, 且不含 Crosstf 打分/去重/状态冷却;
  本脚本 monkeypatch 掉 Scanner 的取数层, 其余逻辑 100% 走线上代码,
  kill_breakdown / after_scoring / observed 与 Actions 上完全一致。

用法:
  python tools/offline_scan.py [--intervals 15m,1h,4h,1d] [--dump output/offline.json]
数据来自 tools/fetch_real_klines.py 拉到本地的 data/klines_<iv>/<SYM>/<SYM>.csv
"""
import os
import sys
import json
import time
import argparse
import logging

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

import yaml  # noqa: E402
from market_data import Kline  # noqa: E402
from scanner import Scanner  # noqa: E402


def load_csv(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            p = ln.strip().split(",")
            if len(p) < 7:
                continue
            try:
                t = int(p[0])
            except ValueError:
                continue
            try:
                rows.append(Kline(
                    openTime=t, open=float(p[1]), high=float(p[2]),
                    low=float(p[3]), close=float(p[4]), volume=float(p[5]),
                    closeTime=int(p[6]),
                    quoteVolume=float(p[7]) if len(p) > 7 else 0.0))
            except (ValueError, IndexError):
                continue
    rows.sort(key=lambda k: k.openTime)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intervals", default="15m,1h,4h,1d")
    ap.add_argument("--dump", default="output/offline_scan.json")
    ap.add_argument("--log", default="INFO")
    ap.add_argument("--state", default="state/state_offline.json",
                    help="离线专用状态文件, 避免污染线上去重状态")
    ap.add_argument("--show", type=int, default=25, help="打印前N条明细")
    args = ap.parse_args()

    logging.basicConfig(level=getattr(logging, args.log.upper(), logging.INFO),
                        format="%(levelname)s %(message)s")
    # 噪音降一档, 只看关键
    logging.getLogger("scanner").setLevel(logging.INFO)

    cfg = yaml.safe_load(open(os.path.join(_ROOT, "config.yaml"),
                             encoding="utf-8"))
    intervals = [s for s in args.intervals.split(",") if s]

    # 只跑本地已有数据的标的：取各周期目录下标的的交集
    sym_sets = []
    for iv in intervals:
        d = os.path.join(_ROOT, "data", f"klines_{iv}")
        s = set()
        if os.path.isdir(d):
            for name in os.listdir(d):
                if os.path.exists(os.path.join(d, name, f"{name}.csv")):
                    s.add(name)
        sym_sets.append(s)
    symbols = sorted(set.intersection(*sym_sets)) if sym_sets else []
    print(f"[离线] 标的 {len(symbols)} 个, 周期 {intervals}")

    cfg.setdefault("state", {})["file"] = args.state
    cfg.setdefault("throttling", {})["batch_pause_sec"] = 0

    sc = Scanner(cfg, dry_run=True, verbose=False)

    # ---- 打桩取数层 ----
    def fake_tiered(core_top_n=300, core_min_volume_usdt=0,
                    tail_top_n=300, tail_min_volume_usdt=0):
        return symbols[:core_top_n], []

    def fake_klines(symbol, interval, limit=500):
        p = os.path.join(_ROOT, "data", f"klines_{interval}", symbol,
                         f"{symbol}.csv")
        if not os.path.exists(p):
            return [], "miss"
        ks = load_csv(p)
        return ks[-limit:] if len(ks) > limit else ks, "local"

    sc.client.get_symbols_tiered = fake_tiered
    sc.client.get_top_symbols = lambda top_n=300, min_volume_usdt=0: \
        symbols[:top_n]
    sc.client.get_klines = fake_klines

    t0 = time.time()
    res = sc.run(intervals=intervals)
    print(f"\n耗时 {time.time()-t0:.0f}s")

    print("\n===== 线上同款漏斗 =====")
    print(f"  扫描对(标的×周期) : {res.scanned_pairs}  (失败 {res.failed_pairs})")
    print(f"  候选 candidates   : {len(res.candidates)}")
    print(f"  已确认 confirmed  : {len(res.confirmed)}")
    print(f"  过滤后 after_scor : {len(res.after_scoring)}")
    print(f"  去重后 after_dedup: {len(res.after_dedup)}")
    print(f"  正式推送 pushed   : {len(res.pushed)}")
    print(f"  观察流 observed   : {len(res.observed)}")
    print("  ---- 各闸砍杀 ----")
    for k, v in sorted(res.kill_breakdown.items(), key=lambda x: -x[1]):
        print(f"    {k:<10}: {v}")

    # 候选按类型分布
    from collections import Counter
    ct = Counter(f"{p.pattern_type}" for p in res.candidates)
    print(f"\n候选形态类型分布: {dict(ct)}")
    ci = Counter(f"{p.interval}" for p in res.candidates)
    print(f"候选周期分布: {dict(ci)}")

    print(f"\n--- 通过了全部闸门但可能没推的样本 (前{args.show}) ---")
    for p in res.after_scoring[:args.show]:
        print(f"  {p.symbol:<12} {p.interval:<4} {p.pattern_type:<20} "
              f"{p.direction.name:<5} age={p.breakout_age:<4} "
              f"str={p.strength_score} rr={p.risk_reward} "
              f"vol={p.volume_ratio} geo={getattr(p,'geometry_score',None)}")

    if args.dump:
        def slim(p):
            return {"symbol": p.symbol, "interval": p.interval,
                    "type": p.pattern_type, "dir": p.direction.name,
                    "age": p.breakout_age, "str": p.strength_score,
                    "rr": p.risk_reward, "vol": p.volume_ratio,
                    "geo": getattr(p, "geometry_score", None),
                    "conf": p.confidence}
        out = {"funnel": {"scanned": res.scanned_pairs,
                          "candidates": len(res.candidates),
                          "confirmed": len(res.confirmed),
                          "after_scoring": len(res.after_scoring),
                          "after_dedup": len(res.after_dedup),
                          "pushed": len(res.pushed),
                          "observed": len(res.observed)},
               "kill": res.kill_breakdown,
               "candidates": [slim(p) for p in res.candidates],
               "after_scoring": [slim(p) for p in res.after_scoring],
               "observed": [slim(p) for p in res.observed]}
        with open(os.path.join(_ROOT, args.dump), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(f"\n明细已存 -> {args.dump}")


if __name__ == "__main__":
    main()
