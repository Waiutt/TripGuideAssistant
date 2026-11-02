# 南京行程规划 AI Agent

用一句自然语言说清「哪天、去哪、待多久」，得到一个可执行的当天行程，并存进本地数据库。

> 课程设计项目（软件设计 II，数字媒体技术）。单人独立完成：需求 → 设计 → 实现 → 测试 → 交付。

## 它解决什么

「周六下午想去总统府和 1912，晚上 9 点前回学校」这种口语，传统待办软件接不住：要判断**说了几件事**、每件事**在地哪儿**、**多久**、**营业时间够不够**、**两点之间怎么走要多久**。这些在真实数据上都不确定，于是本项目的核心取舍是：

**LLM 只负责「理解」和「给候选」，校验、取数、约束求解、解释全在代码里。**

## 架构

```
web (原生 SPA)  →  web_api / lan_server  →  services（编排）
                                          →  domain：engine 排程 / hours 营业 / evidence 时长 / ordering 排序 / intent 意图
                                          →  adapters：amap 高德 / deepseek / mock（离线）
                                          →  storage：SQLite + WAL + 维护
```

| 层 | 关键点 |
|---|---|
| 意图层 `intent.py` | 6 类意图（plan/entry/modify/prune/ask/clarify/vague）+ 7 类动作切片；一句话里的多件事各归各位 |
| 排程引擎 `engine.py` | 锁定优先 + 空档扫描；营业时间参与择时（不排到开门前/闭馆后）；真实通行时长推进时间轴 |
| 排序层 `ordering.py` | 闭馆早的先排、夜游类挪最后、同类就近串联；**只改顺序不改成员** |
| 取数 | 高德（POI/路径/天气/静态图）+ DeepSeek；三级时长证据（官方 > OTA > 估算），配额打满自动降级离线估算 |
| 数据纪律 | 取不到就留缺口：地名核不上转「待补地点」，门票没收录写「待核实」，不编数据 |

## 快速开始

```bash
cd code
pip install -r requirements.txt      # requests / pytest
python main.py                       # 起本地服务并打开浏览器（默认 8765）
```

离线模式不需要任何 Key：内置南京城市包（906 个点位）+ 规则解析 + 距离估算。

```bash
cd code
python ../tools/audit_code.py        # 代码体检：编译 + 装配冒烟 + AST（重复定义/死代码/游离槽位）
python -m pytest tests -q            # 正式套件（离线，约 3–4 分钟）
python -m pytest tests_extra -q      # 扩展套件
python -m pytest -m online -q        # 联网验收（真实 DeepSeek + 高德）
```

## 质量数据（实测）

- 自动化用例 **1142** 条：正式 1053（离线 1044 + 联网 9）+ 扩展 89
- 离线回归 **844 passed / 0 failed**；另有 98 条按契约 `xfail` 记录待修、101 条 `xpassed`（已修好待撤标记）
- 缺陷台账 **130 条**，每条含现象 / 根因 / 修法 / 回归用例
- 自研 `tools/audit_code.py`：编译、装配冒烟、AST 检查（曾靠它抓出"装饰器被脚本改丢"这类事故）

> `xfail` 不是把现状写成断言：契约先写成用例，修好会自动变 `xpassed`。

## 目录

```
code/nanjingtrip/       应用主体（domain / services / adapters / web）
code/nanjingtrip/agent/ Agent 行为规矩（AGENT.md）+ 8 张能力卡
code/tests/             正式套件（离线可复现）
code/tests_extra/       扩展套件
tools/                  开发服务、报告构建、代码体检、模拟与压测脚本
05_设计报告/章节/        设计报告正文（markdown 源）
```

## 说明

- 学期课程作业，单人完成，未部署、无真实用户量。
- 报告 docx/pdf 与演示视频不在本仓库（含学校表格与个人信息）。
