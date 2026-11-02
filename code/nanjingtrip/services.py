# -*- coding: utf-8 -*-
"""应用服务层：用例编排、事务边界与降级决策（第 2 章图 2.2 对应实现）。"""
from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
import time as _time
from datetime import date, datetime, timedelta
from typing import Optional

from . import engine as engine_mod
from . import geo as geo_mod
from . import hours as hours_mod
from . import holidays as holidays_mod
from . import lodging as lodging_mod
from . import traffic as traffic_mod
from . import weather as weather_mod
from .advisor import advise_notices, attach_suggestions
from .adapters.mock import MockLLMAdapter, MockMapAdapter
from .adapters.amap import AmapAdapter, MapError
from .adapters.llm_openai import OpenAICompatLLMAdapter, SYSTEM_PROMPT
from .cities import DEFAULT_CITY, get_city, normalize_city, place_qualifiers
from .config import Settings, SettingsService
from .models import (ChatReply, Conflict, ParsedIntent, Poi, PlanOutcome,
                     ScheduleOptions, ScheduleResult, Suggestion, Task,
                     TaskCategory, TaskStatus, TransitMode, Trip, hhmm,
                     hm_text, minutes_of, same_place)
from .parsing_rules import rule_based_parse
from .storage import (Database, SqliteTransitCache, SqliteTripRepository,
                      SqliteVersionRepository)

log = logging.getLogger('nanjingtrip')

INTENT_SCHEMA = {
    'date': 'YYYY-MM-DD | null', 'start_hint': 'HH:MM | null',
    'mention': 'string', 'item': 'string', 'duration_min': 'int',
    'mode_hint': 'driving|transit|walking|null', 'category': '医疗/餐饮/展览/办事/其他',
    'confidence': 'float', 'missing': 'string[]',
}

#: 大模型有时把事项名只写成纯动词（"游览" / "停留"），行程行就看不出要去哪儿。
#: 这里只补这一类"不带任何内容"的动词；像"看展""吃饭""取报告"这类本身有信息量
#: 的活动名一律原样保留（地点在界面上单独一列显示）。
_BARE_ITEMS = {
    '游览': '游览{name}', '参观': '参观{name}', '游玩': '游览{name}',
    '逛': '游览{name}', '停留': '游览{name}',
}

#: 大模型把"出发点"误当成事项时的措辞（见 preview_text 第 3.5 步）
_ORIGIN_ITEMS = ('出行起点', '出发', '起点', '出发地', '从起点出发')

#: "只占时间、不指定地点"的时间块用词（午休/吃饭）。
#: 有地点时（"在新街口吃饭"）不会走到这条分支，仍按普通事项处理。
_BLOCK_WORDS = ('午饭', '午餐', '中饭', '晚饭', '晚餐', '吃饭', '吃个饭', '吃顿饭',
                '吃点东西', '吃点', '随便吃', '吃个午饭', '吃个晚饭', '用餐', '午休')


def is_time_block_text(text: str) -> bool:
    """这句话是不是"只占一段时间"的块（午休/吃饭），而不是某个地点上的事项"""
    return any(w in (text or '') for w in _BLOCK_WORDS)


def _deadline_of(text: str):
    """从"10:30 前必须抵达"这类表述里取绝对截止时刻（time），没有则 None"""
    from datetime import time as _t
    from .parsing_rules import parse_deadline
    hm = parse_deadline(text or '')
    return _t(hm[0], hm[1]) if hm else None


def _parse_hm(text):
    """'HH:MM' -> time；空或非法返回 None"""
    from datetime import time as _t
    raw = str(text or '').strip()
    if not raw:
        return None
    try:
        hh, mm = raw.split(':')[:2]
        return _t(int(hh), int(mm))
    except (ValueError, TypeError):
        return None


#: "（停留）""(停留50分钟)" 这类只说明停留、没有地点信息的空壳事项名
_BARE_WRAPPER = re.compile(r'^[（(]\s*停留[^）)]*[）)]$')


def title_with_poi(item: str, poi) -> str:
    """事项名只有裸动词或缺名时补上地名；已有地名或本就是自定义事项则原样返回。"""
    core = (item or '').strip()
    if poi is None:
        return core
    if core in _BARE_ITEMS:
        return _BARE_ITEMS[core].format(name=poi.name)
    # 规则解析抽不出事项名时会回退成"未命名事项"/"（停留）"这类空壳，
    # 此时用地点名兜底，别让行程行出现一条看不出干嘛的条目。
    if core in ('', '未命名事项') or _BARE_WRAPPER.match(core):
        return f'在{poi.name}用餐' if poi.is_food else f'游览{poi.name}'
    return core



