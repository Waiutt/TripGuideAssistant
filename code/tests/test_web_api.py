# -*- coding: utf-8 -*-
"""Web 接口层测试（对应报告第 4 章 TC-29-*）。

大改为 Web 之后，`web_api` 成为新的接口边界：它把 HTTP 请求翻译成应用服务调用。
这一层最容易出的两类问题——
  1) **协议错配**：前端要的字段没给、给成了内部对象（不可序列化）；
  2) **动作漏接**：页面能点，但后端没有对应 action（旧版就漏过 `move`）。
因此这里覆盖：预览不落库、确认入库、字段完整性、非法输入、批量与撤销、住宿、
版本回滚、切城市、AI 问答降级。
"""
from datetime import date

import pytest

from nanjingtrip import web_api
from nanjingtrip.app import AppContext

D = date(2026, 11, 3)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


# ---------------------------------------------------------------- TC-29-01
def test_health_payload(ctx):
    """TC-29-01 健康检查带版本与模式，供前端判断连接状态"""
    h = web_api.health_payload(ctx)
    assert h['ok'] is True
    assert h['version'] == '2.0-web'
    assert '离线' in h['mode'] or '在线' in h['mode']
    assert h['city'] == '南京'


# ---------------------------------------------------------------- TC-29-02
def test_options_payload(ctx):
    """TC-29-02 选项接口给城市列表、出行方式与时段，前端据此渲染控件"""
    o = web_api.options_payload(ctx, D)
    assert set(o['cities']) == {'南京', '北京'}
    assert o['city'] == '南京'
    assert o['transit_mode'] in {m['value'] for m in o['modes']}
    assert len(o['modes']) == 4
    assert o['day_start'] and o['day_end']


# ---------------------------------------------------------------- TC-29-03
def test_preview_does_not_persist(ctx):
    """TC-29-03 预览只解析不落库（用户没确认前不能写进数据库）"""
    out = web_api.command_payload(ctx, {
        'action': 'preview', 'date': D.isoformat(),
        'text': '明天下午三点去南京博物院看展，大概2小时'})
    assert out['ok'] and out['kind'] == 'task'
    assert out['draft']['title'] == '看展'
    assert out['draft']['duration_text'] == '2 小时'
    assert out['draft']['poi'] == '南京博物院'
    assert ctx.trip_svc.day_view(D).tasks == []          # 关键：没有落库


# ---------------------------------------------------------------- TC-29-04
def test_preview_reports_missing_fields(ctx):
    """TC-29-04（改）缺时长不再报错：按推荐游玩时长给出草稿，缺的字段照样可读地说明"""
    out = web_api.command_payload(ctx, {
        'action': 'preview', 'date': D.isoformat(), 'text': '明天去火星基地看展'})
    assert out['ok'] is False
    assert out['message'], '报错要给人话'


# ---------------------------------------------------------------- TC-29-05
def test_add_after_preview_uses_draft_patch(ctx):
    """TC-29-05 用户在前端改过的字段（标题/耗时/时刻）必须生效"""
    text = '明天下午三点去南京博物院看展，大概2小时'
    web_api.command_payload(ctx, {'action': 'preview', 'date': D.isoformat(),
                                  'text': text})
    out = web_api.command_payload(ctx, {
        'action': 'add', 'date': D.isoformat(), 'text': text,
        'draft': {'title': '看展（改）', 'duration_min': 90,
                  'desired_start': '10:30', 'category': '展览'}})
    assert out['ok'], out['message']
    task = ctx.trip_svc.day_view(D).tasks[0]
    assert task.title == '看展（改）'
    assert task.duration_min == 90
    assert task.start_ts.strftime('%H:%M') == '10:30'
    assert task.is_locked is True                        # 指定时刻即锁定


# ---------------------------------------------------------------- TC-29-06
def test_state_payload_shape(ctx):
    """TC-29-06 状态接口字段完整：文本化时长、四方式对比、提示与冲突都是结构化对象"""
    web_api.command_payload(ctx, {
        'action': 'add', 'date': D.isoformat(),
        'text': '上午九点去南京博物院看展，大概2小时'})
    web_api.command_payload(ctx, {
        'action': 'add', 'date': D.isoformat(),
        'text': '中午去新街口吃饭，1小时'})
    st = web_api.state_payload(ctx, D)

    assert st['city_label'] == '金陵'
    assert st['stats']['count'] == 2
    assert st['stats']['busy_text']                      # 已人读化（"3 小时"）
    assert '分' not in st['stats']['busy_text'].replace('小时', '')
    items = st['items']
    assert len(items) == 2
    # 第二项应有通勤段与四方式对比
    with_leg = [i for i in items if i['leg']]
    assert with_leg, '第二项之前应插入通勤段'
    leg = with_leg[0]
    assert leg['leg']['minutes_text']
    # 四方式对比改为按需（?modes=1），默认不取
    assert leg['modes'] is None or set(leg['modes']) == {
        'driving', 'transit', 'walking', 'bicycling'}
    if leg['modes']:
        assert any(v['fastest'] for v in leg['modes'].values())
    # 提示与冲突是对象（前端要拿 kind / suggestions）
    assert all('kind' in n and 'detail' in n for n in st['notices'])


