# -*- coding: utf-8 -*-
"""地理投影小工具：把经纬度换算成静态地图上的像素坐标。

高德静态地图用的是 Web Mercator（与主流瓦片地图一致），
因此按 zoom 计算世界像素宽度即可精确落点。
"""
from __future__ import annotations

import math

TILE = 256.0


def distance_km(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    """两点球面距离（公里）：住宿推荐按它排序距离"""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def world_px(zoom: int) -> float:
    return TILE * (2 ** zoom)


def project(lng: float, lat: float, center: tuple[float, float], zoom: int,
            size: tuple[int, int]) -> tuple[float, float]:
    """经纬度 -> 图片内像素坐标（以 center 居中、zoom 缩放）"""
    world = world_px(zoom)

    def x_of(lng_: float) -> float:
        return (lng_ + 180.0) / 360.0 * world

    def y_of(lat_: float) -> float:
        lat_ = max(min(lat_, 85.05112878), -85.05112878)
        rad = math.radians(lat_)
        return (1.0 - math.log(math.tan(rad) + 1 / math.cos(rad)) / math.pi) / 2.0 * world

    cx, cy = x_of(center[0]), y_of(center[1])
    px, py = x_of(lng), y_of(lat)
    w, h = size
    return (w / 2.0 + (px - cx), h / 2.0 + (py - cy))


def fit_zoom(points: list[tuple[float, float]], size: tuple[int, int],
             margin: float = 0.82, min_zoom: int = 9,
             max_zoom: int = 16) -> int:
    """选一个能把所有点都装进画面的最大 zoom"""
    if len(points) < 2:
        return min(max_zoom, 13)
    lngs = [p[0] for p in points]
    lats = [p[1] for p in points]
    span_lng = max(max(lngs) - min(lngs), 1e-4)
    span_lat = max(max(lats) - min(lats), 1e-4)
    w, h = size[0] * margin, size[1] * margin
    # 经度：360 度对应 world 像素
    zoom_x = math.log2(w * 360.0 / (span_lng * TILE)) if span_lng else max_zoom
    # 纬度：用两端点的墨卡托 y 距离
    def y_of(lat_: float) -> float:
        rad = math.radians(max(min(lat_, 85.0), -85.0))
        return (1.0 - math.log(math.tan(rad) + 1 / math.cos(rad)) / math.pi) / 2.0
    span_y = abs(y_of(max(lats)) - y_of(min(lats))) or 1e-6
    zoom_y = math.log2(h / (span_y * TILE))
    zoom = int(math.floor(min(zoom_x, zoom_y)))
    return max(min_zoom, min(max_zoom, zoom))


def bbox_center(points: list[tuple[float, float]]) -> tuple[float, float]:
    lngs = [p[0] for p in points]
    lats = [p[1] for p in points]
    return ((min(lngs) + max(lngs)) / 2, (min(lats) + max(lats)) / 2)


def poi_distance_km(a, b) -> float:
    """两个 POI 之间的距离（公里）；缺坐标返回 99.0（排序时自然排最后）"""
    if not getattr(a, 'lng', None) or not getattr(b, 'lng', None):
        return 99.0
    return distance_km(a.lng, a.lat, b.lng, b.lat)
