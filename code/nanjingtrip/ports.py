# -*- coding: utf-8 -*-
"""端口（Port）：应用服务依赖的抽象接口，基础设施层提供实现（依赖倒置）。"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class LLMPort(Protocol):
    """大模型端口：结构化解析（行程）与通用问答（助手）两个能力"""

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        """把自然语言解析为结构化 JSON（行程意图抽取）"""
        ...

    def chat(self, messages: list[dict], temperature: float = 0.6) -> str:
        """通用多轮问答（桌面助手），messages 为 OpenAI 风格消息列表"""
        ...

    def name(self) -> str:
        ...


@runtime_checkable
class MapPort(Protocol):
    """地图端口：POI 检索与路径规划"""

    def search_poi(self, keyword: str, city: str = '南京') -> list[dict]:
        ...

    def route(self, origin: tuple[float, float], dest: tuple[float, float],
              mode: str) -> Optional[dict]:
        ...

    def name(self) -> str:
        ...


@runtime_checkable
class TripRepository(Protocol):
    def upsert_day(self, trip) -> int:
        ...

    def get_day(self, plan_date) -> Optional[object]:
        ...


@runtime_checkable
class TransitCacheRepository(Protocol):
    def get(self, from_poi_id: int, to_poi_id: int, mode: str) -> Optional[int]:
        ...

    def put(self, from_poi_id: int, to_poi_id: int, mode: str,
            duration_min: int, distance_m: int, source: str) -> None:
        ...


@runtime_checkable
class VersionRepository(Protocol):
    def add(self, trip_id: int, note: str, snapshot_json: str) -> int:
        ...

    def list(self, trip_id: int) -> list[object]:
        ...

    def get(self, version_id: int):
        ...
