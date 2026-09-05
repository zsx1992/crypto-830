#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主动学习标注页 v3 — 专门为朱哥定制，区别于 make_replay_sheet.py

关键改造（基于朱哥 158 张标注后发现的真实需求）：
  1. 去重身份：v2 manifest 内 (symbol,interval,pattern_type,end_ms) 重复行合并
  2. 排除已判：他 v0 时代标过的身份不再重复出图
  3. 分层随机：每种形态均衡轮转（避免他上次的"几何分升序=垃圾优先"陷阱）
  4. 保存到服务器：面板是沙箱，下载/剪贴板被拦；同源 fetch POST 到
     /__save_annotations（serve_with_save.py 的端点）能绕过沙箱
  5. 自动保存：页面加载 + 每次点击判定后自动 POST，朱哥再也不用担心刷新丢数据
  6. 独立 localStorage KEY（crypto830_replay_v3）：不动 v0 时代的旧进度

用法:
  python tools/make_annotate_v3.py \\
      --manifest output/history_replay/manifest.json \\
      --out     output/history_replay/annotate_v3.html \\
      --prefix  history_replay/

依赖：serve_with_save.py 在 8765 端口运行（已验证可用）
"""
import argparse
import collections
import json
import os
import random
import sys

# 复用 v0 标注页的中文映射
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_replay_sheet import PATTERN_CN, DIRECTION_CN, HTML_TMPL  # noqa


HTML_TMPL_V3 = HTML_TMPL.replace(
    'crypto830_replay_v1',
    'crypto830_replay_v3'
).replace(
    '>导出结果 JSON<',
    '>导出结果 JSON</button>\n'
    '    <button onclick="saveToServer()">保存到服务器</button>'
).replace(
    '<p class="sub">__SUB_DETAIL__</p>',
    '<p class="sub">__SUB_DETAIL__</p>\n'
    '  <p class="sub" id="serverStatus" style="color:#888780;font-size:12px;">'
    '  自动保存已开启：每次判定都会推到 8765 服务器，文件落到 user_annotations.json</p>'
).replace(
    "function set(i, v) {\n  ans[i] = v;\n  localStorage.setItem(KEY, JSON.stringify(ans));",
    "function set(i, v) {\n  ans[i] = v;\n  localStorage.setItem(KEY, JSON.stringify(ans));\n"
    "  saveToServer();"
).replace(
    "    const c = document.getElementById('card-' + i);\n"
    "    if (c) { c.className = 'card ' + v;",
    "    saveToServer();\n"
    "    const c = document.getElementById('card-' + i);\n"
    "    if (c) { c.className = 'card ' + v;"
).replace(
    "function flash(msg) {",
    "function saveToServer() {\n"
    "  try {\n"
    "    fetch('/__save_annotations', {\n"
    "      method: 'POST',\n"
    "      headers: {'Content-Type': 'application/json'},\n"
    "      body: JSON.stringify(buildResult())\n"
    "    }).then(r => r.json()).then(d => {\n"
    "      const el = document.getElementById('serverStatus');\n"
    "      if (el) el.textContent = '已推服务器 · judged=' + d.judged + ' · '\n"
    "        + new Date().toLocaleTimeString();\n"
    "    }).catch(e => {\n"
    "      const el = document.getElementById('serverStatus');\n"
    "      if (el) el.textContent = '服务器推送失败: ' + e.message + '（数据仍在 localStorage）';\n"
    "    });\n"
    "  } catch(e) {}\n"
    "}\n"
    "function flash(msg) {"
).replace(
    "refresh();",
    "refresh();\n"
    "// 页面加载后自动推一次（兜底，即便他不点也能保存）\n"
    "setTimeout(saveToServer, 1500);"
)


def stable_key(r):
    return (r.get("symbol"), r.get("interval"),
            r.get("pattern_type"), r.get("end_ms"))


def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description="主动学习标注页 v3")
    ap.add_argument("--manifest", required=True,
                    help="v2 manifest（output/history_replay/manifest.json）")
    ap.add_argument("--annotations",
                    default="output/history_replay/user_annotations.json",
                    help="朱哥已标注 JSON，用于排除已判身份")
    ap.add_argument("--v0-manifest",
                    default="output/history_replay/manifest_before_p1wedge.json",
                    help="v0 manifest（用于把标注图名 → 稳定身份）")
    ap.add_argument("--out", required=True, help="输出 HTML")
    ap.add_argument("--prefix", default="history_replay/",
                    help="HTML 中图片相对路径前缀")
    ap.add_argument("--seed", type=int, default=42,
                    help="分层随机的种子（朱哥用固定 seed 看的是同一批图）")
    args = ap.parse_args()

    # 读 v2 当前推送集
    m = load_json(args.manifest)
    rows = m.get("rows", [])
    print(f"[v2 manifest] {len(rows)} 行")

    # 收集朱哥已判的稳定身份
    judged_keys = set()
    if os.path.exists(args.annotations):
        ann = load_json(args.annotations)
        judged = [r for r in ann.get("rows", [])
                  if r.get("verdict") in ("ok", "bad", "meh")]
        if os.path.exists(args.v0_manifest):
            v0 = load_json(args.v0_manifest)
            v0key = {os.path.basename(r["image"]): stable_key(r)
                     for r in v0.get("rows", [])}
            for j in judged:
                k = v0key.get(os.path.basename(j.get("chart", "")))
                if k:
                    judged_keys.add(k)
        print(f"[已判身份] {len(judged_keys)} 个（用稳定身份排除）")

    # 去重身份
    seen = set()
    uniq = []
    dup_count = 0
    for r in rows:
        k = stable_key(r)
        if k in seen:
            dup_count += 1
            continue
        seen.add(k)
        uniq.append(r)
    print(f"[去重] {len(rows)} 行 → {len(uniq)} 个唯一身份"
          f"（去掉 {dup_count} 行同事件重复）")

    # 排除已判
    todo = [r for r in uniq if stable_key(r) not in judged_keys]
    print(f"[待标] {len(todo)} 个身份（去掉 {len(uniq)-len(todo)} 已判）")

    # 分层随机排序：每种形态均衡轮转
    random.seed(args.seed)
    buckets = collections.defaultdict(list)
    for r in todo:
        buckets[r.get("pattern_type", "?")].append(r)
    # 大类优先（让大头先开始轮转）
    types = sorted(buckets.keys(), key=lambda t: -len(buckets[t]))
    print(f"[分层] 形态类别 {len(types)} 种")
    for t in types:
        print(f"    {t:<24} {len(buckets[t])} 张")

    ordered = []
    while any(buckets[t] for t in types):
        for t in types:
            if buckets[t]:
                idx = random.randrange(len(buckets[t]))
                ordered.append(buckets[t].pop(idx))
    print(f"[排序] 分层随机完成，共 {len(ordered)} 张")

    # 构建 items
    items = []
    for r in ordered:
        pt = r.get("pattern_type", "")
        items.append({
            "file": args.prefix + r.get("image", ""),
            "symbol": r.get("symbol", ""),
            "interval": r.get("interval", ""),
            "pattern": pt,
            "patternCn": PATTERN_CN.get(pt, pt),
            "direction": DIRECTION_CN.get(str(r.get("direction", "")),
                                           r.get("direction", "")),
            "confidence": r.get("confidence"),
            "spanBars": r.get("span_bars"),
            "strength": r.get("strength"),
            "geometryScore": r.get("geometry_score"),
            "geometryReason": r.get("geometry_reason"),
            "end_date": r.get("end_date"),
            "kind": "回放",
            "isRandom": False,
        })
    for it in items:
        g = it.get("geometryScore")
        it["geometryCls"] = (
            "good" if g is not None and g >= 0.7
            else "bad" if g is not None and g < 0.6
            else "mid" if g is not None
            else ""
        )

    # 头部说明
    sub_intro = (
        f"v3 主动学习标注页：v2 当前推送集 <b>{len(rows)} 行</b>"
        f"去重后 <b>{len(uniq)} 唯一事件</b>，"
        f"去掉你 v0 时代已标过 (<b>{len(judged_keys)}</b> 个身份) 后剩 "
        f"<b>{len(items)} 张</b>。"
    )
    sub_detail = (
        "排序策略：按形态分层随机（每种均衡轮转）。不再按几何分升序——上次那种"
        "「垃圾优先」让你标了 141 张不像。新版：好坏都见，对训练分类器更有用。"
        "自动保存到 <code>output/history_replay/user_annotations.json</code>，"
        "数据不会丢。"
    )

    html = HTML_TMPL_V3
    html = html.replace("__SUB_INTRO__", sub_intro)
    html = html.replace("__SUB_DETAIL__", sub_detail)
    html = html.replace("__ITEMS__", json.dumps(items, ensure_ascii=False))
    # v3 隔离：让 saveToServer 写到独立文件，不污染 user_annotations.json
    html = html.replace(
        "body: JSON.stringify(buildResult())",
        "body: JSON.stringify(Object.assign({}, buildResult(), {__target:'user_annotations_v3.json'}))"
    )
    html = html.replace(
        "已推服务器 · judged=",
        "已推服务器 → user_annotations_v3.json · judged="
    )
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nv3 标注页已生成: {args.out}")

    print(f"\n按形态统计（出图顺序 = 分层随机）:")
    for k, v in collections.Counter(i["patternCn"] for i in items).most_common():
        print(f"    {k:<14} {v}")
    print(f"\n直接浏览器打开即可标注。每次点击判定会自动 POST 到 /__save_annotations")


if __name__ == "__main__":
    main()