# -*- coding: utf-8 -*-
"""拉 4 个标的 K 线 (复现 run#214/217/220 推送图几何)"""
import os, sys, csv
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))
from market_data import OkxClient

NEED = {"15m": 600, "1h": 400, "4h": 300, "1d": 240}
CASES = [("AXTIUSDT", "4h"), ("SUIUSDT", "1h"),
         ("ETHUSDT", "15m"), ("ALLOUSDT", "1h")]

def main():
    cli = OkxClient()
    cli.session.trust_env = True
    if not cli.ping():
        print("[FATAL] OKX 不通")
        return 1
    print("[ok] OKX 连通")
    for sym, iv in CASES:
        path = os.path.join(_ROOT, "data_diag", f"klines_{iv}",
                            sym, f"{sym}.csv")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            ks = cli.get_klines(sym, iv, limit=NEED[iv])
        except Exception as e:
            print(f"  !! {sym} {iv} 异常: {e}")
            continue
        print(f"  {sym} {iv}: {len(ks)} 根, 最新 {ks[-1].openTime}")
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            for k in ks:
                w.writerow([k.openTime, k.open, k.high, k.low, k.close,
                            k.volume, k.closeTime, k.quoteVolume])
    return 0

if __name__ == "__main__":
    sys.exit(main())
