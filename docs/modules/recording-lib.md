# 录制库

> 状态：✅
> 一句话：每次录制落盘成一条可复用资产，流程图节点按 **id** 引用它——
> 在库里改一次，所有引用它的节点一起变。

## 代码位置

- `core/recording_lib.py` — 存储层：一条录制一个 JSON 文件、原子写入、id 白名单
- `core/paths.py::recordings_dir()` — 库目录（`{app_dir}/recordings/`）
- `rpc/controller.py` — `record_lib_*` / `record_from_lib` / `record_lib_edit` 八个接口
  + 自动入库 + 名字注入
- `rpc/server.py` — `record.lib.*` / `record.fromLib` 八个方法名
- `tasks/builtin.py::RecordReplayTask` — `source` / `recording_id` 两个参数
- `tauri/src/flow/recording.ts` — 界面侧纯逻辑（下拉选项、缺失兜底、时长文案）
- `tauri/src/components/RecordPanel.vue` — 库的管理区（存/载入/直接编辑/落到画布/改名/删除）
- `tauri/src/utils/confirm.ts` — 删除前的**应用内**确认框（`window.confirm` 在本项目
  里拦不住任何东西，见 [frontend.md](frontend.md) 设计要点 15）
- `tauri/src/components/ParamsPanel.vue` — 节点参数里的「选择录制」下拉

## 功能清单

| 编号 | 功能 | 状态 | 说明 |
| --- | --- | --- | --- |
| F-LIB-01 | 一条录制一个文件 | ✅ | `{app_dir}/recordings/<id>.json`；坏一条只影响那一条（`list_meta` 跳过读不出来的） |
| F-LIB-02 | 原子写入 | ✅ | 先写 `.tmp` 再 `os.replace`；覆盖同 id 时保留原 `created_at` |
| F-LIB-03 | id 白名单 | ✅ | `^[A-Za-z0-9_-]{1,64}$`——id 直接拼路径，`../` 能写到任意位置 |
| F-LIB-04 | 列表不带 events | ✅ | 一条几百 KB，列表页（含参数面板的下拉）用不到事件内容 |
| F-LIB-05 | 自动入库 | ✅ | `record.stop` 落盘这次录制；**空录制不入**；入库失败只 warning，不让停止本身失败 |
| F-LIB-06 | 节点按 id 引用 | ✅ | `record_replay.source="录制库"` + `recording_id`；**取不到必须抛**，不静默回放空序列 |
| F-LIB-07 | 名字注入画布 | ✅ | `_public_node` 捎 `recording_name`/`recording_count`（走缓存），卡片显示名字而不是一串 id |
| F-LIB-08 | 过期可侦测 | ✅ | `_record_rev` 由后端维护（所有编辑经 `_record_snapshot`），`_lib_info().dirty` 是唯一判据 |
| F-LIB-09 | 八个 RPC | ✅ | `record.lib.list/save/rename/delete/edit/endEdit`、`record.fromLib`、`record.lib.toNode` |
| F-LIB-10 | 落到画布的快路径 | ✅ | 库里点一下直接建节点（只存 id），不必先载入缓冲再写入节点 |
| F-LIB-11 | **库里条目直接编辑** | ✅ | `record.lib.edit` 进入编辑模式，之后**每次编辑自动写回**库里那条；`endEdit` 退出。不必「载入 → 改 → 更新到库」三步（2026-09-17） |

## 验收记录

- **F-LIB-01~04**（2026-09-15）：`pytest tests/test_recording_lib.py -q`（22 例）。
  盯的是四类静默错法：
  ① **路径穿越**：id 含 `/`、`..`、空串、超 64 字符一律拒（真写文件前先过白名单）；
  ② **`meta_of` 的 `isinstance(events, list)`**：`len("not-a-list")` 也是 10，会凭空
     造出「10 条事件」的条目（点进去才发现回放什么都没发生）——这条是被测试抓出来的
     真 bug，修之前 `test_meta_of_tolerates_garbage` 挂在"假条数"上；
  ③ **不留 `.tmp` 残留**、覆盖同 id 时 `created_at` 不变；
  ④ 列表**不含 events**（`list_meta` 的每一项里没有 `events` 键）。
- **F-LIB-05**（2026-09-15）：`test_record_stop_auto_saves`、`test_empty_record_not_saved`。
  空录制（count=0）不入库；`record.stop` 的响应里带 `library` 元信息。
