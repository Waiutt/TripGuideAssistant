"""用户档案：字段推导、存档、切换，以及"有无车/方式优先"对择优的影响

（重建版；覆盖 D-39、D-49～D-51 的回归点）
"""
from datetime import date, time

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.models import TransitMode
from nanjingtrip.profile import Profile, ProfileStore

D = date(2026, 11, 6)


def test_effort_maps_to_walk_and_bike_caps():
    assert Profile(name='a', effort='轻松').max_walk_min == 20
    assert Profile(name='a', effort='标准').max_walk_min == 45
    assert Profile(name='a', effort='能走能骑').max_walk_min == 60
    assert Profile(name='a', effort='轻松').max_bike_min == 0
    assert Profile(name='a', effort='标准').max_bike_min == 30
    assert Profile(name='a', effort='能走能骑').max_bike_min == 45


def test_pace_affects_buffer_and_stay():
    assert Profile(name='a', pace='慢').buffer_delta == 5
    assert Profile(name='a', pace='标准').buffer_delta == 0
    assert Profile(name='a', pace='快').buffer_delta == -5
    assert Profile(name='a', pace='慢').stay_factor > Profile(name='a', pace='快').stay_factor


def test_budget_maps_to_price_cap():
    assert Profile(name='a', budget='省').max_price == 60
    assert Profile(name='a', budget='中').max_price == 150
    assert Profile(name='a', budget='不设').max_price is None


def test_mode_priority_maps_to_mode():
    # preferred_mode 返回的是方式字符串（'bicycling' 等），不是枚举
    assert Profile(name='a', mode_priority='骑行优先').preferred_mode == 'bicycling'
    assert Profile(name='a', mode_priority='驾车优先').preferred_mode == 'driving'
    assert Profile(name='a', mode_priority='最快').preferred_mode in (None, '')


def test_describe_is_human_readable():
    text = Profile(name='学生小李', identity='学生', effort='能走能骑',
                   budget='省', has_car=False).describe()
    assert '学生' in text


def test_store_has_default_and_roundtrips(tmp_path):
    st = ProfileStore(tmp_path)
    assert st.active_name(), '空存档也要有可用默认档'
    assert st.active().effort == '标准'
    st.save(Profile(name='学生小李', identity='学生', effort='能走能骑', budget='省'))
    st.set_active('学生小李')
    again = ProfileStore(tmp_path)
    assert again.active_name() == '学生小李'
    assert again.active().effort == '能走能骑'


def test_store_keeps_default_when_name_is_blank(tmp_path):
    """空白名不该被当成新档案（当前实现是静默忽略，不会覆盖默认档）"""
    st = ProfileStore(tmp_path)
    before = st.active_name()
    st.save(Profile(name='   '))
    assert st.active_name() == before


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def test_apply_profile_sets_thresholds(ctx):
    ctx.apply_profile(Profile(name='轻松档', effort='轻松', pace='标准'))
    assert ctx.trip_svc.max_walk_min == 20
    assert ctx.trip_svc.max_bike_min == 0


def test_switch_profile_is_reversible(ctx):
    """D-50/D-51：切来切去不该让缓冲越切越小、起床时间残留"""
    base = ctx.settings.buffer_min
    ctx.apply_profile(Profile(name='慢档', pace='慢', wake='09:30'))
    assert ctx.settings.buffer_min == base + 5
    assert ctx.settings.day_start == '09:30'
    ctx.apply_profile(Profile(name='快档', pace='快'))
    assert ctx.settings.buffer_min == base - 5
    assert ctx.settings.day_start != '09:30'


def test_apply_profile_sets_budget_cap(ctx):
    ctx.apply_profile(Profile(name='省档', budget='省'))
    assert ctx.trip_svc.max_price == 60


def test_profile_does_not_override_explicit_order(ctx):
    """档案只改默认值与阈值，用户显式给的顺序/锁定不能被覆盖"""
    ctx.apply_profile(Profile(name='轻松档', effort='轻松'))
    ok, msg = ctx.trip_svc.add_batch(D, [
        {'kind': 'task', 'title': 'A', 'poi': '总统府', 'duration_min': 60,
         'category': '其他', 'desired_start': '10:00'},
        {'kind': 'task', 'title': 'B', 'poi': '夫子庙', 'duration_min': 60,
         'category': '其他'},
    ], keep_order=True)
    assert ok, msg
    tasks = ctx.trip_svc.day_view(D).tasks
    assert tasks[0].poi.name == '总统府'
    assert tasks[0].is_locked and tasks[0].start_ts == time(10, 0)


def _pois(ctx):
    return (ctx.trip_svc._poi_named('总统府'), ctx.trip_svc._poi_named('夫子庙'))


def test_no_car_excludes_driving_from_pick(ctx):
    ctx.apply_profile(Profile(name='无车', has_car=False, mode_priority='最快'))
    a, b = _pois(ctx)
    ctx.trip_svc.transit.estimate_all = lambda x, y: {
        'driving': 8, 'transit': 25, 'walking': 40, 'bicycling': 20}
    assert ctx.trip_svc.pick_fastest_mode(a, b, TransitMode.DRIVING) != TransitMode.DRIVING


def test_car_allows_driving(ctx):
    ctx.apply_profile(Profile(name='有车', has_car=True, mode_priority='最快'))
    a, b = _pois(ctx)
    ctx.trip_svc.transit.estimate_all = lambda x, y: {
        'driving': 8, 'transit': 25, 'walking': 40, 'bicycling': 20}
    assert ctx.trip_svc.pick_fastest_mode(a, b, TransitMode.TRANSIT) == TransitMode.DRIVING


def test_priority_within_tolerance_wins(ctx):
    """优先方式只要在"最快 + 容差"内就该选它"""
    ctx.apply_profile(Profile(name='公交党', has_car=True, mode_priority='公共交通优先'))
    a, b = _pois(ctx)
    ctx.trip_svc.transit.estimate_all = lambda x, y: {
        'driving': 10, 'transit': 16, 'walking': 50, 'bicycling': 30}
    assert ctx.trip_svc.pick_fastest_mode(a, b, TransitMode.DRIVING) == TransitMode.TRANSIT


def test_only_driving_available_still_allowed(ctx):
    """无车但只剩驾车有数据时不能无解"""
    ctx.apply_profile(Profile(name='无车', has_car=False))
    a, b = _pois(ctx)
    ctx.trip_svc.transit.estimate_all = lambda x, y: {'driving': 10}
    assert ctx.trip_svc.pick_fastest_mode(a, b, TransitMode.DRIVING) == TransitMode.DRIVING


def test_effort_caps_block_long_bike_rides(ctx):
    """D-39：骑行超过阈值就不参与择优"""
    ctx.apply_profile(Profile(name='标准', effort='标准'))
    a, b = _pois(ctx)
    ctx.trip_svc.transit.estimate_all = lambda x, y: {
        'driving': 40, 'transit': 45, 'walking': 70, 'bicycling': 55}
    assert ctx.trip_svc.pick_fastest_mode(a, b, TransitMode.DRIVING) != TransitMode.BICYCLING
