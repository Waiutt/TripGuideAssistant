# -*- coding: utf-8 -*-
"""把 Stitch（或其他 AI 设计工具）导出的 HTML/CSS 解析成设计令牌，并给出迁移风险。

为什么需要它
------------
Stitch 的产物通常是"一个屏一个 HTML + 大量内联样式"，直接搬进项目会变成无法维护的
静态稿。正确做法是：**先把它用到的色值/字号/圆角/间距/阴影抽成令牌**，
再让 Qt 和后续改版都从这套令牌渲染——这才是"无伤迁移"的真实含义：
视觉 1:1，代码结构化。

用法
----
    python tools/extract_stitch_tokens.py 设计稿.html
    python tools/extract_stitch_tokens.py 设计稿.html -o tokens.md

输出：令牌建议表 + 风险清单（绝对定位、内联样式、悬停态缺失、层级过多等）。
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

# ---------------- 我们的令牌名（与 theme.py 对齐） ----------------
TARGET = ['bg.canvas', 'bg.surface', 'bg.sunken', 'bg.hover',
          'line.subtle', 'line.strong',
          'text.1', 'text.2', 'text.3',
          'accent', 'accent.hover', 'accent.tint',
          'ok', 'warn', 'danger']


class _Collector(HTMLParser):
    """收集 <style> 块与所有 style="" 内联样式，以及 img/position 用量"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.css: list[str] = []
        self.inline: list[str] = []
        self._in_style = False
        self.tags = Counter()
        self.remote_imgs = 0

    def handle_starttag(self, tag, attrs):
        self.tags[tag] += 1
        d = dict(attrs)
        if tag == 'style':
            self._in_style = True
        style = d.get('style')
        if style:
            self.inline.append(style)
        if tag == 'img' and str(d.get('src', '')).startswith(('http://', 'https://')):
            self.remote_imgs += 1

    def handle_endtag(self, tag):
        if tag == 'style':
            self._in_style = False

    def handle_data(self, data):
        if self._in_style:
            self.css.append(data)


# ---------------- 提取 ----------------
def _norm_color(c: str) -> str:
    """#abc → #AABBCC；rgb()/rgba() 统一成 #RRGGBB（丢掉 alpha）"""
    c = c.strip().lower()
    if c.startswith('#'):
        h = c[1:]
        if len(h) == 3:
            h = ''.join(ch * 2 for ch in h)
        return '#' + h[:6].upper()
    m = re.match(r'rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)', c)
    if m:
        return '#%02X%02X%02X' % tuple(int(x) for x in m.groups())
    return ''


