# -*- coding: utf-8 -*-
"""
闸门效果对比页（原始 → 收敛闸 → 趋势闸，三轮）

身份匹配用「稳定 key」= (symbol, interval, pattern_type, end_ms)。
不能用文件名！文件名里的序号 _000/_001 会随形态集合变化重排，
前后两次回放的同名文件几乎无交集（实测交集仅 1/432 = 0.2%）。

用法:
  python tools/make_effect_page.py \
      --before output/history_replay/manifest_before_p1wedge.json \
      --mid    output/history_replay/manifest_after_p1wedge.json \
      --after  output/history_replay/manifest.json \
      --annotations output/history_replay/user_annotations.json \
      --out output/history_replay/effect.html

分三组展示:
  A 止损成功：你判「不像」+ 新闸门已拦掉   → 这些垃圾以后不再推
  B 仍在推送：新回放幸存（含漏网误报）      → 这是现在的真实推送质量
  C 误伤：    你判「像」却被新闸门砍掉      → 代价，需回调门槛
"""

import argparse
import collections
import html
import json
import os

CN = {
    "rising_wedge": "上升楔形",
    "falling_wedge": "下降楔形",
    "ascending_triangle": "上升三角形",
    "descending_triangle": "下降三角形",
    "symmetrical_triangle": "对称三角形",
    "double_top": "双顶/M顶",
    "double_bottom": "双底/W底",
    "head_shoulders_top": "头肩顶",
    "head_shoulders_bottom": "头肩底",
}


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def rows_of(path):
    d = load_json(path, [])
    return d.get("rows", []) if isinstance(d, dict) else d


def esc(s):
    return html.escape(str(s if s is not None else ""))


def key(r):
    """形态的稳定身份：与渲染序号无关"""
    return (r.get("symbol"), r.get("interval"),
            r.get("pattern_type"), r.get("end_ms"))


def card(title, sub, img, badge_cls="", note=""):
    badge = f'<span class="badge {badge_cls}">{esc(title)}</span>' if title else ""
    return f"""
    <div class="card">
      <div class="thumb"><img loading="lazy" src="{esc(img)}" alt=""></div>
      <div class="meta">{badge}
        <div class="sub">{esc(sub)}</div>
        {f'<div class="note">{esc(note)}</div>' if note else ''}
      </div>
    </div>"""


