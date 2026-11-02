# -*- coding: utf-8 -*-
"""Web 端接口层：把应用服务暴露成 REST，供浏览器 SPA 调用。

为什么单独一个模块
------------------
这一层**只做协议转换**：把 HTTP 请求翻译成 `TripService` / `AssistantService` 的调用，
再把结果压成"只有叶子字段"的纯 JSON（不回传任何 Cordis/领域对象，避免序列化事故）。
它不启动端口、不碰 socket，因此测试可以直接调函数，不必起服务。

与旧版移动端页面的关系
----------------------
`lan_server.py` 原来只服务一个写死的手机页面 + 两个端点。大改为 Web 之后，
页面变成 `nanjingrip/web/` 下的 SPA，本模块负责它需要的全部数据与动作。

接口总览
--------
GET  /api/health                      健康检查与模式
GET  /api/state?date&rev              当日状态（行程、统计、冲突、提示、住宿）
GET  /api/options?date                出行方式、缓冲、可排时段、城市列表
GET  /api/versions?date               版本列表（回滚用）
GET  /api/leg?task_id&mode            两点之间的路径详情（分段 + 接驳）
GET  /api/plans?task_id               公交方案（多线路 + 班次推算）
GET  /api/advice?task_id              出行建议（几点出发最合适）
GET  /api/modes?from&to               四方式对比
GET  /api/lodging?date                过夜住宿推荐
GET  /api/map?date                    当日位置示意图（PNG）

POST /api/command                     统一写操作（见 ACTIONS）
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from .cities import city_names, get_city
from .hours import weekday_cn
from .models import TaskStatus, TransitMode, hm_text

log = logging.getLogger('nanjingtrip.web')

# 撤销缓冲：批量删除后暂存原始快照（含 Poi 对象，不能塞进 JSON），
# 前端只拿一个令牌回来撤销。进程内、有上限、用完即弃——本地单用户应用足够了。
_UNDO: dict[str, list] = {}
_UNDO_MAX = 20


# ======================================================================
# 工具
# ======================================================================
def _parse_date(raw: Optional[str]) -> date:
    if not raw:
        return date.today()
    return date.fromisoformat(str(raw)[:10])


def _hm(minutes: Optional[int]) -> str:
    return hm_text(minutes) if minutes else ''


# ======================================================================
# GET：读接口
# ======================================================================
def state_payload(ctx, day: date, since_rev: Optional[int] = None,
                  with_modes: bool = False) -> dict:
    """当日状态。`changed` 供客户端轮询判断是否需要重绘。"""
    rev = ctx.db.revision()
    trip = ctx.trip_svc.day_view(day)
    result = ctx.trip_svc.compute_day(day)
    stats = ctx.trip_svc.day_stats(trip, result)
    pack = get_city(ctx.city)

    items = []
    prev_poi = trip.lodging      # 住宿锚点也是真实出发点，首段距离从它算起
    for it in result.ordered():
        leg = it.leg_before
        poi = it.task.poi
        # 距离：引擎的通行段只带时长，距离从通行缓存补（用于里程/碳排估算）
        dist = leg.distance_m if leg else 0
        if leg is not None and not dist and prev_poi is not None \
                and prev_poi.poi_id and poi is not None and poi.poi_id:
            cached = ctx.transit_svc.cache.get_full(prev_poi.poi_id, poi.poi_id,
                                                    leg.mode.value)
            if cached:
                dist = int(cached.get('distance_m') or 0)

        modes = None
        if leg is not None and prev_poi is not None and poi is not None and with_modes:
            try:
                raw = ctx.trip_svc.transit.estimate_all(prev_poi, poi)
                known = {k: v for k, v in raw.items() if v}
                fastest = min(known, key=known.get) if known else ''
                modes = {k: {'minutes': v, 'text': hm_text(v) if v else '—',
                             'fastest': k == fastest}
                         for k, v in raw.items()}
            except Exception:      # noqa: BLE001 对比失败不影响主渲染
                modes = None
        items.append({
            'task_id': it.task.task_id,
            'title': it.task.title,
            'category': it.task.category,
            'time': it.start_ts.strftime('%H:%M'),
            'end': it.end_ts.strftime('%H:%M'),
            'minutes': it.task.duration_min,
            'minutes_text': hm_text(it.task.duration_min),
            'poi': poi.name if poi else '',
            'district': poi.district if poi else '',
            'poi_brief': poi.brief() if poi else '',
            'locked': bool(it.task.is_locked),
            'done': it.task.status == TaskStatus.DONE,
            'mode': (it.task.mode.value if it.task.mode else ''),
            'conflict': any(c.task_title == it.task.title for c in result.conflicts),
            'modes': modes,
            'leg': ({
                'mode': leg.mode.value,
                'mode_cn': leg.mode.cn,
                'minutes': leg.duration_min,
                'minutes_text': hm_text(leg.duration_min),
                'distance_m': dist,
                'distance_text': (f'{dist / 1000:.1f} 公里' if dist >= 1000
                                  else f'{dist} 米') if dist else '',
            } if leg else None),
        })
        prev_poi = poi
    for t in result.unscheduled:
        items.append({
            'task_id': t.task_id, 'title': t.title, 'time': '', 'end': '',
            'minutes': t.duration_min, 'minutes_text': hm_text(t.duration_min),
            'poi': t.poi.name if t.poi else '',
            'district': t.poi.district if t.poi else '',
            'poi_brief': t.poi.brief() if t.poi else '',
            'locked': bool(t.is_locked), 'done': False,
            'mode': (t.mode.value if t.mode else ''), 'leg': None,
            'unscheduled': True, 'conflict': False,
        })

    notices = [{'kind': c.kind, 'detail': c.detail,
                'suggestions': [s.text for s in c.suggestions]}
               for c in result.notices]
    conflicts = [{'kind': c.kind, 'detail': c.detail,
                  'task_title': c.task_title,
                  'suggestions': [{'text': s.text, 'kind': s.kind,
                                   'patch': s.patch} for s in c.suggestions]}
                 for c in result.conflicts]

    return {
        'rev': rev,
        'changed': since_rev is None or int(since_rev) != rev,
        'date': day.isoformat(),
        'weekday': weekday_cn(day),
        'city': ctx.city,
        'city_label': pack.title_label,
        'lodging': trip.lodging.name if trip.lodging else '',
        'trip_id': trip.trip_id,
        'mode': ctx.degrade.mode_label(),
        'mode_short': ('离线' if ctx.degrade.is_offline() else
                       '在线' if (ctx.degrade.llm_ready() and ctx.degrade.map_ready())
                       else '半在线'),
        'offline': ctx.degrade.is_offline(),
        # 天气：None 表示不在 4 天预报窗口内（界面要如实显示"无预报"）
        'weather': ctx.trip_svc.day_weather(day),
        'profile': getattr(getattr(ctx, 'profile', None), 'as_dict', lambda: {})() if getattr(ctx, 'profile', None) else {},
        # 大模型"配了但调不通"时如实标记，别让界面以为一切正常
        'llm_ok': ctx.degrade.llm_ok(),
        'llm_error': ctx.degrade.llm_last_error,
        'explanation': result.explanation,
        'conflicts': conflicts,
        'notices': notices,
        'stats': {
            'count': stats['count'], 'busy': stats['busy'],
            'transit': stats['transit'], 'idle': stats['idle'],
            'busy_text': hm_text(stats['busy']),
            'transit_text': hm_text(stats['transit']),
            'idle_text': hm_text(stats['idle']),
            'cached_legs': stats['cached_legs'],
        },
        'items': items,
    }


def options_payload(ctx, day: date) -> dict:
    s = ctx.settings
    pack = get_city(ctx.city)
    # 建议去处：直接取城市包里"玩/吃"类地点（按包内顺序，不做热度排名）
    places = []
    for p in pack.pois:
        if p.get('kind') not in ('景点', '场馆', '公园', '街区', '书店'):
            continue
        visit = int(p.get('visit') or 0)
        places.append({
            'name': p['name'], 'district': p.get('district', ''),
            'kind': p.get('kind', ''),
            'brief': '｜'.join(x for x in (
                p.get('kind', ''),
                (f"{p['open'][0]}-{p['open'][1]}" if p.get('open') else ''),
                (''.join(p.get('closed', ())) + '闭馆' if p.get('closed') else ''),
                p.get('book', '')) if x),
            'visit_min': visit,
            'visit_text': (f'建议 {visit // 60} 小时' if visit >= 60 else
                           f'建议 {visit} 分钟') if visit else '',
        })
        if len(places) >= 12:
            break
    return {
        'city': ctx.city,
        'cities': city_names(),
        'transit_mode': s.transit_mode,
        'buffer_min': s.buffer_min,
        'day_start': s.day_start,
        'day_end': s.day_end,
        'return_by': getattr(s, 'return_by', ''),
        'peak_aware': bool(getattr(s, 'peak_aware', True)),
        'offline_mode': bool(s.offline_mode),
        'keep_order': bool(getattr(s, 'keep_order', False)),
        'auto_mode': bool(getattr(s, 'auto_mode', False)),
        'optimize_route': bool(getattr(s, 'optimize_route', False)),
        'has_llm_key': s.has_llm_key,
        'has_map_key': s.has_map_key,
        'modes': [{'value': m.value, 'label': m.cn} for m in TransitMode],
        'places': places,
    }


def versions_payload(ctx, day: date) -> dict:
    trip = ctx.trip_svc.day_view(day)
    if trip.trip_id is None:
        return {'versions': []}
    rows = ctx.trip_svc.list_versions(trip.trip_id)
    return {'versions': [
        {'version_id': v.version_id, 'created_at': v.created_at, 'note': v.note}
        for v in rows]}


def month_payload(ctx, year: int, month: int) -> dict:
    """左栏月历：本月每天排了几项（只回真实库内数据，没有就是 0）"""
    if not (1 <= month <= 12) or not (2000 <= year <= 2100):
        today = date.today()
        year, month = today.year, today.month
    counts = ctx.trip_repo.month_counts(year, month, ctx.city)
    return {'year': year, 'month': month, 'days': counts}


MAP_SIZE = (900, 380)
MAP_SIZE_BIG = (1024, 640)          # 地图页：高德单边上限 1024


def _map_size(w: Optional[int], h: Optional[int], big: bool) -> tuple[int, int]:
    """把请求里的宽高夹到高德允许的范围（100–1024），非法值退回默认。"""
    dw, dh = MAP_SIZE_BIG if big else MAP_SIZE
    try:
        ww = int(w) if w else dw
        hh = int(h) if h else dh
    except (TypeError, ValueError):
        ww, hh = dw, dh
    return (max(240, min(1024, ww)), max(180, min(1024, hh)))


def _map_view(ctx, day: date, w=None, h=None, zoom=None, center=None,
              big: bool = False) -> dict:
    """地图页与小地图共用的「视图参数」：尺寸、层级、中心点、点位。"""
    from .geo import bbox_center, fit_zoom

    size = _map_size(w, h, big)
    points, note = ctx.trip_svc.day_map_points(day)
    if not points:
        return {'ok': False, 'message': note or '当天没有带地点的安排',
                'points': [], 'size': list(size)}
    coords = [(p['lng'], p['lat']) for p in points]
    try:
        z = int(zoom) if zoom else fit_zoom(coords, size)
    except (TypeError, ValueError):
        z = fit_zoom(coords, size)
    # 与高德静态图的实际能力保持一致：最高 16 级。
    # 若这里报 17 而底图其实是 16 级的，前端按 17 级投影就会成倍错位。
    z = max(3, min(16, z))
    if center:
        try:
            clng, clat = (float(x) for x in str(center).split(','))
            cen = (clng, clat)
        except (TypeError, ValueError):
            cen = bbox_center(coords)
    else:
        cen = bbox_center(coords)
    return {'ok': True, 'size': list(size), 'zoom': z,
            'center': [round(cen[0], 6), round(cen[1], 6)],
            'points': [{'seq': i, 'name': p['title'], 'time': p['time'],
                        'lng': p['lng'], 'lat': p['lat']}
                       for i, p in enumerate(points, 1)]}


def mapconfig_payload(ctx) -> dict:
    """给前端的地图配置：是否用高德 JS API（交互地图）、以及所需的 Key。

    说明：JS API 的 Key 必须下发到浏览器（高德的设计如此），本机单用户 +
    局域网自用可接受；若要更严，去高德控制台配「域名白名单 + 安全密钥」。
    离线（未配置 Key 或强制离线）时 `ready=False`，前端退回静态图 + SVG 叠标。
    """
    s = ctx.settings
    ready = bool(not s.offline_mode and s.map_api_key)
    return {
        'ready': ready,
        'key': s.map_api_key if ready else '',
        'version': '2.0',
        'plugins': ['AMap.Geolocation'],
        'offline': not ready,
        'reason': '' if ready else (
            '强制离线中' if s.offline_mode else '未配置高德地图 Key'),
    }


def regeo_payload(ctx, lng, lat) -> dict:
    """坐标 → 地址（拖动标记后给用户看"这个点现在对应哪儿"）"""
    try:
        x, y = float(lng), float(lat)
    except (TypeError, ValueError):
        return {'ok': False, 'message': '坐标不合法'}
    fn = getattr(ctx.degrade.map(), 'regeo', None)
    if fn is None or not ctx.degrade.map_ready():
        return {'ok': False, 'message': '离线模式：无法解析地址', 'lng': x, 'lat': y}
    try:
        rg = fn(x, y) or {}
    except Exception as exc:                  # noqa: BLE001
        return {'ok': False, 'message': f'解析失败：{exc}', 'lng': x, 'lat': y}
    return {'ok': True, 'lng': x, 'lat': y,
            'address': rg.get('address', ''), 'district': rg.get('district', '')}


def plans_index_payload(ctx) -> dict:
    """所有规划记录（按日期倒序）：日期 / 城市 / 项数 / 首末时刻 / 起点"""
    rows = ctx.trip_repo.list_days()
    for r in rows:
        trip = ctx.trip_repo.get_day(date.fromisoformat(r['date']), r['city'])
        r['lodging'] = trip.lodging.name if (trip and trip.lodging) else ''
        r['active'] = (r['city'] == ctx.city)
    return {'ok': True, 'count': len(rows), 'days': rows, 'city': ctx.city}


def pointinfo_payload(ctx, day: date) -> dict:
    """第 1 步「确认点」：每个点位的坐标核验信息 + 可切换的候选点位。

    给出高德对该坐标的逆地理地址，用户一眼就能看出"这个点是不是我想去的地方"。
    """
    points = ctx.trip_svc.point_audit(day)
    # 把 task_id 补上，前端才能发"钉住这个坐标"
    trip = ctx.trip_svc.day_view(day)
    tasks = sorted([t for t in trip.tasks if t.start_ts and t.poi],
                   key=lambda t: t.start_ts)  # type: ignore[arg-type,return-value]
    for item, task in zip(points, tasks):
        item['task_id'] = task.task_id
        item['poi_name'] = task.poi.name if task.poi else ''
    return {'ok': True, 'points': points,
            'online': bool(ctx.degrade.map_ready())}


def legoptions_payload(ctx, day: date, mode: str = '', limit: int = 3) -> dict:
    """第 3 步「确认路径」：每段路的多个可选方案（含各自折线与耗时）。"""
    try:
        lim = max(1, min(5, int(limit)))
    except (TypeError, ValueError):
        lim = 3
    legs, note = ctx.trip_svc.day_leg_options(day, mode, lim)
    if not legs:
        return {'ok': False, 'message': note or '当天没有需要绘制的路段',
                'legs': [], 'mode': mode}
    return {'ok': True, 'mode': legs[0]['mode'], 'legs': legs,
            'online': bool(ctx.degrade.map_ready())}


def mappath_payload(ctx, day: date, mode: str = '') -> dict:
    """地图上的路段折线：**按当前通行方式**返回真实走向。

    在线时 `source='amap'`（高德返回的折线，跟着道路走）；离线/失败时 `lines=[]`，
    前端画直线并标注「估算直线」——不把示意图说成真实路径。
    """
    legs, note = ctx.trip_svc.day_leg_paths(day, mode)
    if not legs:
        return {'ok': False, 'message': note or '当天没有需要绘制的路段',
                'legs': [], 'mode': mode}
    return {'ok': True, 'mode': legs[0]['mode'], 'legs': legs,
            'online': bool(ctx.degrade.map_ready())}


def mapinfo_payload(ctx, day: date, w=None, h=None, zoom=None, center=None,
                    big: bool = False) -> dict:
    """位置示意图的「打点数据」：当天点位 + 投影参数。

    底图由 `/api/map` 返回（高德静态图，只有路网，没有标记）。Qt 版是用 QPainter
    在服务端把点和折线画上去的，改成 Web 之后那段绘制没有了，于是地图上只有底图
    （缺陷 D-25）。这里把点位与投影所需的中心点/缩放/画布尺寸一起交给浏览器，
    由前端用标准 Web Mercator 公式换算成像素坐标，叠一层 SVG 打点连线。
    """
    return _map_view(ctx, day, w, h, zoom, center, big)


def leg_payload(ctx, task_id: int, mode: str = '', access: str = 'bicycling',
                egress: str = 'walking') -> dict:
    """路段详情。

    注意 `leg_detail_for()` 返回的是 `(detail, note)` 二元组，必须**解包**：
    早期直接塞进 JSON，前端拿到的是 `[{}, "离线模式…"]` 这种数组（缺陷 D-33）。
    """
    m = TransitMode(mode) if mode in {x.value for x in TransitMode} else None
    detail, note = ctx.trip_svc.leg_detail_for(int(task_id), m, access, egress)
    if not detail:
        return {'ok': False, 'message': note or '该安排没有可解析的上一站',
                'detail': {}}
    return {'ok': True, 'detail': detail, 'message': note}


def plans_payload(ctx, task_id: int) -> dict:
    plans, note = ctx.trip_svc.transit_plans_for(int(task_id))
    return {'plans': plans, 'note': note}


def _advice_dict(adv) -> dict:
    """把 DepartureAdvice 数据类转成纯 JSON（原来直接塞对象 → 500，缺陷 D-34）"""
    def opt(o):
        if o is None:
            return None
        return {'depart_label': getattr(o, 'depart_label', ''),
                'minutes': getattr(o, 'minutes', 0),
                'factor_label': getattr(o, 'factor_label', ''),
                'wait_min': getattr(o, 'wait_min', 0)}
    return {'ok': bool(getattr(adv, 'ok', False)),
            'task_title': getattr(adv, 'task_title', ''),
            'mode_cn': getattr(adv, 'mode_cn', ''),
            'target_label': getattr(adv, 'target_label', ''),
            'base_minutes': getattr(adv, 'base_minutes', 0),
            'latest': opt(getattr(adv, 'latest', None)),
            'best_offpeak': opt(getattr(adv, 'best_offpeak', None)),
            'options': [opt(o) for o in (getattr(adv, 'options', None) or [])],
            'message': getattr(adv, 'message', ''),
            'caveat': getattr(adv, 'caveat', ''),
            'summary': adv.summary() if hasattr(adv, 'summary') else ''}


def advice_payload(ctx, task_id: int) -> dict:
    advice = ctx.trip_svc.departure_advice(int(task_id))
    if advice is None:
        return {'ok': False, 'message': '该安排缺少地点或时间'}
    data = _advice_dict(advice)
    return {'ok': True, 'advice': data}


def modes_payload(ctx, from_name: str, to_name: str) -> dict:
    a = ctx.trip_svc._poi_named(from_name) if from_name else None
    b = ctx.trip_svc._poi_named(to_name) if to_name else None
    if a is None or b is None:
        return {'ok': False, 'message': '地点未找到', 'modes': {}}
    return {'ok': True, 'modes': ctx.trip_svc.transit.estimate_all(a, b)}


def lodging_payload(ctx, day: date) -> dict:
    recs = ctx.trip_svc.recommend_lodging(day)
    trip = ctx.trip_svc.day_view(day)
    return {'current': trip.lodging.name if trip.lodging else '',
            'declined': bool(trip.lodging_declined), 'candidates': recs}


def map_payload(ctx, day: date, w=None, h=None, zoom=None, center=None,
                big: bool = False, scale: int = 1) -> tuple[Optional[bytes], str]:
    """返回 (png 字节, 说明)。离线或失败时 bytes 为 None。

    注意 `day_map_image()` 本身已经返回 `(bytes, note)` 二元组，这里要**解包**：
    早期版本直接把它当成 bytes 交给 HTTP 层，导致响应头写着 `Content-Length: 2`
    （元组长度）而正文不是图片，前端永远看不到位置示意图（缺陷 D-25）。

    `big=True` 用地图页的大画布（1024×640），配 `scale=2` 得到 2048×1280 的底图，
    放大看才不糊。
    """
    size = _map_size(w, h, big)
    cen = None
    if center:
        try:
            clng, clat = (float(x) for x in str(center).split(','))
            cen = (clng, clat)
        except (TypeError, ValueError):
            cen = None
    try:
        data, note = ctx.trip_svc.day_map_image(day, size=size, zoom=zoom,
                                                center=cen, scale=scale)
    except Exception as exc:      # noqa: BLE001 位置图失败不该拖垮页面
        log.warning('位置示意图生成失败：%s', exc)
        return None, f'位置示意图生成失败：{exc}'
    if not data:
        return None, note or '当日没有带地点的安排'
    return data, ''


def health_payload(ctx) -> dict:
    return {'ok': True, 'rev': ctx.db.revision(),
            'mode': ctx.degrade.mode_label(),
            # llm_ok=False 表示 Key 配了但最近一次调用失败（已退回规则解析）：
            # 让调用方/界面能看出"在线"其实是降级状态
            'llm_ok': ctx.degrade.llm_ok(),
            'llm_error': ctx.degrade.llm_last_error,
            'city': ctx.city, 'version': '2.0-web'}



# ======================================================================
# 用户档案（多用户切换 + 偏好存档）
# ======================================================================
def profiles_payload(ctx) -> dict:
    """档案列表 + 当前生效项（界面用来渲染切换下拉）"""
    prof = getattr(ctx, 'profile', None) or ctx.profiles.active()
    return {'ok': True,
            'active': prof.name,
            'profiles': [p.as_dict() | {'describe': p.describe()}
                         for p in ctx.profiles.list()]}


# ======================================================================
# POST：写接口
# ======================================================================
def _text(value, default: str = '') -> str:
    """把外部参数安全地当字符串用：数字、None、对象都不会炸（D-E1）"""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _int_or_none(value):
    """把外部参数安全地转 int；转不了返回 None，由调用方决定报什么错（D-E2）"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _err(message: str, rev: int = 0) -> dict:
    return {'ok': False, 'message': message, 'rev': rev}


