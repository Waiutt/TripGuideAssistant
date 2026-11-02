# -*- coding: utf-8 -*-
"""链路探针：输入一段话 → 排程写库，逐步打印每一步是谁在做、调了什么外部服务、耗时多少。

用途：回答"这段链路是不是独立自主完成的"——把外部依赖（LLM / 高德）全部打点计数，
并额外跑一遍**强制离线**的同一段话，证明没有外部服务时靠本地规则也能跑完。

    python tools/trace_pipeline.py                 # 在线跑一遍 + 离线跑一遍
    python tools/trace_pipeline.py --online-only
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'code'))

from nanjingtrip.app import AppContext                        # noqa: E402
from nanjingtrip.config import SettingsService                 # noqa: E402
from nanjingtrip.parsing_rules import (parse_duration,        # noqa: E402
                                       split_batch, split_by_pois, strip_note_parens)

TEXT = ('我周六早上到南京站，先去玄武湖 1 小时，然后鸡鸣寺 40 分钟，中午在南京大牌档吃饭 1 小时，'
        '下午总统府（这个必须去，我已经预约了）1 小时，再去南京博物院 2 小时，'
        '傍晚夫子庙 1 小时 20 分钟，晚上老门东 1 小时。如果下雨就改去先锋书店。'
        '帮我排最优路线，晚上 21:00 还要赶高铁回上海。')
DAY = '2026-12-18'


class Counter:
    """外部调用打点：把适配器上的方法包一层，只计数与计时"""

    def __init__(self):
        self.calls: dict[str, int] = {}
        self.ms: dict[str, float] = {}

    def wrap(self, obj, name: str, label: str):
        fn = getattr(obj, name, None)
        if fn is None:
            return

        def inner(*a, **kw):
            t0 = time.perf_counter()
            try:
                return fn(*a, **kw)
            finally:
                self.calls[label] = self.calls.get(label, 0) + 1
                self.ms[label] = self.ms.get(label, 0.0) + (time.perf_counter() - t0) * 1000
        setattr(obj, name, inner)

    def report(self) -> str:
        if not self.calls:
            return '无外部调用'
        return '，'.join(f'{k} × {v}（{self.ms[k]:.0f} ms）'
                        for k, v in sorted(self.calls.items()))

    def total(self) -> int:
        return sum(self.calls.values())


def run(online: bool, label: str) -> None:
    tmp = Path(tempfile.mkdtemp(prefix='trace-'))
    if online:
        real = SettingsService()
        for n in ('config.json', 'secrets.json'):
            if (real.dir / n).exists():
                shutil.copy2(real.dir / n, tmp / n)
    ctx = AppContext(tmp)
    if not online:
        ctx.settings.offline_mode = True
        ctx.apply_settings(ctx.settings)

    counter = Counter()
    adapter = ctx.degrade.map()
    for name, tag in (('search_poi', '高德 place/text'), ('place_detail', '高德 place 详情(biz_ext)'),
                      ('estimate', '高德 direction'), ('weather', '高德 天气')):
        counter.wrap(adapter, name, tag)
    llm = ctx.degrade.llm()
    for name in ('parse_intent', 'complete', 'chat'):
        counter.wrap(llm, name, 'DeepSeek 解析')
    counter.wrap(ctx.transit_svc, 'estimate', '通行取数')

    print(f'\n## {label}（{"在线：真实 LLM + 真实高德" if online else "强制离线：规则 + 估算"}）\n')
    print(f'输入（{len(TEXT)} 字）：{TEXT}\n')
    print('| # | 步骤 | 谁在做 | 说明 | 耗时 |')
    print('|---|---|---|---|---|')

    def step(no: int, name: str, who: str, note: str, t0: float) -> None:
        print(f'| {no} | {name} | {who} | {note} | {(time.perf_counter() - t0) * 1000:.0f} ms |')

    t0 = time.perf_counter()
    t = strip_note_parens(TEXT)
    step(1, '预处理：摘掉括号附注', '本地规则 parsing_rules', '「（这个必须去…）」不进解析', t0)

    t0 = time.perf_counter()
    body, conds = ctx.trip_svc._split_conditional(t)
    step(2, '识别条件分支', '本地规则 services._split_conditional',
         f'备选 {len(conds)} 组（{conds[0]["trigger"] if conds else "—"}）', t0)

    t0 = time.perf_counter()
    body = ctx.trip_svc._drop_meta_clauses(body)
    step(3, '丢弃元话语/附注句', '本地规则', '“重复一句”“别排太满”这类整句去掉', t0)

    t0 = time.perf_counter()
    segs = split_batch(body)
    names = list(ctx.geo_svc.pack.names()) + list(ctx.geo_svc.pack.aliases()) \
        if hasattr(ctx.geo_svc, 'pack') else []
    if not names:
        from nanjingtrip.cities import get_city
        pack = get_city(ctx.city)
        names = list(pack.names()) + list(pack.aliases())
    refined: list[str] = []
    for seg in segs:
        sub = split_by_pois(seg, names) if len({n for n in names if n in seg}) >= 2 else []
        refined.extend(sub if len(sub) >= 2 else [seg])
    step(4, '断句（分隔符 + 地点名边界）', '本地规则 split_batch/split_by_pois',
         f'{len(segs)} → {len(refined)} 段', t0)

    t0 = time.perf_counter()
    prev = ctx.trip_svc.preview_batch(TEXT, __import__('datetime').date.fromisoformat(DAY))
    items = prev.get('items') or []
    step(5, '逐段解析（地名/时刻/时长）', 'DeepSeek + 本地规则兜底',
         f'共 {len(items)} 条；未解析 {len(prev.get("unparsed") or [])} 条', t0)

    t0 = time.perf_counter()
    got = [(i.get('poi'), i.get('duration_text')) for i in items]
    step(6, '地点解析 + 景点情报取数', '城市包 → 高德 place/text → biz_ext',
         f'起点 {prev.get("origin") or "未声明"}；返程死线 {prev.get("return_by") or "—"}', t0)

    t0 = time.perf_counter()
    payload = [{k: i.get(k) for k in ('kind', 'title', 'poi', 'duration_min',
                                      'category', 'desired_start', 'raw_text')}
               for i in items if i.get('poi') or i.get('kind') in ('block', 'lodging')]
    msg = ctx.trip_svc.add_batch(__import__('datetime').date.fromisoformat(DAY), payload,
                                origin=prev.get('origin') or None, keep_order=True,
                                return_by=prev.get('return_by') or '')[1]
    step(7, '写入 + 排程 + 落库', '引擎 engine.schedule + SQLite',
         f'{msg}；含通行取数与版本快照', t0)

    t0 = time.perf_counter()
    view = ctx.trip_svc.day_view(__import__('datetime').date.fromisoformat(DAY))
    result = ctx.trip_svc.compute_day(__import__('datetime').date.fromisoformat(DAY))
    step(8, '提示与冲突（营业/闭馆/饭点/天气/预算）', '本地规则 hours/weather/advisor',
         f'提示 {len(result.notices)} 条、冲突 {len(result.conflicts)} 条', t0)

    print()
    print(f'**外部调用合计：{counter.total()} 次** — {counter.report()}')
    print()
    print('时间轴：')
    for it in result.items:
        poi = it.task.poi.name if it.task.poi else '（无地点）'
        print(f'  {it.start_min // 60:02d}:{it.start_min % 60:02d}–'
              f'{it.end_min // 60:02d}:{it.end_min % 60:02d}  {it.task.title}｜{poi}'
              f'｜{it.task.duration_min} 分钟')
    for t in result.unscheduled:
        print(f'  未排入：{t.title}（{t.duration_min} 分钟）')
    if prev.get('conditional'):
        print('备选方案（不排入）：', prev['conditional'])
    print()
    print('提示：')
    for c in result.notices[:6]:
        print(f'  · {c.detail}')

    ctx.close()
    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--online-only', action='store_true')
    a = ap.parse_args()
    run(True, '链路 A · 在线')
    if not a.online_only:
        run(False, '链路 B · 离线（同样的输入，不联网）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