# ---------------------------------------------------------------- TC-29-07
def test_state_changed_by_revision(ctx):
    """TC-29-07 revision 未变时 changed=False，前端据此避免无谓重绘"""
    rev = ctx.db.revision()
    assert web_api.state_payload(ctx, D, rev)['changed'] is False
    web_api.command_payload(ctx, {'action': 'add', 'date': D.isoformat(),
                                  'text': '上午九点去玄武湖公园散步，1小时'})
    assert web_api.state_payload(ctx, D, rev)['changed'] is True


# ---------------------------------------------------------------- TC-29-08
def test_move_and_reject_bad_time(ctx):
    """TC-29-08 改时间是页面高频动作；非法时间必须被拒绝且不改数据"""
    web_api.command_payload(ctx, {'action': 'add', 'date': D.isoformat(),
                                  'text': '上午九点去玄武湖公园散步，1小时'})
    tid = ctx.trip_svc.day_view(D).tasks[0].task_id

    ok = web_api.command_payload(ctx, {'action': 'move', 'date': D.isoformat(),
                                       'task_id': tid, 'start': '15:30'})
    assert ok['ok'] and '15:30' in ok['message']
    assert ctx.trip_svc.day_view(D).tasks[0].start_ts.strftime('%H:%M') == '15:30'

    for bad in ('abc', '25:99', '9-30', ''):
        r = web_api.command_payload(ctx, {'action': 'move', 'date': D.isoformat(),
                                          'task_id': tid, 'start': bad})
        assert r['ok'] is False
    assert ctx.trip_svc.day_view(D).tasks[0].start_ts.strftime('%H:%M') == '15:30'


# ---------------------------------------------------------------- TC-29-09
def test_delete_many_and_restore(ctx):
    """TC-29-09 批量删除返回可回传的快照，撤销后条目数恢复"""
    for t in ('上午九点去玄武湖公园散步，1小时',
              '下午去先锋书店看书，1小时'):
        web_api.command_payload(ctx, {'action': 'add', 'date': D.isoformat(),
                                      'text': t})
    ids = [t.task_id for t in ctx.trip_svc.day_view(D).tasks]
    assert len(ids) == 2

    out = web_api.command_payload(ctx, {'action': 'delete_many',
                                        'date': D.isoformat(), 'task_ids': ids})
    assert out['ok'] and len(out['deleted']) == 2
    # 快照必须可序列化（旧版曾把 Poi 对象直接回传，前端拿不到）
    assert all('title' in s and 'poi_name' in s for s in out['deleted'])
    assert out['undo_token']
    assert ctx.trip_svc.day_view(D).tasks == []

    back = web_api.command_payload(ctx, {'action': 'restore_many',
                                        'date': D.isoformat(),
                                        'undo_token': out['undo_token']})
    assert back['ok'], back['message']
    assert len(ctx.trip_svc.day_view(D).tasks) == 2


# ---------------------------------------------------------------- TC-29-10
def test_lodging_flow(ctx):
    """TC-29-10 住宿推荐 → 设为锚点 → 清除，全链路可用"""
    web_api.command_payload(ctx, {'action': 'add', 'date': D.isoformat(),
                                  'text': '晚上八点去夫子庙逛逛，1个半小时'})
    cand = web_api.lodging_payload(ctx, D)
    assert cand['candidates'], '应给出住宿候选'
    name = cand['candidates'][0]['name']

    r = web_api.command_payload(ctx, {'action': 'set_lodging',
                                      'date': D.isoformat(), 'name': name})
    assert r['ok']
    assert web_api.state_payload(ctx, D)['lodging'] == name

    assert web_api.command_payload(ctx, {'action': 'clear_lodging',
                                         'date': D.isoformat()})['ok']
    assert web_api.state_payload(ctx, D)['lodging'] == ''


# ---------------------------------------------------------------- TC-29-11
def test_versions_and_rollback(ctx):
    """TC-29-11 版本列表可读、回滚后时间恢复（Web 端『更多』菜单用）"""
    web_api.command_payload(ctx, {'action': 'add', 'date': D.isoformat(),
                                  'text': '上午九点去玄武湖公园散步，1小时'})
    tid = ctx.trip_svc.day_view(D).tasks[0].task_id
    web_api.command_payload(ctx, {'action': 'move', 'date': D.isoformat(),
                                  'task_id': tid, 'start': '14:00'})
    vs = web_api.versions_payload(ctx, D)['versions']
    assert len(vs) >= 2
    assert all({'version_id', 'created_at', 'note'} <= set(v) for v in vs)

    trip_id = ctx.trip_svc.day_view(D).trip_id
    first = vs[-1]['version_id']
    r = web_api.command_payload(ctx, {'action': 'rollback', 'date': D.isoformat(),
                                      'trip_id': trip_id, 'version_id': first})
    assert r['ok']
    assert ctx.trip_svc.day_view(D).tasks[0].start_ts.strftime('%H:%M') == '09:00'


