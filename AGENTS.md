# AGENTS.md — 本项目的人和 agent 共读的开发规矩

行程规划 AI Agent（南京市内，单人出行）。课程设计交付物，交付日期比"架构漂亮"更重要：
**能真实跑通、能解释清楚、能被复现**优先于抽象层级。

## 1. 目录地图

| 路径 | 内容 |
| --- | --- |
| `code/nanjingtrip/` | 应用主体（纯 Python，标准库 HTTP 服务 + 浏览器前端） |
| `code/nanjingtrip/agent/` | Agent 能力声明：`AGENT.md`（行为规矩）+ `skills/*.md`（能力卡）+ `skills.py`（只读注册表） |
| `code/nanjingtrip/web/` | 前端：`index.html` / `app.js` / `app.css`（无框架，原生 DOM） |
| `code/nanjingtrip/packs_*.py` | 城市数据包（POI/地铁/配套），数据只写在包里或库里 |
| `code/nanjingtrip/adapters/` | 外部对接：高德、DeepSeek、mock（离线） |
| `code/tests/` | **正式套件**（默认跑，离线可复现） |
| `code/tests_extra/` | 扩展套件（不默认跑，压测/长输入等） |
| `code/docs/` | 数据来源、城市包覆盖度等说明 |
| `05_设计报告/` | 报告 docx/pdf、缺陷表、需求规划 |
| `tools/` | `dev.py`（开发服务）、`build_report.py`、`pack_submission.py`、模拟与压力脚本 |

## 2. 分层与依赖方向

`domain(engine/hours/evidence/…) → services → web_api/lan_server → web/`

- 领域层不得 import 前端、不得发网络请求；取数一律经 `adapters/`。
- `services.py` 只编排，不塞业务规则；规则放领域模块。
- 新增外部能力：先在 `adapters/` 加接口，再在 mock 里给离线实现，最后接服务。

## 3. 数据纪律（最容易翻车的地方）

1. **不许手填数据当真实数据。** 景点耗时、开放时间、票价、天气、距离要么来自城市包/证据表，
   要么来自高德实测；估算值必须显式标注为估算。
2. **取不到就留缺口。** 地名核不上 → `待补地点`；空档排不下 → 冲突 + 取舍建议。不许凑一个"看起来像"的。
3. **取数只对入选点做。** 候选几十个全量取一次要几十秒。
4. **天气只有当天 +3 天有真实预报**，超出范围要说清，不要 extrapolate。

## 4. 编码准则

- 可读性优先：命名写清楚，别用 `tmp`/`d1`/`x` 这类名字。
- 单一职责：一个函数只做一件事；`services` 里不许出现"顺手也算一下排程"。
- 消灭重复：相同的清洗/规范化逻辑抽成模块级函数（如 `name_alike`、`_norm`）。
- 防御性：外部输入（用户口语、模型输出、高德返回）一律当不可信数据校验后再用。
- 拒绝过度设计：能用一个 dict + 一个函数解决，就不要引入框架与插件系统。
- 注释只写"为什么"：`# 为什么` 有用，`# 加一` 没用。
- 小步改：一次只解决一个问题，改完立刻跑测试，再改下一个。

## 5. 测试口径

```bash
cd code
python ../tools/audit_code.py        # 体检：编译 + 冒烟 + AST（重复定义/死代码/游离槽位）
python -m pytest tests -q            # 正式套件（离线，必须全绿）
python -m pytest tests_extra -q      # 扩展套件（不默认跑）
```

改完代码先跑体检再跑测试：装饰器丢失、接线断裂、槽位没登记这类问题体检几秒就能发现，
跑全量测试要一分钟。

- 新增行为必须带用例；边界（空输入、超长输入、模型不可用、地名不存在）优先写。
- 用例固定"今天"（`TODAY` 常量）或注入日期，禁止依赖真实日期。
- 涉及模型的用例只在 `tests/test_online_*.py` 里，用 `-m online` 显式跑，默认离线不触网。

## 6. Agent 能力卡约定

- 能力改动 → 同步改 `code/nanjingtrip/agent/skills/<name>.md`（触发条件、输入、输出、边界、`impl`）。
- `impl` 写 `模块:符号`，测试会校验它真的存在；卡片和代码脱节会直接红。
- `AGENT.md` 只写"规矩与协议"，不抄实现细节。

## 7. 交付同步点（改完代码记得回填）

- 用例数变化 → 报告第 4 章与封面口径。
- 修掉/引入缺陷 → `05_设计报告/` 缺陷表（D-编号连续，不重号）。
- 城市包规模变化 → `code/docs/城市包覆盖度.md` 与报告数据表。
- 重建交付包：`python tools/pack_submission.py`；报告 PDF 用 `tools/build_report.py`。
