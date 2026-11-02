---
name: fit_day
title: 这天太满时给取舍
when_to_use:
  - 用户说"太赶""来不及""能不能再加一个"
inputs:
  - 当天已排事项与剩余空档
outputs:
  - 可压缩项、可换天项、可换交通方式项
  - 具体到"哪一项挪到哪天/压缩多少分钟"
impl: nanjingtrip.services:TripService.fit_day
needs_network: true
fallback: 离线时只做纯时间账，不算通行细节
limits:
  - 只给建议，不擅自删改用户锁定的项
---
建议必须可执行：说清"动哪一项、动多少、动了之后几点结束"。
