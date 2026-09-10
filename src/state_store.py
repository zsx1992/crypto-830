# -*- coding: utf-8 -*-
"""
去重状态持久化

持久化策略（两层，互为备份）：
  1. actions/cache —— 主存储，跨 run 保持，读写快，零 commit 噪音
  2. state/state.json —— 兜底，git commit 回写仓库，永久保存

为什么两层：
  cache 快且干净，但会被 GitHub Actions 清理（保留政策 7 天或有容量上限）；
  git 永久且可追溯，但每次 run 产生一个自动提交。
  两者内容完全一致，cache 未命中时自动回落到文件。

去重逻辑：
  同一 (symbol, pattern_type, interval) 在冷却期内不重复推送。
  注意 direction 不计入去重键——同一标的同一形态若先报多后报空，
  说明结构已破坏，应当推送（让使用者知道情况变了）。

  2026-09-05 修复（同形态冷却期过后重复推送）：
  原实现只按冷却期拦截。实测 4h 周期冷却 480min=8h，而 freshness 窗口 12 根
  =48h——同一形态确认后只要没被破坏，会连续多轮被检出；冷却期一过，
  同一个形态（end_ms 不变）会再次推送，用户被重复刷屏。
  现在追加"形态身份"识别：新检出形态的 end_ms 与上次推送记录相差 ≤
  同周期 2 根K线 → 视为同一形态，即使冷却期已过也抑制推送；
  只有形态演化出新末端（end_ms 明显前移）或方向翻转才允许再推。
"""

import os
import sys
import json
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from patterns.base import Pattern

logger = logging.getLogger(__name__)

# 周期 -> 每根K线时长（分钟），用于"同一形态"的 end_ms 容差判定
INTERVAL_MINUTES = {
    "15m": 15, "1h": 60, "2h": 120, "4h": 240, "1d": 1440,
}


