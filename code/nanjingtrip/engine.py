# -*- coding: utf-8 -*-
"""ScheduleEngine：通行时长感知的排程引擎（纯函数，可单测）。

约束（对应业务规则）：
  BR-02 时间不重叠
  BR-03 通行段必须计入
  BR-04 缓冲时间（每段通行后保留 buffer）
  BR-05 冲突不静默（输出 Conflict 由 ConflictAdvisor 生成建议）

算法：锁定优先 + 贪心空档扫描，复杂度 O(n·m)。
"""
from __future__ import annotations

from typing import Callable, Optional

from .models import (
    Conflict, Poi, ScheduleOptions, ScheduleResult, ScheduledItem,
    Task, TaskStatus, TransitLeg, TransitMode, hhmm, minutes_of,
    same_place,
)

# 通行时长查询函数签名：(起点 Poi|None, 终点 Poi|None, 交通方式) -> 分钟数|None
TransitFn = Callable[[Optional[Poi], Optional[Poi], TransitMode], Optional[int]]
# 逐段择优函数签名：(起点 Poi|None, 终点 Poi|None, 默认方式) -> 选中的方式
ModePickFn = Callable[[Optional[Poi], Optional[Poi], TransitMode], TransitMode]


def _mode_of(task: Task, options: ScheduleOptions,
             prev_poi: Optional[Poi] = None,
             mode_pick: Optional[ModePickFn] = None) -> TransitMode:
    """本段交通方式：逐段择优 > 事项自己指定 > 全局默认

    ``options.auto_mode`` 打开且提供了 ``mode_pick`` 时，按"这一段实际要走的
    起终点"问一次哪种方式最快。注意它**优先于** ``task.mode``：任务上留存的
    ``mode`` 往往是"上一次排程的结果"，不覆盖的话自动择优永远不生效。
    手动「换方式」会顺带关掉自动择优（见 TripService.set_mode），语义不冲突。
    """
    if options.auto_mode and mode_pick is not None:
        return mode_pick(prev_poi, task.poi, options.mode)
    if task.mode is not None:
        return task.mode
    return options.mode


def _transit(transit_fn: TransitFn, a: Optional[Poi], b: Optional[Poi],
             mode: TransitMode, at_min: Optional[int] = None,
             peak=None) -> tuple[int, Optional[TransitLeg]]:
    """查询通行时长；同一地点视为 0，任一地点缺失也视为 0（无需通行）。

    ``at_min`` 给出出发时刻、``peak`` 给出时段模型时，按"这个点出发"放大耗时
    （早晚高峰 ×1.35 等）。不传则用原始耗时，行为与以前完全一致。
    """
    if a is None or b is None:
        return 0, None
    if same_place(a, b):
        # 同点之间不该有通行；也不能走下面"查不到→30 分钟"的兜底
        return 0, None
    minutes = transit_fn(a, b, mode)
    if minutes is None:
        # 查不到通行时长时按保守估计 30 分钟处理，并在结果中标注
        minutes = 30
    elif peak is not None and at_min is not None:
        try:
            f = peak.factor(int(at_min) % (24 * 60), mode)
        except Exception:                     # noqa: BLE001 模型异常不影响排程
            f = 1.0
        if f and f != 1.0:
            minutes = max(1, int(round(minutes * f)))
    leg = TransitLeg(from_poi_id=a.poi_id, to_poi_id=b.poi_id,
                     mode=mode, duration_min=int(minutes), source='engine')
    return int(minutes), leg