# ---------------------------------------------------------------- TC-29-12
def test_set_city_switches_scope(ctx):
    """TC-29-12 切城市后 state 的城市字段与地点作用域同时变化"""
    r = web_api.command_payload(ctx, {'action': 'set_city', 'city': '北京'})
    assert r['ok'] and r['city'] == '北京'
    st = web_api.state_payload(ctx, D)
    assert st['city'] == '北京' and st['city_label'] == '京城'
    # 北京作用域下解析南京地点应被拒（BR-01）
    bad = web_api.command_payload(ctx, {'action': 'preview', 'date': D.isoformat(),
                                        'text': '上午九点去南京博物院看展，2小时'})
    assert bad['ok'] is False and '北京' in bad['message']


# ---------------------------------------------------------------- TC-29-13
def test_assistant_offline_answer(ctx):
    """TC-29-13 离线时 AI 问答不编造，返回明确提示且 ok=False"""
    out = web_api.command_payload(ctx, {'action': 'ask', 'date': D.isoformat(),
                                        'question': '我今天有什么安排？'})
    assert out['source'] == 'offline'
    assert out['message'].strip()
    assert '离线' in out['message']


# ---------------------------------------------------------------- TC-29-14
def test_unknown_action_and_bad_date(ctx):
    """TC-29-14 非法动作与非法日期都被拒绝，且不抛异常"""
    assert web_api.command_payload(ctx, {'action': 'hack'})['ok'] is False
    assert web_api.command_payload(ctx, {
        'action': 'add', 'date': '2026-13-99', 'text': 'x'})['ok'] is False
    assert web_api.command_payload(ctx, {'action': 'add', 'date': D.isoformat(),
                                         'text': ''})['ok'] is False


# ---------------------------------------------------------------- TC-29-15
def test_month_counts_only_real_data(ctx):
    """TC-29-15 左栏月历按真实库内数据计数：有安排的日期计数，空日期不出现"""
    for text in ('上午九点去南京博物院看展，2小时', '中午去新街口吃饭，1小时'):
        web_api.command_payload(ctx, {'action': 'add', 'date': D.isoformat(), 'text': text})
    got = web_api.month_payload(ctx, D.year, D.month)
    assert got['year'] == D.year and got['month'] == D.month
    assert got['days'][D.isoformat()] == 2
    assert all(v >= 1 for v in got['days'].values())
    # 同月其它日期没排过，就不该出现在字典里（不编造 0 项日期）
    assert (date(D.year, D.month, 1).isoformat() in got['days']) is False


# ---------------------------------------------------------------- TC-29-16
def test_month_counts_follow_city_scope(ctx):
    """TC-29-16 月历计数跟随城市作用域：切到北京后看不到南京的日期"""
    web_api.command_payload(ctx, {'action': 'add', 'date': D.isoformat(),
                                  'text': '上午九点去南京博物院看展，2小时'})
    assert D.isoformat() in web_api.month_payload(ctx, D.year, D.month)['days']
    web_api.command_payload(ctx, {'action': 'set_city', 'city': '北京'})
    beijing = web_api.month_payload(ctx, D.year, D.month)
    assert D.isoformat() not in beijing['days']
    assert beijing['days'] == {}


# ---------------------------------------------------------------- TC-29-17
def test_month_payload_rejects_bad_month(ctx):
    """TC-29-17 非法年月不抛异常，退回当月（前端越界翻页不会把页面弄崩）"""
    got = web_api.month_payload(ctx, 2026, 13)
    today = date.today()
    assert (got['year'], got['month']) == (today.year, today.month)
    got2 = web_api.month_payload(ctx, 1990, 0)
    assert got2['month'] == today.month


# ---------------------------------------------------------------- TC-29-18
def test_map_payload_returns_raw_png(ctx, monkeypatch):
    """TC-29-18 位置示意图接口交出的必须是「图片字节」，不是 (bytes, note) 元组

    `day_map_image()` 返回二元组，接口层曾经忘记解包，于是 HTTP 头写着
    Content-Length: 2（元组长度）、正文不是图片，前端永远显示不出地图（D-25）。
    """
    png = b'\x89PNG\r\n\x1a\n' + b'0' * 64
    monkeypatch.setattr(ctx.trip_svc, 'day_map_image', lambda d, **kw: (png, ''))
    data, note = web_api.map_payload(ctx, D)
    assert isinstance(data, bytes) and data.startswith(b'\x89PNG')
    assert note == ''


# ---------------------------------------------------------------- TC-29-19
def test_map_payload_explains_when_no_image(ctx, monkeypatch):
    """TC-29-19 取不到图时给出可读原因（离线/没有带地点的安排），且 data 为 None"""
    monkeypatch.setattr(ctx.trip_svc, 'day_map_image',
                        lambda d, **kw: (b'', '离线模式：位置示意图需联网获取'))
    data, note = web_api.map_payload(ctx, D)
    assert data is None
    assert '离线' in note


