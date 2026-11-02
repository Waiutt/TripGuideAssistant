# -*- coding: utf-8 -*-
"""在线适配器的失败路径与降级测试（对应报告第 4 章 TC-28-*）。

**为什么要单列这一组**：真实地图与 LLM 接口受配额、限流与网络抖动影响，不适合直接
进自动化测试；但**失败路径**完全可以用假的 requests 模拟——超时、鉴权失败、限流
错误码、返回非 JSON。把这些钉住之后，"在线链路未覆盖"的缺口就只剩"真实响应格式"
一层，而这层由人工联调覆盖。

对应用例：
  TC-28-01~05  LLM 适配器：无 Key / 超时重试 / 401 / 非 JSON / 问答失败
  TC-28-06~07  上层降级：解析退回规则、问答返回 error 而不崩
  TC-28-08~10  地图适配器：无 Key / 限流错误码 / 超时 → TransitService 返回 None
  TC-28-11     两路都挂时仍能完成排程（通行时长按 30 分钟保守值）
"""
from datetime import date

import pytest

from nanjingtrip.adapters import amap as amap_mod
from nanjingtrip.adapters import llm_openai as llm_mod
from nanjingtrip.adapters.amap import AmapAdapter, MapError
from nanjingtrip.adapters.llm_openai import LLMError, OpenAICompatLLMAdapter
from nanjingtrip.app import AppContext
from nanjingtrip.models import Poi, TransitMode

D = date(2026, 10, 28)


# ======================================================================
# 测试替身
# ======================================================================
class _Resp:
    """假的 HTTP 响应"""

    def __init__(self, status=200, payload=None, text='', ctype='application/json'):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.headers = {'Content-Type': ctype}
        self.content = b'\x89PNG\r\n' if 'image' in ctype else b'{}'

    def json(self):
        if self._payload is None:
            raise ValueError('响应不是 JSON')
        return self._payload


class _RaisingRequests:
    """模拟网络异常（超时 / 连接失败），并记录被调用次数"""

    def __init__(self, exc=None):
        self.exc = exc or TimeoutError('timed out')
        self.calls = 0

    def _boom(self, *args, **kwargs):
        self.calls += 1
        raise self.exc

    get = post = _boom


class _StaticRequests:
    """固定返回某个响应（用于 401 / 限流 / 非 JSON）"""

    def __init__(self, resp):
        self.resp = resp
        self.calls = 0

    def _ret(self, *args, **kwargs):
        self.calls += 1
        return self.resp

    get = post = _ret


@pytest.fixture()
def online_ctx(tmp_path):
    """两路都在线（有 Key、非强制离线）的应用上下文，但不发真实请求"""
    c = AppContext(tmp_path)
    c.settings.offline_mode = False
    c.settings.llm_api_key = 'fake-llm-key'
    c.settings.map_api_key = 'fake-map-key'
    c.apply_settings(c.settings)
    assert c.degrade.llm_ready() and c.degrade.map_ready()
    yield c
    c.close()


# ======================================================================
# LLM 适配器
# ======================================================================
# ---------------------------------------------------------------- TC-28-01
def test_llm_without_key_raises():
    """TC-28-01 没有 Key 时直接抛 LLMError，不发请求"""
    ad = OpenAICompatLLMAdapter('https://example.invalid/v1', '', 'deepseek-chat')
    with pytest.raises(LLMError, match='未配置大模型 API Key'):
        ad.complete_json('sys', 'user', {})


# ---------------------------------------------------------------- TC-28-02
def test_llm_timeout_retries_then_raises(monkeypatch):
    """TC-28-02 超时会按 retries 次重试，最终抛 LLMError（不无限等待）"""
    fake = _RaisingRequests()
    monkeypatch.setattr(llm_mod, 'requests', fake)
    ad = OpenAICompatLLMAdapter('https://example.invalid/v1', 'k', 'm', retries=2)
    with pytest.raises(LLMError, match='大模型调用失败'):
        ad.complete_json('sys', 'user', {})
    assert fake.calls == 3, 'retries=2 应总共尝试 3 次'


# ---------------------------------------------------------------- TC-28-03
def test_llm_http_error_raises(monkeypatch):
    """TC-28-03 鉴权/配额失败（HTTP 401）→ LLMError 且带状态码"""
    monkeypatch.setattr(llm_mod, 'requests', _StaticRequests(_Resp(status=401)))
    ad = OpenAICompatLLMAdapter('https://example.invalid/v1', 'k', 'm', retries=0)
    with pytest.raises(LLMError, match='401'):
        ad.complete_json('sys', 'user', {})


# ---------------------------------------------------------------- TC-28-04
def test_llm_non_json_reply_raises(monkeypatch):
    """TC-28-04 模型返回非 JSON 文本 → 包装成 LLMError（上层据此回到规则解析）"""
    resp = _Resp(payload={'choices': [{'message': {'content': '好的，我明白了'}}]})
    monkeypatch.setattr(llm_mod, 'requests', _StaticRequests(resp))
    ad = OpenAICompatLLMAdapter('https://example.invalid/v1', 'k', 'm', retries=0)
    with pytest.raises(LLMError, match='大模型调用失败'):
        ad.complete_json('sys', 'user', {})


