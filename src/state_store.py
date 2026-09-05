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
from typing import List, Dict, Optional

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
                 cleanup_days: int = 7):
        self.state_path = state_path
        self.cooldown = dict(self.DEFAULT_COOLDOWN_MINUTES)
        if cooldown_minutes:
            self.cooldown.update(cooldown_minutes)
        self.cleanup_days = cleanup_days
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
            "version": 1,
            "lastScanAt": None,
            "pushedSignals": [],
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

    # ---------- 去重 ----------

    def is_in_cooldown(self, p: Pattern) -> bool:
        """该信号是否应抑制推送（冷却期 + 同形态识别）"""
        h = signal_hash(p.symbol, p.pattern_type, p.interval)
        now = now_utc()
        # "同一形态"容差 = 同周期 2 根K线时长（毫秒）
        same_ms = INTERVAL_MINUTES.get(p.interval, 60) * 60_000 * 2

        for rec in self.state.get("pushedSignals", []):
            if rec.get("signalHash") != h:
                continue
            # 方向翻转：结构已破坏 → 允许重推（原设计意图）
            rec_dir = rec.get("direction")
            if rec_dir and rec_dir != p.direction.value:
                continue
            # 形态身份识别：end_ms 相同 → 同一个形态还挂在图上。
            # 冷却期只是粗粒度闸门，这里做细粒度拦截，
            # 修复"冷却一过同一形态又刷一次"的重复推送。
            rec_end = rec.get("endMs")
            same_form = bool(rec_end and p.end_ms
                             and abs(p.end_ms - rec_end) <= same_ms)
            if same_form:
                logger.info(f"同形态已推过(端差{abs(p.end_ms-rec_end)}ms): "
                            f"{p.symbol} {p.pattern_type} {p.interval}")
                return True
            # 非同一形态（演化出新末端 / 旧记录无 endMs）→ 冷却期兜底
            try:
                until = datetime.fromisoformat(rec["cooldownUntil"])
            except Exception:
                continue
            if until > now:
                remain = (until - now).total_seconds() / 60
                logger.info(f"冷却期内: {p.symbol} {p.pattern_type} "
                            f"{p.interval}，剩余 {remain:.0f} 分钟")
                return True
        return False

    def record(self, p: Pattern):
        """记录已推送的信号"""
        minutes = self.cooldown.get(p.interval, 60)
        now = now_utc()
        rec = {
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
        }
        self.state.setdefault("pushedSignals", []).append(rec)
        self._dirty = True

    def cleanup(self):
        """清理过期记录，防止 JSON 无限膨胀"""
        cutoff = now_utc() - timedelta(days=self.cleanup_days)
        before = len(self.state.get("pushedSignals", []))
        self.state["pushedSignals"] = [
            r for r in self.state.get("pushedSignals", [])
            if self._parse_dt(r.get("pushedAt")) > cutoff
        ]
        removed = before - len(self.state["pushedSignals"])
        if removed:
            logger.info(f"清理过期状态记录 {removed} 条")

    @staticmethod
    def _parse_dt(s) -> datetime:
        if isinstance(s, str):
            try:
                return datetime.fromisoformat(s)
            except Exception:
                pass
        return datetime.min.replace(tzinfo=timezone.utc)

    # ---------- 过滤入口 ----------

    def filter_new(self, patterns: List[Pattern]) -> List[Pattern]:
        """剔除冷却期内的重复信号"""
        fresh = []
        for p in patterns:
            if self.is_in_cooldown(p):
                continue
            fresh.append(p)
        return fresh

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
