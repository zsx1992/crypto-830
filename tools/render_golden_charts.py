# -*- coding: utf-8 -*-
"""
复盘渲染：把 state.json 里的推送记录，还原成当时的形态标注图，供人工金标准标注。

为什么必须重新渲染:
  推送时生成的 PNG 只存在于 GitHub Actions 的临时 runner 上，扫描结束即丢弃，
  本机拿不到；而 state.json 只存了 endMs / breakoutIndex / breakoutPrice 这类
  摘要字段，**没存形态的边界线**，所以无法直接复现原图 —— 必须重跑检测器。

为什么能还原成"当时那张图":
  OKXClient.get_klines 加了 end_time_ms 参数（2026-09-18），能只取该时刻之前的
  K 线。配合各周期的 kline_counts，窗口与线上扫描完全一致，因此复现出的形态
  就是当初推送的那一个（避免前视偏差）。

匹配方式:
  重跑后同一窗口可能检出多个形态。用 breakoutPrice 做绝对价格匹配（最稳），
  辅以 pattern_type + direction。

用法（云端）:
  python tools/render_golden_charts.py --state state/state.json --out charts_golden

本机冒烟（无网络，用缓存验证渲染链路）:
  python tools/render_golden_charts.py --cache-dir data --no-end-time --limit 5
"""
import os
import sys
import json
import time
import argparse
import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

import yaml  # noqa: E402
from market_data import Kline, OkxClient  # noqa: E402
from detector import PatternEngine  # noqa: E402
from chart import render_pattern_chart  # noqa: E402

# 每根 K 线的毫秒时长（用于计算形态末端之后的 lookahead）
BAR_MS = {"15m": 15 * 60_000, "1h": 60 * 60_000,
          "4h": 240 * 60_000, "1d": 24 * 60 * 60_000}

# 形态末端之后再往后看多少根（让突破能被检出）
LOOKAHEAD_BARS = 24