# ---------------------------------------------------------------- TC-29-20
def test_mapinfo_payload_gives_points_and_projection(ctx):
    """TC-29-20 位置示意打点数据：点位带序号/时刻/经纬度，并给出投影参数

    Qt 版由 QPainter 在服务端画点和折线；Web 版底图只有路网，标记必须由前端叠。
    前端要自己算像素坐标，就必须拿到 size / zoom / center（缺陷 D-25）。
    """
    for text in ('上午九点去南京博物院看展，2小时', '中午去新街口吃饭，1小时'):
        web_api.command_payload(ctx, {'action': 'add', 'date': D.isoformat(), 'text': text})
    info = web_api.mapinfo_payload(ctx, D)

    assert info['ok'] is True
    assert info['size'] == [900, 380]
    assert 9 <= info['zoom'] <= 16
    assert len(info['center']) == 2
    pts = info['points']
    assert len(pts) == 2
    assert [p['seq'] for p in pts] == [1, 2]          # 序号从 1 开始，按时间排序
    assert all(p['name'] and p['lng'] and p['lat'] for p in pts)
    assert pts[0]['time'] <= pts[1]['time']


# ---------------------------------------------------------------- TC-29-21
def test_mapinfo_payload_without_points(ctx):
    """TC-29-21 当天没有带地点的安排时，打点接口给出可读原因而不是报错"""
    info = web_api.mapinfo_payload(ctx, D)
    assert info['ok'] is False
    assert info['points'] == []
    assert info['message']


# ---------------------------------------------------------------- TC-29-22
def test_toggle_offline_action(ctx):
    """TC-29-22 「更多」里的强制离线开关：能切离线也能切回在线

    要区分两件事：`settings.offline_mode` 是用户的**开关**，
    `state_payload()['offline']` 是**生效结果**——没配 Key 时开关即使关掉也仍算离线，
    这时模式徽标应显示"离线"，不能骗用户说在线。
    """
    ctx.settings.offline_mode = False
    ctx.apply_settings(ctx.settings)
    off = web_api.command_payload(ctx, {'action': 'toggle_offline',
                                        'date': D.isoformat(), 'offline': True})
    assert off['ok'] is True and '离线' in off['message']
    assert ctx.settings.offline_mode is True
    assert web_api.state_payload(ctx, D)['offline'] is True

    on = web_api.command_payload(ctx, {'action': 'toggle_offline',
                                       'date': D.isoformat(), 'offline': False})
    assert on['ok'] is True
    assert ctx.settings.offline_mode is False
    # 测试环境没有 Key，所以"生效结果"仍是离线：开关关掉 ≠ 真的能联网
    assert web_api.state_payload(ctx, D)['offline'] is True


# ---------------------------------------------------------------- TC-29-23
def test_mapinfo_honours_view_params(ctx):
    """TC-29-23 地图页可指定画布尺寸、层级与中心点（缩放／点击点位居中都靠它）"""
    for text in ('上午九点去南京博物院看展，2小时', '中午去新街口吃饭，1小时'):
        web_api.command_payload(ctx, {'action': 'add', 'date': D.isoformat(), 'text': text})
    info = web_api.mapinfo_payload(ctx, D, w=800, h=600, zoom=16,
                                   center='118.78,32.04')
    assert info['ok'] is True
    assert info['size'] == [800, 600]
    assert info['zoom'] == 16
    assert info['center'] == [118.78, 32.04]
    assert len(info['points']) == 2


# ---------------------------------------------------------------- TC-29-24
def test_mapinfo_clamps_bad_view_params(ctx):
    """TC-29-24 尺寸/层级/中心点给了非法值时不许崩：夹到合法范围或退回自动"""
    web_api.command_payload(ctx, {'action': 'add', 'date': D.isoformat(),
                                  'text': '上午九点去南京博物院看展，2小时'})
    big = web_api.mapinfo_payload(ctx, D, w=5000, h=5000)
    assert big['size'] == [1024, 1024]                 # 高德单边上限
    tiny = web_api.mapinfo_payload(ctx, D, w=10, h=10)
    assert tiny['size'] == [240, 180]                  # 下限
    wild = web_api.mapinfo_payload(ctx, D, zoom=99, center='不是坐标')
    assert wild['zoom'] == 16                          # 层级夹到高德实际上限
    assert len(wild['center']) == 2                    # 非法中心点回退包围盒
    junk = web_api.mapinfo_payload(ctx, D, w='abc', h='?', zoom='x')
    assert junk['ok'] is True and junk['size'][0] >= 240


