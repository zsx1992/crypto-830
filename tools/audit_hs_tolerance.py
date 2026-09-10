# -*- coding: utf-8 -*-
"""收紧头肩两处确认容差的全池 A/B 影响评估

对比: 当前(peak_price=0.08, head_prominence=0.01)
    vs 收紧(peak_price=0.05, head_prominence=0.03)
输出: 检出集合 diff + 各形态颈/肩/头突出参数, 检查是否误杀视觉合格形态。
"""
import os
import sys
import copy
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


def collect(cfg):
    """返回 {key: (pat, klines, sym, iv)}"""
    eng = PatternEngine(cfg)
    out = {}
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
                if not pat.pattern_type.startswith("head_shoulders"):
                    continue
                pv = pat.pivots
                key = (sym, iv, pat.pattern_type,
                       pv[0].index, pv[-1].index)
                out[key] = (pat, ks)
    return out


def describe(pat, ks):
    pt = pat.pattern_type
    if pt.endswith("bottom"):
        ls, n1, head, n2, rs = pat.pivots
        sh_diff = abs(ls.price - rs.price) / max(ls.price, rs.price)
        prom = (min(ls.price, rs.price) - head.price) / head.price
        neck = abs(n1.price - n2.price) / max(n1.price, n2.price)
    else:
        ls, n1, head, n2, rs = pat.pivots
        sh_diff = abs(ls.price - rs.price) / max(ls.price, rs.price)
        prom = (head.price - max(ls.price, rs.price)) / max(ls.price, rs.price)
        neck = abs(n1.price - n2.price) / max(n1.price, n2.price)
    k = ks[head.index]
    return (f"肩差{sh_diff*100:4.1f}% 头突{prom*100:4.1f}% "
            f"颈差{neck*100:4.1f}%  头@{fmt_ts(k.openTime)}")


def main():
    base = yaml.safe_load(open(os.path.join(_ROOT, "config.yaml"),
                               encoding="utf-8"))
    tight = copy.deepcopy(base)
    tol = tight["patterns"]["tolerance"]
    tol["peak_price"] = 0.05
    tol["head_prominence"] = 0.03

    print("收集当前(0.08/0.01) 检出...")
    old = collect(base)
    print(f"  当前 {len(old)} 个 H&S")
    print("收集收紧(0.05/0.03) 检出...")
    new = collect(tight)
    print(f"  收紧 {len(new)} 个 H&S")

    gone = {k: v for k, v in old.items() if k not in new}
    added = {k: v for k, v in new.items() if k not in old}
    print(f"\n收紧后消失 {len(gone)} 个:")
    for k in sorted(gone):
        pat, ks = gone[k]
        print(f"  - {k[0]:<12} {k[1]:<4} {k[2]:<22} "
              f"{describe(pat, ks)}")
    print(f"\n新增 {len(added)} 个:")
    for k in sorted(added):
        pat, ks = added[k]
        print(f"  + {k[0]:<12} {k[1]:<4} {k[2]:<22} "
              f"{describe(pat, ks)}")


if __name__ == "__main__":
    main()