def load_csv(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            p = ln.strip().split(",")
            if len(p) < 7:
                continue
            try:
                int(p[0])
            except ValueError:
                continue
            try:
                rows.append(Kline(
                    openTime=int(p[0]), open=float(p[1]), high=float(p[2]),
                    low=float(p[3]), close=float(p[4]), volume=float(p[5]),
                    closeTime=int(p[6]),
                    quoteVolume=float(p[7]) if len(p) > 7 else 0.0))
            except (ValueError, IndexError):
                continue
    rows.sort(key=lambda k: k.openTime)
    return rows


def load_cache_klines(cache_dir, symbol, interval, limit):
    """本机离线模式：从 data/klines_<iv>/<SYM>/<SYM>.csv 读，取最后 limit 根"""
    for rel in (os.path.join(symbol, "%s.csv" % symbol), "%s.csv" % symbol):
        p = os.path.join(cache_dir, "klines_%s" % interval, rel)
        if os.path.isfile(p):
            ks = load_csv(p)
            return ks[-limit:] if len(ks) > limit else ks
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state/state.json")
    ap.add_argument("--out", default="charts_golden")
    ap.add_argument("--limit", type=int, default=0, help="只渲染前 N 条(0=全部)")
    ap.add_argument("--cache-dir", default="", help="离线模式: 从该目录读缓存")
    ap.add_argument("--no-end-time", action="store_true",
                    help="离线冒烟: 忽略 endMs, 直接取最新窗口")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(os.path.join(_ROOT, "config.yaml"),
                              encoding="utf-8"))
    kline_counts = cfg.get("data", {}).get("kline_counts", {}) or \
        cfg.get("kline_counts", {})
    chart_candles = cfg.get("notification", {}).get("chart_candles", {}) or {}
    # 直接用 OKX：云端 Binance 被 451 封禁，走 MarketDataClient 会每个标的
    # 先白试一次 Binance 再回退，60 个标的纯属浪费。线上 source_stats 也全是 okx。
    client = None if args.cache_dir else OkxClient(timeout=30)
    eng = PatternEngine(cfg)

    recs = json.load(open(args.state, encoding="utf-8")).get("pushedSignals", [])
    if args.limit:
        recs = recs[:args.limit]
    print("待渲染推送记录: %d 条" % len(recs))

    os.makedirs(args.out, exist_ok=True)
    items = []
    cache = {}       # (symbol, interval) -> List[Kline]

    for i, r in enumerate(recs, 1):
        sym, iv = r.get("symbol"), r.get("interval")
        ptype = r.get("patternType")
        end_ms = r.get("endMs")
        bp = r.get("breakoutPrice")
        key = (sym, iv)

        if key not in cache:
            limit = int(kline_counts.get(iv, 240))
            if args.cache_dir:
                ks = load_cache_klines(args.cache_dir, sym, iv, limit)
            else:
                end_at = None
                if end_ms and not args.no_end_time:
                    end_at = end_ms + LOOKAHEAD_BARS * BAR_MS.get(iv, 3_600_000)
                try:
                    ks = client.get_klines(sym, iv, limit, end_time_ms=end_at)
                except Exception as e:
                    print("  取数失败 %s %s: %s" % (sym, iv, str(e)[:60]))
                    ks = []
            cache[key] = ks or []
            time.sleep(0.12)

        ks = cache[key]
        item = {
            "id": "%04d" % i, "symbol": sym, "interval": iv,
            "patternType": ptype, "direction": r.get("direction"),
            "strength": r.get("strength"), "pushedAt": r.get("pushedAt"),
            "breakoutPrice": bp, "status": "no_data", "image": None,
        }
        if len(ks) < 60:
            items.append(item)
            print("  [%04d] %-14s %-4s 数据不足(%d根)" % (i, sym, iv, len(ks)))
            continue

        try:
            pats = eng.scan_multiscale(ks, symbol=sym, interval=iv)
        except Exception as e:
            items.append(item)
            print("  [%04d] %-14s %-4s 检测异常 %s" % (i, sym, iv, str(e)[:50]))
            continue

        cands = [p for p in pats
                 if p.pattern_type == ptype
                 and str(p.direction).replace("Direction.", "") ==
                 str(r.get("direction"))]
        pool = cands or [p for p in pats if p.pattern_type == ptype]

        best, best_err = None, None
        for p in pool:
            if bp and getattr(p, "breakout_price", None):
                err = abs(p.breakout_price - bp) / bp
            elif end_ms and p.pivots:
                err = abs(p.pivots[-1].time - end_ms) / 86_400_000.0
            else:
                err = 9.9
            if best_err is None or err < best_err:
                best, best_err = p, err

        if best is None or (best_err is not None and best_err > 0.05):
            item["status"] = "no_match"
            item["match_err"] = round(best_err, 4) if best_err else None
            items.append(item)
            print("  [%04d] %-14s %-4s %-18s 未匹配(err=%s)"
                  % (i, sym, iv, ptype, best_err))
            continue

        try:
            png = render_pattern_chart(ks, best,
                                       candles=chart_candles or 120)
        except Exception as e:
            item["status"] = "render_fail"
            items.append(item)
            print("  [%04d] %-14s %-4s 渲染异常 %s" % (i, sym, iv, str(e)[:50]))
            continue

        if not png:
            item["status"] = "render_empty"
            items.append(item)
            continue

        fn = "%s_%s_%s_%s.png" % (item["id"], sym, iv, ptype)
        path = os.path.join(args.out, fn)
        with open(path, "wb") as f:
            f.write(png)
        item["image"] = path.replace("\\", "/")
        item["status"] = "ok"
        item["match_err"] = round(best_err, 5)
        items.append(item)
        print("  [%04d] %-14s %-4s %-18s OK err=%.4f"
              % (i, sym, iv, ptype, best_err))

    manifest = {
        "generated_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
        "source_state": args.state,
        "total": len(items),
        "ok": sum(1 for x in items if x["status"] == "ok"),
        "items": items,
    }
    mp = os.path.join(args.out, "manifest.json")
    json.dump(manifest, open(mp, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n成功 %d / %d  -> %s" % (manifest["ok"], len(items), mp))


if __name__ == "__main__":
    main()
