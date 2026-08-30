# -*- coding: utf-8 -*-
"""
图表渲染模块 —— 把识别出的形态画成带标注的 K 线图

关键设计：
  1. 【先识别，后画图】—— 图上的标注坐标全部来自识别阶段产出的 Pattern 对象，
     不是重新识别一遍。这保证「图和消息永远一致」：
     程序没识别出来的形态，图上也不会有标注。

  2. 必须用 Agg 后端 —— GitHub Actions runner 没有显示设备。
     matplotlib.use("Agg") 必须在 import pyplot 之前。

  3. 图上只用英文/数字标签 —— Actions runner 通常没有中文字体，
     中文会渲染成方块。中文全部放在企微 markdown 正文里。
"""

import os
import sys
import io
import logging
from typing import List, Optional

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import matplotlib
matplotlib.use("Agg")          # 必须在 import pyplot 之前
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.lines import Line2D

from market_data import Kline
from patterns.base import Pattern, Direction, PivotType

logger = logging.getLogger(__name__)

# 配色（遵循国内习惯：涨红跌绿）
UP_COLOR = "#E24B4A"        # 涨
DOWN_COLOR = "#3B6D11"      # 跌
NECK_COLOR = "#A32D2D"      # 颈线
BOUND_COLOR = "#185FA5"     # 趋势线/边界
TP1_COLOR = "#854F0B"
TP2_COLOR = "#BA7517"
SL_COLOR = "#CC0000"
PIVOT_HIGH_COLOR = "#E24B4A"
PIVOT_LOW_COLOR = "#3B6D11"

# 形态英文标签（图上用）
PATTERN_LABELS_EN = {
    "double_top": "Double Top",
    "double_bottom": "Double Bottom",
    "head_shoulders_top": "Head & Shoulders Top",
    "head_shoulders_bottom": "Head & Shoulders Bottom",
    "ascending_triangle": "Ascending Triangle",
    "descending_triangle": "Descending Triangle",
    "symmetrical_triangle": "Symmetrical Triangle",
    "flag": "Flag",
    "rising_wedge": "Rising Wedge",
    "falling_wedge": "Falling Wedge",
}


# matplotlib 日期轴单位 = 天；1970-01-01 对应的日期数值
EPOCH_OFFSET_DAYS = 719163


def epoch_to_num(open_time_ms: int) -> float:
    """
    把毫秒时间戳转成 matplotlib 日期数值。

    关键：matplotlib 日期轴的单位是【天】(1.0 = 1天)，不是秒。
    直接把秒传进去，bar_width 会被算成几千天，
    触发 "int too big to convert" 错误。
    （注：mdates.epoch2num 在部分 matplotlib 版本不存在，这里手算更稳）
    """
    return EPOCH_OFFSET_DAYS + open_time_ms / 1000.0 / 86400.0


def render_pattern_chart(klines: List[Kline], pattern: Pattern,
                         candles: int = 120,
                         width_px: int = 900,
                         dpi: int = 120,
                         max_bytes: int = 1_887_436) -> Optional[bytes]:
    """
    渲染形态标注图，返回 PNG 字节。

    参数：
      candles   —— 图上显示最近多少根K线
      width_px  —— 图宽度（像素）
      dpi       —— 渲染DPI
      max_bytes —— 大小上限（默认1.8MB，企微硬限制是2MB）

    超过上限会自动降 DPI 重渲染。
    """
    if not klines or len(klines) < 10:
        return None

    height_px = int(width_px * 0.58)
    fig_w = width_px / float(dpi)
    fig_h = height_px / float(dpi)

    for attempt, use_dpi in enumerate([dpi, 80, 60]):
        try:
            data = _render(klines, pattern, candles, fig_w, fig_h, use_dpi)
            if data and len(data) <= max_bytes:
                if attempt > 0:
                    logger.info(f"图表压缩到 dpi={use_dpi}，"
                                f"{len(data)} bytes")
                return data
            if attempt == 2 and data:
                logger.warning(f"图表仍超上限: {len(data)} bytes")
                return data
        except Exception as e:
            logger.error(f"图表渲染失败 (dpi={use_dpi}): {e}")
            if attempt == 2:
                return None
    return None


