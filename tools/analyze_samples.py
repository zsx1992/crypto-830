#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
样本集分析器：把用户手绘标注的行情截图目录，解析成结构化的标注样本。

设计前提（重要）：
  标注图只用于【离线标定与回测金标准】，不进入运行链路。
  扫描器在 GitHub Actions 上跑的是原始 OHLCV 规则引擎，从不"看图"。

用法：
  python tools/analyze_samples.py --src "C:/path/to/images" --out samples/labels.json

输出：
  - 形态 / 周期 / 标的 的词频分布（用于决定哪些形态值得做 A 级自动化）
  - 结构化样本 JSON（用于回测对拍）
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

# 形态关键词 -> 归一化后的形态 ID
# 注意：这里刻意把"别名"合并，比如 上直三 = 上升三角形（直角），对三 = 对称三角形
PATTERN_ALIASES = [
    ("头肩顶", "head_shoulders_top"),
    ("头肩底", "head_shoulders_bottom"),
    ("头肩", "head_shoulders_unspecified"),
    ("双顶", "double_top"),
    ("双底", "double_bottom"),
    ("三重顶", "triple_top"),
    ("三重底", "triple_bottom"),
    ("w底", "double_bottom"),
    ("W底", "double_bottom"),
    ("m顶", "double_top"),
    ("M顶", "double_top"),
    ("圆弧底", "rounding_bottom"),
    ("圆弧顶", "rounding_top"),
    ("圆底", "rounding_bottom"),
    ("圆顶", "rounding_top"),
    ("杯柄", "cup_and_handle"),
    ("上升三角", "ascending_triangle"),
    ("上直三", "ascending_triangle"),
    ("下降三角", "descending_triangle"),
    ("下直三", "descending_triangle"),
    ("对称三角", "symmetrical_triangle"),
    ("对三", "symmetrical_triangle"),
    ("收敛三角", "symmetrical_triangle"),
    ("三角", "triangle_unspecified"),
    ("三角旗", "pennant"),
    ("旗形", "flag"),
    ("上升楔形", "rising_wedge"),
    ("上涨楔形", "rising_wedge"),
    ("下降楔形", "falling_wedge"),
    ("楔形", "wedge_unspecified"),
    ("矩形", "rectangle"),
    ("箱体", "rectangle"),
    ("通道", "channel"),
    ("v形", "v_reversal"),
    ("V形", "v_reversal"),
    ("v型", "v_reversal"),
    ("V型", "v_reversal"),
]

# 周期关键词（长词优先，避免 15min 被 5min 误吃）
TIMEFRAMES = ["15min", "30min", "1h", "2h", "4h", "1d", "1w", "15m", "30m", "5min", "1min"]

# 交易意图 / 情景关键词
CONTEXT_KEYWORDS = [
    "中继", "反转", "突破", "回踩", "跌破", "假跌破", "假突破",
    "支撑", "压力", "蓄势", "开多", "开空", "做多", "做空",
    "试仓", "止盈", "止损", "目标", "颈线", "诱空", "诱多",
    "顶部", "底部", "高位", "低位", "观察", "精准",
]

SAMPLE_ID_RE = re.compile(r"【(\d+)】")
DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
TIME_RE = re.compile(r"\[(\d{2})\s+(\d{2})\]")


# 周期写法归一化：交易所/标注里的写法不统一，统一成扫描器用的写法
TIMEFRAME_ALIASES = {
    "15min": "15m", "15m": "15m",
    "30min": "30m", "30m": "30m",
    "5min": "5m", "5m": "5m",
    "1min": "1m", "1m": "1m",
    "1h": "1h", "2h": "2h", "4h": "4h",
    "1d": "1d", "1w": "1w",
}


def normalize_timeframe(tf: str) -> str:
    """把 15min / 15m 这类写法统一成 15m，便于与 config.yaml 对齐。"""
    return TIMEFRAME_ALIASES.get(tf.lower(), tf)


def drop_unspecified_if_specific(patterns: list) -> list:
    """
    去歧义：如果同时命中了具体形态和它的"未指定"版本，丢掉后者。

    例："头肩顶" 会同时匹配到 head_shoulders_top 和 head_shoulders_unspecified，
    前者才是真实标注意图。规则：对每条 *_unspecified，若存在同前缀的具体形态则删除。
    """
    specific_prefixes = set()
    for p in patterns:
        if not p.endswith("_unspecified"):
            specific_prefixes.add(p)

    result = []
    for p in patterns:
        if p.endswith("_unspecified"):
            # 例：triangle_unspecified 的前缀是 triangle
            prefix = p[: -len("_unspecified")]
            # 若任一具体形态以该前缀开头（如 triangle_unspecified vs ascending_triangle 需特殊处理）
            # 这里用更直接的方式：检查是否有具体形态属于同一个"族"
            if is_same_family_covered(prefix, patterns):
                continue
        result.append(p)
    return result


