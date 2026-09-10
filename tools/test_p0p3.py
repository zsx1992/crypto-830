# -*- coding: utf-8 -*-
"""
P0~P3 改动验证（离线，不触发真实推送/不读写 state.json）

1) box 水平判定跨度感知 (P1)：
   - 水平箱体 → rectangle
   - 双向同斜带(旧"斜矩形") → ascending_channel，绝不为 rectangle
   - 一平一斜 → 拒（交给三角形）
2) state_store 状态跃迁去重 (P2)：
   - 无记录 → allow_new
   - 同形态无事件 → suppress（旧行为，安静期仍0推送，符合预期）
   - 方向翻转 / 突破延伸 / 强度跳升 / 突破价位移 → allow_event
   - 不同形态 → cooldown 或 allow_new
   - reconfirm 周期重推开关
3) 加权新鲜度兜底逻辑 (P3) 在 scanner 内，单独用小脚本验证逻辑分支（见文末）
"""
import os
import sys
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from market_data import Kline
from zigzag import Pivot, PivotType
from patterns.box import BoxDetector
from patterns.base import Direction
from state_store import StateStore

logging.basicConfig(level=logging.ERROR)


# ---------------- 合成 K 线 ----------------
def build_band(n, upper_fn, lower_fn, atr, breakout_above=None):
    klines = []
    for i in range(n):
        if breakout_above and i >= 61:
            price = breakout_above + 5.0
            klines.append(Kline(openTime=i * 3_600_000, open=price,
                                high=price + 0.2, low=price - 0.2,
                                close=price, volume=1000, closeTime=0,
                                quoteVolume=1000))
            continue
        u = upper_fn(i)
        l = lower_fn(i)
        close = (u + l) / 2.0
        klines.append(Kline(openTime=i * 3_600_000, open=close,
                            high=u + 0.2, low=l - 0.2, close=close,
                            volume=1000, closeTime=0, quoteVolume=1000))
    return klines


def make_pivots(idxs, upper_fn, lower_fn):
    piv = []
    for i in idxs:
        piv.append(Pivot(index=i, price=upper_fn(i), type=PivotType.HIGH))
        piv.append(Pivot(index=i, price=lower_fn(i), type=PivotType.LOW))
    return piv


IDX = [10, 20, 30, 40, 50, 60]


def rect_case():
    up = lambda i: 120.0
    lo = lambda i: 100.0
    k = build_band(70, up, lo, atr=1.0, breakout_above=120.0)
    pv = make_pivots(IDX, up, lo)
    return k, pv


def slant_case():
    up = lambda i: 110.0 + (i - 10) * 0.66   # 10->110, 60->143
    lo = lambda i: 100.0 + (i - 10) * 0.60   # 10->100, 60->130
    k = build_band(70, up, lo, atr=1.0, breakout_above=143.0)
    pv = make_pivots(IDX, up, lo)
    return k, pv


def mixed_case():
    # 上边界水平, 下边界斜 -> 一平一斜, 应拒绝
    up = lambda i: 120.0
    lo = lambda i: 100.0 + (i - 10) * 0.60
    k = build_band(70, up, lo, atr=1.0, breakout_above=120.0)
    pv = make_pivots(IDX, up, lo)
    return k, pv


def run_box(name, k, pv):
    det = BoxDetector()
    res = det.detect(k, pv, atr_value=1.0, symbol="TESTUSDT", interval="4h")
    kinds = sorted({r.pattern_type for r in res})
    print(f"  [{name}] 检出 {len(res)} 个, 类型={kinds}")
    return kinds


print("=== P1 box 水平判定 ===")
rk = run_box("水平箱体", *rect_case())
sk = run_box("斜带(旧斜矩形)", *slant_case())
mk = run_box("一平一斜", *mixed_case())

assert "rectangle" in rk, "水平箱体应判 rectangle"
assert "rectangle" not in sk, "斜带绝不应判 rectangle（应转 channel）"
assert "ascending_channel" in sk, "斜带应判 ascending_channel"
assert len(mk) == 0, "一平一斜应拒绝(交给三角形)"
print("  P1 ✓ 水平判定跨度感知生效")


# ---------------- state_store 去重 ----------------
class FakePattern:
    def __init__(self, symbol, ptype, interval, direction,
                 end_ms=0, breakout_index=0, breakout_price=100.0,
                 strength=60):
        self.symbol = symbol
        self.pattern_type = ptype
        self.interval = interval
        self.direction = direction
        self.end_ms = end_ms
        self.breakout_index = breakout_index
        self.breakout_price = breakout_price
        self.strength_score = strength


