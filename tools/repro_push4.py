# -*- coding: utf-8 -*-
"""复现 run#214/217/220 推送 4 标的, dump 结构验证"图不标准"的共性根因"""
import os
import sys
import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

import yaml  # noqa: E402
from market_data import Kline  # noqa: E402
from indicators import calc_indicators  # noqa: E402
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

    cases = [
        ("AXTIUSDT", "4h", "data_diag/klines_4h/AXTIUSDT/AXTIUSDT.csv"),
        ("SUIUSDT", "1h", "data_diag/klines_1h/SUIUSDT/SUIUSDT.csv"),
        ("ALLOUSDT", "1h", "data_diag/klines_1h/ALLOUSDT/ALLOUSDT.csv"),
        ("ETHUSDT", "15m", "data_diag/klines_15m/ETHUSDT/ETHUSDT.csv"),
    ]

    for sym, iv, path in cases:
        print(f"\n{'#'*72}\n# {sym} {iv}\n{'#'*72}")
        klines = load_csv(path)
        print(f"K线 {len(klines)} 根, 最新 {fmt_ts(klines[-1].openTime)} "
              f"close={klines[-1].close}")
        ind = calc_indicators(klines)
        found = engine.scan_multiscale(klines, symbol=sym, interval=iv)
        print(f"共检出 {len(found)} 个形态")
        for pat in found:
            pv = pat.pivots
            rng = f"[{pv[0].index}~{pv[-1].index}]" if pv else "[]"
            bk = (f"breakout@{pat.breakout_index} "
                  f"{klines[pat.breakout_index].close if pat.breakout_index is not None and pat.breakout_index < len(klines) else '?'}"
                  ) if pat.breakout_index is not None else "无突破"
            print(f"  {pat.pattern_type} {pat.direction.value} "
                  f"status={pat.status.value} conf={pat.confidence} "
                  f"geo_span={rng} {bk}")
            geo, reason = validate_geometry(pat)
            print(f"    geo={geo}  [{reason}]")
            # dump pivot 结构
            if pat.pattern_type.startswith("head_shoulders"):
                names = ("左肩/谷", "颈L", "头", "颈R", "右肩/谷")
                for nm, p in zip(names, pv):
                    k = klines[p.index]
                    print(f"      {nm:<5} {p.type.value:<4} "
                          f"price={p.price:<10.4f} idx={p.index} "
                          f"{fmt_ts(k.openTime)}")
            elif pat.pattern_type in ("double_top", "double_bottom"):
                for nm, p in zip(("翼1", "中", "翼2"), pv):
                    k = klines[p.index]
                    print(f"      {nm:<5} {p.type.value:<4} "
                          f"price={p.price:<10.4f} idx={p.index} "
                          f"{fmt_ts(k.openTime)}")
            # 边界
            for lname, line in (("上边界", pat.upper_boundary),
                                ("下边界", pat.lower_boundary),
                                ("颈线", pat.neckline)):
                if line:
                    k1, k2 = klines[line.p1.index], klines[line.p2.index]
                    print(f"      {lname}: slope={line.rel_slope:+.6f}  "
                          f"{line.p1.price:.4f}@{line.p1.index}"
                          f"({fmt_ts(k1.openTime)}) -> "
                          f"{line.p2.price:.4f}@{line.p2.index}"
                          f"({fmt_ts(k2.openTime)})")


if __name__ == "__main__":
    main()