def _ok(message: str, ctx, **extra) -> dict:
    out = {'ok': True, 'message': message, 'rev': ctx.db.revision()}
    out.update(extra)
    return out


def command_payload(ctx, body: dict) -> dict:
    """统一写入口。所有 action 都返回 {ok, message, rev[, ...]}。"""
    action = _text(body.get('action'))
    try:
        day = _parse_date(body.get('date'))
    except ValueError:
        return _err('日期格式不对')

    # ---------- 批量多目的地：一次说清多个点 ----------
    if action == 'preview_batch':
        text = _text(body.get('text'))
        if not text:
            return _err('请先输入一句话')
        out = ctx.trip_svc.preview_batch(text, day)
        items = out.get('items') or []
        if not items:
            hint = '；'.join((out.get('unparsed') or [])[:3])
            return _err('没能从这句话里解析出地点'
                        + (f'（未解析：{hint}）' if hint else '')
                        + '，请试试"上午九点去A，1小时；然后去B，40分钟"')
        return {'ok': True, 'kind': 'batch', 'rev': ctx.db.revision(),
                'message': f'解析出 {len(items)} 项，确认后写入',
                'origin': out.get('origin') or '',
                'split_by': out.get('split_by') or 'rules',
                'segments': out.get('segments') or [],
                'return_by': out.get('return_by') or '',
                'conditional': out.get('conditional') or [],
                'items': items,
                'unparsed': out.get('unparsed') or [],
                'keep_order': bool(out.get('keep_order')),
                'optimize_route': bool(getattr(ctx.settings,
                                               'optimize_route', False))}

    if action == 'add_batch':
        raw_items = body.get('items')
        if raw_items is not None and not isinstance(raw_items, list):
            return _err('items 需要是数组')
        if isinstance(raw_items, list) and any(not isinstance(x, dict) for x in raw_items):
            return _err('items 的每一项都需要是对象')
        items = body.get('items') or []
        if not items:
            return _err('没有可写入的条目')
        ok, msg = ctx.trip_svc.add_batch(
            day, items, origin=(body.get('origin') or None),
            keep_order=bool(body.get('keep_order', True)),
            return_by=(body.get('return_by') or ''))
        return _ok(msg, ctx) if ok else _err(msg)

    # ---------- 新增：先预览（不落库），再确认 ----------
    if action == 'preview':
        text = _text(body.get('text'))
        if not text:
            return _err('请先输入一句话')
        out = ctx.trip_svc.preview_text(text, day)
        if out.lodging is not None:
            return {'ok': True, 'kind': 'lodging', 'message': out.explanation,
                    'rev': ctx.db.revision(),
                    'lodging': out.lodging.name}
        if not out.ok:
            return _err(out.error or ('缺少：' + '、'.join(out.missing)))
        d = out.draft
        poi = d.poi
        return {
            'ok': True, 'kind': 'task', 'rev': ctx.db.revision(),
            'message': '已解析，确认后写入',
            'draft': {
                'title': d.title,
                'category': d.category,
                'duration_min': d.duration_min,
                'duration_text': hm_text(d.duration_min),
                'desired_start': (d.desired_start.strftime('%H:%M')
                                  if d.desired_start else ''),
                'mode': d.mode.value if d.mode else '',
                'locked': bool(d.is_locked),
                'poi': poi.name if poi else '',
                'poi_district': poi.district if poi else '',
                'poi_brief': poi.brief() if poi else '',
            },
            'candidates': [
                {'name': p.name, 'district': p.district, 'brief': p.brief()}
                for p in (out.candidates or [])[:5]],
            'notices': [c.detail for c in out.notices],
        }

    # ---------- 新增：确认入库 ----------
    if action == 'add':
        text = _text(body.get('text'))
        if text:
            prev = ctx.trip_svc.preview_text(text, day)
            if prev.lodging is not None:
                out = ctx.trip_svc.set_lodging(day, prev.lodging.name)
                return _ok(out.explanation or '已设为住宿锚点', ctx)
            if not prev.ok or prev.draft is None:
                return _err(prev.error or ('缺少：' + '、'.join(prev.missing)))
            draft = prev.draft
        else:
            draft = None
        # 从预览卡带回来的字段（用户在网页上改过）
        patch = body.get('draft') or {}
        if draft is not None:
            if patch.get('title'):
                draft.title = str(patch['title'])
            if patch.get('category'):
                draft.category = str(patch['category'])
            if patch.get('duration_min') not in (None, ''):
                minutes = _int_or_none(patch.get('duration_min'))
                if minutes is None:
                    return _err('时长需要是整数分钟')
                draft.duration_min = max(10, min(600, minutes))
            if patch.get('mode'):
                draft.mode = TransitMode(patch['mode'])
            poi_name = patch.get('poi')
            if poi_name:
                draft.poi = next(
                    (p for p in (prev.candidates or []) if p.name == poi_name),
                    draft.poi)
            start = (patch.get('desired_start') or '').strip()
            if start:
                hh, mm = start.split(':')[:2]
                from datetime import time as _t
                draft.desired_start = _t(int(hh), int(mm))
                draft.is_locked = True
            elif 'desired_start' in patch:
                draft.desired_start = None
                draft.is_locked = False
        if draft is None:
            return _err('请先输入一句话')
        out = ctx.trip_svc.commit_task(day, draft, note='Web 端新增')
        return _ok(out.explanation or '已添加', ctx,
                   conflict=not out.result.is_feasible() if out.result else False)

    # ---------- 单条操作 ----------
    if action in ('delete', 'done', 'set_mode', 'move', 'advice'):
        tid = body.get('task_id')
        if not tid:
            return _err('缺少任务')

    if action == 'delete':
        tid = _int_or_none(body.get('task_id'))
        if tid is None:
            return _err('task_id 需要是数字')
        out, snaps = ctx.trip_svc.delete_many([tid])
        return _ok('已删除', ctx) if snaps else _err(out.error or '删除失败')

    if action == 'done':
        tid = _int_or_none(body.get('task_id'))
        if tid is None:
            return _err('task_id 需要是数字')
        out = ctx.trip_svc.mark_done(tid)
        return _ok('已标记完成', ctx) if out.result else _err(out.error or '操作失败')

    if action == 'set_mode':
        mode = body.get('mode') or ''
        if mode and mode not in {m.value for m in TransitMode}:
            return _err('交通方式不合法')
        # 用户手动指定某一段的方式时，关掉"每段自动择优"（否则会被择优覆盖）
        if ctx.settings.auto_mode:
            ctx.settings.auto_mode = False
            ctx.apply_settings(ctx.settings)
        tid = _int_or_none(body.get('task_id'))
        if tid is None:
            return _err('task_id 需要是数字')
        out = ctx.trip_svc.adjust(tid, {'mode': mode or None})
        return _ok('已切换方式', ctx) if out.result else _err(out.error or '操作失败')

    if action == 'delete_day':
        # 规划记录管理：删掉整天（含所有事项与通行段）
        try:
            d = date.fromisoformat(str(body.get('date') or day.isoformat()))
        except ValueError:
            return _err('日期格式不对')
        n = ctx.trip_repo.delete_day(d, ctx.city)
        return (_ok(f'已删除 {d.isoformat()} 的整份行程（{n} 天记录）', ctx)
                if n else _err('该日没有行程'))

    if action == 'delay':
        # 动态变更：某项多花了 N 分钟，后续链式顺延
        try:
            delay_min = int(body.get('delay_min') or 0)
            task_id = int(body.get('task_id') or 0)
        except (TypeError, ValueError):
            return _err('延误分钟数或任务号不合法')
        if delay_min <= 0:
            return _err('延误分钟数必须大于 0')
        out = ctx.trip_svc.delay_task(
            task_id, delay_min, keep_start=bool(body.get('keep_start', True)))
        return (_ok(out.explanation or f'已按延误 {delay_min} 分钟重排', ctx)
                if out.result else _err(out.error or '重排失败'))

    if action == 'move':
        start = _text(body.get('start'))
        import re as _re
        if not _re.match(r'^\d{1,2}:\d{2}$', start):
            return _err('时间格式应为 HH:MM')
        hh, mm = (int(x) for x in start.split(':'))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return _err('时间超出范围')
        stamp = f'{hh:02d}:{mm:02d}'
        tid2 = _int_or_none(body.get('task_id'))
        if tid2 is None:
            return _err('task_id 需要是数字')
        out = ctx.trip_svc.adjust(tid2, {'start_ts': stamp})
        return _ok(f'已改到 {stamp}', ctx) if out.result else _err(out.error or '调整失败')

    # ---------- 批量 ----------
    if action == 'delete_many':
        ids = [int(i) for i in (body.get('task_ids') or []) if i]
        if not ids:
            return _err('未选择任何安排')
        out, snaps = ctx.trip_svc.delete_many(ids)
        if not snaps:
            return _err(out.error or '删除失败')
        # 快照里含 Poi 对象，不能塞进 JSON：服务端留缓冲，前端只拿一个撤销令牌
        import uuid
        token = uuid.uuid4().hex[:8]
        _UNDO[token] = snaps
        while len(_UNDO) > _UNDO_MAX:
            _UNDO.pop(next(iter(_UNDO)))
        safe = [{'title': s['title'], 'duration_min': s['duration_min'],
                 'category': s['category'], 'raw_text': s.get('raw_text', ''),
                 'poi_name': s['poi'].name if s.get('poi') else ''} for s in snaps]
        return _ok(f'已删除 {len(snaps)} 项', ctx, deleted=safe, undo_token=token)

    if action == 'restore_many':
        token = _text(body.get('undo_token'))
        snaps = _UNDO.pop(token, None) if token else None
        if not snaps:
            return _err('撤销已失效（可能已恢复过或程序重启过）')
        out = ctx.trip_svc.restore_many(snaps, day)
        return _ok(f'已撤销删除（{len(snaps)} 项）', ctx) if out.result \
            else _err(out.error or '撤销失败')

    if action == 'set_mode_many':
        ids = [int(i) for i in (body.get('task_ids') or []) if i]
        mode = body.get('mode') or ''
        if not ids:
            return _err('未选择任何安排')
        if mode and mode not in {m.value for m in TransitMode}:
            return _err('交通方式不合法')
        out = ctx.trip_svc.set_mode_many(ids, mode or None)
        return _ok(f'已批量切换方式（{len(ids)} 项）', ctx) if out.result \
            else _err(out.error or '操作失败')

    # ---------- 建议采纳 ----------
    if action == 'consult':
        text = _text(body.get('text'))
        if not text:
            return _err('说点什么吧：日期、人数、想玩什么，都行')
        session = str(body.get('session') or 'default')
        out = ctx.consult.chat(session, text, day)
        payload = _ok(out.get('message') or '', ctx)
        payload.update({k: out.get(k) for k in ('kind', 'questions', 'slots', 'plan')})
        return payload

    if action == 'propose_trip':
        text = _text(body.get('text'))
        if not text:
            return _err('先说说你想去哪、玩几天')
        plan = ctx.trip_svc.propose_trip(text, day)
        if not plan.get('ok'):
            return _err(plan.get('message') or '没能给出草案')
        out = _ok(plan.get('message') or '已给出草案', ctx)
        out['plan'] = plan
        return out

    if action == 'apply_proposal':
        if not isinstance(body.get('plan') or {}, dict):
            return _err('plan 需要是对象')
        draft_guard = body.get('draft')
        if draft_guard is not None and not isinstance(draft_guard, dict):
            return _err('draft 需要是对象')
        plan = body.get('plan') or {}
        ok, msg = ctx.trip_svc.apply_proposal(plan, hotel=(body.get('hotel') or ''))
        return _ok(msg, ctx) if ok else _err(msg)

    if action == 'fit_day':
        plan = ctx.trip_svc.fit_day(day)
        if not plan.get('ok'):
            return _err(plan.get('message') or '无法给出取舍方案')
        if body.get('apply'):
            # 真按取舍执行：删掉建议舍弃的项后再排一次
            trip = ctx.trip_svc.day_view(day)
            drop = set(plan.get('drop') or [])
            ids = [t.task_id for t in trip.tasks if t.title in drop and t.task_id]
            if ids:
                ctx.trip_svc.delete_many(ids)
                ctx.trip_svc.replan(day)
            plan['message'] = (plan['message'] + '；已按建议舍弃 '
                               + '、'.join(sorted(drop)))
        out = _ok(plan.get('message') or '已给出取舍方案', ctx)
        out['fit'] = plan
        return out

    if action == 'apply_suggestion':
        patch = body.get('patch') or {}
        out = ctx.trip_svc.apply_suggestion(patch, day)
        return _ok(out.explanation or '已采纳建议', ctx) if out.ok \
            else _err(out.error or '采纳失败')

    # ---------- 住宿锚点 ----------
    if action == 'set_lodging':
        name = _text(body.get('name'))
        if not name:
            return _err('缺少住宿名称')
        out = ctx.trip_svc.set_lodging(day, name)
        return _ok(out.explanation or f'已设为住宿：{name}', ctx) if out.ok \
            else _err(out.error or '设置失败')

    if action == 'clear_lodging':
        out = ctx.trip_svc.clear_lodging(day)
        return _ok('已清除住宿锚点', ctx) if out.ok else _err(out.error or '清除失败')

    if action == 'decline_lodging':
        out = ctx.trip_svc.decline_lodging(day, bool(body.get('declined', True)))
        return _ok('已标记当天往返', ctx) if out.ok else _err(out.error or '操作失败')

    # ---------- 重排 / 回滚 ----------
    if action == 'replan':
        out = ctx.trip_svc.replan(day)
        return _ok(out.explanation or '已重新规划', ctx) if out.result \
            else _err(out.error or '重新规划失败')

    if action == 'rollback':
        tid, vid = body.get('trip_id'), body.get('version_id')
        if not tid or not vid:
            return _err('缺少版本信息')
        out = ctx.trip_svc.rollback(int(tid), int(vid))
        return _ok(f'已回滚到版本 {vid}', ctx) if out.result \
            else _err(out.error or '回滚失败')

    # ---------- 用户档案：切换 / 保存 / 删除 ----------
    if action == 'set_profile':
        name = _text(body.get('name'))
        if not name:
            return _err('缺少档案名')
        prof = ctx.switch_profile(name)
        return _ok(f'已切换到「{prof.name}」（{prof.describe()}）', ctx,
                   profile=prof.as_dict())

    if action == 'save_profile':
        from .profile import Profile
        prof = ctx.save_profile(Profile(
            name=_text(body.get('name')) or '默认',
            identity=(body.get('identity') or ''),
            effort=(body.get('effort') or '标准'),
            pace=(body.get('pace') or '标准'),
            budget=(body.get('budget') or '不设'),
            wake=(body.get('wake') or ''),
            has_car=bool(body.get('has_car', True)),
            mode_priority=(body.get('mode_priority') or '最快'),
            note=(body.get('note') or '')))
        return _ok(f'已保存偏好「{prof.name}」（{prof.describe()}）', ctx,
                   profile=prof.as_dict())

    if action == 'delete_profile':
        name = _text(body.get('name'))
        if not name:
            return _err('缺少档案名')
        if not ctx.profiles.delete(name):
            return _err(f'没有「{name}」这个档案')
        prof = ctx.profiles.active()
        ctx.apply_profile(prof, persist_active=False)
        return _ok(f'已删除档案「{name}」，当前为「{prof.name}」', ctx,
                   profile=prof.as_dict())

    # ---------- 城市 / 设置 ----------
    if action == 'set_city':
        name = _text(body.get('city'))
        if not name:
            return _err('缺少城市')
        ctx.set_city(name)
        return _ok(f'已切换到{ctx.city}', ctx, city=ctx.city)

    if action == 'pin_point':
        # 第 1 步「确认点」：把某事项的地点钉到用户确认过的坐标，并重排当天
        try:
            task_id = int(body.get('task_id') or 0)
            lng = float(body.get('lng'))
            lat = float(body.get('lat'))
        except (TypeError, ValueError):
            return _err('坐标不合法')
        ok, message = ctx.trip_svc.pin_point(day, task_id, lng, lat)
        return _ok(message, ctx) if ok else _err(message)

    # ---------- 重新开始一轮顾问对话（清掉槽位，界面清空聊天） ----------
    if action == 'consult_reset':
        session = _text(body.get('session'))
        if session:
            try:
                ctx.consult.store.delete(session)
            except Exception as exc:            # noqa: BLE001 删不掉也不该挡住重开
                log.warning('会话重置失败：%s', exc)
        return _ok('已重新开始：之前的偏好不会带过来', ctx)

    # ---------- 数据规模与维护（只读统计 / 显式维护） ----------
    if action == 'db_stats':
        out = ctx.db.stats()
        out.update({'ok': True, 'rev': ctx.db.revision()})
        return out

    if action == 'db_maintain':
        report = ctx.db.maintain(force_checkpoint=True)
        parts = []
        if report['transit_pruned']:
            parts.append(f"清理通行缓存 {report['transit_pruned']} 条")
        if report['intel_pruned']:
            parts.append(f"清理过期情报 {report['intel_pruned']} 条")
        if report['checkpointed']:
            parts.append('已截断 WAL')
        if report['vacuumed']:
            parts.append('已整理库文件')
        stats = ctx.db.stats()
        parts.append(f"库 {stats['db_bytes'] // 1024} KB / WAL {stats['wal_bytes'] // 1024} KB")
        return _ok('数据维护完成：' + '，'.join(parts), ctx)

    # ---------- 导出（精简版：只留时刻/地点/停留/费用，一次最多 7 天） ----------
    if action == 'export_plan':
        from datetime import date as _date, timedelta as _td
        from .export import brief_markdown, clamp_range

        start = _text(body.get('start')) or _text(body.get('date'))
        end = _text(body.get('end')) or start
        ok, why = clamp_range(start, end)
        if not ok:
            return {'ok': False, 'message': why, 'rev': ctx.db.revision()}
        rows = []
        cur = _date.fromisoformat(start)
        last = _date.fromisoformat(end)
        while cur <= last:
            try:
                trip = ctx.trip_svc.day_view(cur)
            except Exception:                     # noqa: BLE001 某天读不出来就跳过
                trip = None
            items = []
            for task in getattr(trip, 'tasks', []) or []:
                if not getattr(task, 'start_ts', None):
                    continue
                items.append({
                    'start': task.start_ts.strftime('%H:%M'),
                    'end': task.end_ts.strftime('%H:%M') if task.end_ts else '',
                    'title': task.title or '',
                    'poi': (task.poi.name if getattr(task, 'poi', None) else ''),
                    'duration_min': int(getattr(task, 'duration_min', 0) or 0),
                    'category': getattr(task, 'category', '') or ''})
            items.sort(key=lambda it: it['start'])
            try:
                cost = ctx.consult._day_cost(items, {})
            except Exception:                     # noqa: BLE001 计价失败不影响导出
                cost = {}
            rows.append({'date': cur.isoformat(),
                         'weekday': '一二三四五六日'[cur.weekday()],
                         'items': items, 'cost': cost})
            cur += _td(days=1)
        busy = [r for r in rows if r['items']]
        if not busy:
            return {'ok': False, 'rev': ctx.db.revision(),
                    'message': f'{start} 到 {end} 这几天还没有安排，没什么可导出的'}
        md = brief_markdown(rows)
        return {'ok': True, 'rev': ctx.db.revision(), 'markdown': md,
                'days': len(busy),
                'filename': f'行程方案_{start}_{end}.md',
                'message': f'已导出 {len(busy)} 天（精简版）'}

    # ---------- Agent 能力目录（只读：给界面和报告看）----------
    if action == 'agent_skills':
        try:
            from .agent.skills import api_payload
        except Exception as exc:            # noqa: BLE001
            return _err(f'能力目录读取失败：{exc}')
        out = api_payload()
        out['rev'] = ctx.db.revision()
        return out

    if action == 'toggle_offline':
        s = ctx.settings
        s.offline_mode = bool(body.get('offline', not s.offline_mode))
        ctx.apply_settings(s)
        return _ok('已切换为' + ('离线模式' if s.offline_mode else '在线模式'), ctx)

    if action == 'set_settings':
        s = ctx.settings
        # 先校验再落库：非法值一旦写进 config.json，重启后整天视图都会 500（D-E4）
        if body.get('transit_mode') not in (None, ''):
            mode = str(body['transit_mode']).strip()
            if mode not in {m.value for m in TransitMode}:
                return _err(f'不支持的出行方式：{mode}')
        for key in ('day_start', 'day_end'):
            if body.get(key) not in (None, ''):
                value = str(body[key]).strip()
                try:
                    hh, mm = value.split(':')
                    if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                        raise ValueError
                except (ValueError, AttributeError):
                    return _err(f'{key} 需要 HH:MM 格式，收到：{value}')
        if body.get('buffer_min') not in (None, ''):
            try:
                minutes = int(body['buffer_min'])
            except (TypeError, ValueError):
                return _err(f'buffer_min 需要整数分钟，收到：{body.get("buffer_min")}')
            if not 0 <= minutes <= 120:
                return _err('buffer_min 需要在 0–120 分钟之间')
        for key in ('transit_mode', 'buffer_min', 'day_start', 'day_end'):
            if key in body and body[key] not in (None, ''):
                setattr(s, key, body[key])
        # 手动改缓冲 = 改了基准：否则用户档案的节奏会在旧基准上反复加减
        if body.get('buffer_min') not in (None, ''):
            try:
                s.buffer_base = int(body['buffer_min'])
            except (TypeError, ValueError):
                pass
        # 返回截止允许清空（''=不限制），所以单独处理，不能套上面的"非空才写"
        if 'return_by' in body and body['return_by'] is not None:
            s.return_by = str(body['return_by']).strip()
        # 排程策略开关：保持顺序 / 每段自动择优
        was_auto = bool(getattr(s, 'auto_mode', False))
        for key in ('keep_order', 'auto_mode', 'optimize_route', 'peak_aware'):
            if key in body and body[key] is not None:
                setattr(s, key, bool(body[key]))
        ctx.apply_settings(s)
        # 关掉自动择优时，先清掉各段已写回的择优方式，回到全局默认
        if was_auto and not s.auto_mode:
            try:
                ctx.trip_svc.clear_task_modes(day)
            except Exception as exc:      # noqa: BLE001
                log.debug('清理逐段方式失败：%s', exc)
        # 策略变了要重排当日，时间轴才跟着变
        if any(k in body for k in ('keep_order', 'auto_mode', 'optimize_route',
                                   'transit_mode', 'buffer_min', 'peak_aware',
                                   'day_start', 'day_end', 'return_by')):
            try:
                ctx.trip_svc.replan(day)
            except Exception as exc:      # noqa: BLE001 无行程时重排会跳过
                log.debug('设置变更后重排跳过：%s', exc)
        return _ok('设置已保存', ctx)

    # ---------- AI 问答 ----------
    # ---------- 助手：对话即执行（问答 / 录入 / 变更需确认） ----------
    if action == 'assistant_act':
        text = _text(body.get('text'))
        # 1) 用户点了确认之后才真正执行
        # 待执行动作放在 act 键里：action 已被“命令名”占用，不能复用
        if body.get('apply') and isinstance(body.get('act'), dict):
            res = ctx.assistant_svc.apply(body['act'], day)
            return {'ok': bool(res.get('ok')), 'kind': 'applied',
                    'message': res.get('message') or '',
                    'undo_version': res.get('undo_version'),
                    'trip_id': res.get('trip_id'),
                    'rev': ctx.db.revision()}
        if not text:
            return _err('请输入内容')
        out = ctx.assistant_svc.route(text, day)
        kind = out.get('kind')
        if kind == 'answer':
            brief = ctx.assistant_svc.day_brief(day)
            reply = ctx.assistant_svc.ask(text, plan_date=day, context=brief)
            return {'ok': True, 'kind': 'answer', 'message': reply.text,
                    'source': reply.source, 'rev': ctx.db.revision()}
        if kind == 'preview':
            prev = ctx.trip_svc.preview_batch(text, day)
            items = prev.get('items') or []
            if not items:
                hint = '；'.join((prev.get('unparsed') or [])[:3])
                return {'ok': False, 'kind': 'clarify',
                        'message': '没能从这句话里解析出地点'
                                   + (f'（未解析：{hint}）' if hint else ''),
                        'rev': ctx.db.revision()}
            return {'ok': True, 'kind': 'batch', 'origin': prev.get('origin') or '',
                    'items': items, 'unparsed': prev.get('unparsed') or [],
                    'keep_order': bool(prev.get('keep_order')),
                    'message': f'解析出 {len(items)} 项，确认后写入',
                    'rev': ctx.db.revision()}
        return {'ok': True, 'kind': kind, 'message': out.get('message') or '',
                'action': out.get('action'), 'effect': out.get('effect'),
                'options': out.get('options') or [], 'rev': ctx.db.revision()}

    if action == 'ask':
        q = _text(body.get('question'))
        if not q:
            return _err('请输入你想问的内容')
        brief = ctx.assistant_svc.day_brief(day)      # 主线程算好再问（跨线程读库会炸）
        reply = ctx.assistant_svc.ask(q, plan_date=day, context=brief)
        return {'ok': reply.ok, 'message': reply.text, 'source': reply.source,
                'elapsed_ms': reply.elapsed_ms, 'rev': ctx.db.revision()}

    return _err(f'未知操作：{action}')