def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip('#')
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _lum(h: str) -> float:
    r, g, b = (v / 255 for v in _hex_rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _sat(h: str) -> float:
    r, g, b = _hex_rgb(h)
    mx, mn = max(r, g, b), min(r, g, b)
    return 0.0 if mx == 0 else (mx - mn) / mx


def analyse(css_text: str) -> dict:
    colors = Counter()
    for m in re.finditer(r'#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)', css_text):
        c = _norm_color(m.group(0))
        if c:
            colors[c] += 1

    def vals(prop: str) -> list[str]:
        return [v.strip() for v in
                re.findall(prop + r'\s*:\s*([^;}{]+)', css_text, re.I)]

    sizes = Counter()
    for v in vals('font-size'):
        sizes[v] += 1
    weights = Counter(v for v in vals('font-weight'))
    radii = Counter()
    for v in vals('border-radius'):
        for part in re.split(r'[/\s]+', v):
            if part and part != '0':
                radii[part] += 1
    shadows = Counter(v.replace('\n', ' ') for v in vals('box-shadow') if v.strip())
    spaces = Counter()
    for prop in ('padding', 'margin', 'gap', 'row-gap', 'column-gap'):
        for v in vals(prop):
            for part in v.split():
                if re.match(r'^\d', part):
                    spaces[part] += 1
    families = Counter(v for v in vals('font-family'))
    cssvars = {k: v.strip() for k, v in
               re.findall(r'--([\w-]+)\s*:\s*([^;}{]+)', css_text)}
    return {
        'colors': colors, 'sizes': sizes, 'weights': weights, 'radii': radii,
        'shadows': shadows, 'spaces': spaces, 'families': families,
        'cssvars': cssvars,
        'hover_rules': len(re.findall(r':hover', css_text)),
        'focus_rules': len(re.findall(r':focus', css_text)),
        'backdrop': len(re.findall(r'backdrop-filter', css_text, re.I)),
    }


def _hue(h: str) -> float:
    """色相角 0~360"""
    r, g, b = (v / 255 for v in _hex_rgb(h))
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d == 0:
        return 0.0
    if mx == r:
        return (60 * ((g - b) / d)) % 360
    if mx == g:
        return 60 * ((b - r) / d) + 120
    return 60 * ((r - g) / d) + 240


def _colors_in(text: str) -> list[str]:
    out = []
    for m in re.finditer(r'#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)', text):
        c = _norm_color(m.group(0))
        if c:
            out.append(c)
    return out


def resolve_vars(css_text: str) -> str:
    """把 var(--x) 就地展开成真实值。

    不展开的话，`body{background:var(--bg)}` 里的颜色根本统计不到，
    会导致"画布色"被误判成卡片色（早期版本就踩了这个坑）。
    """
    vars_ = {k: v.strip() for k, v in
             re.findall(r'--([\w-]+)\s*:\s*([^;}{]+)', css_text)}

    def rep(m):
        return vars_.get(m.group(1), (m.group(2) or '').strip())

    return re.sub(r'var\(\s*--([\w-]+)\s*(?:,\s*([^)]*))?\)', rep, css_text)


def _decl(css_text: str, prop_pattern: str) -> list[str]:
    vals: list[str] = []
    for m in re.finditer(prop_pattern + r'\s*:\s*([^;}{]+)', css_text, re.I):
        vals.append(m.group(1))
    return vals


def propose_tokens(css_text: str, colors: Counter) -> dict:
    """把设计稿色值映射到语义令牌。

    关键：**先判断深色还是浅色主题**，再决定"最深=文字"还是"最浅=文字"。
    早期版本只按明度排序，深色稿会被完全推反（把画布推成白色、文字推成黑色）。
    """
    if not colors:
        return {}

    # 阴影里的颜色（多半是纯黑/半透明黑）要排除，否则会污染调色板
    shadow_colors: set[str] = set()
    for v in _decl(css_text, 'box-shadow'):
        shadow_colors.update(_colors_in(v))

    bg = Counter()
    for v in _decl(css_text, 'background(?:-color)?'):
        for c in _colors_in(v):
            bg[c] += 1
    fg = Counter()
    for v in _decl(css_text, r'(?<!-)\bcolor'):
        for c in _colors_in(v):
            fg[c] += 1
    line = Counter()
    for v in _decl(css_text, 'border(?:-color|-top|-bottom|-left|-right)?'):
        for c in _colors_in(v):
            line[c] += 1

    for counter in (bg, fg, line):
        for c in list(counter):
            if c in shadow_colors and counter[c] <= 1:
                del counter[c]

    out: dict[str, str] = {}

    # ---------- 画布与表面 ----------
    bg_ranked = [c for c, _ in bg.most_common() if _sat(c) <= 0.22] or \\
        [c for c, _ in bg.most_common() if _sat(c) <= 0.45] or [min(colors, key=_lum)]
    canvas = bg_ranked[0] if bg_ranked else min(colors, key=_lum)
    dark = _lum(canvas) < 0.5
    out['bg.canvas'] = canvas
    others = [c for c in bg_ranked[1:] if c != canvas]
    # 深色主题里"更亮一档"的是卡片；浅色主题里"更白"的是卡片
    if dark:
        cand = sorted(others, key=_lum, reverse=True)
    else:
        cand = sorted(others, key=_lum)
    if cand:
        out['bg.surface'] = cand[0]
        if len(cand) > 1:
            out['bg.sunken'] = cand[-1] if dark else cand[-1]
            out['bg.hover'] = cand[-1]

    # ---------- 文字三级 ----------
    fg_ranked = [c for c, _ in fg.most_common() if _sat(c) <= 0.25] or \\
        [c for c, _ in fg.most_common()]
    if not fg_ranked:
        fg_ranked = sorted(colors, key=_lum, reverse=dark)
    out['text.1'] = fg_ranked[0]
    rest = sorted({c for c in fg_ranked[1:]} - {out['text.1']},
                  key=lambda c: abs(_lum(c) - _lum(out['text.1'])))
    if rest:
        out['text.2'] = rest[-1] if len(rest) == 1 else rest[len(rest) // 2]
        out['text.3'] = rest[-1]

    # ---------- 描边 ----------
    line_ranked = [c for c, _ in line.most_common() if c != out['bg.surface']]
    if line_ranked:
        out['line.subtle'] = line_ranked[0]
        if len(line_ranked) > 1:
            out['line.strong'] = max(line_ranked[:4], key=_lum) if dark else \
                min(line_ranked[:4], key=_lum)

    # ---------- 强调色与语义色：按饱和度 + 色相 ----------
    vivid = [c for c in colors if _sat(c) > 0.3 and c not in shadow_colors]
    for c in sorted(vivid, key=lambda x: -_sat(x)):
        h = _hue(c)
        if 'accent' not in out and not (25 <= h <= 45) and not (0 <= h <= 15) \
                and not (345 <= h <= 360):
            out['accent'] = c
        elif 25 <= h <= 55 and 'warn' not in out:
            out['warn'] = c
        elif (0 <= h <= 15 or 345 <= h <= 360) and 'danger' not in out:
            out['danger'] = c
        elif 90 <= h <= 160 and 'ok' not in out and out.get('accent') != c:
            out['ok'] = c
    return out


def theme_kind(props: dict) -> str:
    c = props.get('bg.canvas')
    if not c:
        return '未知'
    return '深色' if _lum(c) < 0.5 else '浅色'


def risks(html: str, inline: list[str], a: dict, remote_imgs: int) -> list[str]:
    out: list[str] = []
    n_abs = len(re.findall(r'position\s*:\s*(absolute|fixed)', html, re.I))
    if n_abs > 3:
        out.append(f'绝对/固定定位 {n_abs} 处 → Qt 里必须改成 Layout，不能照搬坐标')
    if len(inline) > 10:
        out.append(f'内联 style 属性 {len(inline)} 处 → 无法维护，需全部收敛成令牌')
    if a['hover_rules'] < 3:
        out.append(f'`:hover` 只有 {a["hover_rules"]} 条 → 悬停态缺失，我要逐个补')
    if a['focus_rules'] == 0:
        out.append('没有任何 `:focus` 规则 → 键盘焦点不可见，必须补')
    if a['backdrop']:
        out.append('用了 backdrop-filter（真毛玻璃）→ Qt 做不到，只能半透明近似')
    if len(a['sizes']) > 9:
        out.append(f'字号 {len(a["sizes"])} 种 → 超过 9 级，需要收敛成层级')
    if len(a['colors']) > 20:
        out.append(f'色值 {len(a["colors"])} 个 → 未收敛，建议压到 15 个以内')
    if re.search(r'lorem|ipsum', html, re.I):
        out.append('含 lorem ipsum 占位文字 → 我会替换成真实中文内容')
    if remote_imgs:
        out.append(f'{remote_imgs} 张远程图片 → 本地化或替换为矢量/占位')
    if a['radii'] and len(a['radii']) > 5:
        out.append(f'圆角 {len(a["radii"])} 种 → 建议归到 4/8/12/999 四档')
    return out


def render_report(path: Path, a: dict, props: dict, risk_list: list[str]) -> str:
    L: list[str] = [f'# 设计稿令牌提取报告', '', f'来源：`{path.name}`', '']
    L += ['## 一、颜色（按使用次数）', '', '| 色值 | 次数 | 明度 | 饱和度 |', '|---|---|---|---|']
    for c, n in a['colors'].most_common(20):
        L.append(f'| `{c}` | {n} | {_lum(c):.2f} | {_sat(c):.2f} |')

    L += ['', '## 二、令牌映射建议（可直接粘进 theme.py 的 TOKENS）', '',
          '| 令牌 | 建议色值 | 置信度 |', '|---|---|---|']
    for k in TARGET:
        if k in props:
            L.append(f'| `{k}` | `{props[k]}` | 自动推断，请人工确认 |')

    L += ['', '## 三、字号 / 字重', '',
          '| 字号 | 次数 |', '|---|---|']
    for s, n in a['sizes'].most_common():
        L.append(f'| {s} | {n} |')
    if a['weights']:
        L.append('')
        L.append('字重：' + ' / '.join(f'{w}×{n}' for w, n in a['weights'].most_common()))

    if a['radii']:
        L += ['', '## 四、圆角', '', ' / '.join(
            f'{r}×{n}' for r, n in a['radii'].most_common(8))]
    if a['shadows']:
        L += ['', '## 五、阴影', '']
        L += [f'- `{s}`' for s, _ in a['shadows'].most_common(5)]
    if a['spaces']:
        L += ['', '## 六、间距', '', ' / '.join(
            f'{s}×{n}' for s, n in a['spaces'].most_common(12))]
    if a['families']:
        L += ['', '## 七、字体', '']
        L += [f'- `{f}`' for f, _ in a['families'].most_common(3)]
    if a['cssvars']:
        L += ['', '## 八、设计稿自带的 CSS 变量', '']
        L += [f'- `--{k}: {v}`' for k, v in list(a['cssvars'].items())[:20]]

    L += ['', '## 九、迁移风险', '']
    L += [f'- ⚠️ {r}' for r in risk_list] if risk_list else ['- 未发现明显风险']
    L.append('')
    return '\n'.join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description='从设计稿 HTML/CSS 提取设计令牌')
    ap.add_argument('source', help='Stitch 导出的 .html 或 .css 文件')
    ap.add_argument('-o', '--out', default=None, help='报告输出路径（默认只打印）')
    args = ap.parse_args()

    p = Path(args.source)
    if not p.exists():
        print(f'找不到文件：{p}', file=sys.stderr)
        return 1
    raw = p.read_text(encoding='utf-8', errors='ignore')

    if p.suffix.lower() in ('.html', '.htm'):
        col = _Collector()
        col.feed(raw)
        css = '\n'.join(col.css) + '\n' + '\n'.join(col.inline)
        inline, remote = col.inline, col.remote_imgs
    else:
        css, inline, remote = raw, [], 0

    css = resolve_vars(css)
    a = analyse(css)
    props = propose_tokens(css, a['colors'])
    risk_list = risks(raw, inline, a, remote)
    report = render_report(p, a, props, risk_list)

    if args.out:
        Path(args.out).write_text(report, encoding='utf-8')
        print(f'已写出：{args.out}')
    else:
        print(report)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
