# -*- coding: utf-8 -*-
"""界面比例验证：把「实现截图」与「设计稿实测值」逐项比对。

为什么要有这个工具
------------------
Stitch 设计稿（`设计稿/stitch_nanjingtrip_desktop_ui_suite/nanjingtrip_1/code.html`）
的版面不是靠像素眼估的，而是用无头浏览器读 DOM 的 `getBoundingClientRect()` 量出来的：

    顶栏 60 · 底条 56 · 外边距 12 · 栏间距 12 · 左右栏 340 / 420 · 中栏 flex(1440 下 632)
    x 坐标：12 | 340 | 12 | 632 | 12 | 420 | 12

实现侧没有 DOM 可量，于是改从截图里"反解"版面：设计稿是把卡片摆在一块 #111319 的画布上，
卡片之间留 12px 的画布色缝，所以截图里**整列都是画布色**的那几列就是栏与栏之间的缝。
定位这些缝，就能还原三栏宽度与栏间距，再与设计值比对。

用法
----
    python tools/ui_proportion_check.py                       # 用默认两张图
    python tools/ui_proportion_check.py --shot 某截图.png
    python tools/ui_proportion_check.py --json 结果.json      # 顺便存机器可读结果

判据：任一指标相对偏差超过 `--tol`（默认 3%）即视为不通过，退出码 1。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

try:
    from PIL import Image
except ImportError:                                    # pragma: no cover
    print('需要 Pillow：python -m pip install pillow')
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SHOT = ROOT / '06_图表' / '界面主视图.png'
CANVAS = (17, 19, 25)          # #111319 画布
GAP = 12                       # 设计稿栏间距 / 外边距
TOL = 0.03                     # 允许偏差 3%

# 设计稿实测（无头浏览器读 DOM，1440×900 视口）
DESIGN = {
    'viewport_w': 1440,
    'topbar_h': 60,
    'gap': 12,
    'left_w': 340,
    'center_w': 632,
    'right_w': 420,
}


@dataclass
class Measure:
    name: str
    design: float
    actual: float
    unit: str = 'px'

    @property
    def delta(self) -> float:
        return (self.actual - self.design) / self.design if self.design else 0.0

    @property
    def ok(self) -> bool:
        return abs(self.delta) <= TOL


def _near(px, ref, tol=4) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(px[:3], ref))


def find_gutters(img: Image.Image, y0: int, y1: int, x0: int, x1: int,
                 need: float = 0.97) -> list[tuple[int, int]]:
    """找出整列都是画布色的列，合并成区间 [(start, end), ...]"""
    w, h = img.size
    px = img.convert('RGB').load()
    rows = range(max(0, y0), min(h, y1), 3)
    cols = []
    for x in range(max(0, x0), min(w, x1)):
        hit = sum(1 for y in rows if _near(px[x, y], CANVAS))
        if rows and hit / len(list(rows)) >= need:
            cols.append(x)
    runs, start = [], None
    for i, x in enumerate(cols):
        if start is None:
            start = x
        elif x != cols[i - 1] + 1:
            runs.append((start, cols[i - 1] + 1))
            start = x
    if start is not None:
        runs.append((start, cols[-1] + 1))
    return runs


def find_topbar_bottom(img: Image.Image, x0: int, x1: int) -> int | None:
    """顶栏底边：一行的多数像素是那条半透明细线（比画布略亮、带绿味）"""
    w, h = img.size
    px = img.convert('RGB').load()
    xs = list(range(max(0, x0), min(w, x1), 4))
    for y in range(0, min(h, 140)):
        hits = 0
        for x in xs:
            r, g, b = px[x, y][:3]
            if 26 <= r <= 60 and 30 <= g <= 68 and 28 <= b <= 60 and g >= r:
                hits += 1
        if xs and hits / len(xs) >= 0.6:
            return y + 1
    return None


def measure(shot: Path) -> tuple[list[Measure], dict]:
    img = Image.open(shot)
    w, h = img.size
    topbar = find_topbar_bottom(img, 0, w) or 0

    # 只在「栏内内容区」找缝：避开顶栏与底部录入条
    y0, y1 = topbar + 6, h - 90
    runs = find_gutters(img, y0, y1, 0, w)
    # 两端外边距也是画布色区间，去掉它们，只留中间的分隔缝
    inner = [r for r in runs if r[0] > 2 and r[1] < w - 2]
    info = {'size': [w, h], 'topbar_bottom': topbar,
            'gutters': inner, 'outer_runs': [r for r in runs if r not in inner]}

    ms: list[Measure] = [Measure('顶栏高度', DESIGN['topbar_h'], topbar)]
    if len(inner) >= 2:
        g1, g2 = inner[0], inner[-1]
        left = g1[0] - GAP
        center = g2[0] - g1[1]
        right = (w - GAP) - g2[1]
        ms += [
            Measure('左栏宽度', DESIGN['left_w'], left),
            Measure('中栏宽度', DESIGN['center_w'], center),
            Measure('右栏宽度', DESIGN['right_w'], right),
            Measure('左缝宽度', DESIGN['gap'], g1[1] - g1[0]),
            Measure('右缝宽度', DESIGN['gap'], g2[1] - g2[0]),
        ]
        info['panes'] = {'left': left, 'center': center, 'right': right}
    else:
        info['panes'] = None
    return ms, info


def report(ms: list[Measure], info: dict, shot: Path, tol: float) -> bool:
    print('=' * 68)
    print('界面比例验证 · 实现截图 vs 设计稿（DOM 实测）')
    print('=' * 68)
    print(f'截图        {shot}')
    print(f'画面尺寸    {info["size"][0]} × {info["size"][1]}')
    print(f'顶栏底边    y = {info["topbar_bottom"]}')
    if info['panes']:
        p = info['panes']
        print(f'三栏实测    左 {p["left"]} / 中 {p["center"]} / 右 {p["right"]}')
    print(f'画布色缝    {info["gutters"]}')
    print('-' * 68)
    print(f'{"指标":<10}{"设计":>8}{"实现":>8}{"偏差":>10}   判定')
    print('-' * 68)
    ok = True
    for m in ms:
        good = abs(m.delta) <= tol
        ok = ok and good
        print(f'{m.name:<10}{m.design:>8.0f}{m.actual:>8.0f}'
              f'{m.delta * 100:>9.1f}%   {"通过" if good else "偏差过大"}')
    print('-' * 68)
    print('结论        ' + ('全部在设计容差内，版面与设计稿一致'
                           if ok else '存在超差项，需要调整 CSS 版面'))
    print(f'容差        ±{tol * 100:.0f}%')
    print('=' * 68)
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='界面比例验证（实现截图 vs 设计稿）')
    ap.add_argument('--shot', default=str(DEFAULT_SHOT), help='实现截图路径')
    ap.add_argument('--tol', type=float, default=TOL, help='允许相对偏差，默认 0.03')
    ap.add_argument('--json', default=None, help='把结果写成 JSON')
    a = ap.parse_args(argv)

    shot = Path(a.shot)
    if not shot.exists():
        print(f'找不到截图：{shot}')
        return 2
    ms, info = measure(shot)
    ok = report(ms, info, shot, a.tol)
    if a.json:
        Path(a.json).write_text(json.dumps({
            'shot': str(shot), 'info': info, 'tol': a.tol, 'ok': ok,
            'items': [{**asdict(m), 'delta': m.delta, 'ok': m.ok} for m in ms],
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'已写出 {a.json}')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
