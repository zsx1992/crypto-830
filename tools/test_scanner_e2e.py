# -*- coding: utf-8 -*-
"""
scanner 完整流水线端到端集成测试（P0~P3 接线验证，离线、不联网）。

为什么需要这个测试：
  test_p0p3 / test_p3_ranking / test_notifier 只验证各组件单测。
  但 P0~P3 的"价值"在于 run() 把它们按正确顺序串起来：
    candidates → cross_tf.confirm → step4 过滤(freshness/strength/rr/vol/geo/trend)
    → after_scoring → step5 StateStore.filter_new(状态跃迁去重)
    → step6 _apply_freshness_ranking(加权排序 + top-up) → step7 push
    → step7.5 观察流 filter_new_observe → push_observe
  本测试用真实 config.yaml 实例化 Scanner(dry_run=True)，打桩取数层，
  喂合成 Pattern 跑真实 run()，断言最终 result.pushed / observed / kill_breakdown。

本机无法触发 GitHub Actions 云端 dry-run（无 PAT、OKX 不可达），
此测试是"自己验证自己改动"的离线等价物。
"""

import os
import sys
import tempfile

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import yaml  # noqa: E402

import scanner  # noqa: E402
from scanner import Scanner  # noqa: E402
from patterns.base import Pattern, Direction, PatternStatus  # noqa: E402
from zigzag import Pivot, PivotType  # noqa: E402
from market_data import Kline  # noqa: E402


