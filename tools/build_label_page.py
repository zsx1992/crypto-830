# -*- coding: utf-8 -*-
"""
生成金标准标注页（自包含 HTML）。

读 charts_golden/manifest.json，把元信息内联进 HTML，图片用相对路径引用。
必须内联：用 file:// 打开时浏览器禁止 fetch 本地 JSON，但 <img> 相对路径可以。

用法: python tools/build_label_page.py [--manifest charts_golden/manifest.json]
输出: golden_label.html（双击即可用）
"""
import os
import sys
import json
import argparse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>形态标注 · 金标准</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#F7F6F3;color:#2C2C2A;
 font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;font-size:14px}
header{position:sticky;top:0;z-index:10;background:#fff;
 border-bottom:1px solid rgba(0,0,0,.1);padding:10px 18px;
 display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.bar{height:6px;background:#E8E6E0;border-radius:3px;overflow:hidden;flex:1;min-width:160px}
.bar>i{display:block;height:100%;width:0;background:#378ADD;transition:width .2s}
.st{min-width:96px;text-align:right;color:#5F5E5A;font-size:13px}
main{max-width:1080px;margin:0 auto;padding:18px}
.card{background:#fff;border:1px solid rgba(0,0,0,.1);border-radius:12px;
 padding:14px;margin-bottom:14px}
.meta{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
.tag{background:#F1EFE8;border-radius:6px;padding:3px 9px;font-size:12px;color:#444441}
.tag.b{background:#E6F1FB;color:#185FA5}
.tag.r{background:#FCEBEB;color:#A32D2D}
.tag.g{background:#EAF3DE;color:#3B6D11}
img{width:100%;display:block;border-radius:8px;background:#FAFAF8}
.btns{display:flex;gap:10px;margin-top:12px}
button{flex:1;padding:11px 0;font-size:15px;border-radius:8px;cursor:pointer;
 border:1px solid rgba(0,0,0,.18);background:#fff;color:#2C2C2A}
button:hover{background:#F1EFE8}
button.on{color:#fff;border-color:transparent}
button.g.on{background:#639922}button.a.on{background:#BA7517}button.r.on{background:#E24B4A}
.nav{display:flex;gap:8px;margin-top:10px}
.nav button{flex:0 0 auto;padding:7px 16px;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:8px;margin-top:12px}
.grid div{border:2px solid transparent;border-radius:6px;overflow:hidden;cursor:pointer;
 background:#F1EFE8;font-size:11px;text-align:center;padding:4px}
.grid div.done{border-color:#378ADD}
.grid div.cur{border-color:#185FA5;background:#E6F1FB}
.hint{color:#888780;font-size:12px;margin-top:8px}
#done{display:none;padding:18px}
textarea{width:100%;height:150px;font-family:ui-monospace,Consolas,monospace;font-size:12px}
</style>
</head>
<body>
<header>
<span style="font-weight:500">形态标注</span>
<div class="bar"><i id="bar"></i></div>
<span class="st" id="st"></span>
</header>
<main>
<div class="card" id="card">
  <div class="meta" id="meta"></div>
  <img id="img" alt="形态图">
  <div class="btns">
    <button class="g" data-v="good">像 <span style="color:#888780">1</span></button>
    <button class="a" data-v="ok">勉强 <span style="color:#888780">2</span></button>
    <button class="r" data-v="bad">瞎画 <span style="color:#888780">3</span></button>
  </div>
  <div class="nav">
    <button id="prev">← 上一张</button>
    <button id="next">下一张 →</button>
    <button id="exp">导出结果</button>
    <button id="clr">清空</button>
  </div>
  <div class="hint">键盘：1 / 2 / 3 打分，← → 切换。结果自动存在浏览器本地。</div>
</div>
<div class="card" id="done">
  <h2 style="margin:0 0 10px;font-size:16px">标注完成 ✓</h2>
  <div id="sum" style="margin-bottom:12px"></div>
  <div class="nav" style="margin:0 0 10px"><button id="exp2">⬇ 下载 golden_labels.json</button></div>
  <textarea id="json" readonly></textarea>
  <div class="hint">如果下载没反应：全选上方文本框内容复制发给老K即可。</div>
</div>
<div class="card"><div class="grid" id="grid"></div></div>
</main>
<script>
const ITEMS = __ITEMS__;
const KEY = 'golden_label_v1';
let ans = JSON.parse(localStorage.getItem(KEY) || '{}');
let cur = 0;
const IMGDIR = '__IMGDIR__';

function save(){ localStorage.setItem(KEY, JSON.stringify(ans)); }

function render(){
  const n = ITEMS.length;
  const doneN = Object.keys(ans).length;
  document.getElementById('bar').style.width = (100*doneN/n)+'%';
  document.getElementById('st').textContent = doneN+' / '+n;
  if(doneN >= n){ showDone(); return; }
  document.getElementById('done').style.display='none';
  document.getElementById('card').style.display='';
  while(cur < n && ans[ITEMS[cur].id]) cur++;
  if(cur >= n){ cur = 0; while(cur < n && ans[ITEMS[cur].id]) cur++; }
  const it = ITEMS[cur];
  if(!it){ showDone(); return; }
  document.getElementById('img').src = IMGDIR + it.image.split('/').pop();
  document.getElementById('meta').innerHTML =
    '<span class="tag b">'+it.symbol+'</span>'+
    '<span class="tag">'+it.interval+'</span>'+
    '<span class="tag">'+it.patternType+'</span>'+
    '<span class="tag '+(it.direction==='LONG'?'g':'r')+'">'+it.direction+'</span>'+
    '<span class="tag">强度 '+it.strength+'</span>'+
    '<span class="tag">'+(it.pushedAt||'').slice(0,16).replace('T',' ')+'</span>'+
    '<span class="tag">#'+it.id+'</span>';
  document.querySelectorAll('.btns button').forEach(b=>b.classList.remove('on'));
  if(ans[it.id]){
    document.querySelector('.btns button[data-v="'+ans[it.id]+'"]').classList.add('on');
  }
  paintGrid();
}

function paintGrid(){
  const g = document.getElementById('grid');
  g.innerHTML = ITEMS.map((it,i)=>{
    const v = ans[it.id];
    const cls = (v?'done':'') + (i===cur?' cur':'');
    const mk = v==='good'?'像':v==='ok'?'勉强':v==='bad'?'瞎':'';
    return '<div class="'+cls+'" data-i="'+i+'">'+it.symbol+'<br>'+it.interval+'<br><b>'+mk+'</b></div>';
  }).join('');
  g.querySelectorAll('div').forEach(d=>{
    d.onclick = ()=>{ cur = +d.dataset.i; render(); window.scrollTo(0,0); };
  });
}

function showDone(){
  document.getElementById('card').style.display='none';
  const d = document.getElementById('done');
  // 注意: CSS 里 #done{display:none} 是样式表规则, style.display='' 清不掉,
  // 必须显式设为 'block' —— 否则标完后页面一片空白(已踩坑)
  d.style.display='block';
  const c = {good:0, ok:0, bad:0};
  ITEMS.forEach(i=>{ if(ans[i.id]) c[ans[i.id]]++; });
  const tot = ITEMS.length;
  document.getElementById('sum').innerHTML =
    '<b>像</b> '+c.good+'（'+Math.round(100*c.good/tot)+'%）　'+
    '<b>勉强</b> '+c.ok+'（'+Math.round(100*c.ok/tot)+'%）　'+
    '<b>瞎画</b> '+c.bad+'（'+Math.round(100*c.bad/tot)+'%）';
  document.getElementById('json').value = JSON.stringify(
    {total:tot, counts:c, labels:ans}, null, 1);
  paintGrid();
}

function doExport(){
  const blob = new Blob([JSON.stringify({
    total:ITEMS.length, labels:ans}, null, 1)], {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'golden_labels.json';
  a.click();
}

document.querySelectorAll('.btns button').forEach(b=>{
  b.onclick = ()=>{
    const it = ITEMS[cur];
    ans[it.id] = b.dataset.v; save();
    cur++; render(); window.scrollTo(0,0);
  };
});
document.getElementById('prev').onclick = ()=>{ if(cur>0){cur--;render();} };
document.getElementById('next').onclick = ()=>{
  if(cur<ITEMS.length-1){cur++;render();} window.scrollTo(0,0);
};
document.getElementById('clr').onclick = ()=>{
  if(confirm('清空所有标注？')){ ans={}; save(); cur=0; render(); }
};
document.getElementById('exp').onclick = doExport;
document.getElementById('exp2').onclick = doExport;
document.addEventListener('keydown', e=>{
  const m = {'1':'good','2':'ok','3':'bad'};
  if(m[e.key]){
    const it = ITEMS[cur];
    if(it){ ans[it.id]=m[e.key]; save(); cur++; render(); window.scrollTo(0,0); }
  } else if(e.key==='ArrowLeft'){ if(cur>0){cur--;render();} }
  else if(e.key==='ArrowRight'){ if(cur<ITEMS.length-1){cur++;render();} }
});
render();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="charts_golden/manifest.json")
    ap.add_argument("--out", default="golden_label.html")
    args = ap.parse_args()

    mp = os.path.join(_ROOT, args.manifest)
    if not os.path.isfile(mp):
        print("找不到:", mp)
        sys.exit(1)

    m = json.load(open(mp, encoding="utf-8"))
    items = [x for x in m.get("items", []) if x.get("status") == "ok"]
    if not items:
        print("没有可标注的图片（status=ok 的数量为 0）")
        print("状态分布:", {x.get("status") for x in m.get("items", [])})
        sys.exit(1)

    imgdir = os.path.dirname(args.manifest).replace("\\", "/") + "/"
    html = (TPL.replace("__ITEMS__", json.dumps(items, ensure_ascii=False))
               .replace("__IMGDIR__", imgdir))

    out = os.path.join(_ROOT, args.out)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print("可标注图片 %d / %d 条" % (len(items), m.get("total")))
    print("已生成:", out)


if __name__ == "__main__":
    main()
