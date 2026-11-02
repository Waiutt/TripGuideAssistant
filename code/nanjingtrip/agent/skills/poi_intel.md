---
name: poi_intel
title: 取真实开放时间、票价与游玩耗时
when_to_use:
  - 需要景点的真实营业时间、票价、评分
  - 判断"这个点大概要玩多久"
inputs:
  - 入选地点（只需对最终入选的点取数，不要全量预取）
outputs:
  - 开放时间、票价、评分、最低价
  - 游玩时长证据（来源、区间、置信度）
impl: nanjingtrip.intel:PoiIntelService.apply
needs_network: true
fallback: 无网络时用城市包 visit_min 与证据表兜底，并标明是估算
limits:
  - 高德天气只有今天+3天，更远只能说"到时再看"
  - 绝不把估算值写成实测值
---
取数只对入选点做：候选有几十个，全量取一次要几十秒，得不偿失。
