"""多日行程提案（"我要 9/10–9/16 在南京玩，比较懒，起得晚，两个人，酒店未知"）

为什么要单开一路
----------------
原来的入口是"录入"：**用户给地点，系统排时间**。而这类请求是"行程顾问"：
用户只给**日期区间 + 偏好**，地点、酒店、每天怎么分，全要系统拿主意。
把这种话喂给 `preview_batch` 的结果是 0 条（整句被当成交代表述丢掉），
助手也只能回一句问答——因为系统里根本没有"多日 + 推荐"这条路径。

本模块负责把"区间 + 偏好"变成**逐日草案**，全程只用两种数据：
* 城市包里的 POI（名称/类型/坐标/推荐游玩时长/营业时间/价位）；
* 偏好解析结果（起床时间、每天强度、同行人数、预算档）。
不写库、不臆造：所有地点都来自城市包，时长来自 `intel` 的推荐时长（取不到才用包内种子）。
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional

from .cities import CityPack, dedupe_brands, get_city, is_recommendable

from .geo import poi_distance_km as _km
from .models import Poi

#: 一天里"玩"的类型（推荐候选）
_SIGHT_KINDS = ('景点', '场馆', '公园', '街区', '书店')
_FOOD_KINDS = ('餐饮',)
_SKIP_KINDS = ('交通', '医疗', '住宿', '校园', '办事')

#: 每天安排几个景点（懒人 vs 标准 vs 紧凑）
_PACING = {
    'slow': 2,
    'normal': 3,
    'fast': 4,
}



_CN_DIGIT = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
             '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}


def cn_num(text: str) -> Optional[int]:
    """中文数字 → int（支持 二十、二十三、三十一、十五）"""
    s = (text or '').strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if '十' in s:
        head, _, tail = s.partition('十')
        tens = _CN_DIGIT.get(head, 1) if head else 1
        ones = _CN_DIGIT.get(tail, 0) if tail else 0
        return tens * 10 + ones
    if len(s) == 1 and s in _CN_DIGIT:
        return _CN_DIGIT[s]
    return None


_REL_DAYS = {'今天': 0, '今日': 0, '明天': 1, '明日': 1, '后天': 2,
             '大后天': 3, '今天白天': 0}


def _cn_or_int(token: str) -> int:
    """「3」「三」「两」都转成整数（日期解析里到处要用）"""
    token = str(token or '').strip()
    if token.isdigit():
        return int(token)
    cn = {'一': 1, '两': 2, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7,
          '八': 8, '九': 9, '十': 10}
    if token in cn:
        return cn[token]
    if len(token) == 2 and token[0] == '十' and token[1] in cn:
        return 10 + cn[token[1]]
    if len(token) == 2 and token[1] == '十' and token[0] in cn:
        return cn[token[0]] * 10
    return 0


def parse_single_date(text: str, today: date) -> Optional[date]:
    """单日表述：「9月9号」「9.9」「9/9」「明天」「后天」「周六」"""
    s = (text or '').strip()
    if not s:
        return None
    for word, delta in _REL_DAYS.items():
        if word in s:
            return today + timedelta(days=delta)
    # 「3 天后 / 三天以后 / 一周后」：相对天数（原来只认"今天/明天/后天/大后天"）
    m_rel = re.search(r'(\d{1,2}|[一二两三四五六七八九十]{1,3})\s*天\s*(?:之?后|以后)', s)
    if m_rel:
        return today + timedelta(days=_cn_or_int(m_rel.group(1)))
    m_week = re.search(r'(\d{1,2}|[一二三四五六七八九十]{1,2})\s*(?:个)?\s*(?:周|星期)\s*'
                       r'(?:之?后|以后)', s)
    if m_week:
        return today + timedelta(days=7 * _cn_or_int(m_week.group(1)))
    # ISO 与带年份写法（导入、外部引用、跨年规划常用）
    m_iso = re.search(r'(?<!\d)(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})(?!\d)', s)
    if m_iso:
        try:
            return date(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))
        except ValueError:
            return None
    m_cn = re.search(r'([一二三四五六七八九十]{1,3})\s*月\s*([一二三四五六七八九十]{1,3})\s*[日号]?', s)
    if m_cn:
        mth, dd = cn_num(m_cn.group(1)), cn_num(m_cn.group(2))
        if mth and dd:
            try:
                return date(today.year, mth, dd)
            except ValueError:
                pass
    m = re.search(r'(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?', s)
    if not m:
        m = re.search(r'(?<!\d)(\d{1,2})\s*[./]\s*(\d{1,2})(?!\d)', s)
    if m:
        try:
            return date(today.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    m = re.search(r'(?:下?周|星期)([一二三四五六日天])', s)
    if m:
        idx = '一二三四五六日'.find(m.group(1))
        if idx >= 0:
            ahead = (idx - today.weekday()) % 7 or 7
            return today + timedelta(days=ahead)
    return None


def parse_date_range(text: str, today: date) -> Optional[tuple[date, date]]:
    """从「9月10号-9月16号」这类表述里取出日期区间（含跨年处理）

    只有一天时也返回 (d, d)：用户说「9月9号」就是那一天，不该被反复追问区间。
    """
    s = text or ''
    # 四种写法都认：「9月10号-9月16号」「9.20-9.23」「9/20/9/23」「9月20-23」
    m = re.search(
        r'(?<![\d.])(\d{1,2})\s*[.．]\s*(\d{1,2})\s*(?:-|—|~|～|至|到)\s*'
        r'(\d{1,2})\s*[.．]\s*(\d{1,2})(?![\d])', s)
    if m:
        m = type('m', (), {'group': staticmethod(
            lambda i, _g=(None, *m.groups()): _g[i])})()
    else:
        m = re.search(
            r'(?<![\d/])(\d{1,2})\s*/\s*(\d{1,2})\s*(?:-|—|~|～|至|到)\s*'
            r'(\d{1,2})\s*/\s*(\d{1,2})(?![\d])', s)
        if m:
            m = type('m', (), {'group': staticmethod(
                lambda i, _g=(None, *m.groups()): _g[i])})()
        else:
            m = re.search(
                r'(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?\s*(?:-|—|~|～|至|到)\s*'
                r'(?:(\d{1,2})\s*月\s*)?(\d{1,2})\s*[日号]?', s)
    if not m:
        # 「九月二十号到二十三号」
        m_cn = re.search(
            r'([一二三四五六七八九十]{1,3})\s*月\s*([一二三四五六七八九十]{1,3})\s*[日号]?'
            r'\s*(?:-|—|~|～|至|到)\s*(?:([一二三四五六七八九十]{1,3})\s*月\s*)?'
            r'([一二三四五六七八九十]{1,3})\s*[日号]?', s)
        if m_cn:
            mth = cn_num(m_cn.group(1))
            d1 = cn_num(m_cn.group(2))
            mth2 = cn_num(m_cn.group(3)) if m_cn.group(3) else mth
            d2 = cn_num(m_cn.group(4))
            if mth and d1 and mth2 and d2:
                try:
                    return (date(today.year, mth, d1), date(today.year, mth2, d2))
                except ValueError:
                    pass
        # 「2026-09-20 至 2026-09-23」
        m_full = re.search(r'(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s*(?:-|—|~|～|至|到)\s*'
                           r'(?:(\d{4})[-/.])?(\d{1,2})[-/.](\d{1,2})', s)
        if m_full:
            try:
                return (date(int(m_full.group(1)), int(m_full.group(2)), int(m_full.group(3))),
                        date(int(m_full.group(4) or m_full.group(1)),
                             int(m_full.group(5)), int(m_full.group(6))))
            except ValueError:
                pass
        one = parse_single_date(s, today)
        if one:
            # 「9月20号起连玩4天」「玩四天」→ 扩成区间
            m_days = re.search(r'(?:连玩|连住|玩|待|玩上|安排)\s*(\d{1,2}|[一二三四五六七八九十]{1,3})\s*天', s)
            if m_days:
                n = cn_num(m_days.group(1))
                if n and 1 <= n <= 30:
                    return (one, one + timedelta(days=n - 1))
            return (one, one)
        return None
    m1, d1 = int(m.group(1)), int(m.group(2))
    m2 = int(m.group(3)) if m.group(3) else m1
    d2 = int(m.group(4))
    year = today.year
    try:
        start = date(year, m1, d1)
        end = date(year, m2, d2)
    except ValueError:
        return None
    if end < start:                       # 跨年（12 月 → 1 月）
        try:
            end = date(year + 1, m2, d2)
        except ValueError:
            return None
    if (end - start).days > 30:           # 明显不像旅行区间
        return None
    return start, end


def parse_party(text: str) -> int:
    """同行人数：默认 1；"两个人/2人/一家三口/带爸妈"都能认"""
    s = text or ''
    if any(w in s for w in ('一个人', '独自', '单人', '自己去')):
        return 1
    if any(w in s for w in ('一家三口', '三个人', '三人', '我们仨', '咱们仨', '我俩', '咱俩')):
        return 3 if ('仨' in s or '三' in s) else 2
    if any(w in s for w in ('一家四口', '四个人', '四人')):
        return 4
    if any(w in s for w in ('两个人', '俩人', '两人', '二人', '情侣', '夫妻')):
        return 2
    m = re.search(r'(\d)\s*(?:个)?人', s)
    if m:
        return max(1, min(9, int(m.group(1))))
    if any(w in s for w in ('带爸妈', '带父母', '爸妈')):
        return 3
    if any(w in s for w in ('带孩子', '带娃')):
        return 3
    return 1


def parse_pace(text: str) -> tuple[str, str]:
    """(节奏, 起床时刻)。"比较懒、起床起得晚" → slow + 10:00"""
    s = text or ''
    late = any(w in s for w in ('懒', '起得晚', '起晚', '晚起', '睡到自然醒',
                                '不想早起', '不早起', '自然醒'))
    early = any(w in s for w in ('早起', '早点出发', '一大早就'))
    fast = any(w in s for w in ('特种兵', '赶时间', '尽量多去', '多玩几个'))
    if fast:
        return 'fast', ('08:30' if not late else '10:00')
    if late:
        return 'slow', '10:00'
    if early:
        return 'normal', '08:00'
    return 'normal', '09:00'


def parse_hotel_pref(text: str) -> str:
    """"酒店未知/没订/随便" → 由系统推荐；说了具体酒店名 → 用它"""
    s = text or ''
    if any(w in s for w in ('酒店未知', '没订酒店', '酒店还没', '住哪都行',
                            '随便住', '酒店没定', '没有头绪', '不知道住哪')):
        return 'recommend'
    return ''


class ItineraryPlanner:
    """多日行程提案：把"区间 + 偏好"变成逐日草案（不写库）"""

    def __init__(self, trip_svc):
        self.svc = trip_svc

    # ---------- 候选池 ----------
    def _candidates(self) -> list[Poi]:
        pack: CityPack = get_city(self.svc.city)
        out: list[Poi] = []
        for row in pack.poi_table():
            kind = str(row.get('kind') or '')
            if kind in _SKIP_KINDS:
                continue
            if kind not in _SIGHT_KINDS and kind not in _FOOD_KINDS:
                continue
            if not is_recommendable(row.get('name') or ''):
                continue
            poi = Poi.from_pack(row, self.svc.city)
            self.svc.trip_repo.ensure_poi(poi)
            out.append(poi)               # 情报留到"入选后"再取，别对 60+ 候逐个查
        return out


    def _cluster_days(self, sights: list[Poi], per_day: int) -> list[list[Poi]]:
        """简单地理聚类：挑最远的一个当种子，再贪心补最近的，凑够 per_day

        目的是让同一天的点在地图上连成一条线，而不是"今天城东、下午城西"。
        """
        pool = list(sights)
        days: list[list[Poi]] = []
        while pool:
            seed = max(pool, key=lambda p: _km(p, pool[0]) if len(pool) > 1 else 0)
            pool.remove(seed)
            group = [seed]
            while pool and len(group) < per_day:
                nxt = min(pool, key=lambda p: _km(p, group[-1]))
                pool.remove(nxt)
                group.append(nxt)
            days.append(group)
        return days

    def propose(self, text: str, today: date, default_days: int = 3) -> dict:
        """生成多日提案。返回 {'ok', 'days': [...], 'notes': [...], ...}"""
        rng = parse_date_range(text, today)
        if rng is None:
            start = today + timedelta(days=1)
            days_total = default_days
            rng_note = f'没说要玩几天，先按 {default_days} 天给个草案（你说日期我再改）'
        else:
            start, end = rng
            days_total = (end - start).days + 1
            rng_note = f'{start.isoformat()} 至 {end.isoformat()}，共 {days_total} 天'

        pace, wake = parse_pace(text)
        party = parse_party(text)
        hotel_pref = parse_hotel_pref(text)
        per_day = _PACING.get(pace, 3)

        cands = self._candidates()
        sights = dedupe_brands([p for p in cands if str(p.kind) in _SIGHT_KINDS])
        _DRINK = ('星巴克', '喜茶', '奶茶', '咖啡', '茶饮')
        foods = [p for p in cands if str(p.kind) in _FOOD_KINDS
                 and not any(w in p.name for w in _DRINK)]

        # ① 口碑优先：评分（intel 取到的真实评分）+ 类型权重 + 场馆体量
        #    先挑出"值得去的"，再谈怎么分天；否则会排出燕子矶、江苏大剧院这种冷门组合
        def score(p: Poi) -> float:
            v = 0.0
            intel = getattr(self.svc, 'intel', None)
            row = intel.cached(p.name, self.svc.city) if intel is not None else None
            try:
                v += float((row or {}).get('rating') or 0) or 4.0
            except (TypeError, ValueError):
                v += 4.0
            v += {'景点': 0.3, '场馆': 0.2, '街区': 0.15, '公园': 0.1}.get(str(p.kind), 0)
            v += min(0.5, int(p.visit_min or 0) / 360.0)
            return v

        ranked = sorted(sights, key=score, reverse=True)
        need = days_total * per_day
        picked = ranked[:max(need, per_day)]

        # ② 先按地理聚成"一天一组"（把相邻的点放同一天，别让一天横跨全城），
        #    组内再按就近串成一条线；分组顺序仍按口碑（组内最高分排序）
        groups = self._cluster_days(picked, per_day)
        groups.sort(key=lambda g: max(score(p) for p in g), reverse=True)

        days: list[dict] = []
        used: set[str] = set()
        used_meals: set[str] = set()
        for i in range(days_total):
            day = start + timedelta(days=i)
            wd = '一二三四五六日'[day.weekday()]
            chosen: list[Poi] = []
            # ③ 当天闭馆的点跳过（周一博物馆、周一陵寝…），从后续分组里补
            for g in groups[i:] if i < len(groups) else []:
                for poi in g:
                    if len(chosen) >= per_day:
                        break
                    if poi.name in used or wd in tuple(poi.closed_days or ()):
                        continue
                    chosen.append(poi)
                    used.add(poi.name)
                if len(chosen) >= per_day:
                    break
            if len(chosen) > 1:
                line = [chosen[0]]
                rest = chosen[1:]
                while rest:
                    nxt = min(rest, key=lambda p: _km(p, line[-1]))
                    rest.remove(nxt)
                    line.append(nxt)
                chosen = line
            intel = getattr(self.svc, 'intel', None)
            if intel is not None:
                for po in chosen:
                    try:
                        intel.apply(po)
                    except Exception:             # noqa: BLE001
                        pass
            items = [{
                'poi': poi.name, 'title': f'游览{poi.name}',
                'duration_min': int(poi.visit_min or 90),
                'kind': 'task', 'category': '展览',
                'open': f'{poi.open_from}-{poi.open_to}' if poi.open_from else '',
            } for poi in chosen]
            # ④ 吃饭点选离当天首个景点最近的那家（不再按 i 取模乱配）
            lunch = None
            if foods and chosen:
                pool = [f for f in foods if f.name not in used_meals] or foods
                lunch = min(pool, key=lambda f: _km(f, chosen[0]))
                used_meals.add(lunch.name)
            if lunch is not None:
                items.insert(1 if len(items) > 1 else len(items), {
                    'poi': lunch.name, 'title': '吃饭',
                    'duration_min': int(lunch.visit_min or 60),
                    'kind': 'task', 'category': '餐饮',
                    'open': f'{lunch.open_from}-{lunch.open_to}' if lunch.open_from else '',
                })
            days.append({
                'date': day.isoformat(), 'weekday': wd,
                'items': items, 'sights': len(chosen),
                'stops': [it['poi'] for it in items],
            })

        notes = [
            rng_note,
            f'节奏按“{ "懒散" if pace == "slow" else ("紧凑" if pace == "fast" else "标准") }”'
            f'安排：每天 {per_day} 个点，可排起点 {wake}（晚起就不排早场）',
            f'{party} 人同行：餐饮/门票按 {party} 人算，住宿选能住 {party} 人的房型',
        ]
        if hotel_pref == 'recommend':
            hotels = [Poi.from_pack(r, self.svc.city)
                      for r in get_city(self.svc.city).poi_table()
                      if str(r.get('kind') or '') == '住宿']
            anchor_pois = [p for p in cands if p.name in used][:6]
            if hotels and anchor_pois:
                hotels.sort(key=lambda h: sum(_km(h, a) for a in anchor_pois))
                picked = hotels[:3]
                parts = []
                for h in picked:
                    price = str(h.price or '未标价')
                    parts.append(f'{h.name}（{price}）')
                names = ' / '.join(parts)
                notes.append(f'酒店未定 → 按这几天的点位分布，建议住：{names}'
                             f'（价位标"参考价"的是城市包种子值，不是实时房价；'
                             f'在左栏「锚点」里一键设定后，所有天的通行都从它算起）')
            else:
                notes.append('酒店未定：建议放在地铁 1/2 号线沿线（新街口—大行宫一带）')
        if not sights:
            return {'ok': False, 'message': '城市包里没有可用景点数据', 'days': []}
        covered = sum(len(d['items']) for d in days)
        if covered < days_total:
            notes.append(f'可用地点只够排 {covered} 天，其余天数建议留白或换城市')
        return {
            'ok': True,
            'start': start.isoformat(),
            'days_total': days_total,
            'pace': pace, 'wake': wake, 'party': party,
            'hotel': hotel_pref,
            'days': days,
            'notes': notes,
            'message': f'给出 {days_total} 天草案，共 '
                       f'{sum(len(d["items"]) for d in days)} 项安排',
        }
