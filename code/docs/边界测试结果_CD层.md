# 边界测试结果：C 层（地点解析失败路径）+ D 层（顾问槽位矩阵）

- 执行日期：2026-11-01
- 被测版本：`code/nanjingtrip/`（本次**未改动任何实现代码**）
- 命令（Windows PowerShell，先 `$env:PYTHONIOENCODING="utf-8"`）：

```powershell
cd D:\dsh\软件设计2-课程设计\code
python -m pytest tests/test_boundary_place.py tests/test_slots_matrix.py -q
```

- 结果：**34 passed, 7 xfailed**（41 条 item；失败项全部是与契约不符、疑似缺陷的用例，按 `xfail(strict=False)` 标注，套件不报错）
- 新增文件（仅此三个）：
  - `code/tests/test_boundary_place.py`（C 层，9 个用例函数 / 13 条 item）
  - `code/tests/test_slots_matrix.py`（D 层，26 个用例函数 / 28 条 item）
  - `code/docs/边界测试结果_CD层.md`（本文件）
- 口径来源：`code/docs/边界测试计划.md`、`AGENTS.md` §3「取不到就留缺口」、顾问槽位契约
- 用例全部离线（`settings.offline_mode = True` + `tmp_path`），不读写 `%LOCALAPPDATA%\NanjingTrip`
- 固定"今天"：C 层 `2026-11-10`，D 层 `2026-11-01`（不依赖真实日期）
- 缺陷编号说明：下表 `C-*` / `D-*` 是**本层内编号**；`05_设计报告/` 的正式 D-编号由维护者回填，本次未改动报告目录

---

## 一、C 层用例表（地点解析失败路径）

解析口径统一走用户真正会碰到的入口 `TripService.preview_batch(text, day)`（"一句话录入多个目的地"的预览），
写入口径走 `add_batch`。

| 编号 | 契约 | 用例 | 结果 |
|---|---|---|---|
| C-01 | 带后缀「三山街地铁站」应解析出对应地点 | `test_c01_subway_suffix_is_resolved` | **xfail**（疑似缺陷 ①） |
| C-02 | 带括号「夫子庙（景区）」应解析出对应地点 | `test_c02_paren_suffix_is_resolved` | pass |
| C-03 | 跨城地标「故宫博物院」必须拒绝，落"待补地点" | `test_c03_cross_city_palace_museum_is_rejected` | **xfail**（疑似缺陷 ②） |
| C-04 | 「上海外滩」「西湖」一律待补，不猜不误配（2 例） | `test_c04_other_cross_city_places_are_pending` | pass ×2 |
| C-05 | 本地同名不误伤：「明故宫遗址公园」必须正常解析 | `test_c05_local_lookalike_still_resolves` | pass |
| C-06 | 认不出的地名 → `needs_poi=True` 待补 + 写明原因；写入明确失败；不抛异常 | `test_c06_unknown_place_is_pending_not_invented` | pass |
| C-07 | 地址串「中山路321号」离线按待补（离线不做地理编码） | `test_c07_address_string_is_pending_offline` | pass |
| C-08 | 空名 / 纯标点 / 超长名不崩、不臆造（4 例） | `test_c08_degenerate_input_does_not_crash` | pass ×4 |
| C-09 | 括号说法解析成功后，预览标题不能只剩「（景区）」残渣 | `test_c09_title_is_not_leftover_parens` | **xfail**（疑似缺陷 ③） |

**C 层结论**：跨城防护在**服务层是好的**——`add_batch` 对「故宫博物院」「上海外滩」都返回
`找不到地点`，`_poi_named('故宫博物院')` 也返回 `None`；漏的是**解析/预览层绕过了这道闸**。

---

## 二、D 层用例表（顾问槽位矩阵）

重点槽位按任务要求分组覆盖；每人至少一提正例、一提反例。

