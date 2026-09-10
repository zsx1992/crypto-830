# -*- coding: utf-8 -*-
"""
扫描编排器 —— 串起完整链路

  取数 → 指标 → ZigZag → 形态识别 → 多周期交叉确认
       → 强度过滤 → 去重 → 图表渲染 → 企微推送 → 状态持久化

这是阶段 3 的核心：把前几个阶段的模块组装成可运行的完整流程。
"""

import os
import sys
import time
import logging
from dataclasses import dataclass, field
from typing import Dict
from typing import List, Dict, Optional, Tuple

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from market_data import MarketDataClient, Kline
from indicators import calc_indicators
from zigzag import find_pivots, pivot_health_check
from detector import PatternEngine
from crosstf import CrossTimeframeConfirm, interval_rank
from state_store import StateStore
from notifier import WeComNotifier
from chart import render_pattern_chart
from patterns.base import Pattern, PatternStatus, Direction

logger = logging.getLogger(__name__)


@dataclass
class ScanResult:
    """一次完整扫描的结果"""
    symbols: List[str] = field(default_factory=list)
    scanned_pairs: int = 0
    failed_pairs: int = 0
    candidates: List[Pattern] = field(default_factory=list)
    confirmed: List[Pattern] = field(default_factory=list)
    after_scoring: List[Pattern] = field(default_factory=list)
    after_dedup: List[Pattern] = field(default_factory=list)
    pushed: List[Pattern] = field(default_factory=list)
    # 2026-09-08: 观察流推送的信号（穿过 freshness 但被后续闸砍的边界样本，
    # 供人眼验证攒金标准样本；与正式 pushed 隔离）。
    observed: List[Pattern] = field(default_factory=list)
    # 2026-09-07: 各过滤闸门被砍计数（6 道闸: fresh/strength/rr/vol/geo/trend）
    # 用于摘要漏斗把 0 推送时的责任细化到单道闸，避免读 Actions 日志。
    kill_breakdown: Dict[str, int] = field(default_factory=dict)
    duration_sec: float = 0.0
    errors: List[str] = field(default_factory=list)
    health_stats: Dict[str, int] = field(default_factory=dict)
    source_stats: Dict[str, int] = field(default_factory=dict)
    # 2026-09-07: 分层标的池统计 {tier: {"symbols","scanned","confirmed"}}，
    # 长尾池只扫大周期，用这个判断它对信号供给的真实贡献。
    tier_stats: Dict[str, Dict[str, int]] = field(default_factory=dict)


