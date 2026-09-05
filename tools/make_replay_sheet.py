#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
由 history_replay 的 manifest.json 生成形态标注页 (回放专用)

与 make_annotation_sheet.py 的区别: 直接读 manifest(含形态类型/方向/置信度/跨度根数),
不依赖文件名解析形态名, 标注页信息更全, 且支持多周期混合。

用法:
  python tools/make_replay_sheet.py \
      --manifest output/history_replay/manifest.json \
      --out output/history_replay/annotate.html \
      --prefix history_replay/
"""
import os
import sys
import argparse
import json

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
DIRECTION_CN = {"LONG": "多", "SHORT": "空", "long": "多", "short": "空",
                "BULL": "多", "BEAR": "空"}

HTML_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>形态标注 · 历史回放</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px 20px 60px;
    font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    background: #FAFAF8; color: #2C2C2A; line-height: 1.6; }
  .wrap { max-width: 1180px; margin: 0 auto; }
  h1 { font-size: 19px; font-weight: 500; margin: 0 0 6px; }
  .sub { font-size: 13px; color: #5F5E5A; margin: 0 0 4px; }
  .sub b { color: #A32D2D; font-weight: 500; }
  .bar { display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
    margin: 16px 0 20px; padding: 12px 16px; background: #fff;
    border: 1px solid #E5E3DC; border-radius: 10px; position: sticky; top: 0; z-index: 10; }
  .prog { font-size: 13px; } .prog b { font-size: 15px; color: #185FA5; }
  .counts { font-size: 12px; color: #5F5E5A; margin-left: auto; }
  .counts span { margin-left: 12px; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 4px; }
  button { font-family: inherit; font-size: 13px; cursor: pointer; border-radius: 8px;
    border: 1px solid #D3D1C7; background: #fff; padding: 7px 14px; color: #444441; }
  button:hover { border-color: #888780; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; }
  .card { background: #fff; border: 1px solid #E5E3DC; border-radius: 12px; padding: 12px;
    transition: border-color .12s, background .12s; }
  .card.ok { border-color: #3B6D11; background: #FCFEF7; }
  .card.bad { border-color: #A32D2D; background: #FFFCFC; }
  .card.meh { border-color: #888780; background: #FAFAF8; }
  .meta { font-size: 12px; color: #5F5E5A; margin-bottom: 6px; display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
  .meta .sym { font-size: 14px; font-weight: 500; color: #2C2C2A; }
  .meta .tag { background: #F1EFE8; border-radius: 4px; padding: 1px 6px; }
  .meta .tag-push { background: #D6E4F0; color: #14436E; font-weight: 500; }
  .meta .tag-dir { background: #FBE9E9; color: #8A2B2B; font-weight: 500; }
  .meta .tag-dir-long { background: #E7F3E1; color: #2B5210; font-weight: 500; }
  .stat { font-size: 11px; color: #8A8780; margin-bottom: 8px; }
  .stat b { color: #444441; }
  .stat .good { color: #3B6D11; font-weight: 500; }
  .stat .bad { color: #A32D2D; font-weight: 500; }
  .stat .mid { color: #B8860B; font-weight: 500; }
  .shot { display: block; width: 100%; border-radius: 8px; border: 1px solid #E5E3DC; background: #fff; cursor: zoom-in; }
  .btns { display: flex; gap: 8px; margin-top: 10px; }
  .btns button { flex: 1; }
  .btns button.sel { font-weight: 500; }
  .b-ok.sel { background: #C0DD97; border-color: #3B6D11; color: #173404; }
  .b-bad.sel { background: #F7C1C1; border-color: #A32D2D; color: #501313; }
  .b-meh.sel { background: #D3D1C7; border-color: #5F5E5A; color: #2C2C2A; }
  .tip { font-size: 12px; color: #888780; margin: 20px 0 8px; }
  #msg { font-size: 13px; color: #3B6D11; margin-left: 8px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>形态标注 · 历史回放</h1>
  <p class="sub">__SUB_INTRO__</p>
  <p class="sub">__SUB_DETAIL__</p>

  <div class="bar">
    <span class="prog">已标注 <b id="done">0</b> / <span id="total">0</span></span>
    <button onclick="exportJSON()">导出结果 JSON</button>
    <button onclick="copyJSON()">复制结果到剪贴板</button>
    <button onclick="clearAll()">清空重来</button>
    <button onclick="showJSON()">在页面显示 JSON</button>
    <span class="counts">
      <span><i class="dot" style="background:#C0DD97"></i>像 <b id="n-ok">0</b></span>
      <span><i class="dot" style="background:#F7C1C1"></i>不像 <b id="n-bad">0</b></span>
      <span><i class="dot" style="background:#D3D1C7"></i>拿不准 <b id="n-meh">0</b></span>
    </span>
  </div>

  <div class="grid" id="grid"></div>

  <p class="tip">判据参考：两低点（或高点）是否真在一个水平？颈线是否真被突破？画出来的线和价格贴合是不是勉强凑的？<br>
  别管这笔最后赚亏 —— 这里只判断「图形本身像不像」。打「不像」的会用来算推送精确率。</p>
  <p class="tip">进度自动存在浏览器里，关掉页面不会丢。快捷键：选中卡片后按 1 / 2 / 3 快速标注。</p>
  <span id="msg"></span>
  <textarea id="dump" style="display:none;width:100%;height:260px;font-family:monospace;font-size:12px;margin-top:8px;padding:8px;border:1px solid #CCC;"></textarea>
</div>

<script>
const ITEMS = __ITEMS__;
const KEY = 'crypto830_replay_v1';
let ans = {};
try { ans = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { ans = {}; }

const grid = document.getElementById('grid');
document.getElementById('total').textContent = ITEMS.length;

ITEMS.forEach((it, i) => {
  const card = document.createElement('div');
  card.className = 'card';
  card.id = 'card-' + i;
  const dirCls = it.direction === '多' ? 'tag-dir-long' : 'tag-dir';
  card.innerHTML =
    '<div class="meta">' +
      '<span class="sym">' + it.symbol + '</span>' +
      '<span class="tag">' + it.interval + '</span>' +
      '<span class="tag tag-push">回放</span>' +
      '<span>' + it.patternCn + '</span>' +
      (it.direction ? '<span class="tag ' + dirCls + '">' + it.direction + '</span>' : '') +
    '</div>' +
    '<div class="stat">置信 <b>' + (it.confidence!=null?it.confidence.toFixed(2):'-') + '</b>' +
      ' · 跨度 <b>' + (it.spanBars||'-') + '</b>根 · 强度 <b>' + (it.strength!=null?it.strength.toFixed(2):'-') + '</b>' +
      (it.geometryScore!=null ? ' · <span class="' + (it.geometryCls||'') + '">几何 ' + it.geometryScore.toFixed(2) + '</span>' : '') + '</div>' +
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
    if (ans[k] === 'ok') ok++; else if (ans[k] === 'bad') bad++; else if (ans[k] === 'meh') meh++;
    ITEMS[k] && (function(i, v){
      const c = document.getElementById('card-' + i);
      if (c) { c.className = 'card ' + v;
        c.querySelectorAll('.btns button').forEach(b => b.classList.remove('sel'));
        const b = c.querySelector('.b-' + v); b && b.classList.add('sel'); }
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
    symbol: it.symbol, interval: it.interval, pattern_type: it.pattern,
    patternCn: it.patternCn, direction: it.direction,
    confidence: it.confidence, span_bars: it.spanBars, strength: it.strength, geometry_score: it.geometryScore, geometry_reason: it.geometryReason,
    chart: it.file, verdict: ans[i] || null
  }));
  return { version: 1, generated_at: new Date().toISOString(),
    judged: Object.keys(ans).length, total: ITEMS.length,
    summary: { ok: rows.filter(r=>r.verdict==='ok').length,
               bad: rows.filter(r=>r.verdict==='bad').length,
               meh: rows.filter(r=>r.verdict==='meh').length },
    rows: rows };
}
function flash(msg) { const el = document.getElementById('msg'); if (el) { el.textContent = msg; setTimeout(() => { el.textContent = ''; }, 2500); } }
function showJSON() {
  const ta = document.getElementById('dump');
  ta.value = JSON.stringify(buildResult(), null, 2);
  ta.style.display = 'block'; ta.focus(); ta.select();
  flash('已显示 JSON，请 Ctrl+A 全选后 Ctrl+C 复制，贴给老K');
}
function exportJSON() {
  try {
    const blob = new Blob([JSON.stringify(buildResult(), null, 2)], {type:'application/json'});
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
    a.download = 'replay_annotations.json'; a.click();
    flash('已尝试导出 replay_annotations.json（若下载被拦，请用「在页面显示 JSON」）');
  } catch (e) { showJSON(); }
}
function copyJSON() {
  try {
    const text = JSON.stringify(buildResult(), null, 2);
    navigator.clipboard.writeText(text).then(() => flash('已复制，可直接粘贴给我'),
      () => showJSON());
  } catch (e) { showJSON(); }
}
function clearAll() { if (!confirm('清空所有标注？')) return; ans = {};
  localStorage.removeItem(KEY);
  ITEMS.forEach((_, i) => { const c = document.getElementById('card-'+i);
    c.className = 'card'; c.querySelectorAll('.btns button').forEach(b=>b.classList.remove('sel')); });
  refresh(); }
document.addEventListener('keydown', e => {
  if (!['1','2','3'].includes(e.key)) return;
  const el = document.activeElement; if (el && el.tagName === 'INPUT') return;
  const v = {'1':'ok','2':'bad','3':'meh'}[e.key];
  const hovered = [...document.querySelectorAll('.card:hover')].pop();
  if (hovered) set(parseInt(hovered.id.replace('card-','')), v);
});
</script>
</body>
</html>
"""


