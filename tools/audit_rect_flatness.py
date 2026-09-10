# -*- coding: utf-8 -*-
"""审计 box 矩形"边界抬升占箱高比"分布 (为 flat 判定收紧定阈值)

背景: ETH 15m rectangle 上边界抬升 9.61 / 箱高 51.82 = 18.5%, 图上明显斜,
但 flat_threshold(每根 rel_slope ≤ 0.0004) 放行。用"累计抬升/箱高"更贴近视觉。
"""
import os
import sys
import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

import yaml  # noqa: E402
from market_data import Kline  # noqa: E402
from detector import PatternEngine  # noqa: E402


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
    eng = PatternEngine(cfg)
    rows = []
    for iv in ("15m", "1h", "4h", "1d"):
        d = os.path.join(_ROOT, "data", f"klines_{iv}")
        if not os.path.isdir(d):
            continue
        for sym in sorted(os.listdir(d)):
            path = os.path.join(d, sym, f"{sym}.csv")
            if not os.path.exists(path):
                continue
            ks = load_csv(path)
            if len(ks) < 80:
                continue
            try:
                found = eng.scan_multiscale(ks, symbol=sym, interval=iv)
            except Exception:
                continue
            for pat in found:
                if pat.pattern_type != "rectangle":
                    continue
                u, lo = pat.upper_boundary, pat.lower_boundary
                if not u or not lo:
                    continue
                # 箱高: 用两边界在左/右端的间距均值
                s = min(u.p1.index, lo.p1.index)
                e = max(u.p2.index, lo.p2.index)
                h_l = abs(u.value_at(s) - lo.value_at(s))
                h_r = abs(u.value_at(e) - lo.value_at(e))
                box_h = (h_l + h_r) / 2
                if box_h <= 0:
                    continue
                # 边界抬升占箱高比 (取两者较大)
                u_rise = abs(u.p2.price - u.p1.price) / box_h
                l_rise = abs(lo.p2.price - lo.p1.price) / box_h
                ratio = max(u_rise, l_rise)
                rows.append((ratio, sym, iv, pat.direction.value,
                             u.rel_slope, lo.rel_slope, box_h, e - s))
    rows.sort(reverse=True)
    print(f"共 {len(rows)} 个 rectangle, 按[边界抬升/箱高]降序:")
    for ratio, sym, iv, direc, us, ls, box_h, span in rows[:20]:
        print(f"  {sym:<12} {iv:<4} {direc:<5} 抬升/箱高={ratio*100:5.1f}%  "
              f"箱高={box_h:8.4f} span={span:<4} "
              f"u_slope={us:+.6f} l_slope={ls:+.6f}")
    # 分布
    print("\n分布:")
    for thr in (0.05, 0.10, 0.15, 0.20, 0.30):
        n = sum(1 for r in rows if r[0] <= thr)
        print(f"  ≤{int(thr*100)}%: {n} 个")


if __name__ == "__main__":
    main()
