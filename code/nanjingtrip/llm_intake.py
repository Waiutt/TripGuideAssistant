"""LLM 理解层：把"读人话"交给 DeepSeek 的原生 JSON 模式，把"做决定"留在代码里。

分工（对应架构决策）
------------------
* **模型只产"候选 + 置信度"**：地点、时刻、时长、条件分支、日期、人数、兴趣、酒店是否未定；
* **代码负责校验与裁决**：
    - 地点必须能被 `name_alike()` 命中（否则降级为"待补地点"）；
    - 置信度低于阈值的一律不采信（宁可用规则，也不赌）；
    - 追问与否、问几次由 `consult.py` 的状态机决定，模型只负责**把问题说顺**；
    - 数字（时长/通行/营业时间）永远来自证据表与引擎，模型给的值只作参考。
* 任何异常都返回空：**降级优于编造**。

模型能力不足时（离线、无 Key、配额用尽）本模块整体"透明失效"，
调用方拿到 {} 就走原来的规则链路——所以它只做增强，不做依赖。
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

log = logging.getLogger(__name__)

#: 采信阈值（低于此值视为"模型没把握"）
MIN_CONFIDENCE = 0.6

_PARSE_SCHEMA = {
    'places': [{'name': 'string', 'duration_min': 'int|null',
                'start': 'HH:MM|null', 'deadline': 'HH:MM|null'}],
    'conditionals': [{'trigger': 'string', 'places': ['string']}],
    'return_by': 'HH:MM|null',
    'origin': 'string|null',
    'confidence': 'float 0..1',
}

_PARSE_SYSTEM = (
    '你是行程解析器。只从用户这句话里抽取**已经明确说出**的信息，绝不补全、绝不猜测。\n'
    '输出 JSON，字段如下（没有就留空数组/null）：\n'
    '{"places":[{"name":"地点原话","duration_min":分钟或null,"start":"HH:MM或null",'
    '"deadline":"HH:MM或null"}],'
    '"conditionals":[{"trigger":"下雨/晴天等","places":["备选地点"]}],'
    '"return_by":"HH:MM或null","origin":"出发地或null","confidence":0到1}\n'
    '规则：\n'
    '1) 条件句（"如果下雨就改去X"）里的地点放进 conditionals，不要放进 places；\n'
    '2) "晚上9点的高铁"→ return_by="21:00"；"我从南京站出发"→ origin="南京站"；\n'
    '3) 用户没说的时长不要编；4) confidence 表示你对这次抽取的整体把握。'
)

_SLOT_SCHEMA = {
    'dates': {'start': 'YYYY-MM-DD|null', 'end': 'YYYY-MM-DD|null', 'confidence': 'float'},
    'party': {'value': 'int|null', 'confidence': 'float'},
    'pace': {'value': 'slow|normal|fast|null', 'confidence': 'float'},
    'wake': {'value': 'HH:MM|null', 'confidence': 'float'},
    'interests': {'value': ['历史人文|博物馆|自然风光|美食街区|亲子'], 'confidence': 'float'},
    'hotel_unknown': {'value': 'bool', 'confidence': 'float'},
}

_SLOT_SYSTEM = (
    '你是行程顾问的"槽位抽取器"。从用户这句话里读出行程参数，只输出 JSON：\n'
    '{"dates":{"start":"YYYY-MM-DD|null","end":"YYYY-MM-DD|null","confidence":0~1},'
    '"party":{"value":人数或null,"confidence":0~1},'
    '"pace":{"value":"slow|normal|fast|null","confidence":0~1},'
    '"wake":{"value":"HH:MM|null","confidence":0~1},'
    '"interests":{"value":["历史人文","博物馆","自然风光","美食街区","亲子"],"confidence":0~1},'
    '"hotel_unknown":{"value":true或false,"confidence":0~1}}\n'
    '今天是 {today}（{weekday}）。规则：\n'
    '1) 只认用户说过的；没说就给 null/空数组，并把 confidence 设为 0；\n'
    '2) "懒/起得晚/不想早起"→ pace=slow 且 wake="10:00"；"特种兵/赶时间"→ pace=fast；\n'
    '3) 单日（"9月9号""明天""周六"）→ start=end=那一天；\n'
    '4) 不要输出任何解释文字。'
)


def _llm(degrade) -> Optional[object]:
    """拿到可用的 LLM 适配器；不可用返回 None（调用方据此走规则）"""
    try:
        if not degrade.llm_ready():
            return None
        return degrade.llm()
    except Exception:                    # noqa: BLE001
        return None


def _capabilities(limit: int = 900) -> str:
    """能力清单摘要；能力卡读不出来就当没有，不影响解析"""
    try:
        from .agent.skills import catalog_text
        return catalog_text(limit=limit)
    except Exception:                    # noqa: BLE001 能力卡缺失不能拖垮解析
        return ''


def _ask(llm, system: str, user: str) -> dict:
    if llm is None:
        return {}
    try:
        got = llm.complete_json(system, user, {})
    except Exception as exc:             # noqa: BLE001 模型失败一律降级
        log.warning('结构化解析失败：%s', exc)
        return {}
    return got if isinstance(got, dict) else {}


# ---------------------------------------------------------------- 意图解析
def structured_parse(text: str, today: date, degrade) -> dict:
    """返回 {'places': [...], 'conditionals': [...], 'return_by', 'origin', 'confidence'}

    只做形状校验，不做业务裁决；地点是否真实存在由调用方用 `name_alike` 校验。
    """
    llm = _llm(degrade)
    if llm is None or not (text or '').strip():
        return {}
    raw = _ask(llm, _PARSE_SYSTEM,
               f'今天是 {today.isoformat()}。用户说：{text}')
    if not raw:
        return {}
    places = []
    for p in raw.get('places') or []:
        if not isinstance(p, dict):
            continue
        name = str(p.get('name') or '').strip()
        if not name:
            continue
        dur = p.get('duration_min')
        try:
            dur = int(dur) if dur is not None else None
        except (TypeError, ValueError):
            dur = None
        places.append({'name': name, 'duration_min': dur,
                       'start': str(p.get('start') or '') or None,
                       'deadline': str(p.get('deadline') or '') or None})
    conds = []
    for c in raw.get('conditionals') or []:
        if not isinstance(c, dict):
            continue
        names = [str(x).strip() for x in (c.get('places') or []) if str(x).strip()]
        if names:
            conds.append({'trigger': str(c.get('trigger') or '条件').strip(),
                          'places': names})
    try:
        conf = float(raw.get('confidence'))
    except (TypeError, ValueError):
        conf = 0.0
    return {'places': places, 'conditionals': conds,
            'return_by': str(raw.get('return_by') or '') or '',
            'origin': str(raw.get('origin') or '') or '',
            'confidence': max(0.0, min(1.0, conf))}


# ---------------------------------------------------------------- 槽位抽取
def slot_extract(text: str, today: date, degrade,
                 min_conf: float = MIN_CONFIDENCE) -> dict:
    """读出行程参数；低于阈值的候选值直接丢弃（宁可问，不赌）"""
    llm = _llm(degrade)
    if llm is None or not (text or '').strip():
        return {}
    weekday = '一二三四五六日'[today.weekday()]
    # 注意：模板里含 JSON 大括号，不能用 str.format（会把 JSON 当占位符）
    sys_text = (_SLOT_SYSTEM.replace('{today}', today.isoformat())
                .replace('{weekday}', weekday))
    # 告诉模型"本应用能做什么"，让它别把"应用具备的能力"当成"用户说过的参数"
    caps = _capabilities()
    if caps:
        sys_text += ('\n\n本应用具备以下能力（只用于理解用户意图；'
                     '不要因为"能做得更好"就把用户没说的参数填成有值）：\n' + caps)
    raw = _ask(llm, sys_text, text)
    if not raw:
        return {}
    out: dict = {}

    def conf_of(node) -> float:
        try:
            return float((node or {}).get('confidence') or 0)
        except (TypeError, ValueError, AttributeError):
            return 0.0

    d = raw.get('dates')
    if isinstance(d, dict) and conf_of(d) >= min_conf:
        start, end = str(d.get('start') or ''), str(d.get('end') or '')
        if start:
            out['start'] = start
            out['end'] = end or start
    for key, node_key in (('party', 'party'), ('pace', 'pace'), ('wake', 'wake')):
        node = raw.get(node_key)
        if isinstance(node, dict) and conf_of(node) >= min_conf:
            val = node.get('value')
            if val not in (None, '', []):
                out[key] = int(val) if key == 'party' else str(val)
    it = raw.get('interests')
    if isinstance(it, dict) and conf_of(it) >= min_conf:
        vals = [str(x) for x in (it.get('value') or []) if str(x).strip()]
        if vals:
            out['interests'] = vals
    hu = raw.get('hotel_unknown')
    if isinstance(hu, dict) and conf_of(hu) >= min_conf and hu.get('value'):
        out['hotel'] = 'recommend'
    return out


# ---------------------------------------------------------------- 追问措辞
_ASK_TEMPLATES = {
    'dates': '打算哪天去？一句话就行（例：9月9号 / 明天 / 9月10号-9月16号）',
    'interests': '想偏哪一挂？历史人文、博物馆、自然风光还是逛吃？不说我就按均衡排。',
    'party': '几个人一起？这会影响预算和房型。',
}


def phrase_question(key: str, context: str, degrade,
                    options: Optional[list[str]] = None) -> str:
    """把"要问什么"说得自然一点；模型不可用就用固定文案（不影响流程）"""
    fallback = _ASK_TEMPLATES.get(key, '再补充一点信息？')
    llm = _llm(degrade)
    if llm is None:
        return fallback
    user = (f'已知：{context or "（暂无）"}\n需要向用户确认：{key}'
            + (f"\n可选：{'、'.join(options)}" if options else ''))
    try:
        got = llm.complete_json(
            '你是行程顾问。请用一句口语化的中文问用户，最多 30 字，'
            '不要寒暄、不要解释、不要列出 JSON。输出 {"ask":"..."}', user, {})
        ask = str((got or {}).get('ask') or '').strip()
        return ask or fallback
    except Exception:                    # noqa: BLE001
        return fallback
