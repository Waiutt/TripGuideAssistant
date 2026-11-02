# -*- coding: utf-8 -*-
"""出行时段模型：把"同一段路在不同时刻要多久"量化出来，用于给出出发时刻建议。

数据来源与诚实边界（重要）：
  1. **基础时长**来自高德路径规划或本地缓存（真实数据）；
  2. **时段系数**目前是本地的经验值（南京早晚高峰 / 平峰 / 夜间），
     **不是高德的预测值**——高德 Web 服务未提供公开的"按出发时刻预测"接口；
  3. 该模型被设计成可替换：若将来接入带 departure_time 的预测接口
     （如腾讯位置服务 future_eta 或高德路径规划 2.0 的到达时间能力），
     只需替换 `TrafficModel.factor()`，上层建议逻辑不用改。

覆盖率说明：步行不受拥堵影响（系数恒为 1.0），公交地铁的地面段受拥堵影响但小于驾车。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .models import TransitMode, hhmm

# (起始分钟, 结束分钟, 系数, 说明)  —— 南京经验值
PEAK_WINDOWS = (
    (7 * 60, 9 * 60 + 30, '早高峰'),
    (17 * 60, 19 * 60 + 30, '晚高峰'),
)
NIGHT_RANGE = (22 * 60, 5 * 60)          # 跨零点

PEAK_FACTOR = {'driving': 1.35, 'transit': 1.20, 'walking': 1.0,
               'bicycling': 1.05}
NIGHT_FACTOR = {'driving': 0.85, 'transit': 0.90, 'walking': 1.0,
                'bicycling': 1.0}
FLAT_FACTOR = {'driving': 1.0, 'transit': 1.0, 'walking': 1.0,
               'bicycling': 1.0}


class TrafficModel:
    """时段系数模型（可替换为真实预测数据源）"""

    #: 节假日拥堵：在高峰系数之上再乘的倍数（驾车受影响最大）
    HOLIDAY_FACTOR = {'driving': 1.20, 'transit': 1.10, 'walking': 1.0,
                      'bicycling': 1.0}
    #: 节假日平峰也按此倍数放大（假期全天车多）
    HOLIDAY_FLAT = {'driving': 1.15, 'transit': 1.08, 'walking': 1.0,
                    'bicycling': 1.0}

    def __init__(self, peak_factor: dict | None = None,
                 night_factor: dict | None = None,
                 holiday: bool = False):
        self.peak = peak_factor or PEAK_FACTOR
        self.night = night_factor or NIGHT_FACTOR
        self.holiday = bool(holiday)

    @staticmethod
    def _in(minute: int, start: int, end: int) -> bool:
        return start <= minute < end

    def factor(self, minute_of_day: int, mode: TransitMode) -> float:
        """返回该时刻该方式的时长放大系数（节假日会再叠一层拥堵加成）"""
        m = minute_of_day % (24 * 60)
        base = FLAT_FACTOR.get(mode.value, 1.0)
        for start, end, _label in PEAK_WINDOWS:
            if self._in(m, start, end):
                base = self.peak.get(mode.value, 1.0)
                break
        else:
            start, end = NIGHT_RANGE
            if m >= start or m < end:            # 跨零点
                base = self.night.get(mode.value, 1.0)
        if not self.holiday:
            return base
        # 节假日：高峰段用 HOLIDAY_FACTOR、其余时段用 HOLIDAY_FLAT
        in_peak = any(self._in(m, s, e) for s, e, _n in PEAK_WINDOWS)
        extra = (self.HOLIDAY_FACTOR if in_peak else self.HOLIDAY_FLAT)
        return base * extra.get(mode.value, 1.0)

    def label(self, minute_of_day: int, mode: TransitMode) -> str:
        m = minute_of_day % (24 * 60)
        for start, end, name in PEAK_WINDOWS:
            if self._in(m, start, end):
                f = self.peak.get(mode.value, 1.0)
                return f'{name}（×{f:g}）' if f != 1.0 else name
        start, end = NIGHT_RANGE
        if m >= start or m < end:
            return '夜间（路况好）'
        return '平峰'

    def predict(self, base_minutes: int, minute_of_day: int,
                mode: TransitMode) -> int:
        """按出发时刻预测通行分钟数"""
        if base_minutes <= 0:
            return 0
        return max(1, int(round(base_minutes * self.factor(minute_of_day, mode))))


@dataclass
class DepartureOption:
    """一个候选出发时刻"""
    depart_min: int
    minutes: int
    arrive_min: int
    wait_min: int          # 比目标开始时刻早到多久
    factor_label: str

    @property
    def depart_label(self) -> str:
        return hhmm(self.depart_min)

    @property
    def arrive_label(self) -> str:
        return hhmm(self.arrive_min)


@dataclass
class DepartureAdvice:
    """出发时刻建议结果"""
    ok: bool = False
    task_title: str = ''
    mode_cn: str = ''
    target_label: str = ''          # 目标开始时刻 HH:MM
    base_minutes: int = 0
    latest: DepartureOption | None = None
    best_offpeak: DepartureOption | None = None
    options: list[DepartureOption] = None
    message: str = ''
    caveat: str = ('时段系数为本地经验值（南京早晚高峰 ×1.35 等），'
                   '非高德实时预测；基础时长来自高德或本地缓存')

    def summary(self) -> str:
        if not self.ok:
            return self.message
        parts = []
        if self.latest:
            parts.append(
                f'最晚 {self.latest.depart_label} 出发（{self.latest.minutes} 分钟，'
                f'{self.latest.factor_label}，{self.target_label} 前到）')
        if self.best_offpeak:
            b = self.best_offpeak
            parts.append(
                f'想少堵可 {b.depart_label} 走（{b.minutes} 分钟，'
                f'但会早到 {b.wait_min} 分钟）')
        return '；'.join(parts) if parts else self.message


# ============================ 公交班次推算 ============================
# 说明（重要）：这里**不是实时公交数据**。高德 Web 服务只给线路名称与首末班时间，
# 不提供班次时刻表；实时到站属实时公交数据，需官方开放接口（GTFS-RT / 交通局平台）
# 或商业 API。本模块按"线路类型 → 经验发车间隔"推算下一班与期望等待，
# 并全程标注为估算，绝不冒充实时数据。

HEADWAY_BY_KIND = {
    # 线路类型关键词 -> (最小间隔分钟, 最大间隔分钟)
    '地铁': (3, 6),
    '公交': (8, 15),
}
DEFAULT_HEADWAY = (10, 20)


def _parse_hhmm(value: str) -> int | None:
    """解析高德返回的运营时间，兼容 '0600'、'06:00'、'6:00' 三种写法"""
    if not value:
        return None
    s = str(value).strip()
    if ':' in s:
        hh, _, mm = s.partition(':')
    elif len(s) == 4 and s.isdigit():
        hh, mm = s[:2], s[2:]
    elif len(s) == 3 and s.isdigit():
        hh, mm = s[:1], s[1:]
    else:
        return None
    try:
        hour, minute = int(hh), int(mm)
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def guess_headway(line_name: str) -> tuple[int, int]:
    """按线路名称猜经验发车间隔区间（分钟）

    高德返回的线路名形如"地铁2号线(经天路--鱼嘴)""34路(中山陵停车场--中山码头)"，
    因此除了"地铁/公交"关键词，还要按"号线"和"N路"识别。
    """
    name = line_name or ''
    if '地铁' in name or '号线' in name:
        return HEADWAY_BY_KIND['地铁']
    if '公交' in name or re.search(r'\d+\s*路', name):
        return HEADWAY_BY_KIND['公交']
    return DEFAULT_HEADWAY


def _avg_headway(headway_range: tuple[int, int]) -> int:
    """区间取平均（四舍五入到整数分钟）"""
    lo, hi = headway_range
    return max(1, int((lo + hi) / 2 + 0.5))


def expected_wait(headway_range: tuple[int, int]) -> int:
    """均匀到达假设下的期望等待分钟数（约半个间隔）"""
    return max(1, _avg_headway(headway_range) // 2)


def next_bus(now_min: int, start_min: int | None, end_min: int | None,
             headway_range: tuple[int, int]) -> tuple[int | None, str]:
    """推算下一班车时刻，返回 (分钟数或 None, 说明)"""
    lo, hi = headway_range
    if start_min is None or end_min is None:
        return None, f'运营时间未知，按平均间隔 {lo}–{hi} 分钟推算'
    if now_min < start_min:
        return start_min, f'首班 {hhmm(start_min)}'
    if now_min > end_min:
        return None, f'末班 {hhmm(end_min)} 已过'
    avg = _avg_headway(headway_range)
    offset = (now_min - start_min) % avg
    wait = 0 if offset == 0 else avg - offset
    return now_min + wait, f'按平均 {avg} 分钟一班推算'


def annotate_plans(plans: list[dict], now_min: int) -> list[dict]:
    """给公交方案补上"推算班次"信息（就地修改并返回）。

    每条线路新增：headway_range / expected_wait_min / next_bus_min / estimate_note
    每个方案新增：estimate_wait_min（各段期望等待之和）
    """
    for p in plans:
        total_wait = 0
        for line in p.get('lines', []):
            rng = guess_headway(line.get('name', ''))
            start_min = _parse_hhmm(line.get('start_time', ''))
            end_min = _parse_hhmm(line.get('end_time', ''))
            bus_min, note = next_bus(now_min, start_min, end_min, rng)
            wait = 0 if bus_min is None else max(0, bus_min - now_min)
            line['headway_range'] = rng
            line['expected_wait_min'] = expected_wait(rng)
            line['next_bus_min'] = bus_min
            line['next_bus_label'] = hhmm(bus_min) if bus_min is not None else '—'
            line['estimate_note'] = note
            total_wait += wait if bus_min is not None else 0
        p['estimate_wait_min'] = total_wait
        p['estimate_total_min'] = p.get('duration_min', 0) + total_wait
    return plans
