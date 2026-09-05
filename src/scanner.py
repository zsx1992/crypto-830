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
    duration_sec: float = 0.0
    errors: List[str] = field(default_factory=list)
    health_stats: Dict[str, int] = field(default_factory=dict)
    source_stats: Dict[str, int] = field(default_factory=dict)


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
        self.state = StateStore(
            state_path=st.get("file", "state/state.json"),
            cooldown_minutes=filt.get("cooldown_minutes"),
            cleanup_days=st.get("cleanup_days", 7),
        )

        notif = config.get("notification", {})
        self.notifier = WeComNotifier(
            webhook_url=webhook_url,
            max_per_minute=notif.get("push_rate_limit_per_minute", 18),
            dry_run=dry_run,
            disclaimer=notif.get("template", {}).get(
                "disclaimer", "仅供参考，不构成投资建议"),
            tz_name=notif.get("timezone", "Asia/Shanghai"),
            time_format=notif.get("time_format", "%Y-%m-%d %H:%M"),
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

        # 是否每次运行后都发一条"扫描摘要"（确认服务存活 + 无信号时有反馈）
        self.send_summary = notif.get("send_summary", True)

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

        # ---- 1. 选标的 ----
        logger.info(f"选取 24h 成交额前 {top_n} 的合约...")
        symbols = self.client.get_top_symbols(top_n=top_n,
                                              min_volume_usdt=min_volume)
        if not symbols:
            result.errors.append("标的选取失败")
            result.duration_sec = time.time() - t0
            return result
        result.symbols = symbols
        logger.info(f"已选取 {len(symbols)} 个标的")

        # ---- 2. 逐周期扫描 ----
        # all_signals: 所有已确认信号（用于多周期交叉确认）
        # trends: {(symbol, interval): trend}
        all_signals: List[Pattern] = []
        trends: Dict[Tuple[str, str], str] = {}
        momentum: Dict[Tuple[str, str], dict] = {}
        klines_cache: Dict[Tuple[str, str], List[Kline]] = {}

        for interval in intervals:
            limit = kline_counts.get(interval, 240)
            logger.info(f"--- 扫描周期 {interval} ---")

            for batch_start in range(0, len(symbols), batch_size):
                batch = symbols[batch_start: batch_start + batch_size]

                for symbol in batch:
                    try:
                        klines, source = self.client.get_klines(
                            symbol, interval, limit)
                        if not klines or len(klines) < 30:
                            result.failed_pairs += 1
                            continue

                        result.scanned_pairs += 1
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

                    except Exception as e:
                        result.failed_pairs += 1
                        msg = f"{symbol} {interval}: {e}"
                        logger.error(f"扫描异常 {msg}")
                        result.errors.append(msg)

                if batch_start + batch_size < len(symbols):
                    time.sleep(batch_pause)

                if self.verbose:
                    done = min(batch_start + batch_size, len(symbols))
                    logger.info(f"  {interval}: {done}/{len(symbols)}")

        logger.info(f"候选形态 {len(result.candidates)} 个，"
                    f"已确认 {len(all_signals)} 个")

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
        passed = []
        for p in scored:
            max_age = self.engine.freshness_for(p.interval)
            if p.breakout_age > max_age:
                continue
            if p.strength_score < self.min_strength:
                continue
            if p.risk_reward < self.min_rr:
                continue
            if p.volume_ratio < self.min_volume:
                continue
            # 几何质量闸门：画得不像（几何分低）的直接不推。
            # 修正恒满子分后（2026-09-05）该分才有意义，见 __init__ 注释。
            if getattr(p, "geometry_score", None) is not None:
                if p.geometry_score < self.min_geometry:
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
                        continue
            passed.append(p)

        result.after_scoring = self._limit_per_symbol(passed)
        logger.info(f"完整过滤后 {len(result.after_scoring)} 个 "
                    f"(新鲜度+R:R≥{self.min_rr}+置信度+量能"
                    f"+趋势同向{'✓' if self.require_trend_alignment else '✗'})")

        # ---- 5. 去重 ----
        self.state.cleanup()
        result.after_dedup = self.state.filter_new(result.after_scoring)
        logger.info(f"去重后 {len(result.after_dedup)} 个")

        # ---- 6. 排序 + 限量 ----
        result.after_dedup.sort(key=lambda x: (-x.strength_score,
                                               -x.risk_reward))
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
            if ok:
                self.state.record(p)
                result.pushed.append(p)

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
            "duration_seconds": round(result.duration_sec, 1),
            "source_stats": result.source_stats,
            "health_stats": result.health_stats,
        })
        self.state.save()

        # ---- 9. 扫描摘要（确认服务存活；0 信号时给用户明确反馈）----
        if self.send_summary:
            self.notifier.push_summary(
                scanned=result.scanned_pairs,
                candidates=len(result.candidates),
                signals=len(result.pushed),
                duration=result.duration_sec,
            )

        return result

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