print("\n=== P2 state_store 状态跃迁去重 ===")
ss = StateStore(state_path=":memory:", dedup_cfg={
    "event_repush": True, "event_breakout_advance": 2,
    "event_strength_delta": 8, "event_price_delta": 0.02,
    "reconfirm_cooldown_hours": 0, "max_repush_per_run": 6})

# 1) 无记录
p = FakePattern("AAA", "double_bottom", "4h", Direction.LONG, end_ms=1000)
assert ss.dedup_check(p, []) == "allow_new"
print("  无记录 -> allow_new ✓")

# 2) 记录一条 same_form, 无事件 -> suppress
rec = ss._make_record(p)
recs = [rec]
p2 = FakePattern("AAA", "double_bottom", "4h", Direction.LONG, end_ms=1000)
assert ss.dedup_check(p2, recs) == "suppress", "同形态无事件应抑制"
print("  同形态无事件 -> suppress ✓ (安静期仍0推送, 符合设计)")

# 3) 方向翻转 -> allow_event
p3 = FakePattern("AAA", "double_bottom", "4h", Direction.SHORT, end_ms=1000)
assert ss.dedup_check(p3, recs) == "allow_event"
print("  方向翻转 -> allow_event ✓")

# 4) 突破延伸 >=2 -> allow_event
p4 = FakePattern("AAA", "double_bottom", "4h", Direction.LONG, end_ms=1000,
                 breakout_index=rec.get("breakoutIndex", 0) + 3)
assert ss.dedup_check(p4, recs) == "allow_event"
print("  突破延伸 -> allow_event ✓")

# 5) 强度跳升 >=8 -> allow_event
p5 = FakePattern("AAA", "double_bottom", "4h", Direction.LONG, end_ms=1000,
                 strength=rec.get("strength", 60) + 10)
assert ss.dedup_check(p5, recs) == "allow_event"
print("  强度跳升 -> allow_event ✓")

# 6) 突破价偏移 >=2% -> allow_event
p6 = FakePattern("AAA", "double_bottom", "4h", Direction.LONG, end_ms=1000,
                 breakout_price=rec.get("breakoutPrice", 100.0) * 1.03)
assert ss.dedup_check(p6, recs) == "allow_event"
print("  突破价位移 -> allow_event ✓")

# 7) 不同形态(end_ms 远超 2 根容差) + 冷却仍活跃 -> cooldown
p7 = FakePattern("AAA", "double_bottom", "4h", Direction.LONG,
                 end_ms=rec.get("endMs", 1000) + 100_000_000)
assert ss.dedup_check(p7, recs) == "cooldown"
print("  不同形态(冷却活跃) -> cooldown ✓")

# 7b) 不同形态 + 冷却已过期 -> allow_new
rec_old = dict(rec)
rec_old["cooldownUntil"] = "2000-01-01T00:00:00+00:00"
# 保持原 endMs(与 p7 不同) -> 视为不同形态, 冷却已过 -> allow_new
assert ss.dedup_check(p7, [rec_old]) == "allow_new"
print("  不同形态(冷却已过) -> allow_new ✓")

# 8) reconfirm 开关（真实流转：首次 allow_new → 窗口内 suppress → 过期 allow_event）
ss2 = StateStore(state_path=":memory:", dedup_cfg={
    "event_repush": True, "event_breakout_advance": 2,
    "event_strength_delta": 8, "event_price_delta": 0.02,
    "reconfirm_cooldown_hours": 48, "max_repush_per_run": 6})
assert ss2.dedup_check(p, []) == "allow_new"          # 首次推送
ss2.record(p)                                          # 记录含 reconfirmAt=now+48h
assert ss2.dedup_check(p, ss2.state["pushedSignals"]) == "suppress"  # 窗口内
ss2.state["pushedSignals"][-1]["reconfirmAt"] = "2000-01-01T00:00:00+00:00"
assert ss2.dedup_check(p, ss2.state["pushedSignals"]) == "allow_event"  # 过期
print("  reconfirm_cooldown_hours=48 -> 首充allow_new/窗口内suppress/过期allow_event ✓")

# 9) filter_new 返回 (fresh_new, events) 元组
fresh, events = ss.filter_new([p, p3, p4])
assert isinstance(fresh, list) and isinstance(events, list)
print(f"  filter_new 返回元组 ✓ (fresh={len(fresh)}, events={len(events)})")

print("\n全部 P1/P2 断言通过 ✓")
