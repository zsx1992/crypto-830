# -*- coding: utf-8 -*-
"""全池 H&S 颈线差分布审计 (为收紧 neck_slope_max 0.08->0.05 做误杀评估)

遍历 data/klines_<iv> 全部标的, scan_multiscale 检出所有头肩形态,
统计两颈峰相对差 neck_diff。neck_diff>5% 的形态若曾被推/曾视觉合格,
收紧后会误杀 → 需要人眼复核这批。
"""
import os
import sys
import datetime

import argparse
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

import yaml  # noqa: E402
from market_data import Kline  # noqa: E402
from indicators import calc_indicators  # noqa: E402
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


def collect(cfg, tag=""):
    """跑全池, 返回 (总形态数, >5% 名单, 全部形态 key 集合)"""
    engine = PatternEngine(cfg)
    all_keys = set()
    over05 = []
    for iv in ("15m", "1h", "4h", "1d"):
        d = os.path.join(_ROOT, "data", f"klines_{iv}")
        if not os.path.isdir(d):
            continue
        for sym in sorted(os.listdir(d)):
            path = os.path.join(d, sym, f"{sym}.csv")
            if not os.path.exists(path):
                continue
            klines = load_csv(path)
            if len(klines) < 80:
                continue
            try:
                found = engine.scan_multiscale(klines, symbol=sym,
                                               interval=iv)
            except Exception:
                continue
            for pat in found:
                if pat.pattern_type not in ("head_shoulders_bottom",
                                            "head_shoulders_top"):
                    continue
                _, n1, _, n2, _ = pat.pivots
                denom = max(abs(n1.price), abs(n2.price), 1e-9)
                nd = abs(n1.price - n2.price) / denom
                key = (sym, iv, pat.pattern_type,
                       pat.pivots[0].index, pat.pivots[-1].index)
                all_keys.add(key)
                if nd > 0.05:
                    over05.append((sym, iv, pat.pattern_type,
                                   pat.direction.value, pat.status.value,
                                   nd, n1.price, n1.index, n2.price,
                                   n2.index))
    return all_keys, over05


def fmt_ts(ms):
    return datetime.datetime.fromtimestamp(
        ms / 1000, datetime.timezone(datetime.timedelta(hours=8))
    ).strftime("%m-%d %H:%M")


def main():
    cfg = yaml.safe_load(open(os.path.join(_ROOT, "config.yaml"),
                             encoding="utf-8"))
    engine = PatternEngine(cfg)

    rows_all = []          # 所有头肩形态
    over05 = []            # neck_diff > 5%
    hist = {"0~1%": 0, "1~3%": 0, "3~5%": 0, "5~8%": 0, ">8%": 0}

    for iv in ("15m", "1h", "4h", "1d"):
        d = os.path.join(_ROOT, "data", f"klines_{iv}")
        if not os.path.isdir(d):
            continue
        syms = sorted(os.listdir(d))
        for sym in syms:
            path = os.path.join(d, sym, f"{sym}.csv")
            if not os.path.exists(path):
                continue
            klines = load_csv(path)
            if len(klines) < 80:
                continue
            try:
                found = engine.scan_multiscale(klines, symbol=sym,
                                               interval=iv)
            except Exception:
                continue
            for pat in found:
                if pat.pattern_type not in ("head_shoulders_bottom",
                                            "head_shoulders_top"):
                    continue
                _, n1, _, n2, _ = pat.pivots
                denom = max(abs(n1.price), abs(n2.price), 1e-9)
                nd = abs(n1.price - n2.price) / denom
                row = (sym, iv, pat.pattern_type, pat.direction.value,
                       pat.status.value, nd, n1.price, n1.index,
                       n2.price, n2.index,
                       fmt_ts(klines[n1.index].openTime),
                       fmt_ts(klines[n2.index].openTime))
                rows_all.append(row)
                if nd < 0.01:
                    hist["0~1%"] += 1
                elif nd < 0.03:
                    hist["1~3%"] += 1
                elif nd < 0.05:
                    hist["3~5%"] += 1
                elif nd < 0.08:
                    hist["5~8%"] += 1
                else:
                    hist[">8%"] += 1
                if nd > 0.05:
                    over05.append(row)

    print(f"共检出头肩形态 {len(rows_all)} 个, 颈线相对差分布:")
    for k, v in hist.items():
        print(f"  {k}: {v}")
    print(f"\n颈线差 >5% 的形态 {len(over05)} 个 (收紧 0.05 后将消失):")
    for r in sorted(over05, key=lambda x: -x[5]):
        sym, iv, ptype, direc, st, nd, pL, iL, pR, iR, tL, tR = r
        print(f"  {sym:<14} {iv:<4} {ptype:<22} {direc:<5} "
              f"{st:<10} neck_diff={nd*100:5.2f}%  "
              f"峰L={pL:.4f}@{iL}({tL}) 峰R={pR:.4f}@{iR}({tR})")


if __name__ == "__main__":
    main()

def compare():
    """新旧 config A/B: 确认收紧只删 >5%, 不误杀 5% 以内的形态"""
    old_cfg = yaml.safe_load(open("_safe/config_old_neck08.yaml", encoding="utf-8"))
    new_cfg = yaml.safe_load(open(os.path.join(_ROOT, "config.yaml"),
                                  encoding="utf-8"))
    keys_old, over_old = collect(old_cfg)
    keys_new, over_new = collect(new_cfg)
    print(f"旧(0.08): {len(keys_old)} 个 H&S, 其中 >5% 形态 {len(over_old)} 个")
    print(f"新(0.05): {len(keys_new)} 个 H&S, 其中 >5% 形态 {len(over_new)} 个")
    gone = keys_old - keys_new
    added = keys_new - keys_old
    print(f"\n收紧后消失 {len(gone)} 个, 新增 {len(added)} 个")
    for g in sorted(gone):
        print(f"  - {g}")
    for a in sorted(added):
        print(f"  + {a}")


if __name__ == "__main__":
    compare()