| 编号 | 槽位 | 契约 | 结果 |
|---|---|---|---|
| D-01 | `start` / `end` | 明确区间按用户说的落槽，天数等于区间 | pass |
| D-02 | `start` / `end`（反） | 只说一天就不是区间：不追问、不扩成多天 | pass |
| D-03 | `trip_days` | 「一共3天」压过 12 天区间，终点按起点顺推 | pass |
| D-04 | `window` / `win_start` / `win_end` | 「11月中旬」是窗口不是行程：先按 3 天排，草案落在窗口内 | pass |
| D-05 | `window` + `trip_days`（反） | 窗口会话第二轮说「多加一天」要真的 +1 天 | **xfail**（疑似缺陷 ④） |
| D-06 | `party` | 「4个人」落槽并进草案 | pass |
| D-07 | `party`（反） | 「再加一个人」怎么算 → **PENDING-DECISION**，只钉合法值 | pass（仅钉合法性） |
| D-08 | `pace` / `wake` | 懒散 → `slow`+`10:00`；赶时间 → `fast`+`08:30`（2 例） | pass ×2 |
| D-09 | `day_end` | 「23点后回酒店」是收尾时间，不是"抵达南京" | pass |
| D-10 | `hotel`（反） | 「酒店未知」≠「你帮我选」：只追问，不代选 | pass |
| D-11 | `hotel` | 说「你帮我选」才代选，且说明可换 | pass |
| D-12 | `hotel`（反） | 「不用酒店（当天往返）」怎么落槽 → **PENDING-DECISION** | pass（仅钉"不臆造"） |
| D-13 | `meal` | 「午饭就近随便」→ 由系统挑，但必须挑出具体店 | pass |
| D-14 | `meal`（反） | 点名某家店就以他为准，不许换成"最近的店" | pass |
| D-15 | `taste` | 「清真」是口味，不该被写成店名 | **xfail**（疑似缺陷 ⑤） |
| D-16 | `taste`（反） | 「不吃辣」要真的过滤掉辣的候选 | pass |
| D-17 | `must` | 「必须去 X」进必去清单并排进草案 | pass |
| D-18 | `must`（反） | 句中的城市名（「去南京玩」）不是必去景点 | **xfail**（疑似缺陷 ⑥） |
| D-19 | `avoid` | 「别去 X」只影响新草案，**不删**已有数据 | pass |
| D-20 | `drop` | 「把 X 去掉」才真的删，并明确告知删了什么 | pass |
| D-21 | `extras` | 有"去哪儿办事 + 时长"才记；只有时长不记（2 例） | pass ×2 |
| D-22 | `practical` / `arrival` / `luggage` / `arrive_at` | 「10点到南京南站」→ 事务型行程（不含观光点） | pass |
| D-23 | `practical` × `hotel`（反） | 没委托选酒店，草案就不能声称"我帮你选" | **xfail**（疑似缺陷 ⑦） |
| D-24 | `friend_weekend` | 「朋友工作日没空」落槽 | pass |
| D-25 | 多槽位累积 | 三轮补充的信息同时留住，后一轮不冲掉前一轮 | pass |
| D-26 | 重新开始 | 服务端 `consult_reset` 后槽位清空、旧偏好不带过来 | pass |

**D 层结论**：路网级的关键交互（`drop` 真删 vs `avoid` 只影响草案、`hotel` 未定只追问、
窗口默认 3 天、`consult_reset` 清空）**都是对的**；出问题的是"口语边界"——
语气词被当店名、城市名被当景点、窗口模式下追加天数被自己收窄后的区间夹住。

### 覆盖盲区（本轮未覆盖，如实记录）

| 槽位 | 状态 |
|---|---|
| `interests` / `budget` / `per_day` | 未覆盖（兴趣词表与预算档属体验类，预计命中率低） |
| `anchor_place` / `hotel_near` / `next_day` | 未覆盖（属"务实型 + 返程"复合场景，需另一组多轮脚本） |
| `note` / `asked` / `days` / `v` | 仅间接覆盖（`asked` 由 D-10 追问行为体现；`v` 见既有 `test_consult.py::TC-08-10`） |

---

## 三、疑似缺陷（复现 / 期望 / 实际 / 最小修复建议）

> 以下**只记录，不改代码**。修复前请先跑 `python ../tools/audit_code.py` + 全量测试。

### ① C-01 带后缀的地名在预览层认不出（中危：用户最常见写法直接落待补）

- **复现**：`TripService.preview_batch('三山街地铁站', date(2026,11,10))`（离线）
- **期望**：解析出「三山街(地铁站)」，`needs_poi=False`
- **实际**：`poi=''`、`needs_poi=True`、`reason='缺少：耗时、地点'`（`三山街` 单说同样认不出）
- **定位**：后缀/括号规范化只写在 `services.py:2082 _poi_named()`（`re.sub(r'(地铁站|公交站|…)$')` 后再 `pack.find`），
  而预览链路是 `preview_batch → preview_text → parse_svc.parse`（`parsing_rules.match_poi` 只做 `name in text` 与别名表）
  → 拿不到 `mention` → `_salvage_item` 里的 `_place_in_segment(seg)` 也按裸子串匹配。这一层完全没经过 `_poi_named`
- **最小修复建议**：在 `_place_in_segment()`（或 `_salvage_item` 里 `poi is None` 之后）补一条 `poi = self._poi_named(seg.strip(' ，,。'))`；
  好处是 `_poi_named` 的"后缀/括号/跨城两道闸"一次性都复用上，不必在解析层再写一份规范化

### ② C-03 「故宫博物院」被解析成「南京博物院」（高危：跨城误配，正是最致命的观感问题）