# ============================ 降级控制 ============================
class DegradeController:
    """在线/离线模式单点开关（FR-13）。

    **两路外部服务独立降级**：大模型与地图各自判断可用性，
    因此"只有高德 Key、没有大模型 Key"时仍可用真实通行时长 + 离线规则解析，
    反之亦然；两路都不可用时才是完全离线。
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._llm = None
        self._map = None
        self.refresh(settings)

    def refresh(self, settings: Settings) -> None:
        self.settings = settings
        city = self.city()
        self._llm = (OpenAICompatLLMAdapter(settings.llm_base_url,
                                            settings.llm_api_key,
                                            settings.llm_model, city=city,
                                            reasoning_effort=getattr(
                                                settings, 'llm_reasoning_effort', ''))
                     if settings.llm_ready else MockLLMAdapter(city=city))
        self._map = (AmapAdapter(settings.map_api_key, city=city)
                     if settings.map_ready else MockMapAdapter(city=city))


    # ---------- 当前城市（BR-01 的作用域） ----------
    def city(self) -> str:
        """当前城市：来自配置，非法值回落到默认城市"""
        return normalize_city(getattr(self.settings, 'city', '') or DEFAULT_CITY)

    # ---------- 可用性 ----------
    def llm_ready(self) -> bool:
        return self.settings.llm_ready

    def map_ready(self) -> bool:
        return self.settings.map_ready

    # ---------- 运行时降级（Key 在 ≠ 真的调得通） ----------
    #: 最近一次大模型调用的失败原因；空串表示最近一次调用成功。
    #: 光看 Key 是否配置是不够的：模型改名、欠费、限流都会让调用失败，
    #: 而这时解析已经悄悄退到规则兜底，界面不能还标"在线"。
    llm_last_error: str = ''

    def note_llm_error(self, message: str) -> None:
        self.llm_last_error = (message or '调用失败')[:200]

    def clear_llm_error(self) -> None:
        self.llm_last_error = ''

    def llm_ok(self) -> bool:
        """最近一次大模型调用是否成功（没调用过也算可用）"""
        return not self.llm_last_error

    def is_offline(self) -> bool:
        """两路都不可用才算完全离线"""
        return not (self.llm_ready() or self.map_ready())

    def mode_label(self) -> str:
        city = self.city()
        if self.is_offline():
            return f'离线模式（{city}内置 POI + 规则解析）'
        if self.llm_ready() and self.map_ready():
            return f'在线（LLM：{self._llm.name()} / 地图：{self._map.name()}）'
        if self.map_ready():
            return f'半在线（LLM：离线规则解析 / 地图：{self._map.name()}）'
        return f'半在线（LLM：{self._llm.name()} / 地图：{city}离线内置 POI）'

    def llm(self):
        return self._llm

    def map(self):
        return self._map


# ============================ 解析服务 ============================
class IntentParseService:
    """自然语言 → ParsedIntent（UC-02），带规则兜底与日期交叉校验"""

    WEEK_CN = ('一', '二', '三', '四', '五', '六', '日')

    def __init__(self, degrade: DegradeController):
        self.degrade = degrade

    def parse(self, text: str, today: date) -> ParsedIntent:
        data: Optional[dict] = None
        city = self.degrade.city()
        # 关键：把"今天是几号"写进系统提示，否则模型不知道怎么换算"明天""下周一"
        system = (SYSTEM_PROMPT
                  + f'\n本系统当前城市为{city}，mention 必须是该城市内的地点；'
                  + f'\n今天的日期是 {today.isoformat()}'
                    f'（星期{self.WEEK_CN[today.weekday()]}，时区 Asia/Shanghai）。'
                    '请据此把"今天/明天/后天/下周X/周X"等相对日期换算成绝对日期；'
                    '无法确定时 date 输出 null，不要猜测。')
        try:
            data = self.degrade.llm().complete_json(system, text, INTENT_SCHEMA)
            self.degrade.clear_llm_error()
        except Exception as exc:      # noqa: BLE001 任何失败都走规则兜底
            log.warning('LLM 解析失败，改用规则兜底：%s', exc)
            self.degrade.note_llm_error(str(exc))
            data = None

        rule = rule_based_parse(text, today, [], {})
        intent = self._from_dict(data, text) if data else None
        if intent is None or intent.duration_min <= 0:
            if intent is None:
                intent = self._from_dict(rule, text) or ParsedIntent(raw_text=text)
            else:
                # 合并：保留 LLM 的地点，补全耗时
                if intent.duration_min <= 0 and rule.get('duration_min'):
                    intent.duration_min = int(rule['duration_min'])
                if not intent.mention and rule.get('mention'):
                    intent.mention = rule['mention']

        # 日期交叉校验：中文相对日期用规则算得更准，规则算出就以规则为准
        if rule.get('date'):
            rule_date = date.fromisoformat(rule['date'])
            if intent.date != rule_date:
                if intent.date is not None:
                    log.info('日期以规则为准：模型给 %s，规则算 %s',
                             intent.date, rule_date)
                intent.date = rule_date
        elif intent.date is None or intent.date < today:
            # 模型给不出日期或给了过去的日期 → 视为当天
            intent.date = today
        if intent.start_hint is None and rule.get('start_hint'):
            from datetime import time as _t
            h, m = str(rule['start_hint']).split(':')
            intent.start_hint = _t(int(h), int(m))

        # 必填项只由本地规则判定，不采信模型自报的 missing
        # （模型常把"交通方式"这类可选项也算成缺失，会把整单误判为失败）
        if intent.missing:
            log.info('忽略模型自报的缺失项：%s（必填项由本地规则判定）',
                     intent.missing)
        intent.missing = []
        if intent.duration_min <= 0:
            intent.missing.append('耗时')
        if not intent.mention:
            intent.missing.append('地点')
        # 记下这一条是谁解析的：界面据此说明"模型解析"还是"规则兜底"
        intent.source = 'llm' if data is not None else 'rule'
        return intent

    @staticmethod
    def _from_dict(data: Optional[dict], text: str) -> Optional[ParsedIntent]:
        if not isinstance(data, dict):
            return None
        try:
            d = None
            if data.get('date'):
                d = date.fromisoformat(str(data['date'])[:10])
            start = None
            sh = data.get('start_hint')
            if sh and re.match(r'^\d{1,2}:\d{2}$', str(sh)):
                h, m = str(sh).split(':')
                from datetime import time as _t
                start = _t(int(h), int(m))
            mode = data.get('mode_hint')
            mode_enum = TransitMode(mode) if mode in ('driving', 'transit', 'walking') else None
            cat = data.get('category') or '其他'
            if cat not in [c.value for c in TaskCategory]:
                cat = '其他'
            return ParsedIntent(
                date=d, start_hint=start, mention=(data.get('mention') or '').strip(),
                item=(data.get('item') or '').strip() or '未命名事项',
                duration_min=int(data.get('duration_min') or 0),
                mode_hint=mode_enum, category=cat,
                confidence=float(data.get('confidence') or 0.0),
                missing=list(data.get('missing') or []), raw_text=text)
        except Exception:      # noqa: BLE001
            return None


# ============================ 地点服务 ============================

#: 地址式输入的特征：门牌号 + 路/街/道/巷/弄，或"区+路"
_ADDRESS_HINT = re.compile(r'(\d+\s*号|路|街|道|巷|弄|大道|大街)')
_ADDRESS_NUM = re.compile(r'\d+\s*号')


def looks_like_address(text: str) -> bool:
    """是不是"某某路 12 号"这种地址（而不是地名）"""
    s = (text or '').strip()
    if not s or len(s) < 6:
        return False
    return bool(_ADDRESS_NUM.search(s) and _ADDRESS_HINT.search(s))


def _lcs(a: str, b: str) -> int:
    """最长公共子串长度"""
    best = 0
    for i in range(len(a)):
        for j in range(i + best + 1, len(a) + 1):
            if a[i:j] in b:
                best = j - i
            else:
                break
    return best


#: 通用后缀：共用这些词不构成"同一个地方"，主体必须也对得上
GENERIC_TAILS = ('博物院', '博物馆', '纪念馆', '美术馆', '图书馆', '公园', '植物园',
                 '动物园', '广场', '购物中心', '商业街', '街区', '大学', '学院',
                 '校区', '地铁站', '车站', '机场', '火车站', '景区', '风景区',
                 '陵', '寺', '塔', '楼', '园', '门', '湖', '山')


def name_head(name: str) -> str:
    """去掉括号分店与通用后缀，留下"主体"：南京博物院 → 南京"""
    text = re.sub(r'[（(][^）)]*[）)]', '', str(name or '')).strip()
    changed = True
    while changed:
        changed = False
        for tail in GENERIC_TAILS:
            if text.endswith(tail) and len(text) > len(tail):
                text = text[: -len(tail)]
                changed = True
    return text


def normalize_mention(mention: str) -> str:
    """规范化用户说法：去括号内容与"地铁站/公交站/火车站/车站"后缀（"三山街地铁站" → "三山街"）

    为什么**不许**去掉单独的"站"：包内站名写成「南京站(地铁站)」，去括号后是「南京站」，
    再剥一个"站"就退化成「南京」——于是"火星基地南京分基地"里含「南京」就被认成南京站
    （真实回归事故：垃圾地名被凑成火车站）。多字后缀才剥。
    """
    text = re.sub(r'[（(][^）)]*[）)]', '', str(mention or '')).strip()
    return re.sub(r'(地铁站|公交站|火车站|车站)$', '', text).strip()


def _qualifiers() -> frozenset:
    """地名限定语集合（懒加载一次，城市包注册表变化时会失效重算）"""
    global _QUALIFIER_CACHE
    if _QUALIFIER_CACHE is None:
        _QUALIFIER_CACHE = place_qualifiers()
    return _QUALIFIER_CACHE


_QUALIFIER_CACHE: Optional[frozenset] = None


def name_alike(mention: str, name: str) -> bool:
    """用户说的名字与该 POI 名是否**真的**是同一个地方。

    拿高德模糊命中直接用会出事：真实模拟里「火星基地南京分基地」被凑成「南京基地」、
    「玄武区珠江路653号」被凑成「南京玄武外国语学校」。规则（按"主体"比，主体=去括号分店与通用后缀）：
      · 主体相同 → 认（新街口 ≈ 新街口(地铁站)）；
      · 一方是另一方的前缀 → 认，但多出来的部分不能超过 6 个字；
      · 一方是另一方的后缀，且多出来的只是地名限定语 → 认
        （南京晨光1865创意产业园 ≈ 晨光1865创意产业园，河西金鹰世界 ≈ 金鹰世界）；
      · 主体比不出来时，退回包含关系与最长公共子串。
    """
    a, b = (mention or '').strip(), (name or '').strip()
    if not a or not b:
        return False
    if a == b:
        return True
    # 先比"主体"：新街口 ≈ 新街口(地铁站)、明故宫 ≈ 南京明故宫遗址公园
    head_a, head_b = name_head(a), name_head(b)
    if head_a and head_b:
        if head_a == head_b:
            return True
        # 前缀关系：南京邮电 ⊂ 南京邮电大学仙林（用户省了后缀）。
        # 多出来的部分必须短——多一个字往往就换了所指。
        if head_a.startswith(head_b) or head_b.startswith(head_a):
            extra = max(len(head_a), len(head_b)) - min(len(head_a), len(head_b))
            return extra <= 6
        # 后缀关系：多出来的是**地名限定语**（"南京晨光1865创意产业园" ⊃ "晨光1865创意产业园"、
        # "河西金鹰世界" ⊃ "金鹰世界"）。限定语必须是城市/行政区/片区名，
        # 否则"故宫"会被"明故宫"的后缀关系接住（TC-25-06 守死）。
        for longer, shorter in ((head_a, head_b), (head_b, head_a)):
            extra = longer[: len(longer) - len(shorter)]
            if (len(shorter) >= 2 and longer.endswith(shorter)
                    and extra in _qualifiers()):
                return True
        return False
    # 名字里全是通用词（如"公园"）时，退回包含与公共子串
    if a in b:
        return len(a) >= 0.6 * len(b)
    if b in a:
        return (len(a) - len(b)) <= max(2, 0.5 * len(a))
    lcs = _lcs(a, b)
    return lcs >= 0.6 * len(a) and lcs >= 0.5 * len(b)


class GeoService:
    """地点表述 → 候选 POI（UC-03），并做行政区归属校验（BR-01）

    BR-01 的作用域是**当前城市**：城市由配置决定，行政区列表来自城市包，
    因此"南京模式下解析故宫"会被判为跨城而拒绝，"北京模式下解析故宫"则正常。
    """

    def __init__(self, degrade: DegradeController, trip_repo: SqliteTripRepository):
        self.degrade = degrade
        self.trip_repo = trip_repo

    def resolve(self, mention: str) -> list[Poi]:
        mention = (mention or '').strip()
        if not mention:
            return []
        city = self.degrade.city()
        pack = get_city(city)
        # ① 城市包优先：精确名或别名命中就直接用包内数据。
        #    在线检索会返回"名字相近的邻居"（新街口先锋书店→光华书局、大报恩寺→恩剧场），
        #    先查包能避免这类误配，也让营业/预约信息不会丢。
        pack_hit = self._pack_search(pack, mention)
        if pack_hit:
            log.info('地点「%s」命中城市包，跳过在线检索', mention)
            raw = pack_hit
            return self._pois_from(raw, pack, city)
        # ② 地址式输入（"玄武区珠江路653号"）：走地理编码，而不是拿门牌号去搜 POI
        if looks_like_address(mention):
            poi = self._from_geocode(mention, pack, city)
            if poi is not None:
                return [poi]
            return []

        raw: list[dict] = []
        adapter = self.degrade.map()
        try:
            raw = adapter.search_poi(mention, city)
        except MapError as exc:
            # 在线地图限流/超时时不能直接判"查无此地"：回退城市包内置 POI，
            # 保证弱网与配额用尽时仍能排程（否则在线模式反而不如离线可用）
            log.warning('地图 POI 检索失败，回退%s内置 POI：%s', city, exc)
            raw = []
        if raw:
            # ③ 名称校验：高德对任意串都可能模糊命中，名字不像就不采信
            good = [x for x in raw if name_alike(mention, str(x.get('name') or ''))]
            if len(good) != len(raw):
                log.info('地点「%s」筛掉 %d 条不相似的模糊命中', mention,
                         len(raw) - len(good))
            raw = good
        if not raw:
            raw = self._pack_search(pack, mention)
            if raw:
                log.info('地点「%s」由城市包命中（共 %d 条候选）', mention, len(raw))
            else:
                return []
        return self._pois_from(raw, pack, city)

    def _from_geocode(self, address: str, pack, city: str) -> Optional[Poi]:
        """地址 → 坐标点位（BR-01：跨城地址照样拒绝）"""
        fn = getattr(self.degrade.map(), 'geocode', None)
        if fn is None or not self.degrade.map_ready():
            return None
        try:
            got = fn(address, city)
        except Exception as exc:                 # noqa: BLE001
            log.warning('地址解析失败 %s：%s', address, exc)
            return None
        if not got:
            return None
        district = str(got.get('district') or '')
        if district and not pack.in_city(district):
            log.info('地址「%s」不在 %s（%s），按 BR-01 拒绝', address, city, district)
            return None
        poi = Poi(name=address, address=str(got.get('address') or address),
                  district=district, lng=float(got.get('lng') or 0),
                  lat=float(got.get('lat') or 0), city=city,
                  provider='geocode', kind='地点')
        self.trip_repo.ensure_poi(poi)
        intel = getattr(self, 'intel', None)
        if intel is not None:
            try:
                intel.apply(poi)
            except Exception:                    # noqa: BLE001
                pass
        return poi

    def _pois_from(self, raw: list[dict], pack, city: str) -> list[Poi]:
        """把检索结果转成 Poi 列表（并拒绝"子地点"式的误配）"""
        pois: list[Poi] = []
        for item in raw:
            name = item.get('name', '')
            if not name:
                continue
            # 城市包里有同名地点时，把营业时间/类别/预约要求一并带出来
            # （在线检索结果通常只有坐标，这些字段必须由城市包补齐）
            extra = pack.find(name) or {}
            merged = {**extra, **{k: v for k, v in item.items() if v}}
            poi = Poi.from_pack(merged, city)
            if item.get('provider_poi_id'):
                poi.provider_poi_id = item['provider_poi_id']
            poi.provider = (getattr(self.degrade.map(), 'provider', 'mock')
                            if hasattr(self.degrade.map(), 'provider') else 'amap')
            self.trip_repo.ensure_poi(poi)      # 地点入库，供通行段缓存使用
            intel = getattr(self, 'intel', None)
            if intel is not None:
                try:
                    intel.apply(poi)            # 真实营业时间/票价/推荐时长覆盖种子数据
                except Exception as exc:        # noqa: BLE001 情报失败不影响排程
                    log.debug('景点情报应用失败 %s：%s', poi.name, exc)
            pois.append(poi)
        return pois

    @staticmethod
    def _pack_search(pack, mention: str) -> list[dict]:
        """城市包内检索：精确/别名优先，其次名称包含（与离线适配器同口径）"""
        exact = pack.find(mention)
        if exact:
            return [exact]
        # 「三山街地铁站」「夫子庙（景区）」这类说法：规范化后再查一次
        plain = normalize_mention(mention)
        if plain and plain != mention:
            exact = pack.find(plain)
            if exact:
                return [exact]
            for suffix in ('(地铁站)', '站'):
                exact = pack.find(f'{plain}{suffix}')
                if exact:
                    return [exact]
        # 包内模糊匹配要"够像"：库里现在有 400+ 点位（含明故宫遗址公园、南京城墙…），
        # 若只按"包含"匹配，"故宫"会被判成南京的明故宫，跨城提示就失效了。
        def close(name: str) -> bool:
            if mention in name:
                return len(mention) >= max(2, int(0.6 * len(name)))
            return False
        def alias_ok(alias: str) -> bool:
            # 别名命中也要过相似度："博物院"这种通用别名不能代表"故宫博物院"
            return (alias == mention or close(alias)) and name_alike(mention, alias)

        hits = [p for p in pack.pois
                if close(p['name'])
                or any(alias_ok(a) for a in p.get('aliases', ()))]
        return [dict(h) for h in hits[:5]]


# ============================ 通行时长服务 ============================
class TransitService:
    """通行时长估算（UC-04）：先查缓存，缺失再调地图并回填"""

    def __init__(self, degrade: DegradeController, cache: SqliteTransitCache):
        self.degrade = degrade
        self.cache = cache
        self.calls = 0          # 统计真实地图调用次数（测试用）

    def estimate(self, a: Optional[Poi], b: Optional[Poi],
                 mode: TransitMode) -> Optional[int]:
        if a is None or b is None:
            return 0
        if same_place(a, b):
            return 0                    # 同一处地点之间没有通行
        if a.poi_id and b.poi_id:
            cached = self.cache.get_full(a.poi_id, b.poi_id, mode.value)
            if cached:
                # 离线估算的缓存不得覆盖可用的真实数据：
                # 切到在线后必须重新向地图服务取一次（否则真实通行时长永远进不来）
                src = str(cached.get('source') or '')
                stale_mock = self.degrade.map_ready() and any(
                    k in src for k in ('离线', 'mock', 'Mock'))
                if not stale_mock:
                    return int(cached['duration_min'])
                log.info('忽略离线缓存（%s→%s/%s，来源 %s），改查真实地图服务',
                         a.name, b.name, mode.value, src)
        source = None
        try:
            res = self.degrade.map().route((a.lng, a.lat), (b.lng, b.lat), mode.value)
        except MapError as exc:
            # 在线地图不可用（限流/超时）时退回离线距离估算，并把这个来源写进缓存：
            # 缓存里带"离线"字样，等地图恢复后 D-12 的规则会自动重新取真实值
            log.warning('路径规划失败，回退离线距离估算：%s', exc)
            res = self._offline_route(a, b, mode)
            source = '离线估算（地图不可用）'
        if not res or not res.get('duration_min'):
            return None
        self.calls += 1
        if a.poi_id and b.poi_id:
            self.cache.put(a.poi_id, b.poi_id, mode.value, int(res['duration_min']),
                           int(res.get('distance_m') or 0),
                           source or self.degrade.map().name())
        return int(res['duration_min'])

    def _offline_route(self, a: Poi, b: Poi, mode: TransitMode) -> Optional[dict]:
        """离线距离估算（球面距离 × 方式速度与绕行系数），不依赖网络"""
        try:
            return MockMapAdapter(city=self.degrade.city()).route(
                (a.lng, a.lat), (b.lng, b.lat), mode.value)
        except Exception as exc:      # noqa: BLE001 估算失败也不能影响主流程
            log.warning('离线估算失败：%s', exc)
            return None

    def as_fn(self) -> engine_mod.TransitFn:
        return lambda a, b, mode: self.estimate(a, b, mode)

    def estimate_all(self, a: Optional[Poi], b: Optional[Poi]) -> dict[str, Optional[int]]:
        """一次给出各交通方式的时长，供界面并排对比由用户选择。

        每一种方式单独走缓存（transit_leg 的唯一键含 mode），
        因此对比展示不会造成重复外网调用。
        """
        out: dict[str, Optional[int]] = {}
        for m in (TransitMode.DRIVING, TransitMode.TRANSIT,
                  TransitMode.WALKING, TransitMode.BICYCLING):
            out[m.value] = self.estimate(a, b, m)
        return out

    def transit_plans(self, a: Optional[Poi],
                      b: Optional[Poi]) -> tuple[list[dict], str]:
        """取公交换乘方案详情，返回 (方案列表, 说明)。

        离线或适配器不支持时返回空列表并说明原因，不臆造线路数据。
        """
        if a is None or b is None:
            return [], '缺少地点，无法查询公交方案'
        if not self.degrade.map_ready():
            return [], ('离线模式：公交换乘方案需联网向地图服务查询，'
                        '内置离线数据不含线路与站点信息')
        fn = getattr(self.degrade.map(), 'transit_plans', None)
        if fn is None:
            return [], '当前地图适配器不支持公交方案详情'
        try:
            plans = fn((a.lng, a.lat), (b.lng, b.lat))
        except MapError as exc:
            return [], f'获取公交方案失败：{exc}'
        if not plans:
            return [], '该区间没有查询到可行的公交/地铁方案（可能距离过近或过远）'
        return plans, ''


# ============================ 桌面助手问答 ============================
class AssistantService:
    """通用 AI 问答（桌面助手能力）。

    设计要点：
      1) 在线：走 LLMPort.chat()，并把"本地行程摘要"注入上下文，
         使助手既能闲聊问答，也能回答"我今天下午有什么安排"；
      2) 离线：不编造答案，改为基于本地数据库做确定性回答，并明确提示需要配置 Key；
      3) 任何异常都降级为离线答复，绝不让界面崩或空白。
    """

    OFFLINE_HINT = ('（离线模式）通用问答需要在线大模型支持。请在「设置」里填入'
                    '大模型 API Key 后点击工具栏「离线模式：开」切到在线；'
                    '当前仍可用「行程录入」一句话安排行程。')

    def __init__(self, degrade: DegradeController, trip_svc: 'TripService'):
        self.degrade = degrade
        self.trip_svc = trip_svc

    # ---------- 上下文 ----------
    def day_brief(self, plan_date: date) -> str:
        """把当日行程压缩成一段可注入提示词的中文摘要"""
        trip = self.trip_svc.day_view(plan_date)
        items = sorted([t for t in trip.tasks if t.start_ts is not None],
                       key=lambda t: t.start_ts)  # type: ignore[arg-type,return-value]
        if not items:
            return f'{plan_date.month}月{plan_date.day}日暂无安排。'
        parts = []
        for t in items:
            end = t.end_ts.strftime('%H:%M') if t.end_ts else '--:--'
            poi = f'，地点 {t.poi.name}' if t.poi else ''
            parts.append(f'{t.start_ts.strftime("%H:%M")}-{end} {t.title}'
                         f'（{t.duration_min} 分钟{poi}）')
        unscheduled = [t.title for t in trip.tasks if t.start_ts is None]
        extra = f'；未排入：{"、".join(unscheduled)}' if unscheduled else ''
        return (f'{plan_date.month}月{plan_date.day}日共 {len(items)} 项安排：'
                + '；'.join(parts) + extra)

    # ---------- 问答 ----------
    # ---------- 意图路由：问答 / 录入 / 变更 / 反问 ----------
    #: 变更类动词（这些是"改已有安排"，走选项式确认）
    _ACT_WORDS = ('删掉', '删除', '去掉', '取消', '延误', '晚', '多待', '顺延',
                  '推迟', '提前', '改成', '换成', '完成', '已到', '到了')
    #: 问句特征
    _ASK_WORDS = ('?', '？', '吗', '呢', '多少', '几点', '多久', '合理',
                  '建议', '推荐', '为什么', '怎么')

    def _find_task(self, plan_date: date, hint: str):
        """在当天安排里找用户说的那一项（先精确包含，再退到 poi 名）"""
        trip = self.trip_svc.day_view(plan_date)
        hint = (hint or '').strip()
        for t in trip.tasks:
            if hint and (hint in t.title or t.title in hint):
                return t, trip
        for t in trip.tasks:
            if t.poi is not None and (t.poi.name in hint or hint in t.poi.name):
                return t, trip
        return None, trip

    def route(self, text: str, plan_date: date) -> dict:
        """判断这句话是"问 / 录入 / 改 / 说不清"，并给出可确认的动作。

        写库的事一律交回给界面：这里只**判断 + 预览效果**，绝不落库。
        """
        raw = (text or '').strip()
        if not raw:
            return {'kind': 'clarify', 'message': '想把哪一段改成什么样？'}
        low = raw
        trip = self.trip_svc.day_view(plan_date)
        titles = [t.title for t in trip.tasks]

        # ---------- 0. 行程顾问类：给区间/说"没头绪" → 出多日草案 ----------
        #    这类请求里往往一个地名都没有（"帮我推荐行程"），交给录入路径只会得到 0 条。
        from .proposal import parse_date_range as _pdr
        wants_plan = any(w in low for w in ('推荐', '规划行程', '安排行程', '没头绪',
                                           '没有头绪', '怎么玩', '玩几天', '行程'))
        has_range = _pdr(raw, plan_date) is not None
        # 用户已经在报地名（"顺序固定：新街口、总统府…"）→ 这是录入，不要问日期
        from .cities import get_city as _gc
        _pack = _gc(self.trip_svc.city)
        _names = list(_pack.names()) + list(_pack.aliases())
        hit_places = {n for n in _names if n and n in raw}
        import re as _re
        has_duration = bool(_re.search(r'\d+\s*(分钟|小时)|[一二两三四五六七八九]\s*个?\s*小时', raw))
        looks_like_entry = (len(hit_places) >= 2 and not has_duration
                            and not any(w in low for w in ('推荐', '几天', '没头绪',
                                                           '没有头绪')))
        if looks_like_entry:
            return {'kind': 'entry',
                    'message': '你已经在报地名了，我按录入给你出预览卡（确认后才写库）',
                    'places': sorted(hit_places)[:8]}
        consult = getattr(self, 'consult', None)
        if wants_plan and consult is not None:
            # 多轮顾问优先：它会在信息不足时先问、信息够时给可改的草案
            got = consult.chat(getattr(self, '_consult_session', 'assistant'),
                               raw, plan_date)
            return {'kind': 'consult', 'message': got.get('message'),
                    'questions': got.get('questions') or [],
                    'plan': got.get('plan') or {},
                    'slots': got.get('slots') or {}}
        if wants_plan and (has_range or not trip.tasks):
            plan = self.trip_svc.propose_trip(raw, plan_date)
            if plan.get('ok'):
                return {'kind': 'propose', 'message': plan.get('message'),
                        'plan': plan}

        # ---------- 1. 变更类（改已有安排） ----------
        if any(w in low for w in self._ACT_WORDS) and trip.tasks:
            minutes = self._minutes_of(low)
            # 延误 / 多待 / 顺延
            if any(w in low for w in ('延误', '多待', '晚', '顺延', '推迟')) and minutes:
                t, _ = self._find_task(plan_date, self._hint_before_act(low))
                if t is None:
                    return {'kind': 'clarify',
                            'message': f'当天有：{"、".join(titles)}。你要改哪一项？'}
                action = {'type': 'delay', 'title': t.title,
                          'task_id': t.task_id, 'delay_min': minutes}
                return self._confirm(action, plan_date, t)
            # 删除
            if any(w in low for w in ('删掉', '删除', '去掉', '取消')):
                t, _ = self._find_task(plan_date, self._hint_before_act(low))
                if t is None:
                    return {'kind': 'clarify',
                            'message': f'当天有：{"、".join(titles)}。你要删哪一项？'}
                return self._confirm({'type': 'delete', 'title': t.title,
                                      'task_id': t.task_id}, plan_date, t)
            # 换方式
            mode = None
            for key, val in (('步行', 'walking'), ('走路', 'walking'),
                             ('骑行', 'bicycling'), ('骑车', 'bicycling'),
                             ('公交', 'transit'), ('地铁', 'transit'),
                             ('驾车', 'driving'), ('开车', 'driving')):
                if key in low:
                    mode = val
                    break
            if mode and ('改' in low or '换' in low):
                t, _ = self._find_task(plan_date, self._hint_before_act(low))
                if t is None:
                    return {'kind': 'clarify',
                            'message': f'当天有：{"、".join(titles)}。你要改哪一段？'}
                return self._confirm({'type': 'set_mode', 'title': t.title,
                                      'task_id': t.task_id, 'mode': mode},
                                     plan_date, t)
            # 完成
            if any(w in low for w in ('完成', '已到', '到了')):
                t, _ = self._find_task(plan_date, self._hint_before_act(low))
                if t is not None:
                    return self._confirm({'type': 'done', 'title': t.title,
                                          'task_id': t.task_id}, plan_date, t)

        # ---------- 2. 问句 ----------
        if any(w in low for w in self._ASK_WORDS):
            return {'kind': 'answer'}

        # ---------- 3. 录入类：交给批量预览（已有选项式确认） ----------
        from .parsing_rules import parse_duration
        if any(w in low for w in ('去', '到', '前往')) and (
                parse_duration(low) or low.count('去') >= 2):
            return {'kind': 'preview'}

        # ---------- 4. 说不清 ----------
        return {'kind': 'clarify',
                'message': ('我没听清要改什么。可以说：'
                            '「明天九点去中山陵两小时」「中山陵多待40分钟」'
                            '「把夫子庙删掉」「这段改步行」')}

    @staticmethod
    def _minutes_of(text: str) -> int:
        from .parsing_rules import parse_duration
        return int(parse_duration(text) or 0)

    @staticmethod
    def _hint_before_act(text: str) -> str:
        """取动词之前的片段作为"改哪一项"的线索"""
        for w in ('多待', '延误', '顺延', '推迟', '删掉', '删除', '去掉', '取消',
                  '改成', '换成', '完成'):
            if w in text:
                return text.split(w)[0].strip(' 把将的，,。')
        return text

    def _confirm(self, action: dict, plan_date: date, task) -> dict:
        """生成选项式确认卡：说明"改完会怎样"，并列出可替代选项"""
        import copy
        trip = self.trip_svc.day_view(plan_date)
        opts = self.trip_svc.options(lodging=trip.lodging, plan_date=plan_date)
        base = engine_mod.schedule(copy.deepcopy(trip).tasks, opts,
                                   self.trip_svc.transit.as_fn(),
                                   self.trip_svc._mode_pick())
        base_items = base.ordered()
        end_before = hhmm(base_items[-1].end_min) if base_items else '—'

        t2 = copy.deepcopy(trip)
        target = None
        for x in t2.tasks:
            if x.task_id == action.get('task_id') or x.title == action.get('title'):
                target = x
                break
        if target is None:
            return {'kind': 'clarify', 'message': '这项已经不在当天安排了'}
        kind = action['type']
        if kind == 'delay':
            target.duration_min += int(action['delay_min'])
        elif kind == 'delete':
            t2.tasks = [x for x in t2.tasks if x is not target]
        elif kind == 'set_mode':
            target.mode = TransitMode(action['mode'])
            opts.auto_mode = False      # 只影响这次模拟，不动全局设置
        elif kind == 'done':
            target.status = TaskStatus.DONE
        res = engine_mod.schedule(t2.tasks, opts,
                                  self.trip_svc.transit.as_fn(),
                                  self.trip_svc._mode_pick())
        items = res.ordered()
        end_after = hhmm(items[-1].end_min) if items else '—'

        if kind == 'delay':
            label = f"把「{action['title']}」延长 {action['delay_min']} 分钟"
        elif kind == 'delete':
            label = f"删掉「{action['title']}」"
        elif kind == 'set_mode':
            label = f"把「{action['title']}」这段改成{TransitMode(action['mode']).cn}"
        elif kind == 'done':
            label = f"把「{action['title']}」标记为已完成"
        else:
            label = f"对「{action.get('title', '')}」执行改动"
        effect = {'end_before': end_before, 'end_after': end_after,
                  'conflicts': len(res.conflicts),
                  'unscheduled': len(res.unscheduled)}
        options = [{'label': f'就这样执行（末项 {end_after} 结束）',
                    'action': action, 'primary': True}]
        # 变更有冲突时，顺手给出替代选项（复用并列备选）
        if res.conflicts or res.unscheduled:
            try:
                for alt in self.trip_svc.alternatives(plan_date):
                    options.append({'label': alt['label'] + '　' + alt['effect'],
                                    'action': {'type': 'alternative',
                                               'patch': alt.get('patch') or {}},
                                    'primary': False})
            except Exception:                 # noqa: BLE001 备选失败不影响确认
                pass
        return {'kind': 'confirm', 'message': f'我理解你要：{label}',
                'action': action, 'effect': effect, 'options': options}

    def apply(self, action: dict, plan_date: date) -> dict:
        """用户确认后真正执行，并返回可撤销的版本号"""
        kind = (action or {}).get('type')
        # 记住"改动前"的版本号：撤销要回到它，而不是改动后的快照
        undo_version = None
        try:
            _trip = self.trip_svc.day_view(plan_date)
            if _trip.trip_id:
                _rows = self.trip_svc.list_versions(_trip.trip_id)
                undo_version = _rows[0].version_id if _rows else None
        except Exception:                     # noqa: BLE001 取不到就不提供撤销
            undo_version = None
        try:
            if kind == 'delay':
                out = self.trip_svc.delay_task(int(action['task_id']),
                                               int(action['delay_min']))
            elif kind == 'delete':
                out = self.trip_svc.delete(int(action['task_id']))
            elif kind == 'set_mode':
                out = self.trip_svc.adjust(int(action['task_id']),
                                           {'mode': action['mode']})
            elif kind == 'done':
                out = self.trip_svc.adjust(int(action['task_id']),
                                           {'status': 'done'})
            elif kind == 'alternative':
                out = self.trip_svc.apply_suggestion(
                    type('S', (), {'patch': action.get('patch') or {}})(),
                    plan_date)
            else:
                return {'ok': False, 'message': '不支持的动作'}
        except Exception as exc:              # noqa: BLE001 执行失败如实回报
            return {'ok': False, 'message': f'执行失败：{exc}'}

        ok = bool(getattr(out, 'result', None))
        trip = self.trip_svc.day_view(plan_date)
        items = (out.result.ordered() if ok and out.result else [])
        tail = hhmm(items[-1].end_min) if items else '—'
        return {'ok': ok,
                'message': (getattr(out, 'explanation', '') or
                            (f'已执行，末项 {tail} 结束' if ok else '未执行')),
                'undo_version': undo_version,
                'trip_id': trip.trip_id,
                'end': tail}


    def ask(self, question: str, history: list[dict] | None = None,
            plan_date: date | None = None, context: str | None = None) -> ChatReply:
        """回答问题。

        :param context: 预先算好的"本地行程摘要"。界面在**主线程**先调用
            `day_brief()` 取得它再传入，这样本方法在工作线程里执行时不会碰
            SQLite（sqlite3 连接禁止跨线程使用，否则报
            "SQLite objects created in a thread can only be used in that same thread"）。
        """
        t0 = _time.perf_counter()
        question = (question or '').strip()
        if not question:
            return ChatReply(text='请输入你想问的内容。', source='offline', ok=False)

        brief = context if context is not None else self.day_brief(
            plan_date or date.today())

        if not self.degrade.llm_ready():
            return ChatReply(text=self._offline_answer(question, brief),
                             source='offline',
                             elapsed_ms=int((_time.perf_counter() - t0) * 1000))

        messages: list[dict] = list(history or [])[-6:]
        messages.append({
            'role': 'user',
            'content': f'【本地行程】{brief}\n\n【用户问题】{question}',
        })
        try:
            text = self.degrade.llm().chat(messages)
            return ChatReply(text=text, source='online',
                             elapsed_ms=int((_time.perf_counter() - t0) * 1000))
        except Exception as exc:      # noqa: BLE001 在线失败一律降级
            log.warning('助手问答失败，降级为离线答复：%s', exc)
            return ChatReply(
                text=self._offline_answer(question, brief)
                     + f'\n\n（在线问答失败：{type(exc).__name__}）',
                source='error', ok=False,
                elapsed_ms=int((_time.perf_counter() - t0) * 1000))

    # ---------- 离线确定性答复 ----------
    @classmethod
    def _offline_answer(cls, question: str, brief: str) -> str:
        asks_schedule = any(k in question for k in
                            ('安排', '行程', '计划', '日程', '几点', '什么时候'))
        if asks_schedule:
            return f'{brief}\n\n{cls.OFFLINE_HINT}'
        return cls.OFFLINE_HINT


# ============================ 行程用例门面 ============================
class TripService:
    """行程用例门面（UC-01 / UC-08 / UC-09 / UC-10）"""

    def __init__(self, settings_svc: SettingsService, settings: Settings,
                 db: Database, trip_repo: SqliteTripRepository,
                 version_repo: SqliteVersionRepository,
                 parse_svc: IntentParseService, geo_svc: GeoService,
                 transit_svc: TransitService, degrade: DegradeController):
        self.settings_svc = settings_svc
        self.settings = settings
        self.db = db
        self.trip_repo = trip_repo
        self.version_repo = version_repo
        self.parse_svc = parse_svc
        self.geo_svc = geo_svc
        self.transit = transit_svc
        self.degrade = degrade

    @contextmanager
    def _temporary_settings(self, **overrides):
        """本次调用临时覆盖设置，结束即还原（**不落盘**）。

        写入草案时的起床/回酒店时刻只应影响这一次排程：
        不能因为用户确认了一份草案，就把他全局的默认日界偷偷改掉。
        """
        saved = {k: getattr(self.settings, k) for k in overrides}
        for key, value in overrides.items():
            setattr(self.settings, key, value)
        try:
            yield
        finally:
            for key, value in saved.items():
                setattr(self.settings, key, value)

    # ---------- 城市（BR-01 作用域） ----------
    @property
    def city(self) -> str:
        return self.degrade.city()

    # ---------- 选项 ----------
    def options(self, mode: Optional[TransitMode] = None,
                lodging: Optional[Poi] = None,
                plan_date: Optional[date] = None) -> ScheduleOptions:
        from datetime import time as _t
        hs, ms = (self.settings.day_start or '08:00').split(':')
        he, me = (self.settings.day_end or '22:00').split(':')
        m = mode or TransitMode(self.settings.transit_mode)
        rb = str(getattr(self.settings, 'return_by', '') or '').strip()
        return_by = None
        if rb:
            try:
                rh, rm = rb.split(':')[:2]
                return_by = int(rh) * 60 + int(rm)
            except (ValueError, TypeError):
                return_by = None
        return ScheduleOptions(mode=m, buffer_min=int(self.settings.buffer_min),
                               day_start=_t(int(hs), int(ms)),
                               day_end=_t(int(he), int(me)),
                               lodging=lodging,
                               return_by=return_by,
                               # 雨天优先室内：只在有雨雪预报且当天确有室内点位时才启用
                               prefer_indoor=bool(
                                   plan_date is not None
                                   and (self.day_weather(plan_date) or {}).get('wet')),
                               # 高峰加成只在线启用：离线是距离估算，叠系数没有意义
                               peak_model=(traffic_mod.TrafficModel(
                                               holiday=bool(plan_date is not None
                                                            and holidays_mod.holiday_of(plan_date)))
                                           if (bool(getattr(self.settings,
                                                            'peak_aware', True))
                                               and self.degrade.map_ready())
                                           else None),
                               keep_order=bool(getattr(self.settings,
                                                       'keep_order', False)),
                               auto_mode=bool(getattr(self.settings,
                                                      'auto_mode', False)),
                               optimize_route=bool(getattr(
                                   self.settings, 'optimize_route', False)))

    # ---------- 逐段择优（auto_mode 用） ----------
    #: 高德给的时长只含"车上/路上"，不含取车、扫码、找车位、停车步行。
    #: 因此按直线距离设两条落地规则，避免选出"骑 2 分钟去 180 米外"这类不现实的方案。
    WALK_ONLY_M = 500        # 500 米内直接步行：取车的时间成本已超过省下的时间
    NO_DRIVE_M = 1500        # 1500 米内不参与驾车比较：停车与绕行成本无数据支撑
    #: 体力阈值：用户明确"骑行 30 分钟已经很累"。按 15 km/h 折算，30 分钟约 7.5 公里、
    #: 45 分钟步行约 3.5 公里。超过就不该由系统推荐（仍可手动「换方式」）。
    #: 只卡骑行会冒出"步行 66 分钟"这种更离谱的方案，所以步行也要卡。
    MAX_BIKE_MIN = 30
    MAX_WALK_MIN = 45
    #: 实例级阈值（用户档案可覆盖）：默认等于上面的常量
    max_bike_min: int = MAX_BIKE_MIN
    max_walk_min: int = MAX_WALK_MIN
    #: 档案扩展：有没有车（False 时驾车不参与择优）、优先方式与容差
    has_car: bool = True
    prefer_mode: str = ''
    prefer_tolerance: int = 10
    #: 档案预算：人均上限（元）；None 表示不校验
    max_price: Optional[int] = None
    #: 预算档位名（提示文案用）
    budget_label: str = ''

    def pick_fastest_mode(self, a: Optional[Poi], b: Optional[Poi],
                          default: TransitMode) -> TransitMode:
        """两地点之间挑最快的方式（查不到就退回默认方式，不猜）"""
        if a is None or b is None:
            return default
        try:
            dist_m = geo_mod.distance_km(a.lng, a.lat, b.lng, b.lat) * 1000
        except Exception:                 # noqa: BLE001 坐标异常按无距离处理
            dist_m = 0.0
        if 0 < dist_m <= self.WALK_ONLY_M:
            return TransitMode.WALKING
        try:
            allm = self.transit.estimate_all(a, b)
        except Exception:                 # noqa: BLE001 取不到就退回默认
            return default
        known = {k: v for k, v in (allm or {}).items() if v}
        if 0 < dist_m <= self.NO_DRIVE_M:
            known.pop(TransitMode.DRIVING.value, None)
        # 体力阈值：超过就是"不推荐"，不参与择优（可由用户档案覆盖）
        if known.get(TransitMode.BICYCLING.value, 0) > self.max_bike_min:
            known.pop(TransitMode.BICYCLING.value, None)
        if known.get(TransitMode.WALKING.value, 0) > self.max_walk_min:
            known.pop(TransitMode.WALKING.value, None)
        # 无车用户：驾车不参与择优（仍可手动换方式；若只剩驾车则允许，别让行程无解）
        if not self.has_car and len(known) > 1:
            known.pop(TransitMode.DRIVING.value, None)
        if not known:
            return default
        best = min(known, key=known.get)
        # 出行方式优先：优先项只要在"最快 + 容差"之内就听用户的，不是硬顶
        pref = (self.prefer_mode or '').strip()
        if pref and pref in known and pref != best:
            if known[pref] <= known[best] + int(self.prefer_tolerance or 0):
                best = pref
        try:
            return TransitMode(best)
        except ValueError:
            return default

    def _mode_pick(self):
        """auto_mode 打开时给引擎的回调，关闭时给 None（引擎走原逻辑）"""
        return self.pick_fastest_mode if self.settings.auto_mode else None


    # ---------- 批量多目的地 ----------
    #: 一次录入的合理上限（与顾问侧保持一致）
    MAX_TEXT_LEN = 1200

    def preview_batch(self, text: str, plan_date: date) -> dict:
        """把一句话里的多个目的地一次解析成多条待确认项（不落库）。

        返回 {'origin': 起点名|None, 'items': [...], 'unparsed': [...],
              'segments': [...], 'split_by': 'rules'|'poi', 'keep_order': bool}

        一条铁律：**段落里说到的每一件事都必须有下落**——要么解析成事项，
        要么变成「待补地点」的条目让用户补一句，要么明确列进 unparsed。
        早期版本遇到"缺时长"就直接丢掉，用户贴一整段只排出第一项，其余静默消失。
        """
        raw_text = (text or '').strip()
        if len(raw_text) > self.MAX_TEXT_LEN:
            return {'ok': False, 'items': [], 'unparsed': [raw_text[:40]],
                    'message': f'这段话 {len(raw_text)} 字，太长了：请分几次录入'
                               f'（一次不超过 {self.MAX_TEXT_LEN} 字）'}
        from .parsing_rules import (extract_item, extract_origin, parse_duration,
                                    parse_group_duration, split_batch, split_by_pois,
                                    strip_note_parens)

        raw_text = (text or '').strip()
        # 括号里的附注先摘掉："下午总统府（这个必须去，我已经预约了）"里的总统府不能被挤丢
        raw_text = strip_note_parens(raw_text)
        # LLM 理解层（可选增强）：只采信高置信度结果，且地点必须能过 name_alike。
        # 模型不可用 / 置信度不足 / 名字对不上 → 完全退回规则链路。
        llm_parsed: dict = {}
        try:
            from . import llm_intake
            llm_parsed = llm_intake.structured_parse(raw_text, plan_date,
                                                    self.degrade)
        except Exception as exc:              # noqa: BLE001
            log.debug('LLM 结构化解析不可用：%s', exc)
        llm_conf = float(llm_parsed.get('confidence') or 0)
        # 否定/排除：「除了总统府，其他都去」「不去夫子庙」——这类语义必须反过来执行
        excluded: list[str] = []
        for pat in (r'除了\s*([\u4e00-\u9fa5A-Za-z0-9]{2,14})',
                    r'不去\s*([\u4e00-\u9fa5A-Za-z0-9]{2,14}?)(?:[，,。；;]|$)',
                    r'([\u4e00-\u9fa5A-Za-z0-9]{2,14}?)\s*就不去了',
                    r'不要\s*([\u4e00-\u9fa5A-Za-z0-9]{2,14})'):
            for mm in re.finditer(pat, raw_text):
                cand = mm.group(1).strip()
                poi = self._poi_named(cand) if cand else None
                name = poi.name if poi is not None else cand
                if name and name not in excluded:
                    excluded.append(name)

        # 条件分支先摘出来当"备选方案"：雨天/晴天两套方案不能都排进同一天
        raw_text, conditional = self._split_conditional(raw_text)
        if not conditional and llm_conf >= 0.6 and llm_parsed.get('conditionals'):
            for c in llm_parsed['conditionals']:
                names = []
                for nm in c.get('places') or []:
                    poi = self._poi_named(nm)
                    if poi is not None and name_alike(nm, poi.name):
                        names.append(poi.name)
                if names:
                    conditional.append({'trigger': c.get('trigger') or '条件',
                                        'places': names, 'text': '（模型识别）'})
        raw_text = self._drop_meta_clauses(raw_text)
        origin, rest = extract_origin(raw_text)
        pack = get_city(self.city)
        all_places = list(pack.names()) + list(pack.aliases())
        segments = split_batch(raw_text)
        split_by = 'rules'
        # 整段没有标点分隔时，规则切不出东西 —— 用已知地点名当边界兜底切
        if len(segments) <= 1 and len(raw_text) >= 12:
            by_poi = split_by_pois(raw_text, all_places)
            if len(by_poi) > len(segments):
                segments, split_by = by_poi, 'poi'
        # 片段内部还挤着 ≥2 个已知地点时，按地点名再切一次。
        # 不切的话"先锋书店（40分钟），傍晚回玄武湖散步1小时，晚上去鸡鸣寺和台城"
        # 只会解析出第一处地点，**后面 3 处连"未解析"都不会出现**（实测 L7 型）。
        refined: list[str] = []
        for seg in segments:
            sub = split_by_pois(seg, all_places) if self._place_count(seg) >= 2 else []
            if len(sub) >= 2:
                refined.extend(self._tidy_segment(x) for x in sub)
                split_by = 'poi' if split_by == 'poi' else 'rules+poi'
            else:
                refined.append(self._tidy_segment(seg))
        segments = [s for s in refined if s]
        # "每个地方平均停留 50 分钟"：整句共用的时长，补给没写时长的片段
        group_min = parse_group_duration(raw_text)
        items: list[dict] = []
        unparsed: list[str] = []
        seen_places: set[str] = set()
        for seg in segments:
            if self._is_noise_segment(seg, group_min):
                continue
            if self._is_homecoming(seg, origin):
                continue
            d_seg = parse_duration(seg)
            # "我周六早上到南京站""十点到南京南站"：车站是出发点，不是第一个景点
            if d_seg is None and group_min is None and self._is_arrival_origin(seg, origin):
                if not origin:
                    poi0 = self._place_in_segment(seg)
                    # 起点名用"口语形态"（南京站）而不是包内全名（南京站(地铁站)）：
                    # 它是给人看的出发点，后面的取数会自己再解析一次。
                    if poi0 is not None:
                        origin = normalize_mention(poi0.name) or poi0.name
                continue
            # 含糊指代（"那个什么寺"）：不猜，直接给待补项
            if any(w in seg for w in self._VAGUE):
                items.append(self._salvage_item(seg, group_min, None, plan_date,
                                                force_reason='指代含糊，没有可解析的地名'))
                continue
            # 同一地点、又没写时长 → 当作"重复提及"跳过（"玄武湖和夫子庙我都想再去一次"）
            place_here = self._place_in_segment(seg)
            if (d_seg is None and group_min is None and place_here is not None
                    and place_here.name in seen_places):
                continue
            try:
                need = seg if d_seg is not None or not group_min else \
                    f'{seg}，{group_min}分钟'
                out = self.preview_text(need, plan_date)
            except Exception as exc:          # noqa: BLE001 单条失败不能拖垮整批
                log.debug('批量解析单条失败 %s：%s', seg, exc)
                out = None
            if out is not None and out.lodging is not None:   # 句子里出现"去某酒店住"这类
                items.append({'segment': seg, 'kind': 'lodging',
                              'poi': out.lodging.name, 'title': '住宿',
                              'duration_min': 0, 'duration_text': '—',
                              'desired_start': '', 'locked': False,
                              'category': '', 'poi_brief': '', 'notices': []})
                continue
            if out is not None and out.ok and out.draft is not None:
                d = out.draft
                if d.poi is not None:
                    seen_places.add(d.poi.name)
                ev = self.duration_evidence(d.poi) if d.poi is not None else {}
                items.append({
                    'segment': seg, 'kind': 'task', 'raw_text': seg,
                    'title': d.title,
                    'poi': d.poi.name if d.poi else '',
                    'poi_brief': d.poi.brief() if d.poi else '',
                    'category': d.category,
                    'duration_min': d.duration_min,
                    'duration_text': hm_text(d.duration_min),
                    'duration_source': (ev.get('source') or ''),
                    'duration_label': (ev.get('label') or ''),
                    'duration_range': (ev.get('low'), ev.get('high'))
                    if ev.get('low') and ev.get('high') else None,
                    'desired_start': (d.desired_start.strftime('%H:%M')
                                      if d.desired_start else ''),
                    'deadline': d.deadline.strftime('%H:%M') if d.deadline else '',
                    'locked': bool(d.is_locked),
                    'notices': [c.detail for c in (out.notices or [])],
                })
                continue
            # 解析失败：先抢救，抢救不回来才承认"没听懂"
            salvaged = self._salvage_item(seg, group_min, out, plan_date)
            if salvaged is not None:
                if salvaged.get('poi'):
                    seen_places.add(salvaged['poi'])
                items.append(salvaged)
            else:
                unparsed.append(seg)
        if excluded:
            kept = []
            for it in items:
                if it.get('poi') and any(e and e in it['poi'] for e in excluded):
                    continue                    # 用户明确排除的地点不排
                kept.append(it)
            items = kept

        return {'origin': origin, 'items': items, 'unparsed': unparsed,
                'excluded': excluded,
                'segments': segments, 'split_by': split_by,
                'conditional': conditional,
                'return_by': (self._return_by_of(raw_text)
                              or (llm_parsed.get('return_by') or '')
                              if llm_conf >= 0.6 else self._return_by_of(raw_text)),
                'parse_confidence': llm_conf,
                'keep_order': bool(getattr(self.settings, 'keep_order', False))}

    #: "晚上还要赶 21:00 的高铁/飞机" —— 这条死线必须变成 return_by，否则排到 22:00 也不吭声
    _RETURN_VEHICLE = re.compile(
        r'(\d{1,2})\s*[:：点]\s*(\d{2})?\s*分?\s*(?:左右)?'
        r'[^。；;，,\n]{0,8}?'
        r'(?:高铁|动车|火车|航班|飞机|大巴|车票|返程)')

    def _return_by_of(self, text: str) -> str:
        """从"赶 X 点的高铁"里取最晚返回时刻（HH:MM），没有则空串"""
        m = self._RETURN_VEHICLE.search(text or '')
        if not m:
            return ''
        hh = int(m.group(1))
        mm = int(m.group(2) or 0)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return ''
        return f'{hh:02d}:{mm:02d}'

    #: 纯指令尾巴：出现这些词且没有地点/时长，就当"交代表述"而不是一件事
    _NOISE_WORDS = ('规划', '生成', '计算', '行程表', '最优', '方案', '谢谢', '麻烦',
                    '帮我', '请问', '可以吗', '怎么样')
    #: 附注/偏好类交代表述：不是一件事，而是对整段的补充说明
    _NOTE_WORDS = ('预约', '预订', '实名', '闭馆', '开馆', '门票', '停车', '排队',
                    '尽量', '最好', '能不', '就骑车', '就步行', '少走路', '少绕',
                    '重复', '再说一遍', '再提', '同一句', '不赶时间', '随意',
                    '别排太满', '别太赶', '不要排太满', '赶', '太满')
    #: 元话语：整句都是"我再说一遍…"这类，无论有没有时长都不该成为事项
    _META_WORDS = ('重复一句', '重复一遍', '再说一遍', '再提一句', '同一句', '照抄')
    #: 含糊指代：宁可让用户补一句，也不许拿"那个什么寺"去猜一个具体地点
    _VAGUE = ('那个什么', '这个什么', '某个地', '随便一个', '记不清', '好像是那个',
              '不记得名字', '叫什么来着')
    #: 条件分支："如果下雨就改去 B、C" —— 这是**备选方案**，不是当天的第 5、第 6 个景点
    _COND = re.compile(
        r'(如果|要是|万一|若|假如)?\s*(不下雨|下雨|雨天|天气不好|晴天|天晴)?'
        r'[^。；;\n]{0,12}?(就|则|那就|改去|换成|改成)\s*([^。；;\n]{2,60})')

    def _split_conditional(self, text: str) -> tuple[str, list[dict]]:
        """把整段话里的条件分支摘出来，返回 (当天部分, 备选方案列表)。

        「如果下雨就改去南京博物院和先锋书店」→ 当天部分不含这句，
        备选方案记 {'trigger': '下雨', 'places': [...], 'text': 原句}。
        """
        if not text:
            return text, []
        pack = get_city(self.city)
        names = list(pack.names()) + list(pack.aliases())
        keep_parts: list[str] = []
        conds: list[dict] = []
        for clause in re.split(r'([。；;\n])', text):
            s = clause.strip()
            if not s or s in ('。', '；', ';', '\n'):
                keep_parts.append(clause)
                continue
            m = self._COND.search(s)
            if not (m and (m.group(1) or m.group(2))):
                keep_parts.append(clause)
                continue
            target = (m.group(4) or '').strip()
            found: list[str] = []
            for n in sorted({x for x in names if x}, key=len, reverse=True):
                if n in target and not any(n in f or f in n for f in found):
                    found.append(n)
            std: list[str] = []
            for n in found:
                poi = self._poi_named(pack.aliases().get(n, n)) or self._poi_named(n)
                nm = poi.name if poi is not None else n
                if nm not in std:
                    std.append(nm)
            conds.append({'trigger': (m.group(2) or m.group(1) or '条件').strip(),
                          'places': std, 'text': s})
        return ''.join(keep_parts), conds

    def _drop_meta_clauses(self, text: str) -> str:
        """先整句丢掉元话语（"另外重复一句：玄武湖和夫子庙我都想再去一次"）。

        这类句子含地点名、又可能没写时长，留着会被按地点名切碎成几条假条目。
        """
        kept: list[str] = []
        for chunk in re.split(r'[。；;\n]', text or ''):
            s = chunk.strip()
            if not s:
                continue
            if any(w in s for w in self._META_WORDS):
                continue
            kept.append(s)
        return '。'.join(kept)

    def _place_count(self, seg: str) -> int:
        """片段里出现了几个**互不重叠**的已知地点（别名归并到标准名）"""
        pack = get_city(self.city)
        aliases = pack.aliases()
        hits: list[tuple[int, str]] = []
        for n in {str(x) for x in list(pack.names()) + list(aliases) if x}:
            i = (seg or '').find(n)
            if i >= 0:
                hits.append((i, aliases.get(n, n)))
        if not hits:
            return 0
        hits.sort()
        seen: list[tuple[int, str]] = []
        for i, std in hits:
            if any(std == s for _, s in seen):
                continue
            seen.append((i, std))
        return len(seen)

    def _tidy_segment(self, seg: str) -> str:
        """清理按地点名细切后留在碎片上的毛边：尾巴的连接词、句末的"帮我排…"。"""
        s = (seg or '').strip(' ，,。.；;、')
        # 细切会把"晚上去"这种悬空成分留在片段末尾
        s = re.sub(r'[，,、]?\s*(?:傍晚|晚上|下午|上午|中午|早上|餐后|中途|最后|然后|接着|先|再)?'
                   r'\s*(?:回|去|到|在|又|再|和|及)?\s*$', '', s).strip(' ，,。.；;、')
        # 句末的指令尾巴（细切后可能跟着某个地点名一起被切出来）
        s = re.sub(r'[。；;，,]?\s*(?:帮我|请帮我|麻烦你?)\s*(?:规划|安排|排|生成|计算).{0,24}$', '', s)
        return s.strip(' ，,。.；;、')

    def _is_homecoming(self, seg: str, origin: Optional[str]) -> bool:
        """片段是不是"最后回起点/回住处"这类收尾说明（起点已作为住宿锚点，不再当事项）"""
        from .parsing_rules import parse_duration
        if not origin or parse_duration(seg) is not None:
            return False
        o = (origin or '').strip()
        if not o:
            return False
        # 片段里的地点和起点是同一处（含简称/别名，如"三牌楼校区"之于南邮）→ 收尾说明。
        # 细切会把"最后回"这类连接词清掉、只剩校区名，所以这里不再依赖"回"字。
        poi = self._place_in_segment(seg)
        if poi is not None and (poi.name == o or o in poi.name or poi.name in o):
            return not re.search(r'取|看|吃|逛|办|见|开|买|听|参|游|还|签|搬', seg or '')
        if '回' not in (seg or ''):
            return False
        head = o[:4] if len(o) >= 4 else o
        return o in (seg or '') or head in (seg or '')

    def _is_noise_segment(self, seg: str, group_min=None) -> bool:
        """这一片段是不是"没有实体内容的交代表述"（该丢），而不是一件事（该留）"""
        from .parsing_rules import parse_duration
        s = (seg or '').strip()
        if not s or re.fullmatch(r'[\s，,。.；;、！!？?]+', s):
            return True
        if parse_duration(s) is not None or group_min:
            return False
        # 附注/偏好："记得博物院周一闭馆、要实名预约""全程尽量少走路，能骑车就骑车"
        if any(w in s for w in self._NOTE_WORDS):
            return True
        if any(w in s for w in self._NOISE_WORDS) and not self._mentions_place(s):
            return True
        return False

    def _mentions_place(self, seg: str) -> bool:
        """片段里是否出现了本城已知地点（名或别名）"""
        pack = get_city(self.city)
        for n in list(pack.names()) + list(pack.aliases()):
            if n and n in seg:
                return True
        return False

    def _is_arrival_origin(self, seg: str, origin: Optional[str]) -> bool:
        """片段是不是"到/在 XX站"这类**出发点**声明（车站不该被排成一个景点）"""
        from .parsing_rules import parse_duration
        if parse_duration(seg) is not None:
            return False
        if not re.search(r'到|抵达|到达|在|下车|落地', seg or ''):
            return False
        poi = self._place_in_segment(seg)
        if poi is None:
            return False
        if poi.kind != '交通' and '站' not in poi.name:
            return False
        # 只有"纯出发点句"才算：不能再夹着别的动宾（"在新街口逛街"不是出发点）
        return not re.search(r'逛|看|吃|买|参观|游览|取|办|开会|散步|玩', seg or '')


    #: 没写时长时的兜底：优先用城市包里的推荐游玩时长（visit），再退到按类别取值。
    #: 以前这里一律 60 分钟——南博真实要 2.5 小时，按 60 分钟排出来的时间轴
    #: 看着"都装下了"，实际现场全崩（D-67）。
    _KIND_DEFAULT_MIN = {'景点': 90, '场馆': 90, '公园': 90, '街区': 60,
                         '商圈': 60, '书店': 60, '校园': 60, '医疗': 60,
                         '办事': 60, '餐饮': 60}

    def _default_duration(self, poi) -> int:
        """没写时长时的取值：优先走证据表（官方/平台/估算三级），取中值"""
        ev = getattr(self, 'evidence', None)
        if ev is not None:
            try:
                return int(ev.resolve(poi, self.city)['minutes'])
            except Exception as exc:            # noqa: BLE001 证据失败不影响排程
                log.debug('时长证据查询失败：%s', exc)
        if poi is not None:
            v = int(getattr(poi, 'visit_min', 0) or 0)
            if v > 0:
                return v
            k = str(getattr(poi, 'kind', '') or '')
            if k in self._KIND_DEFAULT_MIN:
                return self._KIND_DEFAULT_MIN[k]
        return 60

    def duration_evidence(self, poi) -> dict:
        """给界面用的时长证据（中值 + 区间 + 来源）"""
        ev = getattr(self, 'evidence', None)
        if ev is None:
            return {}
        try:
            return ev.resolve(poi, self.city)
        except Exception:                        # noqa: BLE001
            return {}

    def _category_of_poi(self, poi: Optional[Poi]) -> str:
        """按地点类型给出事项类别（写库时类别为空就用它兜底）"""
        if poi is None:
            return '其他'
        if poi.is_food:
            return '餐饮'
        return {'医疗': '医疗', '景点': '展览', '场馆': '展览',
                '办事': '办事', '交通': '办事'}.get(poi.kind, '其他')


    def _name_alike(self, a: str, b: str) -> bool:
        """模块级只实现一次；这里保留方法名以兼容既有调用"""
        return name_alike(a, b)


    def _place_in_segment(self, seg: str) -> Optional[Poi]:
        """从片段里捞出一个已知地点（长名优先，别名映射回标准名）

        要点：别名允许做"简称"（"南博"），但不许被当成词根吞进更长的词里——
        "故宫博物院"里含"博物院"（南京博物院的别名），不能因此判成南京博物院。
        """
        pack = get_city(self.city)
        aliases = pack.aliases()
        cands = sorted(set(list(pack.names()) + list(aliases)), key=len, reverse=True)
        text = str(seg or '')
        for n in cands:
            if not n:
                continue
            hit_at = text.find(n)
            if hit_at < 0:
                # 「三山街地铁站」这类：包内标准名是「三山街(地铁站)」，按规范化再试
                plain = normalize_mention(n)
                if plain and plain != n and plain in text:
                    poi = self._poi_named(n)
                    if poi is not None and name_alike(plain, poi.name):
                        return poi
                continue
            # 取命中位置左右的连续中文当作"用户实际说的那个词"
            left, right = hit_at, hit_at + len(n)
            while left > 0 and re.match(r'[\u4e00-\u9fa5A-Za-z0-9]', text[left - 1]):
                left -= 1
            while right < len(text) and re.match(r'[\u4e00-\u9fa5A-Za-z0-9]', text[right]):
                right += 1
            spoken = text[left:right]
            key = aliases.get(n, n)
            poi = self._poi_named(key)
            if poi is None:
                continue
            if name_alike(spoken, poi.name) or spoken == n:
                return poi
        return None

    def _salvage_item(self, seg: str, group_min, out, plan_date: date,
                      force_reason: str = '') -> Optional[dict]:
        """把"解析失败但确实是件事"的片段救成可编辑条目，而不是丢掉。

        优先救成正常事项（地点认得出来就保留，缺时长按默认算）；
        认不出地点时给一张 needs_poi 的「待补」卡，附原因；
        吃饭/午休类没有地点则作为**无地点餐饮块**（默认 60 分钟）。
        ``force_reason`` 给定时不做地点猜测（含糊指代专用）。
        """
        from .parsing_rules import extract_item, guess_category, parse_duration

        dur = parse_duration(seg) or group_min
        reason = force_reason
        if not reason and out is not None and getattr(out, 'missing', None):
            reason = '缺少：' + '、'.join(out.missing)
        elif not reason and out is not None and getattr(out, 'message', ''):
            reason = str(out.message)
        raw_title = re.sub(r'\s+', ' ', extract_item(seg, None) or '').strip()
        # 解析阶段可能已经认出了地点（只是缺时长）：优先用它，别又退回离线包白跑一趟。
        # 离线包里没有的地名（栖霞山、红山动物园）在线时靠这条兜住。
        poi: Optional[Poi] = None
        cands = list(getattr(out, 'candidates', None) or []) if not force_reason else []
        if cands:
            spoken = str(getattr(cands[0], 'name', '') or '')
            poi = cands[0]
            # 候选也可能来自模糊检索：名字不像就不要（"故宫博物院"不能被凑成"南京博物院"）
            seg_places = [t for t in (raw_title, seg) if t]
            if not any(name_alike(t, poi.name) or poi.name in t for t in seg_places):
                log.info('候选 %s 与片段不像，按待补处理：%s', poi.name, seg[:24])
                poi = None
        if poi is None and not force_reason:
            poi = self._place_in_segment(seg)
        if poi is None and not force_reason:
            mention = getattr(getattr(out, 'intent', None), 'mention', None)
            for probe in (mention, (seg or '').strip(' ，,。.；;、')):
                if not probe or not (2 <= len(probe) <= 14):
                    continue
                try:
                    res = self.geo_svc.resolve(probe)      # 在线时走高德，包外地点也能认
                except Exception as exc:                   # noqa: BLE001
                    log.debug('补解析地点失败 %s：%s', probe, exc)
                    res = []
                if res:
                    cand = res[0]
                    # 高德对任意字符串都可能模糊返回一个地点：名字不像就不采信
                    if self._name_alike(probe, cand.name):
                        poi = cand
                    break
        if poi is None and not force_reason:
            # 片段本身就是一个地名（"栖霞山"这种只有名字的）：拿整段去查。
            # 必须校验名称相似度——高德对任意字符串都可能模糊返回一个地点，
            # "能看多久看多久"曾被解析成"辰龙广场"（凭空臆造，真实模拟时抓到过）。
            bare = (seg or '').strip(' ，,。.；;、')
            if 2 <= len(bare) <= 10 and not re.search(r'去|到|在|吃|逛|看|买|玩|待|停留', bare):
                try:
                    res = self.geo_svc.resolve(bare)
                except Exception:                          # noqa: BLE001
                    res = []
                if res and self._name_alike(bare, res[0].name):
                    poi = res[0]
        if poi is not None:
            # 地点认得出来 → 直接当正常事项，缺的时长按 60 分钟兜底。
            # 标题不能"顺手"去掉开头的字：'先锋书店' 去掉'先'就成了'锋书店'。
            if raw_title and (raw_title in poi.name or poi.name in raw_title):
                title = raw_title
            elif raw_title and re.search(r'看|吃|取|办|开|逛|买|参观|游览|听|见|还|签', raw_title):
                title = raw_title
            else:
                title = poi.name
            # 类别跟着地点走：医院就是医疗、馆子就是餐饮，别一律"其他"
            cat = guess_category(seg)
            if poi.is_food:
                cat = '餐饮'
            elif cat == '其他':
                cat = self._category_of_poi(poi)
            return {'segment': seg, 'kind': 'task', 'title': title,
                    'poi': poi.name, 'poi_brief': poi.brief(),
                    'category': cat or '其他',
                    'duration_min': dur or self._default_duration(poi),
                    'duration_text': hm_text(dur or self._default_duration(poi)),
                    'desired_start': '', 'locked': False, 'notices': [],
                    'needs_poi': False,
                    'reason': '时长没写，暂按 60 分钟算（可改）'}
        title = re.sub(r'^(?:我们?|那|就)?\s*(?:先|再|然后|接着|之后|还要|顺便|顺路|最后|另外)\s*',
                       '', raw_title).strip()
        title = re.sub(r'\s+', ' ', title)
        # 解析出地点后，标题里常剩"（景区）"这类残渣；清掉，别让用户看到半个括号
        title = re.sub(r'[（(][^）)]*[）)]', '', title).strip(' 　，,。.；;、-')
        title = re.sub(r'\s+', ' ', title)
        if not title or len(title) <= 1:
            title = f'游览{poi.name}' if poi is not None else seg[:12]
        if is_time_block_text(seg):
            return {'segment': seg, 'kind': 'block', 'title': title or '用餐',
                    'poi': '', 'poi_brief': '', 'category': '餐饮',
                    'duration_min': dur or 60,
                    'duration_text': hm_text(dur or 60),
                    'desired_start': '', 'locked': False, 'notices': [],
                    'needs_poi': False, 'reason': ''}
        return {'segment': seg, 'kind': 'todo', 'title': title[:16],
                'poi': '', 'poi_brief': '',
                'category': '', 'duration_min': dur or 60,
                'duration_text': hm_text(dur or 60),
                'desired_start': '', 'locked': False, 'notices': [],
                'needs_poi': True,
                'reason': f'{reason or "地点没认出来"}（补个地名就能排，或把这一项删掉）'}

    def _save_settings_if_changed(self, before: dict) -> None:
        """设置只有真的变了才落盘：单次写入不该反复改写用户配置"""
        if any(getattr(self.settings, k) != v for k, v in before.items()):
            self.settings_svc.save(self.settings)

    def add_batch(self, plan_date: date, items: list[dict],
                  origin: str | None = None, keep_order: bool = True,
                  return_by: str = '') -> tuple[bool, str]:
        """按预览卡确认过的条目批量写入；顺序即写入顺序（配合 keep_order 生效）。

        ``return_by`` 是"晚上还要赶 21:00 高铁"这类**返程死线**（HH:MM）：
        写进设置后，排程会把超出它的回程如实提示成 return_late。
        """
        from datetime import time as _t

        if origin:
            self.set_lodging(plan_date, origin)
        before = {'return_by': self.settings.return_by,
                  'keep_order': self.settings.keep_order,
                  'optimize_route': self.settings.optimize_route}
        if return_by:
            self.settings.return_by = str(return_by)
        self.settings.keep_order = bool(keep_order)
        # 用户选择"按给定顺序"时关掉自动排序；反之亦然（两者互斥）
        self.settings.optimize_route = not bool(keep_order)
        self._save_settings_if_changed(before)

        added = 0
        made: list[Task] = []
        failed: list[str] = []
        skipped: list[str] = []
        # 去重：同一天同一地点只排一次（用户可能说两遍，或先加过一次又加一次）
        seen = {t.poi.name for t in self.day_view(plan_date).tasks if t.poi is not None}
        for raw in items or []:
            poi = self._poi_named(raw.get('poi') or '') if raw.get('poi') else None
            # 午休块：显式标了 kind=block，或"餐饮类但没有地点"，都允许无地点写入
            is_block = (raw.get('kind') == 'block'
                        or (poi is None and str(raw.get('category') or '') == '餐饮'))
            if poi is None and not is_block:
                failed.append(raw.get('poi') or raw.get('segment') or '?')
                continue
            if poi is not None and poi.name in seen:
                skipped.append(poi.name)
                continue
            if poi is not None:
                seen.add(poi.name)
            # 用户没给或给了非法值（0/负数/非数字）时，按地点默认时长走；
            # 原来 max(10, -30) 会把"负时长"悄悄变成 10 分钟
            try:
                given = int(raw.get('duration_min') or 0)
            except (TypeError, ValueError):
                given = 0
            dur = given if given > 0 else self._default_duration(poi)
            dur = max(10, min(600, dur))
            start = (raw.get('desired_start') or '').strip()
            desired = None
            locked = False
            if start:
                try:
                    hh, mm = start.split(':')[:2]
                    desired = _t(int(hh), int(mm))
                    locked = True
                except (ValueError, TypeError):
                    desired, locked = None, False
            task = Task(title=(raw.get('title') or (poi.name if poi else '午休')),
                        raw_text=str(raw.get('raw_text') or raw.get('segment') or ''),
                        duration_min=dur, poi=poi,
                        category=(raw.get('category')
                                  or ('餐饮' if is_block else self._category_of_poi(poi))),
                        desired_start=desired, is_locked=locked,
                        deadline=_parse_hm(raw.get('deadline')))
            made.append(task)
            added += 1

        # 一次性排程：把整批一起交给引擎，只重排一次、只落一次库
        if made:
            trip = self.day_view(plan_date)
            for t in made:
                # 锁定语义与单条写入保持一致：给了期望时刻就要同步落 start_ts，
                # 否则引擎会把它当"待排"重新择时（午休块会被挪走，饭点提示又冒出来）
                if t.is_locked and t.start_ts is None and t.desired_start is not None:
                    t.start_ts = t.desired_start
                trip.add_task(t)
            out = self._schedule_and_save(trip, '批量新增')
            if out.result is None:
                for t in made:
                    failed.append(t.poi.name if t.poi is not None else t.title)
                added = 0
        # 写入到库 ≠ 排进时间轴：一天装不下时剩下的会成为"未排项"，
        # 原来只报"已排入 N 项"，用户以为全都排好了（边界测试 A-05 暴露）
        placed = unscheduled = 0
        if added:
            day_trip = self.day_view(plan_date)
            placed = len([t for t in day_trip.tasks if t.start_ts is not None])
            unscheduled = len([t for t in day_trip.tasks if t.start_ts is None])
        msg = f'已排入 {added} 项'
        if unscheduled:
            msg += (f'；其中当天排进时间轴 {placed} 项、{unscheduled} 项排不下'
                    '（可看时间轴里的未排项，或说"这天太赶了"我来取舍）')
        if skipped:
            msg += f'；跳过 {len(skipped)} 项重复地点（' + '、'.join(skipped[:3]) + '）'
        if failed:
            msg += f'；{len(failed)} 项未写入（找不到地点：' + '、'.join(failed[:3]) + '）'
        # 只有"确实有写不进去的"才算失败；全部是重复地点时属于重复提交，按成功处理
        if added == 0 and skipped and not failed:
            return True, f'这天的地点已经排过了：跳过 {len(skipped)} 项重复地点'
        return added > 0, msg

    # ---------- 预算：只提示，不改数据 ----------
    def _cheap_food(self, limit: int = 3) -> list:
        """本城市包里最便宜几家餐饮（按人均价升序，取不到价的略过）"""
        city = self.city
        rows = [p for p in get_city(city).poi_table()
                if str(p.get('kind') or '') in ('餐饮',) or p.get('cuisine')]
        priced = [(p.get('price_num')
                   if hasattr(p, 'price_num') else Poi.from_pack(p, city).price_num, p)
                  for p in rows]
        priced = [(v, p) for v, p in priced if v]
        priced.sort(key=lambda x: x[0])
        return [(v, p.get('name', '')) for v, p in priced[:limit]]

    def _budget_notices(self, trip: Trip) -> list[Conflict]:
        """超预算提示 + 午休平价建议（都不动用户选的点位）"""
        cap = self.max_price
        if not cap:
            return []
        label = f'≤¥{cap}/人'          # 档位名与上限分开，别写成“省：省”
        out: list[Conflict] = []
        cheap = self._cheap_food(3)

        # ① 当天餐饮项超预算
        for t in trip.tasks:
            poi = t.poi
            if poi is None or not poi.is_food:
                continue
            price = poi.price_num
            if price is None or price <= cap:
                continue
            alt = '、'.join(f'「{n}」（¥{v}）' for v, n in cheap[:2]) if cheap else ''
            out.append(Conflict(
                kind='budget_over', task_title=t.title,
                detail=(f'「{poi.name}」人均约 ¥{price}，超出你的预算'
                        f'（{self.budget_label or "预算"} {label}）'
                        + (f'；可换 {alt}，或保留原店并留意当天消费' if alt else '')),
                suggestions=[Suggestion(
                    kind='keep',
                    text=(f'换成 {alt}' if alt else '换一家更便宜的，或保留原店'))]))

        # ② 有午休块（无地点）→ 给平价候选参考
        has_block = any(t.poi is None and str(t.category or '') == '餐饮'
                        for t in trip.tasks)
        if has_block and cheap:
            names = '、'.join(f'「{n}」（¥{v}）' for v, n in cheap[:3])
            out.append(Conflict(
                kind='budget_meal', task_title='午饭',
                detail=(f'午饭还没定地方。按你的预算（{label}），城市包里的平价选择：'
                        f'{names}（价格为城市包记录，不保证在你附近）'),
                suggestions=[Suggestion(kind='keep',
                                        text='想指定某家店就直接说"中午去XX吃饭"')]))
        return out

    # ---------- 并列备选方案（冲突时给，不是一句抽象建议） ----------
    def alternatives(self, plan_date: date, limit: int = 4) -> list[dict]:
        """并列备选：每个都**真跑一遍模拟**，报末项几点结束、还剩几项没排进去。

        行程本来就排得下时返回空列表（不给用户制造噪音）。
        """
        import copy

        trip = self.day_view(plan_date)
        opts = self.options(lodging=trip.lodging, plan_date=plan_date)
        base = engine_mod.schedule(copy.deepcopy(trip).tasks, opts,
                                   self.transit.as_fn(), self._mode_pick())
        if not base.conflicts and not base.unscheduled:
            return []

        def sim(mutate) -> dict:
            t2 = copy.deepcopy(trip)
            mutate(t2)
            r = engine_mod.schedule(t2.tasks, opts, self.transit.as_fn(),
                                    self._mode_pick())
            items = r.ordered()
            end_min = items[-1].end_min if items else None
            return {'ok': not r.conflicts and not r.unscheduled,
                    'unscheduled': len(r.unscheduled),
                    'conflicts': len(r.conflicts),
                    'end_min': end_min,
                    'end': hhmm(end_min) if end_min is not None else ''}

        def describe(s: dict) -> str:
            return (f"末项 {s['end'] or '—'} 结束 · 未排入 {s['unscheduled']} 项"
                    f" · 冲突 {s['conflicts']} 处")

        out: list[dict] = []

        # A 少去一个点
        for t in list(trip.tasks):
            def drop(trip2, title=t.title):
                trip2.tasks = [x for x in trip2.tasks if x.title != title]
            s = sim(drop)
            out.append({'kind': 'drop', 'label': f'少去「{t.title}」',
                        'effect': describe(s), 'feasible': s['ok'],
                        'end': s['end'], 'end_min': s['end_min'] or 0,
                        'unscheduled': s['unscheduled'],
                        'patch': {'delete': t.title}})

        # B 缩短停留（每次减 20 分钟，最少保留 30 分钟）
        for t in list(trip.tasks):
            if t.duration_min <= 30:
                continue
            new_dur = max(30, t.duration_min - 20)
            def shorten(trip2, title=t.title, nd=new_dur):
                for x in trip2.tasks:
                    if x.title == title:
                        x.duration_min = nd
            s = sim(shorten)
            out.append({'kind': 'shorten',
                        'label': f'把「{t.title}」缩到 {new_dur} 分钟',
                        'effect': describe(s), 'feasible': s['ok'],
                        'end': s['end'], 'end_min': s['end_min'] or 0,
                        'unscheduled': s['unscheduled'],
                        'patch': {'title': t.title, 'duration_min': new_dur}})

        # C 放宽返回截止
        if opts.return_by and base.return_leg is not None and base.items:
            back = base.items[-1].end_min + base.return_leg.duration_min
            if back > opts.return_by:
                out.append({'kind': 'relax_return',
                            'label': f'把返回截止从 {hhmm(opts.return_by)} '
                                     f'放宽到 {hhmm(back)}',
                            'effect': f'预计 {hhmm(back)} 回到起点 · 其余安排不受影响',
                            'feasible': True, 'end': hhmm(back),
                            'end_min': back, 'unscheduled': 0,
                            'patch': {'return_by': hhmm(back)}})

        # 可行的排前面，其次末项更早的排前面
        out.sort(key=lambda a: (not a['feasible'], a['end_min']))
        # 并列备选要"每类有代表"：先每类取最优一条，再按序补齐，
        # 否则同类方案会把名额占满（例如 4 条全是"少去某某"）。
        picked: list[dict] = []
        for kind in ('drop', 'shorten', 'relax_return'):
            first = next((a for a in out if a['kind'] == kind), None)
            if first is not None:
                picked.append(first)
        for a in out:
            if a not in picked:
                picked.append(a)
        return picked[:limit]

    #: 用户明确说"必须去/一定要去"的标记（解析时原话留在 raw_text 里）
    _MUST_WORDS = ('必须去', '一定要去', '必去', '一定要看', '重点', '不能错过',
                   '最想去', '非去不可')

    # ---------- 多日行程提案（"我要 9/10–9/16 玩，比较懒，两个人，酒店未知"） ----------
    def propose_trip(self, text: str, today: Optional[date] = None) -> dict:
        """把"日期区间 + 偏好"变成逐日草案（不写库）。"""
        from .proposal import ItineraryPlanner

        return ItineraryPlanner(self).propose(text, today or date.today())

    def apply_proposal(self, plan: dict, hotel: str = '') -> tuple[bool, str]:
        """把提案逐日写入（每天一份行程）；`hotel` 给定时所有天都设为住宿锚点。

        用户说"起得晚"时，把 `day_start` 一并落成提案里的起床时刻——
        否则每天还是 08:00 起排，等于没听他的话。
        """
        days = (plan or {}).get('days') or []
        if not days:
            return False, '没有可写入的草案'
        # 没显式给住宿时，用草案里推荐的那家（否则"从酒店出发/回酒店"两段都算不出来）
        hotel = hotel or str((plan or {}).get('hotel_pick') or '')
        wake = str((plan or {}).get('wake') or '')
        day_end = str((plan or {}).get('day_end') or '')
        overrides = {}
        if wake:
            overrides['day_start'] = wake
        if day_end:
            overrides['day_end'] = day_end
        written = 0
        skipped: list[str] = []
        kept_before = 0          # 这些天原本已有多少事项（写入是追加，不是覆盖）
        skipped_items: list[str] = []
        with self._temporary_settings(**overrides):
            for d in days:
                try:
                    day = date.fromisoformat(str(d.get('date')))
                except ValueError:
                    continue
                items = [i for i in d.get('items') or [] if i.get('poi')]
                if not items:
                    continue
                kept_before += len(self.day_view(day).tasks)
                ok, detail = self.add_batch(day, items, origin=hotel or None,
                                            keep_order=True)
                if '跳过' in (detail or ''):
                    skipped_items.append(str(d.get('date')))
                if ok:
                    written += 1
                else:
                    skipped.append(str(d.get('date')))
        msg = f'已写入 {written} 天行程'
        if kept_before:
            msg += f'（这些天原有 {kept_before} 项安排已保留，本次是追加而非覆盖）'
        if skipped_items:
            msg += f'；{len(skipped_items)} 天有重复地点被跳过'
        if hotel:
            msg += f'，住宿锚点统一设为「{hotel}」'
        if wake:
            msg += f'，可排起点 {wake}'
        if skipped:
            msg += f'；{len(skipped)} 天写入失败（{"、".join(skipped)}）'
        return written > 0, msg

    def fit_day(self, plan_date: date) -> dict:
        """一天装不下时，给一份"按优先级取舍"的清单，并真跑一遍模拟证明排得下。

        排序依据（先看用户的意思，再看客观成本）：
          ① 用户标注"必须去"的一律保留；
          ② 停留时长越长越可能是主目标（博物馆 2 小时 > 路过打卡 30 分钟）；
          ③ 离住处/起点越远越"来都来了"，越不该被砍。
        返回 {'ok', 'drop': [标题], 'kept': [标题], 'why': {...}, 'end', 'unscheduled'}
        """

        trip = self.day_view(plan_date)
        opts = self.options(lodging=trip.lodging, plan_date=plan_date)
        tasks = list(trip.tasks)
        if not tasks:
            return {'ok': False, 'drop': [], 'kept': [], 'why': {},
                    'message': '当天没有安排'}

        def sim(keep: list) -> tuple[int, str]:
            trial = [t for t in tasks if t in keep]
            r = engine_mod.schedule(trial, opts, self.transit.as_fn(),
                                    self._mode_pick())
            end = r.items[-1].end_min if r.items else 0
            return len(r.unscheduled), hhmm(end) if end else ''

        base_uns, _ = sim(tasks)
        if base_uns == 0:
            return {'ok': True, 'drop': [], 'kept': [t.title for t in tasks],
                    'why': {}, 'message': '当天全部排得下，不需要取舍'}

        home = trip.lodging
        rank: list[tuple[float, object]] = []
        total = len(tasks)
        for idx, t in enumerate(tasks):
            why = []
            score = 0.0
            text = f'{getattr(t, "raw_text", "") or ""}{t.title}'
            if any(w in text for w in self._MUST_WORDS):
                score += 1000
                why.append('你标注了必去')
            score += min(120, int(t.duration_min)) / 2      # 停留越长越重要
            why.append(f'停留 {t.duration_min} 分钟')
            if opts.keep_order:
                # 用户给了顺序时，越靠前越像"先办正事"：舍弃从队尾开始
                score += (total - idx) * 3
                why.append(f'顺序第 {idx + 1} 位')
            if t.poi is not None and home is not None:
                try:
                    d = self.transit.estimate(home, t.poi) or {}
                    km = (d.get('distance_m') or 0) / 1000.0
                    score += min(30.0, km)                  # 越远越值得留
                    if km:
                        why.append(f'距住处 {km:.1f} 公里')
                except Exception:                       # noqa: BLE001
                    pass
            rank.append((score, t, why))
        rank.sort(key=lambda x: x[0])          # 分数最低的先砍

        keep = list(tasks)
        dropped: list = []
        why_map: dict[str, str] = {}
        for score, t, why in rank:
            if not any(t is k for k in keep):
                continue
            uns, _ = sim(keep)
            if uns == 0:
                break
            keep = [k for k in keep if k is not t]
            dropped.append(t)
            why_map[t.title] = '；'.join(why)
        uns_left, end = sim(keep)
        keep_titles = [t.title for t in keep]
        return {'ok': uns_left == 0,
                'drop': [t.title for t in dropped],
                'kept': keep_titles,
                'why': why_map,
                'end': end,
                'unscheduled': uns_left,
                'message': (f'砍掉 {len(dropped)} 项后当天排得下（末项 {end} 结束）'
                            if uns_left == 0 else
                            f'即便砍到 {len(keep_titles)} 项仍有 {uns_left} 项排不下')}

    # ---------- 天气（仅预报窗口内；窗口外/离线一律 None，不猜） ----------
    def day_weather(self, plan_date: date) -> Optional[dict]:
        """当日天气。高德只给今天+3天，窗口外或离线返回 None，由界面明示"无预报"。

        缓存按**城市**而不是按日期：一次请求就拿到今天+3 整段预报，
        原来按日期缓存会导致"每切一天就重新拉一次天气"（实测每次 0.7–0.8 秒，
        这正是切日期卡的真正原因）。
        """
        cache = getattr(self, '_weather_cache', None)
        if cache is None:
            cache = {}
            self._weather_cache = cache
        key = (self.city, plan_date.isoformat())
        if key in cache:
            return cache[key]
        # 城市级原始预报缓存（30 分钟过期）
        now = _time.time()
        raw = cache.get((self.city, '__casts__'))
        if not raw or now - raw[0] > 1800:
            casts: list = []
            fn = getattr(self.degrade.map(), 'weather', None)
            if fn is not None and self.degrade.map_ready():
                try:
                    casts = fn(get_city(self.city).adcode or '') or []
                except Exception as exc:        # noqa: BLE001 天气失败不影响排程
                    log.warning('天气取数失败：%s', exc)
                    casts = []
            cache[(self.city, '__casts__')] = (now, casts)
        else:
            casts = raw[1]
        val = None
        if True:
            cast = weather_mod.forecast_for(casts or [], plan_date)
            if cast:
                val = {'date': str(cast.get('date'))[:10],
                       'text': weather_mod.summary(cast),
                       'wet': weather_mod.is_wet(cast),
                       'day_weather': cast.get('dayweather') or '',
                       'night_weather': cast.get('nightweather') or '',
                       'day_temp': cast.get('daytemp') or '',
                       'night_temp': cast.get('nighttemp') or ''}
        cache[key] = val
        return val

    def _weather_notice(self, plan_date: date, trip: Trip) -> list[Conflict]:
        """天气提示：有雨雪就提醒，且不删任何点位；无预报就明说没有"""
        wx = self.day_weather(plan_date)
        if wx is None:
            if not self.degrade.is_offline():
                return [Conflict(
                    kind='weather_unknown', task_title='天气',
                    detail=f'{plan_date.month}月{plan_date.day}日不在 4 天预报窗口内，'
                           '暂时没有天气数据（不猜测）')]
            return []
        if not wx.get('wet'):
            return []
        outdoor = [t.title for t in trip.tasks
                   if t.poi is not None and not t.poi.indoor]
        indoor = [t.title for t in trip.tasks
                  if t.poi is not None and t.poi.indoor]
        if not outdoor:
            return [Conflict(kind='weather_wet', task_title='天气',
                             detail=f"当天{wx['text']}，行程以室内为主，影响不大")]
        tail = (f'；室内点位（{"、".join(indoor[:2])}）已优先安排'
                if indoor else '；当天没有室内点位，建议带伞或考虑改期')
        return [Conflict(
            kind='weather_wet', task_title='天气',
            detail=(f"当天{wx['text']}，有 {len(outdoor)} 个室外点位"
                    f"（{'、'.join(outdoor[:3])}）{tail}"),
            suggestions=[Suggestion(kind='keep',
                                    text='带上雨具；或把室外点位换到雨停的时段')])]

    # ---------- 查询 ----------
    def day_view(self, plan_date: date) -> Trip:
        """取当前城市当日的行程（一城一日一份）"""
        trip = self.trip_repo.get_day(plan_date, self.city)
        if trip is None:
            trip = Trip(plan_date=plan_date,
                        title=f'{plan_date.month}月{plan_date.day}日行程',
                        city=self.city)
        trip.city = self.city
        return trip

    def list_versions(self, trip_id: int):
        return self.version_repo.list(trip_id)

    # ---------- 住宿锚点（可随时设定/清除） ----------
    def set_lodging(self, plan_date: date, name: str) -> PlanOutcome:
        """把某地点设为当日"住"的锚点：出门从此出发，末项后留回程时间。

        典型用法是一句话录入"住在王府井希尔顿"，不必先选酒店再排行程。
        """
        poi = self._poi_named(name)
        if poi is None:
            return PlanOutcome(ok=False,
                               error=f'未能在{self.city}内找到住宿地点「{name}」')
        trip = self.day_view(plan_date)
        trip.lodging = poi
        outcome = self._schedule_and_save(trip, f'设定住宿：{poi.name}')
        if outcome.ok:
            outcome.explanation = f'已把「{poi.name}」设为出发点与回程终点。' + \
                (outcome.result.explanation if outcome.result else '')
        return outcome

    def clear_lodging(self, plan_date: date) -> PlanOutcome:
        trip = self.day_view(plan_date)
        if trip.lodging is None:
            return PlanOutcome(ok=True, trip=trip, explanation='当天未设定住宿')
        trip.lodging = None
        return self._schedule_and_save(trip, '清除住宿锚点')

    def _poi_named(self, name: str) -> Optional[Poi]:
        """按名称/别名解析一个城市包内的地点（不依赖网络），并用真实情报覆盖种子数据

        城市包里的营业时间/推荐时长只是演示种子；在线时用 `PoiIntelService`
        取到的真实值覆盖它（取不到就保持原样），所以"规划时用的就是真数据"。
        """
        pack = get_city(self.city)
        data = pack.find(name)
        # 别名必须是"专名"（南博、南邮），不能是通用词（"博物院"是南京博物院的别名，
        # 但它也是"故宫博物院"的后缀，认了就等于把跨城地点悄悄换成本地景点）
        if data is not None and str(name or '').strip() in GENERIC_TAILS:
            log.info('通用词别名不采信：%s', name)
            data = None
        if data is None:
            # 「三山街地铁站」/「夫子庙（景区）」这类带后缀的说法，先规范化再查一次
            plain = re.sub(r'[（(][^）)]*[）)]', '', str(name or '')).strip()
            plain = re.sub(r'(地铁站|公交站|火车站|车站|站)$', '', plain).strip()
            if plain and plain != str(name or '').strip():
                data = pack.find(plain)
                if data is None:
                    data = pack.find(f'{plain}(地铁站)')
                if data is None:
                    data = pack.find(f'{plain}站')
        if data is None and isinstance(name, str):
            # "栖霞区南京邮电大学"这类带行政区前缀的说法：逐段剥前缀再查
            for cut in range(1, min(5, len(name))):
                trimmed = name[cut:]
                if len(trimmed) < 2:
                    break
                data = pack.find(trimmed) or pack.find(normalize_mention(trimmed))
                if data is not None and name_alike(trimmed, str(data.get('name') or '')):
                    name = trimmed      # 后续相似度校验也按剥过的说法比
                    break
                data = None
        if data is None:
            # 地图/离线检索拿到的候选必须过两道闸：城市要一致、名字要真的像，
            # 否则"故宫博物院"会被检索结果里的"南京博物院"顶替（跨城误配）
            poi = None
            for cand in self.geo_svc.resolve(name) or []:
                if cand is None:
                    continue
                if getattr(cand, 'city', '') and cand.city != self.city:
                    log.debug('跨城候选已丢弃：%s（%s）', cand.name, cand.city)
                    continue
                if not self._name_alike(name, cand.name):
                    log.debug('名称不像已丢弃：%s ≠ %s', name, cand.name)
                    continue
                poi = cand
                break
        else:
            poi = Poi.from_pack(data, self.city)
            self.trip_repo.ensure_poi(poi)
        intel = getattr(self, 'intel', None)
        if intel is not None and poi is not None:
            try:
                intel.apply(poi)
            except Exception as exc:                 # noqa: BLE001 情报失败不影响排程
                log.debug('景点情报应用失败 %s：%s', poi.name, exc)
        return poi

    def compute_day(self, plan_date: date) -> ScheduleResult:
        """只读计算当日排程（用于界面展示，不落库）"""
        trip = self.day_view(plan_date)
        opts = self.options(lodging=trip.lodging, plan_date=plan_date)
        result = engine_mod.schedule(trip.tasks, opts, self.transit.as_fn(),
                                     self._mode_pick())
        attach_suggestions(result, opts)
        self._attach_notices(trip, result, opts, plan_date)
        return result

    def _attach_notices(self, trip: Trip, result: ScheduleResult,
                        opts: ScheduleOptions, plan_date: date) -> None:
        """营业时间/闭馆日/预约/饭点 + 过夜住宿建议（软提示，不影响可行性）"""
        notices = hours_mod.collect(trip.tasks, result, plan_date, opts)
        notices.extend(self._weather_notice(plan_date, trip))
        notices.extend(self._budget_notices(trip))
        # 节假日：人流与预约预警（只提示，不改用户选择）
        warn = holidays_mod.holiday_warning(plan_date)
        if warn:
            notices.append(Conflict(
                kind='holiday', task_title='节假日', detail=warn,
                suggestions=[Suggestion(kind='book',
                                        text='提前在线预约/购票，并预留更多路上时间')]))
        # 过去日期：明确告警，别让用户以为在排"今天"
        today = date.today()
        if plan_date < today:
            notices.insert(0, Conflict(
                kind='past_date', task_title='日期',
                detail=(f'{plan_date.month}月{plan_date.day}日已经过去'
                        f'（今天是 {today.month}月{today.day}日），'
                        '这份行程只能作为记录查看，不能再当天执行'),
                suggestions=[Suggestion(kind='keep',
                                        text='如需继续规划，请把日期改到今天或之后')]))
        poi_map = {t.title: t.poi for t in trip.tasks if t.poi is not None}
        # ---------- 过夜检测：末项太晚或异地行程时，主动给住宿选项 ----------
        if not trip.lodging_declined:
            try:
                nxt = self.day_view(plan_date + timedelta(days=1))
                nxt_result = engine_mod.schedule(
                    nxt.tasks, self.options(lodging=nxt.lodging),
                    self.transit.as_fn(), self._mode_pick())
            except Exception as exc:      # noqa: BLE001 次日读取失败不影响当日
                log.debug('次日行程读取失败：%s', exc)
                nxt_result = None
            home = normalize_city(getattr(self.settings, 'home_city', '') or
                                  DEFAULT_CITY)
            notice = lodging_mod.overnight_notice(
                get_city(self.city), result, nxt_result,
                lodging=trip.lodging, away_from_home=(self.city != home))
            if notice is not None:
                notices.append(notice)
        if trip.lodging is not None:
            poi_map.setdefault('住宿', trip.lodging)
        advise_notices(notices, plan_date, poi_map)
        result.notices = notices

    # ---------- 住宿建议（过夜时主动给出候选） ----------
    def recommend_lodging(self, plan_date: date, limit: int = 3) -> list[dict]:
        """返回 [{'name','star','price','reason','distance_km'}...]，供界面直接展示"""
        trip = self.day_view(plan_date)
        opts = self.options(lodging=trip.lodging, plan_date=plan_date)
        result = engine_mod.schedule(trip.tasks, opts, self.transit.as_fn(),
                                     self._mode_pick())
        nxt = self.day_view(plan_date + timedelta(days=1))
        nxt_result = engine_mod.schedule(nxt.tasks, opts, self.transit.as_fn(),
                                         self._mode_pick())
        recs = lodging_mod.recommend(get_city(self.city),
                                     lodging_mod._anchor_points(result, nxt_result),
                                     limit=limit)
        return [{'name': r['poi'].name, 'star': r['poi'].star,
                 'price': r['poi'].price, 'hours': r['poi'].hours_label(),
                 'reason': r['reason'], 'distance_km': r['distance_km']}
                for r in recs]

    def decline_lodging(self, plan_date: date, declined: bool = True) -> PlanOutcome:
        """明确"当天往返、不需要住宿"，之后不再重复推荐"""
        trip = self.day_view(plan_date)
        trip.lodging_declined = declined
        if declined:
            trip.lodging = None
        return self._schedule_and_save(
            trip, '标记当天不需住宿' if declined else '恢复住宿建议')


    # ---------- 用例 UC-01：预览解析结果（不落库） ----------
    _LODGING_RE = re.compile(r'住(?:在|到|于)?\s*([\u4e00-\u9fa5A-Za-z0-9]{2,20})')

    def _lodging_mention(self, text: str) -> str:
        """从一句话里识别"住在某处"的住宿锚点（返回城市包中的标准名）"""
        pack = get_city(self.city)
        for m in self._LODGING_RE.finditer(text or ''):
            name = m.group(1)
            data = pack.find(name)
            if data and data.get('kind') == '住宿':
                return data['name']
            for p in pack.pois:
                if p.get('kind') != '住宿':
                    continue
                if name in p['name'] or any(name in a for a in p.get('aliases', ())):
                    return p['name']
        return ''

    def preview_text(self, text: str, plan_date: date) -> PlanOutcome:
        t0 = _time.perf_counter()
        city = self.city
        outcome = PlanOutcome(trip=self.day_view(plan_date))
        intent = self.parse_svc.parse(text, plan_date)
        outcome.intent = intent

        # ---------- 0. 住宿锚点：一句话设定"住哪"，不产生事项 ----------
        lodging_name = self._lodging_mention(text)
        if lodging_name:
            poi = self._poi_named(lodging_name)
            if poi is not None:
                outcome.ok = True
                outcome.lodging = poi
                outcome.explanation = (f'识别为住宿锚点：「{poi.name}」'
                                       f'（{poi.brief()}），'
                                       '将作为当日出发地与回程终点')
                outcome.elapsed_ms = int((_time.perf_counter() - t0) * 1000)
                return outcome

        # ---------- 1. 地点解析 ----------
        poi: Optional[Poi] = None
        if intent.mention:
            candidates = self.geo_svc.resolve(intent.mention)
            outcome.candidates = candidates
            if candidates:
                poi = candidates[0]

        # ---------- 2. 缺失字段（吃/住类可按默认时长兜底） ----------
        missing = list(intent.missing)
        if poi is not None and '耗时' in missing:
            default_min = (self._default_duration(poi) if poi.is_food
                           else (30 if poi.is_lodging else self._default_duration(poi)))
            if default_min:
                intent.duration_min = default_min
                missing = [m for m in missing if m != '耗时']
                # 用推荐时长兜底必须说清楚来源，别让用户以为是自己填的
                if poi.visit_min:
                    outcome.notices.append(Conflict(
                        kind='duration_default', task_title=poi.name,
                        detail=f'没写停留时长，按城市包推荐「{poi.name} {hm_text(poi.visit_min)}」计'
                               f'（可在预览卡里改）'))
        # 午休/吃饭这类"只占时间、不指定地点"的块：允许没有地点。
        # 否则只能把午饭挂到某个 POI 上，引擎会为了"最短路"把它排到行程末尾，
        # 造成几十公里的折返（11-18 案例实测 15.8 km）。
        if poi is None and '地点' in missing and is_time_block_text(text):
            intent.duration_min = intent.duration_min or 60
            intent.category = '餐饮'
            missing = [m for m in missing if m != '地点']
        if missing:
            outcome.ok = False
            outcome.missing = missing
            outcome.error = '缺少必要信息：' + '、'.join(missing)
            outcome.elapsed_ms = int((_time.perf_counter() - t0) * 1000)
            return outcome

        # ---------- 3. 地点必须存在且属于当前城市（BR-01 参数化） ----------
        if intent.mention:
            if not outcome.candidates:
                outcome.ok = False
                outcome.error = f'未能在{city}内找到地点「{intent.mention}」，请确认描述'
                outcome.elapsed_ms = int((_time.perf_counter() - t0) * 1000)
                return outcome
            if not poi.in_city(city):
                outcome.ok = False
                outcome.error = (f'「{poi.name}」位于{poi.district}，'
                                 f'当前城市为{city}，'
                                 f'请先切换到对应城市（BR-01）')
                outcome.elapsed_ms = int((_time.perf_counter() - t0) * 1000)
                return outcome

        # ---------- 3.5 别把"出发点"当成要去的地点 ----------
        # 句子是"我在X，然后去A、B、C"时，若只解析出一条"在X出发"的任务，说明意图拆错了：
        # 直接说清楚，而不是写一条"出行起点 60 分钟"进库。
        if poi is not None:
            from .parsing_rules import extract_origin
            origin_name, _ = extract_origin(text or '')
            if (origin_name and origin_name.strip() in poi.name
                    and str(intent.item or '').strip() in _ORIGIN_ITEMS):
                outcome.ok = False
                outcome.error = (f'「{poi.name}」在这句话里是出发点，不是目的地；'
                                 '请把要去的地方也说出来，例如"然后去鸡鸣寺50分钟"')
                outcome.elapsed_ms = int((_time.perf_counter() - t0) * 1000)
                return outcome

        # 绝对截止（"10:30 前必须抵达"）里的时间不是出发时刻：
        # 若把同一个时间同时当成 start_hint，就会锁死在截止点上并与前一项重叠。
        deadline = _deadline_of(text)
        start_hint = None if deadline is not None else intent.start_hint
        draft = Task(title=title_with_poi(intent.item, poi),
                     duration_min=max(10, intent.duration_min),
                     category=intent.category, desired_start=start_hint,
                     poi=poi, raw_text=text, is_locked=start_hint is not None,
                     status=TaskStatus.UNSCHEDULED, mode=intent.mode_hint,
                     deadline=deadline)
        if intent.date and intent.date != plan_date:
            draft.trip_id = None
        outcome.draft = draft
        outcome.trip.tasks.append(draft)

        # ---------- 4. 营业提示：预览阶段就拦住"周一去故宫" ----------
        notices: list[Conflict] = []
        target_date = intent.date or plan_date
        detail = hours_mod.closed_detail(poi, target_date)
        if detail:
            notices.append(Conflict(kind='closed_day', detail=detail,
                                    task_title=intent.item))
        if poi is not None and poi.book_hint:
            notices.append(Conflict(
                kind='need_booking',
                detail=f'「{poi.name}」{poi.book_hint}',
                task_title=intent.item))
        if notices:
            poi_map = {intent.item: poi} if poi is not None else {}
            advise_notices(notices, target_date, poi_map)
            # 保留前面已经产生的说明（比如"按城市包推荐时长兜底"）
        outcome.notices = list(outcome.notices) + list(notices)
        outcome.ok = True
        outcome.elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        return outcome

    # ---------- 用例 UC-01：确认并排程（落库 + 版本） ----------
    def commit_task(self, plan_date: date, task: Task, note: str = '新增安排') -> PlanOutcome:
        t0 = _time.perf_counter()
        # 锁定语义：用户给了期望开始时刻即视为锁定，必须同步落 start_ts，
        # 否则引擎会把它当成"待排任务"重新择时（与用户意图不符）
        if task.is_locked and task.start_ts is None and task.desired_start is not None:
            task.start_ts = task.desired_start
        trip = self.day_view(plan_date)
        trip.add_task(task)
        outcome = self._schedule_and_save(trip, note)
        outcome.elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        return outcome

    def add_text(self, text: str, plan_date: date) -> PlanOutcome:
        """预览 + 直接提交（供脚本与测试使用）"""
        prev = self.preview_text(text, plan_date)
        if not prev.ok or prev.draft is None:
            return prev
        return self.commit_task(plan_date, prev.draft, note=f'新增：{prev.draft.title}')

    # ---------- 用例 UC-08：调整行程 ----------
    def delay_task(self, task_id: int, delay_min: int,
                   keep_start: bool = True) -> PlanOutcome:
        """某项多花了 N 分钟：保持开始时刻不变、把结束（时长）往后推，再链式重排后续。

        这是"动态变更"的核心动作——现场延误后不该重排整天的顺序，
        而是把这一项之后的所有节点整体顺延。`keep_start=True` 时该事项被锁定在
        原开始时刻（用户已经到那儿了），其余未锁定项由引擎重新贴合。
        """
        trip, task = self._locate(task_id)
        if trip is None or task is None:
            return PlanOutcome(ok=False, error='未找到该安排')
        add = max(0, int(delay_min))
        if add == 0:
            return PlanOutcome(ok=False, error='延误分钟数必须大于 0')
        if keep_start and task.start_ts is not None:
            task.is_locked = True          # 已经开始的事项钉在原开始时刻
        task.duration_min = int(task.duration_min) + add
        outcome = self._schedule_and_save(trip, f'「{task.title}」延误 {add} 分钟，后续顺延')
        return outcome

    def adjust(self, task_id: int, patch: dict) -> PlanOutcome:
        t0 = _time.perf_counter()
        trip, task = self._locate(task_id)
        if trip is None or task is None:
            return PlanOutcome(ok=False, error='未找到该安排')

        if 'start_ts' in patch and patch['start_ts']:
            from datetime import time as _t
            h, m = str(patch['start_ts']).split(':')
            task.start_ts = _t(int(h), int(m))
            task.is_locked = True                 # 用户指定时刻即锁定（BR-02 例外）
        if 'duration_min' in patch and patch['duration_min']:
            task.duration_min = max(10, int(patch['duration_min']))
        if 'title' in patch and patch['title']:
            task.title = str(patch['title'])
        if 'is_locked' in patch:
            task.is_locked = bool(patch['is_locked'])
        if 'mode' in patch:
            v = patch['mode']
            task.mode = TransitMode(v) if v else None
        if patch.get('unlock'):
            task.is_locked = False
            task.start_ts = None
            task.end_ts = None

        outcome = self._schedule_and_save(trip, '调整安排')
        outcome.elapsed_ms = int((_time.perf_counter() - t0) * 1000)
        return outcome

    # ---------- 用例 UC-09：删除 ----------
    def drop_places(self, days: list[date], names: list[str]) -> list[str]:
        """把某几天的这些地点删掉（"把夫子庙去掉"用），返回真正删掉的"日期 地点"。

        只按地点名匹配，找不到就跳过——绝不因为用户提了个地名就删别的东西。
        """
        targets = {(n or '').strip() for n in names if (n or '').strip()}
        if not targets:
            return []
        removed: list[str] = []
        for day in days:
            trip = self.day_view(day)
            hit = [t for t in trip.tasks
                   if t.poi is not None and any(k in t.poi.name or t.poi.name in k
                                                for k in targets)]
            for task in hit:
                if task.task_id is None:
                    continue
                out = self.delete(task.task_id)
                if out.ok:
                    removed.append(f'{day.isoformat()} {task.poi.name}')
        return removed

    def delete(self, task_id: int) -> PlanOutcome:
        trip, task = self._locate(task_id)
        if trip is None or task is None:
            return PlanOutcome(ok=False, error='未找到该安排')
        trip.remove_task(task_id)
        return self._schedule_and_save(trip, f'删除：{task.title}')

    def mark_done(self, task_id: int) -> PlanOutcome:
        trip, task = self._locate(task_id)
        if trip is None or task is None:
            return PlanOutcome(ok=False, error='未找到该安排')
        task.status = TaskStatus.DONE
        return self._schedule_and_save(trip, f'完成：{task.title}')

    # ---------- 用例 UC-10：重新规划 / 回滚 ----------
    def clear_task_modes(self, plan_date: date) -> None:
        """清掉当日各段的"已选方式"，让排程回到全局默认方式。

        关闭"每段自动择优"时调用：否则上次择优写回的方式会被当成用户手选而继续生效。
        """
        trip = self.trip_repo.get_day(plan_date, self.city)
        if trip is None:
            return
        for t in trip.tasks:
            t.mode = None
        self._schedule_and_save(trip, '恢复默认方式')

    def replan(self, plan_date: date) -> PlanOutcome:
        trip = self.trip_repo.get_day(plan_date, self.city)
        if trip is None:
            return PlanOutcome(ok=False, error='该日暂无行程')
        return self._schedule_and_save(trip, '重新规划（保留锁定项）')

    def rollback(self, trip_id: int, version_id: int) -> PlanOutcome:
        ver = self.version_repo.get(version_id)
        plan_date = self.db.conn.execute(
            'SELECT plan_date, city FROM trip WHERE trip_id = ?',
            (trip_id,)).fetchone()
        if not ver or not plan_date:
            return PlanOutcome(ok=False, error='版本不存在')
        trip = self.trip_repo.get_day(date.fromisoformat(plan_date['plan_date']),
                                      plan_date['city'] or None)
        if trip is None:
            return PlanOutcome(ok=False, error='行程不存在')
        data = json.loads(ver.snapshot_json)
        by_title = {t.title: t for t in trip.tasks}
        for row in data.get('items', []):
            t = by_title.get(row['title'])
            if t is None:
                continue
            from datetime import time as _t
            t.start_ts = _t(*map(int, row['start_ts'].split(':'))) if row.get('start_ts') else None
            t.end_ts = _t(*map(int, row['end_ts'].split(':'))) if row.get('end_ts') else None
            t.seq = row.get('seq', 0)
            t.is_locked = row.get('is_locked', False)
            t.status = TaskStatus(row.get('status', 'scheduled'))
            # 老快照没有这两项：有才恢复，没有就保持原值（向后兼容）
            dur = row.get('duration_min')
            if dur:
                t.duration_min = int(dur)
            if 'mode' in row:
                t.mode = TransitMode(row['mode']) if row.get('mode') else None
        return self._schedule_and_save(trip, f'回滚到版本 {version_id}')

    # ---------- 公交换乘方案详情 ----------
    def transit_plans_for(self, task_id: int) -> tuple[list[dict], str]:
        """查询"从上一项的地点到此项地点"的公交换乘方案详情"""
        trip, task = self._locate(task_id)
        if trip is None or task is None:
            return [], '未找到该安排'
        if task.poi is None:
            return [], '该安排没有地点，无法查询公交方案'
        scheduled = sorted([t for t in trip.tasks
                            if t.start_ts is not None and t.poi is not None],
                           key=lambda t: t.start_ts)  # type: ignore[arg-type,return-value]
        prev = None
        for t in scheduled:
            if t.task_id == task.task_id:
                break
            prev = t
        if prev is None:
            return [], '这是当天第一项，前面没有可出发的地点'
        plans, note = self.transit.transit_plans(prev.poi, task.poi)
        if plans:
            from .traffic import annotate_plans
            now_min = minutes_of(task.start_ts) if task.start_ts \
                else minutes_of(datetime.now().time())
            annotate_plans(plans, now_min)
            note = (note + '　' if note else '') + \
                '班次为按经验发车间隔推算（非实时到站数据）'
        return plans, note

    # ---------- 批量删除 / 撤销 ----------
    @staticmethod
    def _snapshot(t: Task) -> dict:
        """删除前留快照，供"撤销"使用（仅内存，不落库）"""
        return {'title': t.title, 'duration_min': t.duration_min,
                'category': t.category, 'poi': t.poi,
                'raw_text': t.raw_text, 'is_locked': False,
                'mode': t.mode, 'desired_start': t.desired_start}

    def delete_many(self, task_ids: list[int]) -> tuple[PlanOutcome, list[dict]]:
        """批量删除，返回 (结果, 快照列表)。快照用于撤销。"""
        ids = [int(i) for i in task_ids if i]
        if not ids:
            return PlanOutcome(ok=False, error='未选择任何安排'), []
        trip, _ = self._locate(ids[0])
        if trip is None:
            return PlanOutcome(ok=False, error='未找到行程'), []
        snaps: list[dict] = []
        for tid in ids:
            t = next((x for x in trip.tasks if x.task_id == tid), None)
            if t is None:
                continue
            snaps.append(self._snapshot(t))
            trip.remove_task(tid)
        if not snaps:
            return PlanOutcome(ok=False, error='这些安排已不存在'), []
        out = self._schedule_and_save(trip, f'删除 {len(snaps)} 项')
        return out, snaps

    def restore_many(self, snaps: list[dict], plan_date: date) -> PlanOutcome:
        """撤销删除：把快照重新插回当天并重排"""
        if not snaps:
            return PlanOutcome(ok=False, error='没有可恢复的安排')
        trip = self.day_view(plan_date)
        for s in snaps:
            trip.add_task(Task(**s))
        return self._schedule_and_save(trip, f'恢复 {len(snaps)} 项')

    def set_mode_many(self, task_ids: list[int], mode: str) -> PlanOutcome:
        """批量改这些段的交通方式"""
        ids = [int(i) for i in task_ids if i]
        if not ids:
            return PlanOutcome(ok=False, error='未选择任何安排')
        trip, _ = self._locate(ids[0])
        if trip is None:
            return PlanOutcome(ok=False, error='未找到行程')
        m = TransitMode(mode) if mode else None
        count = 0
        for tid in ids:
            t = next((x for x in trip.tasks if x.task_id == tid), None)
            if t is not None:
                t.mode = m
                count += 1
        return self._schedule_and_save(trip, f'{count} 项改为{m.cn if m else "默认"}方式')

    # ---------- 具体路径详情（含接驳拼接） ----------
    @staticmethod
    def _xy(loc: str):
        try:
            lng, lat = str(loc).split(',')
            return float(lng), float(lat)
        except (ValueError, AttributeError):
            return None

    def _connector(self, origin_xy, dest_xy, to_name: str,
                   mode: str = 'walking') -> Optional[dict]:
        """一段接驳：**按指定方式**计算，不再拿骑行和步行比快慢。

        接驳方式是用户的决策（例如"骑车到地铁站、下车后走路到医院"），
        系统不得因为骑行更快就把它替换成骑行——那等于替用户改了出行方式。
        """
        fn = getattr(self.degrade.map(), 'route_steps', None)
        if fn is None or origin_xy is None or dest_xy is None or not mode:
            return None
        cn = {'bicycling': '骑行', 'walking': '步行'}.get(mode, mode)
        try:
            r = fn(origin_xy, dest_xy, mode)
        except MapError:
            r = None
        if not r:
            return None
        return {'kind': 'connector', 'mode_cn': cn, 'mode': mode,
                'minutes': r['duration_min'], 'distance_m': r['distance_m'],
                'text': f'{cn}到 {to_name}'.strip()}

    def leg_detail_for(self, task_id: int, mode: Optional[str] = None,
                       access: str = 'bicycling',
                       egress: str = 'walking') -> tuple[dict, str]:
        """给出"从上一处到这一处"的具体走法。

        :param access: 起点接驳方式（默认骑行，符合"骑车到地铁站"的常见做法）
        :param egress: 终点接驳方式（默认步行，因为车已停在起点站）
        两者都可显式切换，系统不会按"谁快"自动替换。
        """
        trip, task = self._locate(task_id)
        if trip is None or task is None:
            return {}, '未找到该安排'
        if task.poi is None:
            return {}, '该安排没有地点，无法显示路径'
        scheduled = sorted([t for t in trip.tasks
                            if t.start_ts is not None and t.poi is not None],
                           key=lambda t: t.start_ts)  # type: ignore[arg-type,return-value]
        prev = None
        for t in scheduled:
            if t.task_id == task.task_id:
                break
            prev = t
        if prev is None:
            return {}, '这是当天第一项，前面没有可出发的地点'
        if not self.degrade.map_ready():
            return {}, '离线模式：具体路径需联网向地图服务查询'

        m = TransitMode(mode) if mode else (task.mode or self.options().mode)
        detail: dict = {
            'ok': True, 'title': f'前往「{task.title}」',
            'from_name': prev.poi.name, 'to_name': task.poi.name,
            'mode_cn': m.cn, 'mode_key': m.value, 'segments': [],
            'note': '',
        }
        origin_xy = (prev.poi.lng, prev.poi.lat)
        dest_xy = (task.poi.lng, task.poi.lat)

        if m == TransitMode.TRANSIT:
            plans, note = self.transit.transit_plans(prev.poi, task.poi)
            if not plans:
                return {}, note or '未查到可行的公共交通方案'
            plan = plans[0]
            bus_legs = [l for l in plan.get('legs', []) if l['kind'] == 'bus']
            segs: list[dict] = []
            if bus_legs and access:
                acc = self._connector(origin_xy,
                                      self._xy(bus_legs[0].get('from_loc', '')),
                                      bus_legs[0].get('from_stop', '首站'),
                                      access)
                if acc:
                    acc['text'] = (f"{acc['mode_cn']}到 "
                                   f"{bus_legs[0].get('from_stop', '首站')}")
                    segs.append(acc)
            walks = [l for l in plan.get('legs', []) if l['kind'] == 'walk']
            for idx, leg in enumerate(plan.get('legs', [])):
                if leg['kind'] == 'walk':
                    continue        # 首尾步行由接驳段替代，中间换乘步行单独加
                segs.append({'kind': 'bus', 'mode_cn': leg.get('name', ''),
                             'minutes': leg.get('minutes', 0),
                             'distance_m': leg.get('distance_m', 0),
                             'text': (f"{leg.get('from_stop', '')} → "
                                      f"{leg.get('to_stop', '')}"
                                      f"（{leg.get('stops', 0)} 站）"),
                             'operating': (f"{leg.get('start_time', '')}–"
                                           f"{leg.get('end_time', '')}"
                                           if leg.get('start_time') else '')})
            if bus_legs and egress:
                egr = self._connector(
                    self._xy(bus_legs[-1].get('to_loc', '')), dest_xy,
                    task.poi.name, egress)
                if egr:
                    egr['text'] = (f"{egr['mode_cn']}到医院/目的地"
                                   f"（{task.poi.name}）")
                    segs.append(egr)
            transfer_walk = sum(l.get('distance_m', 0)
                                for l in walks[1:-1]) if len(walks) > 2 else 0
            detail['segments'] = segs
            detail['cost'] = plan.get('cost', '')
            detail['access'] = access
            detail['egress'] = egress
            detail['note'] = (
                '接驳方式按你选定的：起点%s、终点%s（由真实路径接口计算，'
                '不按快慢自动替换）；中间换乘步行合计约 %d 米；'
                '班次为经验发车间隔推算，非实时到站'
                % ({'bicycling': '骑行', 'walking': '步行', '': '无'}.get(access, access),
                   {'bicycling': '骑行', 'walking': '步行', '': '无'}.get(egress, egress),
                   transfer_walk))
        else:
            fn = getattr(self.degrade.map(), 'route_steps', None)
            if fn is None:
                return {}, '当前地图适配器不支持分步路径'
            try:
                r = fn(origin_xy, dest_xy, m.value)
            except MapError as exc:
                return {}, f'路径查询失败：{exc}'
            if not r:
                return {}, '未查到可行路径'
            steps = r.get('steps') or []
            for st in steps[:12]:
                detail['segments'].append({
                    'kind': 'step', 'mode_cn': m.cn,
                    'minutes': st.get('minutes', 0),
                    'distance_m': st.get('distance_m', 0),
                    'text': st.get('instruction') or '直行',
                })
            detail['note'] = (f'共 {len(steps)} 个导航步骤'
                              + ('，此处只列前 12 步' if len(steps) > 12 else '')
                              + '；数据来自高德路径规划')

        detail['total_min'] = sum(s.get('minutes') or 0
                                  for s in detail['segments'])
        detail['total_distance_m'] = sum(s.get('distance_m') or 0
                                         for s in detail['segments'])
        return detail, ''

    # ---------- 位置示意图 ----------
    def day_map_points(self, plan_date: date) -> tuple[list[dict], str]:
        """取当天按时间排序的地点（**主线程调用**，因为要读数据库）

        界面在工作线程里只做网络请求，不能在这里访问 sqlite：
        否则会抛 "SQLite objects created in a thread can only be used in
        that same thread"。
        """
        trip = self.day_view(plan_date)
        items = sorted([t for t in trip.tasks if t.start_ts and t.poi],
                       key=lambda t: t.start_ts)  # type: ignore[arg-type,return-value]
        if not items:
            return [], '当天还没有带地点的安排'
        return [{'lng': t.poi.lng, 'lat': t.poi.lat, 'title': t.title,
                 'poi': t.poi.name,
                 'time': t.start_ts.strftime('%H:%M') if t.start_ts else ''}
                for t in items], ''

    #: 路径折线的进程内缓存（键 = 起终点坐标 + 方式）。地图每次重绘都要折线，
    #: 不加缓存会把接口打爆；**按实例持有**，避免不同 AppContext（不同库/不同
    #: 在线状态）之间互相污染——测试里踩过这个坑。
    _PATH_CACHE_MAX = 128

    @property
    def path_cache(self) -> dict:
        cache = getattr(self, '_path_cache', None)
        if cache is None:
            cache = {}
            self._path_cache = cache
        return cache

    #: 路径方案的进程内缓存（键 = 起终点 + 方式）
    _OPT_CACHE_MAX = 96

    @property
    def option_cache(self) -> dict:
        cache = getattr(self, '_option_cache_dict', None)
        if cache is None:
            cache = {}
            self._option_cache_dict = cache
        return cache

    def day_leg_options(self, plan_date: date, mode: str = '',
                        limit: int = 3) -> tuple[list[dict], str]:
        """当天每段路的**多个可选方案**（第 3 步"确认路径"用）。

        每个方案带自己的折线与耗时/里程，选定哪条就画哪条；离线或取不到时
        `options=[]`，由前端退回直线并标注估算。
        """
        points, note = self.day_map_points(plan_date)
        if len(points) < 2:
            return [], note or '当天不足两个地点，没有需要绘制的路段'
        valid = {m.value for m in TransitMode}
        raw = mode if mode in valid else (self.settings.transit_mode or 'driving')
        md = TransitMode(raw if raw in valid else 'driving')
        fn = getattr(self.degrade.map(), 'route_options', None)
        cache = self.option_cache
        out: list[dict] = []
        for a, b in zip(points, points[1:]):
            key = (round(a['lng'], 5), round(a['lat'], 5),
                   round(b['lng'], 5), round(b['lat'], 5), md.value, limit)
            opts = cache.get(key)
            if opts is None:
                opts = []
                if fn is not None and self.degrade.map_ready():
                    try:
                        opts = fn((a['lng'], a['lat']), (b['lng'], b['lat']),
                                  md.value, limit) or []
                    except Exception:            # noqa: BLE001 取不到就退回直线
                        opts = []
                if len(cache) >= self._OPT_CACHE_MAX:
                    cache.pop(next(iter(cache)))
                cache[key] = opts
            out.append({'from': a['title'], 'to': b['title'], 'mode': md.value,
                        'options': opts})
        return out, ''

    def point_audit(self, plan_date: date) -> list[dict]:
        """当日每个点位的"核对信息"（第 1 步"确认点"用）。

        给出：包内坐标、高德对该坐标的逆地理地址、以及按名称检索到的候选点位。
        用户据此可以判断"这个点是不是我想去的地方"，并按候选一键纠正。
        """
        points, _ = self.day_map_points(plan_date)
        if not points:
            return []
        fn_regeo = getattr(self.degrade.map(), 'regeo', None)
        fn_cand = getattr(self.degrade.map(), 'place_candidates', None)
        online = self.degrade.map_ready()
        out = []
        for i, p in enumerate(points, 1):
            item = {'seq': i, 'name': p['title'], 'time': p['time'],
                    'lng': p['lng'], 'lat': p['lat'], 'poi_name': p['title'],
                    'address': '', 'candidates': [], 'online': online}
            if online and fn_regeo is not None:
                try:
                    rg = fn_regeo(p['lng'], p['lat']) or {}
                    item['address'] = rg.get('address', '')
                except Exception:                # noqa: BLE001 地址拿不到不影响主流程
                    item['address'] = ''
            if online and fn_cand is not None:
                # 用**地点名**检索候选（事项标题往往是"吃饭/看展"，搜不出地名）
                kw = p.get('poi') or p['title']
                try:
                    cands = fn_cand(kw, self.city, 5) or []
                except Exception:                # noqa: BLE001
                    cands = []
                for c in cands:
                    c['distance_m'] = int(
                        geo_mod.distance_km(p['lng'], p['lat'], c['lng'], c['lat']) * 1000)
                cands.sort(key=lambda c: c['distance_m'])
                item['candidates'] = cands[:5]
                item['same_name_m'] = next(
                    (c['distance_m'] for c in cands if kw in (c['name'] or '')), None)
            # 更可靠的"点位可疑"判据：高德把这个坐标解析成什么地址，
            # 地址里如果连点位名都不出现，基本可以断定这个坐标不对（如"新街口"解析成写字楼）。
            item['address_mismatch'] = bool(
                item['address'] and p.get('poi') and p['poi'] not in item['address'])
            out.append(item)
        return out

    def pin_point(self, plan_date: date, task_id: int,
                  lng: float, lat: float) -> tuple[bool, str]:
        """把某个事项的地点钉到确认过的坐标，并重排当天（第 1 步的落库动作）。"""
        trip = self.trip_repo.get_day(plan_date, self.city)
        if trip is None:
            return False, '该日暂无行程'
        task = next((t for t in trip.tasks if t.task_id == int(task_id)), None)
        if task is None:
            return False, '找不到该事项'
        if task.poi is None:
            return False, '该事项没有关联地点'
        if not self.trip_repo.update_poi_coord(task.poi, lng, lat):
            return False, '地点尚未入库，无法钉住坐标'
        task.poi.lng, task.poi.lat = float(lng), float(lat)
        # 坐标已落库；replan 会重新读库并按新坐标重算各段通行时长
        self.replan(plan_date)
        self.option_cache.clear()          # 坐标变了，路径方案必须重取
        self.path_cache.clear()
        return True, '已钉住坐标并重新排程'

    def day_leg_paths(self, plan_date: date, mode: str = '') -> tuple[list[dict], str]:
        """当天相邻两点之间、**按指定通行方式**的真实走向折线。

        返回 `[{'from', 'to', 'mode', 'minutes', 'distance_m', 'lines', 'source'}]`。
        在线且取到几何时 `source='amap'`；离线或失败时 `lines=[]`，由前端退回
        直线示意并标注"估算"——不假装那是真实路径。
        """
        points, note = self.day_map_points(plan_date)
        if len(points) < 2:
            return [], note or '当天不足两个地点，没有需要绘制的路段'
        valid = {m.value for m in TransitMode}
        raw = mode if mode in valid else (self.settings.transit_mode or 'driving')
        md = TransitMode(raw if raw in valid else 'driving')
        out: list[dict] = []
        cache = self.path_cache
        fn = getattr(self.degrade.map(), 'route_polyline', None) if md else None
        for a, b in zip(points, points[1:]):
            key = (round(a['lng'], 5), round(a['lat'], 5),
                   round(b['lng'], 5), round(b['lat'], 5), md.value)
            hit = cache.get(key)
            if hit is None:
                hit = {'lines': [], 'distance_m': 0, 'minutes': 0, 'source': 'straight'}
                if fn is not None and self.degrade.map_ready():
                    try:
                        got = fn((a['lng'], a['lat']), (b['lng'], b['lat']), md.value)
                    except Exception:            # noqa: BLE001 取不到就退回直线
                        got = None
                    if got:
                        hit = {'lines': got['lines'], 'distance_m': got['distance_m'],
                               'minutes': got['minutes'], 'source': 'amap'}
                if len(cache) >= self._PATH_CACHE_MAX:
                    cache.pop(next(iter(cache)))
                cache[key] = hit
            out.append({'from': a['title'], 'to': b['title'], 'mode': md.value,
                        'minutes': hit['minutes'], 'distance_m': hit['distance_m'],
                        'lines': hit['lines'], 'source': hit['source']})
        return out, ''

    def static_map_bytes(self, center: tuple[float, float],
                         size: tuple[int, int] = (900, 380),
                         zoom: int = 12, scale: int = 1) -> tuple[bytes, str]:
        """仅取底图（**可在线程中调用**，不碰数据库）

        `scale=2` 让高德出 2 倍像素（如 1024×640 → 2048×1280），
        大屏/高分屏下的地图页靠它才不糊。
        """
        if not self.degrade.map_ready():
            return b'', '离线模式：位置示意图需联网获取（不影响排程使用）'
        fn = getattr(self.degrade.map(), 'static_map_base', None)
        if fn is None:
            return b'', '当前地图适配器不支持静态地图'
        try:
            data = fn(center, size=size, zoom=zoom, scale=scale)
        except MapError as exc:
            return b'', f'获取位置示意图失败：{exc}'
        if not data:
            return b'', '地图服务未返回图片'
        return data, ''

    def day_map_image(self, plan_date: date,
                      size: tuple[int, int] = (900, 380),
                      zoom: Optional[int] = None,
                      center: Optional[tuple[float, float]] = None,
                      scale: int = 1) -> tuple[bytes, str]:
        """一步取底图（主线程用；界面走 day_map_points + static_map_bytes 拆分）

        `zoom` / `center` 给了就用给的（地图页缩放、点击点位居中都走这里），
        没给就按当天点位的包围盒自动取「能装下全部点的最大层级」。
        """
        from .geo import bbox_center, fit_zoom

        points, note = self.day_map_points(plan_date)
        if not points:
            return b'', note
        coords = [(p['lng'], p['lat']) for p in points]
        z = int(zoom) if zoom else fit_zoom(coords, size)
        c = center if center else bbox_center(coords)
        return self.static_map_bytes(c, size=size, zoom=z, scale=scale)

    # ---------- 采纳冲突建议（一键应用） ----------
    def _task_by_title(self, trip: Trip, title: str):
        for t in trip.tasks:
            if t.title == title:
                return t
        return None

    def apply_suggestion(self, suggestion, plan_date: date) -> PlanOutcome:
        """把 ConflictAdvisor 给出的建议直接应用（建议里带可执行 patch）。

        支持的 patch：
          {'title', 'start_ts'}        顺延到指定时刻
          {'title', 'duration_min'}    调整该项耗时
          {'title', 'mode'}            改该项（或其所在段）的交通方式
          {'move_next_day', 'title'}   移到次日
          {'delete', = title}          删除该项
          {'day_end'}                  延长可排时段
        """
        patch = dict(getattr(suggestion, 'patch', {}) or {})
        if not patch:
            return PlanOutcome(ok=False, error='该建议不含可执行内容')

        # 全局项：延长可排时段
        if patch.get('day_end'):
            s = self.settings
            s.day_end = str(patch['day_end'])
            self.settings_svc.save(s)
            self.trip_svc_settings_sync = True
            return self.replan(plan_date)

        trip = self.trip_repo.get_day(plan_date, self.city)
        if trip is None:
            return PlanOutcome(ok=False, error='该日暂无行程')

        title = patch.get('title') or patch.get('delete')
        task = self._task_by_title(trip, title) if title else None
        if task is None and title:
            return PlanOutcome(ok=False, error=f'未找到「{title}」')

        if patch.get('delete'):
            return self.delete(task.task_id)

        if patch.get('move_next_day'):
            # 挪到"下一个**开放**日"而不是机械 +1 天：
            # 周一闭馆的博物馆挪到周二才有意义，否则等于白挪（D-64）。
            nxt = (hours_mod.next_open_date(task.poi, plan_date)
                   or (plan_date + timedelta(days=1)))
            moved = Task(title=task.title, duration_min=task.duration_min,
                         category=task.category, poi=task.poi,
                         raw_text=task.raw_text, is_locked=False,
                         mode=task.mode, status=TaskStatus.UNSCHEDULED)
            self.delete(task.task_id)
            out = self.commit_task(nxt, moved,
                                   note=f'从 {plan_date} 移入：{task.title}')
            if out.ok:
                out.explanation = (f'「{task.title}」已挪到 {nxt.isoformat()}'
                                   f'（{hours_mod.weekday_cn(nxt)}{"闭馆日已避开" if nxt > plan_date + timedelta(days=1) else ""}）')
            return out

        if patch.get('mode'):
            return self.adjust(task.task_id, {'mode': patch['mode']})
        if patch.get('start_ts'):
            return self.adjust(task.task_id,
                               {'start_ts': patch['start_ts'], 'is_locked': True})
        if patch.get('duration_min'):
            return self.adjust(task.task_id,
                               {'duration_min': int(patch['duration_min'])})
        return PlanOutcome(ok=False, error='该建议暂不支持一键应用')

    # ---------- 出行建议：几点出发最方便 ----------
    def departure_advice(self, task_id: int,
                         mode: Optional[TransitMode] = None) -> 'DepartureAdvice':
        """给出某项安排的出发时刻建议。

        - 最晚出发：按"预测通行 + 缓冲"倒推，刚好不迟到、等待最少的时刻；
        - 躲高峰备选：若提前走能明显少花路上时间，则给出备选并说明要多等多久。
        """
        from .traffic import (TrafficModel, DepartureAdvice, DepartureOption)

        trip, task = self._locate(task_id)
        if trip is None or task is None:
            return DepartureAdvice(message='未找到该安排')
        if task.start_ts is None:
            return DepartureAdvice(message='该安排尚未排定时刻，无法给出出发建议')
        if task.poi is None:
            return DepartureAdvice(message='该安排没有地点，不涉及通行时长')

        m = mode or task.mode or self.options().mode
        scheduled = sorted([t for t in trip.tasks
                            if t.start_ts is not None and t.poi is not None],
                           key=lambda t: t.start_ts)  # type: ignore[arg-type,return-value]
        prev = None
        for t in scheduled:
            if t.task_id == task.task_id:
                break
            prev = t
        if prev is None:
            return DepartureAdvice(
                task_title=task.title, mode_cn=m.cn,
                message='这是当天第一项（前面没有可出发的地点），'
                        '从住处或当前位置直接前往即可')

        base = self.transit.estimate(prev.poi, task.poi, m)
        if not base:
            return DepartureAdvice(task_title=task.title, mode_cn=m.cn,
                                   message='该段通行时长暂时取不到（地图服务不可用），'
                                           '无法给出出发建议')

        target = minutes_of(task.start_ts)
        buffer = max(0, int(self.settings.buffer_min))
        model = TrafficModel()

        feasible: list[DepartureOption] = []
        for d in range(target, max(target - 240, 0), -1):
            minutes = model.predict(base, d, m)
            arrive = d + minutes
            if arrive + buffer <= target:
                feasible.append(DepartureOption(
                    depart_min=d, minutes=minutes, arrive_min=arrive,
                    wait_min=max(0, target - arrive),
                    factor_label=model.label(d, m)))

        if not feasible:
            return DepartureAdvice(
                task_title=task.title, mode_cn=m.cn, base_minutes=base,
                target_label=hhmm(target),
                message=f'即使提前 4 小时出发也赶不上 {hhmm(target)}；'
                        f'该段当前预测需 {base} 分钟以上，建议改期或换交通方式')

        latest = max(feasible, key=lambda o: o.depart_min)
        fastest = min(feasible, key=lambda o: (o.minutes, -o.depart_min))
        best_offpeak = None
        if fastest.minutes <= latest.minutes - 3 and fastest.minutes < base + 1:
            best_offpeak = fastest

        # 表格：最近 1 小时内的候选（每 10 分钟一档）+ 最省时档
        picks = {latest.depart_min: latest}
        for o in feasible:
            if latest.depart_min - 60 <= o.depart_min <= latest.depart_min \
                    and o.depart_min % 10 == 0:
                picks[o.depart_min] = o
        if best_offpeak:
            picks[best_offpeak.depart_min] = best_offpeak
        options = sorted(picks.values(), key=lambda o: -o.depart_min)

        advice = DepartureAdvice(
            ok=True, task_title=task.title, mode_cn=m.cn,
            target_label=hhmm(target), base_minutes=base,
            latest=latest, best_offpeak=best_offpeak, options=options,
            message=f'从「{prev.title}」（{prev.poi.name}）出发')
        return advice

    # ---------- 内部 ----------
    def _locate(self, task_id: int):
        """按 task_id 定位「所属行程」与该行程内的**同一个** Task 实例。

        两个坑都在这里踩过：
          1) 必须返回 trip.tasks 中的实例，否则修改只作用于副本（初版缺陷）；
          2) 必须带 city 取行程——同一日期在两个城市可各有一份行程，
             只按日期取会给到先建的那一份，导致调整/删除定位到别的城市的行程
             （多城市改造引入的缺陷，TC-25-14 守死）。
        """
        row = self.db.conn.execute(
            'SELECT t.plan_date, t.city FROM task k'
            ' JOIN trip t ON k.trip_id = t.trip_id WHERE k.task_id = ?',
            (task_id,)).fetchone()
        if not row:
            return None, None
        trip = self.trip_repo.get_day(date.fromisoformat(row['plan_date']),
                                      row['city'] or None)
        if trip is None:
            return None, None
        for t in trip.tasks:
            if t.task_id == task_id:
                return trip, t
        return trip, None

    def _trip_date(self, task: Task) -> date:
        row = self.db.conn.execute(
            'SELECT t.plan_date FROM task k JOIN trip t ON k.trip_id = t.trip_id'
            ' WHERE k.task_id = ?', (task.task_id,)).fetchone()
        return date.fromisoformat(row['plan_date'])

    def _schedule_and_save(self, trip: Trip, note: str) -> PlanOutcome:
        opts = self.options(lodging=trip.lodging, plan_date=trip.plan_date)
        result = engine_mod.schedule(trip.tasks, opts, self.transit.as_fn(),
                                     self._mode_pick())
        attach_suggestions(result, opts)
        self._attach_notices(trip, result, opts, trip.plan_date)
        # 事务内落库：行程、事项与版本
        trip_repo = self.trip_repo
        trip.tasks = [i.task for i in result.ordered()] + result.unscheduled
        trip_repo.upsert_day(trip)
        snapshot = json.dumps({
            'plan_date': trip.plan_date.isoformat(),
            'city': trip.city,
            'lodging': trip.lodging.name if trip.lodging else None,
            'items': [{
                'title': i.task.title, 'start_ts': hhmm(i.start_min),
                'end_ts': hhmm(i.end_min), 'seq': i.task.seq,
                'status': i.task.status.value, 'is_locked': i.task.is_locked,
                'poi': i.task.poi.name if i.task.poi else None,
                # 时长与方式也进快照：否则"延误 40 分钟"这类改动的回滚只是假回滚
                'duration_min': i.task.duration_min,
                'mode': i.task.mode.value if i.task.mode else None,
            } for i in result.ordered()],
            'notices': [c.detail for c in result.notices],
            'unscheduled': [t.title for t in result.unscheduled],
            'note': note,
        }, ensure_ascii=False)
        vid = self.version_repo.add(trip.trip_id, note, snapshot)
        # 写成功 → 推进版本号；手机/桌面各自轮询它来感知对方的修改
        rev = self.db.bump_revision()
        log.debug('revision -> %s（%s）', rev, note)
        return PlanOutcome(ok=result.is_feasible(), trip=trip, result=result,
                           explanation=result.explanation,
                           notices=result.notices,
                           version_id=vid,
                           error='' if result.is_feasible() else '排程存在冲突或未排入项')

    # ---------- 统计 ----------
    def day_stats(self, trip: Trip,
                  result: Optional[ScheduleResult] = None) -> dict:
        """当日统计。

        通行时长**直接取排程结果**（`ScheduleResult.items[].leg_before`），
        不再自己按"任务上存的方式 / 全局默认"重估一遍——任务里的 mode 只有在
        重新排过那天才会写回，重估就会让统计条与排程说明各说一个数。
        """
        items = [t for t in trip.tasks if t.status == TaskStatus.SCHEDULED
                 and t.start_ts and t.end_ts]
        busy = sum(t.duration_min for t in items)
        if result is None:
            result = self.compute_day(trip.plan_date)
        ordered = result.ordered()
        transit = sum(i.leg_before.duration_min for i in ordered if i.leg_before)
        span = (ordered[-1].end_min - ordered[0].start_min) if ordered else 0
        return {'count': len(items), 'busy': busy, 'transit': transit,
                'idle': max(0, span - busy - transit), 'span': span,
                'cached_legs': self.transit.cache.count()}