# ---------------------------------------------------------------- TC-29-25
def test_mappath_follows_selected_mode(ctx):
    """TC-29-25 地图路段按**当前通行方式**取真实走向，不同方式给不同折线与耗时"""
    for text in ('上午九点去南京博物院看展，2小时', '中午去新街口吃饭，1小时'):
        web_api.command_payload(ctx, {'action': 'add', 'date': D.isoformat(), 'text': text})

    seen = []

    class FakeMap:
        def name(self):
            return '高德地图'

        def route_polyline(self, origin, dest, mode):
            seen.append(mode)
            # 两种方式给不同的折线与耗时，模拟真实差异
            n = 3 if mode == 'walking' else 5
            return {'distance_m': 3000 if mode == 'walking' else 4200,
                    'minutes': 45 if mode == 'walking' else 16,
                    'lines': [[[origin[0] + i * 0.001, origin[1]] for i in range(n)]]}

    ctx.settings.offline_mode = False
    ctx.settings.map_api_key = 'k'
    ctx.apply_settings(ctx.settings)
    ctx.degrade._map = FakeMap()

    walk = web_api.mappath_payload(ctx, D, 'walking')
    drive = web_api.mappath_payload(ctx, D, 'driving')

    assert walk['ok'] is True and drive['ok'] is True
    assert walk['mode'] == 'walking' and drive['mode'] == 'driving'
    w0, d0 = walk['legs'][0], drive['legs'][0]
    assert w0['source'] == 'amap' and len(w0['lines'][0]) == 3
    assert d0['source'] == 'amap' and len(d0['lines'][0]) == 5
    assert (w0['minutes'], w0['distance_m']) == (45, 3000)
    assert (d0['minutes'], d0['distance_m']) == (16, 4200)
    assert set(seen) == {'walking', 'driving'}


# ---------------------------------------------------------------- TC-29-26
def test_mappath_falls_back_to_straight_line(ctx):
    """TC-29-26 取不到真实折线时退回直线，并标记 source=straight（不假装是真实路径）"""
    for text in ('上午九点去南京博物院看展，2小时', '中午去新街口吃饭，1小时'):
        web_api.command_payload(ctx, {'action': 'add', 'date': D.isoformat(), 'text': text})
    out = web_api.mappath_payload(ctx, D, 'driving')      # 离线：没有 Key
    assert out['ok'] is True
    assert out['online'] is False
    assert all(leg['source'] == 'straight' and leg['lines'] == [] for leg in out['legs'])
    assert out['legs'][0]['from'] and out['legs'][0]['to']


# ---------------------------------------------------------------- TC-29-27
def test_map_zoom_never_exceeds_provider_cap(ctx):
    """TC-29-27 层级不得超过底图服务实际能力（高德静态图 16 级封顶）

    踩过的坑：前端请求 17 级时，高德返回的仍是 16 级的图，但接口照样回 `zoom=17`，
    于是前端按 17 级投影打点——底图与标记成倍错位，看起来就是"定位不准"（D-29）。
    """
    from nanjingtrip.adapters.amap import AmapAdapter

    web_api.command_payload(ctx, {'action': 'add', 'date': D.isoformat(),
                                  'text': '上午九点去南京博物院看展，2小时'})
    for req in (17, 18, 99):
        info = web_api.mapinfo_payload(ctx, D, zoom=req)
        assert info['zoom'] == AmapAdapter.MAX_ZOOM


# ---------------------------------------------------------------- TC-29-28
def test_pointinfo_flags_mismatched_address(ctx):
    """TC-29-28 「确认点」要能一眼看出点位可疑：高德把坐标解析成的地址里没有点位名就标记

    真实案例：城市包里"新街口"的坐标，高德解析出来是"华泰证券大厦(洪武路)"，
    说明这个坐标根本不在新街口——这种点必须先让用户确认，再算路径（D-31）。
    """
    for text in ('上午九点去南京博物院看展，2小时', '中午去新街口吃饭，1小时'):
        web_api.command_payload(ctx, {'action': 'add', 'date': D.isoformat(), 'text': text})

    class FakeMap:
        def name(self):
            return '高德地图'

        def regeo(self, lng, lat):
            # 第一个点在目标地，第二个点解析成了写字楼
            if lng > 118.80:
                return {'address': '江苏省南京市玄武区南京博物院'}
            return {'address': '江苏省南京市秦淮区华泰证券大厦'}

        def place_candidates(self, kw, city, limit=5):
            return [{'name': kw, 'address': '同名点', 'district': '秦淮区',
                     'lng': 118.78, 'lat': 32.04, 'type': ''}]

    ctx.settings.offline_mode = False
    ctx.settings.map_api_key = 'k'
    ctx.apply_settings(ctx.settings)
    ctx.degrade._map = FakeMap()

    pts = web_api.pointinfo_payload(ctx, D)['points']
    assert len(pts) == 2
    assert all(p['task_id'] for p in pts)
    ok, bad = pts[0], pts[1]
    assert ok['address_mismatch'] is False        # 地址里含"南京博物院"
    assert bad['address_mismatch'] is True         # 地址里没有"新街口"
    assert bad['candidates'] and 'distance_m' in bad['candidates'][0]


# ---------------------------------------------------------------- TC-29-29
def test_pin_point_moves_coord_and_replans(ctx):
    """TC-29-29 确认点后坐标落库并重排，地图与通行时长改用新坐标"""
    web_api.command_payload(ctx, {'action': 'add', 'date': D.isoformat(),
                                  'text': '上午九点去南京博物院看展，2小时'})
    pts = web_api.pointinfo_payload(ctx, D)['points']
    task_id = pts[0]['task_id']
    before = (pts[0]['lng'], pts[0]['lat'])

    out = web_api.command_payload(ctx, {'action': 'pin_point', 'date': D.isoformat(),
                                        'task_id': task_id, 'lng': 118.79, 'lat': 32.05})
    assert out['ok'] is True
    after = web_api.pointinfo_payload(ctx, D)['points'][0]
    assert (round(after['lng'], 5), round(after['lat'], 5)) == (118.79, 32.05)
    assert (after['lng'], after['lat']) != before


