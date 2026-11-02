---
name: ask_clarify
title: 缺信息时追问
when_to_use:
  - 缺日期、人数、住宿等关键信息
  - 用户的说法有歧义，两种理解会排出完全不同的行程
inputs:
  - 当前已确认的槽位
outputs:
  - 一个具体问题 + 可直接点的候选答案
impl: nanjingtrip.consult:Consultant.questions
limits:
  - 同一个问题只问一次，问过就按默认值先给一版并说明可改
  - 缺住宿一定先问，除非用户说"你帮我选"
---
追问一次只问一件事，并给出默认值：不能让用户面对一张空白问卷。
