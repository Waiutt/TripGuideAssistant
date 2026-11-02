# -*- coding: utf-8 -*-
"""局域网多端同步的测试（对应报告第 4 章 TC-24-*）。

用真实 HTTP 服务 + requests 模拟"手机端"，验证：
新增/改方式/删除都能落到桌面同一份数据，且 task_id 稳定、revision 递增。
"""
import threading
from datetime import date

import pytest
import requests

from nanjingtrip.app import AppContext
from nanjingtrip.lan_server import LanServer

D = date(2026, 10, 28)
PORT = 8799


@pytest.fixture(scope='module')
def server():
    import tempfile
    ctx = AppContext(tempfile.mkdtemp(prefix='lan_test_'))
    ctx.settings.offline_mode = True
    ctx.apply_settings(ctx.settings)
    srv = LanServer(ctx, port=PORT, host='127.0.0.1')
    srv.start()
    yield ctx, f'http://127.0.0.1:{PORT}'
    srv.stop()
    ctx.close()


# ---------------------------------------------------------------- TC-24-01
def test_health_and_spa_page(server):
    """TC-24-01 服务健康检查 + Web 单页可访问（含 viewport 适配与前端资源）"""
    ctx, base = server
    h = requests.get(f'{base}/api/health', timeout=5).json()
    assert h['ok'] is True and h['version'].startswith('2.0')

    page = requests.get(f'{base}/', timeout=5)
    assert page.status_code == 200
    assert 'width=device-width' in page.text          # 移动端适配
    assert '/app.js' in page.text                     # 引用前端脚本
    js = requests.get(f'{base}/app.js', timeout=5)
    assert js.status_code == 200
    assert '/api/command' in js.text                  # 前端会调用写接口
    css = requests.get(f'{base}/app.css', timeout=5)
    assert css.status_code == 200 and '--accent' in css.text


# ---------------------------------------------------------------- TC-24-02
def test_phone_add_shows_up_on_desktop(server):
    """TC-24-02 手机端新增 → 桌面同一份数据可见，revision 递增"""
    ctx, base = server
    rev0 = ctx.db.revision()

    r = requests.post(f'{base}/api/command', timeout=60, json={
        'action': 'add', 'date': D.isoformat(),
        'text': '上午九点去南京博物院看展，大概2小时'}).json()
    assert r['ok'], r.get('message')
    assert r['rev'] == rev0 + 1

    trip = ctx.trip_svc.day_view(D)                   # 桌面视角
    assert len(trip.tasks) == 1
    assert trip.tasks[0].title == '看展'
    assert ctx.db.revision() == rev0 + 1              # 桌面轮询能感知


# ---------------------------------------------------------------- TC-24-03
def test_task_id_is_stable_across_saves(server):
    """TC-24-03 多次保存后 task_id 不变（否则外部引用会失效）"""
    ctx, base = server
    state = requests.get(f'{base}/api/state?date={D.isoformat()}', timeout=5).json()
    assert state['items'], '前置：应有安排'
    tid = state['items'][0]['task_id']

    # 手机改方式，触发一次完整保存
    r = requests.post(f'{base}/api/command', timeout=60, json={
        'action': 'set_mode', 'date': D.isoformat(),
        'task_id': tid, 'mode': 'bicycling'}).json()
    assert r['ok'], r.get('message')

    state2 = requests.get(f'{base}/api/state?date={D.isoformat()}', timeout=5).json()
    assert state2['items'][0]['task_id'] == tid, '保存后 task_id 必须保持不变'
    assert state2['items'][0]['mode'] == 'bicycling'


# ---------------------------------------------------------------- TC-24-04
def test_phone_delete_removes_on_desktop(server):
    """TC-24-04 手机端删除 → 桌面数据同步删除"""
    ctx, base = server
    state = requests.get(f'{base}/api/state?date={D.isoformat()}', timeout=5).json()
    tid = state['items'][0]['task_id']

    r = requests.post(f'{base}/api/command', timeout=60, json={
        'action': 'delete', 'date': D.isoformat(), 'task_id': tid}).json()
    assert r['ok'], r.get('message')
    assert ctx.trip_svc.day_view(D).tasks == []


