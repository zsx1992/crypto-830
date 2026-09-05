#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
把人工标注(JSON) 和 回放 manifest 关联，量化「系统画得准不准」。

输入:
  --annotations  标注 JSON (make_replay_sheet 导出的结构: {rows:[{chart,verdict,...}]})
  --manifest     回放 manifest.json ({rows:[{image,geometry_score,span_bars,...}]})

输出:
  - 终端打印文字摘要
  - 写 output/history_replay/analysis_report.html (表格 + 最简柱状)
"""
import json, os, sys, argparse, statistics
from collections import defaultdict

PAT_CN = {
    'double_bottom': '双底/W底', 'double_top': '双顶/M顶',
    'head_shoulders_top': '头肩顶', 'head_shoulders_bottom': '头肩底',
    'rising_wedge': '上升楔形', 'falling_wedge': '下降楔形',
    'ascending_triangle': '上升三角形', 'descending_triangle': '下降三角形',
    'symmetrical_triangle': '对称三角形',
}
DIR_CN = {'LONG': '多', 'SHORT': '空', '多': '多', '空': '空'}


def load_json(p):
    with open(p, encoding='utf-8') as f:
        d = json.load(f)
    if isinstance(d, dict) and 'rows' in d:
        return d['rows']
    if isinstance(d, list):
        return d
    raise SystemExit('无法识别的标注 JSON 结构')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--annotations', required=True)
    ap.add_argument('--manifest', required=True)
    ap.add_argument('--out',
                    default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         '..', 'output', 'history_replay', 'analysis_report.html'))
    args = ap.parse_args()

    ann_rows = load_json(args.annotations)
    with open(args.manifest, encoding='utf-8') as f:
        man = json.load(f)
    man_rows = man['rows'] if isinstance(man, dict) else man
    man_by_img = {r['image']: r for r in man_rows}

    # 关联
    joined = []
    missing = 0
    for a in ann_rows:
        chart = a.get('chart') or a.get('image')
        m = man_by_img.get(chart)
        if not m:
            missing += 1
            continue
        joined.append({
            'chart': chart,
            'verdict': a.get('verdict'),
            'pattern': a.get('pattern_type') or a.get('pattern'),
            'direction': a.get('direction') or m.get('direction'),
            'geometry_score': m.get('geometry_score'),
            'span_bars': m.get('span_bars'),
            'strength_score': m.get('strength_score'),
            'confidence': m.get('confidence'),
            'geometry_reason': m.get('geometry_reason'),
            'symbol': m.get('symbol'),
        })

    judged = [j for j in joined if j['verdict'] in ('ok', 'bad', 'meh')]
    ok = [j for j in judged if j['verdict'] == 'ok']
    bad = [j for j in judged if j['verdict'] == 'bad']
    meh = [j for j in judged if j['verdict'] == 'meh']
    n = len(judged)

    def mean(xs):
        xs = [x for x in xs if x is not None]
        return statistics.mean(xs) if xs else float('nan')

    # 总体精确率（二分类，排除拿不准）
    precision = len(ok) / len(ok + bad) if (ok or bad) else float('nan')

    # 分形态
    by_pat = defaultdict(lambda: {'ok': 0, 'bad': 0, 'meh': 0, 'geo_ok': [], 'geo_bad': []})
    for j in judged:
        b = by_pat[j['pattern']]
        b[j['verdict']] += 1
        if j['verdict'] == 'ok' and j['geometry_score'] is not None:
            b['geo_ok'].append(j['geometry_score'])
        if j['verdict'] == 'bad' and j['geometry_score'] is not None:
            b['geo_bad'].append(j['geometry_score'])

    # 几何分 vs 人工判断
    geo_ok = mean([j['geometry_score'] for j in ok])
    geo_bad = mean([j['geometry_score'] for j in bad])
    str_ok = mean([j['strength_score'] for j in ok])
    str_bad = mean([j['strength_score'] for j in bad])
    conf_ok = mean([j['confidence'] for j in ok])
    conf_bad = mean([j['confidence'] for j in bad])
    span_ok = mean([j['span_bars'] for j in ok])
    span_bad = mean([j['span_bars'] for j in bad])

    # 系统阈(几何>=0.7 算"标准") vs 人工
    sys_std = [j for j in judged if (j['geometry_score'] or 0) >= 0.7]
    sys_std_ok = sum(1 for j in sys_std if j['verdict'] == 'ok')
    sys_std_bad = sum(1 for j in sys_std if j['verdict'] == 'bad')

    # 矛盾案例
    hi_geo_but_bad = [j for j in bad if (j['geometry_score'] or 0) >= 0.7]   # 系统画得标准但人说不
    lo_geo_but_ok = [j for j in ok if (j['geometry_score'] or 0) < 0.6]      # 系统画得歪但人说像

    # ---- 文字摘要 ----
    print('=' * 60)
    print('人工标注 × 回放 manifest 关联分析')
    print('=' * 60)
    print(f'关联成功 {len(joined)} 条，缺失(图名对不上) {missing} 条')
    print(f'已判 {n} 条:  像 {len(ok)} / 不像 {len(bad)} / 拿不准 {len(meh)}')
    print(f'推送精确率(像/(像+不像)) = {precision:.1%}')
    print('-' * 60)
    print(f'几何分均值   像 {geo_ok:.2f}  vs  不像 {geo_bad:.2f}   (差 {geo_ok-geo_bad:+.2f})')
    print(f'强度分均值   像 {str_ok:.1f}  vs  不像 {str_bad:.1f}')
    print(f'置信度均值   像 {conf_ok:.2f}  vs  不像 {conf_bad:.2f}')
    print(f'跨度均值     像 {span_ok:.0f}  vs  不像 {span_bad:.0f} 根')
    print('-' * 60)
    print(f'系统几何>=0.7 标记"标准"的 {len(sys_std)} 张里: 人说像 {sys_std_ok} / 不像 {sys_std_bad}')
    print(f'  → 若不像占比高，说明 P2 对称性维度压得不够/方向偏')
    print('-' * 60)
    print('分形态精确率:')
    for pat, b in sorted(by_pat.items(), key=lambda kv: -(kv[1]['bad'] + kv[1]['ok'])):
        tot = b['ok'] + b['bad']
        pr = (b['ok'] / tot) if tot else float('nan')
        gok = mean(b['geo_ok']); gbad = mean(b['geo_bad'])
        print(f'  {PAT_CN.get(pat,pat):8s}  判 {tot:3d} (像{b["ok"]}/不像{b["bad"]}/?{b["meh"]})  '
              f'精确率 {pr:.0%}   几何 像{gok:.2f}/不像{gbad:.2f}')
    print('-' * 60)
    print(f'矛盾A(系统几何>=0.7 但人说不像): {len(hi_geo_but_bad)} 张 — 最该修的假标准')
    for j in hi_geo_but_bad[:12]:
        print(f'   {j["symbol"]:6s} {PAT_CN.get(j["pattern"],j["pattern"]) if j["pattern"] else "?":8s} '
              f'geo={j["geometry_score"]:.2f} {j["geometry_reason"]}')
    print(f'矛盾B(系统几何<0.6 但人说像): {len(lo_geo_but_ok)} 张 — 人比几何松')
    for j in lo_geo_but_ok[:12]:
        print(f'   {j["symbol"]:6s} {PAT_CN.get(j["pattern"],j["pattern"]) if j["pattern"] else "?":8s} '
              f'geo={j["geometry_score"]:.2f} {j["geometry_reason"]}')
    print('=' * 60)

    # ---- HTML 报告 ----
    html = _render_html(dict(
        n=n, ok=len(ok), bad=len(bad), meh=len(meh), precision=precision,
        geo_ok=geo_ok, geo_bad=geo_bad, str_ok=str_ok, str_bad=str_bad,
        conf_ok=conf_ok, conf_bad=conf_bad, span_ok=span_ok, span_bad=span_bad,
        sys_std=len(sys_std), sys_std_ok=sys_std_ok, sys_std_bad=sys_std_bad,
        by_pat={PAT_CN.get(p, p): dict(ok=b['ok'], bad=b['bad'], meh=b['meh'],
                                       pr=((b['ok'] / (b['ok'] + b['bad'])) if (b['ok'] + b['bad']) else None),
                                       gok=mean(b['geo_ok']), gbad=mean(b['geo_bad']))
                for p, b in by_pat.items()},
        hi=hi_geo_but_bad, lo=lo_geo_but_ok,
    ))
    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'\nHTML 报告已写: {out}')


def _render_html(d):
    def pct(x):
        return '—' if (x is None or x != x) else f'{x:.0%}'
    def f2(x):
        return '—' if (x is None or x != x) else f'{x:.2f}'
    def f1(x):
        return '—' if (x is None or x != x) else f'{x:.1f}'
    rows = ''
    for pat, b in sorted(d['by_pat'].items(), key=lambda kv: -(kv[1]['bad'] + kv[1]['ok'])):
        bar_w = 0 if not b['pr'] else int(b['pr'] * 120)
        color = '#3B6D11' if (b['pr'] or 0) >= 0.6 else ('#A32D2D' if (b['pr'] or 0) < 0.4 else '#B8860B')
        rows += (f"<tr><td>{pat}</td><td>{b['ok']+b['bad']}</td>"
                 f"<td>{b['ok']}</td><td>{b['bad']}</td><td>{b['meh']}</td>"
                 f"<td>{pct(b['pr'])}</td>"
                 f"<td><div style='width:{bar_w}px;height:10px;background:{color}'></div></td>"
                 f"<td>{f2(b['gok'])}</td><td>{f2(b['gbad'])}</td></tr>")
    hi = ''.join(f"<li>{j['symbol']} · {PAT_CN.get(j['pattern'],j['pattern']) if j['pattern'] else '?'} · "
                 f"geo={j['geometry_score']:.2f} · {j['geometry_reason']}</li>" for j in d['hi'][:20])
    lo = ''.join(f"<li>{j['symbol']} · {PAT_CN.get(j['pattern'],j['pattern']) if j['pattern'] else '?'} · "
                 f"geo={j['geometry_score']:.2f} · {j['geometry_reason']}</li>" for j in d['lo'][:20])
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<style>body{{font-family:-apple-system,"Microsoft YaHei",sans-serif;background:#FAFAF8;color:#2C2C2A;padding:24px;max-width:980px;margin:auto}}
h1{{font-size:20px;font-weight:500}} .k{{display:flex;gap:18px;flex-wrap:wrap;margin:14px 0}}
.k .c{{background:#fff;border:1px solid #E5E3DC;border-radius:10px;padding:12px 16px;min-width:150px}}
.k .c b{{font-size:22px;display:block;color:#185FA5}} table{{border-collapse:collapse;width:100%;margin:12px 0;font-size:13px}}
th,td{{border:1px solid #E5E3DC;padding:6px 8px;text-align:center}} th{{background:#F1EFE8}}
.box{{background:#fff;border:1px solid #E5E3DC;border-radius:10px;padding:12px 16px;margin:12px 0}}
.bad{{color:#A32D2D}} .ok{{color:#3B6D11}}</style></head><body>
<h1>形态标注分析 · 系统画得准不准</h1>
<div class="k">
  <div class="c"><span>已判</span><b>{d['n']}</b></div>
  <div class="c"><span>像 / 不像 / 拿不准</span><b>{d['ok']} / {d['bad']} / {d['meh']}</b></div>
  <div class="c"><span>推送精确率</span><b>{pct(d['precision'])}</b></div>
</div>
<div class="box">
<b>质量分 vs 人工判断（均值）</b>
<table><tr><th></th><th>像(ok)</th><th>不像(bad)</th><th>差值</th></tr>
<tr><td>几何 symmetry 分</td><td>{f2(d['geo_ok'])}</td><td>{f2(d['geo_bad'])}</td><td>{'+%.2f'%(d['geo_ok']-d['geo_bad'])}</td></tr>
<tr><td>综合强度分</td><td>{f1(d['str_ok'])}</td><td>{f1(d['str_bad'])}</td><td></td></tr>
<tr><td>置信度</td><td>{f2(d['conf_ok'])}</td><td>{f2(d['conf_bad'])}</td><td></td></tr>
<tr><td>跨度(根)</td><td>{f1(d['span_ok'])}</td><td>{f1(d['span_bad'])}</td><td></td></tr>
</table>
<p>几何分在「像」组应明显高于「不像」组 —— 若相反或接近，说明 P2 对称性维度没压对方向。</p>
</div>
<div class="box"><b>系统几何≥0.7 标"标准"的 {d['sys_std']} 张里</b>：人说像 {d['sys_std_ok']} / 不像 <span class="bad">{d['sys_std_bad']}</span>。
{d['sys_std_bad']} 张是「系统以为标准、人打脸」的假标准，最该修。</div>
<h3>分形态精确率</h3>
<table><tr><th>形态</th><th>判</th><th>像</th><th>不像</th><th>?</th><th>精确率</th><th></th><th>几何(像)</th><th>几何(不像)</th></tr>
{rows}</table>
<div class="box"><b>矛盾A：系统几何≥0.7 但人说不像（{len(d['hi'])} 张，最该修）</b><ul>{hi or '<li>无</li>'}</ul></div>
<div class="box"><b>矛盾B：系统几何&lt;0.6 但人说像（{len(d['lo'])} 张，人比几何松）</b><ul>{lo or '<li>无</li>'}</ul></div>
</body></html>"""


if __name__ == '__main__':
    main()