def main():
    ap = argparse.ArgumentParser(description="闸门效果对比页（三轮）")
    ap.add_argument("--before", default="output/history_replay/manifest_before_p1wedge.json",
                    help="基线 manifest（人工标注所依据的那轮）")
    ap.add_argument("--mid", default="output/history_replay/manifest_after_p1wedge.json",
                    help="中间态 manifest（第一轮闸门后）")
    ap.add_argument("--after", default="output/history_replay/manifest.json")
    ap.add_argument("--annotations", default="output/history_replay/user_annotations.json")
    ap.add_argument("--out", default="output/history_replay/effect.html")
    ap.add_argument("--per-group", type=int, default=8)
    args = ap.parse_args()

    before = rows_of(args.before)
    mid = rows_of(args.mid)
    after = rows_of(args.after)
    ann = load_json(args.annotations, {})
    judged = [r for r in ann.get("rows", [])
              if r.get("verdict") in ("ok", "bad", "meh")]

    bmap = {os.path.basename(r["image"]): r for r in before}
    mkeys = set(key(r) for r in mid)
    akeys = set(key(r) for r in after)

    def survive_stats(keyset):
        """返回 (ok保留, ok总, bad存活, bad总)"""
        st = collections.defaultdict(lambda: {"t": 0, "alive": 0})
        for j in judged:
            src = bmap.get(os.path.basename(j.get("chart", "")))
            if not src:
                continue
            v = j.get("verdict")
            st[v]["t"] += 1
            if key(src) in keyset:
                st[v]["alive"] += 1
        return st["ok"]["alive"], st["ok"]["t"], st["bad"]["alive"], st["bad"]["t"]

    ok_m, ok_t, bad_m, bad_t = survive_stats(mkeys)
    ok_a, _, bad_a, _ = survive_stats(akeys)

    prec_before = ok_t / max(ok_t + bad_t, 1)
    prec_mid = ok_m / max(ok_m + bad_m, 1)
    prec_after = ok_a / max(ok_a + bad_a, 1)

    # 分组取样（以最终 after 为准）
    grp_a, grp_c = [], []
    for j in judged:
        src = bmap.get(os.path.basename(j.get("chart", "")))
        if not src:
            continue
        v = j.get("verdict")
        alive = key(src) in akeys
        img = src.get("image", "")
        sub = (f"{src.get('symbol')} {src.get('interval')} · "
               f"{CN.get(src.get('pattern_type'), src.get('pattern_type'))} · "
               f"{src.get('end_date')} · 跨度{src.get('span_bars')}根")
        note = f"几何分 {src.get('geometry_score')}｜{src.get('geometry_reason', '')}"
        if v == "bad" and not alive:
            grp_a.append(card("已拦下", sub, img, "b-good", note))
        elif v == "ok" and not alive:
            grp_c.append(card("被误伤", sub, img, "b-bad", note))

    # B 组：新回放幸存。优先放"你判不像却仍存活"的漏网，再补未标注的新图
    judged_bad_keys = set()
    for j in judged:
        src = bmap.get(os.path.basename(j.get("chart", "")))
        if src and j.get("verdict") == "bad":
            judged_bad_keys.add(key(src))
    leak = [r for r in after if key(r) in judged_bad_keys]
    fresh = [r for r in after if key(r) not in judged_bad_keys]
    grp_b = []
    for r in leak[:args.per_group]:
        grp_b.append(card("漏网误报", f"{r.get('symbol')} · "
                          f"{CN.get(r.get('pattern_type'), r.get('pattern_type'))} · "
                          f"{r.get('end_date')}", r.get("image", ""), "b-bad",
                          f"你判不像但仍推送｜几何分 {r.get('geometry_score')}"))
    for r in fresh[:max(0, args.per_group - len(leak))]:
        grp_b.append(card("新推送", f"{r.get('symbol')} · "
                          f"{CN.get(r.get('pattern_type'), r.get('pattern_type'))} · "
                          f"{r.get('end_date')}", r.get("image", ""), "b-new",
                          f"几何分 {r.get('geometry_score')}｜{r.get('geometry_reason','')}"))

    grp_a = grp_a[:args.per_group]
    grp_c = grp_c[:args.per_group]

    # 分形态 原始 → 收敛闸 → 趋势闸
    tb = collections.Counter(r.get("pattern_type") for r in before)
    tm = collections.Counter(r.get("pattern_type") for r in mid)
    ta = collections.Counter(r.get("pattern_type") for r in after)
    type_rows = ""
    for t in sorted(set(tb) | set(tm) | set(ta), key=lambda x: -tb.get(x, 0)):
        bn, mn, an = tb.get(t, 0), tm.get(t, 0), ta.get(t, 0)
        delta = an - bn
        cls = "down" if delta < 0 else ("up" if delta > 0 else "")
        type_rows += (f"<tr><td>{esc(CN.get(t, t))}</td><td>{bn}</td>"
                      f"<td>{mn}</td><td>{an}</td>"
                      f"<td class='{cls}'>{delta:+d}</td></tr>")

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>收敛闸门效果对比</title>
<style>
 body{{font-family:-apple-system,"Microsoft YaHei",sans-serif;background:#f6f7f9;
   color:#1c1e21;margin:0;padding:24px;}}
 .wrap{{max-width:1180px;margin:0 auto;}}
 h1{{font-size:22px;margin:0 0 4px;}}
 .tip{{color:#65676b;font-size:13px;margin-bottom:18px;}}
 .kpis{{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:22px;}}
 .kpi{{background:#fff;border:1px solid #e4e6eb;border-radius:10px;padding:14px 18px;min-width:150px;}}
 .kpi .v{{font-size:26px;font-weight:700;line-height:1.15;}}
 .kpi .l{{font-size:12px;color:#65676b;margin-top:3px;}}
 .kpi.good .v{{color:#d93025;}}
 .kpi.warn .v{{color:#f5a623;}}
 h2{{font-size:17px;margin:26px 0 4px;}}
 .desc{{font-size:13px;color:#65676b;margin-bottom:12px;}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px;}}
 .card{{background:#fff;border:1px solid #e4e6eb;border-radius:10px;overflow:hidden;}}
 .thumb{{background:#fff;border-bottom:1px solid #eef0f2;}}
 .thumb img{{width:100%;display:block;}}
 .meta{{padding:9px 11px;font-size:12px;}}
 .badge{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;
   font-weight:600;color:#fff;margin-bottom:5px;}}
 .b-good{{background:#0f9d58;}} .b-bad{{background:#d93025;}}
 .b-new{{background:#1a73e8;}}
 .sub{{color:#1c1e21;font-weight:500;}}
 .note{{color:#8a8d91;margin-top:3px;font-size:11px;}}
 table{{border-collapse:collapse;background:#fff;font-size:13px;margin-top:8px;}}
 th,td{{border:1px solid #e4e6eb;padding:6px 14px;text-align:right;}}
 th:first-child,td:first-child{{text-align:left;}}
 th{{background:#f0f2f5;}}
 td.down{{color:#d93025;font-weight:600;}} td.up{{color:#0f9d58;}}
 .box{{background:#fff;border:1px solid #e4e6eb;border-radius:10px;padding:14px 18px;
   font-size:13px;line-height:1.75;}}
</style></head><body><div class="wrap">
<h1>闸门效果对比：原始 → 收敛闸 → 趋势背景闸</h1>
<div class="tip">对照你的 {len(judged)} 张人工标注 · 身份匹配用 symbol+interval+形态+结束时间（不用文件名，文件名序号会因形态集合变化而重排）</div>

<div class="kpis">
  <div class="kpi"><div class="v">{len(before)} → {len(mid)} → {len(after)}</div><div class="l">推送总量（累计 {len(after)-len(before):+d}）</div></div>
  <div class="kpi good"><div class="v">{prec_before:.1%} → {prec_after:.1%}</div><div class="l">精确率（像÷(像+不像)）</div></div>
  <div class="kpi"><div class="v">{bad_t - bad_a}/{bad_t}</div><div class="l">你判「不像」被拦掉（{(bad_t-bad_a)/max(bad_t,1):.0%}）</div></div>
  <div class="kpi warn"><div class="v">{ok_a}/{ok_t}</div><div class="l">你判「像」保留（{ok_a/max(ok_t,1):.0%}）</div></div>
</div>

<div class="box">
  <b>三轮演进</b><br>
  ① 原始：{len(before)} 张，精确率 <b>{prec_before:.1%}</b>（{ok_t}像 / {bad_t}不像）<br>
  ② +楔形三角收敛闸门：{len(mid)} 张，精确率 <b>{prec_mid:.1%}</b>（拦掉 {bad_t-bad_m} 张误报，误伤 {ok_t-ok_m} 张）<br>
  ③ +反转形态趋势背景闸门：{len(after)} 张，精确率 <b>{prec_after:.1%}</b>（再拦掉 {bad_m-bad_a} 张误报，误伤 {ok_m-ok_a} 张）<br>
  <span style="color:#65676b">③ 的关键：反转形态（双顶/双底/头肩）必须先有一段显著前置趋势，横盘或中继里的局部 H-L-H 不算。</span>
</div>

<h2>A 组 · 止损成功：你判「不像」、新闸门已拦下</h2>
<div class="desc">这些以后不会再推。共 {bad_t - bad_a} 张，展示前 {len(grp_a)} 张（多为横盘/下跌中继里的假反转）。</div>
<div class="grid">{''.join(grp_a) or '<div class="card"><div class="meta">无</div></div>'}</div>

<h2>B 组 · 现在仍在推送的（漏网误报 + 新样本）</h2>
<div class="desc">红色=你判过「不像」却仍推出来的漏网共 {len(leak)} 张；蓝色=本次新检出。幸存总数 {len(after)} 张。</div>
<div class="grid">{''.join(grp_b) or '<div class="card"><div class="meta">无</div></div>'}</div>

<h2>C 组 · 代价：你判「像」却被砍掉（{ok_t-ok_a} 张）</h2>
<div class="desc">共误伤 {ok_t-ok_a} 张（其中收敛闸造成 {ok_t-ok_m} 张、趋势闸造成 {ok_m-ok_a} 张）。收敛闸误伤的这些，旧几何分里收敛度也是 0.00 —— 说明你认可它们并非因为「两条线收窄」。</div>
<div class="grid">{''.join(grp_c) or '<div class="card"><div class="meta">无</div></div>'}</div>

<h2>分形态推送量变化</h2>
<table><tr><th>形态</th><th>原始</th><th>收敛闸</th><th>趋势闸</th><th>累计变化</th></tr>
{type_rows}</table>
</div></body></html>"""

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html_doc)

    print(f"[OK] {args.out}")
    print(f"     A 止损 {len(grp_a)} 张 / B 幸存 {len(grp_b)} 张 / C 误伤 {len(grp_c)} 张")
    print(f"     精确率 {prec_before:.1%} → {prec_after:.1%}"
          f"   推送量 {len(before)} → {len(after)}")


if __name__ == "__main__":
    main()