# ---------------------------------------------------------------- TC-29-30
def test_legoptions_gives_multiple_choices(ctx):
    """TC-29-30 「确认路径」要给出多个可选方案（含各自时间/里程），供用户挑选"""
    for text in ('上午九点去南京博物院看展，2小时', '中午去新街口吃饭，1小时'):
        web_api.command_payload(ctx, {'action': 'add', 'date': D.isoformat(), 'text': text})

    class FakeMap:
        def name(self):
            return '高德地图'

        def route_options(self, origin, dest, mode, limit=3):
            return [
                {'label': '方案 1', 'minutes': 20, 'distance_m': 4000,
                 'lines': [[[origin[0], origin[1]], [dest[0], dest[1]]]]},
                {'label': '方案 2', 'minutes': 14, 'distance_m': 5200,
                 'lines': [[[origin[0], origin[1]], [dest[0], dest[1]]]]},
            ][:limit]

    ctx.settings.offline_mode = False
    ctx.settings.map_api_key = 'k'
    ctx.apply_settings(ctx.settings)
    ctx.degrade._map = FakeMap()

    out = web_api.legoptions_payload(ctx, D, 'driving')
    assert out['ok'] is True and out['mode'] == 'driving'
    leg = out['legs'][0]
    assert len(leg['options']) == 2
    assert [o['minutes'] for o in leg['options']] == [20, 14]
    assert all(o['lines'] for o in leg['options'])


# ---------------------------------------------------------------- TC-29-31
def test_leg_payload_is_json_not_tuple(ctx):
    """TC-29-31 路段详情必须是 JSON 对象，不能把 (detail, note) 元组直接塞进去

    踩过的坑：离线时前端收到的是 `[{}, "离线模式…"]` 这种数组，页面弹出一个
    原始 JSON 的 alert（缺陷 D-33）。
    """
    import json

    web_api.command_payload(ctx, {'action': 'add', 'date': D.isoformat(),
                                  'text': '上午九点去南京博物院看展，2小时'})
    web_api.command_payload(ctx, {'action': 'add', 'date': D.isoformat(),
                                  'text': '中午去新街口吃饭，1小时'})
    items = web_api.state_payload(ctx, D)['items']
    second = [i for i in items if i.get('leg')][0]

    out = web_api.leg_payload(ctx, second['task_id'])
    assert isinstance(out['detail'], dict)          # 不是 list
    json.dumps(out)                                  # 必须可序列化
    if not out['ok']:
        assert out['message']                        # 失败也要有可读原因


# ---------------------------------------------------------------- TC-29-32
def test_advice_payload_is_serialisable(ctx):
    """TC-29-32 出行建议必须能序列化成 JSON（原来直接回数据类 → 500，缺陷 D-34）"""
    import json

    web_api.command_payload(ctx, {'action': 'add', 'date': D.isoformat(),
                                  'text': '上午九点去南京博物院看展，2小时'})
    web_api.command_payload(ctx, {'action': 'add', 'date': D.isoformat(),
                                  'text': '中午去新街口吃饭，1小时'})
    items = web_api.state_payload(ctx, D)['items']
    target = items[-1]

    out = web_api.advice_payload(ctx, target['task_id'])
    json.dumps(out)                                  # 不抛就是过
    assert 'advice' in out or out['ok'] is False
    if out.get('ok'):
        adv = out['advice']
        assert isinstance(adv, dict)
        assert 'summary' in adv and 'caveat' in adv
        # 结构化字段齐全，前端才能渲染出发时刻列表
        assert isinstance(adv.get('options', []), list)


# ---------------------------------------------------------------- TC-29-33
def test_mapconfig_gates_js_key(ctx):
    """TC-29-33 交互地图配置：只有在线且配了 Key 才下发 Key，离线一律 ready=False

    JS API 的 Key 必须到浏览器才能建图，因此这里是唯一"Key 离开服务端"的地方；
    离线时必须关掉，让前端退回静态图（Key 不出机）。
    """
    ctx.settings.offline_mode = True
    ctx.settings.map_api_key = 'k'
    ctx.apply_settings(ctx.settings)
    off = web_api.mapconfig_payload(ctx)
    assert off['ready'] is False and off['key'] == ''
    assert off['offline'] is True and off['reason']

    ctx.settings.offline_mode = False
    ctx.apply_settings(ctx.settings)
    on = web_api.mapconfig_payload(ctx)
    assert on['ready'] is True and on['key'] == 'k'
    assert on['offline'] is False
    assert 'AMap.Geolocation' in on['plugins']       # 实时定位插件


