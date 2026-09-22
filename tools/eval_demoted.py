"""eval_demoted.py — 降级规则实战复盘 (2026-09-20)

问题: 2026-09-18 上线的两条降级规则(头肩顶全周期/15m逆1h趋势)拦下的信号,
     之后走势是否真的比正式推送的差? —— 用真实K线回答。

方法: 从 state.json 取近 N 天的 pushedSignals / observedSignals,
     entry = pushedAt 之后第一根收盘价, 统计方向对齐收益:
     - ret_now:  当前价相对 entry 的方向收益%
     - max_fav:  期间最大有利波动%
     - max_adv:  期间最大不利波动%
     不做 R 倍数判定(state 不存 SL/TP), 只做组间对比。

用法: python tools/eval_demoted.py [--days 2] [--out eval/demoted_report.json]
"""
import json
import os
import sys
import datetime
import argparse
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.market_data import OkxClient  # noqa: E402

DEMOTE_TYPES = {"head_shoulders_top"}  # 15m逆势信号在 state 里无标记, 用类型+周期近似


def ts(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state/state.json")
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--out", default="eval/demoted_report.json")
    args = ap.parse_args()

    st = json.load(open(args.state, encoding="utf-8"))
    cut = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(days=args.days)).isoformat()
    cut_s = cut[:19]

    items = []
    for x in st.get("pushedSignals", []):
        items.append((x, "pushed"))
    for x in st.get("observedSignals", []):
        items.append((x, "observed"))
    items = [(x, g) for x, g in items if str(x.get("pushedAt", "")) >= cut_s]
    if not items:
        print("窗口内无信号")
        sys.exit(0)

    client = OkxClient()
    report = {"generated_at": datetime.datetime.now(
        datetime.timezone.utc).isoformat(), "rows": []}

    # 每个标的每个周期拉一次最新K线, 缓存复用
    cache = {}
    rows = []
    for x, grp in items:
        sym, iv = x["symbol"], x["interval"]
        pushed_ms = None
        try:
            pushed_ms = int(datetime.datetime.fromisoformat(
                x["pushedAt"]).timestamp() * 1000)
        except Exception:
            pass
        key = (sym, iv)
        if key not in cache:
            try:
                ks = client.get_klines(sym, iv, 300)
                cache[key] = [(k.openTime, k.open, k.high, k.low, k.close)
                              for k in ks]
            except Exception as e:
                cache[key] = []
                rows.append({"symbol": sym, "interval": iv,
                             "type": x["patternType"], "dir": x["direction"],
                             "group": grp, "error": str(e)[:120]})
                continue
        bars = [b for b in cache[key] if pushed_ms and b[0] >= pushed_ms]
        if len(bars) < 2:
            rows.append({"symbol": sym, "interval": iv,
                         "type": x["patternType"], "dir": x["direction"],
                         "group": grp, "note": "bars不足"})
            continue
        entry = bars[0][4]
        sign = 1 if x["direction"] == "LONG" else -1
        highs = [b[2] for b in bars[1:]]
        lows = [b[3] for b in bars[1:]]
        last = bars[-1][4]
        if sign == 1:
            fav = (max(highs) - entry) / entry
            adv = (min(lows) - entry) / entry
        else:
            fav = (entry - min(lows)) / entry
            adv = (entry - max(highs)) / entry
        rows.append({
            "symbol": sym, "interval": iv, "type": x["patternType"],
            "dir": x["direction"], "group": grp,
            # 2026-09-22: gate=死因(降级规则名/其他闸名), scanner 已写入 state
            "gate": x.get("gate", ""),
            "pushed_at": x["pushedAt"][:16], "entry": entry,
            "bars": len(bars) - 1,
            "ret_pct": round(100 * sign * (last - entry) / entry, 2),
            "max_fav_pct": round(100 * fav, 2),
            "max_adv_pct": round(100 * adv, 2),
        })

    report["rows"] = rows

    # ---- 组间对比 ----
    def agg(group, ivs=None):
        v = [r for r in rows if r.get("group") == group and "ret_pct" in r
             and (ivs is None or r["interval"] in ivs)]
        if not v:
            return None
        n = len(v)
        return {
            "n": n,
            "ret_avg": round(sum(r["ret_pct"] for r in v) / n, 2),
            "fav_avg": round(sum(r["max_fav_pct"] for r in v) / n, 2),
            "adv_avg": round(sum(r["max_adv_pct"] for r in v) / n, 2),
            "win": sum(1 for r in v if r["ret_pct"] > 0),
        }

    demoted = [r for r in rows if r.get("group") == "observed"
               and r.get("type") in DEMOTE_TYPES]
    print("=== 降级信号(头肩顶) 实盘走势验证 ===")
    for r in demoted:
        if "ret_pct" in r:
            print("  %-12s %s %-5s %s entry=%.5f 现在%+.2f%% 最有利%+.2f%% "
                  "最不利%+.2f%% (%d根)"
                  % (r["symbol"], r["interval"], r["dir"], r["pushed_at"],
                     r["entry"], r["ret_pct"], r["max_fav_pct"],
                     r["max_adv_pct"], r["bars"]))
        else:
            print("  %-12s %s %s" % (r["symbol"], r["interval"],
                                     r.get("note") or r.get("error")))

    print()
    print("=== 组间对比 (方向对齐收益) ===")
    for name, g in (("正式推送", "pushed"), ("降级头肩顶", "observed")):
        a = agg(g)
        if a:
            print("  %-8s %d条 平均收益%+.2f%% 胜%d/%d "
                  "平均最有利%+.2f%% 平均最不利%+.2f%%"
                  % (name, a["n"], a["ret_avg"], a["win"], a["n"],
                     a["fav_avg"], a["adv_avg"]))
    # 按周期细分(4h 降级样本最多)
    print("  --- 4h 细分 ---")
    for name, g in (("正式推送", "pushed"), ("降级头肩顶", "observed")):
        a = agg(g, {"4h"})
        if a:
            print("  %-8s %d条 平均收益%+.2f%% 胜%d/%d"
                  % (name, a["n"], a["ret_avg"], a["win"], a["n"]))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print()
    print("明细已写入", args.out)


if __name__ == "__main__":
    main()
