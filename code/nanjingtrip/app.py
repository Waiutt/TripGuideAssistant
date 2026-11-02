# -*- coding: utf-8 -*-
"""装配层（Composition Root）：把配置、数据库、适配器与服务组装成应用上下文。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .cities import get_city, normalize_city
from .config import Settings, SettingsService
from .profile import PRIORITY_TOLERANCE_MIN, ProfileStore
from .consult import ConsultStore, Consultant
from .evidence import DurationEvidence
from .intel import PoiIntelService
from .services import (AssistantService, DegradeController, GeoService,
                       IntentParseService, TransitService, TripService)
from .storage import (Database, SqliteTransitCache, SqliteTripRepository,
                      SqliteVersionRepository)


class AppContext:
    """应用上下文：持有全部单例服务（界面只依赖本对象）"""

    def __init__(self, data_dir: Optional[str | Path] = None):
        self.settings_svc = SettingsService(Path(data_dir) if data_dir else None)
        self.settings: Settings = self.settings_svc.load()
        self.db = Database(self.settings_svc.db_path())
        # 启动即做一次轻量维护：清过期缓存、必要时截断 WAL（未超阈值几乎不花时间）
        try:
            self.db.maintain()
        except Exception as exc:            # noqa: BLE001 维护失败不挡住启动
            log.warning('数据库维护失败：%s', exc)
        self.trip_repo = SqliteTripRepository(self.db)
        self.transit_cache = SqliteTransitCache(self.db)
        self.version_repo = SqliteVersionRepository(self.db,
                                                   keep=self.settings.keep_versions)
        self.degrade = DegradeController(self.settings)
        self.parse_svc = IntentParseService(self.degrade)
        self.geo_svc = GeoService(self.degrade, self.trip_repo)
        self.transit_svc = TransitService(self.degrade, self.transit_cache)
        self.trip_svc = TripService(self.settings_svc, self.settings, self.db,
                                    self.trip_repo, self.version_repo,
                                    self.parse_svc, self.geo_svc,
                                    self.transit_svc, self.degrade)
        # 景点情报（真实营业时间/票价/评分 + 推荐时长估算）：规划时按需取数并落库缓存
        self.poi_intel = PoiIntelService(self.db, self.degrade)
        self.geo_svc.intel = self.poi_intel
        self.trip_svc.intel = self.poi_intel
        # 行程顾问（多轮）：会话存 SQLite，刷新/换端都能接着聊
        # 游览时长证据（official / ota / estimate 三级来源，取值取中位数 + 区间）
        self.evidence = DurationEvidence(self.db)
        self.trip_svc.evidence = self.evidence
        self.consult = Consultant(self.trip_svc, ConsultStore(self.db))
        self.assistant_svc = AssistantService(self.degrade, self.trip_svc)
        self.assistant_svc.consult = self.consult
        # 用户档案存档（多用户切换）：与配置同目录，不进数据库
        self.profiles = ProfileStore(self.settings_svc.dir)
        self.profile = self.profiles.active()
        self._buffer_base = int(getattr(self.settings, 'buffer_min', 10) or 10)
        self._day_start_base = getattr(self.settings, 'day_start', '08:00') or '08:00'
        # 缓冲基准：老配置没有 buffer_base 时，用当前 buffer_min 补一次
        if not getattr(self.settings, 'buffer_base', 0):
            self.settings.buffer_base = int(getattr(self.settings, 'buffer_min', 10) or 10)
        self.apply_profile(self.profile, persist_active=False)

    # ---------- 用户档案（多用户切换 + 偏好存档） ----------
    def apply_profile(self, profile, persist_active: bool = True) -> None:
        """把档案落到运行参数：体力→步行/骑行阈值、节奏→缓冲、起床→可排起点。

        只影响**默认值与阈值**；不触碰已锁定的时刻、已给定的顺序、手动换的方式。
        """
        self.profile = profile
        trip = self.trip_svc
        trip.max_walk_min = profile.max_walk_min
        trip.max_bike_min = profile.max_bike_min
        trip.has_car = bool(getattr(profile, 'has_car', True))
        trip.prefer_mode = getattr(profile, 'preferred_mode', '') or ''
        trip.prefer_tolerance = int(PRIORITY_TOLERANCE_MIN)
        trip.max_price = getattr(profile, 'max_price', None)
        budget = getattr(profile, 'budget', '') or ''
        trip.budget_label = '' if budget in ('', '不设') else budget
        s = self.settings
        base = int(getattr(s, 'buffer_base', 10) or 0)
        # 起床时间：档案没设就回到基础值，不能沿用上一个档案的残留
        s.day_start = profile.day_start or getattr(self, '_day_start_base', '08:00')
        s.buffer_min = max(0, base + int(profile.buffer_delta))
        self.settings_svc.save(s)
        self.trip_svc.settings = s
        self.degrade.refresh(s)
        self.assistant_svc.degrade = self.degrade
        if persist_active:
            try:
                self.profiles.set_active(profile.name)
            except Exception:                 # noqa: BLE001 存档不可写不影响运行
                pass

    def switch_profile(self, name: str):
        """切换当前用户档案并生效，返回生效后的档案"""
        profile = self.profiles.set_active(name)
        self.apply_profile(profile, persist_active=False)
        return profile

    def save_profile(self, profile):
        """保存（或更新）一份档案并设为当前，返回生效后的档案"""
        saved = self.profiles.save(profile)
        self.apply_profile(saved, persist_active=False)
        return saved

    # ---------- 配置变更（FR-12） ----------
    def apply_settings(self, settings: Settings) -> None:
        self.settings_svc.save(settings)
        self.settings = settings
        self.trip_svc.settings = settings
        self.degrade.refresh(settings)
        self.assistant_svc.degrade = self.degrade

    # ---------- 城市切换（BR-01 作用域变化） ----------
    def set_city(self, name: str) -> None:
        """切换城市：保存配置、重建适配器，并让默认出行方式跟随城市包。

        只有当用户没有自定义过默认方式（仍等于旧城市包的默认值）时才跟随，
        避免把用户手动选过的"骑车/步行"覆盖掉。
        """
        old_pack = get_city(self.settings.city)
        new_pack = get_city(normalize_city(name))
        if self.settings.transit_mode == old_pack.default_mode:
            self.settings.transit_mode = new_pack.default_mode
        self.settings.city = new_pack.name
        self.apply_settings(self.settings)

    @property
    def city(self) -> str:
        return self.degrade.city()

    def mode_label(self) -> str:
        return self.degrade.mode_label()


    def close(self) -> None:
        try:
            self.db.close()
        except Exception:      # noqa: BLE001
            pass
