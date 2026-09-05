# -*- coding: utf-8 -*-
"""
图表渲染模块 —— 把识别出的形态画成带标注的 K 线图

关键设计：
  1. 【先识别，后画图】—— 图上的标注坐标全部来自识别阶段产出的 Pattern 对象，
     不是重新识别一遍。这保证「图和消息永远一致」：
     程序没识别出来的形态，图上也不会有标注。

  2. 必须用 Agg 后端 —— GitHub Actions runner 没有显示设备。
     matplotlib.use("Agg") 必须在 import pyplot 之前。

  3. 图上用中文标签（形态名/颈线/边界/左肩头右肩/入场止损止盈）——
     已打包 assets/fonts/SimHei.ttf，Actions runner 也能渲染中文。
     找不到中文字体时自动回退英文标签（避免方块）。
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

# TA-Lib 蜡烛形态名中英映射（chart.py 不在图上画中文，原因见头部注释）
# key 是 candles.py 返回的中文（去掉"(看涨)/(看跌)"后缀的版本）
CANDLE_LABELS_EN = {
    "锤子线": "Hammer",
    "倒锤子": "Inverted Hammer",
    "流星线": "Shooting Star",
    "上吊线": "Hanging Man",
    "吞没形态": "Engulfing",
    "晨星": "Morning Star",
    "暮星": "Evening Star",
    "刺透形态": "Piercing",
    "乌云盖顶": "Dark Cloud",
    "三白兵": "3 White Soldiers",
    "三只乌鸦": "3 Black Crows",
    "孕育形态": "Harami",
    "蜻蜓十字": "Dragonfly Doji",
    "墓碑十字": "Gravestone Doji",
    "十字星": "Doji",
    "光头光脚": "Marubozu",
}

# 形态中文标签（图上用，需中文字体）
PATTERN_LABELS_CN = {
    "double_top": "双顶 (M顶)",
    "double_bottom": "双底 (W底)",
    "head_shoulders_top": "头肩顶",
    "head_shoulders_bottom": "头肩底",
    "ascending_triangle": "上升三角形",
    "descending_triangle": "下降三角形",
    "symmetrical_triangle": "对称三角形",
    "flag": "旗形",
    "rising_wedge": "上升楔形",
    "falling_wedge": "下降楔形",
}

# ---- 中文字体支持 ----
# 打包 assets/fonts/SimHei.ttf，Actions runner 也能渲染中文，不再出方块。
# 找不到中文字体时 HAS_CJK=False，自动回退英文标签（避免方块）。
import matplotlib.font_manager as fm

_CJK_FONT_PATH_CANDIDATES = [
    os.path.join(os.path.dirname(_SRC_DIR), "assets", "fonts", "SimHei.ttf"),
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/msyh.ttc",
]
_CJK_FONT_NAMES = ["SimHei", "Microsoft YaHei", "Noto Sans CJK SC",
                   "Noto Sans SC", "WenQuanYi Micro Hei", "SimSun"]

HAS_CJK = False
CJK_FONT_PROP = None


def _setup_cjk_font():
    """尝试加载中文字体；成功则设置 matplotlib 全局字体并置 HAS_CJK=True。"""
    global HAS_CJK, CJK_FONT_PROP
    # 1) 优先用打包字体（仓库内，Actions 上也能用）
    for p in _CJK_FONT_PATH_CANDIDATES:
        if os.path.isfile(p):
            try:
                fm.fontManager.addfont(p)
                name = fm.FontProperties(fname=p).get_name()
                CJK_FONT_PROP = fm.FontProperties(fname=p)
                plt.rcParams["font.sans-serif"] = [name]
                plt.rcParams["axes.unicode_minus"] = False
                HAS_CJK = True
                logger.info(f"中文字体已加载: {name} ({p})")
                return
            except Exception as e:
                logger.warning(f"加载字体失败 {p}: {e}")
    # 2) 回退：系统已装的中文字体
    for n in _CJK_FONT_NAMES:
        try:
            fm.findfont(n, fallback_to_default=False)
            plt.rcParams["font.sans-serif"] = [n]
            plt.rcParams["axes.unicode_minus"] = False
            CJK_FONT_PROP = fm.FontProperties(family=n)
            HAS_CJK = True
            logger.info(f"中文字体(系统): {n}")
            return
        except Exception:
            pass
    logger.warning("未找到中文字体，图上回退英文标签")


_setup_cjk_font()


def _L(cn: str, en: str) -> str:
    """有中文字体用中文，否则用英文（避免方块）。"""
    return cn if HAS_CJK else en


def _pivot_role_labels(pattern) -> list:
    """返回 [(pivot, 中文角色名), ...]，用于在关键拐点旁标注。"""
    pt = pattern.pattern_type
    pv = pattern.pivots
    if pt in ("head_shoulders_top", "head_shoulders_bottom"):
        if len(pv) >= 5:
            return [(pv[0], "左肩"), (pv[2], "头"), (pv[4], "右肩")]
    elif pt == "double_top":
        if len(pv) >= 3:
            return [(pv[0], "左顶"), (pv[2], "右顶")]
    elif pt == "double_bottom":
        if len(pv) >= 3:
            return [(pv[0], "左底"), (pv[2], "右底")]
    return []


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
                         candles: int = 80,
                         width_px: int = 1000,
                         dpi: int = 120,
                         max_bytes: int = 1_887_436) -> Optional[bytes]:
    """
    渲染形态标注图，返回 PNG 字节。

    参数：
      candles   —— int: 图上显示最近多少根K线；
                   dict: 按周期取，如 {"15m":600, "1h":400, ...}
      width_px  —— 图宽度基准（像素），实际会按K线数自适应
      dpi       —— 渲染DPI
      max_bytes —— 大小上限（默认1.8MB，企微硬限制是2MB）

    超过上限会自动降 DPI 重渲染。
    """
    if not klines or len(klines) < 10:
        return None

    # candles 支持 dict 按周期取
    if isinstance(candles, dict):
        candles = candles.get(pattern.interval, candles.get("4h", 120))
    candles = int(candles)

    # 图宽按K线数自适应：每根约2.2px，避免500根挤成实心带
    # 同时设上下限：最小900px（企微可读），最大1600px（企微不溢出）
    adaptive_width = max(900, min(1600, int(candles * 2.2)))
    width_px = max(width_px, adaptive_width)

    height_px = int(width_px * 0.65)
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
    # 显示窗口：优先保证形态所有 pivot（含颈线/边界端点、突破点）都在图内，
    # 避免"整张图无线"——例如 AAVE 头肩顶：左肩落在默认 tail(末尾 candles 根)
    # 之前被裁掉，导致颈线/肩点全不显示。
    if len(klines) > candles:
        piv_idx = [p.index for p in pattern.pivots]
        need_start = max(0, min(piv_idx) - 6)           # 左肩留 6 根余量
        need_end = max(piv_idx)
        if pattern.breakout_index >= 0:
            need_end = max(need_end, pattern.breakout_index)
        need_end = min(len(klines), need_end + 12)       # 突破后留 12 根余量
        default_start = len(klines) - candles
        # 默认 tail 已覆盖全部 pivot → 沿用默认；否则自动扩窗
        if need_start >= default_start and need_end <= len(klines):
            tail = klines[default_start:]
        else:
            tail = klines[need_start:need_end]
    else:
        tail = klines
    offset = len(klines) - len(tail)

    fig, (ax, ax_vol) = plt.subplots(
        2, 1, figsize=(fig_w, fig_h),
        gridspec_kw={"height_ratios": [4, 1]},
        sharex=True,
    )
    fig.patch.set_facecolor("white")

    times = [epoch_to_num(k.openTime) for k in tail]
    n = len(tail)

    # ---------- 1. K线（手绘）----------
    # bar_width 0.85 让柱子更宽，视觉清晰
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
        # 影线（细一些，区分于实体）
        ax.vlines(times[i], k.low, body_low, color=color,
                  linewidth=0.7, alpha=0.7)
        ax.vlines(times[i], body_high, k.high, color=color,
                  linewidth=0.7, alpha=0.7)

    # ---------- 2. 趋势线 / 边界 ----------
    pt = pattern.pattern_type
    is_tri = pt in ("ascending_triangle", "descending_triangle",
                    "symmetrical_triangle")
    is_wedge = "wedge" in pt
    if pattern.upper_boundary is not None:
        ulbl = _L("上沿" if is_tri else ("上轨" if is_wedge else "上边界"),
                 "Upper Boundary")
        _draw_line(ax, pattern.upper_boundary, times[0], times[-1],
                   BOUND_COLOR, ulbl, linestyle="-", linewidth=2.0)
    if pattern.lower_boundary is not None:
        llbl = _L("下沿" if is_tri else ("下轨" if is_wedge else "下边界"),
                 "Lower Boundary")
        _draw_line(ax, pattern.lower_boundary, times[0], times[-1],
                   BOUND_COLOR, llbl, linestyle="-", linewidth=2.0)

    # ---------- 3. 颈线 ----------
    if pattern.neckline is not None:
        _draw_line(ax, pattern.neckline, times[0], times[-1],
                   NECK_COLOR, _L("颈线", "Neckline"),
                   linestyle="--", linewidth=2.2)

    # ---------- 4. 摆动点 + 关键角色标注 ----------
    role_map = {id(p): lbl for p, lbl in _pivot_role_labels(pattern)}
    for p in pattern.pivots:
        rel = p.index - offset
        if 0 <= rel < n:
            color = PIVOT_HIGH_COLOR if p.type == PivotType.HIGH \
                else PIVOT_LOW_COLOR
            ax.scatter(times[rel], p.price, s=28, color=color,
                       alpha=0.75, zorder=5, edgecolors="white",
                       linewidths=0.5)
            # 在关键拐点旁标中文角色（左肩/头/右肩、左底/右底等）
            if HAS_CJK and id(p) in role_map:
                ax.annotate(role_map[id(p)], xy=(times[rel], p.price),
                            xytext=(times[rel] + 1.2 * bar_width,
                                    p.price),
                            fontsize=7.5, fontweight="bold",
                            color=color, va="center", ha="left",
                            fontproperties=CJK_FONT_PROP,
                            arrowprops=dict(arrowstyle="-", color=color,
                                            lw=0.6, alpha=0.6))

    # ---------- 5. 突破点 ----------
    bo_rel = pattern.breakout_index - offset
    if 0 <= bo_rel < n:
        marker = "v" if pattern.direction == Direction.SHORT else "^"
        color = DOWN_COLOR if pattern.direction == Direction.SHORT \
            else UP_COLOR
        ax.scatter(times[bo_rel], pattern.breakout_price,
                   marker=marker, s=140, color=color, zorder=6,
                   edgecolors="white", linewidths=1.0,
                   label=_L(f"突破 {pattern.breakout_price:.4f}",
                            f"Breakout {pattern.breakout_price:.4f}"))

        # 单K线确认标注
        candle_cf = getattr(pattern, "candle_confirmations", [])
        if candle_cf:
            if HAS_CJK:
                cf_text = ", ".join(c for c in candle_cf)
            else:
                parts = []
                for c in candle_cf:
                    # c 形如 "锤子线(看涨)" / "十字星"
                    cn = c.split("(")[0].strip()
                    en = CANDLE_LABELS_EN.get(cn, cn)
                    if "(" in c and "看涨" in c:
                        en += "(Bull)"
                    elif "(" in c and "看跌" in c:
                        en += "(Bear)"
                    parts.append(en)
                cf_text = ", ".join(parts)
            ax.annotate(
                cf_text,
                xy=(times[bo_rel], pattern.breakout_price),
                xytext=(times[bo_rel] + 0.9 * bar_width,
                        pattern.breakout_price),
                fontsize=7.5, fontweight="bold", color=color,
                va="center", ha="left",
                fontproperties=CJK_FONT_PROP if HAS_CJK else None,
                arrowprops=dict(arrowstyle="-", color=color,
                                lw=0.6, alpha=0.6),
            )

    # ---------- 6. 交易价位水平线 ----------
    # 每条用不同线型+不同粗细+不同 alpha，标准化让眼睛瞬间分清
    if pattern.entry_price > 0:
        ax.axhline(pattern.entry_price, color="#333333", linestyle="-",
                   linewidth=1.2, alpha=0.85,
                   label=_L(f"入场 {pattern.entry_price:.4f}",
                            f"Entry {pattern.entry_price:.4f}"))
    if pattern.stop_loss > 0:
        ax.axhline(pattern.stop_loss, color=SL_COLOR, linestyle="--",
                   linewidth=1.5, alpha=0.9,
                   label=_L(f"止损 {pattern.stop_loss:.4f}",
                            f"Stop {pattern.stop_loss:.4f}"))
    if pattern.take_profit_1 > 0:
        ax.axhline(pattern.take_profit_1, color=TP1_COLOR, linestyle="-.",
                   linewidth=1.5, alpha=0.9,
                   label=_L(f"目标1 {pattern.take_profit_1:.4f}",
                            f"TP1 {pattern.take_profit_1:.4f}"))
    if pattern.take_profit_2 > 0:
        ax.axhline(pattern.take_profit_2, color=TP2_COLOR, linestyle=":",
                   linewidth=1.5, alpha=0.75,
                   label=_L(f"目标2 {pattern.take_profit_2:.4f}",
                            f"TP2 {pattern.take_profit_2:.4f}"))

    # ---------- 7. 三角形填充（让收敛形态一眼可辨）----------
    # 只在两条边界都覆盖的时间段内填充（取两端 p1 较大者、p2 较小者），
    # 避免对任一边界线做隐式外推导致区域被拉得很大。
    if (pattern.upper_boundary is not None and
            pattern.lower_boundary is not None):
        u = pattern.upper_boundary
        l = pattern.lower_boundary
        x_start = max(u.p1.index, l.p1.index)
        x_end = min(u.p2.index, l.p2.index)
        if x_end > x_start:
            xs = [epoch_to_num(klines[x_start].openTime),
                  epoch_to_num(klines[min(x_end, len(klines) - 1)].openTime)]
            upper_y = [u.value_at(x_start), u.value_at(x_end)]
            lower_y = [l.value_at(x_start), l.value_at(x_end)]
            ax.fill_between(xs, lower_y, upper_y,
                             color=BOUND_COLOR, alpha=0.10, zorder=2)

    # ---------- 8. 装帧 ----------
    label = (_L(PATTERN_LABELS_CN.get(pattern.pattern_type, pattern.pattern_type),
                PATTERN_LABELS_EN.get(pattern.pattern_type, pattern.pattern_type)))
    direction_cn = "多" if pattern.direction == Direction.LONG else "空"
    direction_en = "LONG" if pattern.direction == Direction.LONG else "SHORT"
    title = (f"{pattern.symbol}  {pattern.interval}  {label}  "
             f"[{direction_cn if HAS_CJK else direction_en}]\n"
             f"强度 {pattern.strength_score}/100   "
             f"置信 {pattern.confidence:.2f}   "
             f"盈亏比 1:{pattern.risk_reward:.2f}")
    ax.set_title(title, fontsize=11, fontweight="bold", pad=8,
                 fontproperties=CJK_FONT_PROP if HAS_CJK else None)
    ax.set_ylabel(_L("价格", "Price"), fontsize=9,
                  fontproperties=CJK_FONT_PROP if HAS_CJK else None)
    ax.grid(True, alpha=0.18, linewidth=0.5)
    # legend 放到图外右下角，避免遮 K 线
    ax.legend(loc="center left", bbox_to_anchor=(1.01, 0.5),
              fontsize=8, framealpha=0.9, ncol=1)

    # ---------- 9. 成交量 ----------
    vol_colors = [UP_COLOR if k.close >= k.open else DOWN_COLOR
                  for k in tail]
    ax_vol.bar(times, [k.volume for k in tail], width=bar_width,
               color=vol_colors, alpha=0.6, edgecolor="none")
    ax_vol.set_ylabel(_L("量", "Vol"), fontsize=8,
                       fontproperties=CJK_FONT_PROP if HAS_CJK else None)
    ax_vol.grid(True, alpha=0.15, linewidth=0.5)
    ax_vol.tick_params(labelsize=7)

    # 时间轴格式
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=8))
    ax.tick_params(labelsize=7)
    plt.setp(ax.get_xticklabels(), rotation=0, ha="center")
    plt.setp(ax_vol.get_xticklabels(), rotation=0, ha="center")

    fig.tight_layout()
    # 给右侧 legend 留空间，避免被裁掉
    fig.subplots_adjust(right=0.78)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor="white",
                edgecolor="none")
    plt.close(fig)
    return buf.getvalue()


def _draw_line(ax, line, t_start, t_end, color, label, linestyle="-",
               linewidth=2.0):
    """把 Line 对象画到指定时间范围上

    关键：只画在 pivot 时间区间内 (p1.timestamp ~ p2.timestamp)，
    不外推到图表左右两端 —— TradingView 风格，避免把价格轴拉到
    不真实的范围。t_start/t_end 保留为参数签名兼容，但实际用
    pivot 自身的时间戳决定端点。
    """
    p1, p2 = line.p1, line.p2
    if p2.index == p1.index:
        return
    t1 = epoch_to_num(p1.timestamp)
    t2 = epoch_to_num(p2.timestamp)
    if t2 == t1:
        return
    ax.plot([t1, t2], [p1.price, p2.price],
            color=color, linestyle=linestyle, linewidth=linewidth,
            alpha=0.9, label=label)


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
