# -*- coding: utf-8 -*-
"""诊断"改后无推送": 离线复刻线上扫描过滤链, 统计每道闸门杀掉的信号数。

用法: python tools/diag_no_push.py [--interval 1d] [--limit 57] [--dump output/diag_result.json]
不联网, 用 data/klines_1d 或 data/klines_4h 下的 CSV 逐标的模拟:
  scan_multiscale -> confirm(几何分) -> freshness -> strength -> rr
  -> volume -> geometry(0.6) -> trend(ADX) -> 最终可推数
"""
import os
import sys
import json
import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from patterns.base import Pattern, Direction  # noqa
from detector import PatternEngine  # noqa
from crosstf import CrossTimeframeConfirm  # noqa
from market_data import Kline  # noqa


def load_klines_csv(path):
    """读 history_replay.load_klines_csv 同款 CSV -> List[Kline]"""
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


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--limit", type=int, default=0, help="0=全部")
    ap.add_argument("--dump", default="")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(os.path.join(_ROOT, "config.yaml"), encoding="utf-8"))
    engine = PatternEngine(cfg)
    confirmer = CrossTimeframeConfirm(cfg)

    filt = cfg.get("filter", {})
    min_geo = filt.get("min_geometry", 0.6)
    min_strength = filt.get("min_strength", 60)
    min_rr = filt.get("min_rr", 0.75)
    min_vol = filt.get("min_volume_ratio", 1.5)
    adx_thr = filt.get("trend_adx_threshold", 20)
    kline_counts = cfg.get("scan", {}).get("kline_counts", {})
    scales = cfg.get("zigzag", {}).get("multiscale", [3, 5, 8, 12])

    d = os.path.join(_ROOT, "data", f"klines_{args.interval}")
    targets = []
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            csvp = os.path.join(d, name, f"{name}.csv")
            if os.path.exists(csvp):
                targets.append((name, csvp))
    if args.limit:
        targets = targets[: args.limit]
    print(f"标的数: {len(targets)}  interval={args.interval}  min_geometry={min_geo}")

    # 分阶段计数器: 每道闸门 kill 原因
    stage = {"candidates": 0, "confirmed": 0,
             "kill_fresh": 0, "kill_strength": 0, "kill_rr": 0,
             "kill_vol": 0, "kill_geo": 0, "kill_trend": 0,
             "passed": 0, "geo_missing": 0}
    geo_all = []      # 全部 confirmed 的 geometry_score（看分布）
    kill_geo_rows = []  # 被几何闸门杀掉的明细
    detail = []

    need = kline_counts.get(args.interval, 240)
    for sym_full, csvp in targets:
        sym = sym_full.replace("USDT", "")
        try:
            klines = load_klines_csv(csvp)
        except Exception as e:
            print(f"  !! {sym} 读取失败: {e}")
            continue
        if len(klines) < need + 5:
            continue
        trunc = klines[-need:]  # 与线上一致: 只拿最近 need 根
        try:
            found = engine.scan_multiscale(trunc, sym_full, args.interval,
                                           scales=scales)
        except Exception as e:
            print(f"  !! {sym} scan 异常: {e}")
            continue
        stage["candidates"] += len(found)

        ind = engine.last_indicators
        if ind is not None:
            trends = {(sym_full, args.interval): ind.trend}
            momentum = {(sym_full, args.interval): {
                "adx": ind.adx_current, "rsi": ind.rsi_current,
                "macd_hist": ind.macd_hist_current}}
            try:
                found = confirmer.confirm(found, trends, momentum)
            except Exception as e:
                print(f"  !! {sym} confirm 异常: {e}")
                continue
        stage["confirmed"] += len(found)

        for p in found:
            row = {"sym": sym, "pt": p.pattern_type, "dir": p.direction.value,
                   "age": getattr(p, "breakout_age", None),
                   "str": p.strength_score, "rr": p.risk_reward,
                   "vol": p.volume_ratio,
                   "geo": getattr(p, "geometry_score", None),
                   "adx": (momentum.get((sym_full, args.interval), {}).get("adx")
                           if ind is not None else None)}
            geo_all.append(row["geo"])
            # 逐道闸门
            max_age = engine.freshness_for(args.interval)
            if row["age"] is not None and row["age"] > max_age:
                row["killed_by"] = "fresh"; stage["kill_fresh"] += 1
                detail.append(row); continue
            if row["str"] < min_strength:
                row["killed_by"] = "strength"; stage["kill_strength"] += 1
                detail.append(row); continue
            if row["rr"] < min_rr:
                row["killed_by"] = "rr"; stage["kill_rr"] += 1
                detail.append(row); continue
            if row["vol"] < min_vol:
                row["killed_by"] = "vol"; stage["kill_vol"] += 1
                detail.append(row); continue
            if row["geo"] is not None:
                if row["geo"] < min_geo:
                    row["killed_by"] = "geo"; stage["kill_geo"] += 1
                    kill_geo_rows.append(row)
                    detail.append(row); continue
            else:
                stage["geo_missing"] += 1  # 无几何分->不拦(线上逻辑)
            # 趋势
            if row["adx"] is not None and row["adx"] >= adx_thr:
                trend = trends.get((sym_full, args.interval), "unknown")
                if not ((p.direction == Direction.LONG and trend == "up")
                        or (p.direction == Direction.SHORT and trend == "down")):
                    row["killed_by"] = "trend"; stage["kill_trend"] += 1
                    detail.append(row); continue
            row["killed_by"] = None
            stage["passed"] += 1
            detail.append(row)

    print()
    print("===== 过滤漏斗 =====")
    for k in ["candidates", "confirmed", "kill_fresh", "kill_strength",
              "kill_rr", "kill_vol", "kill_geo", "kill_trend",
              "passed", "geo_missing"]:
        print(f"  {k:>14}: {stage[k]}")

    # 几何分分布
    gs = [g for g in geo_all if g is not None]
    if gs:
        gs.sort(reverse=True)
        print()
        print(f"geometry_score: n={len(gs)}  >=0.6: {sum(1 for g in gs if g>=0.6)}"
              f"  0.4~0.6: {sum(1 for g in gs if 0.4<=g<0.6)}  <0.4: {sum(1 for g in gs if g<0.4)}")
        print(f"  top10: {[round(g,2) for g in gs[:10]]}")

    if kill_geo_rows:
        print()
        print("被几何闸门杀掉的明细 (geo < 0.6):")
        for r in sorted(kill_geo_rows, key=lambda x: x["geo"])[:20]:
            print(f"  {r['sym']:8s} {r['pt']:18s} geo={r['geo']} age={r['age']} str={r['str']} rr={r['rr']} vol={r['vol']} adx={r['adx']}")

    if args.dump:
        with open(os.path.join(_ROOT, args.dump), "w", encoding="utf-8") as f:
            json.dump({"stage": stage, "detail": detail}, f,
                      ensure_ascii=False, indent=1)
        print(f"\n明细已存 -> {args.dump}")


if __name__ == "__main__":
    main()
