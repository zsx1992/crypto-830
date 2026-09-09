# -*- coding: utf-8 -*-
"""MINIMAXUSDT 15m H&S底 复现 (朱哥金标准 run#206: "什么也不是简直瞎画")

对照: RKLBUSDT 4h H&S底 (朱哥: "看着还不错")。
dump 每个 head_shoulders 检出的 5-pivot 坐标 + validate_geometry 四子分
+ 突破信息, 量化"瞎画"根因。
"""
import os
import sys
import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

import yaml  # noqa: E402
from market_data import Kline  # noqa: E402
from indicators import calc_indicators  # noqa: E402
from zigzag import find_pivots, PivotType  # noqa: E402
from detector import PatternEngine  # noqa: E402
from patterns.base import validate_geometry  # noqa: E402


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


def fmt_ts(ms):
    return datetime.datetime.fromtimestamp(
        ms / 1000, datetime.timezone(datetime.timedelta(hours=8))
    ).strftime("%m-%d %H:%M")


def main():
    cfg = yaml.safe_load(open(os.path.join(_ROOT, "config.yaml"),
                             encoding="utf-8"))
    engine = PatternEngine(cfg)
    scales = cfg.get("zigzag", {}).get("multiscale", [5])
    swing_min = cfg.get("zigzag", {}).get("swing_min_atr", 1.0)

    cases = [
        ("MINIMAXUSDT", "15m", "data_diag/klines_15m/MINIMAXUSDT/MINIMAXUSDT.csv"),
        ("RKLBUSDT", "4h", "data_diag/klines_4h/RKLBUSDT/RKLBUSDT.csv"),
    ]

    for sym, iv, path in cases:
        print(f"\n{'#'*74}\n# {sym} {iv}   (run#206 判定时刻 ≈ K线末端)\n{'#'*74}")
        klines = load_csv(path)
        print(f"K线 {len(klines)} 根  最新 {fmt_ts(klines[-1].openTime)}  "
              f"最新close={klines[-1].close}")
        ind = calc_indicators(klines)
        print(f"ATR={ind.atr_current:.6f}")

        found = engine.scan_multiscale(klines, symbol=sym, interval=iv)
        print(f"scan_multiscale 共检出 {len(found)} 个形态:")
        for pat in found:
            print(f"  - {pat.pattern_type} {pat.direction} "
                  f"status={pat.status.value} conf={pat.confidence} "
                  f"idx范围[{pat.pivots[0].index}~{pat.pivots[-1].index}]")
            if pat.breakout_index is not None:
                bk = klines[pat.breakout_index]
                print(f"      breakout@{pat.breakout_index} "
                      f"{fmt_ts(bk.openTime)} close={pat.breakout_price} "
                      f"vol={pat.volume_ratio} mag_atr={pat.breakout_magnitude_atr}")

        # dump 所有头肩形态的五点明细 + 几何
        hs = [p for p in found
              if p.pattern_type in ("head_shoulders_bottom",
                                    "head_shoulders_top")]
        print(f"\n头肩形态 {len(hs)} 个, 逐一 dump 几何:")
        for pat in hs:
            ls, n1, head, n2, rs = pat.pivots
            geo, reason = validate_geometry(pat)
            print(f"\n  [{pat.pattern_type} {pat.direction}] "
                  f"geo={geo}  reason={reason}")
            names = ("左肩/谷", "颈峰L", "头", "颈峰R", "右肩/谷")
            for nm, pv in zip(names, pat.pivots):
                k = klines[pv.index]
                print(f"    {nm:<6} {pv.type.value:<4} idx={pv.index:<4} "
                      f"price={pv.price:<10.4f} {fmt_ts(k.openTime)}")
            nl = pat.neckline
            if nl:
                print(f"    颈线 slope={nl.rel_slope:+.6f} "
                      f"({nl.p1.price:.4f}@{nl.p1.index} -> "
                      f"{nl.p2.price:.4f}@{nl.p2.index})")
                # 颈线在头处的值 vs 头价 → 头部深度(ATR)
                hv = nl.value_at(head.index)
                print(f"    头处颈线值={hv:.4f} 头价={head.price:.4f} "
                      f"深度={(hv-head.price):.4f} "
                      f"(ATR={ind.atr_current:.6f}, "
                      f"{(hv-head.price)/ind.atr_current:.1f}×ATR)")

        # 打印形态区域 pivots 序列 (定位视觉上下文)
        if hs:
            lo_i = min(p.index for p in hs[0].pivots) - 6
            hi_i = max(p.index for p in hs[-1].pivots) + 6
            lo_i = max(0, lo_i)
            print(f"\n  形态区域 pivot 序列 (idx {lo_i}~{hi_i}):")
            for s in scales:
                pivots = find_pivots(klines, left=s, right=s,
                                     atr_list=ind.atr,
                                     min_swing_atr=swing_min)
                seq = [p for p in pivots if lo_i <= p.index <= hi_i]
                print(f"    scale {s}: "
                      + " ".join(f"{p.type.value[0]}{p.price:.2f}@{p.index}"
                                 for p in seq))


if __name__ == "__main__":
    main()