def _render(klines, pattern, candles, fig_w, fig_h, dpi) -> Optional[bytes]:
    """实际渲染逻辑"""
    tail = klines[-candles:] if len(klines) > candles else klines
    offset = len(klines) - len(tail)

    fig, (ax, ax_vol) = plt.subplots(
        2, 1, figsize=(fig_w, fig_h),
        gridspec_kw={"height_ratios": [4, 1]},
        sharex=True,
    )
    fig.patch.set_facecolor("white")

    times = [epoch_to_num(k.openTime) for k in tail]
    n = len(tail)

    # ---------- 1. K线（手绘，不依赖已废弃的 mpl_finance）----------
    bar_width = 0.6 * (times[1] - times[0]) if n > 1 else 0.6
    for i, k in enumerate(tail):
        color = UP_COLOR if k.close >= k.open else DOWN_COLOR
        body_low = min(k.open, k.close)
        body_high = max(k.open, k.close)
        body_h = body_high - body_low
        if body_h <= 0:
            body_h = (k.high - k.low) * 0.02 or 1e-8
        ax.bar(times[i], body_h, bottom=body_low, width=bar_width,
               color=color, edgecolor=color, linewidth=0.3)
        ax.vlines(times[i], k.low, k.high, color=color,
                  linewidth=0.6, alpha=0.8)

    # ---------- 2. 趋势线 / 边界 ----------
    if pattern.upper_boundary is not None:
        _draw_line(ax, pattern.upper_boundary, times[0], times[-1],
                   BOUND_COLOR, "Upper Boundary")
    if pattern.lower_boundary is not None:
        _draw_line(ax, pattern.lower_boundary, times[0], times[-1],
                   BOUND_COLOR, "Lower Boundary")

    # ---------- 3. 颈线 ----------
    if pattern.neckline is not None:
        _draw_line(ax, pattern.neckline, times[0], times[-1],
                   NECK_COLOR, "Neckline", linestyle="--")

    # ---------- 4. 摆动点 ----------
    for p in pattern.pivots:
        rel = p.index - offset
        if 0 <= rel < n:
            color = PIVOT_HIGH_COLOR if p.type == PivotType.HIGH \
                else PIVOT_LOW_COLOR
            ax.scatter(times[rel], p.price, s=28, color=color,
                       alpha=0.75, zorder=5, edgecolors="white",
                       linewidths=0.5)

    # ---------- 5. 突破点 ----------
    bo_rel = pattern.breakout_index - offset
    if 0 <= bo_rel < n:
        marker = "v" if pattern.direction == Direction.SHORT else "^"
        color = DOWN_COLOR if pattern.direction == Direction.SHORT \
            else UP_COLOR
        ax.scatter(times[bo_rel], pattern.breakout_price,
                   marker=marker, s=140, color=color, zorder=6,
                   edgecolors="white", linewidths=1.0,
                   label=f"Breakout {pattern.breakout_price:.4f}")

    # ---------- 6. 交易价位水平线 ----------
    if pattern.take_profit_1 > 0:
        ax.axhline(pattern.take_profit_1, color=TP1_COLOR, linestyle=":",
                   linewidth=1.0, alpha=0.7,
                   label=f"TP1 {pattern.take_profit_1:.4f}")
    if pattern.take_profit_2 > 0:
        ax.axhline(pattern.take_profit_2, color=TP2_COLOR, linestyle=":",
                   linewidth=1.0, alpha=0.45,
                   label=f"TP2 {pattern.take_profit_2:.4f}")
    if pattern.stop_loss > 0:
        ax.axhline(pattern.stop_loss, color=SL_COLOR, linestyle="-.",
                   linewidth=1.0, alpha=0.55,
                   label=f"Stop {pattern.stop_loss:.4f}")
    if pattern.entry_price > 0:
        ax.axhline(pattern.entry_price, color="#5F5E5A", linestyle="-",
                   linewidth=0.8, alpha=0.5,
                   label=f"Entry {pattern.entry_price:.4f}")

    # ---------- 7. 装帧 ----------
    label = PATTERN_LABELS_EN.get(pattern.pattern_type, pattern.pattern_type)
    direction_en = "LONG" if pattern.direction == Direction.LONG else "SHORT"
    title = (f"{pattern.symbol}  {pattern.interval}  {label}  [{direction_en}]\n"
             f"Strength {pattern.strength_score}/100   "
             f"Confidence {pattern.confidence:.2f}   "
             f"R:R 1:{pattern.risk_reward:.2f}")
    ax.set_title(title, fontsize=9, fontweight="bold", pad=8)
    ax.set_ylabel("Price", fontsize=8)
    ax.grid(True, alpha=0.18, linewidth=0.5)
    ax.legend(loc="upper left", fontsize=6.5, framealpha=0.85, ncol=2)

    # ---------- 8. 成交量 ----------
    vol_colors = [UP_COLOR if k.close >= k.open else DOWN_COLOR
                  for k in tail]
    ax_vol.bar(times, [k.volume for k in tail], width=bar_width,
               color=vol_colors, alpha=0.55)
    ax_vol.set_ylabel("Vol", fontsize=7)
    ax_vol.grid(True, alpha=0.15, linewidth=0.5)
    ax_vol.tick_params(labelsize=7)

    # 时间轴格式
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=8))
    ax.tick_params(labelsize=7)
    plt.setp(ax.get_xticklabels(), rotation=0, ha="center")
    plt.setp(ax_vol.get_xticklabels(), rotation=0, ha="center")

    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor="white",
                edgecolor="none")
    plt.close(fig)
    return buf.getvalue()


def _draw_line(ax, line, t_start, t_end, color, label, linestyle="-"):
    """把 Line 对象画到指定时间范围上"""
    p1, p2 = line.p1, line.p2
    if p2.index == p1.index:
        return
    t1 = epoch_to_num(p1.timestamp)
    t2 = epoch_to_num(p2.timestamp)
    # 延伸到图表左右边界
    slope = (p2.price - p1.price) / (p2.index - p1.index)
    # 用 K 线索引 -> 时间的近似映射
    # （p1/p2 的 timestamp 已经是对应的 K 线时间，可直接用）
    t_span = t2 - t1
    if t_span == 0:
        return
    # 线性外推
    price_slope = (p2.price - p1.price) / t_span
    y_start = p1.price + price_slope * (t_start - t1)
    y_end = p1.price + price_slope * (t_end - t1)

    ax.plot([t_start, t_end], [y_start, y_end],
            color=color, linestyle=linestyle, linewidth=1.3,
            alpha=0.8, label=label)


def test_font_availability() -> dict:
    """
    检查中文字体是否可用。

    Actions runner 上通常没有中文字体，所以图上只用英文。
    这个函数用于诊断——如果返回可用，未来可以启用中文标注。
    """
    from matplotlib import font_manager
    available = set()
    for f in font_manager.fontManager.ttflist:
        available.add(f.name)
    cjk_candidates = [
        "Noto Sans CJK SC", "WenQuanYi Micro Hei", "SimHei",
        "Microsoft YaHei", "Noto Sans CJK JP",
    ]
    found = [c for c in cjk_candidates if c in available]
    return {
        "cjk_fonts_found": found,
        "total_fonts": len(available),
        "recommendation": ("可用中文标注" if found
                           else "无中文字体，图上请用英文标签"),
    }
