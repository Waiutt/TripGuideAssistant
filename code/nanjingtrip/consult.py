"""行程顾问：多轮对话把"没头绪"收敛成一份可执行的行程。

与 proposal.py 的区别
--------------------
* `proposal.py`：**一次性**给草案（区间 → 逐日），适合"参数已经够了"的场景；
* `consult.py` ：**多轮**顾问。参数不够就先问（每次只问 1–2 个），够了先给一版；
  用户接着说"第二天太满""换成博物馆""改成 5 天""少走路"时，就在原方案上改。

顾问状态（槽位）保存在 `consult_session` 表里，按 session 键取用，所以刷新页面、
换手机（局域网）都还能接着聊。

设计原则（与项目一致）
--------------------
1. **不臆造**：地点只从城市包取；时长用 `intel` 的推荐值；营业/闭馆用取到的真实数据。
2. **先给一版再调**：理想顾问不会卡在问卷上——缺参数时给"默认版 + 一句话说明"。
3. **每天有主题和理由**：不是把点堆在一天，而是"钟山线/城南线/博物线"，并说明为什么这么排。
"""
from __future__ import annotations

import json
import math
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

from .cities import dedupe_brands, get_city, is_recommendable
from .geo import poi_distance_km as _km
from .models import Poi
from .proposal import (_SIGHT_KINDS, parse_date_range, parse_pace, parse_party)

#: 兴趣 → 命中地点的关键词（按名称/类型判断，不逐点硬编码）
#: "大景区"门槛：一条行程里一天塞两个这种点位，现实中跑不完
BIG_SIGHT_MIN = 180
#: 市区中心（新街口一带）用于判断"是不是远郊"
_CITY_CENTER = (118.778, 32.042)


def is_big_sight(poi) -> bool:
    """大景区：游玩时长 ≥3 小时，或离市中心超过 15 公里"""
    try:
        if int(getattr(poi, 'visit_min', 0) or 0) >= BIG_SIGHT_MIN:
            return True
        lng, lat = getattr(poi, 'lng', 0) or 0, getattr(poi, 'lat', 0) or 0
        if not lng or not lat:
            return False
        dx = (lng - _CITY_CENTER[0]) * 111.0 * math.cos(math.radians(lat))
        dy = (lat - _CITY_CENTER[1]) * 111.0
        return math.hypot(dx, dy) > 15.0
    except (TypeError, ValueError):
        return False


#: 这些词不是地点（口语里常和地名混在一起，被 2–3 字相似度误抓）
_NOT_PLACE = ('找朋友', '朋友', '同学', '我们', '我们两', '我们俩', '你们', '他们',
              '一起', '吃饭', '完饭', '计划', '出发', '到达', '推荐', '规划',
              '什么时候', '附近', '周边', '市区', '学校', '那边', '这边', '地方')


def is_place_word(candidate: str) -> bool:
    """候选词像不像"地名"：太短、含人称/动词短语的一律不要"""
    text = str(candidate or '').strip()
    if len(text) < 2:
        return False
    return not any(w == text or (len(text) <= 5 and w in text) for w in _NOT_PLACE)


#: 中文数字 → 阿拉伯数字（只在"数量词"上下文里替换，地名里的"三山街"不动）
_CN_DIGIT = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
             '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}


def cn_to_number(token: str) -> int:
    """把"两/三/十/十一/二十"这类中文数字转成整数，转不了返回 0"""
    token = str(token or '').strip()
    if not token:
        return 0
    if token.isdigit():
        return int(token)
    if token == '十':
        return 10
    if token.startswith('十') and len(token) == 2:
        return 10 + _CN_DIGIT.get(token[1], 0)
    if token.endswith('十') and len(token) == 2:
        return _CN_DIGIT.get(token[0], 0) * 10
    if len(token) == 3 and token[1] == '十':
        return _CN_DIGIT.get(token[0], 0) * 10 + _CN_DIGIT.get(token[2], 0)
    if len(token) == 1:
        return _CN_DIGIT.get(token, 0)
    return 0


def normalize_cn_numbers(text: str) -> str:
    """「玩两天 / 三天两晚 / 十点起床 / 十一点回酒店 / 每天两个点」里的中文数字归一"""
    out = str(text or '')
    pat = r'([一二两三四五六七八九十]{1,3})'

    def repl(match, unit):
        # 只替换数字本身：单位由后行断言（lookahead）保留，否则会出现"玩2天天"
        value = cn_to_number(match.group(1))
        return str(value) if value else match.group(0)

    for unit in ('天', '晚', '个点', '个地方', '个景点', '个小时', '小时', '点'):
        out = re.sub(pat + rf'(?=\s*{unit})', lambda m, u=unit: repl(m, u), out)
    return out


#: 咖啡/奶茶不适合当正餐（顾问不该推荐"喜茶当午饭"）
_DRINK_WORDS = ('星巴克', '喜茶', '奶茶', '咖啡', '茶饮')

#: 菜系词 → 命中关键词。"吃日料"要的是**这一类店**，不是一家叫"日料"的店；
#: 城市包里没有这一类时宁可留缺口，也不拿别的菜系冒充（数据纪律第 2 条）。
_CUISINE_WORDS = {
    '日料': ('日本料理', '日式', '寿司', '居酒屋', '刺身', '烧肉', '拉面', '天妇罗'),
    '日本料理': ('日本料理', '日式', '寿司', '居酒屋', '刺身', '烧肉', '拉面'),
    '火锅': ('火锅', '涮'),
    '小吃': ('小吃', '汤包', '锅贴', '馄饨', '鸭血', '粉丝汤'),
    '面': ('面馆', '拉面', '面条', '小煮面'),
    '烧烤': ('烧烤', '烤串', '烤肉', 'barbecue'),
    '西餐': ('西餐', '牛排', '意面', '披萨', '西式'),
    '川菜': ('川菜', '川渝', '麻辣'),
    '韩餐': ('韩式', '韩国', '料理'),
    '淮扬菜': ('淮扬', '江浙', '金陵', '本帮'),
}

log = logging.getLogger('nanjingtrip.consult')

INTERESTS = {
    '历史人文': ('总统府', '中山陵', '明孝陵', '朝天宫', '瞻园', '中华门', '城墙',
                 '颐和路', '甘熙', '熙南里', '江宁织造', '夫子庙', '朝天宫'),
    '博物馆': ('博物院', '博物馆', '纪念馆', '科举', '美术馆', '六朝'),
    '自然风光': ('玄武湖', '栖霞山', '燕子矶', '牛首山', '雨花台', '北极阁',
                 '南京眼', '台城', '公园'),
    '美食街区': ('夫子庙', '老门东', '1912', '湖南路', '新街口', '大牌档', '小厨娘',
                 '咸亨', '马祥兴', '绿柳居', '汤包', '清真', '梅苑'),
    '亲子': ('红山', '动物园', '玄武湖', '大报恩寺', '南京眼', '科技馆', '博物院'),
}

#: 用户话里的兴趣线索 → 兴趣名
_INTEREST_HINTS = (
    ('历史', '历史人文'), ('人文', '历史人文'), ('民国', '历史人文'),
    ('博物馆', '博物馆'), ('看展', '博物馆'), ('文物', '博物馆'),
    ('自然', '自然风光'), ('风景', '自然风光'), ('爬山', '自然风光'),
    ('湖', '自然风光'), ('公园', '自然风光'),
    ('吃', '美食街区'), ('美食', '美食街区'), ('小吃', '美食街区'),
    ('夜市', '美食街区'), ('逛吃', '美食街区'),
    ('孩子', '亲子'), ('带娃', '亲子'), ('小孩', '亲子'), ('动物', '亲子'),
)

#: 每天主题命名（按命中兴趣最多的一类命名，纯展示用）
_THEME_NAME = {
    '历史人文': '民国与老城线',
    '博物馆': '博物馆线',
    '自然风光': '山水线',
    '美食街区': '逛吃线',
    '亲子': '亲子线',
}


def detect_interests(text: str) -> list[str]:
    out: list[str] = []
    for hint, name in _INTEREST_HINTS:
        if hint in (text or '') and name not in out:
            out.append(name)
    return out



def nm_key_ok(name: str, probe: str) -> bool:
    """包内名字与用户说法是否够像：相等/互相包含且长度差不大"""
    if not name or not probe:
        return False
    if name == probe:
        return True
    if probe in name or name in probe:
        return abs(len(name) - len(probe)) <= 3
    return False


class ConsultSession:
    """顾问槽位（可序列化到 consult_session 表）"""

    FIELDS = ('start', 'end', 'party', 'pace', 'wake', 'interests', 'budget',
              'hotel', 'must', 'per_day', 'asked', 'days', 'note', 'extras',
              'day_end', 'arrival', 'luggage', 'arrive_at', 'practical',
              'anchor_place', 'hotel_near', 'next_day', 'taste', 'friend_weekend',
              'window', 'win_start', 'win_end', 'v', 'meal', 'avoid',
              'trip_days', 'drop', 'origin_text', 'meal_near', 'home_return',
              'intent_kind')

    #: 槽位语义版本：升版本时把"老口径下推断出来的值"作废，避免陈年误判一直生效
    VERSION = 2

    def __init__(self, data: Optional[dict] = None):
        d = data or {}
        self.start: str = d.get('start') or ''
        self.end: str = d.get('end') or ''
        self.party: int = int(d.get('party') or 0)
        self.pace: str = d.get('pace') or ''
        self.wake: str = d.get('wake') or ''
        self.interests: list[str] = list(d.get('interests') or [])
        self.budget: str = d.get('budget') or ''
        self.hotel: str = d.get('hotel') or ''
        self.must: list[str] = list(d.get('must') or [])
        self.per_day: int = int(d.get('per_day') or 0)
        self.asked: list[str] = list(d.get('asked') or [])
        self.days: list[dict] = list(d.get('days') or [])
        self.note: str = d.get('note') or ''
        self.extras: list[dict] = list(d.get('extras') or [])
        self.day_end: str = d.get('day_end') or ''
        self.arrival: str = d.get('arrival') or ''
        self.luggage: bool = bool(d.get('luggage'))
        self.arrive_at: str = d.get('arrive_at') or ''
        self.practical: bool = bool(d.get('practical'))
        self.anchor_place: str = d.get('anchor_place') or ''
        self.hotel_near: str = d.get('hotel_near') or ''
        self.next_day: dict = dict(d.get('next_day') or {})
        self.taste: str = d.get('taste') or ''
        self.friend_weekend: bool = bool(d.get('friend_weekend'))
        self.window: bool = bool(d.get('window'))   # 起止只是一段窗口，不是确定行程
        self.win_start: str = d.get('win_start') or ''   # 窗口原样留档，改行程日期不会丢
        self.win_end: str = d.get('win_end') or ''
        self.v: int = int(d.get('v') or 0)
        #: 吃哪儿：具体餐厅名 / 'recommend'（让我挑）/ ''（还没问）
        self.meal: str = d.get('meal') or ''
        #: 「去新街口吃日料」里的新街口：这顿饭的**片区**，不是要去办事的地点
        self.meal_near: str = d.get('meal_near') or ''
        #: 「然后再返校」：当天要回到出发地收尾（当天往返，不必再问住宿）
        self.home_return: bool = bool(d.get('home_return'))
        #: 意图层给出的判定（plan/entry/modify/prune/ask/clarify/vague），供追问与调试用
        self.intent_kind: str = d.get('intent_kind') or ''
        #: 用户明确不想去的地方（"不去老门东"）——排程时必须真的排除
        self.avoid: list[str] = list(d.get('avoid') or [])
        #: 明确说的行程天数（"玩3天""改成5天""多加一天"）；0 表示没说
        self.trip_days: int = int(d.get('trip_days') or 0)
        #: 要真正从当天行程里删掉的地点（"把夫子庙去掉"）
        self.drop: list[str] = list(d.get('drop') or [])
        #: 出发地说法（城市包核不到时留原话，用于提示"补个站点"）
        self.origin_text: str = d.get('origin_text') or ''

    def as_dict(self) -> dict:
        data = {k: getattr(self, k) for k in self.FIELDS}
        data['v'] = self.VERSION
        return data

    @property
    def date_span(self) -> int:
        if not (self.start and self.end):
            return 0
        try:
            a = date.fromisoformat(self.start)
            b = date.fromisoformat(self.end)
        except ValueError:
            return 0
        return max(0, (b - a).days + 1)


#: 一段话的合理上限：超过就请用户拆开说（超长输入会让解析与取数成倍变慢）
MAX_TEXT_LEN = 1200


# ---------------------------------------------------------------- 生成进度
#: 生成草案是同步请求（在线要 4–7 秒），前端只能白屏等。这里把"现在卡在哪一步"
#: 写到进程内一张小表里，前端另发一个只读请求轮询，进度条就能显示真实阶段名。
#: 注意：只放阶段名与粗粒度百分比，不伪装成精确进度。
PROGRESS: dict = {}

#: 阶段权重（顺序即推进顺序；百分比按阶段给，不做"每秒滑一点"的假动画）
STAGE_START = ('正在理解你的需求', 12)
STAGE_PICK = ('正在挑当天要去的点', 45)
STAGE_FETCH = ('正在查营业时间与通行时长', 72)
STAGE_RENDER = ('正在生成时间轴', 90)


def set_progress(key: str, stage: tuple) -> None:
    """记录当前阶段（失败也不抛出：进度条坏了不能拖垮主流程）"""
    try:
        PROGRESS[str(key or '')] = {'stage': stage[0], 'pct': int(stage[1])}
    except Exception:                        # noqa: BLE001
        pass


def get_progress(key: str) -> dict:
    return dict(PROGRESS.get(str(key or '')) or {})


def _track_progress(fn):
    """给 chat() 套一层：无论正常返回还是抛错，结束时都把进度清掉（保证有终点）"""
    import functools

    @functools.wraps(fn)
    def wrapper(self, session, text, today):
        key = str(session or '')
        try:
            set_progress(key, STAGE_START)
            return fn(self, session, text, today)
        finally:
            PROGRESS.pop(key, None)
    return wrapper


