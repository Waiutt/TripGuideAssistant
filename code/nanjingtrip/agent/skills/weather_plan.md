---
name: weather_plan
title: 天气与室内外备选
when_to_use:
  - 用户问天气、问下雨怎么办
inputs:
  - 日期（当天或未来 3 天内）
outputs:
  - 天气与温度，是否建议改室内
  - 下雨时的备选点
impl: nanjingtrip.services:TripService.day_weather
needs_network: true
fallback: 拿不到预报就直说拿不到，不猜天气
limits:
  - 只有当天+3天有真实预报，超出范围要明确说明
---
天气影响的是"顺序与备选"，不是"删掉用户想去的地方"。
