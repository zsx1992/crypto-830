# -*- coding: utf-8 -*-
"""box 水平度判定重分类评估

当前: flat = |rel_slope| <= 0.0004 (每根, 与跨度无关 → 长跨度累计倾斜失控)
建议: flat = 边界累计抬升 / 箱高 <= 0.15 (视觉标准)

输出: 全池 box 形态在新规则下的重分类映射 (rectangle→? / channel→?)
"""
import os
import sys
import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

import yaml  # noqa: E402
from market_data import Kline  # noqa: E402
from detector import PatternEngine  # noqa: E402

MAX_RISE = 0.15


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


def reclass(u, lo):
    """按新规则给出分类"""
    s = min(u.p1.index, lo.p1.index)
    e = max(u.p2.index, lo.p2.index)
    h_l = abs(u.value_at(s) - lo.value_at(s))
    h_r = abs(u.value_at(e) - lo.value_at(e))
    box_h = (h_l + h_r) / 2
    if box_h <= 0:
        return None, 0, 0
    u_rr = abs(u.p2.price - u.p1.price) / box_h
    l_rr = abs(lo.p2.price - lo.p1.price) / box_h
    u_up = u.p2.price > u.p1.price
    l_up = lo.p2.price > lo.p1.price
    if max(u_rr, l_rr) <= MAX_RISE:
        kind = "rectangle"
    elif u_up and l_up:
        kind = "ascending_channel"
    elif (not u_up) and (not l_up):
        kind = "descending_channel"
    else:
        kind = None      # 一上一下 → 交给 triangle
    return kind, u_rr, l_rr


def main():
    cfg = yaml.safe_load(open(os.path.join(_ROOT, "config.yaml"),
                             encoding="utf-8"))
    eng = PatternEngine(cfg)
    stats = {}
    changes = []
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
                if pat.pattern_type not in ("rectangle",
                                            "ascending_channel",
                                            "descending_channel"):
                    continue
                new_kind, u_rr, l_rr = reclass(pat.upper_boundary,
                                               pat.lower_boundary)
                old = pat.pattern_type
                stats[(old, new_kind)] = stats.get((old, new_kind), 0) + 1
                if new_kind != old:
                    changes.append((sym, iv, old, new_kind, u_rr, l_rr,
                                    pat.direction.value))
    print("旧类型 -> 新类型 映射统计:")
    for (o, n), c in sorted(stats.items(), key=lambda x: -x[1]):
        flag = "" if o == n else "   ← 变化"
        print(f"  {o:<20} -> {str(n):<20} {c:>3}{flag}")
    print(f"\n发生变化 {len(changes)} 个:")
    for sym, iv, old, new, ur, lr, direc in sorted(changes,
                                                   key=lambda x: -max(x[4], x[5])):
        print(f"  {sym:<12} {iv:<4} {direc:<5} {old:<20} -> {str(new):<20} "
              f"抬升/箱高 u={ur*100:6.1f}% l={lr*100:6.1f}%")


if __name__ == "__main__":
    main()
