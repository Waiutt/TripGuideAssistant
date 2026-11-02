# -*- coding: utf-8 -*-
"""ConflictAdvisor：把排程冲突翻译成用户可执行的建议（BR-05 冲突不静默）。

两类输入：
* ``ScheduleResult.conflicts``（硬冲突：排不下）→ :func:`advise`；
* ``ScheduleResult.notices``（软提示：排得下但会扑空/不合理）→ :func:`advise_notices`。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from .cities import WEEK_CN
from .hours import next_open_date
from .models import (Conflict, Poi, ScheduleOptions, ScheduleResult, Suggestion,
                     hhmm)



def advise(result: ScheduleResult, options: ScheduleOptions) -> list[Suggestion]:
    """遍历冲突，为每类冲突给出不少于一条可执行建议。"""
    suggestions: list[Suggestion] = []
    for c in result.conflicts:
        if c.kind == 'no_slot':
            suggestions.extend(_advise_no_slot(c, result, options))
        elif c.kind == 'locked_overlap':
            suggestions.extend(_advise_overlap(c, result, options))
        elif c.kind == 'out_of_range':
            suggestions.append(Suggestion(
                kind='extend_window',
                text=f'把可排时段延长到 {options.day_end.hour}:59 后重新排程',
                patch={'day_end': '23:59'}))
        if c.suggestions:
            continue
    # 去重（相同文本只保留一条）
    seen, uniq = set(), []
    for s in suggestions:
        if s.text not in seen:
            seen.add(s.text)
            uniq.append(s)
    return uniq


def _find_task(result: ScheduleResult, title: str):
    for t in result.unscheduled:
        if t.title == title:
            return t
    return None


def _advise_no_slot(c: Conflict, result: ScheduleResult,
                    options: ScheduleOptions) -> list[Suggestion]:
    out: list[Suggestion] = []
    task = _find_task(result, c.task_title)
    dur = task.duration_min if task else 0

    out.append(Suggestion(
        kind='next_day',
        text=f'把「{c.task_title}」移到次日（{dur} 分钟）',
        patch={'move_next_day': True, 'title': c.task_title}))

    if dur > 30:
        cut = max(30, dur // 2)
        out.append(Suggestion(
            kind='shorten',
            text=f'将「{c.task_title}」耗时从 {dur} 分钟压缩到 {cut} 分钟后再试',
            patch={'duration_min': cut, 'title': c.task_title}))

    if options.mode.value != 'walking':
        out.append(Suggestion(
            kind='mode',
            text='改用步行/公交（市内短距离时通行时间更稳定）后重排',
            patch={'mode': 'walking'}))

    if sum(i.task.duration_min for i in result.items) > 0:
        earliest = result.items[-1].end_min + options.buffer_min if result.items else None
        if earliest:
            out.append(Suggestion(
                kind='shift_tail',
                text=f'把「{c.task_title}」顺延到 {hhmm(earliest)} 之后（越过现有末项）',
                patch={'start_ts': hhmm(earliest), 'title': c.task_title}))
    return out


def _advise_overlap(c: Conflict, result: ScheduleResult,
                    options: ScheduleOptions) -> list[Suggestion]:
    out: list[Suggestion] = []
    later = [i for i in result.items if i.task.title == c.task_title]
    earlier = [i for i in result.items if i.task.title == c.with_title]
    if later and earlier:
        base = earlier[0].end_min + options.buffer_min
        out.append(Suggestion(
            kind='shift',
            text=f'把「{c.task_title}」顺延到 {hhmm(base)} 开始',
            patch={'start_ts': hhmm(base), 'title': c.task_title}))
        shorter = max(15, earlier[0].task.duration_min - c.overlap_min)
        out.append(Suggestion(
            kind='shorten_prev',
            text=f'把「{c.with_title}」耗时缩短 {c.overlap_min} 分钟'
                 f'（改为 {shorter} 分钟）',
            patch={'duration_min': shorter, 'title': c.with_title}))
    out.append(Suggestion(
        kind='cancel',
        text=f'取消「{c.task_title}」，当日按原计划执行',
        patch={'delete': c.task_title}))
    return out


def attach_suggestions(result: ScheduleResult, options: ScheduleOptions) -> None:
    """把建议写回冲突对象，便于界面直接展示"""
    all_sugg = advise(result, options)
    for c in result.conflicts:
        if c.kind == 'no_slot':
            c.suggestions = [s for s in all_sugg
                             if s.patch.get('title') == c.task_title
                             or s.kind == 'mode']
        elif c.kind == 'locked_overlap':
            c.suggestions = [s for s in all_sugg
                             if s.patch.get('title') in (c.task_title, c.with_title)]
        else:
            c.suggestions = all_sugg


# ======================================================================
# 软提示（营业时间 / 闭馆 / 预约 / 饭点）
# ======================================================================
def advise_notices(notices: list[Conflict], plan_date: date,
                   poi_map: Optional[dict] = None) -> list[Suggestion]:
    """为每条软提示给出可执行建议，并写回 ``notice.suggestions``。

    :param poi_map: {任务标题: Poi}，用于查闭馆日与最近开放日
    """
    poi_map = poi_map or {}
    out: list[Suggestion] = []
    for c in notices:
        if c.kind in ('lodging_suggest', 'budget_over', 'budget_meal'):
            continue          # 建议由业务侧（lodging / 预算）生成，此处不覆盖
        mine: list[Suggestion] = []
        if c.kind == 'closed_day':
            nxt = next_open_date(poi_map.get(c.task_title), plan_date)
            if nxt is not None:
                mine.append(Suggestion(
                    kind='shift_day',
                    text=f'把「{c.task_title}」挪到 {nxt.month}月{nxt.day}日'
                         f'（{WEEK_CN[nxt.weekday()]}，不闭馆）',
                    patch={'date': nxt.isoformat(), 'title': c.task_title}))
            mine.append(Suggestion(
                kind='keep',
                text='仍按原计划：请先确认当天是否临时开放（节假日偶有例外）',
                patch={'title': c.task_title}))
        elif c.kind == 'out_of_hours':
            # 方向反了就是坏建议：排在开门前要往后顺，排到闭馆后才要提前
            if c.hint == 'early':
                mine.append(Suggestion(
                    kind='shift_earlier',
                    text=f'把「{c.task_title}」往后顺到开门之后，或先去下一站再折回来',
                    patch={'title': c.task_title}))
            else:
                mine.append(Suggestion(
                    kind='shift_earlier',
                    text=f'把「{c.task_title}」提前到开放时段内，或缩短停留时间',
                    patch={'title': c.task_title}))
        elif c.kind == 'need_booking':
            mine.append(Suggestion(
                kind='book',
                text='出行前在官方渠道完成实名预约与购票（部分场馆当天不放票）'))
        elif c.kind == 'meal_missing':
            mine.append(Suggestion(
                kind='insert_meal',
                text=f'{c.task_title}时段空着，直接说"中午在附近吃个饭，1小时"即可补上',
                patch={'title': c.task_title, 'insert_min': 60}))
        c.suggestions = mine
        out.extend(mine)
    return out


