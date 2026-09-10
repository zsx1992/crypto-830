# -*- coding: utf-8 -*-
"""P0 notifier 验证（离线）：观察流独立 webhook 路由 + 陈旧信号标注"""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from patterns.base import Direction, PatternStatus
from notifier import WeComNotifier


class FP:
    def __init__(self, stale=False):
        self.symbol = "TESTUSDT"
        self.pattern_type = "double_bottom"
        self.interval = "4h"
        self.direction = Direction.LONG
        self.strength_score = 62
        self.risk_reward = 1.5
        self.entry_price = 100.0
        self.stop_loss = 95.0
        self.take_profit_1 = 108.0
        self.take_profit_2 = 115.0
        self.height = 10.0
        self.breakout_price = 101.0
        self.breakout_magnitude_atr = 2.0
        self.volume_ratio = 2.0
        self.breakout_age = 50
        self.resonant_with = []
        self.candle_confirmations = []
        self.is_stale = stale
        self.neckline = None
        self.upper_boundary = None
        self.lower_boundary = None
        self.geometry_score = None


print("=== P0 notifier 观察流隔离 + 陈旧标注 ===")

# 1) 观察流有独立 webhook -> 发到观察 url
calls = []
n = WeComNotifier(webhook_url="https://main.example/key=MAIN",
                  observe_webhook_url="https://obs.example/key=OBS",
                  dry_run=False)
n._post = lambda payload, retry=2, url=None: (calls.append(url) or True)
ok = n.push_observe(FP(), None, "strength")
assert ok is True
assert calls and calls[0] == "https://obs.example/key=OBS", \
    f"观察流应发到独立 webhook, 实际: {calls}"
print("  观察流独立 webhook 路由 ✓ (未污染主群)")

# 2) 无独立 webhook 且 fallback=False -> 放弃(返回 False, 不推送)
calls.clear()
n2 = WeComNotifier(webhook_url="https://main.example/key=MAIN",
                   observe_webhook_url=None, observe_fallback_same=False,
                   dry_run=False)
n2._post = lambda payload, retry=2, url=None: (calls.append(url) or True)
ok2 = n2.push_observe(FP(), None, "strength")
assert ok2 is False and not calls, "无观察 webhook 时应放弃且不推送(保持主群干净)"
print("  无独立 webhook -> 暂停观察(主群干净) ✓")

# 3) 无独立 webhook 但 fallback=True -> 退回主群
calls.clear()
n3 = WeComNotifier(webhook_url="https://main.example/key=MAIN",
                   observe_webhook_url=None, observe_fallback_same=True,
                   dry_run=False)
n3._post = lambda payload, retry=2, url=None: (calls.append(url) or True)
ok3 = n3.push_observe(FP(), None, "strength")
assert ok3 is True and calls and calls[0] == "https://main.example/key=MAIN"
print("  fallback=True -> 退回主群 ✓")

# 4) 陈旧信号标注
n4 = WeComNotifier(dry_run=True)
fresh_md = n4.build_markdown(FP(stale=False))
stale_md = n4.build_markdown(FP(stale=True))
assert "⚠️" not in fresh_md["markdown"]["content"]
assert "⚠️" in stale_md["markdown"]["content"], "陈旧信号应有提示标注"
assert "形态仍在" in stale_md["markdown"]["content"]
print("  陈旧信号标注 ✓")

print("\nP0 notifier 断言全部通过 ✓")
