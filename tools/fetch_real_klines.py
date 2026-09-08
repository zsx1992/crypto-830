# -*- coding: utf-8 -*-
"""拉取 OKX 真实 K 线到本地 data/klines_<interval>/<SYM>/<SYM>.csv

用途: 给 tools/diag_no_push.py 提供与线上同源的真实数据, 离线复刻过滤漏斗。
本机在中国, 必须走系统代理 (代码默认 trust_env=False 会绕过代理 —— 这里打开)。

用法:
  python tools/fetch_real_klines.py --top 60 [--intervals 15m,1h,4h,1d] [--proxy auto]
"""
import os
import sys
import csv
import time
import argparse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from market_data import OkxClient  # noqa: E402

# 与 config.scan.kline_counts 保持一致
KLINE_COUNTS = {"15m": 600, "1h": 400, "4h": 300, "1d": 240}


def save_csv(klines, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        for k in klines:
            w.writerow([k.openTime, k.open, k.high, k.low, k.close,
                        k.volume, k.closeTime, k.quoteVolume])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--intervals", default="15m,1h,4h,1d")
    ap.add_argument("--min-vol", type=float, default=5_000_000)
    ap.add_argument("--proxy", default="auto",
                    help="http代理地址; auto=读环境变量, none=直连")
    ap.add_argument("--force", action="store_true", help="忽略已存在文件重拉")
    args = ap.parse_args()

    cli = OkxClient()
    if args.proxy == "auto":
        cli.session.trust_env = True      # 走系统代理(中国IP必需)
    elif args.proxy not in ("none", ""):
        cli.session.proxies = {"http": args.proxy, "https": args.proxy}

    if not cli.ping():
        print("[FATAL] OKX 不通, 检查代理")
        return 1
    print("[ok] OKX 连通")

    syms = cli.get_top_symbols(top_n=args.top,
                               min_volume_usdt=args.min_vol)
    print(f"[ok] 标的 {len(syms)} 个: {syms[:8]} ...")

    intervals = [s for s in args.intervals.split(",") if s]
    t0 = time.time()
    ok = fail = skip = 0
    for iv in intervals:
        need = KLINE_COUNTS.get(iv, 300)
        for i, sym in enumerate(syms, 1):
            path = os.path.join(_ROOT, "data", f"klines_{iv}", sym, f"{sym}.csv")
            if os.path.exists(path) and not args.force:
                skip += 1
                continue
            try:
                ks = cli.get_klines(sym, iv, limit=need)
            except Exception as e:
                print(f"  !! {sym} {iv} 异常: {e}")
                fail += 1
                continue
            if len(ks) < need * 0.8:
                print(f"  -- {sym} {iv} 数据不足 {len(ks)}/{need}, 跳过")
                fail += 1
                continue
            save_csv(ks, path)
            ok += 1
            if ok % 50 == 0:
                print(f"  [{iv}] {i}/{len(syms)}  ok={ok}  {time.time()-t0:.0f}s")
        print(f"[{iv}] 完成 ok={ok} fail={fail} skip={skip}  "
              f"{time.time()-t0:.0f}s")
    print(f"ALL DONE ok={ok} fail={fail} skip={skip} {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