def schedule(tasks: list[Task], options: ScheduleOptions,
             transit_fn: TransitFn,
             mode_pick: Optional[ModePickFn] = None) -> ScheduleResult:
    """求解当日时间轴。

    :param tasks: 当日任务集合（已含 poi 与 duration_min）
    :param options: 排程选项（交通方式、缓冲、可排时段、是否保持顺序/自动择优）
    :param transit_fn: 通行时长查询函数（由 TransitService 提供，含缓存）
    :param mode_pick: 逐段择优函数（``options.auto_mode`` 为真时使用）
    :return: ScheduleResult（items 有序、unscheduled、conflicts、explanation）
    """
    result = ScheduleResult()
    buffer = max(0, options.buffer_min)
    lower = options.start_min()
    upper = options.end_min()

    # ---------- 1. 分离已锁定 / 待排 ----------
    locked = [t for t in tasks if t.is_locked and t.start_ts is not None]
    pending = [t for t in tasks if not (t.is_locked and t.start_ts is not None)]
    locked.sort(key=lambda t: minutes_of(t.start_ts))  # type: ignore[arg-type]

    entries: list[ScheduledItem] = []

    # ---------- 2. 锁定项直接入骨架，并检测锁定项之间的重叠 ----------
    for t in locked:
        s = minutes_of(t.start_ts)  # type: ignore[arg-type]
        e = s + t.duration_min
        if s < lower or e > upper:
            result.conflicts.append(Conflict(
                kind='out_of_range',
                detail=f'「{t.title}」{hhmm(s)}-{hhmm(e)} 超出可排时段 '
                       f'{hhmm(lower)}-{hhmm(upper)}',
                task_title=t.title))
        entries = _rebuild_legs(entries + [ScheduledItem(task=t, start_min=s, end_min=e)],
                                options, transit_fn, mode_pick)

    # 锁定项重叠检测（BR-02 例外：用户强行调整时保留，但必须提示）
    for i in range(1, len(entries)):
        prev, cur = entries[i - 1], entries[i]
        if cur.start_min < prev.end_min:
            overlap = prev.end_min - cur.start_min
            result.conflicts.append(Conflict(
                kind='locked_overlap',
                detail=f'「{prev.task.title}」与「{cur.task.title}」时间重叠 {overlap} 分钟',
                overlap_min=overlap,
                task_title=cur.task.title,
                with_title=prev.task.title))

    # ---------- 3. 待排任务：耗时降序、输入序 ----------
    order = {id(t): i for i, t in enumerate(pending)}
    if options.optimize_route and not options.keep_order and len(pending) > 1:
        # 用户说"没有固定顺序、帮我排最顺路"：按总通行时长最小重排
        if options.prefer_indoor:
            # 雨天：先室内后室外，两组内部各自最短（只调整偏好，不删任何点位）
            indoor = [t for t in pending if t.poi is not None and t.poi.indoor]
            outdoor = [t for t in pending if not (t.poi is not None and t.poi.indoor)]
            if indoor and outdoor:
                indoor_o = _optimize_order(indoor, options.lodging, transit_fn,
                                           options, mode_pick)
                anchor = options.lodging
                for t in reversed(indoor_o):
                    if t.poi is not None:
                        anchor = t.poi
                        break
                pending = indoor_o + _optimize_order(outdoor, anchor, transit_fn,
                                                     options, mode_pick)
            else:
                pending = _optimize_order(pending, options.lodging, transit_fn,
                                          options, mode_pick)
        else:
            pending = _optimize_order(pending, options.lodging, transit_fn,
                                      options, mode_pick)
        order = {id(t): i for i, t in enumerate(pending)}
    if options.keep_order:
        # 用户说了"依次去这几个地方"：保持给定顺序，后一项串在前一项之后
        pending.sort(key=lambda t: order[id(t)])
    else:
        pending.sort(key=lambda t: (-t.duration_min, order[id(t)]))

    chain_lower: Optional[int] = None
    for task in pending:
        deadline_min = minutes_of(task.deadline) if task.deadline else None
        best = _find_best_slot(task, entries, options, transit_fn, lower, upper,
                               buffer, mode_pick, deadline_min=deadline_min,
                               start_lower=chain_lower if options.keep_order else None)
        if best is None:
            task.status = TaskStatus.UNSCHEDULED
            task.start_ts = None
            task.end_ts = None
            result.unscheduled.append(task)
            if deadline_min is not None:
                result.conflicts.append(Conflict(
                    kind='deadline_miss',
                    detail=(f'「{task.title}」要求 {hhmm(deadline_min)} 前抵达，'
                            f'但含通行与缓冲后当天排不进这个时刻之前'),
                    task_title=task.title))
            else:
                result.conflicts.append(Conflict(
                    kind='no_slot',
                    detail=f'「{task.title}」需 {task.duration_min} 分钟，当日已无满足'
                           f'通行与缓冲约束的空档',
                    task_title=task.title))
            continue
        cost, start, end, leg = best
        item = ScheduledItem(task=task, start_min=start, end_min=end, leg_before=leg)
        task.start_ts = item.start_ts
        task.end_ts = item.end_ts
        task.status = TaskStatus.SCHEDULED
        entries.append(item)
        entries = _rebuild_legs(entries, options, transit_fn, mode_pick)
        if options.keep_order:
            # 下一项必须排在这一项结束之后（含通行与缓冲由空档扫描自己算）
            chain_lower = item.end_min

    # ---------- 4. 排序、编号、写回 ----------
    entries.sort(key=lambda i: i.start_min)
    # 序号默认按时间；"按给定顺序排"时序号要跟**用户给的顺序**（= 传入 tasks 的顺序）一致。
    # 否则写库后序号被改写成时间序，下一次重排读回来就不是用户的原话了：
    # 锁定项（比如 12:00 订的午饭）会把两者拉开，于是每插一条就偏一点，越排越乱。
    seq_of: dict[int, int] = {}
    if options.keep_order:
        #: 锁定项也算用户顺序的一部分（不能把"9:00 先到总统府"挤到序号末尾）
        seq_of = {id(t): n for n, t in enumerate(tasks, start=1)}
    for idx, it in enumerate(entries, start=1):
        it.task.seq = seq_of.get(id(it.task), idx)
        it.task.start_ts = it.start_ts
        it.task.end_ts = it.end_ts
        it.task.status = TaskStatus.SCHEDULED
    if options.keep_order:
        for k, t in enumerate(result.unscheduled, start=len(tasks) + 1):
            t.seq = seq_of.get(id(t), len(tasks) + k)
    result.items = entries
    # 住宿锚点的回程段：末项 → 酒店（仅在设置了锚点且当日有安排时计算）
    if options.lodging is not None and entries:
        back_mode = _mode_of_by_pair(entries[-1].task.poi, options.lodging, options,
                                    mode_pick, options.mode)
        _, leg = _transit(transit_fn, entries[-1].task.poi, options.lodging, back_mode)
        result.return_leg = leg
    result.explanation = _explain(entries, result, options)
    return result