# ---------------------------------------------------------------- TC-29-34
def test_regeo_payload(ctx):
    """TC-29-34 拖动标记后要能问"这里是什么地方"；离线时给可读提示而不是报错"""
    off = web_api.regeo_payload(ctx, 118.78, 32.04)
    assert off['ok'] is False and '离线' in off['message']
    bad = web_api.regeo_payload(ctx, 'x', 'y')
    assert bad['ok'] is False and '坐标' in bad['message']


# ---------------------------------------------------------------- TC-29-35
def test_split_batch_multi_destination():
    """TC-29-35 一句话多目的地：切出 4 段并抽出"我现在在X"为起点（案例一的核心缺口）"""
    from nanjingtrip.parsing_rules import extract_origin, split_batch

    text = ('我现在在南京邮电大学三牌楼校区，今天11月1日白天出行。我需要依次去：'
            '新街口商圈（停留40分钟）、总统府（停留1小时）、夫子庙（停留1小时20分钟）、'
            '老门东（停留1小时）。帮我规划全天最优路线，自动计算路程、交通方式、抵达离开时间，'
            '生成完整时序行程表。')
    origin, _ = extract_origin(text)
    assert origin == '南京邮电大学三牌楼校区'
    segs = split_batch(text)
    assert len(segs) == 4, segs
    for key in ('新街口', '总统府', '夫子庙', '老门东'):
        assert any(key in s for s in segs), (key, segs)


# ---------------------------------------------------------------- TC-29-36
def test_preview_batch_and_add_in_order(ctx):
    """TC-29-36 批量预览 + 按给定顺序写入：顺序不再被"耗时降序"打乱"""
    text = '依次去：新街口（40分钟）、总统府（1小时）、夫子庙（1小时20分钟）'
    out = web_api.command_payload(ctx, {'action': 'preview_batch', 'date': D.isoformat(),
                                        'text': text})
    assert out['ok'] is True and out['kind'] == 'batch'
    assert len(out['items']) == 3
    assert [i['poi'] for i in out['items']] == ['新街口', '总统府', '夫子庙']

    added = web_api.command_payload(ctx, {
        'action': 'add_batch', 'date': D.isoformat(),
        'items': [{'title': i['title'], 'poi': i['poi'],
                   'duration_min': i['duration_min']} for i in out['items']],
        'keep_order': True})
    assert added['ok'] is True

    st = web_api.state_payload(ctx, D)
    pois = [i['poi'] for i in st['items']]
    assert pois == ['新街口', '总统府', '夫子庙'], pois      # 与输入同序


# ---------------------------------------------------------------- TC-29-37
def test_keep_order_off_allows_reorder(ctx):
    """TC-29-37 关掉"按我给顺序"时仍按原策略（耗时降序）——两种模式都可用"""
    text = '依次去：新街口（40分钟）、总统府（1小时）、夫子庙（1小时20分钟）'
    out = web_api.command_payload(ctx, {'action': 'preview_batch', 'date': D.isoformat(),
                                        'text': text})
    web_api.command_payload(ctx, {
        'action': 'add_batch', 'date': D.isoformat(),
        'items': [{'title': i['title'], 'poi': i['poi'],
                   'duration_min': i['duration_min']} for i in out['items']],
        'keep_order': False})
    st = web_api.state_payload(ctx, D)
    assert len(st['items']) == 3
    # 关掉后至少第一项仍能排出（不强求顺序）
    assert all(i['time'] for i in st['items'])


# ---------------------------------------------------------------- TC-29-38
def test_auto_mode_picks_fastest_per_leg(ctx):
    """TC-29-38 每段自动择优：开着 auto_mode 时各段方式按最快选，关掉时统一用默认"""
    from nanjingtrip.models import TransitMode

    a = ctx.trip_svc._poi_named('南京博物院')
    b = ctx.trip_svc._poi_named('新街口')
    picked = ctx.trip_svc.pick_fastest_mode(a, b, TransitMode.DRIVING)
    assert picked in set(TransitMode)
    # 离线也能给一个确定结果（estimate_all 有本地估算）
    ctx.settings.auto_mode = True
    ctx.apply_settings(ctx.settings)
    assert ctx.trip_svc._mode_pick() is not None
    ctx.settings.auto_mode = False
    ctx.apply_settings(ctx.settings)
    assert ctx.trip_svc._mode_pick() is None


# ---------------------------------------------------------------- TC-29-39
def test_group_duration_parsing():
    """TC-29-39 "每个地方平均停留50分钟"这类统一时长要能整句取到"""
    from nanjingtrip.parsing_rules import parse_group_duration

    assert parse_group_duration('每个地方平均停留50分钟') == 50
    assert parse_group_duration('每处1小时') == 60
    assert parse_group_duration('平均停留两小时') == 120
    assert parse_group_duration('去南京博物院，2小时') is None   # 单条时长不算统一时长