class Consultant:
    """多轮行程顾问"""

    def __init__(self, trip_svc, store=None):
        self.svc = trip_svc
        self.store = store

    # ---------- 会话存取 ----------
    def load(self, session: str) -> ConsultSession:
        if self.store is None:
            return ConsultSession()
        return ConsultSession(self._upgrade(self.store.get(session)))

    @staticmethod
    def _upgrade(row: dict) -> dict:
        """老会话升级：`hotel='recommend'` 曾由模型擅自填入，作废并允许重新追问"""
        if not row or int(row.get('v') or 0) >= ConsultSession.VERSION:
            return row
        fixed = dict(row)
        if fixed.get('hotel') == 'recommend':
            fixed['hotel'] = ''
        fixed['asked'] = [a for a in (fixed.get('asked') or []) if a != 'hotel']
        return fixed

    def save(self, session: str, st: ConsultSession) -> None:
        if self.store is not None:
            self.store.put(session, st.as_dict())

    # ---------- 候选点 ----------
    def _pool(self) -> list[Poi]:
        pack = get_city(self.svc.city)
        out: list[Poi] = []
        for row in pack.poi_table():
            kind = str(row.get('kind') or '')
            if kind in ('交通', '医疗', '住宿', '校园', '办事'):
                continue
            if kind not in _SIGHT_KINDS and kind != '餐饮':
                continue
            # 家居卖场、装修中的商场这类"类别挂错"的点位不进推荐
            if not is_recommendable(row.get('name') or ''):
                continue
            poi = Poi.from_pack(row, self.svc.city)
            # 注意：这里**不**逐点取情报——候选有 60+ 个，全查一遍要 40 秒以上。
            # 只对最终入选的点取（见 build_plan），先把结构搭起来。
            out.append(poi)
        return out

    def _rating(self, poi: Poi) -> float:
        intel = getattr(self.svc, 'intel', None)
        row = intel.cached(poi.name, self.svc.city) if intel is not None else None
        try:
            return float((row or {}).get('rating') or 0) or 4.3
        except (TypeError, ValueError):
            return 4.3

    def _score(self, poi: Poi, st: ConsultSession) -> float:
        v = self._rating(poi) + min(0.4, int(poi.visit_min or 0) / 450.0)
        name = f'{poi.name}{poi.kind}'
        for it in st.interests:
            if any(k in name for k in INTERESTS.get(it, ())):
                v += 0.6                  # 命中兴趣的加权
        if any(m and m in poi.name for m in st.must):
            v += 5.0                      # 点名要去的一律靠前
        return v

    # ---------- 解析用户这句话 ----------
    def absorb(self, st: ConsultSession, text: str, today: date) -> ConsultSession:
        raw = normalize_cn_numbers((text or '').strip())
        # 「参观2-3小时」「1-2小时」：取区间中值再交给时长解析，
        # 不然只会认到前一个数字（2 小时），或者干脆按包内默认值排。
        raw = re.sub(r'(\d+(?:\.\d+)?)\s*[-~到至]\s*(\d+(?:\.\d+)?)\s*(小时|分钟|分)',
                     lambda m: f'{(float(m.group(1)) + float(m.group(2))) / 2:g} {m.group(3)}',
                     raw)
        # 本轮之前就已经确认过的必去项：它们来自前面几轮的原话，
        # 不能拿这一句去校验（"想去总统府和中山陵" → 下一句只说"11月14号"）。
        prev_must = list(st.must)

        # ⓪ 先让模型读一遍（只取高置信度候选），规则再兜底/覆盖。
        #    模型给的是"候选+置信度"，采信与否由这里决定——不是它说什么就信什么。
        try:
            from . import llm_intake
            got = llm_intake.slot_extract(raw, today, self.svc.degrade)
        except Exception:                    # noqa: BLE001 模型不可用就走规则
            got = {}
        if got.get('start'):
            st.start = got['start']
            st.end = got.get('end') or got['start']
        if got.get('party'):
            st.party = int(got['party'])
        if got.get('pace'):
            st.pace = got['pace']
        if got.get('wake'):
            st.wake = got['wake']
        for it_name in got.get('interests') or []:
            if it_name not in st.interests:
                st.interests.append(it_name)
        # 模型常把"晚上回酒店"误解成"让我帮你选酒店"。必须用户明说要我定，才替他选，
        # 否则按规矩先问一句"住哪儿"——住宿锚点决定当天从哪出发、回哪。
        _PICK_HOTEL = ('帮我选', '你选', '你定', '推荐个酒店', '推荐酒店', '订酒店',
                       '随便', '任意', '你看着办', '都可以')
        if got.get('hotel') == 'recommend' and any(w in raw for w in _PICK_HOTEL):
            st.hotel = 'recommend'

        # 换话题：说了新的单日/新区间，且与旧会话的日期不同 → 旧话题的窗口、朋友、住宿追问一律作废
        new_day = None
        new_range = None
        try:
            new_range = parse_date_range(raw, today)
        except Exception:                     # noqa: BLE001
            new_range = None
        try:
            from .proposal import parse_single_date as _psd
            new_day = _psd(raw, today)
        except Exception:                     # noqa: BLE001
            new_day = None
        if new_range is not None and new_range[1] > new_range[0]:
            single_day = None                 # 真区间：不动天数
            new_day = None
        elif re.search(r'\d\s*[月./]\s*\d+\s*[日号]?\s*(?:到|至|—|~|～|-)', raw):
            # 写了区间但解析不出来（倒置/不存在）→ 交给日期质检解释，别当单日排
            single_day = None
            new_day = None
        else:
            single_day = new_day              # 单日：今天/明天/后天/某一个日期
            new_range = None
        old_start = st.start
        switched = False
        if single_day is not None:
            switched = bool(old_start) and old_start != single_day.isoformat()
        elif new_range is not None:
            switched = bool(old_start) and old_start != new_range[0].isoformat()
        if switched:
            st.window = False
            st.win_start = st.win_end = ''
            st.friend_weekend = False
            st.trip_days = 0
            st.extras = []
            st.days = []
            st.practical = False
            st.hotel_near = st.anchor_place = ''
            st.meal_near = ''
            st.home_return = False
            st.next_day = {}
            st.note = ''
            # 只保留"人"和"口味"这类跨话题仍然有效的偏好，其余按新话题重来
        # 换话题了：新日期区间 + 旅游意图 → 清空上一轮遗留的务实槽位（到达/办事/返程）
        if (parse_date_range(raw, today) is not None
                and any(w in raw for w in ('旅游', '玩', '景点', '推荐', '逛'))):
            st.practical = False
            st.arrival = ''
            st.arrive_at = ''
            st.next_day = {}
            st.extras = []
            st.luggage = False
            st.hotel_near = ''
            st.anchor_place = ''
            st.meal_near = ''
            st.home_return = False
            st.must = []

        # 「11月中旬」这类模糊窗口：先给出窗口，并记住"朋友只在周末有空"
        if any(w in raw for w in ('上旬', '中旬', '下旬', '月底')):
            m_mon = re.search(r'(\d{1,2})\s*月', raw)
            mon = int(m_mon.group(1)) if m_mon else (today.month + 1 if today.day > 20 else today.month)
            if '上旬' in raw:
                a, b = 1, 10
            elif '中旬' in raw:
                a, b = 11, 20
            elif '下旬' in raw:
                a, b = 21, 28
            else:
                a, b = 24, 28
            try:
                st.start = date(today.year, mon, a).isoformat()
                st.end = date(today.year, mon, b).isoformat()
                st.window = True
                st.win_start, st.win_end = st.start, st.end
            except ValueError:
                pass
            st.practical = False
            st.note = (st.note + '；' if st.note else '') + f'按 {mon} 月 {a}–{b} 号留窗口，具体哪几天你定'
        if any(w in raw for w in ('工作日没空', '工作日没时间', '只有周末', '周末有空', '上班没空')):
            st.friend_weekend = True

        rng = parse_date_range(raw, today)
        if rng:
            st.start, st.end = rng[0].isoformat(), rng[1].isoformat()
        else:
            m = re.search(r'(?:玩|待|住|去)\s*(\d{1,2})\s*天', raw)
            if m and not st.start:
                st.start = (today + timedelta(days=1)).isoformat()
                st.end = (today + timedelta(days=int(m.group(1)))).isoformat()

        party = parse_party(raw)
        if party > 1 or not st.party:
            st.party = party or st.party
        pace, wake = parse_pace(raw)
        if any(w in raw for w in ('懒', '起得晚', '晚起', '自然醒', '不早起')):
            st.pace, st.wake = 'slow', wake
        elif any(w in raw for w in ('特种兵', '赶时间', '多玩')):
            st.pace, st.wake = 'fast', wake
        elif not st.pace:
            st.pace, st.wake = 'normal', wake

        if '清真' in raw:
            st.taste = '清真'
        elif any(w in raw for w in ('吃素', '素食', '素斋')):
            st.taste = '素'
        elif any(w in raw for w in ('不吃辣', '怕辣', '不要辣')):
            st.taste = '不吃辣'
        for it in detect_interests(raw):
            if it not in st.interests:
                st.interests.append(it)

        if any(w in raw for w in ('省', '穷游', '预算少', '学生')):
            st.budget = '省'
        elif any(w in raw for w in ('不差钱', '预算充足', '随便花')):
            st.budget = '宽'
        if any(w in raw for w in ('酒店未知', '没订', '住哪都行', '酒店没定',
                                  '不知道住哪', '没有头绪')):
            st.hotel = ''            # 未定 → 追问，不替他做主
        # 用户把选择权交出来 / 直接给了酒店名
        if any(w in raw for w in ('你帮我选', '你定', '帮我选', '随便选', '看着办',
                                  '你推荐', '推荐一个酒店')):
            st.hotel = 'recommend'
        pack = get_city(self.svc.city)
        for row in pack.poi_table():
            if str(row.get('kind') or '') != '住宿':
                continue
            nm = str(row.get('name') or '')
            if nm and nm in raw:
                st.hotel = nm
                break

        # 点名"必须去/一定要去"的点
        for poi in self._pool():
            if poi.name in raw and any(w in raw for w in ('必须', '一定要', '必去')):
                if poi.name not in st.must:
                    st.must.append(poi.name)

        # 务实行程：交通接驳 + 住宿 + 办事 + 返程（不是"去哪儿玩"）
        m_arrive = re.search(r'(\d{1,2})\s*[点:：]\s*(\d{1,2})?\s*分?\s*(?:到|抵达|到达)\s*'
                             r'([\u4e00-\u9fa5]{2,8}(?:站|机场)?)', raw)
        if m_arrive and ('酒店' in m_arrive.group(3) or '住处' in m_arrive.group(3)
                         or '家' == m_arrive.group(3)):
            m_arrive = None          # "23点后到酒店"是回住处时间，不是到达
        if m_arrive:
            hh = int(m_arrive.group(1)); mm = int(m_arrive.group(2) or 0)
            st.arrive_at = f'{hh:02d}:{mm:02d}'
            st.arrival = m_arrive.group(3).strip()
            st.luggage = True
            st.practical = True
        m_late = re.search(r'(?:晚点|延误|晚)\s*(\d{1,3})\s*分钟', raw)
        if m_late and st.arrive_at:
            hh, mm = st.arrive_at.split(':')
            total = int(hh) * 60 + int(mm) + int(m_late.group(1))
            st.arrive_at = f'{(total // 60) % 24:02d}:{total % 60:02d}'
            st.note = f'车次晚点 {m_late.group(1)} 分钟，到达时刻已顺延到 {st.arrive_at}'
        m_from = re.search(r'从\s*([\u4e00-\u9fa5A-Za-z0-9]{2,16}?)\s*(?:出发|开始|起)', raw)
        if m_from:
            poi = self._resolve_place(m_from.group(1))
            if poi is not None:
                st.anchor_place = st.anchor_place or poi.name
                if poi.name not in st.must:
                    st.must.insert(0, poi.name)
        # 「我在 X」「从 X 出发」→ 出发地锚点（不是要去玩的点）
        m_from2 = re.search(r'(?:我|我们|咱)(?:在|从)\s*([\u4e00-\u9fa5A-Za-z0-9]{2,14}?)'
                            r'\s*(?:出发|上车|这边|附近|然后|，|。|$)', raw) or \
            re.search(r'从\s*([\u4e00-\u9fa5A-Za-z0-9]{2,14}?)\s*(?:出发|走|上车)', raw)
        if m_from2:
            cand_from = m_from2.group(1).strip()
            if is_place_word(cand_from):
                poi_from = self._resolve_place(cand_from)
                if poi_from is not None:
                    st.anchor_place = st.anchor_place or poi_from.name
                else:
                    st.origin_text = cand_from
                    st.note = (st.note + '；' if st.note else '') + (
                        f'出发地「{cand_from}」我核不到，告诉我最近的站点或换个地名')

        # 每天都要返回学校：这是当日收尾锚点，不是办事/返程
        daily_return = bool(re.search(r'(每天|每日|每晚|天天)[^。；]{0,6}(返回|回)', raw))
        m_back = re.search(r'(?:我要)?回\s*([\u4e00-\u9fa5A-Za-z0-9]{2,14})', raw)
        if m_back and m_back.group(1).startswith(('学校', '宿舍', '酒店', '住处')):
            m_back = None
        if m_back and '回酒店' not in m_back.group(0) and '回南京南' not in m_back.group(0):
            cand = m_back.group(1).strip()
            poi = self._resolve_place(cand)
            name = poi.name if poi is not None else cand
            if poi is None and any(w in cand for w in ('学校', '校区', '母校')):
                st.anchor_place = ''
                st.note = (st.note + '；' if st.note else '') + '你说的「学校」没指明是哪所，需要补一句'
            if poi is not None:
                st.anchor_place = name
                if daily_return:
                    st.note = ''      # 每天回学校只是收尾
                else:
                    if name not in st.must:
                        st.must.append(name)
                    st.practical = True
        if re.search(r'(接|送)(人|站|机)', raw) and not any(
                w in raw for w in ('景点', '玩', '逛', '博物馆', '公园', '吃')):
            st.practical = True
            st.anchor_place = st.anchor_place or ''
        if any(w in raw for w in ('住附近', '附近的酒店', '就近住', '旁边住', '住学校附近')):
            near_anchor = st.anchor_place or st.arrival
            if not st.anchor_place and st.arrival:
                poi = self._resolve_place(st.arrival)
                if poi is not None:
                    st.anchor_place = poi.name
                    near_anchor = poi.name
            st.hotel_near = near_anchor
        if '第二天' in raw or '次日' in raw or '后天' in raw:
            # "第二天太满了"只是在改行程，不能把整单判成事务行程；
            # 只有句子里确实有办事/接送/返程的意图时才算务实型
            if re.search(r'(答辩|面试|开会|办手续|报到|接人|送人|返程|回程|离京|赶高铁|赶飞机)',
                         raw):
                st.practical = True
            if st.start and st.end == st.start:
                st.end = (date.fromisoformat(st.start) + timedelta(days=1)).isoformat()
        m_reply = re.search(r'(答辩|面试|开会|办手续|报到)', raw)
        if m_reply and ('第二天' in raw or '次日' in raw):
            st.next_day = {
                'task': m_reply.group(1),
                'at': st.anchor_place,
                'then_to': '南京南站' if '南京南' in raw else '',
                'then_out': '上海' if '上海' in raw else '',
                'depart_hint': st.arrive_at and '',
            }
            if '中午' in raw:
                st.next_day['depart_hint'] = '中午'
            st.practical = True

        # 「10点到南京南」→ 记下到达站；「放行李/先回酒店」→ 记下要放行李
        m_arr = re.search(r'(\d{1,2})\s*[点:：]\s*(\d{1,2})?\s*(?:到|抵达|到达|落地)\s*'
                          r'([\u4e00-\u9fa5A-Za-z0-9]{2,12})', raw)
        if not m_arr:
            m_arr = re.search(r'(?:到|抵达|到达)\s*([\u4e00-\u9fa5]{2,6}(?:站|机场))', raw)
        if m_arr:
            st.arrival = m_arr.group(m_arr.lastindex).strip()
            st.luggage = True
        if any(w in raw for w in ('放行李', '寄行李', '先回酒店', '入住', 'check in', '放包')):
            st.luggage = True

        # 中途插一件事：「我突然想起来要有40分钟去南京南接同学」
        m_extra = re.search(r'(\d{1,3})\s*分钟', raw)
        if m_extra and any(w in raw for w in ('去', '接', '送', '取', '办', '回')):
            mins = int(m_extra.group(1))
            # 先按"去/接 后面跟的地名"抓一段，再用包内外解析去认
            m_place = re.search(r'去\s*([\u4e00-\u9fa5A-Za-z0-9]{2,10}?)'
                                r'(?:接|送|取|办|回|买|拿|附近|，|。|$)', raw)
            target, poi = '', None
            if m_place:
                guess = m_place.group(1)
                try:
                    poi = self.svc._poi_named(guess)
                except Exception:            # noqa: BLE001
                    poi = None
                target = poi.name if poi is not None else guess
            if not target:
                pack = get_city(self.svc.city)
                names = sorted(set(list(pack.names()) + list(pack.aliases())),
                               key=len, reverse=True)
                hit = next((n for n in names if n and n in raw), None)
                if hit:
                    try:
                        poi = self.svc._poi_named(hit)
                    except Exception:        # noqa: BLE001
                        poi = None
                    target = poi.name if poi is not None else hit
            if target:
                st.extras = list(getattr(st, 'extras', []) or [])
                if not any(e.get('poi') == target for e in st.extras):
                    st.extras.append({
                        'poi': target if poi is not None else '',
                        'title': f'{target}（{mins} 分钟）',
                        'duration_min': mins,
                        'category': '办事',
                        'kind': 'task' if poi is not None else 'block'})

        # 「多加一天 / 少一天」：芯片上就有这两个按钮，必须真生效
        add_day = bool(re.search(r'(?:多加|再加|加|延|多)\s*[1一2二3三]?\s*天', raw)) or \
            any(w in raw for w in ('多加一天', '加一天', '再多一天', '延一天'))
        sub_day = bool(re.search(r'(?:少|减|缩短|砍|去掉)\s*[1一]\s*天', raw)) or \
            any(w in raw for w in ('少一天', '减一天', '缩短一天'))
        if add_day:
            # 单日 + "多加一天"：把天数变化记下来，最终裁决时生效
            # 只有"还没有终点"时（单日场景）才用天数表达；有区间的沿用原来的终点+1
            if not st.window and (not st.end or st.end == st.start):
                st.trip_days = max(2, (getattr(st, 'trip_days', 0) or 1) + 1)
            if st.window:
                # 窗口只说明"哪几天去"，加一天加的是行程长度，不是把窗口撑大
                base = st.trip_days or min(st.date_span or 3, 3)
                st.trip_days = min(base + 1, max(1, st.date_span or base + 1))
            elif st.end:
                st.end = (date.fromisoformat(st.end) + timedelta(days=1)).isoformat()
            st.per_day = st.per_day or self._default_per_day(st)
        elif sub_day:
            if st.window:
                base = st.trip_days or min(st.date_span or 3, 3)
                st.trip_days = max(1, base - 1)
            elif st.start and st.end:
                end = date.fromisoformat(st.end)
                if end > date.fromisoformat(st.start):
                    st.end = (end - timedelta(days=1)).isoformat()

        # 「10点起床 / 8点半才起」→ 起床时刻（排程从这时开始）
        m_wake = re.search(r'(\d{1,2})\s*点\s*(半|\d{1,2})?\s*(?:才|能|再)?\s*(?:起床|起来|起|醒)', raw)
        if m_wake:
            hh = int(m_wake.group(1))
            tail = m_wake.group(2) or ''
            mm = 30 if tail == '半' else (int(tail) if tail.isdigit() else 0)
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                st.wake = f'{hh:02d}:{mm:02d}'

        # 「每天最多2个点 / 一天3个地方」→ 每天点数上限
        m_per = re.search(r'(?:每天|一天|每日)\s*(?:最多|别超过|不超过|就)?\s*'
                          r'(\d)\s*个\s*(?:点|地方|景点)', raw)
        if m_per:
            st.per_day = max(1, min(6, int(m_per.group(1))))

        # 「一共3天 / 去3天 / 待3天」→ 行程天数（给了起点就顺着算终点）
        m_len = re.search(r'(?:一共|总共|去|待|玩|住)\s*(\d{1,2})\s*天', raw)
        if m_len and not any(w in raw for w in ('加一天', '少一天', '多一天')):
            n_days = max(1, min(15, int(m_len.group(1))))
            st.trip_days = n_days
            if st.start:
                st.end = (date.fromisoformat(st.start)
                          + timedelta(days=n_days - 1)).isoformat()
                st.window = False

        # 裸地名堆（"总统府 夫子庙 老门东"）：没有动词也要认，否则整句只剩时长
        if not st.must:
            try:
                pack = get_city(self.svc.city)
                either_or = bool(re.search(r'(或者|或是|还是|或)', raw))
                names = sorted(set(list(pack.names()) + list(pack.aliases())),
                               key=len, reverse=True)
                seen = set()
                for name in names:
                    if not name or name not in raw or not is_place_word(name):
                        continue
                    poi = self.svc._poi_named(name)
                    if poi is None or str(poi.kind) in ('交通', '住宿', '餐饮'):
                        continue
                    # "我在X""从X出发"是出发点，不是要去玩的地方
                    at = raw.find(name)
                    if at > 0 and re.search(r'(在|从|位于)\s*$', raw[max(0, at - 3):at]):
                        st.anchor_place = st.anchor_place or poi.name
                        continue
                    key = self._norm(poi.name)
                    if key in seen or len(st.must) >= 5:
                        continue
                    seen.add(key)
                    st.must.append(poi.name)
                    if either_or:            # 二选一：只排第一个，另一个作为候选
                        break
            except Exception:                 # noqa: BLE001 认不出就不加，别影响主流程
                pass

        # 「不想去X / 别去X / 把X去掉 / X不要了」→ 排进排除项；
        # 其中"去掉/删掉"这类是要**动已有数据**的，单独记到 drop，由 chat 真正删除。
        for name, hard in self.exclusion_names(raw):
            key = self._norm(name)
            if not key:
                continue
            if key not in {self._norm(a) for a in st.avoid}:
                st.avoid.append(name)
            if hard and key not in {self._norm(a) for a in st.drop}:
                st.drop.append(name)
            st.must = [m for m in st.must if self._norm(m) != key]

        for m_no in re.finditer(r'(?:不想去|不想再|不去|别去|不要去|不想要|换掉|去掉|删掉)\s*'
                               r'([\u4e00-\u9fa5A-Za-z0-9·（）()]{2,16})', raw):
            cand = m_no.group(1).strip()
            if not cand:
                continue
            poi = None
            try:
                poi = self._resolve_place(cand)
            except Exception:                 # noqa: BLE001
                poi = None
            name = (poi.name if poi is not None else cand).strip()
            key = self._norm(name)
            if not key:
                continue
            if key not in {self._norm(a) for a in st.avoid}:
                st.avoid.append(name)
            # 已经是"排过的安排"时，这句话是要动数据的——单独记下来真正删掉
            if any(w in raw for w in ('去掉', '删掉', '删除', '取消', '不要了', '换掉', '移掉')):
                if key not in {self._norm(a) for a in st.drop}:
                    st.drop.append(name)
            st.must = [m for m in st.must if self._norm(m) != key]

        # 「吃哪儿」的回答：点名某家店 → 就用它；说"随便/你帮我选" → 我按位置挑
        named_meal = self.explicit_meal_place(raw)
        hit_meal = None
        try:
            for poi in self._pool():
                if str(poi.kind) == '餐饮' and poi.name and poi.name in raw:
                    hit_meal = poi.name
                    break
        except Exception:                     # noqa: BLE001
            hit_meal = None
        if named_meal:
            st.meal = named_meal          # 用户点名了就以他为准，不许换成"最近的店"
        elif hit_meal:
            st.meal = hit_meal
        elif (any(w in raw for w in ('就近', '随便', '你帮我选', '都行'))
              and any(w in raw for w in ('吃', '饭', '餐'))):
            st.meal = 'recommend'
        # 「吃日料 / 想吃火锅」：说的是**菜系**，不是店名。记进口味槽位并按菜系筛候选；
        # 以前直接把"日料"当店名写进时间轴（"吃饭（日料）"），等于没回答"推荐一家"。
        for word in _CUISINE_WORDS:
            if word in raw and any(w in raw for w in ('吃', '饭', '餐', '来一', '推荐')):
                if st.meal and st.meal != 'recommend' and st.meal not in _CUISINE_WORDS:
                    # 记在 meal 上的可能是个**片区**（"去新街口吃日料"）：那是吃饭的地方，
                    # 不是店名。转成片区 + 按菜系推荐，别把地名当餐厅写进时间轴。
                    poi_m = self.svc._poi_named(st.meal)
                    if poi_m is not None and str(getattr(poi_m, 'kind', '')) == '餐饮':
                        break
                    st.meal_near = st.meal_near or st.meal
                st.taste = word
                st.meal = 'recommend'
                break

        # ---- 意图层：把这句话切成"动作片段"，一处解析 ----
        # 以前在这条链路上有六处正则各自猜"哪个词是地名、动词是哪个、时长归谁"，
        # 于是「去新街口吃日料」被抢成"去新街口找人 + 吃饭（日料）"。现在交给 intent.py。
        try:
            from . import intent as intent_mod
            judged = intent_mod.analyze(raw, resolve=self.svc._poi_named,
                                        is_place=is_place_word)
        except Exception as exc:              # noqa: BLE001 意图层失败不能拖垮主流程
            log.warning('意图层不可用，退回规则：%s', exc)
            judged = {'kind': '', 'segments': []}
        st.intent_kind = str(judged.get('kind') or '')
        intent_must: set[str] = set()   # 意图层这一轮自己解析出来的地名：已经核过，不再过原话闸
        for seg_ in judged.get('segments') or []:
            verb = seg_.get('verb') or ''
            place = str(seg_.get('place') or '')
            need = str(seg_.get('need') or '')
            dur = int(seg_.get('duration_min') or 0)
            poi = seg_.get('poi')
            if seg_.get('origin'):
                st.anchor_place = st.anchor_place or str(seg_['origin'])
            if verb == 'eat':
                # 「吃日料」→ 菜系，「去新街口吃」→ 吃饭片区；都不是"去那儿办事"
                if need:
                    st.taste = st.taste or need
                    st.meal = 'recommend'
                if place and str(getattr(poi, 'kind', '')) == '餐饮':
                    st.meal = poi.name
                elif place:
                    st.meal_near = st.meal_near or place
            elif verb in ('meet', 'pickup'):
                if place and place not in st.must:
                    st.must.append(place)
                    intent_must.add(place)
                st.extras = list(getattr(st, 'extras', []) or [])
                title = f'去{place}找人' if verb == 'meet' else f'去{place}接人'
                if place and not any(e.get('title') == title for e in st.extras):
                    st.extras.append({'poi': place, 'title': title,
                                      'duration_min': dur or 60,
                                      'category': '办事', 'kind': 'task'})
            elif verb == 'return':
                if st.anchor_place:
                    st.home_return = True
                    st.hotel = st.hotel or 'none'   # 当天往返，别再追问住哪儿
            elif verb == 'play' and place:
                if place not in st.must and len(st.must) < 4:
                    st.must.append(place)
                    intent_must.add(place)
            elif verb == 'go' and place and dur and place not in st.must:
                if len(st.must) < 4:
                    st.must.append(place)
                    intent_must.add(place)

        # 「起点：新街口如家酒店 / 终点：新街口如家酒店」这类**冒号写法**：
        # 用户已经把话说成结构化字段了，必须按字段吃，不能当普通句子放着。
        for m_colon in re.finditer(r'(起点|出发地|终点|目的地|住宿|酒店|出发|返回|回)\s*[:：]\s*'
                                   r'([^\n，,。；;]{2,20})', raw):
            label = m_colon.group(1)
            value = m_colon.group(2).strip()
            poi = None
            try:
                poi = self.svc._poi_named(value)
            except Exception:              # noqa: BLE001
                poi = None
            is_lodging = any(w in value for w in ('酒店', '宾馆', '饭店', '旅馆',
                                                  '民宿', '旅舍', '公寓', '客栈'))
            # 酒店名一律用**用户原话**：包里没有「新街口如家酒店」时，
            # 模糊解析会把它截成「新街口」——那不是酒店，还会把新街口排成一个景点。
            name = value if is_lodging else (getattr(poi, 'name', None) or value)
            if is_lodging or label in ('住宿', '酒店'):
                st.hotel = name
                st.anchor_place = st.anchor_place or name
                st.home_return = True
            elif label in ('起点', '出发地', '出发'):
                st.anchor_place = st.anchor_place or name
            elif label in ('终点', '目的地', '返回', '回'):
                st.home_return = True
                st.anchor_place = st.anchor_place or name

        # 「10 点出门 / 9 点半动身」：可排起点（原来只认"几点起床"）
        m_wake = re.search(r'(\d{1,2})\s*点\s*(半|\d{1,2})?\s*(?:出门|出发|动身|走|起床|起)', raw)
        if m_wake:
            hh = int(m_wake.group(1))
            tail = m_wake.group(2) or ''
            mm = 30 if tail == '半' else (int(tail) if tail.isdigit() else 0)
            if 0 <= hh <= 23 and 0 <= mm < 60:
                st.wake = f'{hh:02d}:{mm:02d}'

        # 「N 点之前回到学校/酒店」→ 当日必须收尾的时刻（不是"去那儿玩"）
        m_back = re.search(r'(\d{1,2})\s*点\s*(半|\d{1,2})?\s*(?:之前|以前|前)?\s*'
                           r'(?:回到|回|返回|到)\s*(学校|宿舍|酒店|住处|宾馆)', raw)
        if m_back:
            hh = int(m_back.group(1))
            hh = hh + 12 if (hh < 12 and re.search(r'(晚上|夜里|夜间|傍晚|下午)', raw)) else hh
            mm = 30 if (m_back.group(2) or '') == '半' else (
                int(m_back.group(2)) if (m_back.group(2) or '').isdigit() else 0)
            if 0 <= hh <= 23:
                st.day_end = f'{hh:02d}:{mm:02d}'
            if m_back.group(3) in ('学校', '宿舍') and not st.anchor_place:
                # 上文提到学校名就沿用它（"我在南邮仙林…回学校"）
                hit = next((n for n in (st.must or []) if '大学' in n or '学院' in n or '校区' in n), '')
                if hit:
                    st.anchor_place = hit
                st.note = (st.note + '；' if st.note else '') + '按你说的，当天要回到学校收尾'

        # 「晚上23点后到酒店」→ 收尾时间放宽
        # "晚上/夜里/下午 N 点"要按 24 小时制算（原来晚上 11 点会算成 11:00）
        def _to24(hh: int, tail: str) -> int:
            if hh < 12 and re.search(r'(晚上|夜里|夜间|傍晚|下午)', tail):
                return hh + 12
            return hh

        m_end = re.search(r'(\d{1,2})\s*点\s*(半|\d{1,2})?\s*(?:后|以后|之后)?\s*(?:到|回)酒店', raw)
        if m_end:
            hh = _to24(int(m_end.group(1)), raw[:m_end.start() + 8])
            mm = int(m_end.group(2) or 0) if (m_end.group(2) or '').isdigit() else 0
            if 0 <= hh <= 23:
                st.day_end = f'{min(23, hh + 1):02d}:{mm:02d}' if hh >= 23 else f'{hh:02d}:{mm:02d}'

        # 「想去A和B / A、B / A还有B」→ 每个并列地名都算点名必去
        for chunk_match in re.finditer(r'(?:想去|要去|想去看看|去|逛逛|参观|游览)\s*'
                                       r'([^。；;\n]{2,44})', raw):
            for part in re.split(r'[和、,，跟与及]|还有|以及|＋|\+', chunk_match.group(1)):
                # "老门东我不想去了"这类片段是在排除，不是要去
                if re.search(r'(不|别|取消|去掉|删掉|不要去|不想)', part):
                    continue
                poi = self._place_in_chunk(part)
                if poi is None or str(poi.kind) in ('交通', '住宿'):
                    continue
                if poi.name not in st.must and len(st.must) < 5:
                    st.must.append(poi.name)

        # 「再加个X / 加个X / 补个X」→ 点名必去（在已有行程上追加时最常用）
        for m_add in re.finditer(r'(?:再加|加个|加上|增加|添加|补个|补上|顺路去)\s*'
                                 r'([\u4e00-\u9fa5A-Za-z0-9·（）()]{2,16})', raw):
            cand = m_add.group(1).strip()
            poi = self._resolve_place(cand) if cand else None
            if poi is not None and str(poi.kind) not in ('交通', '住宿'):
                if poi.name not in st.must and len(st.must) < 5:
                    st.must.append(poi.name)

        # 「去/逛/参观 X」→ 点名必去（不依赖"找/吃"这类动词）
        try:
            for m_go in re.finditer(r'(?:去|逛|参观|游览|看看)\s*'
                                    r'([\u4e00-\u9fa5A-Za-z0-9·（）()]{2,20})', raw):
                cand = m_go.group(1).strip()
                if not cand or any(w in cand for w in ('吃饭', '饭', '酒店', '住')):
                    continue
                poi = self._resolve_place(cand)
                if poi is None or str(poi.kind) in ('交通', '住宿', '餐饮'):
                    continue
                if poi.name in self._CITY_WORDS or cand in self._CITY_WORDS:
                    continue
                if not is_place_word(poi.name):
                    continue
                if poi.name not in st.must and len(st.must) < 4:
                    st.must.append(poi.name)
        except Exception:                     # noqa: BLE001 解析失败不影响主流程
            pass

        # 「去X吃…」「去X找…」「去X接…」：**按动词分开处理**，而且每一处都要看。
        # 以前只有一次 search，抓第一处、且把"吃"也当成"找人"：
        # 「从南邮去新街口吃日料，吃完去南京理工找朋友」被拆成
        # "去新街口找人 60 分 + 吃饭（日料）"，真正要去的南京理工反而丢了。
        for _m in re.finditer(r'去\s*([\u4e00-\u9fa5A-Za-z0-9]{2,16}?)\s*([找见吃喝接])', raw):
            guess, verb = _m.group(1), _m.group(2)
            try:
                poi = self.svc._poi_named(guess)
            except Exception:                 # noqa: BLE001
                poi = None
            target = poi.name if poi is not None else guess
            if not is_place_word(target):
                continue                      # "找朋友""我们两"不是地名
            tail = raw[_m.end():_m.end() + 24]
            if verb in ('吃', '喝'):
                # "去新街口吃日料"：新街口是这顿饭的**片区**，不是要去办事的地点，
                # 更不该变成"去新街口找人"这种没说过的事项。
                if poi is not None and str(poi.kind) == '餐饮':
                    if not st.meal or st.meal == 'recommend':
                        st.meal = poi.name
                else:
                    st.meal_near = st.meal_near or target
                continue
            # 找/见/接：这才是"去某个地方办事"，时长按这句里说的算
            from .parsing_rules import parse_duration as _pd
            mins = _pd(tail) or 60
            if target not in st.must:
                st.must.append(target)
            st.extras = list(getattr(st, 'extras', []) or [])
            title = f'去{target}找人' if verb in ('找', '见') else f'去{target}接人'
            if not any(e.get('title') == title for e in st.extras):
                st.extras.append({'poi': target, 'title': title,
                                  'duration_min': int(mins), 'category': '办事',
                                  'kind': 'task'})
            if any(w in raw for w in ('吃', '午饭', '午餐', '饭')):
                # 用户没说去哪家店就不要替他定：留成"待补"，把候选抛回给他
                if not any(e.get('title', '').startswith('吃饭') for e in st.extras):
                    if st.meal and st.meal != 'recommend':
                        mp = self.svc._poi_named(st.meal)
                        st.extras.append({
                            'poi': (mp.name if mp is not None else st.meal),
                            'title': f'吃饭（{st.meal}）',
                            'duration_min': int(getattr(mp, 'visit_min', 0) or 60),
                            'category': '餐饮', 'kind': 'task'})
                    else:
                        st.extras.append({
                            'poi': '', 'title': f'在{target}附近吃午饭',
                            'duration_min': 60, 'category': '餐饮',
                            'kind': 'block', 'needs_poi': True,
                            'reason': '哪家店你定：点候选或补个店名，我按它排'})

        # 日期质检：看不懂的日期、过去的日期、跨度太长，都要说清楚
        if not st.start:
            hint = self._bad_date_hint(raw)
            if hint:
                st.note = (st.note + '；' if st.note else '') + hint
        else:
            try:
                start_day = date.fromisoformat(st.start)
            except ValueError:
                start_day = None
            if start_day is not None:
                # 本轮句子里写了个看不懂的日期时，不能默默沿用上一轮日期
                if self._bad_date_hint(raw) and not self._mentions_valid_date(raw, today):
                    st.note = (st.note + '；' if st.note else '') + (
                        self._bad_date_hint(raw)
                        + f'（先按之前说的 {st.start} 排）')
                if start_day < today - timedelta(days=1):
                    st.note = (st.note + '；' if st.note else '') + (
                        f'{st.start} 已经过去了：我按你说的这个日期排；'
                        '如果指的是明年，补一句"明年"')
                if st.date_span > 15:
                    st.end = (start_day + timedelta(days=14)).isoformat()
                    st.note = (st.note + '；' if st.note else '') + (
                        '跨度太长，先按 15 天排（够了就说"就这样"，不够再说加天）')

        # 收尾：这句只是"某一天"（后天/明天/具体某日）→ 就排这一天
        if single_day:
            kept_end = st.end
            st.start = single_day.isoformat()
            if getattr(st, 'trip_days', 0) and st.trip_days > 1:
                # 用户另外说了天数（"玩 4 天""多加一天"）
                st.end = (single_day + timedelta(days=st.trip_days - 1)).isoformat()
            elif kept_end and kept_end > st.start:
                st.end = kept_end          # 别的规则（加天/减天）已经算过终点，尊重它
            else:
                st.end = st.start
                st.trip_days = 1
            st.window = False
            st.win_start = st.win_end = ''

        # 收尾清理：排除项不能同时是"必去项"（口语会被多条规则重复抓）
        avoid_keys_final = {self._norm(a) for a in st.avoid}
        if avoid_keys_final:
            st.must = [m for m in st.must if self._norm(m) not in avoid_keys_final]

        # 「3 天内玩 5-6 个地点」：天数与总点数一起说的时候，两个槽位都要落
        m_within = re.search(r'(\d{1,2})\s*(?:天|日)\s*(?:内|里|之内)', raw)
        m_play_n = re.search(r'(?:玩|游|逛|计划)\s*(?:\d{1,2}\s*(?:天|日))?'
                             r'[^\d]{0,4}(\d{1,2})\s*[-~到]\s*(\d{1,2})\s*个\s*(?:点|景点|地点)', raw)
        if not m_play_n:
            m_play_n = re.search(r'(\d{1,2})\s*[-~到]\s*(\d{1,2})\s*个\s*(?:点|景点|地点)', raw)
        if m_within:
            st.trip_days = max(1, min(15, int(m_within.group(1))))
        if m_play_n:
            total = max(int(m_play_n.group(1)), int(m_play_n.group(2)))
            span_guess = st.trip_days or st.date_span or 1
            st.per_day = max(1, min(6, -(-total // span_guess)))
        elif re.search(r'(\d{1,2})\s*个\s*(?:点|景点|地点)', raw) and st.trip_days:
            total = int(re.search(r'(\d{1,2})\s*个\s*(?:点|景点|地点)', raw).group(1))
            st.per_day = max(1, min(6, -(-total // st.trip_days)))
        # 「吃行玩你来安排」：整趟都交给我定 → 正餐按"我挑"
        if re.search(r'(你来安排|你安排|你来定|你决定|你帮我安排)', raw) and any(
                w in raw for w in ('吃', '饭', '餐', '食')):
            st.meal = 'recommend'

        # 微调指令
        if any(w in raw for w in ('太满', '太多', '累', '少一点', '少排')):
            st.per_day = max(1, (st.per_day or self._default_per_day(st)) - 1)
        elif any(w in raw for w in ('太松', '多点', '多一点', '再加')):
            st.per_day = min(5, (st.per_day or self._default_per_day(st)) + 1)
        m = re.search(r'(?:改成|改到|变成|调整成|调整为)\s*(\d{1,2})\s*天', raw)
        if not m:
            m = re.search(r'(\d{1,2})\s*天', raw)
        if m and any(w in raw for w in ('改成', '改到', '变成', '调整')):
            n = max(1, min(15, int(m.group(1))))
            st.trip_days = n
            if st.start:
                a = date.fromisoformat(st.start)
                st.end = (a + timedelta(days=n - 1)).isoformat()
        # 最终裁决：单日就是这一天；区间解析失败（倒置/不存在）就把日期清空交给追问
        if single_day is not None:
            if getattr(st, 'trip_days', 0) and st.trip_days > 1 and not st.window:
                st.start = single_day.isoformat()
                st.end = (single_day + timedelta(days=st.trip_days - 1)).isoformat()
            else:
                st.start = st.end = single_day.isoformat()
        elif new_range is None and re.search(r'\d\s*[月./]\s*\d+\s*[日号]?\s*(?:到|至|—|~|～|-)', raw):
            st.start = st.end = ''      # 区间写了但解析不出来：让日期质检解释原因

        # 必去项必须"用户真的说过"：地名可能来自模型或模糊检索，会塞进原话里没有的点
        # （真实事故：整段话被抽出「然后精品瓷器(明发新城中心4幢店)」并排进行程）。
        kept: list[str] = []
        for name in st.must:
            if name in prev_must or name in intent_must or self._spoken_in(raw, name):
                kept.append(name)
            else:
                log.info('必去项不在原话里，丢弃：%s', name)
                st.note = (st.note + '；' if st.note else '') + \
                    f'「{name}」这句里没有提到，我没把它排进去'
        # 「然后再返校 / 回学校 / 回南邮」：当天回到出发地收尾。
        # 判定放在**所有槽位来源之后**：在线模型可能晚一步才给出出发地，
        # 早判就会漏（实测在线时 home_return 恒为 False，还在追问住宿）。
        if st.home_return and not st.hotel:
            st.hotel = 'none'
        m_home = re.search(r'(返校|回校|回学校|回宿舍|回(?:到)?[\u4e00-\u9fa5]{0,6}'
                           r'(?:大学|学院|校区|学校)'
                           r'|(?:得|要|必须|需要)(?:到|回到|回|赶回|返回)'
                           r'[\u4e00-\u9fa5]{0,4}(?:学校|宿舍|校区))', raw)
        if not st.home_return and st.anchor_place and m_home:
            st.home_return = True
            st.hotel = st.hotel or 'none'          # 当天往返：不再追问住哪儿
        # 「晚上 10 点得回学校」：这是当日必须收尾的时刻（不是"再去一个景点"）
        m_back2 = re.search(r'(\d{1,2})\s*点.*?(?:得|要|必须|需要)\s*(?:到|回到|回|赶回|返回)'
                            r'.{0,4}(?:学校|宿舍|校区)', raw)
        if m_back2:
            hh = int(m_back2.group(1))
            if hh < 12 and re.search(r'(晚上|夜里|傍晚|下午)', raw):
                hh += 12
            if 0 <= hh <= 23:
                st.day_end = f'{hh:02d}:00'

        # 出发地不排成"游览项"（见 build_plan 候选池），但**不**从必去项里删：
        # 「从总统府出发去夫子庙」里总统府就是起点，用户确实提到了它。
        if st.hotel and st.hotel not in ('recommend', 'none'):
            hotel_key = self._norm(st.hotel)
            st.must = [m for m in st.must
                       if not (hotel_key and self._norm(m) and self._norm(m) in hotel_key)]
            if st.meal and st.meal not in ('recommend', '') and self._norm(st.meal) in hotel_key:
                st.meal = 'recommend'          # 酒店名里的地名不是餐厅
            if st.meal_near and self._norm(st.meal_near) in hotel_key:
                st.meal_near = ''

        # 吃饭的片区不该同时是"必去景点"（"去新街口吃日料"说的是吃饭，不是逛新街口）
        if st.meal_near:
            near_key = self._norm(st.meal_near)
            st.must = [m for m in st.must if self._norm(m) != near_key]
        if st.meal_near:
            near_key = self._norm(st.meal_near)
            st.must = [m for m in kept if self._norm(m) != near_key]
        # 最后一刀：酒店名 / 吃饭片区里的地名片段不许留在必去项里
        # （"新街口如家酒店"里的"新街口"是地址，不是要逛的地方）
        hotel_key = self._norm(st.hotel) if st.hotel not in ('', 'recommend', 'none') else ''
        if hotel_key:
            st.must = [m for m in st.must
                       if not (self._norm(m) and self._norm(m) in hotel_key)]
            if (st.meal and st.meal not in ('', 'recommend')
                    and self._norm(st.meal) in hotel_key):
                st.meal = 'recommend'          # 酒店名里的地名不是餐厅
                st.meal_near = st.meal_near or ''

        return st

    def _spoken_in(self, raw: str, name: str) -> bool:
        """这个名字是不是真的在用户这句话里出现过（含包内别名）"""
        text = str(raw or '')
        spoken = self._norm(name)
        if not spoken or not text:
            return False
        if spoken in self._norm(text):
            return True
        # 包内标准名常常比用户说的长（「南京理工」→「南京理工大学(钟山校区)」）：
        # 前缀 ≥3 字出现在原话里就认，太短的（"然后"）不算
        plain = re.sub(r'[（(][^）)]*[）)]', '', str(name or '')).strip()
        for cand in (plain, spoken):
            for cut in range(len(cand), 2, -1):
                if cand[:cut] in text:
                    return True
        # 别名（南博 / 南邮）
        try:
            pack = get_city(self.svc.city)
            for alias in (pack.aliases() or {}):
                # 城市名与通用词不算别名：「南京」会让「南京邮电大学」被判成「南京站」
                if (not alias or len(alias) < 2 or alias in self._CITY_WORDS
                        or alias == pack.name or alias not in raw):
                    continue
                try:
                    got = self.svc._poi_named(alias)
                except Exception:             # noqa: BLE001
                    got = None
                if got is not None and self._norm(got.name) == spoken:
                    return True
        except Exception:                     # noqa: BLE001
            pass
        return False

    @staticmethod
    def _default_per_day(st: ConsultSession) -> int:
        return {'slow': 2, 'fast': 4}.get(st.pace, 3)

    # ---------- 提问策略 ----------
    def questions(self, st: ConsultSession) -> list[dict]:
        """返回还缺什么（每轮最多问两个；**问过的不会再问第二遍**）"""
        qs: list[dict] = []
        if not st.start and 'dates' not in st.asked:
            qs.append({'key': 'dates',
                       'ask': self._ask_text('dates', st,
                                             ['明天', '9月9号', '9月10号-9月16号']),
                       'options': ['明天', '9月9号', '9月10号-9月16号']})
        if not st.hotel and not getattr(st, 'home_return', False) and 'hotel' not in st.asked:
            names = [h['name'] for h in self.hotel_options(st, limit=3)]
            lead = '住哪儿？' + ('按你的行程分布挑了这几家：' if names else '给我个酒店名：')
            qs.append({'key': 'hotel',
                       'ask': lead + '、'.join(names)
                              + '；点一个，或者说「你帮我选」，也可以「不用酒店（当天往返）」',
                       'options': names + ['你帮我选', '不用酒店（当天往返）']})
        if not st.meal and 'meal' not in st.asked:
            qs.append({'key': 'meal',
                       'ask': '正餐想怎么解决？说个店名或口味都行；'
                              '不想操心就说「就近随便」（按当天位置找）或「你帮我选」（挑评分高的）',
                       'options': ['就近随便', '你帮我选', '不吃辣', '清真']})
        return qs[:2]

    # ---------- 住宿候选（带价位来源标注 + 预算过滤） ----------
    def hotel_options(self, st: 'ConsultSession', limit: int = 3) -> list[dict]:
        """按预算档给住宿候选：省档只留起价 ≤400 的；无起价的标"未标价"

        价位来源分两种，界面必须标清楚：
          · `高德起价`  —— 在线取到的 `lowest_price`（带取数日期）
          · `参考价`    —— 城市包里的种子估价（离线/取数失败时）
        """
        pack = get_city(self.svc.city)
        rows = [r for r in pack.poi_table() if str(r.get('kind') or '') == '住宿']
        out: list[dict] = []
        intel = getattr(self.svc, 'intel', None)
        for r in rows:
            poi = Poi.from_pack(r, self.svc.city)
            price_num = None
            price_label = ''
            if intel is not None:
                got = intel.cached(poi.name, self.svc.city) or {}
                low = str(got.get('lowest_price') or '')
                if low:
                    try:
                        price_num = float(low)
                        day = str(got.get('fetched_at') or '')[:10]
                        price_label = f'¥{price_num:.0f} 起（高德 {day}）'
                    except (TypeError, ValueError):
                        price_num = None
            if price_num is None:
                import re as _re
                m = _re.search(r'(\\d+)\\s*元|¥\\s*(\\d+)', str(poi.price or ''))
                if m:
                    try:
                        price_num = float(m.group(1) or m.group(2))
                        price_label = f'约 ¥{price_num:.0f}（参考价）'
                    except (TypeError, ValueError):
                        price_num = None
            if not price_label:
                price_label = '未标价'
            out.append({'name': poi.name, 'price': price_num,
                        'price_label': price_label,
                        'star': getattr(poi, 'star', '') or '',
                        'district': poi.district})
        if st.budget == '省':
            cheap = [h for h in out if h['price'] is not None and h['price'] <= 400]
            out = cheap or out
        out.sort(key=lambda h: (h['price'] is None, h['price'] or 0))
        return out[:limit]

    def meal_options(self, st: 'ConsultSession', near=None,
                     limit: int = 3) -> list[dict]:
        """正餐候选：先按口味过滤，再按"离 anchor 近 + 评分高"排序"""
        foods = [p for p in self._pool() if str(p.kind) == '餐饮'
                 and not any(w in p.name for w in _DRINK_WORDS)]
        taste = str(getattr(st, 'taste', '') or '')
        if taste == '清真':
            foods = [f for f in foods if '清真' in f.name] or foods
        elif taste == '不吃辣':
            foods = [f for f in foods
                     if not any(w in f.name for w in ('麻辣', '辣', '川', '渝'))] or foods
        elif taste in _CUISINE_WORDS:
            # 菜系（日料/火锅…）：跟 meal_options 同一条口径，不许换成别的菜系顶替
            keys = _CUISINE_WORDS[taste]
            foods = [f for f in foods
                     if any(w in f.name or w in str(getattr(f, 'cuisine', '') or '')
                            for w in keys)]
        elif taste in _CUISINE_WORDS:
            # 菜系（日料/火锅/小吃…）：城市包里没有这一类的就返回空，
            # 由调用方标成"待补"——不许拿别的菜系冒充，也不许把"日料"当店名。
            keys = _CUISINE_WORDS[taste]
            foods = [f for f in foods
                     if any(w in f.name or w in str(getattr(f, 'cuisine', '') or '')
                            for w in keys)]
        if near is not None:
            foods = sorted(foods, key=lambda f: (_km(f, near) > 4.0,
                                                 -self._rating(f), _km(f, near)))
        else:
            foods = sorted(foods, key=lambda f: -self._rating(f))
        return [{'name': f.name, 'rating': round(self._rating(f), 1),
                 'district': f.district} for f in foods[:limit]]

    def _ask_text(self, key: str, st: 'ConsultSession',
                  options: list[str]) -> str:
        """追问文案：模型说顺一点；同一个槽位只请求一次（省钱也省时间）"""
        cache = getattr(self, '_ask_cache', None)
        if cache is None:
            cache = {}
            self._ask_cache = cache
        if key in cache:
            return cache[key]
        ctx = []
        if st.start:
            ctx.append(f'日期 {st.start}~{st.end}')
        if st.party:
            ctx.append(f'{st.party} 人')
        if st.interests:
            ctx.append('兴趣 ' + '、'.join(st.interests))
        try:
            from . import llm_intake
            text = llm_intake.phrase_question(key, '；'.join(ctx),
                                              self.svc.degrade, options)
        except Exception:                    # noqa: BLE001
            text = ''
        cache[key] = text or '再补充一点信息？'
        return cache[key]

    # ---------- 出方案 ----------
    def build_plan(self, st: ConsultSession) -> dict:
        # 统一规划器：不再按"事务/观光"分两套模板。
        # practical 只作为统计标记（用户是否有到达/办事/返程这类要素），不改变排程路径；
        # 要不要补点由"剩余容量 + 顺路"决定（见下方 backfill 门槛）。
        practical_mode = bool(st.practical)

        pool = self._pool()
        avoid_keys = {self._norm(a) for a in (getattr(st, 'avoid', None) or [])}
        # 当天已经排进去的地点：既是"不要在草案里重复推荐"，也要占掉每日配额
        scheduled_by_day = self._scheduled_locations(st)

        def avoid(name: str) -> bool:
            return bool(avoid_keys) and self._norm(name) in avoid_keys

        # 出发点不进候选池：它就是出发地，不该被"顺路补点"补成一个游览项目
        anchor_key = self._norm(getattr(st, 'anchor_place', '') or '')
        sights = [p for p in pool if str(p.kind) in _SIGHT_KINDS and not avoid(p.name)
                  and not (anchor_key and self._norm(p.name) == anchor_key)]
        # 吃饭只认真餐饮：咖啡/奶茶店不适合当正餐（顾问不该推荐"喜茶当午饭"）
        foods = [p for p in pool if str(p.kind) == '餐饮'
                 and not any(w in p.name for w in _DRINK_WORDS) and not avoid(p.name)]
        taste = str(getattr(st, 'taste', '') or '')
        if taste == '清真':
            foods = [f for f in foods if '清真' in f.name] or foods
        elif taste == '素':
            foods = [f for f in foods
                     if not any(w in f.name for w in ('烤', '肉', '鸡', '鱼', '虾', '牛'))] or foods
        elif taste == '不吃辣':
            foods = [f for f in foods
                     if not any(w in f.name for w in ('麻辣', '辣', '川', '渝'))] or foods
        per_day = st.per_day or self._default_per_day(st)

        # 点名"必须去/某天要去"的地点，直接放到第一天开头，不参与评分淘汰
        must_pois = []
        for nm in st.must:
            try:
                poi = self.svc._poi_named(nm)
            except Exception:                 # noqa: BLE001
                poi = None
            if poi is None or poi in must_pois or avoid(poi.name):
                continue
            # 同一个地方可能有两种写法（"南京邮电大学仙林校区" / "南京邮电大学(仙林校区)"）
            if any(self._norm(m.name) == self._norm(poi.name) for m in must_pois):
                continue
            # 点名的是**餐厅**：那是"这顿吃什么"，不占景点名额。
            # 街区/商圈/商场（1912街区、夫子庙、德基广场）本身就能逛，
            # 用户列进必去就别偷偷变成一顿饭，否则会出现"在1912街区吃饭 + 游览1912街区"两条。
            if str(poi.kind) == '餐饮':
                if not st.meal or st.meal == 'recommend':
                    st.meal = poi.name
                continue
            must_pois.append(poi)
        # 排掉"已经点名过的点"。这里必须按规范化名字比，不能靠对象相等：
        # 候选池里的 Poi 与点名得到的 Poi 是两个实例，同一处地点会被算成两处，
        # 于是 6 个名额里 3 个被同一批点名点占掉，剩下的天才空着（实测"3 天 2 点"
        # 出来只有前两天有点、第三天留白）。
        must_keys = {self._norm(p.name) for p in must_pois}
        ranked = sorted([p for p in sights if self._norm(p.name) not in must_keys],
                        key=lambda p: self._score(p, st), reverse=True)
        # 同一连锁只留评分最高的那一家，避免一趟行程推荐两家"西西弗"
        ranked = dedupe_brands(ranked)
        ranked = must_pois + ranked
        span = st.date_span or 1
        # 「11月中旬」这种是窗口不是行程：窗口 10 天不等于要玩 10 天
        win_a = win_b = None
        if getattr(st, 'window', False):
            ws = getattr(st, 'win_start', '') or st.start
            we = getattr(st, 'win_end', '') or st.end
            try:
                win_a = date.fromisoformat(ws)
                win_b = date.fromisoformat(we)
            except (TypeError, ValueError):
                win_a = win_b = None
        if getattr(st, 'trip_days', 0):
            span = max(1, min(15, int(st.trip_days)))
            if win_a is not None:
                span = max(1, min(span, (win_b - win_a).days + 1))
        elif win_a is not None:
            span = max(1, min(span, 3))
        picked = ranked[:span * per_day]

        # 地理分组：相邻点同一天（每天能串成一条线，不横跨全城）
        groups: list[list[Poi]] = []
        rest = list(picked)
        while rest:
            seed = max(rest, key=lambda p: sum(_km(p, q) for q in rest))
            rest.remove(seed)
            g = [seed]
            while rest and len(g) < per_day:
                nxt = min(rest, key=lambda p: _km(p, g[-1]))
                rest.remove(nxt)
                g.append(nxt)
            groups.append(g)
        groups.sort(key=lambda g: max(self._score(p, st) for p in g), reverse=True)

        # 到达日：先到站、再去酒店放行李，之后才出门玩（否则等于拖着箱子逛景点）
        hotel_poi = None
        if st.hotel:                              # '' = 还没定，等他回答
            try:
                if st.hotel == 'recommend':
                    opts = self.hotel_options(st, limit=1)
                    if opts:
                        hotel_poi = self.svc._poi_named(opts[0]['name'])
                else:
                    hotel_poi = self.svc._poi_named(st.hotel)
            except Exception:                     # noqa: BLE001
                hotel_poi = None

        start = date.fromisoformat(st.start) if st.start else date.today() + timedelta(days=1)
        days: list[dict] = []
        used: set[str] = set()
        used_meals: set[str] = set()
        meal_done = False          # 用户指定的餐厅只落一顿，其余天仍按"就近待定"处理
        for i in range(span):
            day = start + timedelta(days=i)
            wd = '一二三四五六日'[day.weekday()]
            existing_items = scheduled_by_day.get(day.isoformat(), [])
            already = {self._norm(it['poi']) for it in existing_items if it.get('poi')}
            # 已有 N 项就少排 N 项：用户说"再加一个点"，不能又塞进一整组
            day_quota = max(0, per_day - len(existing_items))
            chosen: list[Poi] = []
            for g in groups[i:] if i < len(groups) else []:
                for poi in g:
                    if len(chosen) >= day_quota:
                        break
                    key_name = self._norm(poi.name)
                    if (key_name in used or key_name in already or avoid(poi.name)
                            or wd in tuple(poi.closed_days or ())):
                        continue
                    # 顺路门槛：不是我点名的点，离当天第一个点超过 4 km 就不塞
                    if (chosen and poi not in must_pois
                            and _km(poi, chosen[0]) > 4.0):
                        continue
                    # 一天最多一个"大景区"：牛首山 + 栖霞山这种组合现实中跑不完。
                    # 但用户点名必去的（"我要去南邮找朋友"）不受这条限制——那是他指定的。
                    if (is_big_sight(poi) and poi not in must_pois
                            and any(is_big_sight(p) for p in chosen)):
                        continue
                    chosen.append(poi)
                    used.add(key_name)
                if len(chosen) >= day_quota:
                    break
            # 一天的顺序交给纯函数（只改顺序、不改成员）：闭馆早的先排、
            # 夜游类挪最后、同类内部就近串联
            try:
                from .ordering import order_day as _order_day
                chosen = _order_day(chosen, distance=_km)
            except Exception as exc:          # noqa: BLE001 排序失败就保留原顺序
                log.warning('排序失败，保留原顺序：%s', exc)
            intel = getattr(self.svc, 'intel', None)
            if intel is not None:
                for p_ in chosen:
                    try:
                        intel.apply(p_)          # 只为入选的点取真实营业时间/时长
                    except Exception:            # noqa: BLE001
                        pass
            items = [{'poi': p.name, 'title': f'游览{p.name}',
                      'duration_min': int(p.visit_min or 90), 'kind': 'task',
                      'category': '展览',
                      'open': ('' if (not p.open_from or str(p.open_from) == '00:00'
                                      and str(p.open_to) == '23:59')
                               else f'{p.open_from}-{p.open_to}')}
                     for p in chosen]
            # 带小孩：午饭后补一段午睡（真实带娃行程绕不开）
            if st.party >= 3 and any(w in (getattr(st, 'note', '') + str(st.interests))
                                     for w in ('孩子', '娃', '小孩', '亲子')):
                items.append({'poi': '', 'title': '回酒店午睡', 'duration_min': 90,
                              'kind': 'block', 'category': '休息', 'open': '',
                              'needs_poi': False, 'reason': '带小孩按 1.5 小时预留'})
            # 已经有"某某附近吃饭"（来自中途追加或待补）就不再插一条
            has_meal = any('吃饭' in str(it.get('title') or '') for it in items)
            meal_pick = None
            if st.meal and st.meal != 'recommend':
                try:
                    meal_pick = self.svc._poi_named(st.meal)
                except Exception:             # noqa: BLE001
                    meal_pick = None
            meal_label = ''
            if (st.meal and st.meal != 'recommend' and meal_pick is None
                    and not has_meal and not meal_done):
                # 店名没核到（城市包里没有、离线取不到）：照用户说的留，不替换成别家
                meal_label = st.meal
                items.insert(1 if len(items) > 1 else len(items), {
                    'poi': '', 'title': f'吃饭（{meal_label}）',
                    'duration_min': 60, 'kind': 'block', 'category': '餐饮',
                    'open': '', 'needs_poi': False,
                    'reason': '按你说的这家排；店名没在城市包里核实，出门前确认下地址'})
                has_meal = True
                meal_done = True
            if foods and chosen and not has_meal and meal_pick is not None and not meal_done:
                # 商场/街区里吃饭只需要一顿饭的时间，不是逛街时间
                eating_min = (60 if str(meal_pick.kind) in ('商场', '街区', '商圈')
                              else int(getattr(meal_pick, 'visit_min', 0) or 60))
                items.insert(1 if len(items) > 1 else len(items), {
                    'poi': meal_pick.name,
                    'title': (f'在{meal_pick.name}吃饭'
                              if str(meal_pick.kind) in ('商场', '街区', '商圈') else '吃饭'),
                    'duration_min': eating_min,
                    'kind': 'task', 'category': '餐饮',
                    'open': (f'{meal_pick.open_from}-{meal_pick.open_to}'
                             if meal_pick.open_from else '')})
                has_meal = True
                meal_done = True
            if foods and chosen and not has_meal and st.meal == 'recommend':
                pool_f = [f for f in foods if f.name not in used_meals] or foods
                lunch = min(pool_f, key=lambda f: _km(f, chosen[0]))
                far = _km(lunch, chosen[0])
                if far > 6.0:
                    # 附近没有匹配的餐饮：保留吃饭时间（无地点块），别把用户带到 6 公里外
                    items.insert(1 if len(items) > 1 else len(items), {
                        'poi': '', 'title': f'在{chosen[0].name}附近就近吃饭',
                        'duration_min': 60, 'kind': 'block', 'category': '餐饮',
                        'open': '', 'needs_poi': False, 'reason': ''})
                    has_meal = True
                else:
                    used_meals.add(lunch.name)
                    items.insert(1 if len(items) > 1 else len(items), {
                        'poi': lunch.name, 'title': '吃饭',
                        'duration_min': int(lunch.visit_min or 60), 'kind': 'task',
                        'category': '餐饮',
                        'open': f'{lunch.open_from}-{lunch.open_to}' if lunch.open_from else ''})
                    has_meal = True
            if foods and chosen and not has_meal:
                area = st.meal_near or chosen[0].name
                want = str(getattr(st, 'taste', '') or '')
                label = f'在{area}附近吃{want}' if want else f'在{area}附近吃午饭'
                items.insert(1 if len(items) > 1 else len(items), {
                    'poi': '', 'title': label,
                    'duration_min': 60, 'kind': 'block', 'category': '餐饮',
                    'open': '', 'needs_poi': False,
                    'reason': '吃哪儿你定：说个店名或点候选，我换成具体餐厅'})
            # 当天往返：起止点要出现在时间轴上（每日起止点是验收明确要求的一项）
            # 知道出发点就要显示起止点：以前绑在"说了返校"上，
            # 于是"我在南邮……去三牌楼参观"这种没有"返校"字样的行程看不到起点终点。
            if st.anchor_place:
                items.insert(0, {'poi': st.anchor_place,
                                 'title': f'从{st.anchor_place}出发',
                                 'duration_min': 20, 'kind': 'block',
                                 'category': '交通', 'open': ''})
                items.append({'poi': st.anchor_place,
                              'title': f'返回{st.anchor_place}',
                              'duration_min': 30, 'kind': 'block',
                              'category': '交通', 'open': ''})
            # 中途追加的临时事项（接人/办事）挂在第一天：排在最前面，避免被"顺路优化"吃掉
            # 朋友只在周末有空时，只在周末那天挂（日期已按周末整体挪过，见下方 notes）
            extras = list(getattr(st, 'extras', []) or [])
            if getattr(st, 'friend_weekend', False) and day.weekday() < 5:
                extras = []
            if i == 0 and st.luggage and hotel_poi is not None:
                # 到达 + 放行李：两条无景点负担的时间块，排在最前
                items.insert(0, {'poi': hotel_poi.name, 'title': '到酒店放行李',
                                 'duration_min': 40, 'kind': 'task',
                                 'category': '办事', 'open': ''})
                if st.arrival:
                    items.insert(0, {'poi': st.arrival, 'title': f'抵达{st.arrival}',
                                     'duration_min': 30, 'kind': 'block',
                                     'category': '交通', 'open': ''})
            if extras and i == 0:
                existing = {it.get('poi') for it in items}
                for ex in extras[:2]:
                    # 顾问自己会按"吃饭片区 + 菜系"排午饭时，extras 里那条午饭不要再插一遍
                    if ('午饭' in str(ex.get('title') or '')
                            and (st.meal_near or st.meal == 'recommend')):
                        continue
                    # 用户在追问里点了餐厅 → 填进这顿"待补午饭"，别再标成缺地点
                    if (meal_pick is not None and not ex.get('poi')
                            and any(w in str(ex.get('title') or '') for w in ('午饭', '吃饭'))):
                        ex = dict(ex, poi=meal_pick.name, needs_poi=False,
                                  title=f'吃饭（{meal_pick.name}）',
                                  duration_min=int(getattr(meal_pick, 'visit_min', 0) or 60))
                    if ex.get('poi') and ex.get('poi') in existing:
                        continue            # 必须项已经排进去了，别重复出现两次
                    # 吃饭类事项排在第一个点之后，别把午饭放在"去南邮找人"前面
                    at = 1 if (str(ex.get('category') or '') == '餐饮'
                               and len(items) > 1) else 0
                    items.insert(at, {'poi': ex.get('poi') or '',
                                     'title': ex.get('title'),
                                     'duration_min': int(ex.get('duration_min') or 40),
                                     'kind': ex.get('kind') or 'task',
                                     'category': ex.get('category') or '办事',
                                     'open': '',
                                     # 待补标记与原因必须带过去，否则界面认不出"缺地点"
                                     'needs_poi': bool(ex.get('needs_poi')),
                                     'reason': ex.get('reason') or ''})
            seen_titles, uniq = set(), []
            for it in items:
                key = str(it.get('title') or '')
                if key in seen_titles:
                    continue
                seen_titles.add(key)
                uniq.append(it)
            items = uniq
            theme = self._theme_of(chosen, st)
            # 这天原本有没有安排：有就必须说清楚"确认后是追加"，不能让人以为会覆盖
            existing = 0
            try:
                existing = len(self.svc.day_view(day).tasks)
            except Exception:                 # noqa: BLE001 查不到就当没有
                existing = 0
            days.append({
                'date': day.isoformat(), 'weekday': wd, 'theme': theme,
                'existing': len(existing_items), 'existing_items': existing_items,
                'why': self._why(chosen, st),
                'items': items, 'sights': len(chosen),
                'stops': [it['poi'] for it in items],
            })

        notes: list[str] = []
        hotel_choices = []
        if not st.hotel or st.hotel == 'recommend':
            try:
                hotel_choices = [{'name': h['name'], 'price_label': h['price_label'],
                                  'district': h.get('district') or ''}
                                 for h in self.hotel_options(st, limit=3)]
            except Exception:              # noqa: BLE001 住宿候选失败不影响出方案
                hotel_choices = []
        for day_row in days:
            if day_row.get('existing_items'):
                shown = '、'.join(
                    f"{it['poi'] or it['title']}{(' ' + it['start']) if it.get('start') else ''}"
                    for it in day_row['existing_items'][:4])
                notes.append(f"{day_row['date']} 已有 {day_row['existing']} 项"
                             f"（{shown}），本次是追加，不会覆盖")
        if hotel_poi is not None and st.hotel == 'recommend':
            notes.append(f'住宿是我按行程分布挑的：{hotel_poi.name}（可以换，左侧锚点里改）')
        if win_a is not None and days:
            friend_idx = None
            fw = [m for m in st.must if m] + ['南邮', '南京邮电', '找朋友']
            for k, d in enumerate(days):
                if any(any(w and (w in str(it.get('poi') or '')
                                  or w in str(it.get('title') or '')) for w in fw)
                       for it in d['items']):
                    friend_idx = k
                    break
            if friend_idx is not None and getattr(st, 'friend_weekend', False):
                pick = None
                for k in range((win_b - win_a).days + 1):
                    d0 = win_a + timedelta(days=k)
                    if d0 + timedelta(days=span - 1) > win_b:
                        break
                    if (d0 + timedelta(days=friend_idx)).weekday() >= 5:
                        pick = d0
                        break
                if pick is not None:
                    for k, d in enumerate(days):
                        dd = pick + timedelta(days=k)
                        d['date'] = dd.isoformat()
                        d['weekday'] = '一二三四五六日'[dd.weekday()]
                    fd = pick + timedelta(days=friend_idx)
                    notes.append('朋友工作日没空，所以把「去他那儿吃饭」这天排到 '
                                 f'{fd.isoformat()}（周{"六日"[fd.weekday() - 5]}）')
                else:
                    notes.append('这个中旬窗口里凑不出含周末的连续几天，最好换个日期再约朋友')
            notes.append(f'中旬窗口是 {win_a.isoformat()}~{win_b.isoformat()}，'
                         f'先按 {span} 天给你排（想多玩几天说一句就行）')
            st.start = days[0]['date']
            st.end = days[-1]['date']
        st.days = days
        # 餐饮候选锚在"第一天第一个地点"附近：避免把仙林那天配到新街口的店
        meal_near = None
        for day_row in days[:1]:
            for item in day_row['items']:
                if item.get('poi'):
                    try:
                        meal_near = self.svc._poi_named(item['poi'])
                    except Exception:          # noqa: BLE001
                        meal_near = None
                    break
            if meal_near is not None:
                break
        new_count = sum(len(d.get('items') or []) for d in days)
        busy_days = sum(1 for d in days if d.get('existing'))
        if new_count == 0 and busy_days:
            # 已有安排把每日配额占满时，不能还说"草案已就绪"然后一个字都不给：
            # 用户会以为方案生成了、只是没显示（D-91 同一类"说了没做"）。
            headline = (f'这 {span} 天已经有安排了（每天 {per_day} 个点已占满），'
                        f'这次没有再塞新点；要加就说「每天 {per_day + 1} 个点」'
                        f'或者直接点名要我换掉哪一个')
        else:
            headline = f'{span} 天草案已就绪（每天 {per_day} 个点）'
        return {
            'ok': True, 'start': st.start, 'end': st.end, 'days_total': span,
            'notes_extra': notes, 'notes': notes,
            'existing_days': [d['date'] for d in days if d.get('existing')],
            'hotel_options': hotel_choices,
            'meal': st.meal,
            'meal_options': ([] if st.meal and st.meal != 'recommend'
                             else self.meal_options(st, near=meal_near, limit=3)),
            'pace': st.pace or 'normal', 'wake': st.wake or '09:00',
            'party': st.party or 1, 'interests': st.interests,
            'hotel': st.hotel, 'per_day': per_day,
            'hotel_undecided': not bool(st.hotel),
            'day_end': getattr(st, 'day_end', '') or '',
            'must': list(st.must),
            'arrival': st.arrival, 'luggage': bool(st.luggage),
            'hotel_pick': (hotel_poi.name if hotel_poi is not None else ''),
            'days': days,
            'message': headline + ('；' + '；'.join(notes) if notes else ''),
        }

    #: "别排进来"的说法（软排除：不出现在新草案里即可）
    _SOFT_NO = ('不想去', '不想再', '不去', '别去', '不要去', '不想要')
    #: "从已有行程里拿掉"的说法（硬删除：要真的删数据）
    _HARD_NO = ('去掉', '删掉', '删除', '取消', '拿掉', '移掉', '不要了', '换掉')

    @staticmethod
    def _mentions_valid_date(raw: str, today: date) -> bool:
        """这句里是否写出了能解析的日期（用于区分"没提日期"和"日期写错了"）"""
        from .proposal import parse_date_range, parse_single_date
        return bool(parse_date_range(raw, today) or parse_single_date(raw, today))

    @staticmethod
    def _bad_date_hint(raw: str) -> str:
        """日期看不懂时给出具体原因，不要泛泛地问是哪一天"""
        text = raw or ''
        m = re.search(r'(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?', text)
        if m:
            month, day = int(m.group(1)), int(m.group(2))
            if not 1 <= month <= 12:
                return f'没有 {month} 月这个月份，日期我没看懂'
            if not 1 <= day <= 31:
                return f'{month} 月没有 {day} 号，日期我没看懂'
        if re.search(r'\d{4}\s*[-/.年]\s*\d{1,2}', text):
            return '这个年份日期没解析出来（写法试试 2026-11-14 或 2026年11月14号）'
        if re.search(r'(月|号|日)[^。；]{0,6}(到|至|—|~|～)[^。；]{0,6}(月|号|日)', text):
            return '区间没看懂：起始日是不是晚于结束日了？'
        return ''

    def exclusion_names(self, raw: str) -> list:
        """从一句话里读出"不要哪些地点"，返回 [(地名, 是否要动数据)]

        中文语序两边都有（"别去夫子庙" / "把夫子庙去掉"），所以先用动词/名词两侧都试，
        候选词再交给 `_resolve_place` 从尾巴往前裁——裁到能解析出地点为止，
        这样"11月5号这天把夫子庙去掉"里的时间状语不会被当成地名。
        """
        text = raw or ''
        words = self._SOFT_NO + self._HARD_NO
        verb_pat = '|'.join(words)
        found: list = []
        seen: set = set()

        def take(candidate: str) -> None:
            name = self._trim_to_place(candidate)
            if not name:
                return
            key = self._norm(name)
            if key in seen:
                return
            seen.add(key)
            found.append(name)

        # 名词在前：把X去掉 / X不要了
        for m_pos in re.finditer(rf'把\s*([\u4e00-\u9fa5A-Za-z0-9·（）()]{{2,20}}?)\s*(?:{verb_pat})',
                                 text):
            take(m_pos.group(1))
        for m_pos in re.finditer(rf'(?:{verb_pat})\s*([\u4e00-\u9fa5A-Za-z0-9·（）()]{{2,20}})',
                                 text):
            take(m_pos.group(1))
        # 名词在前的口语："老门东我不想去了 / 夫子庙我不去了"
        for m_soft in re.finditer(r'([\u4e00-\u9fa5A-Za-z0-9·（）()]{2,20})\s*(?:我|我们|都|也)?\s*'
                                  r'(?:不想去|不去了|不想再|不去|别去|不要去)', text):
            take(m_soft.group(1))

        # 尾部无"把"的简化说法：X 去掉 / X 不要了（交给裁剪逻辑兜）
        tail_pat = '|'.join(self._HARD_NO)
        for m_tail in re.finditer(rf'([\u4e00-\u9fa5A-Za-z0-9·（）()]{{2,24}}?)\s*(?:{tail_pat})', text):
            take(m_tail.group(1))

        hard_words = self._HARD_NO
        return [(name, any(w in text for w in hard_words)) for name in found]

    #: 这些词是"城市/行政区"，不是可游玩的点位（"去南京玩"不等于"去南京站"）
    _CITY_WORDS = ('南京', '南京市', '北京', '北京市', '上海', '上海市', '杭州', '苏州',
                   '玄武区', '秦淮区', '建邺区', '鼓楼区', '浦口区', '栖霞区',
                   '雨花台区', '江宁区', '六合区', '溧水区', '高淳区', '市区', '郊区',
                   '老城区', '新城区', '周边')

    def _place_in_chunk(self, chunk: str):
        """从"南京博物院看展2小时"这种片段里抠出地点：去掉时长/动词噪声，再逐段解析"""
        text = re.sub(r'\d{1,2}\s*[点时]\s*(半|\d{1,2})?', ' ', str(chunk or ''))
        text = re.sub(r'\d+\s*(分钟|小时|h|H)', ' ', text)
        text = re.sub(r'(看展|参观|游览|逛逛|逛街|转转|吃饭|用餐|集合|出发)+', ' ', text)
        text = text.strip(' 　的了吧啊')
        if len(text) < 2 or text in self._CITY_WORDS or not is_place_word(text):
            return None
        poi = self._resolve_place(text)
        if poi is not None:
            return poi
        # 尾部还有噪声就一点点剁掉再试（最多 6 个字）
        for cut in range(1, min(7, len(text) - 1)):
            poi = self._resolve_place(text[:-cut].strip())
            if poi is not None:
                return poi
        return None

    def _trim_to_place(self, candidate: str) -> str:
        """把"这天把夫子庙"这类长候选裁成真正的地名；裁不出来就返回空"""
        text = (candidate or '').strip()
        if not text:
            return ''
        for cut in range(0, min(12, len(text))):
            piece = text[cut:].strip()
            if len(piece) < 2:
                continue
            if any(piece.startswith(w) for w in ('这天', '那天', '今天', '明天', '当天',
                                                 '行程', '安排', '我们')):
                continue
            poi = self._resolve_place(piece)
            if poi is not None:
                return poi.name
        return ''

    #: 常见简称 → 包内标准名的关键词（"南邮仙林" → 南京邮电大学仙林校区）
    _NICK = {'南邮': '南京邮电大学', '东大': '东南大学', '南大': '南京大学',
             '南师': '南京师范大学', '南财': '南京财经大学', '南中医': '南京中医药大学'}

    @staticmethod
    def _norm(name: str) -> str:
        """规范化地名：去括号内容、去"地铁站/公交站/站"后缀，便于"三山街地铁站"对"三山街(地铁站)" """
        s0 = re.sub(r'[（(][^）)]*[）)]', '', str(name or '')).strip()
        s0 = re.sub(r'(地铁站|公交站|火车站|站)$', '', s0).strip()
        return s0

    #: 句子里的"吃饭"说法（越多越能覆盖真实口语）
    _MEAL_VERBS = ('吃饭', '用餐', '聚餐', '吃午饭', '吃午餐', '吃晚饭', '吃晚餐', '吃点东西')
    #: 抽出来的词如果只是这些，说明没点名地点
    _MEAL_STOP = ('饭', '午饭', '午餐', '晚饭', '晚餐', '东西', '点东西', '茶', '咖啡',
                  '吃喝玩', '吃喝', '吃的玩的', '好吃的', '好玩', '喝玩',
                  '便饭', '清真', '清真的', '素的', '素', '不辣', '不吃辣', '辣的', '辣',
                  '随便', '都行', '什么都行', '简单的')

    @staticmethod
    def _branch_of(name: str) -> str:
        """取出店名里的分店标记：新白鹿（德基店） → 德基店"""
        m = re.search(r'[（(]([^）)]{1,10})[）)]', str(name or ''))
        if m:
            return m.group(1).strip()
        m = re.search(r'([\u4e00-\u9fa5]{1,6}店)$', str(name or '').strip())
        return m.group(1) if m else ''

    def explicit_meal_place(self, raw: str) -> str:
        """用户点名的吃饭地点：返回地点名（解析不到就原样返回，绝不改成别家）

        覆盖三种真实说法：
          · "中午在德基广场吃饭"        → 商场（店在商场里）
          · "中午去南京大牌档德基店吃饭" → 具体餐厅
          · "中午去新白鹿（德基店）吃饭" → 城市包里没有的店，也照用户说的留
        """
        text = (raw or '').strip()
        if not any(w in text for w in self._MEAL_VERBS + ('吃',)):
            return ''
        patterns = (
            r'(?:在|去|到)\s*([^，。；、\s]{2,24}?)\s*(?:吃饭|用餐|聚餐|吃午饭|吃晚饭)',
            r'(?:午饭|午餐|晚饭|晚餐|吃饭)\s*(?:就|要)?\s*(?:在|去|到)\s*([^，。；、\s]{2,24})',
            r'吃\s*([\u4e00-\u9fa5A-Za-z0-9·（）()]{2,24})',
        )
        for pattern in patterns:
            m = re.search(pattern, text)
            if not m:
                continue
            cand = m.group(1).strip()
            cand = re.sub(r'[（(]\s*$', '', cand).strip()
            # 尾部常见噪声：句子的后半截（"完饭要去牛首山玩"）
            cand = re.split(r'(要|然后|再|我们|他们|计划|帮我们|顺便|之后)', cand)[0].strip()
            cand = re.sub(r'(完饭|吃饭|用餐)$', '', cand).strip()
            if not is_place_word(cand):
                continue
            # "吃行玩你来安排""吃饭你定"：说的是**授权给我**，不是店名。
            # 不挡的话会被写成"吃饭（行玩你来安排）"，等于把用户的话当餐厅。
            if cand and re.search(r'(你来|你定|你看着|随便|都行|看你的|随你|安排)', cand):
                continue
            # 「去吃个饭」「吃顿饭」：量词开头的短语不是店名
            if cand and re.match(r'^[个份顿碗杯串只双]', cand):
                continue
            if not cand or cand in self._MEAL_STOP:
                continue
            if any(cand.startswith(w) for w in self._MEAL_STOP):
                continue
            # "去南邮找朋友吃饭"里的地点是去找人，不是这顿饭的店
            if any(w in cand for w in ('找', '朋友', '同学', '碰头', '集合')):
                continue
            if len(cand) > 16:
                continue
            try:
                poi = self.svc._poi_named(cand)
            except Exception:              # noqa: BLE001
                poi = None
            if poi is None:
                poi = self._resolve_place(cand)
            if poi is None:
                return cand
            branch = self._branch_of(cand)
            if branch and branch not in poi.name:
                return cand        # 说的是某个分店，就别拿另一家分店顶替
            return poi.name
        return ''

    def _resolve_place(self, cand: str):
        """地名解析：精确/别名 → 规范化精确 → 规范化前缀 → 包含匹配（按规范化名字）"""
        cand = (cand or '').strip()
        if not cand:
            return None
        if cand in ('学校', '校区', '母校', '公司', '单位', '家', '宿舍', '住的地方'):
            return None
        try:
            poi = self.svc._poi_named(cand)
        except Exception:                      # noqa: BLE001
            poi = None
        if poi is not None:
            return poi
        pack = get_city(self.svc.city)
        key = self._norm(cand)
        if not key:
            return None
        rows = pack.poi_table()
        # 规范化后完全相等（三山街地铁站 → 三山街(地铁站)）
        for row in rows:
            if self._norm(row.get('name')) == key:
                return Poi.from_pack(row, self.svc.city)
        # 简称替换后再试一次
        probe = key
        for short, full in self._NICK.items():
            if short in probe:
                probe = probe.replace(short, full)
                break
        for row in rows:
            nm = self._norm(row.get('name'))
            if nm == probe:
                return Poi.from_pack(row, self.svc.city)
        # 前缀/包含（要求够长，避免"仙林中心"落到"仙林湖"）
        cands = [row for row in rows
                 if nm_key_ok(nm := self._norm(row.get('name')), probe)]
        if cands:
            cands.sort(key=lambda r: len(self._norm(r.get('name'))))
            return Poi.from_pack(cands[0], self.svc.city)
        return None

    # ---------- 务实行程（交通 + 住宿 + 办事 + 返程） ----------
    def _near_hotel(self, place_name: str):
        """按"离某地最近"挑酒店，而不是全市评分最高那家"""
        pack = get_city(self.svc.city)
        anchor = None
        try:
            anchor = self.svc._poi_named(place_name) if place_name else None
        except Exception:                      # noqa: BLE001
            anchor = None
        rows = [r for r in pack.poi_table() if str(r.get('kind') or '') == '住宿']
        hotels = [Poi.from_pack(r, self.svc.city) for r in rows]
        if anchor is None or not hotels:
            return hotels[0] if hotels else None
        hotels.sort(key=lambda h: _km(h, anchor))
        near = [h for h in hotels if _km(h, anchor) <= 4.0]
        pick = (near or hotels)[0]
        d_km = _km(pick, anchor)
        pick._near_note = (f'离{anchor.name}约 {d_km:.1f} 公里' if d_km <= 4.0
                           else f'附近 4 公里内没有酒店，最近的是它（约 {d_km:.1f} 公里）')
        return pick

    def _build_practical(self, st) -> dict:
        """到站→回学校→住附近→第二天办事→返程，两天闭环，不掺观光点"""
        start = date.fromisoformat(st.start) if st.start else date.today() + timedelta(days=1)
        span = st.date_span or 2
        anchor = st.anchor_place or ''
        hotel = None
        if st.hotel_near or st.hotel == 'recommend' or st.luggage:
            hotel = self._near_hotel(anchor)
            if hotel is None:
                opts = self.hotel_options(st, limit=1)
                if opts:
                    hotel = self.svc._poi_named(opts[0]['name'])
        days = []
        for i in range(span):
            day = start + timedelta(days=i)
            items = []
            if i == 0:
                if st.arrival:
                    items.append({'poi': st.arrival,
                                  'title': (f'{st.arrive_at} 到{st.arrival}' if st.arrive_at
                                            else f'到{st.arrival}'),
                                  'duration_min': 20, 'kind': 'block', 'category': '交通',
                                  'desired_start': st.arrive_at or '', 'locked': bool(st.arrive_at),
                                  'open': '', 'needs_poi': False, 'reason': ''})
                if anchor:
                    items.append({'poi': anchor, 'title': f'回{anchor}',
                                  'duration_min': 40, 'kind': 'task', 'category': '办事',
                                  'open': '', 'needs_poi': False, 'reason': ''})
                if hotel is not None:
                    # 入住受酒店 checkin 限制：到得太早先寄存行李，别写成"已入住"
                    ck = str(getattr(hotel, 'checkin', '') or '')
                    if ck and st.arrive_at and st.arrive_at < ck:
                        items.append({'poi': hotel.name,
                                      'title': f'到{hotel.name}寄存行李（{ck} 后才能入住）',
                                      'duration_min': 20, 'kind': 'task', 'category': '办事',
                                      'open': '', 'needs_poi': False,
                                      'reason': f'酒店 {ck} 起才能办理入住，先存包'})
                    items.append({'poi': hotel.name, 'title': f'入住{hotel.name}',
                                  'duration_min': 30, 'kind': 'task', 'category': '办事',
                                  'open': '', 'needs_poi': False,
                                  'desired_start': ck or '',
                                  'reason': (getattr(hotel, '_near_note', '') or '按离学校最近挑的，可改')
                                            + (f'；{ck} 起可入住' if ck else '')})
            else:
                nd = st.next_day or {}
                # 退房：先退房再办事（酒店 checkout 一般在 12:00 前）
                if hotel is not None:
                    ck = str(getattr(hotel, 'checkout', '') or '')
                    items.append({'poi': hotel.name,
                                  'title': f'退房{"（" + ck + " 前）" if ck else ""}',
                                  'duration_min': 20, 'kind': 'task', 'category': '办事',
                                  'open': '', 'needs_poi': False,
                                  'reason': '有行李就先退房寄存，办完事直接去车站'})
                if nd.get('task'):
                    items.append({'poi': nd.get('at') or anchor, 'title': nd['task'],
                                  'duration_min': 120, 'kind': 'task', 'category': '办事',
                                  'open': '', 'needs_poi': False,
                                  'reason': '时间未知，按 2 小时预留；建议提前 15 分钟到场'})
                if nd.get('then_to'):
                    items.append({'poi': nd['then_to'], 'title': f'前往{nd["then_to"]}',
                                  'duration_min': 60, 'kind': 'task', 'category': '交通',
                                  'open': '', 'needs_poi': False, 'reason': ''})
                if nd.get('then_out'):
                    hint = nd.get('depart_hint') or '时间待定'
                    items.append({'poi': '', 'title': f'乘高铁去{nd["then_out"]}（{hint}）',
                                  'duration_min': 30, 'kind': 'block', 'category': '交通',
                                  'open': '', 'needs_poi': False,
                                  'reason': f'车次未知，按{hint}预留，定了告诉我改'})
            days.append({'date': day.isoformat(),
                         'weekday': '一二三四五六日'[day.weekday()],
                         'theme': '到达与安置' if i == 0 else '答辩与返程',
                         'why': ('从到站到落脚：先回学校、再安顿住处'
                                 if i == 0 else '办完事直奔车站，行程终点是南京南，不回酒店'),
                         'items': items, 'sights': 0,
                         'stops': [it['poi'] for it in items if it['poi']]})
        st.days = days
        notes_extra = []
        if getattr(st, 'friend_weekend', False) and st.start:
            wk = []
            d0 = date.fromisoformat(st.start)
            for k in range(span):
                dd = d0 + timedelta(days=k)
                if dd.weekday() >= 5:
                    wk.append(f'{dd.isoformat()}（周{"六日"[dd.weekday() - 5]}）')
            if wk:
                notes_extra.append('朋友只在周末有空 → 建议约：' + '、'.join(wk))
            else:
                notes_extra.append('这个窗口里没有周末，建议把日期挪到含周末的那几天再约朋友')
        return {'ok': True, 'start': st.start, 'end': st.end, 'days_total': span,
                'notes_extra': notes_extra,
                'pace': st.pace or 'normal', 'wake': st.wake or '', 'party': st.party or 1,
                'interests': st.interests, 'hotel': st.hotel or 'recommend',
                'per_day': 0, 'days': days, 'must': list(st.must),
                'arrival': st.arrival, 'luggage': bool(st.luggage),
                'hotel_pick': (hotel.name if hotel is not None else ''),
                'practical': True,
                'message': f'{span} 天事务行程已就绪（到达/住宿/办事/返程，不含观光点）'}

    def _span_days(self, st: 'ConsultSession') -> list:
        """草案覆盖的日期列表（用于查当天已有安排 / 执行删除）"""
        if not st.start:
            return []
        try:
            day0 = date.fromisoformat(st.start)
        except ValueError:
            return []
        span = max(1, min(15, getattr(st, 'trip_days', 0) or (st.date_span or 1)))
        return [day0 + timedelta(days=i) for i in range(span)]

    def _scheduled_locations(self, st: 'ConsultSession') -> dict:
        """读出这些天**已经排好的**地点：顾问据此不重复推荐，也让配额算上它们"""
        out: dict = {}
        for day in self._span_days(st):
            try:
                trip = self.svc.day_view(day)
            except Exception:                 # noqa: BLE001 查不到就当没有
                continue
            items = [{'poi': (t.poi.name if t.poi is not None else ''),
                      'title': t.title,
                      'start': t.start_ts.strftime('%H:%M') if t.start_ts else ''}
                     for t in trip.tasks]
            if items:
                out[day.isoformat()] = items
        return out

    def _apply_drops(self, st: 'ConsultSession') -> list:
        """执行"把 X 去掉"：真的从当天行程里删掉，只做一次"""
        if not st.drop:
            return []
        try:
            removed = self.svc.drop_places(self._span_days(st), list(st.drop))
        except Exception as exc:              # noqa: BLE001 删除失败不影响继续排
            log.warning('删除已排地点失败：%s', exc)
            removed = []
        st.drop = []                          # 清空，避免下一轮又删一遍
        return removed

    def _theme_of(self, chosen: list[Poi], st: ConsultSession) -> str:
        tally: dict[str, int] = {}
        for p in chosen:
            name = f'{p.name}{p.kind}'
            for it, keys in INTERESTS.items():
                if any(k in name for k in keys):
                    tally[it] = tally.get(it, 0) + 1
        if not tally:
            return '自由组合'
        best = max(tally, key=lambda k: tally[k])
        return _THEME_NAME.get(best, '自由组合')

    def _why(self, chosen: list[Poi], st: ConsultSession) -> str:
        if not chosen:
            return '这天留白（可加你点名的点）'
        parts = []
        if len(chosen) > 1:
            far = max((_km(a, b) for i, a in enumerate(chosen) for b in chosen[i + 1:]),
                      default=0)
            parts.append(f'{len(chosen)} 个点彼此相距最远 {far:.1f} 公里，串成一条线')
        if st.interests:
            parts.append('优先你选的：' + '、'.join(st.interests))
        if st.must:
            parts.append('含点名必去：' + '、'.join(st.must))
        if st.pace == 'slow':
            parts.append(f'按懒散节奏，{st.wake or "10:00"} 起排、不赶早场')
        return '；'.join(parts)

    # ---------- 一轮对话 ----------
    #: 问句特征：出现这些词说明用户是在问，而不是在下指令
    _ASK_MARKS = ('为什么', '什么时候', '几点', '多久', '多远', '多少钱', '多少', '门票', '开门',
                  '闭馆', '天气', '下雨', '合理', '来得及', '会不会', '是不是',
                  '怎么样', '如何', '请问')
    #: 明确的下指令动词：出现这些词就仍然按"要改/要排"处理
    #: 只有"要动东西"的动词才算指令；"安排/规划/推荐"是请求方案，不是修改
    _IMPERATIVE = ('改成', '换掉', '换成', '删掉', '去掉', '取消', '加个', '多加',
                   '少一天', '加一天', '你帮我选', '写进去')
    #: 含糊指代
    _VAGUE_REF = ('那边', '那个地方', '老地方', '那家', '这个地方', '就那个')
    #: 纯应答
    _ACK = ('嗯', '嗯嗯', '好的', '好', '可以', '行', '随便', '你看着办', '听你的',
            '都行', 'ok', 'OK', '没问题', '知道了')

    def _timing_advice(self, st: 'ConsultSession', text: str) -> str:
        """问"推荐什么时候出发/到达"：按时长与通行倒推出发与到达时刻"""
        places = [nm for nm in (st.must or []) if nm]
        if not places:
            return '先告诉我大概去哪几个地方，我才能算出发与到达时刻'
        origin_name = st.anchor_place or getattr(st, 'origin_text', '') or ''
        try:
            origin_poi = self.svc._poi_named(origin_name) if origin_name else None
        except Exception:                     # noqa: BLE001
            origin_poi = None
        cursor, prev, legs = 9 * 60, origin_poi, []
        for name in places[:4]:
            try:
                poi = self.svc._poi_named(name)
            except Exception:                 # noqa: BLE001
                poi = None
            travel = 0
            if prev is not None and poi is not None:
                try:
                    travel = self.svc.transit.estimate(prev, poi, None) or 0
                except Exception:             # noqa: BLE001
                    travel = 0
            arrive = cursor + travel
            stay = int(getattr(poi, 'visit_min', 0) or 90) if poi is not None else 90
            hh, mm = divmod(arrive, 60)
            lh, lm = divmod(arrive + stay, 60)
            legs.append(f'{name}：{hh:02d}:{mm:02d} 到、{lh:02d}:{lm:02d} 走'
                        + (f'（路上约 {travel} 分钟）' if travel else ''))
            cursor, prev = arrive + stay + 10, poi if poi is not None else prev
        head = f'从 {origin_name} 出发' if origin_name else '出发'
        return head + '，建议：' + '；'.join(legs)

    #: 城市包 price 字段里的数字：「人均约 ¥90」「约 ¥800 起/晚」「¥70」
    _PRICE_RE = re.compile(r'¥\s*(\d+)|(\d+)\s*元')

    def _price_of(self, poi) -> Optional[int]:
        """从城市包取价（餐饮=人均、住宿=起价）。取不到返回 None——**不许编**"""
        text = str(getattr(poi, 'price', '') or '')
        m = self._PRICE_RE.search(text)
        if not m:
            return None
        try:
            return int(m.group(1) or m.group(2))
        except (TypeError, ValueError):
            return None

    def _day_cost(self, items: list[dict], plan: dict) -> dict:
        """当日费用：餐饮人均 + 门票 + 交通，**逐项标来源**，取不到的标"待核实"

        数据纪律：门票只有少数点位在城市包里有价，没有就写"待核实"，
        交通按"公交/地铁 2 元/次"估算并标明是估算，绝不把估的数写成实价。
        """
        food = 0
        food_unknown = 0
        ticket = 0
        ticket_unknown = 0
        stops = 0
        for it in items:
            name = str(it.get('poi') or '')
            cat = str(it.get('category') or '')
            if cat == '餐饮':
                poi = None
                if name:
                    try:
                        poi = self.svc._poi_named(name)
                    except Exception:         # noqa: BLE001
                        poi = None
                price = self._price_of(poi) if poi is not None else None
                if price:
                    food += price
                else:
                    food_unknown += 1
                continue
            if cat == '交通' or not name:
                continue
            stops += 1
            poi = None
            try:
                poi = self.svc._poi_named(name)
            except Exception:                 # noqa: BLE001
                poi = None
            price = self._price_of(poi) if poi is not None else None
            if price:
                ticket += price
            else:
                ticket_unknown += 1
        # 站与站之间算一段车费（同点不计）
        legs = max(0, stops - 1)
        fare = legs * 2
        total = food + ticket + fare
        return {'food': food, 'food_unknown': food_unknown,
                'ticket': ticket, 'ticket_unknown': ticket_unknown,
                'legs': legs, 'fare': fare, 'total': total}

    @staticmethod
    def _cost_line(cost: dict) -> str:
        """把费用渲染成一行（每项都带来源，缺的写"待核实"）"""
        parts = []
        if cost['food']:
            parts.append(f"餐饮 ¥{cost['food']}（城市包人均参考价）")
        elif cost['food_unknown']:
            parts.append('餐饮 待核实')
        if cost['ticket']:
            parts.append(f"门票 ¥{cost['ticket']}（城市包收录价）")
        if cost['ticket_unknown']:
            parts.append(f"门票待核实 {cost['ticket_unknown']} 处")
        if cost['fare']:
            parts.append(f"交通 ¥{cost['fare']}（公交/地铁 2 元/次，估算）")
        if not parts:
            return '  当日费用：这一天没有可计价的项'
        return ('  当日费用：' + ' + '.join(parts)
                + f" ≈ ¥{cost['total']} 起（未含待核实项）")

    def _timeline_text(self, plan: dict) -> str:
        """把草案渲染成时间轴：抵达 / 离开 / 停留 / 交通 / 备注，并给汇总与总结。

        说明：这是**草案**时刻，由"起点 + 停留 + 10 分钟级缓冲"累加得到；
        写入后会按真实通行时长与营业时间重算（界面时间轴为准）。
        """
        days = plan.get('days') or []
        if not days:
            return ''
        wake = str(plan.get('wake') or '09:00')
        try:
            hh, mm = wake.split(':')
            cursor = int(hh) * 60 + int(mm)
        except (ValueError, AttributeError):
            cursor = 9 * 60
        fixed_names = {m for m in (plan.get('must') or []) if m}
        grand = {'food': 0, 'food_unknown': 0, 'ticket': 0, 'ticket_unknown': 0,
                 'fare': 0, 'total': 0}
        out = ['', '**时间轴（草案，写入后按真实通行重算）**']
        for day in days[:5]:
            items = day.get('items') or []
            if not items:
                out.append(f'{day.get("date")} 周{day.get("weekday")}：留白')
                continue
            out.append(f'{day.get("date")} 周{day.get("weekday")}')
            day_start = cursor
            prev_poi = None
            budget = 6                      # 整个草案最多估 6 段真实通行
            # 先算每段的真实通行时长：它决定下一站**几点到**，而不只是显示一行字
            _pois = []
            for _it in items[:8]:
                try:
                    _pois.append(self.svc._poi_named(str(_it.get('poi') or ''))
                                 if _it.get('poi') else None)
                except Exception:             # noqa: BLE001
                    _pois.append(None)
            _legs = [None] * len(_pois)
            for _k in range(1, len(_pois)):
                if _pois[_k - 1] is None or _pois[_k] is None:
                    continue
                try:
                    from .models import TransitMode
                    try:
                        _m = self.svc._mode_pick()
                    except Exception:         # noqa: BLE001
                        _m = None
                    if _m is None:
                        _m = TransitMode(
                            getattr(self.svc.settings, 'transit_mode', 'driving'))
                    _legs[_k] = self.svc.transit.estimate(_pois[_k - 1], _pois[_k], _m)
                except Exception as exc:      # noqa: BLE001 取不到就按缓冲算
                    log.debug('通行时长取不到：%s', exc)
            for idx, it in enumerate(items[:8]):
                stay = int(it.get('duration_min') or 60)
                name = it.get('poi') or it.get('title') or '（待补地点）'
                tag = '【固定】' if (it.get('poi') in fixed_names
                                     or '吃' in str(it.get('title') or '')) else '【可选】'
                # 到下一站先要花掉通行时间；取不到通行时长时退回 10 分钟缓冲
                gap = int(_legs[idx]) if (idx < len(_legs) and _legs[idx]) else 10
                arrive = cursor + (gap if idx else 0)
                leave = arrive + stay
                ah, am = divmod(arrive, 60)
                lh, lm = divmod(leave, 60)
                note = it.get('open') or ''
                if it.get('reason'):
                    note = (note + '；' if note else '') + str(it['reason'])[:24]
                # 通行段：能查到就写"方式+耗时"，查不到（或超额）留给写入时重算
                leg_text = ''
                if prev_poi is not None and it.get('poi') and budget > 0:
                    try:
                        poi_now = self.svc._poi_named(str(it['poi']))
                        if poi_now is not None and poi_now is not prev_poi:
                            # 第三个参数是 TransitMode 枚举（内部要用 .value），
                            # 以前传 None → AttributeError 被 except 吞掉 →
                            # 时间轴上一段交通都不显示（等于把主需求里"通行时长"整段丢了）
                            try:
                                _mode = self.svc._mode_pick()
                            except Exception:         # noqa: BLE001
                                _mode = None
                            if _mode is None:         # 离线时 _mode_pick 返回 None，退回全局默认
                                from .models import TransitMode
                                try:
                                    _mode = TransitMode(
                                        getattr(self.svc.settings, 'transit_mode', 'driving'))
                                except Exception:     # noqa: BLE001
                                    _mode = TransitMode.driving
                            mins = self.svc.transit.estimate(prev_poi, poi_now, _mode)
                            if mins:
                                mode_cn = {'driving': '驾车', 'transit': '公交/地铁',
                                           'walking': '步行', 'bicycling': '骑行'}.get(
                                    str(getattr(self.svc.settings, 'transit_mode', 'driving')), '通行')
                                leg_text = f'  ↓ {mode_cn}约 {int(mins)} 分钟'
                                budget -= 1
                    except Exception:         # noqa: BLE001 取不到就不写
                        leg_text = ''
                if leg_text:
                    out.append(leg_text)
                out.append(f'  {ah:02d}:{am:02d}–{lh:02d}:{lm:02d}｜{name}｜停留{stay}分｜'
                           f'{it.get("category") or "游览"} {tag}'
                           + (f'｜{note}' if note else ''))
                cursor = leave               # 下一段的通行时长在下一轮加
                if it.get('poi'):
                    try:
                        prev_poi = self.svc._poi_named(str(it['poi']))
                    except Exception:         # noqa: BLE001
                        prev_poi = None
            eh, em = divmod(cursor, 60)
            bh, bm = divmod(day_start, 60)
            out.append(f'  合计 {bh:02d}:{bm:02d}–{eh:02d}:{em:02d}（约 {(cursor - day_start) // 60} 小时 '
                       f'{len(items)} 项；段间已含真实通行时长，取不到时按 10 分钟缓冲）')
            day_cost = self._day_cost(items, plan)
            out.append(self._cost_line(day_cost))
            grand['food'] += day_cost['food']
            grand['food_unknown'] += day_cost['food_unknown']
            grand['ticket'] += day_cost['ticket']
            grand['ticket_unknown'] += day_cost['ticket_unknown']
            grand['fare'] += day_cost['fare']
            grand['total'] += day_cost['total']
            cursor = day_start
        out.append('')
        out.append(self._cost_line(grand).replace('当日费用：', '全程费用：'))
        out.append('（餐饮取城市包人均价、门票取城市包收录价、交通按公交/地铁 2 元/次估算；'
                   '标"待核实"的项城市包没有收录，出门前以现场价为准）')
        tips = []
        if plan.get('meal') in (None, '', 'recommend'):
            tips.append('午饭按"就近吃"留了时间块，指定店名我就换成具体餐厅')
        if (plan.get('hotel') or '') == '':
            # 追问里已经会问住哪儿，这里不再重复一遍
            if not any('住哪' in str(q.get('ask') or '') for q in (plan.get('questions') or [])):
                tips.append('住宿还没定：告诉我酒店名（或说"你帮我选"），我从酒店算出发与回程')
        tips.append('任一站超时 30 分钟以上，说一句"这天太赶了"，我按"点名必去优先"帮你删点并给备选')
        out.append('')
        out.append('**注意事项**：' + '；'.join(tips[:3]))
        return '\n'.join(out)

    def _answer_question(self, st: 'ConsultSession', text: str, today: date) -> str:
        """回答关于行程的问题：只读，不改任何槽位"""
        day = None
        for cand in (st.start, today.isoformat()):
            try:
                day = date.fromisoformat(cand) if cand else None
            except ValueError:
                day = None
            if day is not None:
                break
        if day is None:
            return '我还不知道是哪一天，先说个日期我就能照着答'
        try:
            trip = self.svc.day_view(day)
        except Exception:                     # noqa: BLE001
            trip = None
        tasks = [t for t in getattr(trip, 'tasks', []) if t.start_ts]
        if '天气' in text or '下雨' in text:
            got = None
            try:
                got = self.svc.day_weather(day)
            except Exception:                 # noqa: BLE001
                got = None
            if not got:
                return f'{day.isoformat()} 的预报我拿不到（只有今天+3 天有真实预报）'
            return (f'{day.isoformat()}：{got.get("text") or "有预报"}，'
                    f'{got.get("temp") or ""}；{"建议改室内" if got.get("wet") else "可以按原计划"}')
        if not tasks:
            return f'{day.isoformat()} 还没有安排：给我一句话（去哪、几点、多久）我就能排'
        head = '、'.join(f'{(t.poi.name if t.poi else t.title)} '
                        f'{t.start_ts.strftime("%H:%M")}-{t.end_ts.strftime("%H:%M")}'
                        for t in tasks[:6])
        tail = f'共 {len(tasks)} 项'
        if '门票' in text or '多少钱' in text:
            prices = [f'{(t.poi.name if t.poi else t.title)}：{getattr(t.poi, "book_hint", "") or "门票见现场"}'
                      for t in tasks[:4] if t.poi is not None]
            if prices:
                return '；'.join(prices)
        if '为什么' in text:
            return f'{day.isoformat()} 这么排的依据：{head}；{tail}（按锁定优先+空档扫描+真实通行时长算的）'
        return f'{day.isoformat()} 现在是：{head}；{tail}'

    def chat(self, session: str, text: str, today: date) -> dict:
        raw = (text or '').strip()
        set_progress(session, STAGE_PICK)
        st0 = self.load(session)
        # ---- 意图闸：先判断"是问 / 是含糊 / 是纯应答 / 是要办事" ----
        # 不加这一层时，只要槽位里有日期就会去 build_plan，问天气、问门票都被换成一版新行程
        has_imperative = any(w in raw for w in self._IMPERATIVE)
        # 一句里既问时刻又要方案（"帮我规划…推荐什么时候出发"）→ 按"要方案"走，
        # 不能因为含"什么时候"就整句当问句，那样会把规划请求吃掉
        wants_plan_too = any(w in raw for w in ('规划', '安排', '帮我排', '推荐几个',
                                                '推荐一下', '去玩', '玩几天', '行程'))
        asks = any(w in raw for w in self._ASK_MARKS) and not wants_plan_too
        if raw and asks and not has_imperative:
            timing_words = ('出发', '到达', '几点', '什么时候', '多久')
            msg = (self._timing_advice(st0, raw) if any(w in raw for w in timing_words)
                   else self._answer_question(st0, raw, today))
            return {'kind': 'answer', 'questions': [], 'message': msg,
                    'slots': st0.as_dict()}
        if raw in self._ACK or (len(raw) <= 3 and raw.strip('。.!！') in self._ACK):
            has_plan = bool(st0.days)
            tip = ('草案已经准备好了：点「查看并写入」就能落库；想改就说「换一版 / 多加一天 / 别去 X」'
                   if has_plan else '那我先按你说的排一版：把日期和大概想去的地方告诉我，或者直接说「你帮我安排」')
            return {'kind': 'clarify', 'questions': [],
                    'message': tip, 'slots': st0.as_dict()}
        if raw and any(w in raw for w in self._VAGUE_REF) and not has_imperative:
            return {'kind': 'clarify', 'questions': [],
                    'message': '你说的"那边/老地方"我这边对不上具体地点：给我个地名，或说「就上次那家馆子」也行',
                    'slots': st0.as_dict()}
        if len(raw) > MAX_TEXT_LEN:
            return {'kind': 'notice', 'questions': [],
                    'message': f'这段话 {len(raw)} 字，太长了：请拆成两三次说'
                               f'（一次不超过 {MAX_TEXT_LEN} 字），我每次都能接着前面改',
                    'slots': self.load(session).as_dict()}
        st = self.load(session)
        st = self.absorb(st, text, today)
        self.save(session, st)

        # 用户只是问"有什么建议"且参数还缺 → 先给默认版 + 提问
        qs = self.questions(st)
        if not st.start:
            if 'dates' in st.asked:
                # 已经问过一次还说不清 → 不再重复追问，先按明天给一版，说清楚可改
                st.start = st.end = (today + timedelta(days=1)).isoformat()
                st.note = '你没有明确日期，我先按明天排；说一句"改成9月9号"我就改'
                self.save(session, st)
                plan = self.build_plan(st)
                return {'kind': 'plan', 'message': st.note + '；' + plan['message'],
                        'plan': plan, 'questions': [], 'slots': st.as_dict()}
            st.asked.append('dates')
            self.save(session, st)
            head = '先问一句：' if qs else ''
            ask = head + (qs[0]['ask'] if qs else '')
            if st.note:                 # 日期看不懂的原因要说出来，不要泛泛追问
                ask = st.note + '；' + ask
            return {'kind': 'ask', 'message': ask, 'questions': qs,
                    'slots': st.as_dict()}

        dropped = self._apply_drops(st)
        set_progress(session, STAGE_FETCH)
        plan = self.build_plan(st)
        set_progress(session, STAGE_RENDER)
        self.save(session, st)
        msg = plan['message']
        timeline = self._timeline_text(plan)
        if timeline:
            msg += timeline
        # 说明里重复出现同一句（多轮累积导致）会让回复像复读机，去重后再拼
        if st.note:
            parts, seen_note = [], set()
            for part in str(st.note).split('；'):
                part = part.strip()
                if part and part not in seen_note:
                    seen_note.add(part)
                    parts.append(part)
            st.note = '；'.join(parts)
        if st.note:                     # 过去的日期、跨度被截断等提示要随草案一起出现
            msg = st.note + '；' + msg
        if dropped:
            msg += '；已按你说的从行程里删掉：' + '、'.join(dropped)
        if qs:
            msg += '；另外' + str(qs[0].get('ask') or '')
            st.asked.extend(q.get('key', '') for q in qs)
        return {'kind': 'plan', 'message': msg, 'plan': plan,
                'questions': qs, 'slots': st.as_dict()}


class ConsultStore:
    """会话持久化（SQLite，一张小表）"""

    TABLE = """
    CREATE TABLE IF NOT EXISTS consult_session (
        session TEXT PRIMARY KEY,
        payload TEXT NOT NULL,
        updated_at TEXT DEFAULT ''
    )"""

    def __init__(self, db):
        self.db = db
        with self.db.transaction() as conn:
            conn.execute(self.TABLE)

    def get(self, session: str) -> dict:
        row = self.db.conn.execute(
            'SELECT payload FROM consult_session WHERE session = ?',
            (session,)).fetchone()
        if not row:
            return {}
        try:
            return json.loads(row['payload'])
        except (ValueError, TypeError):
            return {}

    def delete(self, session: str) -> None:
        """删掉一个会话（界面上的"重新开始"用）"""
        with self.db.transaction() as conn:
            conn.execute('DELETE FROM consult_session WHERE session = ?', (session,))

    def put(self, session: str, data: dict) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                'INSERT INTO consult_session (session, payload, updated_at)'
                ' VALUES (?,?,?) ON CONFLICT(session) DO UPDATE SET'
                ' payload=excluded.payload, updated_at=excluded.updated_at',
                (session, json.dumps(data, ensure_ascii=False),
                 datetime.now().isoformat(timespec='seconds')))


# chat() 套一层进度包装（结束时必定清空，前端不会停在 90% 转圈）
Consultant.chat = _track_progress(Consultant.chat)