def _rebuild_legs(entries: list[ScheduledItem], options: ScheduleOptions,
                  transit_fn: TransitFn,
                  mode_pick: Optional[ModePickFn] = None) -> list[ScheduledItem]:
    """重算相邻项之间的通行段（骨架变化后必须刷新）

    每段的方式取"到达那一项"自己的选择（Task.mode），未指定则按逐段择优或全局默认。
    设置了住宿锚点（``options.lodging``）时，首项的 ``leg_before`` 即"酒店→首项"。
    """
    entries = sorted(entries, key=lambda i: i.start_min)
    prev_poi: Optional[Poi] = options.lodging
    prev_end: int = options.start_min()
    for it in entries:
        if prev_poi is None:
            it.leg_before = None
        else:
            m = _mode_of(it.task, options, prev_poi, mode_pick)
            # 出发时刻：上一项结束的时刻；但首段可能远晚于 day_start（例如"十一点才去"），
            # 这时要按"到达时刻 − 原始通行 − 缓冲"倒推，否则会把早高峰系数错套到平峰上。
            depart = prev_end
            if it.start_min is not None and it.task.poi is not None:
                try:
                    raw = transit_fn(prev_poi, it.task.poi, m)
                except Exception:                 # noqa: BLE001 取不到就按上一项结束算
                    raw = None
                raw_min = int(raw) if raw is not None else 30
                depart = max(prev_end, it.start_min - raw_min - options.buffer_min)
            _, leg = _transit(transit_fn, prev_poi, it.task.poi, m,
                              at_min=depart, peak=options.peak_model)
            it.leg_before = leg
            if options.auto_mode:
                # 把择优结果写回任务：徽标、统计、地图都读 task.mode，不写回就对不上
                it.task.mode = m
        # 没有地点的"时间块"（如午休）不改变位置：通行链要跨过它继续算，
        # 否则"上一站→下一站"会变成 0 分钟。
        if it.task.poi is not None:
            prev_poi = it.task.poi
        prev_end = it.end_min
    return entries


