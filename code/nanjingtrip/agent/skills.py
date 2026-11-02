# -*- coding: utf-8 -*-
"""Agent 能力目录。

能力写在 `skills/*.md` 的能力卡里（触发条件、输入、输出、边界、实现位置），
本模块只负责读卡、做形状校验、渲染成给模型看的紧凑清单。

约定：能力卡只做**声明与路由**，真正干活的是既有模块里的确定性代码；
`impl` 字段写 `模块:符号`，测试会校验它确实存在且可调用。
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

CARDS_DIR = Path(__file__).with_name('skills')
AGENT_DOC = Path(__file__).with_name('AGENT.md')

#: 每张卡必须声明的前置字段（缺一个就算卡写坏了，测试会拦下）
REQUIRED_FIELDS = ('name', 'title', 'when_to_use', 'inputs', 'outputs', 'limits')
#: 这些字段允许写成多行列表
LIST_FIELDS = ('when_to_use', 'inputs', 'outputs', 'limits')


@dataclass
class SkillCard:
    """一项 Agent 能力的声明"""

    name: str
    title: str
    when_to_use: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    limits: list[str] = field(default_factory=list)
    impl: str = ''
    needs_network: bool = False
    fallback: str = ''
    body: str = ''
    path: str = ''

    def resolve_impl(self) -> Any:
        """把 `模块:符号` 解析成真实对象；解析不了直接抛错，避免卡与代码脱节"""
        if ':' not in self.impl:
            raise ValueError(f'能力卡 {self.name} 的 impl 需要写成 模块:符号')
        module_name, symbol = self.impl.split(':', 1)
        obj: Any = importlib.import_module(module_name)
        for part in symbol.split('.'):
            obj = getattr(obj, part)
        return obj

    def as_dict(self) -> dict:
        return {'name': self.name, 'title': self.title,
                'when_to_use': list(self.when_to_use),
                'inputs': list(self.inputs), 'outputs': list(self.outputs),
                'limits': list(self.limits), 'impl': self.impl,
                'needs_network': self.needs_network,
                'fallback': self.fallback}


def _parse_front_matter(text: str) -> tuple[dict, str]:
    """解析极简 YAML 头（只支持 `key: 值` 与 `key:` + `- 列表项`），返回 (字段, 正文)"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        raise ValueError('能力卡缺少 --- 开头的头部')
    fields: dict[str, Any] = {}
    key: Optional[str] = None
    end = len(lines)
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == '---':
            end = i + 1
            break
        raw = line.rstrip()
        if not raw.strip():
            continue
        if raw.lstrip().startswith('- ') and key:
            fields.setdefault(key, [])
            if isinstance(fields[key], list):
                fields[key].append(raw.lstrip()[2:].strip())
            continue
        if ':' in raw:
            key, val = raw.split(':', 1)
            key, val = key.strip(), val.strip()
            fields[key] = val if val else []
            continue
        raise ValueError(f'能力卡头部有不认识的行：{raw!r}')
    return fields, '\n'.join(lines[end:]).strip()


def _as_bool(value) -> bool:
    """头部里的布尔值（true/false/yes/no）转真值；空值算 False"""
    return str(value or '').strip().lower() in ('true', 'yes', '1')


def _to_card(fields: dict, body: str, path: Path) -> SkillCard:
    missing = [k for k in REQUIRED_FIELDS if not fields.get(k)]
    if missing:
        raise ValueError(f'{path.name} 缺少字段：{"、".join(missing)}')
    card = SkillCard(name=str(fields['name']), title=str(fields['title']),
                     impl=str(fields.get('impl') or ''),
                     needs_network=_as_bool(fields.get('needs_network')),
                     fallback=str(fields.get('fallback') or ''),
                     body=body, path=str(path))
    for key in LIST_FIELDS:
        val = fields.get(key) or []
        setattr(card, key, [str(x) for x in val] if isinstance(val, list) else [str(val)])
    return card


def load_cards(directory: Optional[Path] = None) -> list[SkillCard]:
    """读出全部能力卡（按文件名排序，保证清单顺序稳定）"""
    folder = Path(directory or CARDS_DIR)
    cards: list[SkillCard] = []
    for path in sorted(folder.glob('*.md')):
        fields, body = _parse_front_matter(path.read_text(encoding='utf-8'))
        cards.append(_to_card(fields, body, path))
    return cards


def catalog_text(directory: Optional[Path] = None, limit: int = 1200) -> str:
    """渲染成给模型看的紧凑清单（一行一能力，超出预算就截断）"""
    lines = []
    for card in load_cards(directory):
        use = card.when_to_use[0] if card.when_to_use else ''
        avoid = card.limits[0] if card.limits else ''
        line = f'- {card.name}（{card.title}）：用于 {use}'
        if avoid:
            line += f'；注意 {avoid}'
        lines.append(line)
    text = '\n'.join(lines)
    return text[:limit]


def prompt_block(directory: Optional[Path] = None) -> str:
    """给模型的完整上下文：行为规矩（AGENT.md）+ 能力清单"""
    rules = AGENT_DOC.read_text(encoding='utf-8') if AGENT_DOC.exists() else ''
    return f'{rules}\n\n## 你能调用的能力\n{catalog_text(directory)}\n'


def api_payload(directory: Optional[Path] = None) -> dict:
    """给界面/报告用的能力目录（不含实现细节之外的运行态数据）"""
    cards = load_cards(directory)
    return {'ok': True, 'count': len(cards),
            'skills': [c.as_dict() for c in cards],
            'agent_doc': AGENT_DOC.name if AGENT_DOC.exists() else ''}
