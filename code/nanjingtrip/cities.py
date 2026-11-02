# -*- coding: utf-8 -*-
"""城市包（City Pack）：把「城市」从代码常量变成数据。

设计要点
--------
1. **BR-01 参数化**：业务规则仍是"地点必须属于当前城市"，但行政区列表、
   离线 POI、默认交通方式、示例话术全部来自本模块的城市包，不再写死在代码里。
   新增一座城市 = 增加一个 :class:`CityPack`，不改任何业务逻辑。
2. **营业时间是数据不是逻辑**：``open`` / ``closed`` / ``book`` 三个字段让排程
   能够提示"周一闭馆""17:00 关门""需提前预约"，而不必把规则写进引擎。
3. **离线包为演示数据**：坐标、开放时间、闭馆日均取自公开信息的近似值，
   仅用于离线演示与距离估算，出行前请以官方发布为准（:attr:`CityPack.note`）。

POI 字段
--------
name / address / district / lng / lat / aliases 为必填；
open 为 ``('HH:MM', 'HH:MM')`` 开放时段；
closed 为闭馆星期元组，如 ``('周一',)``；
book 为预约要求说明；visit 为建议游览时长（分钟）；kind 为类别提示。
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional

#: 默认城市（老库、老配置无 city 字段时回落到这里）
DEFAULT_CITY = '南京'

#: 星期中文名（与 date.weekday() 对齐：0 = 周一）
WEEK_CN = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')

# 早高峰/晚高峰时段（分钟数区间、说明）——城市包可覆盖
_DEFAULT_PEAKS = ((7 * 60, 9 * 60 + 30, '早高峰'),
                  (17 * 60, 19 * 60 + 30, '晚高峰'))


# ======================================================================
# 南京市离线 POI
# ======================================================================
NANJING_POIS: tuple[dict, ...] = (
    {'name': '南京博物院', 'address': '中山东路321号', 'district': '玄武区',
     'lng': 118.82506, 'lat': 32.04080, 'aliases': ('南博', '博物院'),
     'open': ('09:00', '17:00'), 'closed': ('周一',),
     'book': '需提前在官方渠道实名预约', 'visit': 150, 'kind': '景点'},
    {'name': '新街口', 'address': '中山路18号', 'district': '秦淮区',
     'lng': 118.7787, 'lat': 32.0416, 'aliases': ('新街口地铁站',),
     'open': ('09:00', '22:00'), 'visit': 60, 'kind': '商圈'},
    {'name': '夫子庙', 'address': '秦淮河北岸', 'district': '秦淮区',
     'lng': 118.78783, 'lat': 32.02158, 'aliases': (),
     'open': ('09:00', '22:00'), 'visit': 120, 'kind': '景点'},
    {'name': '老门东', 'address': '剪子巷54号', 'district': '秦淮区',
     'lng': 118.78764, 'lat': 32.01160, 'aliases': (),
     'open': ('09:00', '22:00'), 'visit': 90, 'kind': '街区'},
    {'name': '总统府', 'address': '长江路292号', 'district': '玄武区',
     'lng': 118.79740, 'lat': 32.04423, 'aliases': (),
     'open': ('08:30', '18:00'), 'book': '建议提前预约', 'visit': 120,
     'kind': '景点'},
    {'name': '中山陵', 'address': '紫金山南麓', 'district': '玄武区',
     'lng': 118.85331, 'lat': 32.06221, 'aliases': (),
     'open': ('08:30', '17:00'), 'closed': ('周一',),
     'book': '需提前预约', 'visit': 150, 'kind': '景点'},
    {'name': '明孝陵', 'address': '紫金山南麓', 'district': '玄武区',
     'lng': 118.84046, 'lat': 32.05193, 'aliases': (),
     'open': ('06:30', '18:30'), 'visit': 120, 'kind': '景点'},
    {'name': '玄武湖公园', 'address': '玄武巷1号', 'district': '玄武区',
     'lng': 118.78395, 'lat': 32.07272, 'aliases': ('玄武湖',),
     'open': ('06:00', '21:00'), 'visit': 90, 'kind': '公园'},
    {'name': '鸡鸣寺', 'address': '鸡鸣寺路1号', 'district': '玄武区',
     'lng': 118.79525, 'lat': 32.06106, 'aliases': (),
     'open': ('07:00', '17:00'), 'visit': 60, 'kind': '景点'},
    # 台城段：坐标取"明城墙台城景区(出入口)/南京城墙景区"两个检索结果在解放门 8 号
    # 的收敛点（118.79626,32.06272，相距约 50m）；景区整体质心在 118.802 以东 500m，
    # 那是九华山侧的城墙，不是登城口，故不采用。营业时间取高德 biz_ext.opentime2。
    {'name': '南京城墙台城景区', 'address': '解放门8号（解放门票口登城）',
     'district': '玄武区', 'lng': 118.79626, 'lat': 32.06272,
     'aliases': ('玄武湖台城段', '台城', '明城墙台城段', '台城景区', '解放门'),
     'open': ('08:30', '17:00'), 'visit': 60, 'kind': '景点',
     'book': '日场 08:30-17:00，从解放门票口登城'},
    {'name': '北极阁公园', 'address': '鸡鸣寺路1号（鸡笼山）', 'district': '玄武区',
     'lng': 118.79069, 'lat': 32.0597,
     'aliases': ('北极阁', '鸡笼山', '北极阁气象博物馆'),
     'open': ('09:00', '16:00'), 'visit': 40, 'kind': '公园'},
    {'name': '环亚凯瑟琳广场', 'address': '北京东路1号', 'district': '玄武区',
     'lng': 118.7883, 'lat': 32.05929,
     'aliases': ('凯瑟琳广场', '凯瑟琳', '环亚凯瑟琳'),
     'open': ('09:00', '22:00'), 'visit': 60, 'kind': '商圈'},
    {'name': '南京站', 'address': '龙蟠路111号', 'district': '玄武区',
     'lng': 118.79751, 'lat': 32.08713, 'aliases': (), 'kind': '交通'},
    {'name': '南京南站', 'address': '江南路', 'district': '雨花台区',
     'lng': 118.79803, 'lat': 31.96876, 'aliases': ('南站',), 'kind': '交通'},
    {'name': '鼓楼医院', 'address': '中山路321号', 'district': '鼓楼区',
     'lng': 118.78304, 'lat': 32.05618, 'aliases': ('南京鼓楼医院',),
     'kind': '医疗'},
    {'name': '江苏省人民医院', 'address': '广州路300号', 'district': '鼓楼区',
     'lng': 118.76628, 'lat': 32.04921, 'aliases': ('省人民医院', '人民医院'),
     'kind': '医疗'},
    {'name': '南京大学鼓楼校区', 'address': '汉口路22号', 'district': '鼓楼区',
     'lng': 118.7809, 'lat': 32.0567, 'aliases': ('南大', '南京大学'),
     'book': '入校一般需预约', 'visit': 90, 'kind': '校园'},
    {'name': '东南大学四牌楼校区', 'address': '四牌楼2号', 'district': '玄武区',
     'lng': 118.79531, 'lat': 32.05356, 'aliases': ('东南大学', '东大'),
     'visit': 60, 'kind': '校园'},
    {'name': '南京邮电大学仙林校区', 'address': '文苑路9号', 'district': '栖霞区',
     'lng': 118.93388, 'lat': 32.11299, 'aliases': ('南邮', '南京邮电大学'),
     'visit': 90, 'kind': '校园'},
    {'name': '南京邮电大学三牌楼校区', 'address': '新模范马路66号',
     'district': '鼓楼区', 'lng': 118.77142, 'lat': 32.08024,
     'aliases': ('三牌楼校区',), 'visit': 60, 'kind': '校园'},
    {'name': '南京图书馆', 'address': '中山东路189号', 'district': '玄武区',
     'lng': 118.79571, 'lat': 32.04195, 'aliases': (),
     'open': ('09:00', '17:30'), 'closed': ('周一',), 'visit': 120, 'kind': '场馆'},
    {'name': '德基广场', 'address': '中山路18号', 'district': '秦淮区',
     'lng': 118.78490, 'lat': 32.04408, 'aliases': (),
     'open': ('10:00', '22:00'), 'visit': 90, 'kind': '商圈'},
    {'name': '河西万达广场', 'address': '江东中路98号', 'district': '建邺区',
     'lng': 118.73849, 'lat': 32.03263, 'aliases': ('万达',),
     'open': ('10:00', '22:00'), 'visit': 90, 'kind': '商圈'},
    {'name': '奥体中心', 'address': '江东中路222号', 'district': '建邺区',
     'lng': 118.7355, 'lat': 32.0389, 'aliases': ('南京奥体中心',),
     'visit': 90, 'kind': '场馆'},
    {'name': '禄口国际机场', 'address': '禄口街道', 'district': '江宁区',
     'lng': 118.8626, 'lat': 31.7425, 'aliases': ('机场',), 'kind': '交通'},
    {'name': '南京眼步行桥', 'address': '江东中路', 'district': '建邺区',
     'lng': 118.7387, 'lat': 32.0255, 'aliases': (),
     'open': ('06:00', '22:00'), 'visit': 45, 'kind': '景点'},
    {'name': '雨花台烈士陵园', 'address': '雨花路215号', 'district': '雨花台区',
     'lng': 118.7925, 'lat': 31.9916, 'aliases': ('雨花台',),
     'open': ('08:00', '17:00'), 'closed': ('周一',), 'visit': 90, 'kind': '景点'},
    {'name': '侵华日军南京大屠杀遇难同胞纪念馆', 'address': '水西门大街418号',
     'district': '建邺区', 'lng': 118.74237, 'lat': 32.03522,
     'aliases': ('大屠杀纪念馆', '纪念馆'),
     'open': ('08:30', '16:30'), 'closed': ('周一',),
     'book': '需提前实名预约', 'visit': 120, 'kind': '场馆'},
    {'name': '紫峰大厦', 'address': '中山北路1号', 'district': '鼓楼区',
     'lng': 118.78319, 'lat': 32.06064, 'aliases': (),
     'open': ('09:00', '21:00'), 'visit': 60, 'kind': '景点'},
    {'name': '1912街区', 'address': '太平北路', 'district': '玄武区',
     'lng': 118.79578, 'lat': 32.04478, 'aliases': (),
     'open': ('11:00', '23:00'), 'visit': 90, 'kind': '街区'},
    {'name': '先锋书店五台山店', 'address': '广州路173号', 'district': '鼓楼区',
     'lng': 118.7731, 'lat': 32.0543, 'aliases': ('先锋书店',),
     'open': ('10:00', '21:00'), 'visit': 60, 'kind': '书店'},
    {'name': '江苏大剧院', 'address': '梦都大街181号', 'district': '建邺区',
     'lng': 118.71364, 'lat': 32.01243, 'aliases': (),
     'open': ('09:00', '22:00'), 'visit': 120, 'kind': '场馆'},
    {'name': '五台山体育馆', 'address': '广州路173号', 'district': '鼓楼区',
     'lng': 118.7748, 'lat': 32.0538, 'aliases': (),
     'open': ('08:00', '22:00'), 'visit': 90, 'kind': '场馆'},
    {'name': '湖南路', 'address': '湖南路', 'district': '鼓楼区',
     'lng': 118.77798, 'lat': 32.06901, 'aliases': (),
     'open': ('09:00', '22:00'), 'visit': 60, 'kind': '街区'},
    {'name': '龙江新城市广场', 'address': '草场门大街99号', 'district': '鼓楼区',
     'lng': 118.74119, 'lat': 32.05589, 'aliases': ('龙江',),
     'open': ('10:00', '22:00'), 'visit': 60, 'kind': '商圈'},
    {'name': '大报恩寺遗址公园', 'address': '雨花路1号', 'district': '秦淮区',
     'lng': 118.78307, 'lat': 32.00978, 'aliases': ('大报恩寺',),
     'open': ('08:30', '17:00'), 'visit': 90, 'kind': '景点'},
    # ---------------- 吃 / 喝 ----------------
    {'name': '南京大牌档德基店', 'address': '中山路18号德基广场', 'district': '秦淮区',
     'lng': 118.7803, 'lat': 32.0432, 'aliases': ('大牌档', '南京大牌档'),
     'open': ('11:00', '21:30'), 'visit': 80, 'kind': '餐饮',
     'cuisine': '金陵小吃', 'price': '人均约 ¥90'},
    {'name': '小厨娘淮扬菜新街口店', 'address': '中山路18号', 'district': '秦淮区',
     'lng': 118.7787, 'lat': 32.0416, 'aliases': ('小厨娘',),
     'open': ('11:00', '21:00'), 'visit': 85, 'kind': '餐饮',
     'cuisine': '淮扬菜', 'price': '人均约 ¥120'},
    {'name': '咸亨酒店夫子庙店', 'address': '贡院街', 'district': '秦淮区',
     'lng': 118.7909, 'lat': 32.0209, 'aliases': ('咸亨酒店', '咸亨'),
     'open': ('10:30', '21:00'), 'visit': 80, 'kind': '餐饮',
     'cuisine': '江浙菜', 'price': '人均约 ¥70'},
    {'name': '马祥兴菜馆', 'address': '云南北路32号', 'district': '鼓楼区',
     'lng': 118.77931, 'lat': 32.06640, 'aliases': ('马祥兴',),
     'open': ('11:00', '20:30'), 'visit': 80, 'kind': '餐饮',
     'cuisine': '清真京苏菜', 'price': '人均约 ¥100'},
    {'name': '绿柳居太平南路店', 'address': '太平南路248号', 'district': '秦淮区',
     'lng': 118.7900, 'lat': 32.0400, 'aliases': ('绿柳居',),
     'open': ('10:30', '20:00'), 'visit': 70, 'kind': '餐饮',
     'cuisine': '素食', 'price': '人均约 ¥60'},
    {'name': '鸡鸣汤包湖南路店', 'address': '湖南路', 'district': '鼓楼区',
     'lng': 118.7801, 'lat': 32.0697, 'aliases': ('鸡鸣汤包',),
     'open': ('06:30', '13:30'), 'visit': 40, 'kind': '餐饮',
     'cuisine': '早点小吃', 'price': '人均约 ¥25'},
    {'name': '李记清真馆评事街店', 'address': '评事街', 'district': '秦淮区',
     'lng': 118.7830, 'lat': 32.0330, 'aliases': ('李记', '李记清真馆'),
     'open': ('06:00', '14:00'), 'visit': 40, 'kind': '餐饮',
     'cuisine': '清真小吃', 'price': '人均约 ¥30'},
    {'name': '金陵饭店梅苑餐厅', 'address': '汉中路2号', 'district': '鼓楼区',
     'lng': 118.7790, 'lat': 32.0420, 'aliases': ('梅苑',),
     'open': ('11:00', '14:00'), 'visit': 90, 'kind': '餐饮',
     'cuisine': '淮扬菜', 'price': '人均约 ¥300'},
    {'name': '海底捞新街口店', 'address': '中山路18号', 'district': '秦淮区',
     'lng': 118.7790, 'lat': 32.0420, 'aliases': ('海底捞',),
     'open': ('10:00', '23:00'), 'visit': 100, 'kind': '餐饮',
     'cuisine': '火锅', 'price': '人均约 ¥130'},
    {'name': '星巴克德基广场店', 'address': '中山路18号德基广场', 'district': '秦淮区',
     'lng': 118.7805, 'lat': 32.0435, 'aliases': ('星巴克', '德基星巴克'),
     'open': ('07:30', '22:00'), 'visit': 45, 'kind': '餐饮',
     'cuisine': '咖啡', 'price': '人均约 ¥40'},
    {'name': '喜茶德基广场店', 'address': '中山路18号德基广场', 'district': '秦淮区',
     'lng': 118.7807, 'lat': 32.0434, 'aliases': ('喜茶',),
     'open': ('10:00', '22:00'), 'visit': 30, 'kind': '餐饮',
     'cuisine': '茶饮', 'price': '人均约 ¥30'},
    # ---------------- 住 ----------------
    {'name': '金陵饭店', 'address': '汉中路2号', 'district': '鼓楼区',
     'lng': 118.78287, 'lat': 32.04241, 'aliases': ('金陵', '南京金陵饭店'),
     'kind': '住宿', 'checkin': '14:00', 'checkout': '12:00',
     'price': '约 ¥800 起/晚', 'star': '五星'},
    {'name': '南京中心大酒店', 'address': '中山路75号', 'district': '秦淮区',
     'lng': 118.78343, 'lat': 32.04543, 'aliases': ('中心大酒店',),
     'kind': '住宿', 'checkin': '14:00', 'checkout': '12:00',
     'price': '约 ¥500 起/晚', 'star': '四星'},
    {'name': '锦江之星新街口店', 'address': '中山南路', 'district': '秦淮区',
     'lng': 118.7780, 'lat': 32.0400, 'aliases': ('锦江之星',),
     'kind': '住宿', 'checkin': '14:00', 'checkout': '12:00',
     'price': '约 ¥260 起/晚', 'star': '经济型'},
    {'name': '汉庭酒店夫子庙店', 'address': '建康路', 'district': '秦淮区',
     'lng': 118.7912, 'lat': 32.0215, 'aliases': ('汉庭',),
     'kind': '住宿', 'checkin': '14:00', 'checkout': '12:00',
     'price': '约 ¥280 起/晚', 'star': '经济型'},
    {'name': '南京南站亚朵酒店', 'address': '江南路', 'district': '雨花台区',
     'lng': 118.78832, 'lat': 31.96654, 'aliases': ('亚朵', '南站亚朵'),
     'kind': '住宿', 'checkin': '14:00', 'checkout': '12:00',
     'price': '约 ¥380 起/晚', 'star': '中档'},
    {'name': '玄武湖假日酒店', 'address': '中央路', 'district': '玄武区',
     'lng': 118.81593, 'lat': 32.06647, 'aliases': ('假日酒店',),
     'kind': '住宿', 'checkin': '14:00', 'checkout': '12:00',
     'price': '约 ¥450 起/晚', 'star': '中档'},
    # ---------------- 2026-10 补充：高频点位（坐标双源核对，营业时间取高德 biz_ext）
    # 「苜蓿园美食街」在高德侧没有对应 POI（检索会命中附近小吃店/夜市），
    # 故收录同一条大街的道路级点位，别名保留用户说法，避免解析跑偏到几公里外。
    {'name': '苜蓿园大街', 'address': '苜蓿园大街（美食街沿街）', 'district': '秦淮区',
     'lng': 118.8311, 'lat': 32.02874,
     'aliases': ('苜蓿园美食街', '苜蓿园'), 'visit': 40, 'kind': '街区'},
    # 高德条目名为「颐和路公馆区第十二片区」（4A 景区形态），
    # 坐标由 place/text 与 geocode(颐和路)→regeo AOI 两路印证。
    {'name': '颐和路民国公馆区', 'address': '颐和路（第十二片区）', 'district': '鼓楼区',
     'lng': 118.77009, 'lat': 32.06398,
     'aliases': ('颐和路公馆区', '颐和路公馆区第十二片区', '颐和路'),
     'visit': 60, 'kind': '景点'},
    {'name': '朝天宫', 'address': '朝天宫4号', 'district': '秦淮区',
     'lng': 118.77532, 'lat': 32.03434, 'aliases': ('南京市博物馆',),
     'open': ('09:00', '18:00'), 'closed': ('周一',), 'visit': 90, 'kind': '景点'},
    {'name': '瞻园', 'address': '瞻园路128号', 'district': '秦淮区',
     'lng': 118.78516, 'lat': 32.02103, 'aliases': ('太平天国历史博物馆',),
     'open': ('08:30', '17:00'), 'visit': 60, 'kind': '景点'},
    {'name': '中国科举博物馆', 'address': '贡院街95号（江南贡院）', 'district': '秦淮区',
     'lng': 118.79067, 'lat': 32.02079,
     'aliases': ('江南贡院', '科举博物馆'), 'open': ('09:00', '22:00'),
     'visit': 90, 'kind': '场馆'},
    {'name': '中华门瓮城', 'address': '中华路（中华门）', 'district': '秦淮区',
     'lng': 118.78182, 'lat': 32.0128, 'aliases': ('中华门', '中华门城堡'),
     'open': ('08:30', '22:00'), 'book': '日场 08:30 起，夜场开放至 22:00',
     'visit': 60, 'kind': '景点'},
    {'name': '六朝博物馆', 'address': '长江路302号', 'district': '玄武区',
     'lng': 118.79912, 'lat': 32.04284, 'aliases': (),
     'open': ('09:00', '17:30'), 'visit': 90, 'kind': '场馆'},
    {'name': '阅江楼', 'address': '建宁路202号（狮子山）', 'district': '鼓楼区',
     'lng': 118.74854, 'lat': 32.09386, 'aliases': (), 'visit': 60, 'kind': '景点'},
    {'name': '燕子矶公园', 'address': '燕子矶街（幕燕滨江）', 'district': '栖霞区',
     'lng': 118.82716, 'lat': 32.13656, 'aliases': ('燕子矶',), 'visit': 60,
     'kind': '公园'},
    {'name': '牛首山文化旅游区', 'address': '宁丹大道18号', 'district': '江宁区',
     'lng': 118.74415, 'lat': 31.90331, 'aliases': ('牛首山', '佛顶宫'),
     'open': ('08:30', '17:30'), 'book': '16:30 停止入园', 'visit': 180,
     'kind': '景点'},
    # 甘熙宅第与熙南里相邻（约 130 m），同属南捕厅—熙南里街区；
    # 坐标由 place/text 与 geocode(中山南路南捕厅19号)→regeo AOI 双源印证。
    {'name': '甘熙宅第', 'address': '中山南路南捕厅19号', 'district': '秦淮区',
     'lng': 118.78176, 'lat': 32.02592,
     'aliases': ('南京市民俗博物馆', '甘熙故居'),
     'open': ('09:00', '17:30'), 'closed': ('周一',),
     'book': '最晚 17:00 入园；夜场至 21:00', 'visit': 50, 'indoor': True,
     'kind': '景点'},
    {'name': '熙南里', 'address': '中山南路（熙南里历史文化街区）', 'district': '秦淮区',
     'lng': 118.78134, 'lat': 32.02497,
     'aliases': ('熙南里历史文化街区', '熙南里街区'),
     'book': '街区 24 小时开放', 'visit': 40, 'kind': '街区'},
    {'name': '南京审计大学莫愁校区', 'address': '汉中门大街34号', 'district': '鼓楼区',
     'lng': 118.75186, 'lat': 32.03963,
     'aliases': ('南京审计大学', '南审', '南审莫愁校区'), 'visit': 60, 'kind': '校园'},
)


# ======================================================================
# 北京市离线 POI
# ======================================================================
BEIJING_POIS: tuple[dict, ...] = (
    {'name': '故宫博物院', 'address': '景山前街4号', 'district': '东城区',
     'lng': 116.39703, 'lat': 39.91784, 'aliases': ('故宫', '紫禁城'),
     'open': ('08:30', '17:00'), 'closed': ('周一',),
     'book': '需提前实名预约（旺季约满很快）', 'visit': 180, 'kind': '景点'},
    {'name': '天安门广场', 'address': '东长安街', 'district': '东城区',
     'lng': 116.39776, 'lat': 39.90318, 'aliases': ('天安门',),
     'open': ('05:00', '22:00'), 'book': '需实名预约入广场', 'visit': 45,
     'kind': '景点'},
    {'name': '中国国家博物馆', 'address': '东长安街16号', 'district': '东城区',
     'lng': 116.40130, 'lat': 39.90537, 'aliases': ('国博', '国家博物馆'),
     'open': ('09:00', '17:00'), 'closed': ('周一',),
     'book': '需提前实名预约', 'visit': 180, 'kind': '场馆'},
    {'name': '天坛公园', 'address': '天坛路甲1号', 'district': '东城区',
     'lng': 116.4109, 'lat': 39.8822, 'aliases': ('天坛',),
     'open': ('06:00', '22:00'), 'visit': 120, 'kind': '景点'},
    {'name': '颐和园', 'address': '新建宫门路19号', 'district': '海淀区',
     'lng': 116.27518, 'lat': 39.99962, 'aliases': (),
     'open': ('06:30', '18:00'), 'visit': 150, 'kind': '景点'},
    {'name': '圆明园', 'address': '清华西路28号', 'district': '海淀区',
     'lng': 116.2980, 'lat': 40.0084, 'aliases': (),
     'open': ('07:00', '19:00'), 'visit': 120, 'kind': '景点'},
    {'name': '八达岭长城', 'address': '八达岭镇', 'district': '延庆区',
     'lng': 116.01527, 'lat': 40.36144, 'aliases': ('长城', '八达岭'),
     'open': ('06:30', '19:00'), 'book': '需提前预约购票',
     'visit': 240, 'kind': '景点'},
    {'name': '国家体育场', 'address': '国家体育场南路1号', 'district': '朝阳区',
     'lng': 116.39578, 'lat': 39.99333, 'aliases': ('鸟巢',),
     'open': ('09:00', '18:00'), 'visit': 90, 'kind': '场馆'},
    {'name': '国家游泳中心', 'address': '天辰东路11号', 'district': '朝阳区',
     'lng': 116.3903, 'lat': 39.9927, 'aliases': ('水立方',),
     'open': ('09:00', '18:00'), 'visit': 60, 'kind': '场馆'},
    {'name': '798艺术区', 'address': '酒仙桥路2号', 'district': '朝阳区',
     'lng': 116.4959, 'lat': 39.9843, 'aliases': ('798',),
     'open': ('10:00', '18:00'), 'closed': ('周一',), 'visit': 120,
     'kind': '街区'},
    {'name': '三里屯太古里', 'address': '三里屯路19号', 'district': '朝阳区',
     'lng': 116.45399, 'lat': 39.93487, 'aliases': ('三里屯',),
     'open': ('10:00', '22:00'), 'visit': 90, 'kind': '商圈'},
    {'name': '王府井大街', 'address': '王府井大街', 'district': '东城区',
     'lng': 116.41112, 'lat': 39.91618, 'aliases': ('王府井',),
     'open': ('09:30', '22:00'), 'visit': 90, 'kind': '街区'},
    {'name': '南锣鼓巷', 'address': '南锣鼓巷', 'district': '东城区',
     'lng': 116.4032, 'lat': 39.9376, 'aliases': (),
     'open': ('09:00', '22:00'), 'visit': 90, 'kind': '街区'},
    {'name': '什刹海', 'address': '前海西街', 'district': '西城区',
     'lng': 116.38512, 'lat': 39.94189, 'aliases': ('后海',),
     'open': ('00:00', '23:59'), 'visit': 90, 'kind': '景点'},
    {'name': '前门大街', 'address': '前门大街', 'district': '东城区',
     'lng': 116.40390, 'lat': 39.89615, 'aliases': ('前门',),
     'open': ('09:00', '22:00'), 'visit': 90, 'kind': '街区'},
    {'name': '恭王府', 'address': '前海西街17号', 'district': '西城区',
     'lng': 116.38631, 'lat': 39.93722, 'aliases': (),
     'open': ('08:30', '17:00'), 'closed': ('周一',),
     'book': '需提前预约', 'visit': 90, 'kind': '景点'},
    {'name': '雍和宫', 'address': '雍和宫大街12号', 'district': '东城区',
     'lng': 116.41730, 'lat': 39.94724, 'aliases': (),
     'open': ('09:00', '16:30'), 'book': '需提前预约', 'visit': 60,
     'kind': '景点'},
    {'name': '北海公园', 'address': '文津街1号', 'district': '西城区',
     'lng': 116.39180, 'lat': 39.92878, 'aliases': (),
     'open': ('06:30', '20:00'), 'visit': 90, 'kind': '公园'},
    {'name': '景山公园', 'address': '景山西街44号', 'district': '西城区',
     'lng': 116.39655, 'lat': 39.92587, 'aliases': ('景山',),
     'open': ('06:30', '21:00'), 'visit': 60, 'kind': '公园'},
    {'name': '北京动物园', 'address': '西直门外大街137号', 'district': '西城区',
     'lng': 116.33670, 'lat': 39.94210, 'aliases': ('动物园',),
     'open': ('07:30', '18:00'), 'visit': 150, 'kind': '公园'},
    {'name': '首都博物馆', 'address': '复兴门外大街16号', 'district': '西城区',
     'lng': 116.34207, 'lat': 39.90641, 'aliases': ('首博',),
     'open': ('09:00', '17:00'), 'closed': ('周一',),
     'book': '需提前预约', 'visit': 120, 'kind': '场馆'},
    {'name': '中国科学技术馆', 'address': '北辰东路5号', 'district': '朝阳区',
     'lng': 116.39860, 'lat': 40.00602, 'aliases': ('科技馆',),
     'open': ('09:30', '17:00'), 'closed': ('周一',),
     'book': '需提前预约购票', 'visit': 180, 'kind': '场馆'},
    {'name': '北京天文馆', 'address': '西直门外大街138号', 'district': '西城区',
     'lng': 116.33683, 'lat': 39.93731, 'aliases': ('天文馆',),
     'open': ('09:00', '16:30'), 'closed': ('周一', '周二'), 'visit': 120,
     'kind': '场馆'},
    {'name': '国家大剧院', 'address': '西长安街2号', 'district': '西城区',
     'lng': 116.38981, 'lat': 39.90491, 'aliases': (),
     'open': ('09:00', '17:00'), 'visit': 90, 'kind': '场馆'},
    {'name': '北京环球影城', 'address': '京哈高速与东六环交汇', 'district': '通州区',
     'lng': 116.6747, 'lat': 39.8557, 'aliases': ('环球影城', '环球'),
     'open': ('09:00', '21:00'), 'book': '需提前购票并预约入园',
     'visit': 480, 'kind': '景点'},
    {'name': '奥林匹克森林公园', 'address': '科荟路33号', 'district': '朝阳区',
     'lng': 116.39216, 'lat': 40.01864, 'aliases': ('奥森',),
     'open': ('06:00', '21:00'), 'visit': 120, 'kind': '公园'},
    {'name': '北京站', 'address': '毛家湾胡同甲13号', 'district': '东城区',
     'lng': 116.4270, 'lat': 39.9029, 'aliases': (), 'kind': '交通'},
    {'name': '北京南站', 'address': '永外大街12号', 'district': '丰台区',
     'lng': 116.3786, 'lat': 39.8652, 'aliases': ('南站',), 'kind': '交通'},
    {'name': '北京西站', 'address': '莲花池东路118号', 'district': '丰台区',
     'lng': 116.3215, 'lat': 39.8949, 'aliases': ('西站',), 'kind': '交通'},
    {'name': '首都国际机场', 'address': '首都机场路', 'district': '顺义区',
     'lng': 116.6039, 'lat': 40.0525, 'aliases': ('首都机场', 'T3'),
     'kind': '交通'},
    {'name': '大兴国际机场', 'address': '大兴区榆垡镇', 'district': '大兴区',
     'lng': 116.4107, 'lat': 39.5097, 'aliases': ('大兴机场',), 'kind': '交通'},
    {'name': '北京大学', 'address': '颐和园路5号', 'district': '海淀区',
     'lng': 116.3105, 'lat': 39.9927, 'aliases': ('北大',),
     'book': '入校需预约', 'visit': 90, 'kind': '校园'},
    {'name': '清华大学', 'address': '双清路30号', 'district': '海淀区',
     'lng': 116.3264, 'lat': 40.0031, 'aliases': ('清华',),
     'book': '入校需预约', 'visit': 90, 'kind': '校园'},
    {'name': '中关村', 'address': '中关村大街', 'district': '海淀区',
     'lng': 116.32167, 'lat': 39.98527, 'aliases': (),
     'open': ('10:00', '21:00'), 'visit': 60, 'kind': '商圈'},
    {'name': '五道口', 'address': '成府路', 'district': '海淀区',
     'lng': 116.3376, 'lat': 39.9926, 'aliases': (),
     'open': ('10:00', '22:00'), 'visit': 45, 'kind': '街区'},
    {'name': '国贸CBD', 'address': '建国门外大街1号', 'district': '朝阳区',
     'lng': 116.45885, 'lat': 39.90986, 'aliases': ('国贸',),
     'open': ('09:00', '22:00'), 'visit': 60, 'kind': '商圈'},
    {'name': '北京协和医院', 'address': '帅府园1号', 'district': '东城区',
     'lng': 116.4166, 'lat': 39.9126, 'aliases': ('协和医院', '协和'),
     'kind': '医疗'},
    {'name': '全聚德前门店', 'address': '前门大街30号', 'district': '东城区',
     'lng': 116.3958, 'lat': 39.8990, 'aliases': ('全聚德',),
     'open': ('11:00', '20:30'), 'book': '热门时段建议提前排号',
     'visit': 90, 'kind': '餐饮'},
    {'name': '便宜坊崇文门店', 'address': '崇文门外大街16号', 'district': '东城区',
     'lng': 116.4180, 'lat': 39.9020, 'aliases': ('便宜坊',),
     'open': ('11:00', '20:30'), 'visit': 90, 'kind': '餐饮',
     'cuisine': '烤鸭', 'price': '人均约 ¥150'},
    # ---------------- 吃 / 喝（补充） ----------------
    {'name': '四季民福烤鸭店故宫店', 'address': '南池子大街11号', 'district': '东城区',
     'lng': 116.4025, 'lat': 39.9200, 'aliases': ('四季民福',),
     'open': ('10:30', '21:30'), 'visit': 90, 'kind': '餐饮',
     'cuisine': '烤鸭', 'price': '人均约 ¥180'},
    {'name': '姚记炒肝鼓楼店', 'address': '鼓楼东大街311号', 'district': '东城区',
     'lng': 116.3945, 'lat': 39.9420, 'aliases': ('姚记炒肝', '姚记'),
     'open': ('06:00', '21:00'), 'visit': 45, 'kind': '餐饮',
     'cuisine': '北京小吃', 'price': '人均约 ¥35'},
    {'name': '护国寺小吃护国寺店', 'address': '护国寺大街93号', 'district': '西城区',
     'lng': 116.3811, 'lat': 39.9376, 'aliases': ('护国寺小吃', '护国寺'),
     'open': ('06:00', '21:00'), 'visit': 45, 'kind': '餐饮',
     'cuisine': '北京小吃', 'price': '人均约 ¥30'},
    {'name': '南门涮肉前门店', 'address': '前门大街', 'district': '东城区',
     'lng': 116.3975, 'lat': 39.8990, 'aliases': ('南门涮肉',),
     'open': ('11:00', '22:00'), 'visit': 90, 'kind': '餐饮',
     'cuisine': '涮羊肉', 'price': '人均约 ¥120'},
    {'name': '大董烤鸭团结湖店', 'address': '团结湖北口', 'district': '朝阳区',
     'lng': 116.4460, 'lat': 39.9330, 'aliases': ('大董',),
     'open': ('11:00', '22:00'), 'visit': 95, 'kind': '餐饮',
     'cuisine': '烤鸭', 'price': '人均约 ¥300'},
    {'name': '老北京炸酱面前门店', 'address': '前门大街', 'district': '东城区',
     'lng': 116.3968, 'lat': 39.8995, 'aliases': ('炸酱面',),
     'open': ('11:00', '21:00'), 'visit': 50, 'kind': '餐饮',
     'cuisine': '面食', 'price': '人均约 ¥40'},
    {'name': '胡大饭馆簋街店', 'address': '东直门内大街', 'district': '东城区',
     'lng': 116.4230, 'lat': 39.9420, 'aliases': ('胡大', '簋街'),
     'open': ('11:00', '23:59'), 'visit': 100, 'kind': '餐饮',
     'cuisine': '小龙虾', 'price': '人均约 ¥150'},
    {'name': '星巴克王府井店', 'address': '王府井大街', 'district': '东城区',
     'lng': 116.4100, 'lat': 39.9145, 'aliases': ('星巴克',),
     'open': ('07:00', '22:00'), 'visit': 45, 'kind': '餐饮',
     'cuisine': '咖啡', 'price': '人均约 ¥40'},
    {'name': '喜茶三里屯店', 'address': '三里屯路19号', 'district': '朝阳区',
     'lng': 116.4545, 'lat': 39.9340, 'aliases': ('喜茶',),
     'open': ('10:00', '22:00'), 'visit': 30, 'kind': '餐饮',
     'cuisine': '茶饮', 'price': '人均约 ¥30'},
    # ---------------- 住 ----------------
    {'name': '北京饭店', 'address': '东长安街33号', 'district': '东城区',
     'lng': 116.4100, 'lat': 39.9090, 'aliases': ('北京饭店',),
     'kind': '住宿', 'checkin': '14:00', 'checkout': '12:00',
     'price': '约 ¥1200 起/晚', 'star': '五星'},
    {'name': '王府井希尔顿酒店', 'address': '王府井东街8号', 'district': '东城区',
     'lng': 116.41331, 'lat': 39.91484, 'aliases': ('希尔顿', '王府井希尔顿'),
     'kind': '住宿', 'checkin': '15:00', 'checkout': '12:00',
     'price': '约 ¥1100 起/晚', 'star': '五星'},
    {'name': '前门建国饭店', 'address': '永安路175号', 'district': '东城区',
     'lng': 116.38579, 'lat': 39.88678, 'aliases': ('建国饭店',),
     'kind': '住宿', 'checkin': '14:00', 'checkout': '12:00',
     'price': '约 ¥650 起/晚', 'star': '四星'},
    {'name': '如家酒店前门店', 'address': '前门西大街', 'district': '东城区',
     'lng': 116.3990, 'lat': 39.9010, 'aliases': ('如家',),
     'kind': '住宿', 'checkin': '14:00', 'checkout': '12:00',
     'price': '约 ¥300 起/晚', 'star': '经济型'},
    {'name': '汉庭酒店王府井店', 'address': '王府井大街', 'district': '东城区',
     'lng': 116.4110, 'lat': 39.9140, 'aliases': ('汉庭',),
     'kind': '住宿', 'checkin': '14:00', 'checkout': '12:00',
     'price': '约 ¥350 起/晚', 'star': '经济型'},
    {'name': '亚朵酒店三里屯店', 'address': '工人体育场北路', 'district': '朝阳区',
     'lng': 116.4555, 'lat': 39.9345, 'aliases': ('亚朵',),
     'kind': '住宿', 'checkin': '14:00', 'checkout': '12:00',
     'price': '约 ¥500 起/晚', 'star': '中档'},
    {'name': '全季酒店北京南站店', 'address': '永外大街', 'district': '丰台区',
     'lng': 116.36500, 'lat': 39.86045, 'aliases': ('全季',),
     'kind': '住宿', 'checkin': '14:00', 'checkout': '12:00',
     'price': '约 ¥420 起/晚', 'star': '中档'},
    # ---------------- 2026-10 补充：西山一线 + 高校起点（坐标双源核对）
    # 营业时间说明：高德 biz_ext 对季节性景区常给出多段（旺季/淡季）字符串，
    # 这里按"11 月所在淡季"取一对保守值，原始串写进 book 便于追溯。
    {'name': '国家植物园北园', 'address': '香山路与卧佛寺路交叉口', 'district': '海淀区',
     'lng': 116.20938, 'lat': 40.0018,
     'aliases': ('植物园北园', '北京植物园', '国家植物园（北园）', '国家植物园（北园'),
     'open': ('06:30', '18:00'),
     'book': '淡季(11/16-3/15) 6:30-18:00；旺季 6:00-20:00', 'visit': 120,
     'kind': '公园'},
    {'name': '国家植物园南园', 'address': '香山南辛村20号', 'district': '海淀区',
     'lng': 116.21572, 'lat': 39.98995,
     'aliases': ('植物园南园', '国家植物园（南园）', '国家植物园（南园'),
     'open': ('08:00', '17:00'),
     'book': '淡季(11/16-3/15) 8:00-17:00；旺季 8:00-18:00', 'visit': 90,
     'kind': '公园'},
    {'name': '香山公园', 'address': '香山买卖街40号', 'district': '海淀区',
     'lng': 116.18875, 'lat': 39.99011, 'aliases': ('香山',),
     'open': ('06:00', '19:00'), 'visit': 150, 'kind': '公园'},
    {'name': '八大处公园', 'address': '八大处路3号', 'district': '石景山区',
     'lng': 116.18436, 'lat': 39.96268, 'aliases': ('八大处',),
     'open': ('06:00', '17:30'), 'visit': 120, 'kind': '公园'},
    {'name': '模式口大街', 'address': '模式口大街（法海寺南麓）', 'district': '石景山区',
     'lng': 116.16194, 'lat': 39.93509, 'aliases': ('模式口古街', '模式口'),
     'visit': 50, 'kind': '街区'},
    {'name': '卧佛寺', 'address': '卧佛寺路（国家植物园北园内）', 'district': '海淀区',
     'lng': 116.20722, 'lat': 40.00749, 'aliases': ('十方普觉寺',),
     'open': ('08:30', '17:00'), 'closed': ('周一',),
     'book': '淡季(11/16-3/15) 8:30-17:00', 'visit': 60, 'kind': '景点'},
    {'name': '碧云寺', 'address': '买卖街40号（香山公园内）', 'district': '海淀区',
     'lng': 116.19136, 'lat': 39.99748, 'aliases': (),
     'open': ('08:30', '17:00'), 'visit': 60, 'kind': '景点'},
    {'name': '法海寺', 'address': '翠微山南麓模式口48号', 'district': '石景山区',
     'lng': 116.15946, 'lat': 39.94148, 'aliases': (),
     'open': ('09:00', '16:00'), 'closed': ('周一',), 'visit': 60, 'kind': '景点'},
    {'name': '西山国家森林公园', 'address': '闵庄路与香山南路交叉口西100米',
     'district': '海淀区', 'lng': 116.19306, 'lat': 39.97519,
     'aliases': ('西山森林公园',), 'open': ('07:00', '20:00'), 'visit': 150,
     'kind': '公园'},
    {'name': '北京理工大学中关村校区', 'address': '中关村南大街5号',
     'district': '海淀区', 'lng': 116.31547, 'lat': 39.95998,
     'aliases': ('北理工', '北京理工大学'), 'visit': 60, 'kind': '校园'},
)


# ======================================================================
# 城市包
# ======================================================================
@dataclass(frozen=True)
class CityPack:
    """一座城市的全部"可替换数据"：行政区、离线 POI、默认方式、示例话术、主题。

    ``label`` 是城市在界面上的文化称谓（南京=金陵、北京=京城），
    ``theme`` 是覆盖设计令牌的少量键（强调色族与底色），
    所以"换城市"连视觉风格一起换，却不需要改任何一条样式规则。
    """

    name: str
    short: str
    districts: tuple[str, ...]
    center: tuple[float, float]
    label: str = ''
    default_mode: str = 'driving'
    #: 高德 adcode（天气接口按行政区编码取数；空串表示不取天气）
    adcode: str = ''
    peaks: tuple = _DEFAULT_PEAKS
    sample: str = ''
    pois: tuple = ()
    note: str = ''
    theme: dict = None

    # ---------- 派生 ----------
    def poi_table(self) -> list[dict]:
        """离线 POI 表（返回浅拷贝，调用方可安全修改）"""
        return [dict(p) for p in self.pois]

    @property
    def title_label(self) -> str:
        """界面称谓：优先文化称谓（金陵/京城），否则用城市名"""
        return self.label or self.name

    def aliases(self) -> dict[str, str]:
        """简称 → 标准名（离线解析用）"""
        out: dict[str, str] = {}
        for p in self.pois:
            for a in p.get('aliases', ()):
                out[a] = p['name']
        return out

    def names(self) -> list[str]:
        return [p['name'] for p in self.pois]

    def in_city(self, district: str) -> bool:
        """行政区/区县归属校验（BR-01 的参数化实现）"""
        d = district or ''
        return any(x in d for x in self.districts)

    def find(self, name: str) -> Optional[dict]:
        """按标准名或别名取 POI 数据（含营业时间）"""
        key = (name or '').strip()
        if not key:
            return None
        for p in self.pois:
            if p['name'] == key or key in p.get('aliases', ()):
                return dict(p)
        return None


from .packs_city import CITY_POIS
from .packs_metro import METRO_POIS
from .packs_pool import POOL_POIS
from .packs_xianlin import XIANLIN_POIS

NANJING_PACK = CityPack(
    name='南京', short='宁', label='金陵',
    districts=('玄武区', '秦淮区', '建邺区', '鼓楼区', '浦口区', '栖霞区',
               '雨花台区', '江宁区', '六合区', '溧水区', '高淳区'),
    center=(118.7969, 32.0603),
    default_mode='driving',
    adcode='320100',
    sample='明天上午九点去南京博物院看展，大概2小时',
    pois=NANJING_POIS + XIANLIN_POIS + POOL_POIS + CITY_POIS + METRO_POIS,
    note='离线包为演示用近似数据，开放时间与预约要求请以官方为准。',
    # 金陵：城墙青灰底 + 梧桐绿，克制、偏冷、有旧都的书卷气
    theme={
        'accent': '#3A7D5C', 'accent.hover': '#2F6A4C', 'accent.tint': '#E6EFE9',
        'bg.canvas': '#F2F1EE', 'bg.sunken': '#E8E6E1',
        'line.subtle': '#DCD9D2', 'line.strong': '#B8B4AA',
        'mode.transit': '#3A7D5C',
        # 深色方向（D）用的提亮版：梧桐绿在深底上必须提亮才看得见
        'accent.dark': '#56CFA0', 'accent.dark.hover': '#6FDDB2',
        'accent.dark.tint': '#12312A',
    },
)

BEIJING_PACK = CityPack(
    name='北京', short='京', label='京城',
    districts=('东城区', '西城区', '朝阳区', '海淀区', '丰台区', '石景山区',
               '门头沟区', '房山区', '通州区', '顺义区', '昌平区', '大兴区',
               '怀柔区', '平谷区', '密云区', '延庆区'),
    center=(116.4074, 39.9042),
    default_mode='transit',          # 北京地铁覆盖好，默认推公交地铁
    adcode='110000',
    # 北京早晚高峰比南京更早、更集中
    peaks=((7 * 60, 9 * 60 + 30, '早高峰'), (17 * 60, 19 * 60 + 30, '晚高峰')),
    sample='明天上午八点半去故宫看展，大概3小时',
    pois=BEIJING_POIS,
    note='故宫、国博、科技馆等周一闭馆且需实名预约；'
         '开放时间与预约要求请以官方发布为准。',
    # 京城：宫墙红 + 暖灰底，比金陵更暖、更重，呼应红墙琉璃瓦
    theme={
        'accent': '#9C3B2E', 'accent.hover': '#853126', 'accent.tint': '#F7E9E6',
        'bg.canvas': '#F5F1EA', 'bg.sunken': '#EBE4DA',
        'line.subtle': '#DFD8CC', 'line.strong': '#BDB2A2',
        'mode.transit': '#9C3B2E',
        # 深色方向（D）用的提亮版：宫墙红在深底上必须提亮
        'accent.dark': '#FF7A66', 'accent.dark.hover': '#FF9585',
        'accent.dark.tint': '#3A1E1A',
    },
)

CITY_PACKS: dict[str, CityPack] = {
    NANJING_PACK.name: NANJING_PACK,
    BEIJING_PACK.name: BEIJING_PACK,
}


# ======================================================================
# 注册表访问
# ======================================================================
#: 名字里带这些词的点位不是"游玩目的地"：家居、建材、专卖、超市、装修中……
#: 高德的类别归属并不严格（家具店能被归进"街区"），所以推荐候选要按名字再筛一道
NON_LEISURE_WORDS = (
    '家居', '家具', '建材', '家装', '电器', '五金', '专卖', '超市', '便利店', '菜场',
    '农贸', '药店', '装修中', '施工中', '暂停营业', '瓷砖', '卫浴', '地板', '灯具',
    '窗帘', '木门', '空调', '源氏木语', '红星美凯龙', '居然之家', '月星家居',
)

#: 门店/场馆常见后缀：去掉后剩下的就是品牌，用于"同一连锁只推荐一家"
_BRANCH_SUFFIX = ('购物中心', '生活中心', '中心', '广场', '店')


def is_recommendable(name: str) -> bool:
    """这个点位值不值得推荐给人去玩（家居卖场、装修中的商场都不值得）"""
    text = str(name or '')
    return bool(text) and not any(w in text for w in NON_LEISURE_WORDS)


def brand_key(name: str) -> str:
    """品牌键：去掉括号里的分店信息与"店/广场/中心"等后缀"""
    text = re.sub(r'[（(][^）)]*[）)]', '', str(name or '')).strip()
    for suffix in _BRANCH_SUFFIX:
        if text.endswith(suffix) and len(text) > len(suffix) + 1:
            text = text[: -len(suffix)].strip()
    return text


def dedupe_brands(pois: list) -> list:
    """同一连锁/品牌只保留**排在前面的那一个**（调用方已按评分或距离排好序）"""
    seen: set = set()
    out = []
    for poi in pois:
        key = brand_key(getattr(poi, 'name', '') or '')
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(poi)
    return out


def city_names() -> list[str]:
    """可选城市列表（界面切换器直接用）"""
    return list(CITY_PACKS.keys())


#: 常见片区名（行政区之下、商圈一级的说法）。城市包只存行政区，
#: 但用户与地图数据常用"河西""仙林"这种片区，名称相似度判定要认得它们。
_PLACE_AREAS = ('河西', '新街口', '夫子庙', '仙林', '江宁', '江北', '城南', '城北',
                '城东', '城西', '城中', '下关', '龙江', '奥体', '迈皋桥', '马群',
                '孝陵卫', '汤山', '燕子矶', '滨江', '老山', '板桥', '汤泉')


def place_qualifiers() -> frozenset:
    """"南京""玄武""河西"这类可以加在地名前头的限定语。

    用途：判断"南京晨光1865创意产业园"与"晨光1865创意产业园"是不是同一处
    （多出来的只是地名限定语）。反过来"明故宫"的"明"不在表里——那是另一个地方。
    """
    words = set(_PLACE_AREAS)
    for name, pack in CITY_PACKS.items():
        words.add(name)
        words.add(name + '市')
        for district in pack.districts:
            words.add(district)
            words.add(district.rstrip('区市'))
    words.discard('')
    return frozenset(words)


def get_city(name: str = '') -> CityPack:
    """按名称取城市包；未知城市回落到默认城市（不抛异常，保证界面不崩）"""
    return CITY_PACKS.get((name or '').strip(), CITY_PACKS[DEFAULT_CITY])


def normalize_city(name: str = '') -> str:
    """把任意输入规范成已注册城市名"""
    return get_city(name).name

