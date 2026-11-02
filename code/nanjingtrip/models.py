# -*- coding: utf-8 -*-
"""领域模型：行程、事项、地点、通行段、排程结果等实体与值对象。

本模块不依赖界面、数据库或网络，可被单元测试直接引用（见第 4 章测试）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from enum import Enum
from typing import Optional

from .cities import DEFAULT_CITY, get_city

# 南京市行政区（保留旧常量名以兼容既有引用；真正的数据源是城市包）
NANJING_DISTRICTS = get_city('南京').districts

# 地点类别（用于"吃住行玩"的归类与界面着色）
KIND_ATTRACTION = '景点'
KIND_VENUE = '场馆'
KIND_PARK = '公园'
KIND_NEIGHBORHOOD = '街区'
KIND_MALL = '商圈'
KIND_FOOD = '餐饮'
KIND_LODGING = '住宿'
KIND_TRANSPORT = '交通'
KIND_MEDICAL = '医疗'
KIND_CAMPUS = '校园'
KIND_BOOKSTORE = '书店'

POI_KINDS = (KIND_ATTRACTION, KIND_VENUE, KIND_PARK, KIND_NEIGHBORHOOD,
             KIND_MALL, KIND_FOOD, KIND_LODGING, KIND_TRANSPORT,
             KIND_MEDICAL, KIND_CAMPUS, KIND_BOOKSTORE)



class TaskStatus(str, Enum):
    """事项状态（与状态图一致，冲突为运行时派生状态）"""
    SCHEDULED = 'scheduled'
    UNSCHEDULED = 'unscheduled'
    DONE = 'done'


class TransitMode(str, Enum):
    """交通方式"""
    DRIVING = 'driving'
    TRANSIT = 'transit'
    WALKING = 'walking'
    BICYCLING = 'bicycling'

    @property
    def cn(self) -> str:
        return {'driving': '驾车', 'transit': '公交/地铁',
                'walking': '步行', 'bicycling': '骑行'}[self.value]


class TaskCategory(str, Enum):
    """事项类别（用于界面着色与统计）"""
    MEDICAL = '医疗'
    FOOD = '餐饮'
    EXHIBITION = '展览'
    ERRAND = '办事'
    OTHER = '其他'


#: 这些类别默认算"室内"（雨天优先室内排序用）；其余（公园/景点/街区/校园/交通）算室外
_INDOOR_KINDS = ('场馆', '商圈', '书店', '餐饮', '住宿', '展览')


def minutes_of(t: time) -> int:
    """time -> 当日分钟数"""
    return t.hour * 60 + t.minute

def clock(m: int) -> time:
    """当日分钟数 -> time（自动裁剪到 00:00~23:59）"""
    m = max(0, min(23 * 60 + 59, int(m)))
    return time(m // 60, m % 60)


def hhmm(m: int) -> str:
    return clock(m).strftime('%H:%M')


def same_place(a: Optional['Poi'], b: Optional['Poi']) -> bool:
    """两个地点是不是同一处。

    判定口径（宁严不宽）：
      * 两条都取自数据库/城市包时，比 `poi_id`；
      * 任一没有 id（在线检索来的）时，要求**名称一致且坐标一致**；
    不用"坐标相近"来判同点——商圈里的两家店、景区里的两个馆常共用近似坐标，
    按距离判会把它们合并，反而吃掉本应存在的通行时间。
    """
    if a is None or b is None:
        return a is b
    if a.poi_id is not None and b.poi_id is not None:
        return a.poi_id == b.poi_id
    return bool(a.name) and a.name == b.name and (a.lng, a.lat) == (b.lng, b.lat)


def hm_text(minutes, unit_only: bool = False) -> str:
    """分钟数 → 人读得懂的时长文本。

    界面上写"300 分"是自找的减分项：没有人会把 300 分换算成 5 小时。
    规则：整小时写"N 小时"；不足 1 小时写"N 分钟"；其余写"N 小时 M 分钟"。

    :param unit_only: 为 True 时把"分钟"缩成"分"，用于紧凑的多方式对比矩阵
    """
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return '—'
    if m <= 0:
        return '0 分' if unit_only else '0 分钟'
    h, mi = divmod(m, 60)
    if h and not mi:
        return f'{h} 小时'
    if h:
        return f'{h} 小时 {mi} 分' if unit_only else f'{h} 小时 {mi} 分钟'
    return f'{mi} 分' if unit_only else f'{mi} 分钟'



@dataclass
class Poi:
    """地点（值对象）：实现 BR-01"地点必须属于当前城市"。

    除坐标与行政区外，还承载**营业信息**（开放时段、闭馆日、预约要求、
    人均价位），使排程能提示"周一闭馆""17:00 关门""需提前预约"，
    而不必把这类规则写进排程引擎。营业信息随城市包加载，不落数据库。
    """
    name: str = ''
    address: str = ''
    district: str = ''
    lng: float = 0.0
    lat: float = 0.0
    provider: str = 'mock'
    provider_poi_id: str = ''
    poi_id: Optional[int] = None
    # ---- 城市与类别（"玩/吃/住/行"归类） ----
    city: str = ''
    kind: str = ''
    # ---- 营业信息 ----
    open_from: str = ''
    open_to: str = ''
    closed_days: tuple = ()
    book_hint: str = ''
    visit_min: int = 0
    price: str = ''
    cuisine: str = ''
    star: str = ''
    checkin: str = ''
    checkout: str = ''
    #: 是否主要在室内（雨天优先室内排序用）。由 kind 推导，城市包可显式覆盖。
    indoor: bool = False

    # ---------- 构造 ----------
    @classmethod
    def from_pack(cls, data: dict, city: str = '') -> 'Poi':
        """由城市包中的 POI 字典构造值对象"""
        d = dict(data or {})
        op = d.get('open') or ()
        kind = d.get('kind', '')
        return cls(
            name=d.get('name', ''), address=d.get('address', ''),
            district=d.get('district', ''),
            lng=float(d.get('lng') or 0), lat=float(d.get('lat') or 0),
            provider=d.get('provider') or 'mock',
            provider_poi_id=d.get('name', ''),
            city=city or d.get('city', ''),
            kind=d.get('kind', ''),
            open_from=op[0] if len(op) > 0 else '',            open_to=op[1] if len(op) > 1 else '',
            closed_days=tuple(d.get('closed') or ()),
            book_hint=d.get('book', ''),
            visit_min=int(d.get('visit') or 0),
            price=d.get('price', ''),
            cuisine=d.get('cuisine', ''),
            star=d.get('star', ''),
            checkin=d.get('checkin', ''),
            checkout=d.get('checkout', ''),
            # 显式标了就用显式值；否则按类别推导（场馆/商圈/书店/餐饮/住宿=室内）
            indoor=(bool(d['indoor']) if 'indoor' in d
                    else kind in _INDOOR_KINDS))

    # ---------- 归属校验 ----------
    def in_city(self, city: str = '') -> bool:
        """是否属于指定城市（未指定则用本对象自带城市，再退回默认城市）"""
        name = (city or self.city or DEFAULT_CITY).strip()
        if self.city and name and self.city != name:
            return False
        return get_city(name).in_city(self.district)

    def in_nanjing(self) -> bool:      # 兼容旧调用
        return self.in_city('南京')

    # ---------- 类别判断 ----------
    @property
    def is_lodging(self) -> bool:
        return self.kind == KIND_LODGING

    @property
    def is_food(self) -> bool:
        return self.kind == KIND_FOOD

    @property
    def is_transport(self) -> bool:
        return self.kind == KIND_TRANSPORT

    # ---------- 展示 ----------
    def label(self) -> str:
        return f'{self.name}' if not self.district else f'{self.name}（{self.district}）'

    def hours_label(self) -> str:
        if self.checkin and self.checkout:
            return f'入住 {self.checkin} / 退房 {self.checkout}'
        if self.open_from and self.open_to:
            return f'{self.open_from}-{self.open_to}'
        return '全天' if self.kind == KIND_TRANSPORT else ''

    def closed_label(self) -> str:
        return ''.join(self.closed_days) + '闭馆' if self.closed_days else ''

    @property
    def price_num(self) -> Optional[int]:
        """从 '人均约 ¥90' 这类文案里取人均价（元）；取不到返回 None"""
        import re as _re
        m = _re.search(r'(\d{1,5})', self.price or '')
        return int(m.group(1)) if m else None

    def brief(self) -> str:
        """一行摘要：类别 + 菜系/星级 + 营业 + 预约 + 价位（供界面与提示复用）"""
        bits = [self.kind]
        for extra in (self.cuisine, self.star):
            if extra:
                bits.append(extra)
        hours = self.hours_label()
        if hours:
            bits.append(hours)
        if self.closed_days:
            bits.append(self.closed_label())
        if self.book_hint:
            bits.append(self.book_hint)
        if self.price:
            bits.append(self.price)
        return '｜'.join(b for b in bits if b)



@dataclass
class TransitLeg:
    """通行段（值对象）：两地点之间的一次通行"""
    from_poi_id: Optional[int]
    to_poi_id: Optional[int]
    mode: TransitMode
    duration_min: int
    distance_m: int = 0
    source: str = 'mock'

    def label(self) -> str:
        return f'{self.mode.cn} {self.duration_min} min'


@dataclass
class Suggestion:
    """冲突建议：patch 为可直接应用的调整"""
    kind: str
    text: str
    patch: dict = field(default_factory=dict)


@dataclass
class Conflict:
    """冲突描述（BR-05：冲突不得静默）"""
    kind: str                     # locked_overlap | no_slot | poi_missing | out_of_hours …
    detail: str
    overlap_min: int = 0
    task_title: str = ''
    with_title: str = ''
    #: 细分方向（如 out_of_hours 的 early/late）：建议文案要按方向给，不能一律"提前"
    hint: str = ''
    suggestions: list[Suggestion] = field(default_factory=list)


@dataclass
class Task:
    """事项实体"""
    title: str
    duration_min: int
    task_id: Optional[int] = None
    trip_id: Optional[int] = None
    category: str = TaskCategory.OTHER.value
    desired_start: Optional[time] = None
    poi: Optional[Poi] = None
    raw_text: str = ''
    is_locked: bool = False
    status: TaskStatus = TaskStatus.UNSCHEDULED
    seq: int = 0
    start_ts: Optional[time] = None
    end_ts: Optional[time] = None
    # 本段（从上一项到这里）的交通方式；None 表示跟随全局默认
    mode: Optional[TransitMode] = None
    #: 绝对截止：必须在此时刻**之前抵达**（"10:30 前必须到鸡鸣寺"）。
    #: 与 desired_start（锁定开始时刻）不同——deadline 允许更早到，只禁止更晚。
    deadline: Optional[time] = None

    @property
    def poi_id(self) -> Optional[int]:
        return self.poi.poi_id if self.poi else None

    def occupy_range(self) -> tuple[int, int]:
        """返回当日分钟数区间 [start, end)"""
        if self.start_ts is None:
            return (0, 0)
        s = minutes_of(self.start_ts)
        return (s, s + self.duration_min)


@dataclass
class TripVersion:
    """行程版本（BR-07 可回滚）"""
    version_id: Optional[int]
    trip_id: Optional[int]
    created_at: str
    note: str
    snapshot_json: str


@dataclass
class Trip:
    """行程聚合根：以"日"为单位（同时属于一座城市）"""
    plan_date: date
    trip_id: Optional[int] = None
    title: str = ''
    tasks: list[Task] = field(default_factory=list)
    city: str = DEFAULT_CITY
    # 当日"住"的锚点：酒店作为出门起点与回程终点（None 表示不设锚点）
    lodging: Optional[Poi] = None
    # 用户已明确"当天往返、不需要住宿"，避免反复推荐
    lodging_declined: bool = False

    def add_task(self, t: Task) -> None:
        t.trip_id = self.trip_id
        self.tasks.append(t)

    def remove_task(self, task_id: int) -> None:
        self.tasks = [t for t in self.tasks if t.task_id != task_id]

    def scheduled(self) -> list[Task]:
        return [t for t in self.tasks if t.status == TaskStatus.SCHEDULED]


@dataclass
class ScheduledItem:
    """排程输出的一项：时段 + 前置通行段"""
    task: Task
    start_min: int
    end_min: int
    leg_before: Optional[TransitLeg] = None

    @property
    def start_ts(self) -> time:
        return clock(self.start_min)

    @property
    def end_ts(self) -> time:
        return clock(self.end_min)

    def span_label(self) -> str:
        return f'{hhmm(self.start_min)} - {hhmm(self.end_min)}'


@dataclass
class ScheduleOptions:
    """排程选项（对应 app_setting 配置）"""
    mode: TransitMode = TransitMode.DRIVING
    buffer_min: int = 10
    day_start: time = time(8, 0)
    day_end: time = time(22, 0)
    # 住宿锚点：设置后，首项之前要留出"酒店→首项"的通行，末项之后要留出"回酒店"的通行
    lodging: Optional[Poi] = None
    # 是否启用"午餐/晚餐窗口"提示（默认开启；纯数据驱动的软提示，不阻断排程）
    meal_notice: bool = True
    #: 是否按用户给出的顺序依次排（True 时不再"按耗时降序 + 最优空档"重排）
    keep_order: bool = False
    #: 是否每段自动选最快通行方式（True 时逐段择优，而非全用全局默认方式）
    auto_mode: bool = False
    #: 是否自动排最短路（最近邻 + 2-opt；与 keep_order 互斥，keep_order 优先）
    optimize_route: bool = False
    #: 最晚必须回到起点（住宿锚点）的时刻（分钟数）；None 表示不限制。
    #: 用户说"17:30 前必须回到酒店"时生效：超出给提示，不悄悄放行。
    return_by: Optional[int] = None
    #: 是否让"雨天优先室内"参与排序（配合天气提示使用）
    prefer_indoor: bool = False
    #: 时段路况模型（提供 factor(minute, mode) -> 放大系数）。
    #: None 表示不做高峰加成——离线估算没有真实时效，叠加系数没有意义。
    peak_model: Optional[object] = None

    def start_min(self) -> int:
        return minutes_of(self.day_start)

    def end_min(self) -> int:
        return minutes_of(self.day_end)



@dataclass
class ScheduleResult:
    """排程结果（值对象）"""
    items: list[ScheduledItem] = field(default_factory=list)
    unscheduled: list[Task] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    explanation: str = ''
    # 非阻断提示（营业时间 / 闭馆日 / 预约 / 餐饮窗口）：排得下但需要用户留意，
    # 与 conflicts 分开，避免把"提醒"误当成"排不下"而影响可行性判断
    notices: list[Conflict] = field(default_factory=list)
    # 住宿锚点的回程段（末项 → 酒店），未设锚点时为 None
    return_leg: Optional[TransitLeg] = None

    def is_feasible(self) -> bool:
        return not self.conflicts and not self.unscheduled

    def ordered(self) -> list[ScheduledItem]:
        return sorted(self.items, key=lambda i: i.start_min)



@dataclass
class ParsedIntent:
    """自然语言解析结果"""
    date: Optional[date] = None
    start_hint: Optional[time] = None
    mention: str = ''
    item: str = ''
    duration_min: int = 0
    mode_hint: Optional[TransitMode] = None
    category: str = TaskCategory.OTHER.value
    confidence: float = 0.0
    missing: list[str] = field(default_factory=list)
    raw_text: str = ''
    #: 解析来源：'llm'（大模型）/ 'rule'（大模型不可用时的规则兜底）
    #: —— 界面要能说清这一条是谁解析的，不能降级了还标"在线"
    source: str = ''


@dataclass
class PlanOutcome:
    """用例返回对象（供界面直接消费）"""
    ok: bool = False
    trip: Optional[Trip] = None
    result: Optional[ScheduleResult] = None
    explanation: str = ''
    error: str = ''
    missing: list[str] = field(default_factory=list)
    version_id: Optional[int] = None
    elapsed_ms: int = 0
    intent: Optional['ParsedIntent'] = None
    draft: Optional[Task] = None
    candidates: list = field(default_factory=list)
    # 营业时间/闭馆日/预约等非阻断提示（预览阶段即可看到，提交前拦住"周一去故宫"）
    notices: list = field(default_factory=list)
    # 识别为"住宿锚点"时填写（此时不产生事项，只设定出发地与回程终点）
    lodging: Optional[Poi] = None



@dataclass
class ChatReply:
    """助手问答的返回对象（桌面助手能力）"""
    text: str = ''
    source: str = 'offline'          # online | offline | error
    elapsed_ms: int = 0
    ok: bool = True