# ---------------------------------------------------------------- TC-29-40
def test_resolve_prefers_city_pack(ctx):
    """TC-29-40 地点解析以城市包为先：别名能命中，就不会被在线检索带偏

    真实案例：'新街口先锋书店'曾被解析成'光华书局'、'大报恩寺'被解析成
    '大报恩寺恩剧场'（都是名字相近的邻居 POI）。
    """
    geo = ctx.trip_svc.geo_svc
    assert geo.resolve('大报恩寺')[0].name == '大报恩寺遗址公园'
    assert geo.resolve('玄武湖')[0].name == '玄武湖公园'
    assert geo.resolve('先锋书店')[0].name == '先锋书店五台山店'
    assert geo.resolve('南京博物院')[0].name == '南京博物院'


# ---------------------------------------------------------------- TC-29-41
def test_optimize_order_is_optimal(ctx):
    """TC-29-41 "没有固定顺序、帮我排最顺路"：最近邻+2-opt 的顺序不劣于暴力最优

    这里用 5 个真实点位、以真实通行估算为距离，枚举 120 种排列取最优作对照。
    """
    import itertools

    from nanjingtrip.engine import _optimize_order
    from nanjingtrip.models import ScheduleOptions, TransitMode

    names = ['玄武湖公园', '鸡鸣寺', '先锋书店五台山店', '大报恩寺遗址公园', '南京博物院']
    pois = [ctx.trip_svc._poi_named(n) for n in names]
    anchor = ctx.trip_svc._poi_named('南京站')

    def transit(a, b, mode=TransitMode.DRIVING):
        if a is None or b is None:
            return 0
        v = ctx.trip_svc.transit.estimate(a, b, mode)
        return int(v) if v else 30

    opts = ScheduleOptions(mode=TransitMode.DRIVING, optimize_route=True)
    tasks = [type('T', (), {'poi': p, 'mode': None, 'title': p.name,
                            'duration_min': 50, 'is_locked': False})()
             for p in pois]
    order = _optimize_order(tasks, anchor, transit, opts, None)
    got = [t.poi for t in order]

    def cost(seq):
        total = transit(anchor, seq[0])
        total += sum(transit(a, b) for a, b in zip(seq, seq[1:]))
        return total

    best = min(cost(list(p)) for p in itertools.permutations(pois))
    assert cost(got) == best, (cost(got), best, [p.name for p in got])


# ---------------------------------------------------------------- TC-29-42
def test_delay_shifts_all_downstream(ctx):
    """TC-29-42 动态变更：某项多花 40 分钟，后续所有节点精确顺延 40 分钟

    这是"延误重算"的核心语义：不重排序，只把受影响段落整体后移；
    已经开始的那一项保持原开始时刻，只把结束（时长）推后。
    """
    D3 = date(2026, 11, 3)
    # 用城市包里一定有的点位：苜蓿园/莫愁湖/颐和路只存在于联网检索结果里，
    # 离线包未收录（属已知覆盖缺口，见案例三报告）
    items = [('中山陵', '中山陵', 90), ('夫子庙', '夫子庙', 40),
             ('新街口', '新街口', 60)]
    web_api.command_payload(ctx, {
        'action': 'add_batch', 'date': D3.isoformat(),
        'items': [{'title': t, 'poi': p, 'duration_min': d} for t, p, d in items],
        'keep_order': True})
    before = web_api.state_payload(ctx, D3)['items']
    assert len(before) == 3
    starts_before = [i['time'] for i in before]

    first_id = before[0]['task_id']
    out = web_api.command_payload(ctx, {'action': 'delay', 'date': D3.isoformat(),
                                        'task_id': first_id, 'delay_min': 40})
    assert out['ok'] is True

    after = web_api.state_payload(ctx, D3)['items']
    # 首项开始时刻不变，结束推迟 40 分钟
    assert after[0]['time'] == starts_before[0]
    assert after[0]['minutes'] == before[0]['minutes'] + 40
    # 后续各项开始时刻恰好 +40 分钟
    def to_min(hhmm):
        h, m = hhmm.split(':')
        return int(h) * 60 + int(m)
    for i in range(1, len(after)):
        assert to_min(after[i]['time']) - to_min(starts_before[i]) == 40, (
            after[i]['time'], starts_before[i])


# ---------------------------------------------------------------- TC-29-43
def test_plans_index_and_delete_day(ctx):
    """TC-29-43 规划记录管理：列表能查到"已有规划"，删除能删整天

    这三天（11-01/02/03）在库里就是"已有的规划记录"，现场演示要能列出、
    打开、删除，而不是靠脚本去动数据库。
    """
    D1 = date(2026, 11, 1)
    web_api.command_payload(ctx, {'action': 'add_batch', 'date': D1.isoformat(),
                                  'items': [{'title': '看展', 'poi': '南京博物院',
                                             'duration_min': 60}],
                                  'keep_order': True})
    idx = web_api.plans_index_payload(ctx)
    assert idx['ok'] is True
    day = next(d for d in idx['days'] if d['date'] == D1.isoformat())
    assert day['count'] == 1
    assert day['first'] and day['last']          # HH:MM

    out = web_api.command_payload(ctx, {'action': 'delete_day',
                                        'date': D1.isoformat()})
    assert out['ok'] is True
    assert not any(d['date'] == D1.isoformat()
                   for d in web_api.plans_index_payload(ctx)['days'])