# ---------------------------------------------------------------- TC-24-05
def test_state_reports_changed_by_revision(server):
    """TC-24-05 客户端带旧 revision 查询 → changed=True，便于自动刷新"""
    ctx, base = server
    requests.post(f'{base}/api/command', timeout=60, json={
        'action': 'add', 'date': D.isoformat(),
        'text': '中午去新街口吃饭，大约1小时'})
    rev = ctx.db.revision()

    stale = requests.get(f'{base}/api/state?date={D.isoformat()}&rev={rev - 1}',
                         timeout=5).json()
    assert stale['changed'] is True
    fresh = requests.get(f'{base}/api/state?date={D.isoformat()}&rev={rev}',
                         timeout=5).json()
    assert fresh['changed'] is False


# ---------------------------------------------------------------- TC-24-06
def test_invalid_requests_are_rejected(server):
    """TC-24-06 非法输入被拒绝且不抛 500"""
    ctx, base = server
    bad_action = requests.post(f'{base}/api/command', timeout=5,
                               json={'action': 'hack'}).json()
    assert bad_action['ok'] is False and '未知操作' in bad_action['message']

    bad_date = requests.get(f'{base}/api/state?date=2026-13-99', timeout=5)
    assert bad_date.status_code == 400

    empty_add = requests.post(f'{base}/api/command', timeout=5,
                              json={'action': 'add', 'text': ''}).json()
    assert empty_add['ok'] is False


# ---------------------------------------------------------------- TC-24-07
def test_phone_move_time(server):
    """TC-24-07 手机端改开始时间：落到桌面同一份数据并锁定该项（BR-02 例外）"""
    ctx, base = server
    requests.post(f'{base}/api/command', timeout=60, json={
        'action': 'add', 'date': D.isoformat(),
        'text': '下午去鼓楼医院取报告，半小时'})
    items = requests.get(f'{base}/api/state?date={D.isoformat()}',
                         timeout=5).json()['items']
    tid = next(it['task_id'] for it in items if it['title'] == '取报告')

    r = requests.post(f'{base}/api/command', timeout=60, json={
        'action': 'move', 'date': D.isoformat(),
        'task_id': tid, 'start': '16:30'}).json()
    assert r['ok'], r.get('message')
    assert '16:30' in r['message']

    task = next(t for t in ctx.trip_svc.day_view(D).tasks if t.task_id == tid)
    assert task.start_ts.strftime('%H:%M') == '16:30'    # 桌面权威数据已变
    assert task.is_locked is True                        # 用户指定时刻即锁定


# ---------------------------------------------------------------- TC-24-08
def test_phone_move_rejects_bad_time(server):
    """TC-24-08 改时间时非法格式/越界时间被拒绝，且不改动已排数据"""
    ctx, base = server
    items = requests.get(f'{base}/api/state?date={D.isoformat()}',
                         timeout=5).json()['items']
    tid = items[0]['task_id']
    before = next(t for t in ctx.trip_svc.day_view(D).tasks
                  if t.task_id == tid).start_ts

    for bad in ('abc', '', '25:99', '12-30'):
        r = requests.post(f'{base}/api/command', timeout=5, json={
            'action': 'move', 'date': D.isoformat(),
            'task_id': tid, 'start': bad}).json()
        assert r['ok'] is False, f'非法时间 {bad!r} 应被拒绝'

    after = next(t for t in ctx.trip_svc.day_view(D).tasks
                 if t.task_id == tid).start_ts
    assert after == before


# ---------------------------------------------------------------- TC-24-09
def test_state_exposes_city_and_notices(server):
    """TC-24-09 状态里带城市（含文化称谓）、住宿与营业提示，供手机端展示"""
    ctx, base = server
    st = requests.get(f'{base}/api/state?date={D.isoformat()}', timeout=5).json()
    assert st['city'] == '南京' and st['city_label'] == '金陵'
    assert st['lodging'] == '' or isinstance(st['lodging'], str)
    assert isinstance(st['notices'], list)
    # 大改为 Web 之后，提示与冲突都改为结构化对象（前端要用 kind/suggestions）
    assert all(isinstance(x, dict) and 'detail' in x for x in st['notices'])
    assert isinstance(st['conflicts'], list) and all(
        isinstance(x, dict) and 'detail' in x for x in st['conflicts'])


# ---------------------------------------------------------------- TC-24-10
def test_spa_has_time_control(server):
    """TC-24-10 前端提供改时间入口与对应动作名（前后端接口不脱节）"""
    _, base = server
    js = requests.get(f'{base}/app.js', timeout=5).text
    assert "'move'" in js                 # 会调用 move 动作
    assert '改时间' in js                # 卡片上有改时间入口
    assert 'notices' in js                # 会渲染营业提示
    page = requests.get(f'{base}/', timeout=5).text
    assert 'previewSheet' in page         # 解析结果卡（先预览再确认）

