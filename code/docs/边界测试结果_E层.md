# 边界测试结果 · E 层（`/api/command` 接口契约）

| 项 | 值 |
| --- | --- |
| 被测入口 | `nanjingtrip.web_api.command_payload(ctx, body)`（前端唯一总写入口，对应 `POST /api/command`） |
| 用例文件 | `code/tests/test_boundary_api.py` |
| 运行方式 | `cd code; python -m pytest tests/test_boundary_api.py -q` |
| 环境 | 离线（`AppContext(tmp_path)` + `settings.offline_mode = True`），不触网、不碰真实数据目录 |
| 结果 | **79 passed, 29 xfailed, 0 failed**（约 31 s） |
| 覆盖口径 | `web_api.py` 里 `if action == '...'` 全部分支：**36 个动作** |

动作清单以源文件为准，与任务书清单有两处差异，已按源文件处理：

* 源文件**有** `delete_day`（规划记录管理），清单未列 → 已测；
* 清单里的 `alternatives` 是 `GET /api/alternatives`（`lan_server.py`），`adjust` 是
  `trip_svc.adjust()` 服务方法，二者都不是 command 动作 → 不在本层范围。

用例结构（一条参数化 + 少量专项，避免文件冗长）：

| 用例 | 覆盖 |
| --- | --- |
| `test_legal_call_returns_contract_dict`（36 参数） | 最小合法调用回 dict、`ok` 是布尔量、无异常；只读动作 revision 不变、写动作 revision 前进、配置动作有 message |
| `test_illegal_call_is_rejected_without_exception`（36 参数） | 缺失/非法参数回 `ok=False` + 非空 message + 不改数据 |
| `test_action_list_matches_web_api_branches` | 自检：用例覆盖集合 == 源文件分支集合（36 个），防止动作增删后测试悄悄失效 |
| 边界专项（9 条） | 空 body / 未知动作、`preview_batch` 空文本、`add_batch` 空条目、`delete` 不存在 id、`consult` 超长转 notice、写链 revision 单调、撤销令牌一次性 |
| 疑似缺陷 xfail（29 条） | 3 条专项 + 13 条字符串类型错误 + 10 条数字类型错误 + 3 条复合结构错误 |

---

## 1. 动作契约表

「合法结果」= 最小合法调用的实际结果；「非法参数结果」= 用例里那一条非法输入的
实际结果。**备注**列标注缺陷编号（见第 2 节）、PENDING-DECISION（第 3 节）或行为说明。

