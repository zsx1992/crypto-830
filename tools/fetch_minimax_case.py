# -*- coding: utf-8 -*-
"""拉 MINIMAXUSDT 15m / RKLBUSDT 4h K线到 data_diag (复现 run#206 观察流用)"""
import os, sys, csv
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))
from market_data import OkxClient

NEED = {"15m": 600, "4h": 300}

def main():
    cli = OkxClient()
    cli.session.trust_env = True  # 走系统代理
    if not cli.ping():
        print("[FATAL] OKX 不通, 检查代理/梯子")
        return 1
    print("[ok] OKX 连通")
    for sym, iv in [("MINIMAXUSDT", "15m"), ("RKLBUSDT", "4h")]:
        path = os.path.join(_ROOT, "data_diag", f"klines_{iv}",
                            sym, f"{sym}.csv")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            ks = cli.get_klines(sym, iv, limit=NEED[iv])
        except Exception as e:
            print(f"  !! {sym} {iv} 异常: {e}")
            continue
        print(f"  {sym} {iv}: {len(ks)} 根, "
              f"最新 {ks[-1].openTime} -> {ks[0].openTime}")
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            for k in ks:
                w.writerow([k.openTime, k.open, k.high, k.low, k.close,
                            k.volume, k.closeTime, k.quoteVolume])
    return 0

if __name__ == "__main__":
    sys.exit(main())
