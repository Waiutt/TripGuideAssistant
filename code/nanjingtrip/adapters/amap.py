# -*- coding: utf-8 -*-
"""高德地图 Web 服务适配器：POI 检索 + 路径规划（驾车/步行/公交）。"""
from __future__ import annotations

from typing import Optional

import requests

#: 复用一个 Session：每次新建连接都要重做 TLS 握手、重载 CA 证书（实测 0.68 s/次），
#: 切一次日期要发 5 个请求，光这一步就吃掉 4 秒。Session 复用连接后总耗时降到 ~1 秒。
_SESSION = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=2)
_SESSION.mount('https://', _adapter)
_SESSION.mount('http://', _adapter)

#: 测试里会把 `requests.get` 换成假 HTTP 层；一旦发现被换过就走假层，
#: 否则用 Session（真实运行时复用连接，省掉每次 0.68 秒的 TLS 握手）。
_ORIG_GET = requests.get


def _get(url, **kwargs):
    if requests.get is not _ORIG_GET:
        return requests.get(url, **kwargs)
    return _SESSION.get(url, **kwargs)

from .. import weather as weather_mod


class MapError(RuntimeError):
    """地图服务调用失败"""


# 与目的地同名的附属设施关键词：命中则降低排序权重（用户多半不是要去这些点）
_SECONDARY_POI_WORDS = (
    '公交站', '地铁站', '停车场', '售票处', '出入口', '收费站', '洗手间',
    '地下车库', '停靠站', '站牌', '临时停靠', '营业厅', '服务点',
)