def is_same_family_covered(unspecified_prefix: str, patterns: list) -> bool:
    """
    判断某条 unspecified 形态是否已被同族的具体形态覆盖。

    族的对应关系（前缀 -> 该族的具体形态集合）：
      head_shoulders -> {head_shoulders_top, head_shoulders_bottom}
      triangle       -> {ascending_triangle, descending_triangle, symmetrical_triangle}
      wedge          -> {rising_wedge, falling_wedge}
    """
    FAMILY_MEMBERS = {
        "head_shoulders": {"head_shoulders_top", "head_shoulders_bottom"},
        "triangle": {"ascending_triangle", "descending_triangle",
                     "symmetrical_triangle"},
        "wedge": {"rising_wedge", "falling_wedge"},
    }
    members = FAMILY_MEMBERS.get(unspecified_prefix)
    if not members:
        return False
    return any(m in patterns for m in members)


def parse_filename(name: str) -> dict:
    """从一个标注图文件名里抠出：编号、日期、标的、周期、形态、情景标签。"""
    stem = Path(name).stem
    rec = {
        "file": name,
        "sample_id": None,
        "date": None,
        "time": None,
        "detected_at": None,
        "symbol": None,
        "timeframe": None,
        "patterns": [],
        "context": [],
        "note": "",
    }

    m = SAMPLE_ID_RE.search(stem)
    if m:
        rec["sample_id"] = int(m.group(1))
        # 编号之后的部分才是真正的标注正文
        stem_body = stem[m.end():]
    else:
        stem_body = stem

    d = DATE_RE.search(stem)
    if d:
        rec["date"] = f"{d.group(1)}-{int(d.group(2)):02d}-{int(d.group(3)):02d}"

    # 从文件名前缀 [HH MM] 提取截图时刻——这是回测防未来函数的关键
    t = TIME_RE.search(stem)
    if t:
        rec["time"] = f"{t.group(1)}:{t.group(2)}"
        if rec["date"]:
            rec["detected_at"] = f"{rec['date']}T{rec['time']}:00"

    low = stem_body.lower()

    # 周期：优先匹配长写法，并归一化为统一写法
    for tf in TIMEFRAMES:
        if tf.lower() in low:
            rec["timeframe"] = normalize_timeframe(tf)
            break

    # 形态：按别名表顺序扫，允许一张图命中多个（例如"头肩底+杯柄"）
    for zh, pid in PATTERN_ALIASES:
        if zh.lower() in low:
            if pid not in rec["patterns"]:
                rec["patterns"].append(pid)
    # 去歧义：命中了具体形态就丢掉 "xxx_unspecified"
    # （"头肩顶" 会同时命中 "头肩顶" 和 "头肩"，后者是冗余的）
    rec["patterns"] = drop_unspecified_if_specific(rec["patterns"])

    for kw in CONTEXT_KEYWORDS:
        if kw in stem_body:
            rec["context"].append(kw)

    # 标的：形态/周期词之前的第一个英文 token
    cleaned = stem_body
    for zh, _ in PATTERN_ALIASES:
        cleaned = cleaned.replace(zh, " ")
    for tf in TIMEFRAMES:
        cleaned = re.sub(tf, " ", cleaned, flags=re.IGNORECASE)
    for kw in CONTEXT_KEYWORDS:
        cleaned = cleaned.replace(kw, " ")

    toks = re.findall(r"[A-Za-z][A-Za-z0-9]{1,9}", cleaned)
    # 注意：这里不要过滤 "link" —— LINK 是真实交易标的
    # 只过滤明确的文件格式/计价词
    stop = {"png", "webp", "jpg", "jpeg", "usdt", "perp", "swap"}
    cand = [t.lower() for t in toks if t.lower() not in stop]
    if cand:
        rec["symbol"] = cand[0].upper()

    rec["note"] = stem_body.strip()
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="标注图目录")
    ap.add_argument("--out", default="samples/labels.json", help="输出 JSON")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.is_dir():
        print(f"[x] 目录不存在: {src}", file=sys.stderr)
        return 1

    files = [p for p in sorted(src.iterdir())
             if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}]

    records = [parse_filename(p.name) for p in files]

    pat_counter = Counter()
    tf_counter = Counter()
    sym_counter = Counter()
    ctx_counter = Counter()

    for r in records:
        for p in r["patterns"]:
            pat_counter[p] += 1
        if r["timeframe"]:
            tf_counter[r["timeframe"]] += 1
        if r["symbol"]:
            sym_counter[r["symbol"]] += 1
        for c in r["context"]:
            ctx_counter[c] += 1

    total = len(records)
    labeled = sum(1 for r in records if r["patterns"])

    print(f"\n样本总数: {total}    含形态标注: {labeled}    未标注: {total - labeled}\n")

    def show(title, counter, n=20):
        print(f"--- {title} ---")
        for k, v in counter.most_common(n):
            bar = "#" * max(1, round(v / counter.most_common(1)[0][1] * 30))
            print(f"  {k:<28} {v:>4}  {bar}")
        print()

    show("形态分布", pat_counter)
    show("周期分布", tf_counter, 12)
    show("标的出现次数（前20）", sym_counter, 20)
    show("情景关键词", ctx_counter, 20)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({
            "source_dir": str(src),
            "total": total,
            "labeled": labeled,
            "pattern_dist": dict(pat_counter),
            "timeframe_dist": dict(tf_counter),
            "context_dist": dict(ctx_counter),
            "samples": records,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[ok] 结构化样本已写入: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
