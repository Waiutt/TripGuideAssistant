# -*- coding: utf-8 -*-
"""配置与密钥管理（NFR-05：密钥不落数据库、不进日志）。"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = 'NanjingTrip'


def app_data_dir() -> Path:
    """数据目录：%LOCALAPPDATA%\\NanjingTrip（与代码分离，便于备份迁移 NFR-11）"""
    base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
    root = Path(base) if base else Path.home() / '.local' / 'share'
    d = root / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Settings:
    """系统配置（非敏感项写 config.json，密钥写 secrets.json）"""
    llm_provider: str = 'DeepSeek'
    llm_base_url: str = 'https://api.deepseek.com/v1'
    llm_model: str = 'deepseek-flash'
    #: 结构化解析是否关闭"思考"。推理型模型（deepseek-flash）默认先输出一长串
    #: 推理 token 再给答案；本项目的解析是"温度 0 + 固定 JSON 字段"，实测关掉后
    #: 同一任务 3576ms → 1284ms，答案质量不变。留空则不下发该参数。
    llm_reasoning_effort: str = 'none'
    llm_api_key: str = ''

    map_provider: str = '高德地图'
    map_api_key: str = ''

    # 当前城市（BR-01 的作用域；城市包见 nanjingtrip/cities.py）
    city: str = '南京'
    # 常住城市：与当前城市不同即视为"异地行程"，过夜时会主动推荐住宿
    home_city: str = '南京'

    transit_mode: str = 'driving'
    buffer_min: int = 10
    day_start: str = '08:00'
    day_end: str = '22:00'
    #: 最晚回到起点的时刻（HH:MM）；空串表示不限制。超出时给提示，不悄悄放行。
    return_by: str = ''
    #: 早晚高峰是否放大通行预估（在线生效；离线估算没有真实时效，不叠加）
    peak_aware: bool = True
    #: 缓冲的**基准**值（分钟）。用户档案的节奏只在这个基准上加减，
    #: 不能拿算出来的 buffer_min 当基准，否则反复切档案/重启会越切越小。
    buffer_base: int = 10
    offline_mode: bool = True          # 无 key 时默认离线，保证可直接演示
    keep_versions: int = 20
    # 排程策略（对应 ScheduleOptions.keep_order / auto_mode，界面可切换）
    keep_order: bool = False           # 按用户给定顺序依次排
    auto_mode: bool = False            # 每段自动选最快通行方式
    optimize_route: bool = False       # 自动排最短路（最近邻 + 2-opt）
    extra: dict = field(default_factory=dict)

    # ---------- 派生 ----------
    @property
    def has_llm_key(self) -> bool:
        return bool(self.llm_api_key.strip())

    @property
    def has_map_key(self) -> bool:
        return bool(self.map_api_key.strip())

    @property
    def llm_ready(self) -> bool:
        """大模型是否可用（强制离线或没有 Key 时退回离线规则解析）"""
        return (not self.offline_mode) and self.has_llm_key

    @property
    def map_ready(self) -> bool:
        """地图是否可用（强制离线或没有 Key 时退回内置 POI）"""
        return (not self.offline_mode) and self.has_map_key

    @property
    def effective_offline(self) -> bool:
        """两路外部服务都不可用时才算完全离线"""
        return not (self.llm_ready and self.map_ready)


class SettingsService:
    """配置读写（JSON 落盘）"""

    SECRET_FIELDS = ('llm_api_key', 'map_api_key')

    def __init__(self, data_dir: Path | None = None):
        self.dir = Path(data_dir) if data_dir else app_data_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.dir / 'config.json'
        self.secret_path = self.dir / 'secrets.json'

    def load(self) -> Settings:
        cfg: dict = {}
        if self.config_path.exists():
            try:
                cfg = json.loads(self.config_path.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, OSError):
                cfg = {}
        secrets: dict = {}
        if self.secret_path.exists():
            try:
                secrets = json.loads(self.secret_path.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, OSError):
                secrets = {}
        cfg = {k: v for k, v in cfg.items() if k in Settings.__dataclass_fields__}
        s = Settings(**cfg)
        for f in self.SECRET_FIELDS:
            if secrets.get(f):
                setattr(s, f, secrets[f])
        return s

    def save(self, settings: Settings) -> None:
        cfg = {k: v for k, v in asdict(settings).items() if k not in self.SECRET_FIELDS}
        self.config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                    encoding='utf-8')
        secrets = {f: getattr(settings, f) for f in self.SECRET_FIELDS
                   if getattr(settings, f)}
        self.secret_path.write_text(json.dumps(secrets, ensure_ascii=False, indent=2),
                                    encoding='utf-8')
        self._restrict_permission(self.secret_path)

    @staticmethod
    def _restrict_permission(path: Path) -> None:
        """尽力限制密钥文件为仅当前用户可读（Windows ACL）"""
        try:
            if os.name == 'nt':
                os.system(f'icacls "{path}" /inheritance:r /grant:r "%USERNAME%:F" > nul 2>&1')
            else:
                path.chmod(0o600)
        except Exception:      # noqa: BLE001 权限收紧失败不影响主流程
            pass

    def db_path(self) -> Path:
        return self.dir / 'nanjingtrip.db'