- **复现**：`preview_batch('故宫博物院', …)` → item `{'poi': '南京博物院', 'title': '故宫博物院', 'duration_min': 150}`
- **期望**：`poi=''` + `needs_poi=True`（待补地点）
- **实际**：悄悄换成本地馆，用户直接看到"游览南京博物院"
- **定位**：城市包别名表里有一条 `'博物院' → 南京博物院`，而 `parsing_rules.match_poi()` 的"别名优先"用
  `if alias in text`（裸子串）：`'博物院' in '故宫博物院'` → 命中，mention 在解析阶段就已变成 `南京博物院`，
  后面的服务层闸门（`_poi_named` / `add_batch`）根本没机会介入
- **对照证据**：`_poi_named('故宫博物院')` → `None`，`add_batch` → `找不到地点`；说明闸是好的，只是被绕过了
- **最小修复建议**：`match_poi` 的别名分支不要裸子串（要求 `len(alias) / len(mention)` 达标，或直接复用
  `services.name_alike(mention, 别名值)`）；更稳的做法是 `match_poi` 命中后再过一道
  "城市一致 + 名字像"校验，与 `_poi_named` 同一套口径

### ③ C-09 括号说法的预览标题只剩「（景区）」（低危：观感）

- **复现**：`preview_batch('上午去夫子庙（景区）2小时', …)` → `title='（景区）'`（`poi` 是对的：`夫子庙`）
- **期望**：标题能看出是哪个地方（`夫子庙` 或 `夫子庙（景区）`）
- **实际**：`（景区）` 这个括号残渣成了标题
- **定位**：`preview_batch` 的 `strip_note_parens()` 没把这个括号当附注摘掉，
  随后 `extract_item(text, mention)` 把 mention「夫子庙」替换掉，只剩「（景区）」
- **最小修复建议**：`_salvage_item` / `preview_text` 生成标题时优先取 `poi.name`；
  或在 `extract_item` 末尾清掉纯括号残渣（`re.sub(r'[（(][^）)]*[）)]', '', core)`）

### ④ D-05 窗口会话里「多加一天」不生效（中危：文案承诺了却没做到）

- **复现**：
  ```python
  con.chat('s', '11月中旬去南京玩', TODAY)   # plan['days_total'] == 3
  con.chat('s', '多加一天', TODAY)            # plan['days_total'] 仍是 3
  ```
- **期望**：第二轮之后 `days_total == 4`（`trip_days` 由 3 → 4）
- **实际**：`trip_days` 变成 3，天数不变；同一句话**一轮说完**时又是对的
  （`'11月中旬去南京玩，多加一天'` → `trip_days=4`）——说明是跨轮状态导致的
- **定位**：`consult.py:492` 窗口分支 `base = st.trip_days or min(st.date_span or 3, 3)`，
  而上一轮 `build_plan()` 已把 `st.end` 收窄成 3 天（`consult.py:1156`），于是 `date_span == 3`，
  上限 `min(base+1, max(1, st.date_span or …))` 又把 4 夹回 3
- **同时暴露的文案问题**：同一轮回复里仍写着"中旬窗口是 …，先按 3 天给你排（**想多玩几天说一句就行**）"，
  用户照做却没反应
- **最小修复建议**：窗口模式下把上限改成窗口长度（`win_start~win_end`）而不是被草案收窄后的 `date_span`：
  `st.trip_days = min(base + 1, (win_end - win_start).days + 1)`；文案同步检查

### ⑤ D-15 口味句「我想吃清真的」被写成店名（中危：把"口味"当"餐厅"）

- **复现**：`con.chat('s', BASE)` → `con.chat('s', '我想吃清真的')`
- **期望**：`taste='清真'`，`meal` 留空（或 `recommend`），餐饮候选按清真过滤
- **实际**：`taste='清真'` **且** `meal='清真的'`；因为 `meal` 非空，`plan['meal_options']` 被压成 `[]`，
  草案里出现"吃饭（清真的）"这样的时间块
- **定位**：`consult.py:1337 explicit_meal_place()` 的第三条 pattern `吃\s*([\u4e00-\u9fa5…]{2,24})`
  会把「吃」后面任意短语当店名，而 `poi is None` 时该函数**原样返回候选**（设计上是为了保留
  "城市包里没有的真实店名"），于是「清真的」这种词也进来了
- **最小修复建议**：`explicit_meal_place` 返回前加一个口味/停用词黑名单
  （`清真 / 素 / 吃素 / 辣 / 不吃辣 / 随便 / 都行 / 什么` 等）；或要求候选要么命中城市包/`_resolve_place`、
  要么带明显店铺特征（"店""馆""楼""餐厅"）才写入 `meal`，否则只落 `taste`

### ⑥ D-18 「去南京玩」把「南京站」写成必去景点（中危：草案第一天变成"游览南京站"）

- **复现**：`con.chat('s', '11月14号到11月16号去南京玩')` → `plan['must'] == ['南京站']`，
  第一天 items 第一条是 `{'poi': '南京站', 'title': '游览南京站'}`
