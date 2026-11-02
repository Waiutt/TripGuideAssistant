# -*- coding: utf-8 -*-
"""在线大模型适配器：国内 LLM 的 OpenAI 兼容接口（DeepSeek / 通义千问 / 智谱等）。

特性：
  1) 强制 JSON 输出（response_format=json_object），降低解析失败率；
  2) 失败自动重试（默认 2 次），超过次数抛出 LLMError 由上层降级；
  3) 温度 0，保证同样输入结果稳定（答辩演示可复现）。
"""
from __future__ import annotations

import json
import re
from typing import Optional

import requests


class LLMError(RuntimeError):
    """LLM 调用失败（超时、鉴权、返回不可解析）"""


SYSTEM_PROMPT = """你是「行程规划助手」的信息抽取模块。
把用户的一句话解析为 JSON，字段如下：
- date: 日期，格式 YYYY-MM-DD；未提及则为 null
- start_hint: 期望开始时刻，格式 HH:MM；未提及则为 null
- mention: 地点表述（用户原话中的地点词，不要自行补充）；未提及则为 ""
- item: 事项名称（如"看展""取报告""午餐"）
- duration_min: 预估耗时（分钟，整数）；未提及则为 0
- mode_hint: 交通方式，取值 driving/transit/walking；未提及则为 null
- category: 事项类别，取值 医疗/餐饮/展览/办事/其他
- confidence: 解析置信度 0~1
- missing: 缺失且必要的字段名列表，如 ["地点","耗时"]
只输出 JSON，不要输出任何解释或多余文本。"""

CHAT_SYSTEM = """你是「NanjingTrip 桌面助手」，一款常驻桌面的中文 AI 助手，宿主程序是{system_hint}的行程规划应用。

你的职责：
1) 通用问答：回答用户关于常识、学习、写作、代码、生活等各类问题，简洁准确，必要时分点；
2) 行程参谋：当用户问及自己的日程安排时，依据对话中提供的「本地行程」信息回答，不得编造未提供的安排；
3) 规划引导：当用户想新增行程时，提示其在「行程录入」里用一句话描述（例如"{sample}"），由系统解析、排程并入库。
4) 营业信息：涉及闭馆日、开放时间、预约要求时，只能依据「本地行程」里给出的信息回答；没有就说不确定，不要凭印象编造。

回答要求：中文作答；先给结论再给理由；不确定就说明不确定；不要输出 Markdown 表格；长度控制在 300 字以内。"""


def chat_system_prompt(city: str = '南京', sample: str = '') -> str:
    """按当前城市生成问答系统提示（城市名与示例话术都来自城市包）"""
    from ..cities import get_city
    pack = get_city(city)
    return CHAT_SYSTEM.format(
        system_hint=f'{pack.name}市内',
        sample=sample or pack.sample or '明天上午九点去博物馆看展，大概2小时')



def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\s*', '', text)
        text = re.sub(r'```\s*$', '', text)
    return text.strip()


class OpenAICompatLLMAdapter:
    """OpenAI 兼容的 Chat Completions 适配器"""

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: int = 20, retries: int = 2, city: str = '南京',
                 reasoning_effort: str = ''):
        self.base_url = (base_url or '').rstrip('/')
        self.api_key = api_key or ''
        self.model = model or 'deepseek-flash'
        self.timeout = timeout
        self.retries = max(0, retries)
        self.city = city or '南京'
        # 推理型模型（如 deepseek-flash）默认会先写一长串思考再给答案。
        # 本项目的输出是"温度 0 + 固定 JSON 字段"，思考纯属额外开销：
        # 实测同一解析任务 3576ms → 1284ms。设成 'none' 即关闭；
        # 留空表示不下发该参数（其它厂商的兼容接口未必认这个字段）。
        self.reasoning_effort = reasoning_effort or ''

    def name(self) -> str:
        return f'{self.model}'

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        if not self.api_key:
            raise LLMError('未配置大模型 API Key')
        url = f'{self.base_url}/chat/completions'
        headers = {'Authorization': f'Bearer {self.api_key}',
                   'Content-Type': 'application/json'}
        payload = {
            'model': self.model,
            'temperature': 0,
            'messages': [
                {'role': 'system', 'content': system or SYSTEM_PROMPT},
                {'role': 'user', 'content': user},
            ],
            'response_format': {'type': 'json_object'},
        }
        if self.reasoning_effort:
            payload['reasoning_effort'] = self.reasoning_effort
        last_err: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                resp = requests.post(url, headers=headers, json=payload,
                                     timeout=self.timeout)
                if resp.status_code != 200:
                    raise LLMError(f'HTTP {resp.status_code}: {resp.text[:200]}')
                body = resp.json()
                content = body['choices'][0]['message']['content']
                data = json.loads(_strip_code_fence(content))
                if not isinstance(data, dict):
                    raise LLMError('返回不是 JSON 对象')
                return data
            except Exception as exc:      # noqa: BLE001 失败统一重试
                last_err = exc
                payload['temperature'] = 0
                # 对方可能不认 reasoning_effort（其它厂商的兼容接口）：去掉后重试，
                # 不能因为一个可选参数把整条解析链拖进规则兜底。
                if 'reasoning_effort' in payload:
                    payload.pop('reasoning_effort', None)
        raise LLMError(f'大模型调用失败：{last_err}')

    # ---------------- 通用问答（桌面助手） ----------------
    def chat(self, messages: list[dict], temperature: float = 0.6) -> str:
        """OpenAI 兼容的多轮问答；失败重试后抛出 LLMError 由上层降级"""
        if not self.api_key:
            raise LLMError('未配置大模型 API Key')
        url = f'{self.base_url}/chat/completions'
        headers = {'Authorization': f'Bearer {self.api_key}',
                   'Content-Type': 'application/json'}
        payload = {
            'model': self.model,
            'temperature': temperature,
            'messages': [{'role': 'system',
                          'content': chat_system_prompt(self.city)}] + list(messages),
        }
        last_err: Optional[Exception] = None
        for _ in range(self.retries + 1):
            try:
                resp = requests.post(url, headers=headers, json=payload,
                                     timeout=max(self.timeout, 30))
                if resp.status_code != 200:
                    raise LLMError(f'HTTP {resp.status_code}: {resp.text[:200]}')
                content = (resp.json()['choices'][0]['message']['content'] or '').strip()
                if not content:
                    raise LLMError('返回内容为空')
                return content
            except Exception as exc:      # noqa: BLE001
                last_err = exc
        raise LLMError(f'大模型问答失败：{last_err}')
