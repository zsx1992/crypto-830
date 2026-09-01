#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成形态标注页（负样本采集工具）

用途：
  把系统推送过的信号图做成一张可点的网页，人工判定"这个形态你认不认可"。
  打「不像」的就是负样本——用来算精确率（precision），
  而现有 97 条金标准只能算召回率（recall），算不出精确率。

用法:
  python tools/make_annotation_sheet.py                      # 扫 output/charts
  python tools/make_annotation_sheet.py --dir output/charts --out output/annotate.html

输出:
  一个自包含的 HTML（图片用相对路径，数据内联，file:// 直接打开即可用）。
  标注结果存 localStorage，可导出 JSON。
"""

import os
import re
import glob
import argparse

# 形态类型 -> 中文名
PATTERN_CN = {
    "double_top": "双顶 / M顶",
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

# 文件名格式: SYMBOL_INTERVAL_PATTERN.png
# pat 允许数字后缀（随机样本图命名 {SYM}_4h_random_01.png 用）
FNAME_RE = re.compile(r"^(?P<sym>[A-Z0-9]+)_(?P<tf>[0-9a-z]+)_(?P<pat>[a-z0-9_]+)\.png$")


def parse_items(chart_dir: str, img_prefix: str):
    items = []
    for path in sorted(glob.glob(os.path.join(chart_dir, "*.png"))):
        name = os.path.basename(path)
        m = FNAME_RE.match(name)
        if not m:
            continue
        sym, tf, pat = m.group("sym"), m.group("tf"), m.group("pat")
        is_random = pat.startswith("random")
        items.append({
            "file": img_prefix + name,
            "symbol": sym,
            "interval": tf,
            "pattern": pat,
            "patternCn": "随机窗口" if is_random else PATTERN_CN.get(pat, pat),
            "kind": "随机" if is_random else "推送",
            "isRandom": is_random,
        })
    return items


HTML_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>形态标注 · 负样本采集</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px 20px 60px;
    font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    background: #FAFAF8; color: #2C2C2A; line-height: 1.6;
  }
  .wrap { max-width: 1180px; margin: 0 auto; }
  h1 { font-size: 19px; font-weight: 500; margin: 0 0 6px; }
  .sub { font-size: 13px; color: #5F5E5A; margin: 0 0 4px; }
  .sub b { color: #A32D2D; font-weight: 500; }
  .bar {
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
    margin: 16px 0 20px; padding: 12px 16px;
    background: #fff; border: 1px solid #E5E3DC; border-radius: 10px;
    position: sticky; top: 0; z-index: 10;
  }
  .prog { font-size: 13px; }
  .prog b { font-size: 15px; color: #185FA5; }
  .counts { font-size: 12px; color: #5F5E5A; margin-left: auto; }
  .counts span { margin-left: 12px; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 4px; }
  button {
    font-family: inherit; font-size: 13px; cursor: pointer;
    border-radius: 8px; border: 1px solid #D3D1C7; background: #fff;
    padding: 7px 14px; color: #444441;
  }
  button:hover { border-color: #888780; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; }
  .card {
    background: #fff; border: 1px solid #E5E3DC; border-radius: 12px;
    padding: 12px; transition: border-color .12s, background .12s;
  }
  .card.ok    { border-color: #3B6D11; background: #FCFEF7; }
  .card.bad   { border-color: #A32D2D; background: #FFFCFC; }
  .card.meh   { border-color: #888780; background: #FAFAF8; }
  .meta { font-size: 12px; color: #5F5E5A; margin-bottom: 8px; display: flex; gap: 8px; align-items: baseline; }
  .meta .sym { font-size: 14px; font-weight: 500; color: #2C2C2A; }
  .meta .tag { background: #F1EFE8; border-radius: 4px; padding: 1px 6px; }
  .meta .tag-push { background: #DDEDCB; color: #2B5210; font-weight: 500; }
  .meta .tag-rnd  { background: #E7E6E1; color: #5F5E5A; font-weight: 500; }
  .shot { display: block; width: 100%; border-radius: 8px; border: 1px solid #E5E3DC; background: #fff; cursor: zoom-in; }
  .btns { display: flex; gap: 8px; margin-top: 10px; }
  .btns button { flex: 1; }
  .btns button.sel { font-weight: 500; }
  .b-ok.sel  { background: #C0DD97; border-color: #3B6D11; color: #173404; }
  .b-bad.sel { background: #F7C1C1; border-color: #A32D2D; color: #501313; }
  .b-meh.sel { background: #D3D1C7; border-color: #5F5E5A; color: #2C2C2A; }
  .tip { font-size: 12px; color: #888780; margin: 20px 0 8px; }
  #msg { font-size: 13px; color: #3B6D11; margin-left: 8px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>形态标注 · 负样本采集</h1>
  <p class="sub">__SUB_INTRO__</p>
  <p class="sub">__SUB_DETAIL__</p>

  <div class="bar">
    <span class="prog">已标注 <b id="done">0</b> / <span id="total">0</span></span>
    <button onclick="exportJSON()">导出结果 JSON</button>
    <button onclick="copyJSON()">复制结果到剪贴板</button>
    <button onclick="clearAll()">清空重来</button>
    <span class="counts">
      <span><i class="dot" style="background:#C0DD97"></i>像 <b id="n-ok">0</b></span>
      <span><i class="dot" style="background:#F7C1C1"></i>不像 <b id="n-bad">0</b></span>
      <span><i class="dot" style="background:#D3D1C7"></i>拿不准 <b id="n-meh">0</b></span>
    </span>
  </div>

  <div class="grid" id="grid"></div>

  <p class="tip">判据参考：形态的两个低点（或高点）是否真的在一个水平上？颈线是否真的被突破？画出来的线和价格的贴合是不是勉强凑的？<br>
  别管这笔最后是赚是亏 —— 那是盈利回测的事，这里只判断「图形本身像不像」。</p>
  <p class="tip">进度自动存在浏览器里，关掉页面不会丢。快捷键：选中卡片后按 1 / 2 / 3 快速标注。</p>
  <span id="msg"></span>
</div>

<script>
const ITEMS = __ITEMS__;
const KEY = 'crypto830_annotations_v1';
let ans = {};
try { ans = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { ans = {}; }

const grid = document.getElementById('grid');
document.getElementById('total').textContent = ITEMS.length;

ITEMS.forEach((it, i) => {
  const card = document.createElement('div');
  card.className = 'card';
  card.id = 'card-' + i;
  card.innerHTML =
    '<div class="meta">' +
      '<span class="sym">' + it.symbol + '</span>' +
      '<span class="tag">' + it.interval + '</span>' +
      '<span class="tag ' + (it.isRandom ? 'tag-rnd' : 'tag-push') + '">' +
        (it.isRandom ? '随机' : '推送') + '</span>' +
      '<span>' + it.patternCn + '</span>' +
    '</div>' +
    '<img class="shot" src="' + it.file + '" loading="lazy" onclick="window.open(this.src)">' +
    '<div class="btns">' +
      '<button class="b-ok"  onclick="set(' + i + ',\\'ok\\')">像</button>' +
      '<button class="b-bad" onclick="set(' + i + ',\\'bad\\')">不像</button>' +
      '<button class="b-meh" onclick="set(' + i + ',\\'meh\\')">拿不准</button>' +
    '</div>';
  grid.appendChild(card);
});

function set(i, v) {
  ans[i] = v;
  localStorage.setItem(KEY, JSON.stringify(ans));
  const card = document.getElementById('card-' + i);
  card.className = 'card ' + v;
  card.querySelectorAll('.btns button').forEach(b => b.classList.remove('sel'));
  card.querySelector('.b-' + v).classList.add('sel');
  refresh();
}

function refresh() {
  let ok = 0, bad = 0, meh = 0;
  for (const k in ans) {
    if (ans[k] === 'ok') ok++;
    else if (ans[k] === 'bad') bad++;
    else if (ans[k] === 'meh') meh++;
    ITEMS[k] && (function(i, v){
      const c = document.getElementById('card-' + i);
      if (c) {
        c.className = 'card ' + v;
        c.querySelectorAll('.btns button').forEach(b => b.classList.remove('sel'));
        const b = c.querySelector('.b-' + v);
        b && b.classList.add('sel');
      }
    })(k, ans[k]);
  }
  document.getElementById('done').textContent = Object.keys(ans).length;
  document.getElementById('n-ok').textContent = ok;
  document.getElementById('n-bad').textContent = bad;
  document.getElementById('n-meh').textContent = meh;
}
refresh();

function buildResult() {
  const rows = ITEMS.map((it, i) => ({
    symbol: it.symbol, interval: it.interval, pattern: it.pattern,
    patternCn: it.patternCn, chart: it.file, verdict: ans[i] || null
  }));
  return {
    version: 1,
    generated_at: new Date().toISOString(),
    judged: Object.keys(ans).length,
    total: ITEMS.length,
    summary: {
      ok: rows.filter(r => r.verdict === 'ok').length,
      bad: rows.filter(r => r.verdict === 'bad').length,
      meh: rows.filter(r => r.verdict === 'meh').length
    },
    rows: rows
  };
}

function flash(msg) {
  const el = document.getElementById('msg');
  el.textContent = msg;
  setTimeout(() => { el.textContent = ''; }, 2500);
}

function exportJSON() {
  const blob = new Blob([JSON.stringify(buildResult(), null, 2)], {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'annotations.json';
  a.click();
  flash('已导出 annotations.json');
}

function copyJSON() {
  const text = JSON.stringify(buildResult(), null, 2);
  navigator.clipboard.writeText(text).then(
    () => flash('已复制到剪贴板，可直接粘贴给我'),
    () => flash('复制失败，请用「导出结果 JSON」')
  );
}

function clearAll() {
  if (!confirm('清空所有标注？')) return;
  ans = {};
  localStorage.removeItem(KEY);
  ITEMS.forEach((_, i) => {
    const c = document.getElementById('card-' + i);
    c.className = 'card';
    c.querySelectorAll('.btns button').forEach(b => b.classList.remove('sel'));
  });
  refresh();
}

document.addEventListener('keydown', e => {
  if (!['1','2','3'].includes(e.key)) return;
  const el = document.activeElement;
  if (el && el.tagName === 'INPUT') return;
  const v = {'1':'ok','2':'bad','3':'meh'}[e.key];
  const hovered = [...document.querySelectorAll('.card:hover')].pop();
  if (hovered) set(parseInt(hovered.id.replace('card-','')), v);
});
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="output/charts", help="图表目录")
    ap.add_argument("--out", default="output/annotate.html", help="输出 HTML")
    ap.add_argument("--prefix", default="charts/", help="HTML 中图片的相对路径前缀")
    args = ap.parse_args()

    items = parse_items(args.dir, args.prefix)
    if not items:
        print(f"未在 {args.dir} 找到符合 '标的_周期_形态.png' 命名的图片")
        return

    # 副标按混合情况自适应：全推送 / 全随机 / 混合
    n_random = sum(1 for i in items if i["pattern"].startswith("random"))
    n_pushed = len(items) - n_random
    if n_random and n_pushed:
        sub_intro = (
            f"本批共 {len(items)} 张图：<b>{n_pushed} 张</b>是系统真实推送过的形态信号，"
            f"<b>{n_random} 张</b>是随机时刻的 K 线窗口（模型没标）。请你当裁判统一标准："
            f"<b>光看这张图，里头有没有像样的形态结构？</b>"
        )
        sub_detail = (
            "打「像」= 这图里看得出某种形态（推送图的命中确认 OR 随机图的模型漏报，可补金标准）。"
            "打「不像」= 这图里没形态（推送图的误报 OR 随机图的正常基线，是负样本来源）。"
            "「拿不准」= 跳过。"
        )
    elif n_random:
        sub_intro = (
            f"本批 {n_random} 张图全部是<b>随机时刻的 K 线窗口</b>（模型没标）。"
            "请你当裁判：<b>光看这张图，里头有没有像样的形态结构？</b>"
        )
        sub_detail = (
            "打「像」= 看得出形态（可作为模型漏报样本补充金标准）；"
            "打「不像」= 没有形态（负样本基线）；「拿不准」= 跳过。"
        )
    else:
        sub_intro = (
            "下面每张图都是系统真实推送过的信号。请你当裁判："
            "<b>光看这张图，你认可系统画的这个形态吗？</b>"
        )
        sub_detail = (
            "关键是打「不像」的那批 —— 它们就是负样本，用来算「我推的信号准不准」。"
            "这个数现在一个都没有。"
        )

    html = HTML_TMPL
    html = html.replace("__SUB_INTRO__", sub_intro)
    html = html.replace("__SUB_DETAIL__", sub_detail)
    html = html.replace("__ITEMS__", __import__("json").dumps(items, ensure_ascii=False))
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"标注页已生成: {args.out}")
    print(f"  图片数: {len(items)}")
    from collections import Counter
    for k, v in Counter(i["patternCn"] for i in items).most_common():
        print(f"    {k:<14} {v}")
    print("\n直接用浏览器打开即可标注。")


if __name__ == "__main__":
    main()
