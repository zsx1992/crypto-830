# -*- coding: utf-8 -*-
"""
回归测试: 双顶/双底"相邻锚点最小间隔"(min_pivot_gap)。

金标准来源 (2026-09-11):
  朱哥截图 TAOUSDT 1h 双顶, 反馈"仅凭这几根K线不足以构成有效的双顶"。
  像素反解 (@1h, 1.78125 px/根):
    峰1   x=643 →  0        (274.9 ~ 276.9)
    谷   x=699 →  +31.4 根  (249.2)    峰1-谷 = 31.4
    峰2   x=706 →  +35.0 根 (269.78)   谷-峰2 =  3.6  ← 病灶(长上影插针)
    峰3   x=742 →  +55.6 根 (269.0)    ← 第三个高点, 检测器未标
  用真实缓存独立复跑检测器确认: 同一窗口内 h1=276.90/l1=254.20/h2=263.90,
  两峰价差 4.70% > 3% 被拒 —— 说明"选哪两个峰"决定形态是否成立。

断言:
  A. 关闭约束(min_pivot_gap=0) 时该形态能被检出(证明不是被别的闸拦)
  B. 落地值 min_pivot_gap=5 时该形态被拦
  C. 真实 config.yaml 读到 double_min_pivot_gap == 5
  D. 放宽到 4 时又能检出(证明拦的就是那 4 根)
  E. 相邻间隔 = 5 时保留(边界不误伤)
  F. 双底场景同样受约束(对称性)
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

import yaml  # noqa: E402
from market_data import Kline  # noqa: E402
from zigzag import Pivot, PivotType  # noqa: E402
from patterns.double import DoubleTopBottomDetector  # noqa: E402

_fails = []


def _check(name, cond, detail=""):
    print("  %s %s%s" % ("✓" if cond else "✗", name,
                         (" | " + detail) if detail else ""))
    if not cond:
        _fails.append(name)


_BASE_TS = 1_700_000_000_000
_HOUR = 3_600_000


def _build_klines(anchors, n=110, skeleton=None):
    """按骨架造 K 线, 并把每个锚点位置的极值钉到位。"""
    pts = skeleton
    closes = []
    for i in range(n):
        lo, hi = pts[0], pts[-1]
        for k in range(len(pts) - 1):
            if pts[k][0] <= i <= pts[k + 1][0]:
                lo, hi = pts[k], pts[k + 1]
                break
        if hi[0] == lo[0]:
            c = hi[1]
        else:
            t = (i - lo[0]) / (hi[0] - lo[0])
            c = lo[1] + (hi[1] - lo[1]) * t
        closes.append(c)

    kl = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i > 0 else c
        kl.append(Kline(openTime=_BASE_TS + i * _HOUR, open=o,
                        high=max(o, c) * 1.0008, low=min(o, c) * 0.9992,
                        close=c, volume=1000.0,
                        closeTime=_BASE_TS + (i + 1) * _HOUR - 1,
                        quoteVolume=100000.0))
    for idx, price, ptype in anchors:
        k = kl[idx]
        if ptype == PivotType.HIGH:
            k.high = price
            k.close = max(k.close, price * 0.995)
        else:
            k.low = price
            k.close = min(k.close, price * 1.005)
    return kl


def _pivots(anchors):
    return [Pivot(index=i, price=p, type=t, timestamp=_BASE_TS + i * _HOUR)
            for i, p, t in anchors]


def _detector(gap, **over):
    p = {
        "peak_tolerance": 0.03, "peak_tolerance_min": 0.03,
        "peak_tolerance_max": 0.05, "tol_span_lo": 30, "tol_span_hi": 200,
        "min_depth": 0.03, "min_span": 30, "max_span": 350,
        "min_pivot_gap": gap,
        "peak_overshoot_max": 0.02, "trough_undershoot_max": 0.03,
        "breakout_candles": 2, "breakout_atr_ratio": 0.5,
        "volume_ratio_min": 1.5, "min_height_atr": 1.0,
        "require_prior_trend": True, "prior_trend_bars": 20,
        "prior_trend_min_move": 0.05, "pullback_bars": 0,
    }
    p.update(over)
    return DoubleTopBottomDetector(p)


def main():
    print("=" * 76)
    print("TAOUSDT 1h 双顶 相邻锚点间隔 — 回归测试")
    print("=" * 76)

    # ---- 双顶: 峰1(25) 谷(57) 峰2(61) → gap = (32, 4) ----
    A_TOP = [(25, 276.90, PivotType.HIGH), (57, 249.20, PivotType.LOW),
             (61, 269.78, PivotType.HIGH)]
    SK_TOP = [(0, 218.0), (5, 222.0), (25, 276.9), (57, 249.2), (61, 269.78),
              (70, 256.0), (85, 246.0), (109, 236.0)]
    kl = _build_klines(A_TOP, 110, SK_TOP)
    pv = _pivots(A_TOP)
    atr = 3.7
    print("\n[场景] 合成 TAO 锚点: 峰1(25, 276.90) 谷(57, 249.20) 峰2(61, 269.78)")
    print("       相邻间隔 = (32, 4), span = 36, 两峰价差 = 2.57%")

    print("\nA. min_pivot_gap=0 (关闭约束, 等价改动前)")
    r0 = _detector(0)._check_double_top(pv[0], pv[1], pv[2], kl, atr, "TAOUSDT", "1h")
    _check("该形态可被检出(非其它闸拦截)", r0 is not None,
           "result=%s" % (r0.pattern_type if r0 else "None"))

    print("\nB. min_pivot_gap=5 (本次落地值)")
    r5 = _detector(5)._check_double_top(pv[0], pv[1], pv[2], kl, atr, "TAOUSDT", "1h")
    _check("该形态被 min_pivot_gap 拦下", r5 is None,
           "result=%s" % (r5.pattern_type if r5 else "None(拦截)"))

    print("\nC. 真实 config.yaml 读取")
    with open(os.path.join(_ROOT, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    v = cfg.get("patterns", {}).get("span", {}).get("double_min_pivot_gap")
    _check("config.patterns.span.double_min_pivot_gap == 5", v == 5, "实际=%s" % v)

    print("\nD. min_pivot_gap=4 (放宽到等于畸形间隔)")
    r4 = _detector(4)._check_double_top(pv[0], pv[1], pv[2], kl, atr, "TAOUSDT", "1h")
    _check("放宽到 4 后又能检出(证明拦的就是那 4 根)", r4 is not None,
           "result=%s" % (r4.pattern_type if r4 else "None"))

    print("\nE. 相邻间隔 = 5 (边界)")
    A5 = [(25, 276.90, PivotType.HIGH), (57, 249.20, PivotType.LOW),
          (62, 269.78, PivotType.HIGH)]
    kl5 = _build_klines(A5, 112, SK_TOP)
    pv5 = _pivots(A5)
    r5b = _detector(5)._check_double_top(pv5[0], pv5[1], pv5[2], kl5, atr,
                                         "TAOUSDT", "1h")
    _check("间隔=5 时保留(阈值边界不误伤)", r5b is not None,
           "result=%s" % (r5b.pattern_type if r5b else "None"))

    print("\nF. 双底场景(对称性)")
    A_BOT = [(25, 220.00, PivotType.LOW), (57, 260.00, PivotType.HIGH),
             (61, 224.00, PivotType.LOW)]
    SK_BOT = [(0, 300.0), (5, 296.0), (25, 220.0), (57, 260.0), (61, 224.0),
              (70, 240.0), (85, 252.0), (109, 262.0)]
    klb = _build_klines(A_BOT, 110, SK_BOT)
    pvb = _pivots(A_BOT)
    rb5 = _detector(5)._check_double_bottom(pvb[0], pvb[1], pvb[2], klb, atr,
                                            "TAOUSDT", "1h")
    rb0 = _detector(0)._check_double_bottom(pvb[0], pvb[1], pvb[2], klb, atr,
                                            "TAOUSDT", "1h")
    _check("双底 gap=4 被拦下", rb5 is None,
           "result=%s" % (rb5.pattern_type if rb5 else "None(拦截)"))
    _check("双底 gap=4 在关闭约束时可检出(证明是同一闸)", rb0 is not None,
           "result=%s" % (rb0.pattern_type if rb0 else "None"))

    print("\n" + "=" * 76)
    if _fails:
        print("失败 %d 项: %s" % (len(_fails), ", ".join(_fails)))
        sys.exit(1)
    print("全部通过 ✓")


if __name__ == "__main__":
    main()