- **期望**：句中城市名不产生 `must` 项（`must` 只放"点名必须去"的具体地点）
- **实际**：城市名进了必去清单，还占掉当天的点数配额（每天 3 个点变成 2 个景点 + 1 个火车站）
- **定位**：`consult.py:614` 的「去/逛/参观 X」规则抓走「南京」→
  `_resolve_place('南京')` → `_poi_named('南京')` → `_pack_search` 的"名称包含 + 长度占比"落到「南京站」
  （`_poi_named('南京')` 实测返回 `南京站`）
- **最小修复建议**：`_resolve_place` 增加"城市名/城市自身词"停用项（`南京 / 南京市 / 市区 / 景点 / 大学城` …）；
  或该规则只在候选 `kind` 不属于 `交通/住宿` 且名字长度 ≥ 3 时才写 `must`
  （`consult.py:621` 那条规则已经排除了交通类，但 `:614` 这条没有）

### ⑦ D-23 事务草案在住宿未定时自称"我帮你选"（中危：正好踩中"≠"那条契约）

- **复现**：`con.chat('s', BASE)` → `con.chat('s', '我10点到南京南站')`
- **期望**：用户从未说"你帮我选"，草案就不能把住宿标成"我替你挑的"
- **实际**：槽位 `hotel` 仍是 `''`（未定），但 `plan['hotel'] == 'recommend'`、
  `plan['hotel_pick'] == '金陵饭店'`，**同一轮回复里还在追问"住哪儿？"**
- **定位**：`consult.py:1520 _build_practical()` 返回 `'hotel': st.hotel or 'recommend'`（把"未定"映射成"我选"）；
  `consult.py:1435` 在 `st.luggage` 为真时无条件下场挑酒店并写 `hotel_pick`
- **最小修复建议**：事务草案照实返回 `'hotel': st.hotel` 并补 `'hotel_undecided': not st.hotel`（与观光草案字段对齐）；
  `hotel_pick` 只在 `st.hotel == 'recommend'` 时落，`luggage` 场景改成给"候选 + 一句待确认"而不是既成事实

---

## 四、PENDING-DECISION（需要产品口径，不由测试或实现决定）

| 编号 | 待决问题 | 现状（仅记录，不作断言） | 影响 |
|---|---|---|---|
| P-1 | 「再加一个人」是否应把 `party` 累加？ | `party` 保持 1（`parse_party` 里「一个人」优先命中），与「多加一天/少一天」已支持天数增减不对称 | 多人行程报错人数 → 餐厅/时长建议偏差；用例 `test_d07` 只钉"1–9 的整数" |
| P-2 | 「不用酒店（当天往返）」该落哪个槽？ | 追问选项里**明确提供了**这个按钮，但点击后 `hotel` 仍为 `''`（= 未定），系统会继续按"未定"追问与推荐 | 用户明说不要，仍被反复追问；可能需要在槽位里表达 `hotel='none'` 之类的语义，属数据模型决策 |
| P-3 | 说了「别去 X」之后又改口「X 还是去吧」，`avoid` 是否需要出口？ | 当前没有把 X 从 `avoid` 移除的语句规则，`avoid` 只增不减（`drop` 用完即清） | 用户改主意后 X 永远进不了草案 |
| P-4 | 用户单说「明故宫」「三山街」这类**只有主体词**的说法，应解析成哪个点位？ | `_poi_named('明故宫')` → `None`；`_poi_named('三山街')` → `None`（都不臆造，落待补）；同名候选里有「南京明故宫遗址公园」与「明故宫(地铁站)」 | 决定 C-01 那类修复做到多"聪明"：只补后缀，还是连主体词也做候选排序（需要口径，否则又是一次"看起来像"的凑数） |

---

## 五、最脆弱的三点（一句话总结）

1. **解析层与服务层的闸是两套口径**：跨城防护（`_poi_named()` 里那两道）只在服务层生效，
   预览/解析链路（`match_poi` 的裸子串别名 + 包内"包含匹配"）自己另有一套更松的匹配，
   于是「故宫博物院→南京博物院」「南京→南京站」「三山街地铁站→待补」同时存在。
2. **口语边界缺停用词表**：口味词（清真的）、城市名（南京）、纯时长（40 分钟）都能被当成
   "店名/景点/事项"，槽位一脏，后面的排程与文案全跟着歪（`must` 被污染时连每天的点数都被吃掉一个）。
3. **跨轮的"自我收窄"最容易出坑**：上一轮 `build_plan` 会把 `st.end` 收窄成草案天数，
   下一轮的天数类指令又拿它当上限，结果"多加一天"静默失效，而同一句话一轮说完却是对的——
   这类只在多轮里出现的缺陷，单轮用例永远抓不到。
