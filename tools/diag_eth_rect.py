# -*- coding: utf-8 -*-
"""ETHUSDT 15m rectangle 深挖 (run#217 推送时刻 2026-09-09T20:49Z)

疑点: 子Agent称"边界向上倾斜像上升通道" + "框选范围偏窄(前段大震荡未框入)"。
量化: 截断到推送时刻, 复现矩形; 输出边界斜率/绝对抬升/框选区间,
     并统计前段走势验证"框选窄"是否 box 固有行为。
"""
import os
import sys
import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

import yaml  # noqa: E402
from market_data import Kline  # noqa: E402
from detector import PatternEngine  # noqa: E402

PUSH_TS = 1788986940000  # 2026-09-09T20:49Z 附近的 15m 收盘


def load_csv(path, cutoff_ms=None):
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
            if cutoff_ms and t > cutoff_ms:
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
    eng = PatternEngine(cfg)
    path = "data_diag/klines_15m/ETHUSDT/ETHUSDT.csv"

    ks_full = load_csv(path)
    print(f"全量 {len(ks_full)} 根, {fmt_ts(ks_full[0].openTime)} ~ "
          f"{fmt_ts(ks_full[-1].openTime)}")

    # 找推送时刻最近的收盘K线
    ks = load_csv(path, cutoff_ms=PUSH_TS)
    print(f"截断到推送时刻 {fmt_ts(PUSH_TS)}: {len(ks)} 根, "
          f"最后 {fmt_ts(ks[-1].openTime)} close={ks[-1].close}")

    found = eng.scan_multiscale(ks, symbol="ETHUSDT", interval="15m")
    print(f"\n推送时刻复现: {len(found)} 个形态")
    for pat in found:
        if "rectangle" not in pat.pattern_type:
            continue
        u, lo = pat.upper_boundary, pat.lower_boundary
        print(f"\n  {pat.pattern_type} {pat.direction.value} "
              f"status={pat.status.value} conf={pat.confidence}")
        for nm, line in (("上边界", u), ("下边界", lo)):
            k1, k2 = ks[line.p1.index], ks[line.p2.index]
            abs_rise = line.p2.price - line.p1.price
            print(f"    {nm}: {line.p1.price:.2f}@{line.p1.index}"
                  f"({fmt_ts(k1.openTime)}) -> {line.p2.price:.2f}"
                  f"@{line.p2.index}({fmt_ts(k2.openTime)})  "
                  f"rel_slope={line.rel_slope:+.6f}  "
                  f"绝对抬升={abs_rise:+.2f} ({abs_rise/line.p1.price*100:+.2f}%)")
        if pat.breakout_index is not None:
            bk = ks[pat.breakout_index]
            print(f"    突破@{pat.breakout_index} {fmt_ts(bk.openTime)} "
                  f"close={pat.breakout_price} vol={pat.volume_ratio}")

    # 走势背景: 分区间统计高低点, 看 box 框选区间 vs 前段
    print(f"\n  走势背景 (最近 300 根, 每 50 根一段):")
    seg = ks[-300:]
    for i in range(0, len(seg), 50):
        chunk = seg[i:i + 50]
        if not chunk:
            continue
        hi = max(k.high for k in chunk)
        lo = min(k.low for k in chunk)
        print(f"    {fmt_ts(chunk[0].openTime)}~{fmt_ts(chunk[-1].openTime)}: "
              f"高{hi:.2f} 低{lo:.2f} 幅{(hi-lo)/lo*100:.2f}%")


if __name__ == "__main__":
    main()