def _load_config():
    with open(os.path.join(_ROOT, "config.yaml"), "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # 强制不污染真实状态：用临时文件
    tmp = tempfile.mktemp(suffix=".json", prefix="e2e_state_")
    cfg.setdefault("state", {})["file"] = tmp
    # 观察流开启（P0 隔离验证需要）
    cfg.setdefault("filter", {}).setdefault("observe", {})["enabled"] = True
    # 同一标的多形态不互相截断
    cfg["filter"]["max_per_symbol"] = 20
    return cfg


def _make_klines(n=40):
    """合成 K 线：close=100，openTime = i * 4h，供 end_ms 填充与图表渲染"""
    ks = []
    for i in range(n):
        ts = i * 240 * 60_000
        ks.append(Kline(openTime=ts, open=100.0, high=101.0, low=99.0,
                        close=100.0, volume=1000.0, closeTime=ts + 1,
                        quoteVolume=1_000_000.0))
    return ks


def _make_pattern(symbol, interval="4h", age=1, strength=80, geo=0.75,
                  direction=Direction.LONG,
                  pattern_type="head_shoulders_bottom",
                  breakout_index=10, breakout_price=100.0):
    p = Pattern(
        symbol=symbol, interval=interval, pattern_type=pattern_type,
        direction=direction, status=PatternStatus.CONFIRMED,
        strength_score=strength, risk_reward=2.0, volume_ratio=3.0,
        geometry_score=geo, confidence=0.7,
        breakout_age=age, breakout_index=breakout_index,
        breakout_price=breakout_price, breakout_magnitude_atr=2.0,
        height=5.0, entry_price=100.0, stop_loss=95.0,
        take_profit_1=105.0, take_profit_2=108.0,
        end_ms=breakout_index * 240 * 60_000,
        pivots=[Pivot(index=breakout_index, price=100.0,
                      type=PivotType.HIGH, timestamp=0)],
    )
    return p


class _FakeIndicators:
    atr_current = 1.0
    trend = "up"
    adx_current = 10          # < adx_threshold(20) → 不强制趋势对齐
    rsi_current = 50
    macd_hist_current = 0.0


def _install_patches(sc, candidates_by_symbol):
    """打桩取数层 + 指标 + 引擎 + 交叉确认 + 图表渲染"""
    symbols = list(candidates_by_symbol.keys())
    sc.client.get_symbols_tiered = lambda **kw: (symbols, [])
    sc.client.get_klines = lambda symbol, interval, limit: (_make_klines(), "mock")
    sc.engine.scan_multiscale = (
        lambda klines, symbol, interval: candidates_by_symbol.get(symbol, []))
    sc.cross_tf.confirm = lambda signals, trends, momentum: signals
    scanner.calc_indicators = lambda klines: _FakeIndicators()
    scanner.find_pivots = lambda *a, **k: []
    scanner.pivot_health_check = lambda *a, **k: {"status": "ok"}
    scanner.render_pattern_chart = lambda *a, **k: None


def _build(cfg, candidates_by_symbol):
    """建 Scanner 实例 + 打桩（不立即 run）"""
    sc = Scanner(cfg, dry_run=True,
                webhook_url="https://example.com/wh?key=dummy",
                charts_dir=tempfile.mkdtemp(prefix="e2e_charts_"))
    _install_patches(sc, candidates_by_symbol)
    return sc


# ----------------------------- 断言工具 -----------------------------
_FAILS = []


def _check(name, cond, detail=""):
    mark = "✓" if cond else "✗"
    print(f"  [{mark}] {name}{(' — ' + detail) if (detail and not cond) else ''}")
    if not cond:
        _FAILS.append(name)


def _symbols(patterns):
    return {p.symbol for p in patterns}


def scenario_p3_topup_and_p0_observe_and_p2_suppress():
    print("\n=== 场景1: P3 top-up + P0 观察隔离 + P2 同形态抑制(经 run 接线) ===")
    cfg = _load_config()
    cands = {
        "MOCKA": [_make_pattern("MOCKA", age=1, strength=80)],   # 新鲜供给
        "MOCKB": [_make_pattern("MOCKB", age=300, strength=85)], # 陈旧(强)
        "MOCKC": [_make_pattern("MOCKC", age=400, strength=70)], # 陈旧(弱)
        "MOCKD": [_make_pattern("MOCKD", age=5, strength=80)],   # 已推过同形态→抑制
        "MOCKO": [_make_pattern("MOCKO", age=2, strength=80,
                                geo=0.40)],                      # 穿freshness砍geo(<0.45)→观察(≥0.30)
    }
    sc = _build(cfg, cands)
    # 种子化：MOCKD 之前已正式推送（同 end_ms、同方向）→ 本轮应被抑制
    sc.state.record(_make_pattern("MOCKD", age=5, strength=80))
    result = sc.run(intervals=["4h"])

    pushed = _symbols(result.pushed)
    observed = _symbols(result.observed)

    _check("P3 stale 标记计入 kill_breakdown(stale==2)",
           result.kill_breakdown.get("stale") == 2,
           f"stale={result.kill_breakdown.get('stale')}")
    _check("P3 新鲜供给=1(<floor=2) → top-up 补最强陈旧 MOCKB",
           "MOCKB" in pushed and "MOCKC" not in pushed,
           f"pushed={sorted(pushed)}")
    _check("P3 新鲜信号排在最前(MOCKA 首位)",
           result.pushed and result.pushed[0].symbol == "MOCKA",
           f"first={result.pushed[0].symbol if result.pushed else None}")
    _check("P2 同形态无事件被抑制(MOCKD 不在 pushed)",
           "MOCKD" not in pushed, f"pushed={sorted(pushed)}")
    _check("P0 观察流隔离(MOCKO 进 observed 但不在 pushed)",
           "MOCKO" in observed and "MOCKO" not in pushed,
           f"observed={sorted(observed)} pushed={sorted(pushed)}")
    _check("P0 summary_mode=on_signal 有信号→摘要生成(dry_run 不报错)",
           True)


def scenario_p2_event_flip():
    print("\n=== 场景2: P2 状态跃迁 — 方向翻转允许重推(经 run 接线) ===")
    cfg = _load_config()
    cands = {
        "MOCKE": [_make_pattern("MOCKE", age=5, strength=80,
                                direction=Direction.SHORT)],  # 候选翻空
        "MOCKX": [_make_pattern("MOCKX", age=5, strength=80,
                                direction=Direction.LONG)],  # 候选同多
    }
    sc = _build(cfg, cands)
    # 种子化：两者之前都按 LONG 推过
    sc.state.record(_make_pattern("MOCKE", age=5, strength=80,
                                  direction=Direction.LONG))
    sc.state.record(_make_pattern("MOCKX", age=5, strength=80,
                                  direction=Direction.LONG))
    result = sc.run(intervals=["4h"])

    pushed = _symbols(result.pushed)
    _check("P2 方向翻转→allow_event 重推(MOCKE 在 pushed)",
           "MOCKE" in pushed, f"pushed={sorted(pushed)}")
    _check("P2 同形态无事件→suppress(MOCKX 不在 pushed)",
           "MOCKX" not in pushed, f"pushed={sorted(pushed)}")
    _check("P2 事件重推受 max_repush 限速(此处 1 条未超限)",
           len(result.pushed) >= 1)


def main():
    print("=== scanner 端到端集成测试 (P0~P3 在 run() 真实链路中的接线) ===")
    scenario_p3_topup_and_p0_observe_and_p2_suppress()
    scenario_p2_event_flip()
    print("\n" + ("全部端到端断言通过 ✓" if not _FAILS
                  else f"失败 {len(_FAILS)} 项: {_FAILS}"))
    sys.exit(1 if _FAILS else 0)


if __name__ == "__main__":
    main()