class AmapAdapter:
    """高德地图适配器（https://lbs.amap.com/api/webservice/guide/api/）"""

    BASE = 'https://restapi.amap.com/v3'
    #: 静态地图接口实测的最高层级（请求更高也只会返回 16 级的图）
    MAX_ZOOM = 16

    def __init__(self, api_key: str, city: str = '南京', timeout: int = 12):
        self.api_key = api_key or ''
        self.city = city
        self.timeout = timeout

    def name(self) -> str:
        return '高德地图'

    # ---------------- POI 检索 ----------------
    def search_poi(self, keyword: str, city: str = '') -> list[dict]:
        if not self.api_key:
            raise MapError('未配置高德地图 API Key')
        params = {
            'key': self.api_key,
            'keywords': keyword,
            'city': city or self.city,
            'citylimit': 'true',
            'offset': 5,
            'page': 1,
            'extensions': 'base',
        }
        try:
            resp = _get(f'{self.BASE}/place/text', params=params,
                                timeout=self.timeout)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise MapError(f'POI 检索失败：{exc}') from exc
        if data.get('status') != '1':
            raise MapError(f"POI 检索失败：{data.get('info')}")
        out = []
        for p in data.get('pois', []):
            lng, lat = (p.get('location') or '0,0').split(',')
            name = p.get('name', '')
            # 名称匹配质量打分：精确 > 前缀 > 包含，避免"新街口"选中"新街口(地铁站)"
            if name == keyword:
                score = 100
            elif name.startswith(keyword):
                score = 90
            elif keyword in name:
                score = 80
            else:
                score = 70
            # 同名附属设施降权：公交站、地铁站、停车场、售票处等不是用户要去的目的地
            if any(odd in name for odd in _SECONDARY_POI_WORDS):
                score -= 25
            out.append({
                'name': name,
                'address': p.get('address', '') if isinstance(p.get('address'), str) else '',
                'district': p.get('adname', '') or p.get('cityname', ''),
                'lng': float(lng or 0),
                'lat': float(lat or 0),
                'provider_poi_id': p.get('id', ''),
                'score': score,
            })
        # 同分时名称更短更贴近用户表述的优先
        out.sort(key=lambda x: (-x['score'], len(x['name'])))
        return out

    # ---------------- 单个地点详情（营业时间/票价/评分） ----------------
    def place_detail(self, keyword: str, city: str = '') -> Optional[dict]:
        """取单个地点的完整详情：`extensions=all` 才带 `biz_ext`。

        `biz_ext` 里是**真实营业时间**（`open_time`，可能形如 "09:00-17:00" 或
        "24小时营业"）、票价（`cost`）与评分（`rating`）——排程要用它，不该写死。
        名称匹配按"精确 > 前缀 > 包含"挑一个，避免拿到隔壁同名小店铺。
        """
        if not self.api_key:
            return None
        params = {
            'key': self.api_key, 'keywords': keyword, 'city': city or self.city,
            'citylimit': 'true', 'offset': 5, 'page': 1, 'extensions': 'all',
        }
        try:
            resp = _get(f'{self.BASE}/place/text', params=params,
                                timeout=self.timeout)
            data = resp.json()
        except Exception as exc:                     # noqa: BLE001
            self.last_error = f'地点详情失败：{exc}'
            return None
        if data.get('status') != '1':
            return None
        best = None
        best_score = -1
        for p in data.get('pois') or []:
            name = p.get('name', '')
            if name == keyword:
                s = 3
            elif name.startswith(keyword):
                s = 2
            elif keyword in name:
                s = 1
            else:
                s = 0
            if any(odd in name for odd in _SECONDARY_POI_WORDS):
                s -= 1
            if s > best_score:
                best, best_score = p, s
        return best

    # ---------------- 公交方案详情 ----------------
    def transit_plans(self, origin: tuple[float, float], dest: tuple[float, float],
                      limit: int = 3) -> list[dict]:
        """返回公交/地铁换乘方案详情（**不含班次时刻表**）。

        高德能给的：方案总时长、总步行距离、票价、每段线路名称与上下车站、
        途经站数、该段耗时、线路首末班时间。
        高德给不了的：某一班车具体的到站/发车时刻——那属于实时公交数据，
        Web 服务未提供，本系统不会凭运营时间臆造班次。
        """
        if not self.api_key:
            raise MapError('未配置高德地图 API Key')
        params = {
            'key': self.api_key,
            'origin': f'{origin[0]:.6f},{origin[1]:.6f}',
            'destination': f'{dest[0]:.6f},{dest[1]:.6f}',
            'city': self.city,
            'strategy': 0,
        }
        try:
            resp = _get(f'{self.BASE}/direction/transit/integrated',
                                params=params, timeout=self.timeout)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise MapError(f'公交方案查询失败：{exc}') from exc
        if data.get('status') != '1':
            raise MapError(f"公交方案查询失败：{data.get('info')}")

        plans: list[dict] = []
        for t in (data.get('route', {}).get('transits') or [])[:limit]:
            lines: list[dict] = []
            legs: list[dict] = []
            walk_m = 0
            for seg in t.get('segments') or []:
                walk = seg.get('walking') or {}
                w_dist = int(walk.get('distance') or 0)
                w_dur = int(walk.get('duration') or 0)
                walk_m += w_dist
                if w_dist:
                    legs.append({'kind': 'walk', 'distance_m': w_dist,
                                 'minutes': max(1, round(w_dur / 60)),
                                 'from_loc': walk.get('origin', ''),
                                 'to_loc': walk.get('destination', '')})
                bus = seg.get('bus') or {}
                buslines = bus.get('buslines') or []
                if not buslines:
                    continue
                bl = buslines[0]        # 同段多线路时取第一条作为代表
                dep = bl.get('departure_stop') or {}
                arr = bl.get('arrival_stop') or {}
                item = {
                    'name': bl.get('name', ''),
                    'from_stop': dep.get('name', ''),
                    'to_stop': arr.get('name', ''),
                    'from_loc': dep.get('location', ''),
                    'to_loc': arr.get('location', ''),
                    'stops': int(bl.get('via_num') or 0),
                    'minutes': max(1, round(int(bl.get('duration') or 0) / 60)),
                    'distance_m': int(bl.get('distance') or 0),
                    'start_time': bl.get('start_time', ''),
                    'end_time': bl.get('end_time', ''),
                }
                lines.append(item)
                legs.append({'kind': 'bus', **item})
            plans.append({
                'duration_min': max(1, round(int(t.get('duration') or 0) / 60)),
                'walking_m': walk_m,
                'cost': t.get('cost', ''),
                'transfers': max(0, len(lines) - 1),
                'lines': lines,
                'legs': legs,
                'night': str(t.get('nightflag', '')) == '1',
            })
        return plans

    # ---------------- 静态地图（位置示意底图） ----------------
    def static_map_base(self, center: tuple[float, float],
                        size: tuple[int, int] = (900, 400),
                        zoom: int = 12, scale: int = 1) -> bytes:
        """只取**底图**（不含标记与连线），返回 PNG 字节。

        说明：实测该接口的 `paths` 参数在普通 Key 上恒返回
        UNKNOWN_ERROR(20003)，`markers` 可用；因此连线与序号标记由客户端
        （浏览器 SVG）叠加，既避开接口限制，也能按时间顺序编号。

        `scale` 取 1 或 2：取 2 时返回 2 倍像素（1024×640 → 2048×1280），
        地图页放大看时才不糊。
        """
        if not self.api_key:
            raise MapError('未配置高德地图 API Key')
        w = max(100, min(1024, int(size[0])))
        h = max(100, min(1024, int(size[1])))
        params = {
            'key': self.api_key,
            'location': f'{center[0]:.6f},{center[1]:.6f}',
            # 实测：静态图接口**最高只到 16 级**，请求 17/18 返回的仍是 16 级的图。
            # 因此这里必须夹到 16，否则调用方按 17 级投影标记，底图与标记会成倍错位。
            'zoom': max(3, min(self.MAX_ZOOM, int(zoom))),
            'size': f'{w}*{h}',
            'scale': 2 if int(scale) >= 2 else 1,
        }
        try:
            resp = _get(f'{self.BASE}/staticmap', params=params,
                                timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            raise MapError(f'获取静态地图失败：{exc}') from exc
        ctype = resp.headers.get('Content-Type', '')
        if resp.status_code != 200 or 'image' not in ctype:
            raise MapError(f'静态地图返回异常：{resp.status_code} {ctype}')
        return resp.content

    # 兼容旧名字
    def static_map(self, center: tuple[float, float],
                   points: list[tuple[float, float]],
                   size: tuple[int, int] = (900, 400)) -> bytes:
        return self.static_map_base(center, size)

    # ---------------- 骑行（v4 接口） ----------------
    def _bicycling(self, origin: tuple[float, float],
                   dest: tuple[float, float]) -> Optional[dict]:
        params = {
            'key': self.api_key,
            'origin': f'{origin[0]:.6f},{origin[1]:.6f}',
            'destination': f'{dest[0]:.6f},{dest[1]:.6f}',
        }
        try:
            resp = _get(f'{self.BASE.replace("/v3", "/v4")}'
                                f'/direction/bicycling', params=params,
                                timeout=self.timeout)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise MapError(f'骑行路径规划失败：{exc}') from exc
        if str(data.get('errcode', '0')) != '0':
            raise MapError(f"骑行路径规划失败：{data.get('errmsg')}")
        paths = (data.get('data') or {}).get('paths') or []
        if not paths:
            return None
        path = paths[0]
        try:
            seconds = int(path.get('duration') or 0)
            meters = int(path.get('distance') or 0)
        except (TypeError, ValueError):
            return None
        if seconds <= 0:
            return None
        return {'duration_min': max(1, round(seconds / 60)), 'distance_m': meters}

    # ---------------- 逐步路径（用于"路径详情"） ----------------
    def route_steps(self, origin: tuple[float, float], dest: tuple[float, float],
                    mode: str) -> Optional[dict]:
        """返回带分步指令的路径：驾车/步行用 v3，骑行用 v4。

        返回 {'duration_min', 'distance_m', 'steps': [{'instruction', 'distance_m', 'minutes'}]}
        """
        if not self.api_key:
            raise MapError('未配置高德地图 API Key')
        if mode == 'bicycling':
            params = {'key': self.api_key,
                      'origin': f'{origin[0]:.6f},{origin[1]:.6f}',
                      'destination': f'{dest[0]:.6f},{dest[1]:.6f}'}
            url = f'{self.BASE.replace("/v3", "/v4")}/direction/bicycling'
            try:
                data = _get(url, params=params, timeout=self.timeout).json()
            except Exception as exc:  # noqa: BLE001
                raise MapError(f'骑行路径查询失败：{exc}') from exc
            if str(data.get('errcode', '0')) != '0':
                raise MapError(f"骑行路径查询失败：{data.get('errmsg')}")
            paths = (data.get('data') or {}).get('paths') or []
        else:
            api = 'walking' if mode == 'walking' else 'driving'
            params = {'key': self.api_key,
                      'origin': f'{origin[0]:.6f},{origin[1]:.6f}',
                      'destination': f'{dest[0]:.6f},{dest[1]:.6f}'}
            if mode != 'walking':
                params['strategy'] = 0
            try:
                data = _get(f'{self.BASE}/direction/{api}',
                                    params=params, timeout=self.timeout).json()
            except Exception as exc:  # noqa: BLE001
                raise MapError(f'路径查询失败：{exc}') from exc
            if data.get('status') != '1':
                raise MapError(f"路径查询失败：{data.get('info')}")
            paths = (data.get('route') or {}).get('paths') or []
        if not paths:
            return None
        path = paths[0]
        steps = []
        for st in path.get('steps') or []:
            inst = st.get('instruction') or st.get('action') or ''
            try:
                meters = int(st.get('distance') or 0)
                secs = int(st.get('duration') or 0)
            except (TypeError, ValueError):
                meters, secs = 0, 0
            steps.append({'instruction': inst, 'distance_m': meters,
                          'minutes': max(0, round(secs / 60))})
        try:
            seconds = int(path.get('duration') or 0)
            meters = int(path.get('distance') or 0)
        except (TypeError, ValueError):
            return None
        return {'duration_min': max(1, round(seconds / 60)),
                'distance_m': meters, 'steps': steps}

    # ---------------- 逆地理编码与候选点位（"确认点"用） ----------------
    def weather(self, adcode: str) -> list[dict]:
        """取当天+未来 3 天预报（高德 weatherInfo）；失败返回空列表，不抛异常。

        注意：高德只提供 4 天窗口，窗口之外的日期调用方必须显示"无预报数据"。
        """
        if not self.api_key:
            return []
        try:
            resp = _get(f'{self.BASE}/weather/weatherInfo',
                                params={'key': self.api_key, 'city': adcode,
                                        'extensions': 'all'},
                                timeout=self.timeout)
            data = resp.json()
        except Exception as exc:                  # noqa: BLE001 天气失败不影响排程
            self.last_error = f'天气接口失败：{exc}'
            return []
        if data.get('status') != '1':
            return []
        return weather_mod.parse_casts(data)

    def geocode(self, address: str, city: str = '') -> Optional[dict]:
        """地址 → 坐标（"玄武区珠江路653号"这类门牌号靠它，不靠 POI 关键词硬搜）"""
        if not self.api_key:
            return None
        params = {'key': self.api_key, 'address': address,
                  'city': city or self.city}
        try:
            data = _get(f'{self.BASE}/geocode/geo', params=params,
                                timeout=self.timeout).json()
        except Exception as exc:                 # noqa: BLE001
            self.last_error = f'地理编码失败：{exc}'
            return None
        if data.get('status') != '1':
            return None
        geos = data.get('geocodes') or []
        if not geos:
            return None
        g = geos[0]
        loc = (g.get('location') or '').split(',')
        if len(loc) != 2:
            return None
        try:
            lng, lat = float(loc[0]), float(loc[1])
        except ValueError:
            return None
        comp = g.get('province') or ''
        return {'lng': lng, 'lat': lat,
                'address': g.get('formatted_address') or address,
                'district': g.get('district') or '',
                'province': comp}

    def regeo(self, lng: float, lat: float) -> Optional[dict]:
        """坐标 → 结构化地址，用来让用户核对"这个点到底是不是我想去的地方"。"""
        if not self.api_key:
            raise MapError('未配置高德地图 API Key')
        p = {'key': self.api_key, 'location': f'{lng:.6f},{lat:.6f}',
             'extensions': 'base', 'radius': 500}
        try:
            data = _get(f'{self.BASE}/geocode/regeo', params=p,
                                timeout=self.timeout).json()
        except Exception as exc:                  # noqa: BLE001
            raise MapError(f'逆地理编码失败：{exc}') from exc
        if data.get('status') != '1':
            raise MapError(f"逆地理编码失败：{data.get('info')}")
        rg = data.get('regeocode') or {}
        comp = rg.get('addressComponent') or {}
        return {'address': rg.get('formatted_address') or '',
                'district': comp.get('district') or '',
                'street': comp.get('township') or ''}

    def place_candidates(self, keywords: str, city: str,
                         limit: int = 5) -> list[dict]:
        """按名称找候选 POI（带门牌地址），供用户"确认/纠正点位"。"""
        if not self.api_key:
            raise MapError('未配置高德地图 API Key')
        p = {'key': self.api_key, 'keywords': keywords, 'city': city,
             'citylimit': 'true', 'offset': str(max(1, min(10, limit))),
             'page': '1', 'extensions': 'base'}
        try:
            data = _get(f'{self.BASE}/place/text', params=p,
                                timeout=self.timeout).json()
        except Exception as exc:                  # noqa: BLE001
            raise MapError(f'地点检索失败：{exc}') from exc
        if data.get('status') != '1':
            raise MapError(f"地点检索失败：{data.get('info')}")
        out = []
        for item in data.get('pois') or []:
            try:
                lng, lat = (float(v) for v in item['location'].split(','))
            except (KeyError, ValueError):
                continue
            out.append({'name': item.get('name') or '',
                        'address': item.get('address') or '',
                        'district': item.get('adname') or '',
                        'lng': lng, 'lat': lat,
                        'type': item.get('type') or ''})
        return out

    def route_options(self, origin: tuple[float, float],
                      dest: tuple[float, float], mode: str,
                      limit: int = 3) -> list[dict]:
        """同一方式下的**多个可选路径**（供用户"确认路径"时挑选）。

        返回 `[{'label', 'minutes', 'distance_m', 'lines'}]`，驾车/步行/骑行按方案、
        公交按线路分别给出；每个方案带自己的折线，选定后地图只画这一条。
        """
        if not self.api_key:
            raise MapError('未配置高德地图 API Key')
        o = f'{origin[0]:.6f},{origin[1]:.6f}'
        d = f'{dest[0]:.6f},{dest[1]:.6f}'
        base = {'key': self.api_key, 'origin': o, 'destination': d}
        out: list[dict] = []
        try:
            if mode == 'bicycling':
                data = _get(f'{self.BASE.replace("/v3", "/v4")}'
                                    f'/direction/bicycling', params=base,
                                    timeout=self.timeout).json()
                if str(data.get('errcode', '0')) != '0':
                    return []
                paths = (data.get('data') or {}).get('paths') or []
                for i, path in enumerate(paths[:limit], 1):
                    lines = [self._parse_polyline(st.get('polyline', ''))
                             for st in path.get('steps') or []]
                    lines = [ln for ln in lines if len(ln) > 1]
                    if lines:
                        out.append({'label': f'骑行方案 {i}',
                                    'minutes': max(1, round(int(path.get('duration') or 0) / 60)),
                                    'distance_m': int(path.get('distance') or 0),
                                    'lines': lines})
            elif mode == 'transit':
                params = dict(base, city=self.city)
                data = _get(f'{self.BASE}/direction/transit/integrated',
                                    params=params, timeout=self.timeout).json()
                if data.get('status') != '1':
                    return []
                for i, t0 in enumerate(((data.get('route') or {}).get('transits') or [])[:limit], 1):
                    lines = []
                    names = []
                    for seg in t0.get('segments') or []:
                        for st in (seg.get('walking') or {}).get('steps') or []:
                            pts = self._parse_polyline(st.get('polyline', ''))
                            if len(pts) > 1:
                                lines.append(pts)
                        for bl in ((seg.get('bus') or {}).get('buslines') or [])[:1]:
                            pts = self._parse_polyline(bl.get('polyline', ''))
                            if len(pts) > 1:
                                lines.append(pts)
                            if bl.get('name'):
                                names.append(str(bl['name']).split('(')[0])
                    if lines:
                        label = ' → '.join(names[:3]) if names else f'公交方案 {i}'
                        out.append({'label': label,
                                    'minutes': max(1, round(int(t0.get('duration') or 0) / 60)),
                                    'distance_m': int(t0.get('distance') or 0),
                                    'lines': lines})
            else:
                api = 'walking' if mode == 'walking' else 'driving'
                params = dict(base)
                if api == 'driving':
                    params['strategy'] = 0
                    params['extensions'] = 'base'
                data = _get(f'{self.BASE}/direction/{api}',
                                    params=params, timeout=self.timeout).json()
                if data.get('status') != '1':
                    return []
                paths = (data.get('route') or {}).get('paths') or []
                for i, path in enumerate(paths[:limit], 1):
                    lines = [self._parse_polyline(st.get('polyline', ''))
                             for st in path.get('steps') or []]
                    lines = [ln for ln in lines if len(ln) > 1]
                    if lines:
                        out.append({'label': f'{api == "walking" and "步行" or "驾车"}方案 {i}',
                                    'minutes': max(1, round(int(path.get('duration') or 0) / 60)),
                                    'distance_m': int(path.get('distance') or 0),
                                    'lines': lines})
        except Exception:                          # noqa: BLE001 取不到就交给上层退回
            return out
        out.sort(key=lambda x: x['minutes'])
        return out

    # ---------------- 路径折线（地图上画真实走向） ----------------
    @staticmethod
    def _parse_polyline(raw: str) -> list[list[float]]:
        """'lng,lat;lng,lat' → [[lng, lat], ...]"""
        out: list[list[float]] = []
        for seg in (raw or '').split(';'):
            if ',' not in seg:
                continue
            try:
                lng, lat = seg.split(',')[:2]
                out.append([round(float(lng), 6), round(float(lat), 6)])
            except (TypeError, ValueError):
                continue
        return out

    def route_polyline(self, origin: tuple[float, float],
                       dest: tuple[float, float],
                       mode: str) -> Optional[dict]:
        """返回该方式下的**真实走向折线**，供地图按当前通行方式绘制。

        返回 {'distance_m', 'minutes', 'lines': [[[lng,lat], ...], ...]}；
        取不到（无 Key、限流、无方案）时返回 None，由上层退回直线示意。
        公交会把「步行接驳 + 线路」拆成多条折线，避免把不着边的点连起来。
        """
        if not self.api_key:
            raise MapError('未配置高德地图 API Key')
        o = f'{origin[0]:.6f},{origin[1]:.6f}'
        d = f'{dest[0]:.6f},{dest[1]:.6f}'
        base = {'key': self.api_key, 'origin': o, 'destination': d}
        lines: list[list[list[float]]] = []
        dist = mins = 0
        try:
            if mode == 'bicycling':
                data = _get(f'{self.BASE.replace("/v3", "/v4")}'
                                    f'/direction/bicycling', params=base,
                                    timeout=self.timeout).json()
                if str(data.get('errcode', '0')) != '0':
                    return None
                paths = (data.get('data') or {}).get('paths') or []
                if not paths:
                    return None
                path = paths[0]
                for st in path.get('steps') or []:
                    pts = self._parse_polyline(st.get('polyline', ''))
                    if len(pts) > 1:
                        lines.append(pts)
                dist = int(path.get('distance') or 0)
                mins = max(1, round(int(path.get('duration') or 0) / 60))
            elif mode == 'transit':
                params = dict(base, city=self.city)
                data = _get(f'{self.BASE}/direction/transit/integrated',
                                    params=params, timeout=self.timeout).json()
                if data.get('status') != '1':
                    return None
                transits = (data.get('route') or {}).get('transits') or []
                if not transits:
                    return None
                t0 = transits[0]
                for seg in t0.get('segments') or []:
                    walking = seg.get('walking') or {}
                    for st in walking.get('steps') or []:
                        pts = self._parse_polyline(st.get('polyline', ''))
                        if len(pts) > 1:
                            lines.append(pts)
                    bus = seg.get('bus') or {}
                    for bl in (bus.get('buslines') or [])[:1]:
                        pts = self._parse_polyline(bl.get('polyline', ''))
                        if len(pts) > 1:
                            lines.append(pts)
                dist = int(t0.get('distance') or 0)
                mins = max(1, round(int(t0.get('duration') or 0) / 60))
            else:
                api = 'walking' if mode == 'walking' else 'driving'
                params = dict(base)
                if api == 'driving':
                    params['strategy'] = 0
                data = _get(f'{self.BASE}/direction/{api}',
                                    params=params, timeout=self.timeout).json()
                if data.get('status') != '1':
                    return None
                paths = (data.get('route') or {}).get('paths') or []
                if not paths:
                    return None
                path = paths[0]
                for st in path.get('steps') or []:
                    pts = self._parse_polyline(st.get('polyline', ''))
                    if len(pts) > 1:
                        lines.append(pts)
                dist = int(path.get('distance') or 0)
                mins = max(1, round(int(path.get('duration') or 0) / 60))
        except Exception:                       # noqa: BLE001  取不到就走直线示意
            return None
        if not lines:
            return None
        return {'distance_m': dist, 'minutes': mins, 'lines': lines}

    # ---------------- 路径规划 ----------------
    def route(self, origin: tuple[float, float], dest: tuple[float, float],
              mode: str) -> Optional[dict]:
        if not self.api_key:
            raise MapError('未配置高德地图 API Key')
        if mode == 'bicycling':
            return self._bicycling(origin, dest)
        dir_map = {'driving': 'driving', 'walking': 'walking',
                   'transit': 'transit/integrated'}
        api = f'{self.BASE}/direction/{dir_map.get(mode, "driving")}'
        params = {
            'key': self.api_key,
            'origin': f'{origin[0]:.6f},{origin[1]:.6f}',
            'destination': f'{dest[0]:.6f},{dest[1]:.6f}',
        }
        if mode == 'driving':
            params['strategy'] = 0        # 速度优先
        if mode == 'transit':
            params['city'] = self.city
        try:
            resp = _get(api, params=params, timeout=self.timeout)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise MapError(f'路径规划失败：{exc}') from exc
        if data.get('status') != '1':
            raise MapError(f"路径规划失败：{data.get('info')}")
        route = data.get('route', {})

        # 公交/地铁：结果在 route.transits 里（duration/distance 为秒/米），
        # 驾车与步行才在 route.paths 里 —— 两者必须分开解析
        if mode == 'transit':
            transits = route.get('transits') or []
            if not transits:
                return None
            best = transits[0]
            try:
                seconds = int(best.get('duration') or 0)
                meters = int(best.get('distance') or 0)
            except (TypeError, ValueError):
                return None
            if seconds <= 0:
                return None
            return {'duration_min': max(1, round(seconds / 60)), 'distance_m': meters}

        paths = route.get('paths') or []
        if not paths:
            return None
        path = paths[0]
        try:
            seconds = int(path.get('duration') or 0)
            meters = int(path.get('distance') or 0)
        except (TypeError, ValueError):
            return None
        if seconds <= 0:
            return None
        return {'duration_min': max(1, round(seconds / 60)), 'distance_m': meters}