- **F-LIB-06~08**（2026-09-15）：`test_replay_from_library`（事件从库里取到）、
  `test_replay_library_missing_raises`（库里删掉后**必须抛**）、
  `test_replay_without_source_keeps_old_behavior`（缺 `source` 按内嵌算——
  判错会让所有历史流程一次性全废）、
  `test_public_node_injects_recording_name`（画布名字注入）、
  `test_lib_dirty_after_edit` / `test_lib_dirty_after_undo`（编辑/撤销都让库过期）、
  `test_lib_cache_invalidated_on_write`（写操作后缓存失效，否则改完名字画布还是旧的）。
- **F-LIB-09~10**（2026-09-15）：`test_record_from_lib_*`（载入后 `saved_rev` 对齐、
  界面显示「已同步」）、`test_lib_to_node_stores_only_id`（节点里只有 id，
  **不复制事件**——否则"改一次处处变"就不成立了）。
- **F-LIB-11**（2026-09-17，直接编辑）：8 个用例钉住三件事——
  ① **每次编辑都写回**：`test_lib_edit_writes_every_edit_back_to_the_library`、
  `test_lib_edit_undo_also_writes_back`（撤销也算一次改动，同样要落盘）、
  `test_lib_edit_covers_structural_edits_too`（增删/重排这些**结构性**编辑也写回，
  不只字段编辑）；
  ② **退出与误伤**：`test_lib_end_edit_returns_to_manual_update`、
  `test_plain_load_does_not_write_back_to_anything`（**纯「载入」不能写回**——
  它和「直接编辑」只差一个收尾，混淆了会让用户只是看一眼就把库改了）、
  `test_loading_another_recording_leaves_direct_edit_mode`、
  `test_stopping_a_new_recording_leaves_direct_edit_mode`（新录制与旧的编辑目标无关）；
  ③ **另存为要改目标**：`test_save_as_new_retargets_direct_edit`——存成新条目后
  继续编辑必须写回**新的**那条，否则用户刚存的副本与后续改动对不上。
  反向验证：变异「载入不再清掉写回目标」「另存为不重定向」均变红。
- **F-LIB-11 真机目视**（2026-09-17，安装版）：判据是**日志里的 RPC 帧 + 库文件的副作用**。
  点「直接编辑」→ `record.lib.edit {"id":"20260917-095335-f0b115"}`，浅蓝提示条出现；
  点「复制」→ `record.duplicate {"index":28}`，库文件 **42 → 43 条**（写回真的落盘了）；
  点「完成编辑」→ `record.lib.endEdit`，提示条消失；再点「直接编辑」→ 提示条回来。
  界面侧的完整验收表见 [frontend.md](frontend.md) 的 F-UI-18b 块。
- **界面侧**（2026-09-17）：`cd tauri && npm test` → `tests/recording.test.mjs` **19 例**
  （选项文案带条数 / 缺失兜底 / 时长文案 / 卡片名字 / 与组件接线，外加本次编辑区与
  「直接编辑」的 7 例）+ `flow-ports.test.mjs` 2 例 + `app-store.test.mjs` 3 例。
  反向验证见 [frontend.md](frontend.md) 的 F-UI-18 / F-UI-18b 验收块。

## 为什么需要它

录制原先只活在内存里（`controller._last_record`），「写入节点」把整段事件序列**内嵌**进
节点。于是：

- 同一段操作要用在两个地方就得录两遍，改了一处另一处不同步；
- 「先在 A 场景录好，再到 B、C 两个流程里复用」——最常见的用法——根本表达不了。

存进库里之后节点只记一个 id，回放时现取。**一处改，处处变**。

## 存储格式

一条一个文件：`{app_dir}/recordings/<id>.json`

```json
{
  "schema": 1,
  "id": "20260915-190433-a1b2c3",
  "name": "登录群聊",
  "created_at": "2026-09-15 19:04:33",
  "events": [ ... MacroEvent ... ],
  "origin": [0, 0]
}
```

**为什么一条一个文件**，而不是一个总 JSON：录制动辄几百 KB，总文件方式下改个名、
删一条都要整体重写；而且一个字节损坏就整个库读不出来。一条一个文件，坏一条只影响
那一条（`list_meta()` 会跳过读不出来的，不让整个面板打不开）。

### 三条实现约定

