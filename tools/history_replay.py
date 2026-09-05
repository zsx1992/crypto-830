#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
历史回放: 模拟"过去某时刻系统会推什么形态"

对本地历史 K 线, 按截断点(默认每10天)切到"当时", 只用该时刻之前的数据跑检测器,
把通过过滤(置信度门槛 + 形态跨度下限)的形态渲染成标注图 + 记录清单。
完全防前视 —— 绝不让检测器看到截断点之后的行情。

用途: 把"挂机观察几周才能攒到的推送样本"压缩成一次离线运行,
      产出几十~上百张历史形态图, 由人工标注 ok/bad/meh 来校准容差参数。

用法:
  python tools/history_replay.py --interval 4h --step-days 10
  python tools/history_replay.py --symbols BTC ETH SOL --interval 1d
  python tools/history_replay.py --all                    # 所有已下载的标的×周期

依赖: data/klines/{interval}/{SYM}/{SYM}.csv  (先跑 download_binance_klines.py)
输出:
  output/history_replay/charts/*.png
  output/history_replay/manifest.json
然后: python tools/make_replay_sheet.py --manifest output/history_replay/manifest.json \
        --out output/history_replay/annotate.html
"""
import os
import sys
import json
import argparse
import logging
from collections import defaultdict
from datetime import datetime

import matplotlib
matplotlib.use("Agg")  # 无显示环境必须

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

import yaml
from market_data import Kline
from detector import PatternEngine
from chart import render_pattern_chart
from crosstf import CrossTimeframeConfirm

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("replay")

_INTERVAL_MIN = {"15m": 15, "1h": 60, "2h": 120, "4h": 240, "1d": 1440}
_ZIGZAG = {"15m": (5, 5), "1h": (5, 5), "2h": (4, 4), "4h": (3, 3), "1d": (3, 3)}


def load_klines_csv(path: str) -> list:
    """读 Binance 历史 CSV -> List[Kline] (兼容 download_binance_klines.py 输出)"""
    rows = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            p = ln.strip().split(",")
            if len(p) < 7:
                continue
            try:
                t = int(p[0])
            except ValueError:
                continue  # 跳过表头
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


def pattern_span_bars(pattern) -> int:
    """第一拐点到最后拐点的 K 线跨度"""
    if not pattern.pivots:
        return 0
    idxs = [q.index for q in pattern.pivots]
    return max(idxs) - min(idxs)


def main():
    ap = argparse.ArgumentParser(description="历史回放(防前视)")
    ap.add_argument("--interval", default="4h")
    ap.add_argument("--symbols", default=None, help="空格/逗号分隔基名; 默认全部已下载")
    ap.add_argument("--step-days", type=int, default=10, help="截断点步长(天)")
    ap.add_argument("--config", default=os.path.join(_ROOT, "config.yaml"))
    ap.add_argument("--out-dir", default=os.path.join(_ROOT, "output", "history_replay"))
    ap.add_argument("--max-per-symbol", type=int, default=15, help="每标的最多渲染张数")
    ap.add_argument("--min-confidence", type=float, default=None, help="覆盖全局门槛")
    ap.add_argument("--min-geometry", type=float, default=None,
                    help="模拟线上几何闸门(与 config filter.min_geometry 同口径); "
                         "默认 None 不过闸门——保留低分样本供评估误报用")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    engine = PatternEngine(cfg)

    pat_cfg = cfg.get("patterns", {})
    min_pattern_bars = pat_cfg.get("min_pattern_bars", {})
    kline_counts = cfg.get("scan", {}).get("kline_counts", {})
    cd = cfg.get("chart", {}).get("candles_displayed", 240)
    candles_by_interval = cd if isinstance(cd, dict) else {i: cd for i in _ZIGZAG}
    scales = cfg.get("zigzag", {}).get("multiscale", [3, 5, 8, 12])

    # 收集目标 CSV (与 download_binance_klines.py 的 data/klines_{interval}/ 对齐)
    d = os.path.join(_ROOT, "data", f"klines_{args.interval}")
    targets = []
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            csvp = os.path.join(d, name, f"{name}.csv")
            if os.path.exists(csvp):
                sym = name.replace("USDT", "")
                if args.symbols:
                    allowed = args.symbols.replace(",", " ").split()
                    if sym not in allowed:
                        continue
                targets.append((sym, args.interval, csvp))
    if not targets:
        print(f"未找到 {d} 下的 CSV。先跑: "
              f"python tools/download_binance_klines.py --interval {args.interval}")
        return 1
    print(f"回放目标: {len(targets)} 个标的, 周期={args.interval}, step={args.step_days}天")

    charts_dir = os.path.join(args.out_dir, "charts")
    os.makedirs(charts_dir, exist_ok=True)
    confirmer = CrossTimeframeConfirm(cfg)
    # 给回放样本算七维 strength_score（含 ADX/RSI/MACD 趋势打分），
    # 写进 manifest 让标注页能看综合强度（图对但分低 / 图错但分高）。
    manifest = []
    total = 0
    dedup_ms = args.step_days * 2 * 24 * 3600 * 1000  # 同类型去重窗口

    for sym, interval, csvp in targets:
        klines = load_klines_csv(csvp)
        need = kline_counts.get(interval, 240)
        if len(klines) < need + 5:
            logger.info(f"{sym} {interval}: 数据 {len(klines)}根 < 窗口 {need}, 跳过")
            continue
        step = max(1, int(args.step_days * 1440 / _INTERVAL_MIN[interval]))
        # 2026-09-05 修复：原 last_end 只记"最近一次"末端，
        # 多尺度/滑窗下同一形态隔几轮重现时会漏拦（abs 超窗口）。
        # 改为记录该类型【所有已入册】末端，任一相近即跳过。
        seen_ends: Dict[str, list] = defaultdict(list)  # pattern_type -> [end_ms,...]
        per = 0
        for t in range(need, len(klines) - 2, step):
            trunc = klines[:t]  # 防前视: 只用截断点之前的数据
            try:
                found = engine.scan_multiscale(
                    trunc, sym + "USDT", interval, scales=scales)
                # 计算七维 strength_score（含 ADX/RSI/MACD 趋势打分），
                # 写进 manifest 让标注页能看综合强度（图对但分低/图错但分高）。
                ind = engine.last_indicators
                if ind is not None:
                    trends = {(sym + "USDT", interval): ind.trend}
                    momentum = {(sym + "USDT", interval): {
                        "adx": ind.adx_current,
                        "rsi": ind.rsi_current,
                        "macd_hist": ind.macd_hist_current,
                    }}
                    found = confirmer.confirm(found, trends, momentum)
            except Exception as e:
                logger.warning(f"{sym} {interval} t={t}: {e}")
                continue
            for p in found:
                thr = args.min_confidence or engine.confidence_threshold_for(interval)
                if p.confidence < thr:
                    continue
                span = pattern_span_bars(p)
                if span < min_pattern_bars.get(interval, 0):
                    continue
                # 模拟线上几何闸门（默认关闭，评估误报需看低分样本）
                if args.min_geometry is not None:
                    g = getattr(p, "geometry_score", None)
                    if g is None or g < args.min_geometry:
                        continue
                end_idx = max(q.index for q in p.pivots)
                end_ms = trunc[end_idx].openTime
                p.end_ms = end_ms  # 同步给 pattern，供后续记录/比对
                # 与该类型所有已入册末端比较（不只最近一次）
                if any(abs(end_ms - e) < dedup_ms for e in seen_ends[p.pattern_type]):
                    continue  # 同一形态在去重窗口内已入册, 跳过避免重复图
                seen_ends[p.pattern_type].append(end_ms)
                candles = min(candles_by_interval.get(interval, 240), len(trunc))
                png = render_pattern_chart(
                    trunc, p, candles=candles, width_px=1100, dpi=120)
                if not png:
                    continue
                end_date = datetime.utcfromtimestamp(end_ms / 1000).strftime("%Y%m%d")
                fname = f"{sym}{interval}_{end_date}_{p.pattern_type}_{total:03d}.png"
                with open(os.path.join(charts_dir, fname), "wb") as f:
                    f.write(png)
                manifest.append({
                    "symbol": sym, "interval": interval,
                    "pattern_type": p.pattern_type,
                    "direction": getattr(p.direction, "value", str(p.direction)),
                    "confidence": round(p.confidence, 3),
                    "strength_score": round(getattr(p, "strength_score", 0), 1),
                    "geometry_score": round(getattr(p, "geometry_score", 0), 3),
                    "geometry_reason": getattr(p, "geometry_reason", ""),
                    "span_bars": span, "pivots": len(p.pivots),
                    "end_date": end_date, "end_ms": end_ms,
                    "image": f"charts/{fname}",
                })
                total += 1
                per += 1
                if per >= args.max_per_symbol:
                    break
        print(f"  {sym} {interval}: {per} 张 (共 {len(klines)} 根K线)")

    mpath = os.path.join(args.out_dir, "manifest.json")
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.utcnow().isoformat(),
            "interval": args.interval, "step_days": args.step_days,
            "total": total, "rows": manifest,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n回放完成: {total} 张图 -> {charts_dir}")
    print(f"清单: {mpath}")
    print(f"下一步生成标注页: python tools/make_replay_sheet.py "
          f"--manifest {mpath} --out {os.path.join(args.out_dir, 'annotate.html')}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
