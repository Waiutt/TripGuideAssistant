# -*- coding: utf-8 -*-
"""用户档案：身份/体力/节奏/预算/起床时间，以及多档案的保存与切换。

设计边界（与 tests/test_profile.py 对齐）
----------------------------------------
* 档案只影响**默认值与阈值**：体力→步行/骑行阈值、节奏→缓冲与未指定时长、
  起床→可排起点、预算→餐饮候选排序；
* **不覆盖**用户显式给出的东西（锁定时刻、给定顺序、手动换的方式）；
* 落盘为数据目录下的 `profiles.json`（非敏感、不含密钥、不进数据库）。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

#: 体力 → (步行上限分钟, 骑行上限分钟)。骑行 0 表示"不推荐骑行"。
_EFFORT_CAPS = {
    '轻松': (20, 0),
    '标准': (45, 30),
    '能走能骑': (60, 45),
}
#: 节奏 → (缓冲增量分钟, 未指定时长时的系数)
_PACE = {
    '慢': (5, 1.2),
    '标准': (0, 1.0),
    '快': (-5, 0.8),
}
_EFFORT_DEFAULT = '标准'
_PACE_DEFAULT = '标准'
_BUDGETS = ('省', '中', '不设')
_IDENTITIES = ('学生', '上班族', '带长辈', '带小孩', '情侣', '')
#: 出行方式优先：'最快' 表示不干预；其余按"容差内优先"处理
MODE_PRIORITIES = ('最快', '公共交通优先', '骑行优先', '步行优先', '驾车优先')
#: 优先方式到实际方式的映射
_PRIORITY_MODE = {
    '公共交通优先': 'transit',
    '骑行优先': 'bicycling',
    '步行优先': 'walking',
    '驾车优先': 'driving',
}
#: 优先的容差（分钟）：只快不过它才"听用户的"，否则仍按最快
PRIORITY_TOLERANCE_MIN = 10

#: 预算档位 → 人均上限（元）；None 表示不校验
_BUDGET_CAPS = {'省': 60, '中': 150}

DEFAULT_NAME = '默认'


@dataclass
class Profile:
    """一份用户偏好档案（可切换、可存档）"""
    name: str = DEFAULT_NAME
    identity: str = ''          # 学生 / 上班族 / 带长辈 / 带小孩 / 情侣
    effort: str = _EFFORT_DEFAULT
    pace: str = _PACE_DEFAULT
    budget: str = '不设'         # 省 / 中 / 不设
    wake: str = ''              # HH:MM；空表示不干预可排起点
    #: 有没有车：False 时驾车不参与择优（仍可手动换方式）
    has_car: bool = True
    #: 出行方式优先（'最快' 表示不干预）
    mode_priority: str = '最快'
    note: str = ''

    # ---------- 派生 ----------
    @property
    def max_walk_min(self) -> int:
        return _EFFORT_CAPS.get(self.effort, _EFFORT_CAPS[_EFFORT_DEFAULT])[0]

    @property
    def max_bike_min(self) -> int:
        return _EFFORT_CAPS.get(self.effort, _EFFORT_CAPS[_EFFORT_DEFAULT])[1]

    @property
    def buffer_delta(self) -> int:
        return _PACE.get(self.pace, _PACE[_PACE_DEFAULT])[0]

    @property
    def stay_factor(self) -> float:
        return _PACE.get(self.pace, _PACE[_PACE_DEFAULT])[1]

    @property
    def day_start(self) -> str:
        return (self.wake or '').strip()

    @property
    def max_price(self) -> Optional[int]:
        """人均预算上限（元）；'不设' 返回 None（不做价位校验）"""
        return _BUDGET_CAPS.get(self.budget)

    @property
    def preferred_mode(self) -> str:
        """优先方式对应的 TransitMode 值；'最快' 返回空串（不干预）"""
        return _PRIORITY_MODE.get(self.mode_priority, '')

    def as_dict(self) -> dict:
        d = asdict(self)
        d.update({'max_walk_min': self.max_walk_min,
                  'max_bike_min': self.max_bike_min,
                  'buffer_delta': self.buffer_delta,
                  'stay_factor': self.stay_factor,
                  'preferred_mode': self.preferred_mode})
        return d

    def describe(self) -> str:
        bits = [f'体力 {self.effort}', f'节奏 {self.pace}']
        if self.identity:
            bits.insert(0, self.identity)
        if self.budget and self.budget != '不设':
            bits.append(f'预算 {self.budget}')
        if not self.has_car:
            bits.append('无车')
        if self.mode_priority and self.mode_priority != '最快':
            bits.append(self.mode_priority)
        if self.day_start:
            bits.append(f'{self.day_start} 起')
        return ' · '.join(bits)


@dataclass
class ProfileStore:
    """档案存档：数据目录下的 profiles.json（多档案 + 当前选中）"""
    data_dir: Path
    _cache: Optional[dict] = field(default=None, repr=False)

    FILE = 'profiles.json'

    # ---------- 读写 ----------
    @property
    def path(self) -> Path:
        return Path(self.data_dir) / self.FILE

    def _load(self) -> dict:
        if self._cache is not None:
            return self._cache
        data: dict = {}
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding='utf-8')) or {}
        except Exception:                          # noqa: BLE001 坏文件不应拖垮应用
            data = {}
        profiles = data.get('profiles')
        if not isinstance(profiles, dict) or not profiles:
            profiles = {DEFAULT_NAME: asdict(Profile(name=DEFAULT_NAME))}
        active = data.get('active') or DEFAULT_NAME
        if active not in profiles:
            active = next(iter(profiles))
        self._cache = {'active': active, 'profiles': profiles}
        return self._cache

    def _flush(self) -> None:
        data = self._load()
        try:
            Path(self.data_dir).mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                 encoding='utf-8')
        except Exception:                          # noqa: BLE001 落盘失败不影响运行
            pass

    # ---------- 查询 ----------
    def list(self) -> list[Profile]:
        data = self._load()
        out = []
        for name, raw in data['profiles'].items():
            f = {k: v for k, v in (raw or {}).items()
                 if k in ('name', 'identity', 'effort', 'pace', 'budget', 'wake',
                          'has_car', 'mode_priority', 'note')}
            f['name'] = name
            out.append(Profile(**f))
        return out

    def active_name(self) -> str:
        return self._load()['active']

    def active(self) -> Profile:
        name = self.active_name()
        for p in self.list():
            if p.name == name:
                return p
        return Profile(name=DEFAULT_NAME)

    # ---------- 写 ----------
    def save(self, profile: Profile) -> Profile:
        data = self._load()
        profile.name = (profile.name or DEFAULT_NAME).strip() or DEFAULT_NAME
        data['profiles'][profile.name] = asdict(profile)
        data['active'] = profile.name
        self._flush()
        return profile

    def set_active(self, name: str) -> Profile:
        data = self._load()
        if name in data['profiles']:
            data['active'] = name
            self._flush()
        return self.active()

    def delete(self, name: str) -> bool:
        data = self._load()
        if name not in data['profiles']:
            return False
        data['profiles'].pop(name, None)
        if not data['profiles']:
            data['profiles'][DEFAULT_NAME] = asdict(Profile(name=DEFAULT_NAME))
        if data['active'] not in data['profiles']:
            data['active'] = next(iter(data['profiles']))
        self._flush()
        return True
