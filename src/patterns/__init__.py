# -*- coding: utf-8 -*-
"""
形态识别包

已实现（P0，覆盖样本集 84.3% 的标注）：
  DoubleTopBottomDetector   —— 双顶 / 双底 (样本37次 + 归入的双底)
  HeadShouldersDetector     —— 头肩顶 / 头肩底 (样本41次)
  TriangleDetector          —— 上升/下降/对称三角形 (样本65次)

已实现（P1）：
  FlagDetector              —— 旗形 (样本7次)
  WedgeDetector             —— 上升/下降楔形 (样本5次)

未实现（P2，C级形态，主观性太强不建议自动化）：
  圆弧顶/底 (样本2次)
  V型反转   (样本1次)
  杯柄      (样本1次)
"""

from .base import (
    Pattern, Line, Direction, PatternStatus, BaseDetector,
    fit_trendline, count_touches, horizontal_line,
    is_flat, is_rising, is_falling,
    check_breakout, find_breakout_index, calc_volume_ratio, calc_trade_levels,
    set_trade_level_params,
)
from .double import DoubleTopBottomDetector
from .head_shoulders import HeadShouldersDetector
from .triangle import TriangleDetector
from .flag_wedge import FlagDetector, WedgeDetector

__all__ = [
    "Pattern", "Line", "Direction", "PatternStatus", "BaseDetector",
    "fit_trendline", "count_touches", "horizontal_line",
    "is_flat", "is_rising", "is_falling",
    "check_breakout", "find_breakout_index", "calc_volume_ratio",
    "calc_trade_levels", "set_trade_level_params",
    "DoubleTopBottomDetector", "HeadShouldersDetector",
    "TriangleDetector", "FlagDetector", "WedgeDetector",
]