# ---------------------------------------------------------------- TC-28-05
def test_llm_chat_empty_reply_raises(monkeypatch):
    """TC-28-05 问答返回空内容 → LLMError（上层据此降级，而不是显示空白）"""
    resp = _Resp(payload={'choices': [{'message': {'content': ''}}]})
    monkeypatch.setattr(llm_mod, 'requests', _StaticRequests(resp))
    ad = OpenAICompatLLMAdapter('https://example.invalid/v1', 'k', 'm', retries=0)
    with pytest.raises(LLMError, match='返回内容为空'):
        ad.chat([{'role': 'user', 'content': '你好'}])


# ======================================================================
# 上层降级
# ======================================================================
# ---------------------------------------------------------------- TC-28-06
def test_parse_falls_back_to_rules_when_llm_fails(online_ctx, monkeypatch):
    """TC-28-06 在线但模型不可用：解析自动退回规则，仍能得到正确意图"""
    monkeypatch.setattr(llm_mod, 'requests', _RaisingRequests())
    intent = online_ctx.parse_svc.parse('明天下午三点去南京博物院看展，大概2小时', D)
    assert intent.mention == '南京博物院'
    assert intent.duration_min == 120
    assert intent.start_hint.strftime('%H:%M') == '15:00'
    assert intent.date == date(2026, 10, 29)


# ---------------------------------------------------------------- TC-28-07
def test_assistant_returns_error_reply_when_llm_fails(online_ctx, monkeypatch):
    """TC-28-07 在线但问答失败：返回 source='error' 的答复并带离线兜底，界面不空白"""
    monkeypatch.setattr(llm_mod, 'requests', _RaisingRequests())
    reply = online_ctx.assistant_svc.ask('我今天有什么安排？',
                                         plan_date=D, context='10月28日暂无安排。')
    assert reply.ok is False and reply.source == 'error'
    assert reply.text.strip(), '必须仍有可读文本'
    assert '离线' in reply.text


# ======================================================================
# 地图适配器
# ======================================================================
# ---------------------------------------------------------------- TC-28-08
def test_map_without_key_raises():
    """TC-28-08 没有 Key 时抛 MapError，不发请求"""
    ad = AmapAdapter('')
    with pytest.raises(MapError, match='未配置高德地图 API Key'):
        ad.search_poi('南京博物院')
    with pytest.raises(MapError, match='未配置高德地图 API Key'):
        ad.route((118.7, 32.0), (118.8, 32.1), 'driving')


# ---------------------------------------------------------------- TC-28-09
def test_map_quota_error_raises(monkeypatch):
    """TC-28-09 高德返回 status!=1（限流/配额）→ MapError 且带原始 info"""
    resp = _Resp(payload={'status': '0', 'info': 'DAILY_QUERY_OVER_LIMIT'})
    monkeypatch.setattr(amap_mod, 'requests', _StaticRequests(resp))
    ad = AmapAdapter('fake-key')
    with pytest.raises(MapError, match='DAILY_QUERY_OVER_LIMIT'):
        ad.search_poi('南京博物院')


# ---------------------------------------------------------------- TC-28-10
def test_map_timeout_degrades_to_offline_estimate(online_ctx, monkeypatch):
    """TC-28-10 地图超时：适配器抛 MapError，通行时长退回离线距离估算并标注来源

    宁可给"离线估算（地图不可用）"也不编造真实路况；缓存里带"离线"字样，
    等地图恢复后会按 D-12 的规则重新取真实值。
    """
    monkeypatch.setattr(amap_mod, 'requests', _RaisingRequests())
    a = Poi(name='A', district='玄武区', lng=118.82, lat=32.04,
            provider='amap', provider_poi_id='A', city='南京')
    b = Poi(name='B', district='秦淮区', lng=118.78, lat=32.04,
            provider='amap', provider_poi_id='B', city='南京')
    online_ctx.trip_repo.ensure_poi(a)          # 拿到 poi_id 才会写通行段缓存
    online_ctx.trip_repo.ensure_poi(b)
    minutes = online_ctx.transit_svc.estimate(a, b, TransitMode.DRIVING)
    assert minutes is not None and minutes > 0, '应给出离线估算值而不是 None'
    cached = online_ctx.transit_svc.cache.get_full(a.poi_id, b.poi_id, 'driving')
    assert cached is not None and '离线' in str(cached['source'])   # 来源可追溯


# ---------------------------------------------------------------- TC-28-11
def test_poi_search_falls_back_to_city_pack(online_ctx, monkeypatch):
    """TC-28-11 在线地图挂掉时，地点仍能由城市包解析（否则在线模式不如离线可用）"""
    monkeypatch.setattr(amap_mod, 'requests', _RaisingRequests())
    out = online_ctx.trip_svc.preview_text('上午九点去南京博物院看展，大概2小时', D)
    assert out.ok, out.error
    assert out.draft.poi.name == '南京博物院'
    assert out.draft.poi.closed_days == ('周一',)     # 营业信息也补齐了


# ---------------------------------------------------------------- TC-28-12
def test_online_but_all_services_down_still_schedules(online_ctx, monkeypatch):
    """TC-28-12 两路都挂时：解析退回规则、地点退回城市包、通行退回估算，排程仍成功"""
    monkeypatch.setattr(llm_mod, 'requests', _RaisingRequests())
    monkeypatch.setattr(amap_mod, 'requests', _RaisingRequests())

    out = online_ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D)
    assert out.ok, out.error
    saved = online_ctx.trip_svc.day_view(D).tasks
    assert saved and saved[0].poi is not None             # POI 由城市包兜底并入库
    assert saved[0].poi.name == '南京博物院'
    item = out.result.ordered()[0]
    assert item.task.title == '看展'
    assert item.start_min == 9 * 60                      # 指定时刻即锁定
