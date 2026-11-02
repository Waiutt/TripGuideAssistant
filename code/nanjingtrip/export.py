# -*- coding: utf-8 -*-
"""把某几天的行程导成**精简版**（给人看的一页纸）。

为什么不复用时间轴那段文本
--------------------------
时间轴是"内部工作稿"：带 `【固定】/【可选】`、带每条的判断理由、带"写入后按真实通行重算"
这类说明。导出的东西是拿去打印、发给同行人、贴进备忘录的，**只该留时刻、地点、停留**，
其余一律去掉——否则一页纸变成三页，反而看不清。

本模块只做渲染，不读库、不取数：输入是已经排好的逐日事项，输出一段 Markdown。
"""
from __future__ import annotations

from typing import Iterable, Optional

#: 一次导出最多几天（前端可以拦，但后端必须自己校验）
MAX_EXPORT_DAYS = 7

#: 这些类别在导出的表格里不写"停留"（出发/返回/交通段没有停留时长）
_NO_STAY = ('交通',)


def _week_cn(weekday: str) -> str:
    text = str(weekday or '')
    if text in ('一', '二', '三', '四', '五', '六', '日'):
        return '周' + text
    return text


def _cn_date(iso: str, weekday: str = '') -> str:
    """2026-11-05 → 11月5日 周四"""
    try:
        _, month, day = str(iso).split('-')
        text = f'{int(month)}月{int(day)}日'
    except (ValueError, AttributeError):
        text = str(iso)
    got = _week_cn(weekday)
    return f'{text} {got}'.strip()


def _clean_title(title: str, poi: str = '') -> str:
    """条目名去掉括号里的解释与"游览/吃饭"这类动词前缀，只留地点"""
    text = str(title or '').strip()
    if poi:
        text = str(poi)
    else:
        for prefix in ('游览', '参观', '前往'):
            if text.startswith(prefix):
                text = text[len(prefix):]
    text = text.replace('（', '(').split('(')[0].strip()
    return text or str(poi or '')


def brief_markdown(days: Iterable[dict], title: str = '南京行程',
                   subtitle: str = '') -> str:
    """逐日渲染精简版表格。

    输入 `days` 是逐日字典，每项形如：
      {'date': '2026-11-05', 'weekday': '四', 'items': [
          {'start': '10:00', 'end': '12:20', 'title': '总统府', 'poi': '总统府',
           'duration_min': 140, 'category': '展览', 'cost': 0}, ...],
       'cost': {'food': 70, 'ticket': 0, 'ticket_unknown': 2, 'fare': 2, 'total': 72}}
    没有事项的日子**不导出**（避免拿留白冒充安排）。
    """
    days = [d for d in (days or []) if d.get('items')]
    if not days:
        return ''
    head = [f'# {title}',
            f'{_cn_date(days[0].get("date"), days[0].get("weekday"))} – '
            f'{_cn_date(days[-1].get("date"), days[-1].get("weekday"))}'
            f'（{len(days)} 天）']
    if subtitle:
        head.append(subtitle)
    out = ['\n'.join(head), '']
    stops = 0
    total = 0
    ticket_unknown = 0
    for day in days:
        out.append(f'## {_cn_date(day.get("date"), day.get("weekday"))}')
        out.append('| 时间 | 安排 | 停留 |')
        out.append('|---|---|---|')
        for it in day.get('items') or []:
            start = str(it.get('start') or '')
            end = str(it.get('end') or '')
            when = f'{start}–{end}' if start and end else (start or end or '')
            name = _clean_title(it.get('title') or '', it.get('poi') or '')
            if not name:
                continue
            stay = ''
            if str(it.get('category') or '') not in _NO_STAY and it.get('duration_min'):
                stay = f'{int(it["duration_min"])} 分'
            if str(it.get('category') or '') not in _NO_STAY:
                stops += 1
            out.append(f'| {when} | {name} | {stay} |')
        cost = day.get('cost') or {}
        day_total = int(cost.get('total') or 0)
        day_pending = int(cost.get('ticket_unknown') or 0)
        total += day_total
        ticket_unknown += day_pending
        parts = []
        if cost.get('food'):
            parts.append(f'餐饮 ¥{cost["food"]}')
        if cost.get('ticket'):
            parts.append(f'门票 ¥{cost["ticket"]}')
        if cost.get('fare'):
            parts.append(f'交通 ¥{cost["fare"]}')
        line = '费用：' + (' + '.join(parts) if parts else '这一天没有可计价的项')
        if day_total:
            line += f' ≈ ¥{day_total}'
        if day_pending:
            line += f'（门票待核实 {day_pending} 处）'
        out.append('')
        out.append(line)
        out.append('')
    out.append('---')
    summary = f'合计：{len(days)} 天 · {stops} 个点位'
    if total:
        summary += f' · 已计价约 ¥{total} 起'
    if ticket_unknown:
        summary += f'（另有 {ticket_unknown} 处门票待核实）'
    out.append(summary)
    out.append('')
    out.append('> 门票与餐饮取城市包收录价，交通按公交/地铁 2 元/次估算；'
               '标"待核实"的以现场价为准。')
    return '\n'.join(out)


def clamp_range(start: Optional[str], end: Optional[str]) -> tuple[bool, str]:
    """校验导出范围：最多 7 天、起止合法。返回 (ok, 错误信息)"""
    from datetime import date
    try:
        a = date.fromisoformat(str(start))
        b = date.fromisoformat(str(end))
    except (TypeError, ValueError):
        return False, '日期格式不对，应形如 2026-11-05'
    if b < a:
        return False, '结束日期早于开始日期'
    span = (b - a).days + 1
    if span > MAX_EXPORT_DAYS:
        return False, f'一次最多导出 {MAX_EXPORT_DAYS} 天（这次是 {span} 天），请分段导出'
    return True, ''
