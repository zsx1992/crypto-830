#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
随机 K 线样本生成器（负样本采集）

用途：
  从本地缓存的 4h K 线数据（data/klines_4h/，data.binance.vision 下载）
  随机抽取时间窗口，画【纯 K 线图】（无任何形态标注）。
  配合 make_annotation_sheet.py 使用：
    随机图中人眼判定「像某个形态」的 → 模型漏报（可补充金标准，召回率）
    随机图中人眼判定「没有形态」的 → 负样本基线（确认模型不瞎报）
  完全离线运行，不依赖网络。

用法:
  python tools/generate_random_samples.py                    # 默认 60 张
  python tools/generate_random_samples.py --count 80
  python tools/generate_random_samples.py --candles 80       # 每张窗口 K 线数

输出:
  output/random_samples/{SYM}_4h_random_{nn}.png
  命名兼容标注页 FNAME_RE（pat 部分 random_01）
"""

import os
import sys
import glob
import random
import argparse
import logging

# 让 src/ 可 import（复用 chart.py 的配色与时间轴逻辑）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from chart import epoch_to_num, UP_COLOR, DOWN_COLOR, EPOCH_OFFSET_DAYS
from market_data import Kline

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = os.path.join(_ROOT, "data", "klines_4h")
DEFAULT_OUT_DIR = os.path.join(_ROOT, "output", "random_samples")


def load_klines(csv_path: str):
    """读本地 CSV -> List[Kline]（data/klines_4h/{SYM}/{SYM}.csv 格式）"""
    rows = []
    with open(csv_path, "r", encoding="utf-8") as f:
        next(f, None)  # 跳过表头
        for line in f:
            parts = line.strip().split(",")
            if len(parts) < 7:
                continue
            try:
                rows.append(Kline(
                    openTime=int(parts[0]),
                    open=float(parts[1]),
                    high=float(parts[2]),
                    low=float(parts[3]),
                    close=float(parts[4]),
                    volume=float(parts[5]),
                    closeTime=int(parts[6]),
                    quoteVolume=float(parts[7]) if len(parts) > 7 else 0.0,
                ))
            except (ValueError, IndexError):
                continue
    rows.sort(key=lambda k: k.openTime)
    return rows


def render_plain(klines, symbol, candles, out_path, width_px=1200, dpi=110):
    """画纯 K 线图（K线 + 成交量），无任何形态标注"""
    tail = klines[-candles:] if len(klines) > candles else klines
    n = len(tail)
    if n < 10:
        return False

    fig_w = width_px / float(dpi)
    fig_h = fig_w * 0.65
    fig, (ax, ax_vol) = plt.subplots(
        2, 1, figsize=(fig_w, fig_h),
        gridspec_kw={"height_ratios": [4, 1]},
        sharex=True,
    )
    fig.patch.set_facecolor("white")

    times = [epoch_to_num(k.openTime) for k in tail]
    bar_width = 0.85 * (times[1] - times[0]) if n > 1 else 0.85

    for i, k in enumerate(tail):
        color = UP_COLOR if k.close >= k.open else DOWN_COLOR
        body_low = min(k.open, k.close)
        body_high = max(k.open, k.close)
        body_h = body_high - body_low
        if body_h <= 0:
            body_h = (k.high - k.low) * 0.02 or 1e-8
        ax.bar(times[i], body_h, bottom=body_low, width=bar_width,
               color=color, edgecolor="white", linewidth=0.4)
        ax.vlines(times[i], k.low, body_low, color=color,
                  linewidth=0.7, alpha=0.7)
        ax.vlines(times[i], body_high, k.high, color=color,
                  linewidth=0.7, alpha=0.7)

    vol_colors = [UP_COLOR if k.close >= k.open else DOWN_COLOR for k in tail]
    ax_vol.bar(times, [k.volume for k in tail], width=bar_width,
               color=vol_colors, alpha=0.6, edgecolor="none")
    ax_vol.set_ylabel("Vol", fontsize=8)
    ax_vol.grid(True, alpha=0.15, linewidth=0.5)
    ax_vol.tick_params(labelsize=7)

    # 标题只给身份信息（标的/周期/时间范围），不给任何"像不像形态"的暗示
    t0 = tail[0].openTime
    t1 = tail[-1].openTime
    ax.set_title(f"{symbol}  4h\n"
                 f"{__import__('time').strftime('%Y-%m-%d', __import__('time').gmtime(t0 / 1000 + 8 * 3600))}"
                 f" ~ "
                 f"{__import__('time').strftime('%Y-%m-%d', __import__('time').gmtime(t1 / 1000 + 8 * 3600))}",
                 fontsize=11, fontweight="bold", pad=8)
    ax.set_ylabel("Price", fontsize=9)
    ax.grid(True, alpha=0.18, linewidth=0.5)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=8))
    ax.tick_params(labelsize=7)
    plt.setp(ax.get_xticklabels(), rotation=0, ha="center")
    plt.setp(ax_vol.get_xticklabels(), rotation=0, ha="center")

    fig.tight_layout()
    buf = __import__("io").BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor="white",
                edgecolor="none")
    plt.close(fig)
    with open(out_path, "wb") as f:
        f.write(buf.getvalue())
    return True


def main():
    ap = argparse.ArgumentParser(description="随机 K 线样本生成器（负样本采集）")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="4h K线目录")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="输出目录")
    ap.add_argument("--count", type=int, default=60, help="生成张数")
    ap.add_argument("--candles", type=int, default=240, help="每张窗口 K 线数(默认240=40天4h)")
    ap.add_argument("--seed", type=int, default=None, help="随机种子（可复现）")
    args = ap.parse_args()

    random.seed(args.seed)

    csvs = sorted(glob.glob(os.path.join(args.data_dir, "*", "*.csv")))
    if not csvs:
        print(f"未在 {args.data_dir} 找到 CSV（先跑 download_binance_klines.py）")
        return 1

    # 每标的可用窗口数，用于加权采样（数据长的标的更容易被抽到）
    windows = []
    for p in csvs:
        klines = load_klines(p)
        if len(klines) >= args.candles:
            windows.append((os.path.basename(os.path.dirname(p)), klines))
    if not windows:
        print(f"没有标的的数据足够 {args.candles} 根 K 线")
        return 1

    os.makedirs(args.out_dir, exist_ok=True)

    made = 0
    for i in range(args.count):
        sym, klines = random.choice(windows)
        start = random.randint(0, len(klines) - args.candles)
        window = klines[start:start + args.candles]
        out_path = os.path.join(args.out_dir, f"{sym}_4h_random_{i:02d}.png")
        if render_plain(window, sym, args.candles, out_path):
            made += 1

    print(f"随机样本已生成: {made} 张 -> {args.out_dir}")
    print(f"  标的池: {len(windows)} 个（每标的平均可用窗口 "
          f"{sum(len(w) - args.candles + 1 for _, w in windows) // len(windows)} 个）")
    print("下一步: 合并到标注页 -> python tools/make_annotation_sheet.py "
          "--dir output/random_samples --out output/annotate_random.html "
          "--prefix random_samples/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