# 由形态类型推断方向(用于目录扫描模式, manifest 缺失 direction 时)
DIR_BY_PATTERN = {
    "double_top": "空", "head_shoulders_top": "空", "descending_triangle": "空",
    "rising_wedge": "空", "flag": "双向", "symmetrical_triangle": "双向",
    "double_bottom": "多", "head_shoulders_bottom": "多", "ascending_triangle": "多",
    "falling_wedge": "多",
}


def _build_items_from_dir(d, interval, prefix):
    """扫描目录中的 png, 从文件名解析形态, 不依赖 manifest。"""
    import re, glob
    pat = re.compile(
        r"^(.+)4h_(\d{8})_(.+)_(\d{3})\.png$"
        if interval == "4h" else r"^(.+)1d_(\d{8})_(.+)_(\d{3})\.png$")
    items = []
    for fp in sorted(glob.glob(os.path.join(d, "*.png"))):
        name = os.path.basename(fp)
        m = pat.match(name)
        if not m:
            continue
        sym, _date, pen, _seq = m.group(1), m.group(2), m.group(3), m.group(4)
        pt = pen  # 形如 descending_triangle
        items.append({
            "file": prefix + "charts/" + name,
            "symbol": sym,
            "interval": interval,
            "pattern": pt,
            "patternCn": PATTERN_CN.get(pt, pt),
            "direction": DIR_BY_PATTERN.get(pt, ""),
            "confidence": None,
            "spanBars": None,
            "strength": None,
            "kind": "回放",
            "isRandom": False,
        })
    return items