class Scanner:
    """完整扫描流程"""

    def __init__(self, config: dict, dry_run: bool = False,
                 webhook_url: Optional[str] = None,
                 charts_dir: str = "output/charts",
                 verbose: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.charts_dir = charts_dir
        self.verbose = verbose

        ds = config.get("data_source", {})
        self.client = MarketDataClient(
            primary=ds.get("primary", "binance"),
            timeout=ds.get("timeout_seconds", 30),
            retry_max=ds.get("retry_max", 3),
            backoff_base=ds.get("retry_backoff_base", 2.0),
        )

        self.engine = PatternEngine(config)
        self.cross_tf = CrossTimeframeConfirm(config)

        filt = config.get("filter", {})
        st = config.get("state", {})
        self.dedup_cfg = filt.get("dedup", {})
        self.state = StateStore(
            state_path=st.get("file", "state/state.json"),
            cooldown_minutes=filt.get("cooldown_minutes"),
            cleanup_days=st.get("cleanup_days", 7),
            dedup_cfg=self.dedup_cfg,
        )
        # P2/P3 派生参数
        self.max_repush = int(self.dedup_cfg.get("max_repush_per_run", 6))
        self.freshness_mode = filt.get("freshness_mode", "weighted")
        self.freshness_topup_floor = int(filt.get("freshness_topup_floor", 2))

        notif = config.get("notification", {})
        observe_env = notif.get("observe_webhook_url_env")
        observe_webhook = os.environ.get(observe_env) if observe_env else None
        self.notifier = WeComNotifier(
            webhook_url=webhook_url,
            max_per_minute=notif.get("push_rate_limit_per_minute", 18),
            dry_run=dry_run,
            disclaimer=notif.get("template", {}).get(
                "disclaimer", "仅供参考，不构成投资建议"),
            tz_name=notif.get("timezone", "Asia/Shanghai"),
            time_format=notif.get("time_format", "%Y-%m-%d %H:%M"),
            observe_webhook_url=observe_webhook,
        )
        self.max_push = filt.get("max_per_run",
                                 notif.get("max_per_run", 20))
        self.min_strength = filt.get("min_strength", 60)
        self.min_rr = filt.get("min_rr", 1.5)
        self.min_confidence = filt.get("min_confidence", 0.4)
        # 几何质量分硬闸门（2026-09-05 加入）
        # 依据：209 张人工标注（v4），修正"深/触"恒满子分后几何分才有区分度。
        #   以修正分 ≥0.6 作推送闸门：精确率 16.7% → 25.6%，保留 65% 的"像"样本。
        # 注意：只有新推送（走交叉确认）才会算 geometry_score，老版本数据无此字段。
        self.min_geometry = filt.get("min_geometry", 0.6)
        self.min_volume = filt.get("min_volume_ratio", 1.5)
        # 同一标的最多推几个（防止 XRP 这种四周期各报一次刷屏）
        self.max_per_symbol = filt.get("max_per_symbol", 2)
        # 趋势过滤（实盘推送前必须与当前周期趋势同向）
        self.require_trend_alignment = filt.get("require_trend_alignment", True)
        # 趋势对齐的 ADX 门槛：低于此值视为横盘，不强制方向对齐
        self.adx_threshold = filt.get("trend_adx_threshold", 20)

        # 观察流配置（2026-09-08）：穿过 freshness 但被后续闸砍的新鲜信号
        # 单独推送供人眼验证/攒金标准样本。正式推送线不受影响。
        obs = filt.get("observe", {})
        self.observe_enabled = obs.get("enabled", False)
        self.observe_max = obs.get("max_per_run", 5)
        self.observe_min_strength = obs.get("min_strength", 45)
        self.observe_min_geometry = obs.get("min_geometry", 0.30)

        # 是否每次运行后都发一条"扫描摘要"（P0：summary_mode 控制发送策略）
        self.send_summary = notif.get("send_summary", True)
        self.summary_mode = notif.get("summary_mode", "on_signal")

    # ---------- 主流程 ----------

    def run(self, top_n: Optional[int] = None,
            intervals: Optional[List[str]] = None) -> ScanResult:
        cfg_scan = self.config.get("scan", {})
        top_n = top_n or cfg_scan.get("top_n", 300)
        intervals = intervals or cfg_scan.get(
            "intervals", ["15m", "1h", "4h", "1d"])
        kline_counts = cfg_scan.get("kline_counts", {})
        min_volume = cfg_scan.get("min_volume_usdt", 10_000_000)

        throttling = self.config.get("throttling", {})
        batch_size = throttling.get("batch_size", 50)
        batch_pause = throttling.get("batch_pause_sec", 2)

        result = ScanResult()
        t0 = time.time()

        # ---- 1. 选标的（S3 分层）----
        # core = 成交额>=min_volume 的头部（全周期扫描）；
        # tail = 长尾区间 [tail_min_volume, min_volume)（只扫大周期，抗噪）。
        # 动机：近端检测盲区 + 市场事实导致新鲜形态供给稀缺(0~1张/轮)，
        # 与其放宽 freshness 推陈年突破，不如扩长尾标的的大周期供给。
        # 低流动性标的小周期插针噪声大，故 tail 只扫 4h/1d。
        tail_enabled = cfg_scan.get("tail_enabled", False)
        tail_top_n = cfg_scan.get("tail_top_n", 300)
        tail_min_vol = cfg_scan.get("tail_min_volume_usdt", 1_000_000)
        tail_intervals = set(cfg_scan.get("tail_intervals", ["4h", "1d"]))

        core_symbols: List[str] = []
        tail_symbols: List[str] = []
        try:
            # tail 关闭时把 tail 下限提到与 core 相同 → 尾池区间为空，
            # 一次请求即可，行为与 S3 前完全一致。
            core_symbols, tail_symbols = self.client.get_symbols_tiered(
                core_top_n=top_n, core_min_volume_usdt=min_volume,
                tail_top_n=tail_top_n,
                tail_min_volume_usdt=(tail_min_vol if tail_enabled
                                      else min_volume))
        except Exception as e:
            logger.warning(f"分层标的选取失败({e})，退回单池 get_top_symbols")
            try:
                core_symbols = self.client.get_top_symbols(
                    top_n=top_n, min_volume_usdt=min_volume)
            except Exception as e2:
                logger.error(f"单池标的选取也失败: {e2}")

        if not core_symbols:
            result.errors.append("标的选取失败")
            result.duration_sec = time.time() - t0
            return result
        if not tail_enabled:
            tail_symbols = []
        result.symbols = core_symbols + tail_symbols
        result.tier_stats = {
            "core": {"symbols": len(core_symbols), "scanned": 0,
                     "confirmed": 0},
            "tail": {"symbols": len(tail_symbols), "scanned": 0,
                     "confirmed": 0},
        }
        tail_set = set(tail_symbols)
        logger.info(f"分层标的: core {len(core_symbols)} 个全周期扫, "
                    f"tail {len(tail_symbols)} 个只扫 "
                    f"{sorted(tail_intervals)}")

        # ---- 2. 逐周期扫描 ----
        # all_signals: 所有已确认信号（用于多周期交叉确认）
        # trends: {(symbol, interval): trend}
        all_signals: List[Pattern] = []
        trends: Dict[Tuple[str, str], str] = {}
        momentum: Dict[Tuple[str, str], dict] = {}
        klines_cache: Dict[Tuple[str, str], List[Kline]] = {}

        for interval in intervals:
            limit = kline_counts.get(interval, 240)
            # 分层：core 全周期；tail 只在配置的大周期参与
            if tail_symbols and interval in tail_intervals:
                pool = core_symbols + tail_symbols
            else:
                pool = core_symbols
            logger.info(f"--- 扫描周期 {interval} "
                        f"(标的 {len(pool)} 个"
                        f"{' = core+tail' if pool is not core_symbols else ''}) ---")

            for batch_start in range(0, len(pool), batch_size):
                batch = pool[batch_start: batch_start + batch_size]

                for symbol in batch:
                    # 长尾池只扫大周期，这里不会与 core 重叠（区间划分）
                    tier = "tail" if symbol in tail_set else "core"
                    try:
                        klines, source = self.client.get_klines(
                            symbol, interval, limit)
                        if not klines or len(klines) < 30:
                            result.failed_pairs += 1
                            continue

                        result.scanned_pairs += 1
                        result.tier_stats[tier]["scanned"] += 1
                        result.source_stats[source] = \
                            result.source_stats.get(source, 0) + 1

                        indicators = calc_indicators(klines)
                        if indicators.atr_current <= 0:
                            continue

                        health = pivot_health_check(
                            klines, find_pivots(klines, 3, 3))
                        result.health_stats[health["status"]] = \
                            result.health_stats.get(health["status"], 0) + 1

                        klines_cache[(symbol, interval)] = klines
                        trends[(symbol, interval)] = indicators.trend
                        momentum[(symbol, interval)] = {
                            "adx": indicators.adx_current,
                            "rsi": indicators.rsi_current,
                            "macd_hist": indicators.macd_hist_current,
                        }

                        # 形态识别（多尺度）
                        cands = self.engine.scan_multiscale(
                            klines, symbol, interval)
                        result.candidates.extend(cands)

                        # 不限新鲜度地收集已确认信号，供多周期参照
                        # （交叉确认需要看到"曾经出现过"的形态，
                        #   即使它已经不新鲜，因为大周期信号天然更新慢）
                        confirmed = [p for p in cands
                                     if p.status == PatternStatus.CONFIRMED]
                        all_signals.extend(confirmed)
                        if confirmed:
                            result.tier_stats[tier]["confirmed"] += len(confirmed)

                    except Exception as e:
                        result.failed_pairs += 1
                        msg = f"{symbol} {interval}: {e}"
                        logger.error(f"扫描异常 {msg}")
                        result.errors.append(msg)

                if batch_start + batch_size < len(pool):
                    time.sleep(batch_pause)

                if self.verbose:
                    done = min(batch_start + batch_size, len(pool))
                    logger.info(f"  {interval}: {done}/{len(pool)}")

        logger.info(f"候选形态 {len(result.candidates)} 个，"
                    f"已确认 {len(all_signals)} 个")
        # 2026-09-07: 分层统计——判断长尾池对供给的真实贡献
        _tc, _tt = result.tier_stats.get("core", {}), \
            result.tier_stats.get("tail", {})
        if _tt.get("symbols"):
            logger.info(f"分层供给 core: {_tc.get('symbols', 0)}标的 "
                        f"scanned={_tc.get('scanned', 0)} "
                        f"confirmed={_tc.get('confirmed', 0)} | "
                        f"tail: {_tt.get('symbols', 0)}标的 "
                        f"scanned={_tt.get('scanned', 0)} "
                        f"confirmed={_tt.get('confirmed', 0)}")

        # ---- 3. 多周期交叉确认 + 评分 ----
        # 注意：这里传入的是【全部】已确认信号，包括不新鲜的。
        # 因为共振判断需要知道"这个标的在大周期上曾经是什么方向"，
        # 而大周期信号天然更新慢（1d 一天才一根K线）。
        # 但下面第 4 步会把不新鲜的过滤掉，不会拿去推送。
        scored = self.cross_tf.confirm(all_signals, trends, momentum)
        result.confirmed = scored
        logger.info(f"交叉确认后剩余 {len(scored)} 个")

        # ---- 3.5 填充形态末端时间 ----
        # end_ms = 最后一个 pivot 对应 K 线的 openTime。
        # 供第 5 步去重做"同一形态"识别——修复冷却期过后同形态重复推送
        # （同一形态只要没被破坏，会连续多轮被检出）。
        for p in scored:
            kl = klines_cache.get((p.symbol, p.interval))
            if kl and p.pivots:
                ei = max(q.index for q in p.pivots)
                if 0 <= ei < len(kl):
                    p.end_ms = kl[ei].openTime

        # ---- 4. 完整过滤 ----
        # 这一步一个都不能漏。曾经只过滤了强度，结果推出去的信号里
        # 有"突破距今 201 根K线"的、也有 R:R 只有 1:0.6 的。
        # 新鲜度、风险回报比、置信度、量能、趋势同向，全部检查。
        # 2026-09-07 增加各闸 kill 计数，让 0 推送摘要漏斗能定位是哪道闸最严。
        result.kill_breakdown = {
            "freshness": 0, "strength": 0, "rr": 0,
            "volume": 0, "geometry": 0, "trend": 0, "stale": 0,
        }
        passed = []
        # 2026-09-07: 收集被 6 闸砍掉信号的 age, 循环结束后按周期打
        # 直方图分布。freshness 放宽没生效(砍数不变)时, 用真实分布
        # 决定"放宽到几根"才有数, 不再盲调。
        _killed_ages: Dict[str, List[int]] = {}
        # 2026-09-07: 被砍信号明细收集器 (gate, pattern)。用户肉眼在 OKX
        # 上看到不少币种有形态、系统却 0 推送——age 分布显示 4h 有 age=1
        # 的确认信号被砍，说明新鲜形态有、死在后续闸。收集每个被砍信号的
        # 币/周期/形态/age/被哪闸砍/各分数，下轮日志直接定位"谁杀了新鲜信号"。
        _killed_detail: List[Tuple[Pattern, str]] = []  # (pattern, gate)
        for p in scored:
            _killed_ages.setdefault(p.interval, []).append(p.breakout_age)
            max_age = self.engine.freshness_for(p.interval)
            if p.breakout_age > max_age:
                if self.freshness_mode == "hard":
                    result.kill_breakdown["freshness"] += 1
                    _killed_detail.append((p, "freshness"))
                    continue
                # 加权模式(P3): 不砍死, 标记陈旧, 降权排后, 待供给不足时兜底
                p.is_stale = True
                result.kill_breakdown["stale"] += 1
                # 仍继续走后续各闸(strength/rr/vol/geo/trend)
            if p.strength_score < self.min_strength:
                result.kill_breakdown["strength"] += 1
                _killed_detail.append((p, "strength"))
                continue
            if p.risk_reward < self.min_rr:
                result.kill_breakdown["rr"] += 1
                _killed_detail.append((p, "rr"))
                continue
            if p.volume_ratio < self.min_volume:
                result.kill_breakdown["volume"] += 1
                _killed_detail.append((p, "volume"))
                continue
            # 几何质量闸门：画得不像（几何分低）的直接不推。
            # 修正恒满子分后（2026-09-05）该分才有意义，见 __init__ 注释。
            # 注意：geometry_score 没算出来时（多数量级形态的兜底分支）闸门自动
            # 放行，不计入 kill——这是 by design，避免把未打分样本误杀。
            if getattr(p, "geometry_score", None) is not None:
                if p.geometry_score < self.min_geometry:
                    result.kill_breakdown["geometry"] += 1
                    _killed_detail.append((p, "geometry"))
                    continue
            # 趋势过滤（实测依据见 config.yaml 注释）
            # 仅当 ADX 显著（趋势存在）时才强制方向对齐；横盘不杀，
            # 避免把区间内的反转形态误剔。
            if self.require_trend_alignment:
                trend = trends.get((p.symbol, p.interval), "unknown")
                mom = momentum.get((p.symbol, p.interval))
                adx = mom.get("adx", 0) if mom else 0
                if adx >= self.adx_threshold:
                    aligned = (
                        (p.direction == Direction.LONG and trend == "up")
                        or (p.direction == Direction.SHORT and trend == "down")
                    )
                    if not aligned:
                        result.kill_breakdown["trend"] += 1
                        _killed_detail.append((p, "trend"))
                        continue
            passed.append(p)

        result.after_scoring = self._limit_per_symbol(passed)
        # 2026-09-07: 被 6 闸砍的 age 分布（按周期）。freshness 放宽
        # 后砍数没变时, 用真实年龄分布决定"放宽到几根"才靠谱。
        _passed_keys = {(p.interval, p.breakout_age) for p in passed}
        for _iv, _ages in _killed_ages.items():
            _killed = [a for a in _ages if (_iv, a) not in _passed_keys]
            if not _killed:
                continue
            _sorted = sorted(_killed)
            _n = len(_sorted)
            _med = _sorted[_n // 2] if _n % 2 else (
                _sorted[_n // 2 - 1] + _sorted[_n // 2]) / 2
            _bk = {"0-5": 0, "6-10": 0, "11-20": 0, "21-30": 0,
                   "31-50": 0, "51-100": 0, "100+": 0}
            for _a in _killed:
                if _a <= 5:    _bk["0-5"] += 1
                elif _a <= 10: _bk["6-10"] += 1
                elif _a <= 20: _bk["11-20"] += 1
                elif _a <= 30: _bk["21-30"] += 1
                elif _a <= 50: _bk["31-50"] += 1
                elif _a <= 100: _bk["51-100"] += 1
                else:          _bk["100+"] += 1
            logger.info("被6闸砍age分布 %s: n=%d min=%d median=%s max=%d "
                        "buckets=%s (当前窗口=%d)",
                        _iv, _n, _sorted[0], _med, _sorted[-1], _bk,
                        self.engine.freshness_for(_iv))
        # 被砍信号逐条明细（gate, symbol, interval, pattern, dir, age, 分数）
        # ——新鲜信号(age≤窗口)死在哪道闸，一眼可见。
        # 注意 _killed_detail 元素是 (pattern, gate)，解包顺序勿反。
        for _p, _gate in _killed_detail:
            _geo_r = getattr(_p, "geometry_reason", None)
            logger.info("砍杀明细 %-10s %-18s %-5s %-16s %-5s age=%-5d "
                        "strength=%s rr=%s vol=%s geo=%s%s",
                        _gate, _p.symbol, _p.interval, _p.pattern_type,
                        _p.direction.name if _p.direction else "?",
                        _p.breakout_age, _p.strength_score, _p.risk_reward,
                        _p.volume_ratio, getattr(_p, "geometry_score", None),
                        f" [{_geo_r}]" if _geo_r else "")

        # ---- 4.5 观察流候选收集 (2026-09-08) ----
        # 穿过 freshness 但被后续闸砍的新鲜信号 = "差一口气"的边界样本。
        # 0 推送连续 10+ 轮 → 金标准样本停滞; 观察流单独推送供人眼验证。
        # 结构下限: 太歪的(strength/geo 过低)不打扰, 聚焦接近推送线的边界案例。
        # 注: 观察候选元素 (pattern, gate)，后续推送要带 gate 说明死因。
        observe_candidates: List[Tuple[Pattern, str]] = []
        if self.observe_enabled:
            for _p, _gate in _killed_detail:
                if _gate == "freshness":
                    continue          # 只收新鲜突破，老突破无时效价值
                if _p.strength_score < self.observe_min_strength:
                    continue
                _g = getattr(_p, "geometry_score", None)
                if _g is not None and _g < self.observe_min_geometry:
                    continue
                observe_candidates.append((_p, _gate))
            logger.info(f"观察流候选 {len(observe_candidates)} 个 "
                        f"(穿freshness被后续闸砍, strength≥"
                        f"{self.observe_min_strength}, geo≥"
                        f"{self.observe_min_geometry})")
        logger.info(f"完整过滤后 {len(result.after_scoring)} 个 "
                    f"(新鲜度+R:R≥{self.min_rr}+置信度+量能"
                    f"+趋势同向{'✓' if self.require_trend_alignment else '✗'})")
        # 2026-09-07: kill 分布也打进日志（不只企微摘要），0 推送时
        # 直接从 Actions logs 定位主闸，无需等企微截图。
        _kb = result.kill_breakdown
        logger.info("各闸被砍 ▶ freshness:%d strength:%d rr:%d volume:%d "
                    "geometry:%d trend:%d stale:%d (确认%d→过滤后%d)",
                    _kb.get("freshness", 0), _kb.get("strength", 0),
                    _kb.get("rr", 0), _kb.get("volume", 0),
                    _kb.get("geometry", 0), _kb.get("trend", 0),
                    _kb.get("stale", 0),
                    len(scored), len(result.after_scoring))

        # ---- 5. 去重（状态跃迁可重推, P2）----
        self.state.cleanup()
        fresh_new, events = self.state.filter_new(result.after_scoring)
        # 事件重推限速: 避免形态重分类/积压一次性洪峰(配 PushLimiter 18条/分)
        if len(events) > self.max_repush:
            events = sorted(events,
                            key=lambda x: (-x.strength_score,
                                           -x.risk_reward))[:self.max_repush]
            logger.info(f"事件重推超限, 截断至 {self.max_repush} 个")
        result.after_dedup = fresh_new + events
        logger.info(f"去重后 {len(result.after_dedup)} 个 "
                     f"(全新 {len(fresh_new)} + 事件重推 {len(events)})")

        # ---- 6. 排序 + 限量 ----
        result.after_dedup = self._apply_freshness_ranking(result.after_dedup)
        to_push = result.after_dedup[:self.max_push]

        # ---- 7. 渲染图表 + 推送 ----
        os.makedirs(self.charts_dir, exist_ok=True)
        for p in to_push:
            image = None
            klines = klines_cache.get((p.symbol, p.interval))
            if klines:
                try:
                    chart_cfg = self.config.get(
                        "notification", {}).get("chart", {})
                    image = render_pattern_chart(
                        klines, p,
                        candles=chart_cfg.get("candles_displayed", 120),
                        width_px=chart_cfg.get("width", 900),
                        dpi=chart_cfg.get("dpi", 120),
                    )
                    if image:
                        path = os.path.join(
                            self.charts_dir,
                            f"{p.symbol}_{p.interval}_{p.pattern_type}.png")
                        with open(path, "wb") as f:
                            f.write(image)
                except Exception as e:
                    logger.error(f"图表渲染失败 {p.symbol} "
                                 f"{p.interval}: {e}")

            ok = self.notifier.push(p, image)
            if ok and not self.dry_run:
                # 2026-09-09 修复: dry-run 虚拟推送不得写入去重表。
                # 根因: notifier.push 在 dry_run 返回 True(模拟成功),
                # 旧代码无条件 state.record → run#202 dry-run 把 DOS 记入
                # "已推送"冷却, run#203 真实轮判"同形态已推过"吞掉真信号。
                self.state.record(p)
            if ok:
                result.pushed.append(p)

        # ---- 7.5 观察流推送 (2026-09-08) ----
        # 穿过 freshness 但被后续闸砍的边界样本，独立去重空间推送，
        # 供人眼验证/攒金标准样本。观察过的不阻塞之后正式推送，
        # 正式推过的也不进观察流（去重空间隔离，见 state_store）。
        if self.observe_enabled and observe_candidates:
            # 观察流自己也要限量 + 同形态去重（否则每轮刷屏同一批）
            observe_candidates.sort(
                key=lambda x: -x[0].strength_score)
            to_observe = observe_candidates[:self.observe_max]
            fresh_obs = self.state.filter_new_observe(
                [p for p, _ in to_observe])[0]
            if len(fresh_obs) < len(to_observe):
                logger.info(f"观察流去重拦下 "
                            f"{len(to_observe) - len(fresh_obs)} 个")
            for p in fresh_obs:
                gate = next((g for cp, g in observe_candidates
                             if cp is p), "?")
                image = None
                klines = klines_cache.get((p.symbol, p.interval))
                if klines:
                    try:
                        chart_cfg = self.config.get(
                            "notification", {}).get("chart", {})
                        image = render_pattern_chart(
                            klines, p,
                            candles=chart_cfg.get("candles_displayed", 120),
                            width_px=chart_cfg.get("width", 900),
                            dpi=chart_cfg.get("dpi", 120),
                        )
                        if image:
                            path = os.path.join(
                                self.charts_dir,
                                f"OBS_{p.symbol}_{p.interval}_"
                                f"{p.pattern_type}.png")
                            with open(path, "wb") as f:
                                f.write(image)
                    except Exception as e:
                        logger.error(f"观察图表渲染失败 {p.symbol} "
                                     f"{p.interval}: {e}")
                ok = self.notifier.push_observe(p, image, gate)
                if ok and not self.dry_run:
                    # 同上 (2026-09-09): dry-run 观察推送也不写观察去重表
                    self.state.record_observe(p)
                if ok:
                    result.observed.append(p)
                    logger.info(f"观察推送 {p.symbol} {p.interval} "
                                f"{p.pattern_type} (gate={gate})")
            logger.info(f"观察流推送 {len(result.observed)} 张")

        # ---- 8. 保存状态 ----
        result.duration_sec = time.time() - t0
        self.state.update_stats({
            "scanned_pairs": result.scanned_pairs,
            "failed_pairs": result.failed_pairs,
            "candidates": len(result.candidates),
            "confirmed": len(result.confirmed),
            "after_scoring": len(result.after_scoring),
            "after_dedup": len(result.after_dedup),
            "pushed": len(result.pushed),
            "observed": len(result.observed),
            "duration_seconds": round(result.duration_sec, 1),
            "source_stats": result.source_stats,
            "health_stats": result.health_stats,
        })
        self.state.save()

        # ---- 9. 扫描摘要（确认服务存活；0 信号时给用户明确反馈）----
        # 摘要里同步输出过滤漏斗 6 个数：candidates/confirmed/after_scoring/after_dedup/pushed
        # 当 pushed=0 时这几段差值就是定位"哪道闸砍光了所有信号"的唯一线索
        # ——否则只能从 Actions 日志看（logs 需认证）。
        # 2026-09-07 增加：candidates=476 一连 40 次 0 推送时无法判断哪道闸手软
        if self.send_summary:
            mode = self.summary_mode
            has_signal = len(result.pushed) > 0
            has_error = len(result.errors) > 0
            do_summary = True
            if mode == "on_signal":
                do_summary = has_signal or has_error
            elif mode == "daily":
                last = self.state.get_last_scan()
                today = datetime.now(timezone.utc).date()
                do_summary = (last is None) or (last.date() != today)
            # always → do_summary 保持 True
            if do_summary:
                self.notifier.push_summary(
                    scanned=result.scanned_pairs,
                    candidates=len(result.candidates),
                    confirmed=len(result.confirmed),
                    after_scoring=len(result.after_scoring),
                    after_dedup=len(result.after_dedup),
                    signals=len(result.pushed),
                    duration=result.duration_sec,
                    kill_breakdown=result.kill_breakdown,
                    observed=len(result.observed),
                )

        return result

    def _apply_freshness_ranking(self, patterns: List[Pattern]) -> List[Pattern]:
        """加权新鲜度排序 + top-up 兜底（P3）。

        - freshness_mode=hard：旧逻辑，按强度降序。
        - freshness_mode=weighted：新鲜信号永远排前，陈旧信号(突破较旧,
          is_stale=True)排后；仅当新鲜供给 < freshness_topup_floor 时，
          用最强陈旧信号补到 floor 个（覆盖兜底），且绝不把陈旧信号混进
          新鲜供给充足的轮次。返回的是已排序 / 已裁剪的列表（待 max_push 截断）。
        """
        if not patterns:
            return patterns

        if self.freshness_mode != "weighted":
            patterns.sort(key=lambda x: (-x.strength_score, -x.risk_reward))
            return patterns

        patterns.sort(
            key=lambda x: (0 if not getattr(x, "is_stale", False) else 1,
                           -x.strength_score, -x.risk_reward))
        fresh_count = sum(1 for x in patterns
                          if not getattr(x, "is_stale", False))
        if fresh_count >= self.freshness_topup_floor:
            # 新鲜供给充足 → 丢弃所有陈旧信号（不推陈年突破）
            return [x for x in patterns if not getattr(x, "is_stale", False)]

        # 新鲜供给不足 → 最多补 floor-fresh_count 个最强陈旧信号
        keep = self.freshness_topup_floor - fresh_count
        stale_sorted = sorted(
            [x for x in patterns if getattr(x, "is_stale", False)],
            key=lambda x: (-x.strength_score, -x.risk_reward))
        keep_ids = {id(x) for x in stale_sorted[:keep]}
        return [x for x in patterns
                if (not getattr(x, "is_stale", False)) or id(x) in keep_ids]

    def _limit_per_symbol(self, patterns: List[Pattern]) -> List[Pattern]:
        """
        限制同一标的的推送数量，并消解方向冲突。

        为什么要这一步（实测发现的问题）：

        XRPUSDT 一次扫描里同时报了 4 条：
          15m 头肩顶(空)、1h 头肩顶(空)、4h 双底(多)、1d 双底(多)
        同一个标的既让做多又让做空，使用者会无所适从。

        消解规则：
          1. 若同一标的方向冲突，【以最大周期的方向为准】——
             大周期代表更大的格局，小周期的反向信号通常是噪音或回调。
          2. 每个标的最多保留 max_per_symbol 条（默认 2）。
        """
        if not patterns:
            return patterns

        by_symbol: Dict[str, List[Pattern]] = {}
        for p in patterns:
            by_symbol.setdefault(p.symbol, []).append(p)

        result: List[Pattern] = []
        for symbol, group in by_symbol.items():
            if len(group) == 1:
                result.extend(group)
                continue

            # 方向是否冲突
            directions = {p.direction for p in group}
            if len(directions) > 1:
                # 找出最大周期的信号方向
                largest = max(group, key=lambda x: interval_rank(x.interval))
                keep_dir = largest.direction
                dropped = [p for p in group if p.direction != keep_dir]
                if dropped:
                    logger.info(
                        f"{symbol} 多周期方向冲突，以最大周期 "
                        f"{largest.interval}({keep_dir.value}) 为准，"
                        f"丢弃 {len(dropped)} 条反向信号")
                group = [p for p in group if p.direction == keep_dir]

            # 每个标的限量：按强度排序取前 N
            group.sort(key=lambda x: (-x.strength_score, -x.risk_reward))
            result.extend(group[:self.max_per_symbol])

        return result

    # ---------- 报告 ----------

    @staticmethod
    def print_report(result: ScanResult):
        print()
        print("=" * 66)
        print("  扫描报告")
        print("=" * 66)
        print(f"  标的数          : {len(result.symbols)}")
        print(f"  扫描对数        : {result.scanned_pairs} "
              f"(失败 {result.failed_pairs})")
        print(f"  数据源分布      : {result.source_stats}")
        print(f"  候选形态        : {len(result.candidates)}")
        print(f"  已确认          : {len(result.confirmed)}")
        print(f"  通过强度过滤    : {len(result.after_scoring)}")
        print(f"  去重后          : {len(result.after_dedup)}")
        print(f"  实际推送        : {len(result.pushed)}")
        print(f"  总耗时          : {result.duration_sec:.1f}s")
        if result.health_stats:
            print(f"  摆动点健康度    : {result.health_stats}")
        if result.errors:
            print(f"  错误            : {len(result.errors)} 条")
            for e in result.errors[:5]:
                print(f"    - {e}")

        if result.pushed:
            print()
            print("  推送明细:")
            for p in result.pushed:
                icon = "\U0001F4C8" if p.direction.value == "LONG" \
                    else "\U0001F4C9"
                print(f"    {icon} {p.symbol:<12}{p.interval:<5}"
                      f"{p.pattern_type:<22}"
                      f"强度={p.strength_score:<4} R:R=1:{p.risk_reward:.1f} "
                      f"共振={len(p.resonant_with)}")
        print()