def signal_hash(symbol: str, pattern_type: str, interval: str) -> str:
    """
    去重键（不含方向）。

    排除 direction 的原因：同一标的同一形态，若方向从多翻空，
    说明原结构已被破坏，这是重要信息，应当再次推送。
    """
    raw = f"{symbol}|{pattern_type}|{interval}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class StateStore:
    """去重状态管理"""

    DEFAULT_COOLDOWN_MINUTES = {
        "15m": 30,
        "1h": 120,
        "2h": 240,
        "4h": 480,
        "1d": 1440,
    }

    def __init__(self, state_path: str = "state/state.json",
                 cooldown_minutes: Optional[dict] = None,
                 cleanup_days: int = 7,
                 dedup_cfg: Optional[dict] = None):
        self.state_path = state_path
        self.cooldown = dict(self.DEFAULT_COOLDOWN_MINUTES)
        if cooldown_minutes:
            self.cooldown.update(cooldown_minutes)
        self.cleanup_days = cleanup_days
        self.dedup_cfg = dedup_cfg or {}
        self.state = self._load()
        self._dirty = False

    # ---------- 加载 / 保存 ----------

    def _load(self) -> dict:
        """从文件加载；不存在则返回空状态（cache 未命中时的兜底路径）"""
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.info(f"已加载状态: {self.state_path} "
                            f"({len(data.get('pushedSignals', []))} 条历史记录)")
                return data
            except Exception as e:
                logger.warning(f"状态文件损坏，重置: {e}")
        logger.info("无历史状态，从头开始")
        return self._empty_state()

    @staticmethod
    def _empty_state() -> dict:
        return {
            "version": 2,
            "lastScanAt": None,
            "pushedSignals": [],
            # 2026-09-08: 观察流独立去重空间。观察过的形态不阻塞正式推送
            # （之后达标仍可正式推），正式推过的也不进观察流（避免重复打扰）。
            "observedSignals": [],
            "scanStats": {},
        }

    def save(self):
        """写入状态文件（供 git commit 回写）"""
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".",
                        exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            self._dirty = False
            logger.info(f"状态已保存: {self.state_path}")
        except Exception as e:
            logger.error(f"状态保存失败: {e}")

    # ---------- 去重（状态跃迁可重推, 2026-09-10 P2）----------

    def dedup_check(self, p: Pattern, records: List[dict]) -> str:
        """去重判定，返回:
          'allow_new'   → 首次 / 冷却已过且非同形态，放行（计入新推送）
          'allow_event' → 同形态但发生状态跃迁（方向翻转 / 突破延伸 /
                          强度跳升 / 突破价位移），放行（计入事件重推，
                          受 max_repush_per_run 限速）
          'cooldown'    → 不同形态但仍在冷却期，抑制
          'suppress'    → 同形态无新事件（旧永久抑制行为）
        """
        h = signal_hash(p.symbol, p.pattern_type, p.interval)
        now = now_utc()
        same_ms = INTERVAL_MINUTES.get(p.interval, 60) * 60_000 * 2

        matching = [r for r in records if r.get("signalHash") == h]
        if not matching:
            return "allow_new"

        latest = max(matching, key=lambda r: r.get("pushedAt", ""))

        # 方向翻转：结构已破坏 → 重要信息，允许重推（视为新事件）
        if latest.get("direction") and latest["direction"] != p.direction.value:
            return "allow_event"

        rec_end = latest.get("endMs")
        same_form = bool(rec_end and getattr(p, "end_ms", 0)
                         and abs(p.end_ms - rec_end) <= same_ms)
        if not same_form:
            # 不同形态（演化出新末端 / 旧记录无 endMs）→ 冷却期兜底
            until = self._parse_dt(latest.get("cooldownUntil"))
            if until > now:
                return "cooldown"
            return "allow_new"

        # 同一形态仍挂在图上：检查是否发生"新事件"
        cfg = self.dedup_cfg
        if cfg.get("event_repush") and self._has_new_event(p, latest):
            return "allow_event"

        # 可选：开启"形态仍在"周期重推（有刷屏风险，默认关）
        reconf_h = int(cfg.get("reconfirm_cooldown_hours", 0) or 0)
        if reconf_h:
            rat = self._parse_dt(latest.get("reconfirmAt"))
            if rat > now:
                return "suppress"
            return "allow_event"

        # 默认：同形态无新事件 → 抑制（旧永久抑制行为，安静期0推送主因）
        return "suppress"

    def _has_new_event(self, p: Pattern, rec: dict) -> bool:
        """同形态是否发生值得重推的状态跃迁"""
        cfg = self.dedup_cfg
        # 突破又延伸了若干根（价格取得新进展）
        adv = int(cfg.get("event_breakout_advance", 2))
        rec_idx = rec.get("breakoutIndex")
        if rec_idx is not None and p.breakout_index is not None:
            if p.breakout_index - rec_idx >= adv:
                return True
        # 突破价明显偏移（入场位变了）
        rec_px = rec.get("breakoutPrice")
        if rec_px and getattr(p, "breakout_price", 0):
            if abs(p.breakout_price - rec_px) / rec_px > \
                    float(cfg.get("event_price_delta", 0.02)):
                return True
        # 强度明显提升
        rec_s = rec.get("strength")
        if rec_s is not None and p.strength_score is not None:
            if p.strength_score - rec_s >= int(cfg.get("event_strength_delta", 8)):
                return True
        return False

    def is_in_cooldown(self, p: Pattern) -> bool:
        """正式推送空间：该信号是否应抑制推送（冷却期 / 同形态无事件）"""
        r = self.dedup_check(p, self.state.get("pushedSignals", []))
        return r in ("cooldown", "suppress")

    def is_observed(self, p: Pattern) -> bool:
        """观察空间：该信号是否已观察推送过（同形态 / 冷却期）"""
        r = self.dedup_check(p, self.state.get("observedSignals", []))
        return r in ("cooldown", "suppress")

    def record(self, p: Pattern):
        """记录已正式推送的信号"""
        rec = self._make_record(p)
        self.state.setdefault("pushedSignals", []).append(rec)
        self._dirty = True

    def record_observe(self, p: Pattern):
        """记录已观察推送的信号（独立空间，不阻塞正式推送）"""
        rec = self._make_record(p)
        self.state.setdefault("observedSignals", []).append(rec)
        self._dirty = True

    def _make_record(self, p: Pattern) -> dict:
        """构造一条推送/观察记录（两空间共用字段）"""
        minutes = self.cooldown.get(p.interval, 60)
        now = now_utc()
        reconf_h = int(self.dedup_cfg.get("reconfirm_cooldown_hours", 0) or 0)
        return {
            "symbol": p.symbol,
            "patternType": p.pattern_type,
            "interval": p.interval,
            "direction": p.direction.value,
            "strength": p.strength_score,
            "detectedAt": now.isoformat(),
            "pushedAt": now.isoformat(),
            "cooldownUntil": (now + timedelta(minutes=minutes)).isoformat(),
            "signalHash": signal_hash(p.symbol, p.pattern_type, p.interval),
            "endMs": getattr(p, "end_ms", 0) or 0,
            "breakoutIndex": getattr(p, "breakout_index", None),
            "breakoutPrice": getattr(p, "breakout_price", None),
            "reconfirmAt": ((now + timedelta(hours=reconf_h)).isoformat()
                            if reconf_h else None),
        }

    def cleanup(self):
        """清理过期记录，防止 JSON 无限膨胀（正式 + 观察两空间）"""
        cutoff = now_utc() - timedelta(days=self.cleanup_days)
        for key in ("pushedSignals", "observedSignals"):
            before = len(self.state.get(key, []))
            self.state[key] = [
                r for r in self.state.get(key, [])
                if self._parse_dt(r.get("pushedAt")) > cutoff
            ]
            removed = before - len(self.state[key])
            if removed:
                logger.info(f"清理过期状态记录({key}) {removed} 条")

    @staticmethod
    def _parse_dt(s) -> datetime:
        if isinstance(s, str):
            try:
                return datetime.fromisoformat(s)
            except Exception:
                pass
        return datetime.min.replace(tzinfo=timezone.utc)

    # ---------- 过滤入口 ----------

    def filter_new(self, patterns: List[Pattern]) -> Tuple[List, List]:
        """剔除重复信号，返回 (全新信号, 事件重推信号) 两个列表。

        事件重推信号需在调用方按 max_repush_per_run 限速后再合并进推送队列。
        """
        fresh_new, events = [], []
        for p in patterns:
            r = self.dedup_check(p, self.state.get("pushedSignals", []))
            if r == "allow_new":
                fresh_new.append(p)
            elif r == "allow_event":
                events.append(p)
        return fresh_new, events

    def filter_new_observe(self, patterns: List[Pattern]) -> Tuple[List, List]:
        """观察空间：返回 (可观察信号, []) —— 观察流不区分新 / 事件"""
        fresh = []
        for p in patterns:
            r = self.dedup_check(p, self.state.get("observedSignals", []))
            if r in ("allow_new", "allow_event"):
                fresh.append(p)
        return fresh, []

    # ---------- 统计 ----------

    def update_stats(self, stats: dict):
        self.state["scanStats"] = stats
        self.state["lastScanAt"] = now_utc().isoformat()
        self._dirty = True

    def get_last_scan(self) -> Optional[datetime]:
        s = self.state.get("lastScanAt")
        if not s:
            return None
        return self._parse_dt(s)

    def summary(self) -> dict:
        active = 0
        now = now_utc()
        for r in self.state.get("pushedSignals", []):
            until = self._parse_dt(r.get("cooldownUntil"))
            if until > now:
                active += 1
        return {
            "total_records": len(self.state.get("pushedSignals", [])),
            "active_cooldowns": active,
            "last_scan": self.state.get("lastScanAt"),
        }