| 动作 | 只读 | 合法结果 | 非法参数结果 | 备注 |
| --- | --- | --- | --- | --- |
| `preview` | 是 | `ok=True kind=task`，revision 不变 | 空 text → `ok=False`「请先输入一句话」 | 只解析不落库 |
| `preview_batch` | 是 | `ok=True kind=batch` + `items`，revision 不变 | 空文本 → `ok=False`「请先输入一句话」 | 契约 1 ✔ |
| `add` | 否 | `ok=True`，revision +1 | 空 text → `ok=False`「请先输入一句话」 | 带 `draft` 改字段时另有缺陷 **D-E3** |
| `add_batch` | 否 | `ok=True`「已排入 1 项」，revision +1 | `items=[]` → `ok=False`「没有可写入的条目」 | 契约 2 ✔；元素非 dict 见 **D-E6** |
| `delete` | 否 | `ok=True`「已删除」，revision +1 | id=987654 → `ok=False`「未找到行程」 | 契约 3 ✔；`task_id='abc'` 见 **D-E2** |
| `done` | 否 | `ok=True`「已标记完成」，revision +1 | id=987654 → `ok=False`「未找到该安排」 | **D-E2** |
| `set_mode` | 否 | `ok=True`「已切换方式」，revision +1 | `mode='teleport'` → `ok=False`「交通方式不合法」 | **D-E2** |
| `set_mode_many` | 否 | `ok=True`「已批量切换方式（2 项）」，revision +1 | `task_ids=[]` → `ok=False`「未选择任何安排」 | **D-E2** |
| `move` | 否 | `ok=True`「已改到 15:30」，revision +1 | `start='abc'` → `ok=False`「时间格式应为 HH:MM」 | 拒绝后数据不变 ✔；**D-E2** |
| `delay` | 否 | `ok=True` + 重排说明，revision +1 | `delay_min=0` → `ok=False`「延误分钟数必须大于 0」 | 该项唯一做了 `try/except` 的数字字段 |
| `delete_many` | 否 | `ok=True` + `deleted` 快照 + `undo_token`，revision +1 | `task_ids=[]` → `ok=False`「未选择任何安排」 | 快照已 JSON 安全（只有 poi_name 字符串） |
| `restore_many` | 否 | `ok=True`「已撤销删除（1 项）」，revision +1 | 无效 token → `ok=False`「撤销已失效…」 | 令牌一次性，二次使用被拒 ✔ |
| `delete_day` | 否 | `ok=True`「已删除 … 的整份行程」，revision +1 | `date='2026-02-30'` → `ok=False`「日期格式不对」 | 无行程时 `ok=False` 但 revision 仍 +1（观察项 O-1） |
| `replan` | 否 | `ok=True` + 排程摘要，revision +1 | `date='2026-02-30'` → `ok=False`「日期格式不对」 | 无行程时 `ok=False`「该日暂无行程」 |
| `rollback` | 否 | `ok=True`「已回滚到版本 1」，revision +1 | `version_id=999999` → `ok=False`「版本不存在」 | **D-E2**（`trip_id='abc'` 抛异常） |
| `pin_point` | 否 | `ok=True`「已钉住坐标并重新排程」，revision +1 | `lng='x'` → `ok=False`「坐标不合法」 | 与 `delay` 一样自带 `try/except` |
| `set_lodging` | 否 | `ok=True` + 排程摘要，revision +1 | `name=''` → `ok=False`「缺少住宿名称」 | **D-E1**（`name=123` 抛异常） |
| `clear_lodging` | 配置 | `ok=True`「已清除住宿锚点」 | `date='2026-02-30'` → `ok=False`「日期格式不对」 | 当天没锚点也回 `ok=True`（幂等，可接受） |
| `decline_lodging` | 否 | `ok=True`「已标记当天往返」，revision +1 | `date='2026-02-30'` → `ok=False` | 无必需参数 |
| `apply_proposal` | 否 | `ok=True`「已写入 1 天行程」，revision +1 | `plan={}` → `ok=False`「没有可写入的草案」 | `plan='x'` 见 **D-E6** |
| `fit_day` | 是 | `ok=True` + `fit` 方案，revision 不变 | `date='2026-02-30'` → `ok=False` | 不带 `apply` 时只算不写 |
| `consult` | 是 | `ok=True kind=plan` + `slots`/`questions`，revision 不变 | 空 text → `ok=False`「说点什么吧…」 | 契约 4 ✔：>1200 字 → `kind='notice'`、message 含「太长」与上限 1200 |
| `consult_reset` | 是 | `ok=True`「已重新开始…」，revision 不变 | `date='2026-02-30'` → `ok=False` | 无 session 也回 `ok=True`（幂等，见 PENDING-3）；**D-E1** |
| `propose_trip` | 是 | `ok=True` + `plan`，revision 不变 | 空 text → `ok=False`「先说说你想去哪、玩几天」 | **D-E1** |
| `ask` | 是 | `ok=True` + 离线规则回答，revision 不变 | 空 question → `ok=False`「请输入你想问的内容」 | 离线 `source='offline'` ✔ |
| `assistant_act` | 是 | `ok=True` + `kind`，revision 不变 | 空 text → `ok=False`「请输入内容」 | `apply` 分支由 `test_assistant_act.py` 覆盖 |
| `agent_skills` | 是 | `ok=True` + `skills`/`count`，**无 message**，revision 不变 | `date='2026-02-30'` → `ok=False` | 只读目录；成功时不带 message（见 PENDING-2） |
| `db_stats` | 是 | `ok=True` + 库规模字段，**无 message**，revision 不变 | `date='2026-02-30'` → `ok=False` | 同上 |
| `db_maintain` | 配置 | `ok=True`「数据维护完成：…」，revision 不变 | `date='2026-02-30'` → `ok=False` | 显式维护才截断 WAL |
| `set_settings` | 配置 | `ok=True`「设置已保存」，revision +1（触发重排） | `date='2026-02-30'` → `ok=False` | **D-E4（最严重）**：非法枚举/时刻/缓冲被当成合法值落库 |
| `toggle_offline` | 配置 | `ok=True`「已切换为离线模式」，revision 不变 | `date='2026-02-30'` → `ok=False` | `offline='??'` 被 `bool()` 强转 True（PENDING-4） |
| `set_city` | 配置 | `ok=True`「已切换到北京」，revision 不变 | `city=''` → `ok=False`「缺少城市」 | **D-E1** |
| `set_profile` | 配置 | `ok=True`「已切换到「临时档」（…）」，revision 不变 | `name=''` → `ok=False`「缺少档案名」 | 不存在的档案名静默回退当前档案（PENDING-1）；**D-E1** |
| `save_profile` | 配置 | `ok=True`「已保存偏好「测试档」」，revision 不变 | `date='2026-02-30'` → `ok=False` | 字段全有默认值 |
| `delete_profile` | 配置 | `ok=True`「已删除档案「临时档」，当前为「默认」」，revision 不变 | `name='不存在'` → `ok=False`「没有「不存在」这个档案」 | **D-E1** |
| `apply_suggestion` | 否 | **取不到合法结果**：任何 patch 都 `ok=False`「该建议不含可执行内容」 | 同上（`patch={}` 也是这句） | **D-E5**：动作整体不可达 |