| 约定 | 为什么 |
| --- | --- |
| id 走 `^[A-Za-z0-9_-]{1,64}$` 白名单 | id 直接参与拼路径，`../` 能写到任意位置（甚至覆盖 `workflows/` 下的文件） |
| 写入先 `.tmp` 再 `os.replace` | 直接覆盖写，写到一半崩掉会留下半截 JSON——那一条录制就**永久损坏**了（用户的原始操作没法重来） |
| 覆盖同 id 时保留原 `created_at` | 库里按时间倒序排，改名/更新后跳到列表最前面会很怪 |

`meta_of()` 里那句 `isinstance(events, list)` 不能省：`len("not-a-list")` 也是 10，
会凭空造出一个「10 条事件」的条目，而面板上根本看不出它是假的（点进去才发现回放
什么都没发生）。

## 节点怎么引用

`record_replay` 的 `source` 取值是**跨语言协议**：

| `source` | `events` | 回放时 |
| --- | --- | --- |
| `内嵌`（默认，缺字段按这个算） | 事件序列存在节点里 | 直接用节点里的 |
| `录制库` | `[]`（空数组） | 用 `recording_id` 去库里现取 |

- **`内嵌` 是默认、也是老流程文件的隐含值**：老节点 `params` 里根本没有 `source`
  这个 key，判成「录制库」会让它们去查一个空 id、取不到就抛错——所有历史流程一次性全废。
- **取不到必须抛**，不能静默回放空序列：库里那条被删掉时，节点回放失败是**正确**行为
  （比"跑完了但什么都没做"好排查得多）。
- `origin_x/origin_y` 的 `show_if` 只对 `内嵌` 开放：用库里的录制时，原点是**那条
  录制自己的属性**（它是在哪块屏、哪个窗口位置录的）。节点再存一份就成了第二份真源。

### 边界名注入

节点里只存 id，画布上直接显示一串 `20260915-190433-a1b2c3` 没人认得出来。
`_public_node()` 会顺带给引用库的节点补上 `recording_name` / `recording_count`，
画布摘要显示名字（`tauri/src/flow/summary.ts` 里按 `ptype === "recording"` 取）。
查表走 `_lib_metas()` 的缓存——否则画布上每改一次参数都要全库扫一遍目录。

## 「库里那份过期了没有」

`_record_rev` 是唯一的判据，**不靠前端自己记「我改过没有」**：编辑接口有六七个，
漏掉一个就会出现「明明改了、库里还是旧的，而界面显示已同步」——而按 id 引用它的
节点回放的是库里那份，用户完全无从察觉。

- 所有编辑都必须先调 `_record_snapshot()`（它顺手 `rev += 1`）；撤销是唯一例外，
  它自己加。**新增编辑接口时别绕过这里**。
- `_lib_info()` 返回 `{id, name, dirty, editing}`，`dirty = saved_rev != rev`。

## 直接编辑库里的条目

老流程是「**载入 → 改 → 更新到库**」三步。想改库里某一条的坐标，得先把它载入
编辑缓冲（这会**替换掉当前正在编辑的那段**），改完再点「更新到库」。库里条目一多
就是纯粹的来回折腾。

`record.lib.edit` 把这三步并成一步：载入的同时**记下写回目标**
（`_record_lib_edit_id`），之后每次编辑自动落盘。区别只在收尾：

| 入口 | 编辑后 | 界面提示 |
| --- | --- | --- |
| `record.fromLib`（「载入」） | 只改内存，**要手动点「更新到库」** | `未保存 ●` |
| `record.lib.edit`（「直接编辑」） | **自动写回**库里那条 | `✎ 正在直接编辑库里的「…」——改动会立刻写回` |

**写回必须在后端统一做，前端不记账**：编辑接口有十几个（`remove` /
`removeMovesBefore` / `setOrigin` / `setText` / `keysToText` / `patch` / `insert` /
`duplicate` / `moveEvent` / `undo`…），它们全部收尾于 `_record_commit()` →
`_sync_lib_edit()`。让前端记「我调过哪些编辑接口、该写回了」必然漏一个，而漏的
表现是**库里还是旧的、界面却显示已同步**——最难发现的那类不一致（按 id 引用它的
节点回放的是库里那份，用户完全无从察觉）。

写回**失败只 `logger.warning`**，不把编辑本身判失败：改动仍在编辑缓冲里有效
（还能手动「更新到库」），不该因为落盘出问题就把整个操作回滚掉。

三条边界（都有用例钉住）：

- **纯「载入」必须退出编辑模式**（`record_from_lib` 清 `_record_lib_edit_id`）。
  不清的话，用户只是「载入看一眼」就把库改了。
