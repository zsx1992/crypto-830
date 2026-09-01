#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 Binance 官方历史数据站 (data.binance.vision) 批量下载 USDT 永续合约 K 线。
国内可直连（AWS S3 / CloudFront），无需梯子。

用法:
    python tools/download_binance_klines.py                # 全量下载(默认40标的×12个月)
    python tools/download_binance_klines.py --symbols BTCUSDT,ETHUSDT --months 3   # 指定标的和月数
    python tools/download_binance_klines.py --list-only    # 只打印计划不下载

输出:
    data/klines_4h/{SYMBOL}.csv    合并后的完整K线(4h)
    data/klines_4h/{SYMBOL}_meta.json  每个标的的实际数据信息
"""
import argparse
import json
import os
import re
import sys
import time
import zipfile

import requests

# 强制不走环境变量代理(WorkBuddy 的失效代理 127.0.0.1:56352)
SESSION = requests.Session()
SESSION.trust_env = False
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (research/backtest)"})

BASE = "https://data.binance.vision/data/futures/um/monthly/klines"
# 全局：由 --interval 设置，供 download_month 使用
CUR_INTERVAL = "4h"

# Binance USDT 永续主流合约（按流动性/成交量经验排序，覆盖回测所需广度）
DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT", "DOTUSDT", "TRXUSDT",
    "MATICUSDT", "SHIBUSDT", "UNIUSDT", "ATOMUSDT", "ETCUSDT", "FILUSDT",
    "ICPUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "SUIUSDT",
    "TIAUSDT", "SEIUSDT", "INJUSDT", "WLDUSDT", "PEPEUSDT", "BONKUSDT",
    "WIFUSDT", "ORDIUSDT", "JUPUSDT", "PYTHUSDT", "ENAUSDT", "EIGENUSDT",
    "TAOUSDT", "RENDERUSDT", "FETUSDT", "GRTUSDT", "SANDUSDT", "MANAUSDT",
    "AXSUSDT", "GALAUSDT", "IMXUSDT", "DYDXUSDT", "AAVEUSDT", "MKRUSDT",
    "CRVUSDT", "LDOUSDT", "HBARUSDT", "XLMUSDT", "EOSUSDT", "VETUSDT",
]

INTERVAL_LEGACY = "4h"  # 保留旧常量，避免其它引用断裂（实际由 --interval 控制）
HEADER = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore"


def data_dir_for(interval):
    """按周期返回输出目录（4h → data/klines_4h, 1d → data/klines_1d）"""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", f"klines_{interval}")


def gen_months(n_months, end_year=None, end_month=None):
    """生成最近 n_months 个 (YYYY, MM) 月份，从最新往前。

    注意：Binance 月度文件在次月 1~2 号才发布，当月文件不存在。
    因此默认从上个月开始回推（避免 404），多生成 2 个候选月兜底。
    """
    now = time.gmtime()
    # 默认起点：上个月
    sy, sm = (now.tm_year, now.tm_mon - 1)
    if sm == 0:
        sy, sm = sy - 1, 12
    y, m = end_year or sy, end_month or sm
    out = []
    for _ in range(n_months + 2):  # +2 候选月，凑不足时由调用方继续往前
        out.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return out


def download_month(symbol, year, month, out_dir):
    """下载单个月的 zip 并解压，返回 CSV 行数；文件不存在(404)返回 None。"""
    zname = f"{symbol}-{CUR_INTERVAL}-{year:04d}-{month:02d}.zip"
    url = f"{BASE}/{symbol}/{CUR_INTERVAL}/{zname}"
    zpath = os.path.join(out_dir, zname)
    # 已下载过且非空 → 跳过
    if os.path.exists(zpath) and os.path.getsize(zpath) > 100:
        return _extract(zpath, symbol, out_dir)
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=30)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            with open(zpath, "wb") as f:
                f.write(r.content)
            return _extract(zpath, symbol, out_dir)
        except Exception as e:
            if attempt == 2:
                print(f"  ! {symbol} {year}-{month:02d} 下载失败: {type(e).__name__} {e}")
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def _extract(zpath, symbol, out_dir):
    """解压 zip 里的 CSV，返回行数。"""
    try:
        with zipfile.ZipFile(zpath) as zf:
            csv_name = zf.namelist()[0]
            with zf.open(csv_name) as f:
                content = f.read().decode("utf-8")
        lines = content.strip().split("\n")
        data_lines = [ln for ln in lines if ln and not ln.startswith("open_time")]
        if not data_lines:
            return 0
        # 追加到合并文件（去重表头）
        csv_path = os.path.join(out_dir, f"{symbol}.csv")
        write_header = not os.path.exists(csv_path)
        with open(csv_path, "a", encoding="utf-8") as f:
            if write_header:
                f.write(HEADER + "\n")
            f.write("\n".join(data_lines) + "\n")
        return len(data_lines)
    except Exception as e:
        print(f"  ! 解压失败 {zpath}: {type(e).__name__} {e}")
        return 0


def dedupe_and_sort(symbol, out_dir):
    """按 open_time 去重排序（月度文件边界可能重叠）。"""
    csv_path = os.path.join(out_dir, f"{symbol}.csv")
    if not os.path.exists(csv_path):
        return 0
    rows = {}
    with open(csv_path, encoding="utf-8") as f:
        for i, ln in enumerate(f):
            ln = ln.strip()
            if not ln or ln.startswith("open_time"):
                continue
            parts = ln.split(",")
            rows[int(parts[0])] = ln  # open_time 去重，保留最后一个
    ordered = [rows[k] for k in sorted(rows)]
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write(HEADER + "\n")
        f.write("\n".join(ordered) + "\n")
    return len(ordered)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None, help="逗号分隔的标的列表")
    ap.add_argument("--months", type=int, default=12, help="下载最近几个月(默认12)")
    ap.add_argument("--interval", default="4h", help="K线周期(默认4h, 可1d/1h/15m)")
    ap.add_argument("--list-only", action="store_true", help="只打印计划")
    ap.add_argument("--sleep", type=float, default=0.15, help="请求间隔秒(默认0.15)")
    args = ap.parse_args()

    global CUR_INTERVAL
    CUR_INTERVAL = args.interval
    DATA_DIR = data_dir_for(args.interval)
    symbols = [s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()] if args.symbols else DEFAULT_SYMBOLS
    months = gen_months(args.months)
    os.makedirs(DATA_DIR, exist_ok=True)

    print(f"计划下载 {len(symbols)} 标的 × {len(months)} 个月 ({months[0][0]}-{months[0][1]:02d} ~ {months[-1][0]}-{months[-1][1]:02d}) 周期={CUR_INTERVAL}")
    print(f"输出目录: {DATA_DIR}")
    if args.list_only:
        return

    ok, empty, failed = [], [], []
    t0 = time.time()
    for idx, sym in enumerate(symbols):
        sym_dir = os.path.join(DATA_DIR, sym)
        os.makedirs(sym_dir, exist_ok=True)
        total_rows = 0
        got_months = 0
        got_any = False
        for (y, m) in months:
            if got_months >= args.months:
                break
            n = download_month(sym, y, m, sym_dir)
            if n is None:
                continue  # 404：该月无数据（新上架合约/文件未发布）
            got_any = True
            got_months += 1
            total_rows += n
            time.sleep(args.sleep)
        if got_any:
            final = dedupe_and_sort(sym, sym_dir)
            ok.append(sym)
            print(f"[{idx+1}/{len(symbols)}] {sym}: {final} 根K线(合并去重)")
        else:
            failed.append(sym)
            print(f"[{idx+1}/{len(symbols)}] {sym}: 无任何数据(可能未上架)")
        time.sleep(args.sleep * 2)

    # 写 meta
    meta = {
        "interval": CUR_INTERVAL,
        "months": [f"{y}-{m:02d}" for y, m in months],
        "symbols_ok": ok,
        "symbols_failed": failed,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    with open(os.path.join(DATA_DIR, "_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"\n完成: 成功 {len(ok)} / 失败 {len(failed)} / 耗时 {meta['elapsed_sec']}s")
    print(f"失败标的: {failed}")


if __name__ == "__main__":
    main()
