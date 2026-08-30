# -*- coding: utf-8 -*-
"""
单K线确认层 —— 结合 TA-Lib 蜡烛图形态

定位：
  我们的检测器负责【多摆点结构】（头肩/双底/三角形，几十根K线）。
  TA-Lib 负责【单根K线确认】（锤子线/吞没/晨星等，1~3根K线）。
  两者互补：结构给出方向与价位，K线形态给出"突破那一刻的力度"。

设计：
  - 优先用 TA-Lib（C 库，61 种形态，快且成熟）
  - TA-Lib 不可用时降级到内置 numpy 实现（仅核心形态），功能不中断
  - 返回值带方向：如 "吞没形态(看涨)" / "流星线(看跌)"
"""

import logging
from typing import List

_SRC_DIR = None  # 由调用方负责 sys.path

try:
    import numpy as np
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False

try:
    import talib
    HAVE_TALIB = True
except Exception:
    HAVE_TALIB = False

from market_data import Kline

logger = logging.getLogger(__name__)

# (TA-Lib 函数名, 中文名)
# 只挑选对"突破确认"最有判别力、且图形直观的形态
TALIB_PATTERNS = [
    ("CDLHAMMER", "锤子线"),
    ("CDLINVERTEDHAMMER", "倒锤子"),
    ("CDLSHOOTINGSTAR", "流星线"),
    ("CDLHANGINGMAN", "上吊线"),
    ("CDLENGULFING", "吞没形态"),
    ("CDLMORNINGSTAR", "晨星"),
    ("CDLEVENINGSTAR", "暮星"),
    ("CDLPIERCING", "刺透形态"),
    ("CDLDARKCLOUDCOVER", "乌云盖顶"),
    ("CDL3WHITESOLDIERS", "三白兵"),
    ("CDL3BLACKCROWS", "三只乌鸦"),
    ("CDLHARAMI", "孕育形态"),
    ("CDLDRAGONFLYDOJI", "蜻蜓十字"),
    ("CDLGRAVESTONEDOJI", "墓碑十字"),
    ("CDLDOJI", "十字星"),
    ("CDLMARUBOZU", "光头光脚"),
]

# 中性形态：TA-Lib 返回正数但并非看涨含义（如十字星 = 犹豫不定）
# 这些不标注方向，避免误导
NEUTRAL_PATTERNS = {"CDLDOJI", "CDLDRAGONFLYDOJI", "CDLGRAVESTONEDOJI"}


def detect_at(klines: List[Kline], index: int) -> List[str]:
    """
    检测第 index 根K线的蜡烛图形态（含方向标注）。

    返回示例：["锤子线(看涨)", "十字星"] 
    """
    if not klines or not (0 <= index < len(klines)):
        return []

    if HAVE_TALIB:
        return _detect_talib(klines, index)
    if HAVE_NUMPY:
        return _detect_numpy(klines, index)
    return []


# ============================================================
#  TA-Lib 实现
# ============================================================

def _detect_talib(klines: List[Kline], index: int) -> List[str]:
    o = np.array([k.open for k in klines], dtype=float)
    h = np.array([k.high for k in klines], dtype=float)
    l = np.array([k.low for k in klines], dtype=float)
    c = np.array([k.close for k in klines], dtype=float)

    found = []
    for func_name, cn_name in TALIB_PATTERNS:
        try:
            fn = getattr(talib, func_name)
            out = fn(o, h, l, c)
            v = float(out[index])
        except Exception:
            continue
        if v > 0.5:
            if func_name in NEUTRAL_PATTERNS:
                found.append(cn_name)          # 中性：不加方向
            else:
                found.append(f"{cn_name}(看涨)")
        elif v < -0.5:
            found.append(f"{cn_name}(看跌)")
        # 注意：CDLDOJI 等中性形态可能只返回 0/100/-100，
        # v=0 表示无信号，忽略
    return found


# ============================================================
#  内置 numpy 实现（TA-Lib 不可用时的降级）
# ============================================================

def _detect_numpy(klines: List[Kline], index: int) -> List[str]:
    """核心形态的内置实现（锤子/流星/十字/吞没/晨星/三乌鸦）"""
    if index < 1:
        return []
    k = klines[index]
    found = []

    rng = k.high - k.low
    if rng <= 0:
        return []
    body = abs(k.close - k.open)
    upper = k.high - max(k.open, k.close)
    lower = min(k.open, k.close) - k.low

    # 十字星：实体极小，上下影线都在
    if body <= 0.1 * rng:
        found.append("十字星")

    # 锤子线：下影线长、上影线短（看涨）
    if lower >= 2 * body and upper <= 0.3 * lower and body > 0:
        found.append("锤子线(看涨)")

    # 流星线：上影线长、下影线短（看跌）
    if upper >= 2 * body and lower <= 0.3 * upper and body > 0:
        found.append("流星线(看跌)")

    # 吞没形态：当前实体完全包住前一根实体
    prev = klines[index - 1]
    prev_body_low = min(prev.open, prev.close)
    prev_body_high = max(prev.open, prev.close)
    cur_body_low = min(k.open, k.close)
    cur_body_high = max(k.open, k.close)
    if (cur_body_low <= prev_body_low and cur_body_high >= prev_body_high
            and body > prev_body_high - prev_body_low):
        if k.close > k.open:
            found.append("吞没形态(看涨)")
        else:
            found.append("吞没形态(看跌)")

    # 三只乌鸦 / 三白兵（看跌/看涨三连）
    if index >= 2:
        k2 = klines[index - 2]
        k1 = klines[index - 1]
        if (k.close < k.open and k1.close < k1.open and k2.close < k2.open
                and k.close < k1.close < k2.close):
            found.append("三只乌鸦(看跌)")
        if (k.close > k.open and k1.close > k1.open and k2.close > k2.open
                and k.close > k1.close > k2.close):
            found.append("三白兵(看涨)")

    return found


def summary() -> str:
    """返回当前使用的引擎说明（用于日志/诊断）"""
    if HAVE_TALIB:
        return f"TA-Lib {getattr(talib, '__version__', '?')} 61种形态"
    if HAVE_NUMPY:
        return "内置numpy实现（TA-Lib未安装，仅核心形态）"
    return "K线确认不可用（numpy也未安装）"
