# -*- coding: utf-8 -*-
"""
回归测试: 头肩顶"相邻锚点最小间隔"(min_pivot_gap)。

金标准来源 (2026-09-11):
  朱哥截图 CRVUSDT 1h 头肩顶, 反馈"标记的进场点位不理想, 所选几根K线彼此
  距离太近, 缺乏参考意义"。像素反解 5 个锚点 (@1h, 1.7765 px/根):
    LS  x=611 → idx 0   (0.3905)
    N1  x=633 → idx 12  (0.3602)   LS-N1  = 12
    Head x=653 → idx 24 (0.4056)   N1-Hd  = 12
    N2  x=717 → idx 60  (0.3551)   Hd-N2  = 36
    RS  x=721 → idx 62  (0.3726)   N2-RS  =  2  ← 病灶
  右颈锚 N2 与右肩 RS 只隔 2 根 → 右半形态压成 V 型急拉。

  注意: "颈线两锚点" N1<->N2 隔了 48 根, 本身不近 —— 要拦的是相邻锚点。

断言:
  A. 关闭约束(min_pivot_gap=0) 时该形态能被检出(证明不是被别的闸拦)
  B. 开启约束(min_pivot_gap=4) 时该形态被拦
  C. 真实 config 读到的 head_shoulders_min_gap == 4
  D. 放宽到 2 时又能检出(证明拦的就是那 2 根)
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

import yaml  # noqa: E402
from market_data import Kline  # noqa: E402
from zigzag import Pivot, PivotType  # noqa: E402
from patterns.head_shoulders import HeadShouldersDetector  # noqa: E402

_fails = []


def _check(name, cond, detail=""):
    mark = "✓" if cond else "✗"
    print("  %s %s%s" % (mark, name, (" | " + detail) if detail else ""))
    if not cond:
        _fails.append(name)


# ---- 构造 CRV 合成场景 ----
# 锚点 (index, price, type)
_ANCHORS = [
    (30, 0.3905, PivotType.HIGH),   # LS
    (42, 0.3602, PivotType.LOW),    # N1
    (54, 0.4056, PivotType.HIGH),   # Head
    (90, 0.3551, PivotType.LOW),    # N2
    (92, 0.3726, PivotType.HIGH),   # RS
]
# 相对偏移 → 保证 span=62, gap=(12,12,36,2)
_N = 150


def _build_klines():
    """构造一条能过所有前置闸的 K 线序列, 只在 N2-RS 间隔上畸形。"""
    # 分段线性价格骨架
    pts = [(0, 0.2790), (20, 0.3300), (30, 0.3905), (42, 0.3602),
           (54, 0.4056), (72, 0.3780), (90, 0.3551), (91, 0.3660),
           (92, 0.3726), (100, 0.3500), (110, 0.3350), (149, 0.3200)]
    closes = []
    for i in range(_N):
        # 找当前所在段
        lo = pts[0]
        hi = pts[-1]
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
        hi = max(o, c) * 1.0008
        lo = min(o, c) * 0.9992
        kl.append(Kline(openTime=1_700_000_000_000 + i * 3_600_000,
                        open=o, high=hi, low=lo, close=c,
                        volume=1000.0,
                        closeTime=1_700_000_000_000 + (i + 1) * 3_600_000 - 1,
                        quoteVolume=100000.0))
    # 把锚点索引处的极值钉到位（Pivot 的 price 必须与 K 线一致）
    for idx, price, ptype in _ANCHORS:
        k = kl[idx]
        if ptype == PivotType.HIGH:
            k.high = price
            k.close = max(k.close, price * 0.995)
        else:
            k.low = price
            k.close = min(k.close, price * 1.005)
    return kl


def _pivots():
    return [Pivot(index=i, price=p, type=t,
                  timestamp=1_700_000_000_000 + i * 3_600_000)
            for i, p, t in _ANCHORS]


def _detector(min_gap):
    return HeadShouldersDetector({
        "shoulder_tolerance": 0.05,
        "neck_tolerance": 0.05,
        "head_prominence": 0.03,
        "min_span": 35,
        "max_span": 420,
        "min_pivot_gap": min_gap,
        "require_prior_trend": True,
        "prior_trend_bars": 20,
        "prior_trend_min_move": 0.05,
    })


def main():
    print("=" * 74)
    print("CRVUSDT 1h 头肩顶相邻锚点间隔 — 回归测试")
    print("=" * 74)
    kl = _build_klines()
    pv = _pivots()
    atr = 0.004

    print("\n[场景] 合成 CRV 锚点: LS(30) N1(42) Head(54) N2(90) RS(92)")
    print("       相邻间隔 = (12, 12, 36, 2), span = 62")

    print("\nA. min_pivot_gap=0 (关闭约束, 等价改动前)")
    d0 = _detector(0)
    r0 = d0._check_hs_top(pv, kl, atr, "CRVUSDT", "1h")
    _check("该形态可被检出(非其它闸拦截)", r0 is not None,
           "pattern=%s" % (r0.pattern_type if r0 else None))

    print("\nB. min_pivot_gap=4 (本次落地值)")
    d4 = _detector(4)
    r4 = d4._check_hs_top(pv, kl, atr, "CRVUSDT", "1h")
    _check("该形态被 min_pivot_gap 拦下", r4 is None,
           "result=%s" % (r4.pattern_type if r4 else "None(拦截)"))

    print("\nC. 真实 config.yaml 读取")
    with open(os.path.join(_ROOT, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    v = cfg.get("patterns", {}).get("span", {}).get("head_shoulders_min_gap")
    _check("config.patterns.span.head_shoulders_min_gap == 4", v == 4,
           "实际=%s" % v)

    print("\nD. min_pivot_gap=2 (放宽到等于畸形间隔)")
    d2 = _detector(2)
    r2 = d2._check_hs_top(pv, kl, atr, "CRVUSDT", "1h")
    _check("放宽到 2 后又能检出(证明拦的就是那 2 根)", r2 is not None,
           "result=%s" % (r2.pattern_type if r2 else "None"))

    print("\n" + "=" * 74)
    if _fails:
        print("失败 %d 项: %s" % (len(_fails), ", ".join(_fails)))
        sys.exit(1)
    print("全部通过 ✓")


if __name__ == "__main__":
    main()
