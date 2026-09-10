# -*- coding: utf-8 -*-
"""P3 加权新鲜度排序 + top-up 逻辑验证（离线）"""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from patterns.base import Direction
from scanner import Scanner

CFG_W = {
    "data_source": {"primary": "binance"},
    "filter": {"dedup": {"event_repush": True, "max_repush_per_run": 6},
               "freshness_mode": "weighted", "freshness_topup_floor": 2,
               "max_per_run": 20},
    "state": {"file": ":memory:", "cleanup_days": 7},
    "notification": {"push_rate_limit_per_minute": 18, "send_summary": True,
                     "summary_mode": "on_signal"},
    "scan": {}, "throttling": {},
}
CFG_H = dict(CFG_W)
CFG_H["filter"] = dict(CFG_W["filter"])
CFG_H["filter"]["freshness_mode"] = "hard"


class FP:
    def __init__(self, sym, strength, stale=False, rr=1.0):
        self.symbol = sym
        self.pattern_type = "double_bottom"
        self.interval = "4h"
        self.direction = Direction.LONG
        self.strength_score = strength
        self.risk_reward = rr
        self.is_stale = stale
        self.end_ms = 0


def kinds(lst):
    return [f"{p.symbol}(s{p.strength_score},"
            f"{'旧' if p.is_stale else '新'})" for p in lst]


print("=== P3 加权新鲜度排序/top-up ===")
s_w = Scanner(CFG_W, dry_run=True)
s_h = Scanner(CFG_H, dry_run=True)

# 场景1: 新鲜充足(3新) + 2陈旧 -> 丢弃陈旧, 仅留新鲜按强度降序
inp1 = [FP("A", 60, stale=False), FP("B", 80, stale=False),
        FP("C", 70, stale=False), FP("X", 90, stale=True),
        FP("Y", 85, stale=True)]
out1 = s_w._apply_freshness_ranking(inp1)
assert all(not p.is_stale for p in out1), "新鲜充足时应丢弃全部陈旧"
assert [p.symbol for p in out1] == ["B", "C", "A"], f"应按强度降序: {kinds(out1)}"
print(f"  场景1 新鲜充足丢弃陈旧 ✓ -> {kinds(out1)}")

# 场景2: 新鲜不足(1新) + 3陈旧, floor=2 -> 补最强1个陈旧, 共2
inp2 = [FP("A", 60, stale=False), FP("X", 90, stale=True),
        FP("Y", 85, stale=True), FP("Z", 70, stale=True)]
out2 = s_w._apply_freshness_ranking(inp2)
assert len(out2) == 2, f"应补到 floor=2: {kinds(out2)}"
assert out2[0].symbol == "A" and not out2[0].is_stale, "新鲜信号必须排第一"
assert out2[1].symbol == "X" and out2[1].is_stale, f"补最强陈旧X: {kinds(out2)}"
print(f"  场景2 新鲜不足补最强陈旧 ✓ -> {kinds(out2)}")

# 场景3: hard 模式 -> 不丢陈旧, 全按强度降序
inp3 = [FP("A", 60, stale=False), FP("X", 90, stale=True),
        FP("Y", 85, stale=True)]
out3 = s_h._apply_freshness_ranking(inp3)
assert len(out3) == 3, "hard 模式应保留全部"
assert [p.symbol for p in out3] == ["X", "Y", "A"], f"hard 按强度降序: {kinds(out3)}"
print(f"  场景3 hard 保留全部 ✓ -> {kinds(out3)}")

print("\nP3 排序/top-up 断言全部通过 ✓")