> 说明：「配置」= 写设置 / 档案 / 城市 / 离线开关，不写行程库，故 revision 不变，
> 但必须有明确 message（用例里按这个口径判）。「是」= 只读，revision 必须不变。

---

## 2. 疑似缺陷

以下每条在 `test_boundary_api.py` 里都有对应的 `xfail(strict=False)` 用例，**没有**把
现状写成断言，也没有改动 `nanjingtrip/**` 任何代码。缺陷编号用 `D-E*` 前缀，避免与
`05_设计报告/` 里既有的 `D-*` 表撞号（是否并入正式缺陷表请维护人决定）。

### D-E4（严重，最该先修）`set_settings` 不校验任何值，坏配置会持久化并弄坏整天视图

* **复现**：`command_payload(ctx, {'action': 'set_settings', 'date': '2026-11-02', 'transit_mode': 'rocket'})`
  → 返回 `ok=True`「设置已保存」。随后 `state_payload(ctx, day)`（或页面刷新任何一天）
  抛 `ValueError: 'rocket' is not a valid TransitMode`（栈：`services.options()` →
  `TransitMode(self.settings.transit_mode)`）。
* **期望**：`ok=False` + 「交通方式不合法」；设置项不落库。
* **实际**：非法值被 `settings_svc.save()` 写进设置文件，**重启进程后依然读回
  `transit_mode='rocket'`**，直到手工改设置文件；同一类问题还有
  `day_start='99:99'` → `ValueError: hour must be in 0..23`、`buffer_min='abc'` →
  `ValueError: invalid literal for int()`。
* **影响**：这不是"某个按钮报错"，而是把用户当天整份行程的读取入口打穿——前端每天
  打开都是 500，且刷新、重启都救不回来。属于**输入校验缺失 → 持久化污染**。
* **最小修复建议**：在 `web_api.command_payload` 的 `set_settings` 分支里先校验再落库：
  `transit_mode` 必须 ∈ `{m.value for m in TransitMode}`（`set_mode` 分支已有同样的
  校验可抄）；`buffer_min` 走 `int()` 并 `try/except`，限个合理区间；
  `day_start`/`day_end` 用 `HH:MM` 正则 + 时/分范围检查（`move` 分支的逻辑可直接复用）。
  校验不通过就 `return _err(...)`，不要 `apply_settings()`。

### D-E1（面最广）13 个字符串参数直接 `.strip()`，非字符串输入抛 `AttributeError`

* **复现**（任一条）：`{'action': 'preview', 'date': '2026-11-02', 'text': 123}`。
* **期望**：`ok=False` + 可读 message。
* **实际**：`AttributeError: 'int' object has no attribute 'strip'` —— 未被捕获，
  `lan_server` 会转成 500。
* **受影响动作/字段**：`preview`/`preview_batch`/`add`/`consult`/`propose_trip`/
  `assistant_act`（`text`）、`ask`（`question`）、`set_lodging`/`set_profile`/
  `delete_profile`（`name`）、`set_city`（`city`）、`consult_reset`（`session`）、
  `restore_many`（`undo_token`）。共 13 处，模式完全一样：
  `(body.get('text') or '').strip()`。