def _nearest_poi(entries: list[ScheduledItem], start: Optional[ScheduledItem],
                 home: Optional[Poi], step: int) -> Optional[Poi]:
    """从 start 起沿 step 方向找最近一个**带地点**的项，返回它的地点。

    没有地点的"时间块"（午休/休息）不产生通行，找通行起终点时要跳过它们，
    否则会算出"这里走过去 0 分钟"，把空档插入位置算歪。
    """
    if start is None:
        return home
    try:
        i = next(k for k, it in enumerate(entries) if it is start)
    except StopIteration:
        return home
    while 0 <= i < len(entries):
        poi = entries[i].task.poi
        if poi is not None:
            return poi
        i += step
    return home


def _find_best_slot(task: Task, entries: list[ScheduledItem],
                    options: ScheduleOptions, transit_fn: TransitFn,
                    lower: int, upper: int, buffer: int,
                    mode_pick: Optional[ModePickFn] = None,
                    deadline_min: Optional[int] = None,
                    start_lower: Optional[int] = None):
    """在骨架空档中为任务寻找最优插入位置。

    规则 A3：通行总时长最小优先，其次开始时间最早。
    ``deadline_min`` 给定时，只接受开始时刻不晚于它的空档（绝对截止语义）。
    ``start_lower`` 给定时，开始时刻不得早于它——用于"按给定顺序排"（keep_order）时
    强制后一项串在前一项之后，否则短任务会抢进前面的空档、把顺序打乱。
    :return: (cost, start_min, end_min, leg_before) 或 None
    """
    entries = sorted(entries, key=lambda i: i.start_min)
    candidates = []

    # 构造空档：(gap_start, gap_end, prev_item, next_item)
    gaps: list[tuple[int, int, Optional[ScheduledItem], Optional[ScheduledItem]]] = []
    if not entries:
        gaps.append((lower, upper, None, None))
    else:
        gaps.append((lower, entries[0].start_min, None, entries[0]))
        for i in range(len(entries) - 1):
            gaps.append((entries[i].end_min, entries[i + 1].start_min,
                         entries[i], entries[i + 1]))
        gaps.append((entries[-1].end_min, upper, entries[-1], None))

    for gap_start, gap_end, prev_item, next_item in gaps:
        # 住宿锚点：首个空档的前驱是"酒店"（出门要花时间），
        # 末个空档的后继也是"酒店"（回程要留时间）。未设锚点时为 None，行为不变。
        home: Optional[Poi] = options.lodging
        prev_poi = _nearest_poi(entries, prev_item, home, step=-1)
        next_poi = _nearest_poi(entries, next_item, home, step=+1)

        # 进这一段的交通方式用"本任务"的选择；出这一段用"下一项"的选择
        in_mode = _mode_of(task, options, prev_poi, mode_pick)
        out_mode = (_mode_of(next_item.task, options, task.poi, mode_pick)
                    if next_item else options.mode)
        # 出发时刻用空档起点近似（高峰加成取决于"几点上路"）
        t_prev, _ = _transit(transit_fn, prev_poi, task.poi, in_mode,
                             at_min=gap_start, peak=options.peak_model)
        t_next, _ = _transit(transit_fn, task.poi, next_poi, out_mode,
                             at_min=gap_start + t_prev + buffer + task.duration_min,
                             peak=options.peak_model)
        # 下一项若是"没有地点的时间块"（午休），离开这一项不需要赶路：
        # 否则会被算成"午休前还得先回一趟酒店"，把本来塞得进的空档判成不可行
        # （11-18 案例：上午空转 115 分钟的真凶）。
        if next_item is not None and next_item.task.poi is None:
            t_next = 0

        earliest = gap_start + (t_prev + buffer if prev_poi else 0)
        latest = gap_end - (t_next + buffer if next_poi else 0) - task.duration_min
        if start_lower is not None and earliest < start_lower:
            earliest = start_lower      # 顺序约束：不得抢在前一项之前
        if latest < earliest:
            continue  # 该空档容不下（BR-03 / BR-04）
        if deadline_min is not None and earliest > deadline_min:
            continue  # 绝对截止：赶不上这个时刻的空档直接排除

        start = earliest
        # 用户指定期望时段：在不破坏可行性的前提下尽量贴近
        if task.desired_start is not None:
            want = minutes_of(task.desired_start)
            if want > start:
                start = min(want, latest)
        # 不早于开门：到位太早只能在门口等，不如把这段留白
        open_min, close_min = _open_window(task.poi)
        if (task.desired_start is None and open_min is not None
                and start < open_min <= latest):
            start = open_min
        # 不晚于闭馆：能提前就提前到"关门前刚好逛完"
        if (task.desired_start is None and close_min is not None
                and start + task.duration_min > close_min):
            alt = close_min - task.duration_min
            if earliest <= alt <= latest:
                start = alt
        end = start + task.duration_min

        # 不重叠校验（BR-02）
        overlap = False
        for it in entries:
            if start < it.end_min and it.start_min < end:
                overlap = True
                break
        if overlap:
            continue

        _, leg = _transit(transit_fn, prev_poi, task.poi, in_mode,
                          at_min=start - t_prev - buffer, peak=options.peak_model)
        cost = t_prev + t_next
        # 开放时段偏好（不是硬约束）：景点排在开门前/闭馆后是"能排但白跑一趟"，
        # 只要当天还有落在营业时间内的空档就优先选它；全都放不下才退回原候选，
        # 并由 hours_notices 如实提示"排在开门前/闭馆后"。
        out_of_hours = 0
        if open_min is not None and start < open_min:
            out_of_hours = 1
        elif close_min is not None and end > close_min:
            out_of_hours = 1
        candidates.append((out_of_hours, cost, start, end, leg))

    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    best = candidates[0]
    return (best[1], best[2], best[3], best[4])