- **新录制与旧目标无关**：`record_start` / `record_stop` 都清掉写回目标。
- **「另存为新的」要改目标**：`record.lib.save` 不带 id 时把 `_record_lib_edit_id`
  重定向到新条目，否则用户刚存完副本、继续编辑却写回到**旧**那条上。

## 自动入库

`record_stop` 在返回前把这次录制存进库（`stats["library"]` 是那条的元信息）：

- **空录制不入**——那只是点了一下按钮，放进库里纯属噪音；
- **入不进库不让「停止录制」失败**：事件还在内存里，用户仍能写入节点。所以
  `try/except` 只记 warning。

## 删除确认

`record.lib.delete` 走的是 `os.remove`——**真删，不进废纸篓**（工作流库那边是移进
废纸篓、还能捞回来，两者**不一样**）。所以确认文案必须写「会被永久删除，不能恢复」，
不能照抄工作流库那句。

确认框走 `tauri/src/utils/confirm.ts` 的 `confirmDanger`（TDesign `DialogPlugin`），
**且调用方必须 `await`**。不能用 `window.confirm`：Tauri 把它注入成
`async function(){ return await invoke("plugin:dialog|confirm", …) }`，返回的是
**Promise**（恒 truthy）⇒ 拦截条件恒不成立；而本项目**没授予 `dialog:allow-confirm`**
（`tauri-plugin-dialog` 的 `default` 只有 allow-message / allow-open / allow-save），
那个 invoke **被权限拒绝** ⇒ **连框都不弹**。合起来就是
「点『删除』→ 没有任何提示 → 文件没了」——**这不是理论风险，是真踩过的 bug**。

确认文案里要带**引用数**：节点只存 id，删库不会改节点，那些节点回放时才报
「录制不存在」。用户得在删之前知道有几个节点会坏。

⚠️ **真机目视已验**（2026-09-17，安装版）：造一条 → 点「删除」→ **确认框真的弹出来**
（修复前这里**什么都不会弹**）→ 点「取消」文件仍在 → 再点「删除」+「永久删除」→
文件真被删、列表回到「0 条」、日志里 `record.lib.delete` 紧接着 `record.lib.list`。
逐条结果见 [frontend.md](frontend.md) 的 F-UI-18 验收块。

## 已知问题

- **库里删掉一条录制，不会拦下引用它的节点**。节点只存 id，删库不改变节点数据；
  那些节点回放时会报「录制不存在」。界面上删除时确认框写了引用数（见上一节），
  但没有引用计数索引——节点多时要靠现扫一遍工作流。
- 库目录**固定**在 `{app_dir}/recordings/`，不能改到别处（如 iCloud、U 盘）。
  跨设备复用只能手工拷目录。
- **「直接编辑」期间没有「放弃改动」的出口**。编辑是即时写回的，所以后悔只能靠
  `record.undo`（撤销栈上限 20，超出就真回不去了）。要真正的「编辑会话」需要
  在库侧做一份影子文件 + 提交/回滚，暂未做。

## 变更记录

- 2026-09-15 初版：一条录制一个文件、原子写入、id 白名单、列表不带 events；
  `record.lib.list/save/rename/delete` + `record.fromLib` + `record.lib.toNode`
  六个方法；`record.stop` 自动入库；节点按 id 引用（`_public_node` 注入名字）。
  22 个 Python 用例 + 14 个前端用例
- 2026-09-17 **删除确认**：改走应用内 `confirmDanger`（`window.confirm` 在 Tauri
  下是 async 且无 `allow-confirm` 权限，**连框都不弹**——真踩过的数据丢失 bug），
  文案带引用数；真机目视已验
- 2026-09-17 **直接编辑库条目**（F-LIB-11）：新增 `record.lib.edit` /
  `record.lib.endEdit`，写回在**后端统一**做（`_record_commit` → `_sync_lib_edit`），
  `_lib_info()` 多一个 `editing` 标志。8 个用例 + 2 条反向验证。
  ⚠️ `record_lib_edit` 里**必须先设写回目标、再重建响应里的 `lib`**：
  它内部调的 `record_from_lib` 会构造一次 payload（那时 `_record_lib_edit_id` 还是
  `None`，纯载入会清掉它），顺序反了响应就是 `editing=False` ⇒ 前端提示条**不出现**，
  而写回其实已经在生效——最难发现的那类不一致。这条是跑**安装版**的
  `check_installed.py` 才抓出来的（单测当时只断言了 `lib.id` 与 `dirty`）。
  真机目视：点「复制」后库文件 42 → 43 条，写回确实落盘。
