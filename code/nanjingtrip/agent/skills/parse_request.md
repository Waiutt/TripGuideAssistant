---
name: parse_request
title: 读懂一句话里的多件事
when_to_use:
  - 用户一口气说了多个地点、时长或时刻
  - 句子里有条件句（"下雨就改去X"）、返程死线（"晚上9点的高铁"）、出发地
inputs:
  - 用户的口语原句
outputs:
  - 地点 + 时长 + 开始时刻/截止时刻
  - 条件句分组（什么条件下才去哪些点）
  - 返程死线与出发地
impl: nanjingtrip.llm_intake:structured_parse
needs_network: true
fallback: 模型不可用时走规则解析（正则+关键词），并标出未解析片段
limits:
  - 只抽用户明确说过的信息，不补全、不猜时长
  - 地点是否真实存在交给 place_lookup 校验，模型不拍板
---
解析结果是"候选"，不是结论：低置信度的候选一律丢弃，
剩下的再由代码做存在性校验与冲突检查。