* **为什么现在才发现**：前端 `app.js` 只会发字符串，所以日常点不出这个 500；
  但只要有人用 `curl`/脚本调 `/api/command`（本项目的演示脚本就会），就会命中。
* **最小修复建议**：抽一个边界取值函数放在 `web_api` 顶部
  `def _text(body, key): v = body.get(key); return v.strip() if isinstance(v, str) else ''`，
  13 处统一替换；非字符串等价于"没给"，走各自已有的"缺少参数"分支即可。

### D-E2（面次广）8 处数字参数直接 `int()`，非数字输入抛 `ValueError`

* **复现**：`{'action': 'delete', 'date': '2026-11-02', 'task_id': 'abc'}`；
  `{'action': 'delete_many', 'task_ids': ['abc']}`；
  `{'action': 'rollback', 'trip_id': 'abc', 'version_id': 'x'}`。
* **期望**：`ok=False` +「任务号不合法」之类。
* **实际**：`ValueError: invalid literal for int() with base 10: 'abc'`（未捕获 → 500）。
* **受影响**：`delete`/`done`/`set_mode`/`move` 的 `task_id`；`delete_many`/
  `set_mode_many` 的 `task_ids` 元素；`rollback` 的 `trip_id`/`version_id`。
* **对照**：同一文件里 `delay`（`delay_min`/`task_id`）和 `pin_point`（坐标）都写了
  `try/except (TypeError, ValueError) → _err('…不合法')`，说明作者知道该这么做，
  只是没推广。修法：照抄这两处的 `try/except`。

### D-E3 `add` 的 `draft` 补丁字段未校验，改过的预览卡能打出 3 种 500

* **复现**：`{'action': 'add', 'date': '2026-11-02', 'text': '上午十点去玄武湖公园散步，1小时',
  'draft': {'duration_min': 'abc'}}`（另两种：`desired_start='xx'`、`mode='rocket'`）。
* **期望**：忽略坏字段或 `ok=False`。
* **实际**：分别抛 `ValueError: invalid literal for int()`、
  `ValueError: not enough values to unpack (expected 2, got 1)`、
  `ValueError: 'rocket' is not a valid TransitMode`。
* **说明**：`draft` 来自前端预览卡的可编辑控件，是**用户能直接改到**的字段，比 E1/E2
  更容易被真实用户触发（比如把时长输入框清空再提交）。
* **最小修复建议**：`duration_min` 用 `try/except` + 区间钳制（现有 `max(10, ...)`
  只防了小数没防非数字）；`desired_start` 复用 `move` 分支的 `HH:MM` 校验；
  `mode` 用 `TransitMode` 白名单校验，不合法就不覆盖。

### D-E5 `apply_suggestion` 动作不可达（dict 被当成 `Suggestion` 对象用）

* **复现**：`{'action': 'apply_suggestion', 'date': '2026-11-02',
  'patch': {'title': '看展', 'duration_min': 45}}`。
* **期望**：`ok=True`，该项耗时改为 45 分钟。
* **实际**：`ok=False`「该建议不含可执行内容」。任何 patch 都是这句。
* **根因**：`web_api.py` 传的是**普通 dict**，而
  `services.apply_suggestion(suggestion, …)` 第一行是
  `patch = dict(getattr(suggestion, 'patch', {}) or {})` —— dict 没有 `.patch`
  属性，于是永远取到空 dict，直接走"不含可执行内容"的失败分支。
  `delete`/`move_next_day`/`day_end` 等后续逻辑一次都没被执行过。
* **补充事实**：前端 `web/app.js` 从未调用过这个 action（47 个 `cmd(...)` 里没有它），
  所以这是一个"后端有分支、前端没接线、且分支本身不可用"的三重死角。
* **最小修复建议**：接口层改成 `patch = body.get('patch') or {}` 后直接调用
  `ctx.trip_svc.apply_suggestion(types.SimpleNamespace(patch=patch), day)`
  （或给服务加一个接受 dict 的重载）；同时决定是否在前端接上建议采纳按钮
  （见 PENDING-5）。

### D-E6 复合参数结构不校验，元素不是 dict 就抛 `AttributeError`