def _open_window(poi: Optional[Poi]) -> tuple[Optional[int], Optional[int]]:
    """地点的营业区间（分钟）。无营业信息、或"全天开放"这类异常值都返回 (None, None)。"""
    if poi is None:
        return None, None

    def _hm(text: Optional[str]) -> Optional[int]:
        try:
            hh, mm = str(text or '').split(':')[:2]
            return int(hh) * 60 + int(mm)
        except (ValueError, TypeError):
            return None

    o, c = _hm(poi.open_from), _hm(poi.open_to)
    if o is None or c is None or c <= o:
        return None, None
    return o, c


def _explain(entries: list[ScheduledItem], result: ScheduleResult,
             options: ScheduleOptions) -> str:
    """生成可解释的排程说明（答辩时可直接展示）"""
    if not entries:
        return '当日暂无安排。'
    total_transit = sum(i.leg_before.duration_min for i in entries if i.leg_before)
    span = entries[-1].end_min - entries[0].start_min
    busy = sum(i.task.duration_min for i in entries)
    idle = max(0, span - busy - total_transit)
    parts = []
    if getattr(options, 'optimize_route', False):
        # 让界面自己说清"我替你重排过顺序"，而不是默默改
        parts.append('已按最短路自动排序（避免来回折返）')
    parts += [
        f'共排入 {len(entries)} 项，首项 {hhmm(entries[0].start_min)} 开始、'
        f'末项 {hhmm(entries[-1].end_min)} 结束',
        f'通行合计 {total_transit} 分钟（按各段所选方式，缓冲 {options.buffer_min} 分钟/段）',
        f'事项占用 {busy} 分钟，中间空档 {idle} 分钟',
    ]
    if result.unscheduled:
        parts.append('未排入 ' + '、'.join(f'「{t.title}」' for t in result.unscheduled))
    if options.lodging is not None:
        back = (f'，回酒店约 {result.return_leg.duration_min} 分钟'
                if result.return_leg else '')
        parts.append(f'以「{options.lodging.name}」为出发点与回程终点{back}')
    return '；'.join(parts) + '。'

