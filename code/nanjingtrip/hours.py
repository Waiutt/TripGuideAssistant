# -*- coding: utf-8 -*-
"""营业时间与"吃饭"窗口检查：产出**软提示**（notices），不阻断排程。

与 :mod:`engine` 的分工
-----------------------
* engine 判定"**排不下**"（时间重叠、无空档、超时段）→ ``ScheduleResult.conflicts``，
  可行性为 False；
* 本模块判定"**排得下但会扑空/不合理**"（闭馆日、超出开放时段、需预约、漏了饭点）
  → ``ScheduleResult.notices``，可行性仍为 True。

这样"周一去故宫"会在**预览阶段就被提示**，但不会把一条本来排得下的行程判为失败；
用户可以选择改天，也可以坚持（例如只是路过门口）。

所有判断都基于城市包里 POI 的 ``open`` / ``closed`` / ``book`` 字段，
换城市、换 POI 即换规则，不含任何硬编码的城市逻辑。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from .cities import WEEK_CN
from .models import (Conflict, Poi, ScheduleOptions, ScheduleResult, Suggestion,
                     Task, hhmm)

# 饭点窗口（分钟数）：只在这两个窗口被其他安排占满且当天没排餐饮时才提示
LUNCH = (11 * 60 + 30, 13 * 60 + 30)
DINNER = (17 * 60 + 30, 19 * 60 + 30)

# 需要预约的类别（这些地点没预约大概率进不去，值得单独提示）
_BOOKING_KINDS = ('景点', '场馆', '校园')


def _hm(text: Optional[str]) -> Optional[int]:
    """'08:30' -> 510（分钟）；解析失败返回 None"""
    if not text:
        return None
    try:
        h, m = str(text).split(':')[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def weekday_cn(d: date) -> str:
    """date -> '周一'…'周日'"""
    return WEEK_CN[d.weekday()]


# ======================================================================
# 单点检查
# ======================================================================
def closed_detail(poi: Optional[Poi], plan_date: date) -> str:
    """闭馆日提示文案；不闭馆返回空串"""
    if poi is None or not poi.closed_days:
        return ''
    wd = weekday_cn(plan_date)
    if wd not in poi.closed_days:
        return ''
    return f'「{poi.name}」{poi.closed_label()}，这一天去会扑空'


def hours_hint(poi: Optional[Poi], start_min: int, end_min: int) -> str:
    """超出开放时段的方向：'early'（排在开门前）/ 'late'（排到闭馆后）/ ''（未超出）"""
    if poi is None:
        return ''
    o, c = _hm(poi.open_from), _hm(poi.open_to)
    if o is None or c is None or c <= o:
        return ''
    if start_min >= o and end_min <= c:
        return ''
    return 'early' if start_min < o else 'late'


def hours_detail(poi: Optional[Poi], start_min: int, end_min: int) -> str:
    """超出开放时段提示文案；未超出或数据缺失返回空串"""
    hint = hours_hint(poi, start_min, end_min)
    if not hint:
        return ''
    head = (f'「{poi.name}」开放时间 {poi.open_from}-{poi.open_to}，'
            f'当前排在 {hhmm(start_min)}-{hhmm(end_min)}')
    if hint == 'early':
        # 说清差多少分钟：用户才知道是"早到十分钟"还是"根本还没开门"
        return f'{head}，比开门时间早 {_hm(poi.open_from) - start_min} 分钟'
    return f'{head}，超出开放时段'


def next_open_date(poi: Optional[Poi], plan_date: date,
                   lookahead: int = 7) -> Optional[date]:
    """从明天起找最近一个不闭馆的日期（供"挪到某天"建议使用）"""
    if poi is None or not poi.closed_days:
        return None
    for i in range(1, lookahead + 1):
        d = plan_date + timedelta(days=i)
        if weekday_cn(d) not in poi.closed_days:
            return d
    return None


# ======================================================================
# 集合检查
# ======================================================================
def closed_notices(tasks: list[Task], plan_date: date) -> list[Conflict]:
    """当日任务里落在闭馆日的项"""
    out: list[Conflict] = []
    seen = set()
    for t in tasks:
        detail = closed_detail(t.poi, plan_date)
        if not detail or detail in seen:
            continue
        seen.add(detail)
        out.append(Conflict(kind='closed_day', detail=detail,
                            task_title=t.title))
    return out


def hours_notices(result: ScheduleResult) -> list[Conflict]:
    """已排入的项是否落在开放时段之外"""
    out: list[Conflict] = []
    for it in result.items:
        detail = hours_detail(it.task.poi, it.start_min, it.end_min)
        if detail:
            out.append(Conflict(kind='out_of_hours', detail=detail,
                                hint=hours_hint(it.task.poi, it.start_min, it.end_min),
                                task_title=it.task.title))
    return out


def booking_notices(tasks: list[Task]) -> list[Conflict]:
    """需要预约的地点汇总成一条，避免逐条刷屏"""
    names = []
    for t in tasks:
        poi = t.poi
        if poi is None or not poi.book_hint:
            continue
        if poi.kind in _BOOKING_KINDS or poi.kind == '住宿':
            if poi.name not in names:
                names.append(poi.name)
    if not names:
        return []
    return [Conflict(
        kind='need_booking',
        detail='需提前预约/购票：' + '、'.join(names[:4])
               + ('等' if len(names) > 4 else ''),
        task_title='预约')]


def meal_notices(result: ScheduleResult,
                 options: Optional[ScheduleOptions] = None) -> list[Conflict]:
    """饭点被占满却没安排吃饭时提示（吃/喝覆盖的兜底保障）"""
    if options is not None and not options.meal_notice:
        return []
    items = [it for it in result.items if it.end_min > it.start_min]
    if not items:
        return []
    foods = [(it.start_min, it.end_min) for it in items
             if (it.task.poi is not None and it.task.poi.is_food)
             # 没有地点的"午休块"（category=餐饮）也算安排了吃饭
             or it.task.category == '餐饮']
    out: list[Conflict] = []
    for label, (a, b) in (('午餐', LUNCH), ('晚餐', DINNER)):
        # 只有当天的墙钟安排确实横跨了该窗口，才可能"漏饭"
        if not any(it.start_min < b and a < it.end_min for it in items):
            continue
        if any(s < b and a < e for s, e in foods):
            continue
        out.append(Conflict(
            kind='meal_missing',
            detail=f'{label}时段（{hhmm(a)}-{hhmm(b)}）排满了其他安排，但当天没有餐饮项',
            task_title=label))
    return out


def return_notice(result: ScheduleResult,
                  options: Optional[ScheduleOptions] = None) -> list[Conflict]:
    """最晚返回截止：预计回到起点的时刻晚于用户要求时提示（不静默放行）"""
    if options is None or not options.return_by or result.return_leg is None:
        return []
    if not result.items:
        return []
    back = result.items[-1].end_min + result.return_leg.duration_min
    if back <= options.return_by:
        return []
    late = back - options.return_by
    return [Conflict(
        kind='return_late',
        detail=(f'预计 {hhmm(back)} 才回到起点（回程 '
                f'{result.return_leg.duration_min} 分钟），'
                f'比要求的 {hhmm(options.return_by)} 晚 {late} 分钟'),
        overlap_min=late,
        task_title='回程',
        suggestions=[Suggestion(
            kind='keep',
            text=f'把末项结束时间提前 {late} 分钟（缩短停留或少去一个点），'
                 '或把返回截止时间往后调'),
        ])]


def collect(tasks: list[Task], result: ScheduleResult, plan_date: date,
            options: Optional[ScheduleOptions] = None) -> list[Conflict]:
    """汇总当日全部软提示：闭馆 → 超时段 → 返回截止 → 预约 → 饭点"""
    notices: list[Conflict] = []
    notices.extend(closed_notices(tasks, plan_date))
    notices.extend(hours_notices(result))
    notices.extend(return_notice(result, options))
    notices.extend(booking_notices(tasks))
    notices.extend(meal_notices(result, options))
    return notices
