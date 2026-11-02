---
name: place_lookup
title: 地名核验与纠错
when_to_use:
  - 用户说的是简称、带"地铁站"后缀、或听起来像地址
  - 怀疑地图匹配到了同名的另一个地方
inputs:
  - 用户说的地名原文
outputs:
  - 城市包或高德解析出的标准地点
  - 匹配不上时给出"待补地点"与补全建议
impl: nanjingtrip.services:TripService._poi_named
needs_network: true
fallback: 匹配不上就留成待补地点，不用相近的地方顶替
limits:
  - 相似度不够一律判为待补，不许凑一个"看起来像"的地点
  - 只有像地址的串才走高德地理编码，普通地名先查城市包
---
这是"防臆造"的关键一环：宁可让用户补一句，也不要把人带到同名的错误地点。
