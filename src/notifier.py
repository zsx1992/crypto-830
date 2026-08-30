# -*- coding: utf-8 -*-
"""
企业微信群机器人推送模块

消息格式：每条信号发两条消息
  1. markdown 文字（含币种/形态/周期/方向/强度/价位/计算依据）
  2. image 图片（base64 + md5，标注了形态结构）

为什么分两条发：
  企微的 markdown 不支持内嵌图片，只能单独发 image 类型消息。

企微硬限制：
  - 每个机器人 ≤ 20 条/分钟
  - 图片 base64 前 ≤ 2MB，仅 JPG/PNG
  - markdown 内容 ≤ 4096 字节
"""

import os
import sys
import time
import base64
import hashlib
import logging
from typing import List, Optional

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from patterns.base import Pattern, Direction

logger = logging.getLogger(__name__)

try:
    import requests
except ImportError:
    requests = None


# 形态中文名（推送正文用中文）
PATTERN_NAMES_CN = {
    "double_top": "双顶",
    "double_bottom": "双底 / W底",
    "head_shoulders_top": "头肩顶",
    "head_shoulders_bottom": "头肩底",
    "ascending_triangle": "上升三角形",
    "descending_triangle": "下降三角形",
    "symmetrical_triangle": "对称三角形",
    "flag": "旗形",
    "rising_wedge": "上升楔形",
    "falling_wedge": "下降楔形",
}


class PushLimiter:
    """
    推送频率限制器（令牌桶）。

    企微硬限制 20 条/分钟，这里默认限到 18 条留余量。
    """

    def __init__(self, max_per_minute: int = 18):
        self.timestamps: List[float] = []
        self.max_per_minute = max_per_minute

    def acquire(self):
        """若已达限速则阻塞等待"""
        now = time.time()
        self.timestamps = [t for t in self.timestamps if now - t < 60]
        if len(self.timestamps) >= self.max_per_minute:
            wait = 60 - (now - self.timestamps[0])
            if wait > 0:
                logger.info(f"推送频率已达上限，等待 {wait:.1f}s")
                time.sleep(wait)
            self.timestamps = [t for t in self.timestamps
                               if time.time() - t < 60]
        self.timestamps.append(time.time())