def main():
    ap = argparse.ArgumentParser(description="由回放 manifest 生成标注页")
    ap.add_argument("--manifest", help="history_replay 输出的 manifest.json")
    ap.add_argument("--from-dir", help="直接扫描目录中的 png(不读 manifest, 用于回放中途预览)")
    ap.add_argument("--out", required=True, help="输出 HTML")
    ap.add_argument("--interval", default="4h", help="--from-dir 模式下的周期(4h/1d)")
    ap.add_argument("--prefix", default="", help="HTML 中图片相对路径前缀(默认空, 图片与HTML同目录)")
    args = ap.parse_args()

    if args.from_dir:
        items = _build_items_from_dir(args.from_dir, args.interval, args.prefix)
        if not items:
            print(f"目录无回放图: {args.from_dir}")
            return 1
        print(f"从目录扫描到 {len(items)} 张回放图")
    else:
        if not args.manifest:
            print("必须提供 --manifest 或 --from-dir")
            return 1
        with open(args.manifest, encoding="utf-8") as f:
            m = json.load(f)
        rows = m.get("rows", [])
        if not rows:
            print(f"manifest 无 rows: {args.manifest}")
            return 1
        items = []
        for r in rows:
            pt = r.get("pattern_type", "")
            direction = DIRECTION_CN.get(str(r.get("direction", "")), r.get("direction", ""))
            items.append({
                "file": args.prefix + r.get("image", ""),
                "symbol": r.get("symbol", ""),
                "interval": r.get("interval", ""),
                "pattern": pt,
                "patternCn": PATTERN_CN.get(pt, pt),
                "direction": direction,
                'confidence': r.get('confidence'),
                'spanBars': r.get('span_bars'),
                'strength': r.get('strength'),
                'geometryScore': r.get('geometry_score'),
                'geometryReason': r.get('geometry_reason'),
                "kind": "回放",
                "isRandom": False,
            })

    sub_intro = (f"本批 <b>{len(items)} 张</b>图来自历史回放：把过去几个月的 K 线按月截断，"
                 f"只喂「当时」之前的数据跑检测器，模拟系统当时会推什么形态。")
    sub_detail = ("这是离线校准用的负/正样本。打「像」= 这图形态标准（系统推得对）；"
                  "打「不像」= 形态不标准（系统的误报，用来算精确率）；「拿不准」= 跳过。")

    # 默认按几何质量分升序：画得歪(低分)的排前面，优先标注误报
    for idx, it in enumerate(items):
        g = it.get('geometryScore')
        it['geometryCls'] = ('good' if g is not None and g >= 0.7
                             else 'bad' if g is not None and g < 0.6
                             else 'mid') if g is not None else ''
    items.sort(key=lambda x: (x.get('geometryScore') is None, x.get('geometryScore') or 0))

    html = HTML_TMPL
    html = html.replace("__SUB_INTRO__", sub_intro)
    html = html.replace("__SUB_DETAIL__", sub_detail)
    html = html.replace("__ITEMS__", json.dumps(items, ensure_ascii=False))
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"回放标注页已生成: {args.out}")
    print(f"  图片数: {len(items)}")
    from collections import Counter
    for k, v in Counter(i["patternCn"] for i in items).most_common():
        print(f"    {k:<14} {v}")
    print("\n直接用浏览器打开即可标注。导出 JSON 后贴给我，我据此校准容差。")


if __name__ == "__main__":
    sys.exit(main() or 0)