* **复现**：`{'action': 'add_batch', 'date': '2026-11-02', 'items': ['x']}`；
  `{'action': 'apply_proposal', 'date': '2026-11-02', 'plan': 'x'}`。
* **期望**：`ok=False`「条目格式不对」。
* **实际**：`AttributeError: 'str' object has no attribute 'get'`（未捕获 → 500）。
* **说明**：`items=[{}]` 这类"结构对、内容空"的输入反而被正确拒收
  （「已排入 0 项；1 项未写入（找不到地点：?）」），说明差的只是**类型**这一层。
* **最小修复建议**：`add_batch` 分支入口先
  `items = [i for i in items if isinstance(i, dict)]` 并计数丢弃；
  `apply_proposal` 分支加 `isinstance(plan, dict)` 判断。

### 观察项（不算缺陷，未写成断言）

* **O-1** `delete_day` 在"该日没有行程"时回 `ok=False`，但 revision 仍然 +1
  （`trip_repo.delete_day` 里做了版本类写入才判断条数）。语义上"失败不该推进版本"，
  目前只影响前端要不要重绘，先记录不改。
* **O-2** `advice` 出现在 `if action in ('delete','done','set_mode','move','advice')`
  的 task_id 守卫元组里，但**没有任何 `if action == 'advice'` 分支**，请求会落到
  「未知操作：advice」。真正的建议接口是 `GET /api/advice`（`advice_payload`）。
  属于死引用，建议把那串元组里的 `'advice'` 删掉，避免读者误以为 command 支持它。
* **O-3** `preview_batch` 对「今天天气不错」这类没有地点的句子仍解析出 1 条
  （`ok=True kind=batch`）。不算契约破坏（前端还会二次确认），只是"解析器的宽容度"
  与"用户预期"的差距，价值判断留给产品。

---

## 3. PENDING-DECISION（需要产品/维护人拍板，测试未判失败）

1. **`set_profile` 给了不存在的档案名，是否该报错？**
   现状：`profiles.set_active()` 找不到就什么都不做，接口回
   `ok=True`「已切换到「默认」」——message 说的是**实际生效**的档案，不算撒谎，
   但用户以为切到了「张三」其实是「默认」。选项：A 回 `ok=False`「没有这个档案」；
   B 保持现状但 message 改成「没有「张三」，仍用「默认」」。
2. **只读动作成功时要不要带 message？**
   `db_stats` / `agent_skills` 成功时不带 `message` 键（其余动作都带）。契约里
   `message` 是"给人话"的字段，只读统计类可以不要；若前端有统一的"提示条"习惯，
   建议统一为 `message=''` 而不是缺键，省得前端做 `out.message || ''` 兜底。
3. **`consult_reset` 不带 `session` 回 `ok=True` 算不算合法？**
   现状是幂等 no-op。倾向于"合法"（重开对话本来就该随便点），但其语义与"缺参数
   必须 `ok=False`"的口径冲突，需要在契约文档里写明"无参即全局重置"。
4. **`toggle_offline` 的 `offline` 用 `bool()` 强转，`'??'`/`'false'` 都变成 True。**
   JS 侧发的是真布尔，Python 侧 `bool('false')` 是 True —— 一旦有脚本用字符串传参，
   "关闭离线"会反向执行。建议 `offline` 只认 `True/False/0/1`，其余按缺省处理。
5. **`apply_suggestion` 是接线还是删除？**
   前端从未调用（D-E5）。要么补齐接口层 + 前端"采纳建议"按钮，要么把这个 action
   连同分支一起删掉，别留一个永远 `ok=False` 的死动作在 36 个动作里。
6. **`delay` 的 `delay_min` 上界**：目前只校验 `> 0`，`delay_min=100000` 会让整条链
   推到次日之后。是否要按当日可排时段上限拒绝？

---

## 4. 一句话总结：最脆弱的三个动作

**`set_settings`（坏值落库后整天视图 500，且重启不自愈）、`apply_suggestion`（任何输入
都拿不到 `ok=True`，动作整体不可达）、`add`（预览卡里能改的 `draft` 字段没有校验，
用户手一抖就 500）**——三者都踩在"用户能直接触发"的路径上，`set_settings` 还带持久化污染，
应最先修；其余三个缺陷（13 处 `.strip()`、8 处 `int()`、复合参数结构）面更广但只是单次 500。