def _optimize_order(tasks: list[Task], anchor: Optional[Poi],
                    transit_fn: TransitFn, options: ScheduleOptions,
                    mode_pick: Optional[ModePickFn] = None) -> list[Task]:
    """把待排任务排成"总通行时长尽量小"的顺序：最近邻起步 + 2-opt 改进。

    只对**没有锁定时刻**的任务生效（锁定项已经在骨架上，顺序不由这里决定）。
    目标函数 = 相邻两段通行分钟数之和（含从 anchor 出发的第一段），
    与"折返最少"一致：折返必然抬高这个和。
    """
    if len(tasks) < 3:
        return tasks

    mode_default = options.mode
    cache: dict[tuple, int] = {}

    def leg(a: Optional[Poi], b: Optional[Poi]) -> int:
        if a is None or b is None:
            return 0
        key = (a.name, b.name)
        if key not in cache:
            m = _mode_of_by_pair(a, b, options, mode_pick, mode_default)
            minutes = transit_fn(a, b, m)
            cache[key] = int(minutes) if minutes is not None else 30
        return cache[key]

    def total(order: list[Task]) -> int:
        prev = anchor
        cost = 0
        for t in order:
            cost += leg(prev, t.poi)
            prev = t.poi
        return cost

    # ---------- 1. 最近邻 ----------
    rest = list(tasks)
    order: list[Task] = []
    cur = anchor
    while rest:
        nxt = min(rest, key=lambda t: leg(cur, t.poi))
        order.append(nxt)
        rest.remove(nxt)
        cur = nxt.poi
    best_cost = total(order)

    # ---------- 1.5 小规模直接枚举精确解（一天通常 ≤7 个点，7! = 5040 可接受）----------
    if len(order) <= 7:
        import itertools
        best_perm, best_cost_ex = None, None
        for perm in itertools.permutations(tasks):
            c = total(list(perm))
            if best_cost_ex is None or c < best_cost_ex:
                best_perm, best_cost_ex = perm, c
        if best_perm is not None and best_cost_ex <= best_cost:
            return list(best_perm)

    # ---------- 2. 2-opt（点数较多时的启发式改进）----------
    improved = True
    rounds = 0
    while improved and rounds < 40:
        improved = False
        rounds += 1
        for i in range(len(order) - 1):
            for j in range(i + 2, len(order)):
                cand = order[:i + 1] + order[i + 1:j + 1][::-1] + order[j + 1:]
                c = total(cand)
                if c < best_cost:
                    order, best_cost = cand, c
                    improved = True
    return order


def _mode_of_by_pair(a: Poi, b: Poi, options: ScheduleOptions,
                     mode_pick: Optional[ModePickFn],
                     default: TransitMode) -> TransitMode:
    """排序阶段按两地点取方式：优先逐段择优，否则用全局默认"""
    if options.auto_mode and mode_pick is not None:
        return mode_pick(a, b, default)
    return default