class WeComNotifier:
    """企业微信群机器人推送"""

    def __init__(self, webhook_url: Optional[str] = None,
                 max_per_minute: int = 18,
                 dry_run: bool = False,
                 disclaimer: str = "仅供参考，不构成投资建议"):
        self.webhook_url = webhook_url
        self.dry_run = dry_run
        self.limiter = PushLimiter(max_per_minute)
        self.disclaimer = disclaimer

        self.sent_count = 0
        self.failed_count = 0

        if dry_run:
            logger.info("推送模块处于 DRY-RUN 模式，不会发送真实消息")
        elif not webhook_url:
            logger.warning("未提供 webhook_url，推送将失败")

    # ---------- 消息构造 ----------

    def build_markdown(self, p: Pattern) -> dict:
        """构造 markdown 消息体"""
        name = PATTERN_NAMES_CN.get(p.pattern_type, p.pattern_type)
        is_long = p.direction == Direction.LONG
        icon = "\U0001F4C8" if is_long else "\U0001F4C9"
        dir_cn = "做多" if is_long else "做空"

        # 强度分档
        if p.strength_score >= 75:
            grade = "强"
        elif p.strength_score >= 60:
            grade = "中"
        else:
            grade = "弱"

        # 盈亏百分比
        def pct(target: float) -> str:
            if p.entry_price <= 0:
                return "0.0"
            return f"{(target - p.entry_price) / p.entry_price * 100:+.1f}"

        current = p.entry_price      # 入场价即最新收盘价

        lines = [
            f"{icon} **{p.symbol} {name}**",
            f"> 周期: `{p.interval}` | 方向: **{dir_cn}** "
            f"| 强度: **{p.strength_score}/100** ({grade})",
            ">",
            f"> 当前价: `{current:.4f}`",
            ">",
            "> **交易计划**",
            f"> 入场: `{p.entry_price:.4f}`",
            f"> 止损: `{p.stop_loss:.4f}` ({pct(p.stop_loss)}%)",
            f"> 止盈1: `{p.take_profit_1:.4f}` ({pct(p.take_profit_1)}%)",
            f"> 止盈2: `{p.take_profit_2:.4f}` ({pct(p.take_profit_2)}%)",
            f"> 风险回报比: `1:{p.risk_reward:.2f}`",
            ">",
            "> **计算依据**",
        ]

        # 颈线 / 边界
        if p.neckline is not None:
            neck_v = p.neckline.value_at(p.breakout_index)
            lines.append(f"> 颈线: `{neck_v:.4f}`")
        elif p.upper_boundary is not None and is_long:
            lines.append(
                f"> 上边界: `{p.upper_boundary.value_at(p.breakout_index):.4f}`")
        elif p.lower_boundary is not None and not is_long:
            lines.append(
                f"> 下边界: `{p.lower_boundary.value_at(p.breakout_index):.4f}`")

        lines.extend([
            f"> 形态高度: `{p.height:.4f}`",
            f"> 突破价: `{p.breakout_price:.4f}` "
            f"({p.breakout_magnitude_atr:.2f}×ATR)",
            f"> 量能: `{p.volume_ratio:.2f}×` 均量",
            f"> 突破距今: `{p.breakout_age}` 根K线",
            "> 多周期共振: "
            f"`{', '.join(p.resonant_with) if p.resonant_with else '无'}`",
        ])

        lines.extend([
            ">",
            f"> {self.disclaimer}",
        ])

        content = "\n".join(lines)

        # 企微 markdown 上限 4096 字节
        if len(content.encode("utf-8")) > 4000:
            logger.warning("markdown 内容接近上限，已截断")
            content = content[:1500]

        return {"msgtype": "markdown", "markdown": {"content": content}}

    @staticmethod
    def build_image(image_bytes: bytes) -> dict:
        """构造图片消息体（base64 + md5）"""
        return {
            "msgtype": "image",
            "image": {
                "base64": base64.b64encode(image_bytes).decode("ascii"),
                "md5": hashlib.md5(image_bytes).hexdigest(),
            },
        }

    # ---------- 发送 ----------

    def push(self, p: Pattern, image_bytes: Optional[bytes] = None) -> bool:
        """
        推送一条信号（markdown + 可选图片）。

        返回是否成功。dry_run 模式下只打印不发送。
        """
        md_payload = self.build_markdown(p)

        if self.dry_run:
            print("\n" + "=" * 60)
            print(f"[DRY-RUN] 将推送: {p.symbol} {p.interval} "
                  f"{p.pattern_type} {p.direction.value}")
            print("=" * 60)
            print(md_payload["markdown"]["content"])
            if image_bytes:
                print(f"\n[DRY-RUN] 附带图片: {len(image_bytes) / 1024:.1f} KB")
            print()
            self.sent_count += 1
            return True

        if not self.webhook_url:
            logger.error("未配置 webhook_url，无法推送")
            self.failed_count += 1
            return False

        if requests is None:
            logger.error("requests 未安装，无法推送")
            self.failed_count += 1
            return False

        # 1. 文字
        self.limiter.acquire()
        ok1 = self._post(md_payload)

        # 2. 图片（间隔一下避免触发频率限制）
        ok2 = True
        if image_bytes:
            time.sleep(1)
            self.limiter.acquire()
            ok2 = self._post(self.build_image(image_bytes))

        if ok1 and ok2:
            self.sent_count += 1
        else:
            self.failed_count += 1
        return ok1 and ok2

    def _post(self, payload: dict, retry: int = 2) -> bool:
        """实际发送，失败重试"""
        for attempt in range(retry + 1):
            try:
                resp = requests.post(self.webhook_url, json=payload,
                                     timeout=10)
                data = resp.json()
                code = data.get("errcode")
                if code == 0:
                    return True
                logger.error(f"企微返回错误 errcode={code} "
                             f"errmsg={data.get('errmsg')}")
                if code == 45009:      # 图片过大
                    logger.error("图片超过 2MB 限制")
                    return False
                time.sleep(2)
            except Exception as e:
                logger.error(f"推送请求异常 (attempt {attempt + 1}): {e}")
                time.sleep(2)
        return False

    def push_summary(self, scanned: int, candidates: int,
                     signals: int, duration: float):
        """推送本次扫描摘要（确认服务存活，0 信号时也发）"""
        signal_note = f"触发信号: `{signals}`"
        if signals == 0:
            signal_note += "（本次无满足条件的形态）"
        content = (
            f"扫描完成\n"
            f"> 扫描对数: `{scanned}`\n"
            f"> 候选形态: `{candidates}`\n"
            f"> {signal_note}\n"
            f"> 耗时: `{duration:.1f}s`"
        )
        if self.dry_run:
            print(f"[DRY-RUN] 摘要: {content}")
            return True
        if not self.webhook_url:
            return False
        self.limiter.acquire()
        return self._post({"msgtype": "markdown",
                           "markdown": {"content": content}})

    def get_stats(self) -> dict:
        return {"sent": self.sent_count, "failed": self.failed_count}
