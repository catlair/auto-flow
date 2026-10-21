# 工作流库

> 状态：✅
> 一句话：流程图落盘成库里一条条可管理的资产，菜单里直接**列表 / 打开 / 运行 /
> 更名 / 删除 / 导入导出**，路径不再需要用户操心。

## 代码位置

- `core/workflow_lib.py` — 存储层：一份工作流一个 JSON 文件、原子写入、id 白名单、
  移进废纸篓、导入导出
- `core/paths.py::workflows_dir()` — 库目录（`{app_dir}/workflows/`，此前已存在但闲置）
- `rpc/controller.py` — `workflow_lib_*` 七个接口 + `workflow_dirty` / `_guard_unsaved`
  + `workflow_save` 的自动入库
- `rpc/server.py` — `workflow.lib.*` 七个方法名；`workflow.new` / `workflow.lib.open`
  多一个 `force`
- `tauri/src/flow/workflow.ts` — 界面侧纯逻辑（副标题、导出文件名、数据规整）
- `tauri/src/components/WorkflowMenu.vue` — 新建 / 工作流库 / 保存 + 未保存标记
- `tauri/src/components/WorkflowLibDialog.vue` — 库弹层（列表 + 五个动作 + 导入导出）
- `tauri/src/utils/confirm.ts` — 应用内确认框（不依赖 WebView 对 JS 原生对话框的支持）
- `tauri/src/components/ScheduleDialog.vue` — 定时运行改为从库里选工作流

## 功能清单

| 编号 | 功能 | 状态 | 说明 |
| --- | --- | --- | --- |
| F-WLIB-01 | 一份工作流一个文件 | ✅ | `{app_dir}/workflows/<id>.json`；坏一份只影响那一份（`list_meta` 跳过读不出来的） |
| F-WLIB-02 | 原子写入 | ✅ | 先写 `.tmp` 再 `os.replace`；覆盖同 id 时保留原 `created_at` |
| F-WLIB-03 | id 白名单 | ✅ | `^[A-Za-z0-9_-]{1,64}$`——id 直接拼路径，`../` 能写到任意位置 |
| F-WLIB-04 | 列表不带 nodes/edges | ✅ | 一份流程几十个节点，节点参数里还可能内嵌 MB 级录制事件 |
| F-WLIB-05 | 按**文件 mtime** 倒序 | ✅ | 用户可能用 Finder / 编辑器直接改库里的文件，那时 JSON 里的时间戳是陈旧的 |
| F-WLIB-06 | **新建 → 保存**自动入库 | ✅ | 修掉「必须先另存为才能存下第一版」这条死路 |
| F-WLIB-07 | 未保存标记 | ✅ | 唯一判据是后端自增的版本号（`_wf_rev != _wf_saved_rev`），不靠前端记账 |
| F-WLIB-08 | 未保存保护 | ✅ | 丢弃前必须 `force`，否则 `-32006`；判断在后端，前端只负责把它翻译成确认框 |
| F-WLIB-09 | 七个 RPC | ✅ | `workflow.lib.list/save/open/rename/delete/import/export` |
| F-WLIB-10 | 删除进废纸篓 | ✅ | 移进 `~/.Trash`，跨卷失败**报错**而不是退化成删除 |
| F-WLIB-11 | 导入拷贝 / 导出剥离元信息 | ✅ | 导入是**拷贝**；导出文件与旧的 `Workflow.save` 逐字节同构 |
| F-WLIB-12 | 列表直接运行 | ✅ | 载入当前画布并开跑；**运行中拒绝切换**（`toggleRun` 会把「启动」当「停止」） |
| F-WLIB-13 | 定时运行从库里选 | ✅ | 库外路径（老配置）补一项显示，不让下拉变成空白 |

## 验收记录

- **F-WLIB-01~05**（2026-09-16）：`pytest tests/test_workflow_lib.py -q`（36 例）。
  盯的是五类静默错法：
  ① **路径穿越**：id 含 `/`、`..`、空串、超 64 字符一律拒；
  ② **`id_of_path` 只看文件名不看父目录**：`/tmp/saved.json` 会被判成库条目
     `saved`——用户按「导出到桌面」，文件却静默落进了 `workflows/`，而界面报的是
     「已导出到 /Users/…/Desktop/saved.json」。**这条是反向验证时从
     `test_workflow_load_reports_list_migration` 的 FileNotFoundError 里顺出来的**，
     补了专门的 `test_save_to_an_external_path_does_not_land_in_the_library`；
  ③ **`data.get("nodes") or data.get("events")`**：空列表是 falsy，而 `nodes: []`
     正是「新建后还没加节点」——写成 or 链会把**每一份空工作流**判成格式损坏，
     症状是新建的条目改不了名、也打不开（被 `test_save_with_same_id_...` 等 5 条抓住）；
  ④ 覆盖同 id 时 `created_at` 不变、不留 `.tmp` 残留；
  ⑤ 列表**不含 nodes/edges**。
- **F-WLIB-06~08**（2026-09-16）：`test_new_then_save_creates_a_library_entry`、
  `test_save_twice_keeps_one_entry`、
  `test_dirty_flag_follows_every_kind_of_edit`（加节点/改参数/改名/停用/移动/改流名/
  加连线/删节点**八种**改动逐条验）、`test_new_refuses_to_discard_unsaved_changes`、
  `test_lib_open_refuses_to_discard_unsaved_changes`、
  `test_broadcast_carries_the_dirty_flag`。
- **F-WLIB-09~13**（2026-09-16）：`test_workflow_lib_rpc_roundtrip`（**真实 sidecar**，
  18 步往返；刻意不放在进程内的 `test_workflow_lib.py`——进程内调用绕过了
  `rpc/server.py` 的方法名注册，注册表少一条时进程内测试全绿而安装版点了没反应）、
  `test_remove_moves_the_file_to_trash_instead_of_deleting`、
  `test_import_copies_the_file`、`test_export_strips_library_metadata_and_round_trips`、
  `test_lib_delete_of_current_keeps_the_tree_but_clears_the_path`、
  `test_lib_export_of_current_uses_the_canvas_state`。
- **界面侧**（2026-09-16）：`npm --prefix tauri test` → 203 例（新增
  `tests/workflow-lib.test.mjs` 10 例 + `app-store.test.mjs` 11 例）。
  其中一条是**跨语言常量一致性**：直接读 `rpc/controller.py` 取出 `unsaved_changes`
  的错误码，再断言两个 `.vue` 里写的是同一个数。
- **真机目视验收**（2026-09-16，安装版）：菜单三项、加节点后橙色 `●` 立刻出现、
  按保存后 `●` 消失且库里多一条、库弹层的空状态/列表行/五个动作、行内改名输入框
  能打开、**两个确认框都渲染在应用内且按钮真的能点**（点「丢弃并新建」后日志出现
  `workflow.new {"force": true}`）。逐条结果与两处未验到的（行内改名的提交、
  删除的 UI 路径）见 [frontend.md](frontend.md) 的 F-UI-20 验收记录。
- **反向验证**（2026-09-16）：9 个变异全部被抓到，且**逐条看是哪些红了**，不是只数条数。

  | 变异 | 红了 |
  | --- | --- |
  | `remove` 用 `os.remove` 真删 | 2 条 |
  | `id_of_path` 不看父目录 | 2 条（+ 顺出上面 ②） |
  | `_has_node_list` 用 `or` 链 | 5 条 |
  | `_guard_unsaved` 永远不拦 | 3 条 |
  | `_broadcast_workflow` 不自增 rev | 7 条 |
  | 保存没路径时报错（回到旧行为） | 14 条 |
  | 前端 `applyLocation` 用 `typeof` 判 path | 1 条 |
  | 前端 `wfExportName` 不洗 `/` 与 `:` | 2 条 |
  | 前端 `applyWorkflow` 无条件覆盖 dirty | 1 条 |

- **安装版校验**（2026-09-16）：`scripts/check_installed.py` 新增 **8 条产物级**
  断言（`core.workflow_lib` 进包、`remove` 走 `shutil` 而不是 `os.remove`、
  `_has_node_list` 用 `any(...)`、有 `id_of_path`/`path_of`、
  `workflow_save` 里出现 `workflow_lib`、`workflow_dirty` 比的是两个版本号、
  `_guard_unsaved` 用 `-32006`、`rpc.server` 注册了七个 `workflow.lib.*`）
  + **14 条 RPC 级**断言（`dirty` 用行为验、未保存保护两条、保存覆盖同一条、
  导出剥元信息、导入不切换、删除进废纸篓、删当前那份不清画布等）。
  **区分度**：对**上一版** sidecar（`dist/.autoflow-sidecar.old.20260916-215942`，
  它确实不含 `core.workflow_lib`）跑同 40 条产物级断言 → 这 8 条**全部 ✗**、
  其余 32 条不受影响；已安装版 40 条全 ✓。基线是**真实存在过的上一版产物**，
  比手搓变异更有说服力。
  ⚠️ 校验会话的**废纸篓是临时目录**（`AUTOFLOW_TRASH_DIR`）——原先它往用户的真
  废纸篓里丢文件且「清理」静默失效（macOS 对 `~/.Trash` 的程序化删除与列目录都有
  TCC 保护）。现在断言里多了一条「`trash` 路径落在本次临时废纸篓下」，证明没碰
  用户的真废纸篓。三个真踩到的坑（`_load_pyz` 提取清单漏项、校验脚本自己的期望写错、
  废纸篓假绿）记在 [packaging.md](packaging.md) 变更记录。

## 为什么需要它

此前的「新建 / 打开 / 保存 / 另存为」是四个直通系统文件对话框的按钮，流程文件散落
在用户随手挑的位置。于是：

- 「我上周那个流程存哪了」只能回 Finder 里翻；
- **「新建 → 保存」必然失败**：`workflow.new` 把路径清成 `None`，`workflow.save`
  不传 path 就抛「尚未指定保存路径」——用户必须先「另存为」才能存下第一版；
- 改个名要回 Finder 里改文件名（还可能顺手改坏扩展名），删一个同理。

存进库里之后一份一个文件、列表里直接管。文件对话框降级成「导入 / 导出」——
只用来跟外部交换，不再是日常操作。

## 存储格式

一份一个文件：`{app_dir}/workflows/<id>.json`（打包后 `app_dir` 是
`~/Library/Application Support/AutoFlow`）

```json
{
  "schema": 1,
  "id": "20260916-213325-9e448d",
  "name": "登录流程",
  "created_at": "2026-09-16 21:33:25",
  "version": 4, "speed": 1.0, "repeat": 1, "start": "",
  "nodes": [ ... ], "edges": [ ... ]
}
```

前四个键是**库自己**的元信息，`Workflow.load()` 会忽略它们（向后兼容）；
导出时会被剥掉，让导出文件与旧的 `Workflow.save` 写出来的逐字节同构。

### 三条实现约定

| 约定 | 为什么 |
| --- | --- |
| id 走 `^[A-Za-z0-9_-]{1,64}$` 白名单 | id 直接参与拼路径，`../` 能写到任意位置（甚至覆盖录制库里的文件） |
| 写入先 `.tmp` 再 `os.replace` | 直接覆盖写，写到一半崩掉会留下半截 JSON——那一份流程就**永久损坏**了（几十个节点的图没法重画一遍） |
| 覆盖同 id 时保留原 `created_at`；改名**连 mtime 一起保留** | 库里按时间排，改名/更新后跳到最前会很怪——看着像多了一份 |

## 「当前这份在不在库里」怎么判

`workflow_lib.id_of_path(path)` 是**唯一判据**，不另外记一份 `_lib_id` 状态：
两份状态总有一天会对不上，而对不上的表现是「保存写进了 A 文件、界面显示已保存到
B」——用户的改动看着存了、实际进了另一个文件。

两个细节都不能省：

- **必须比父目录**，不能只看 basename。只看 basename 的话 `/tmp/saved.json` 会被
  判成库条目 `saved`，于是「导出到桌面」静默写进了库目录。反向验证时这条是从别的
  用例的 `FileNotFoundError` 里顺出来的。
- **必须是 `workflows_dir()` 的直属子文件**：`workflows/templates/` 也在库目录下面，
  把它当库条目会让「另存为到模板」变成覆盖某条工作流。

## 「有没有未保存的改动」怎么判

`_wf_rev` / `_wf_saved_rev` 两个版本号比出来的，**钩在 `_broadcast_workflow` 上**：
16 个改动接口全部经由它广播，钩一处就全盖住了。放进各个接口的话，将来加第 17 个时
忘了加一行，症状是「改了但保存按钮不亮」——静默的数据丢失，最难查。

不靠前端自己记「我改过没有」：定时任务会在后台直接换掉整棵工作流树
（`_schedule_fire`），那条路上前端什么都没发过。

### 未保存保护为什么在后端

`workflow_new` / `workflow_lib_open` 在 dirty 且没给 `force` 时抛 `-32006`。
前端确认框只是**体验**，后端这一层是**保证**：前端漏了（或将来多出第二个调用方）
就会静默丢掉用户刚连好的一整张图，而那之后没有任何地方能找回来——`workflow.new`
不落盘、也没有撤销。

`workflow.load`（按路径打开）**刻意不加这个保护**：它是低层的原语，
`scripts/check_installed.py` 要在一段会话里连续 load 好几个样本文件，被一个确认框式
的错误码挡下来会表现成「安装版校验突然红了」，而原因跟被测功能毫无关系。

## 为什么「新建」不立刻落盘

原始需求里写的是「新建即在库里落盘一份」，实现改成了**第一次保存时才建**：

- 新建即入库的话，「点开看看」也会在库里留下一个空文件，试几次就攒出一堆
  「工作流 2026-09-16 21:33:25」把列表糊住；
- 而「保存按钮任何时候都可用」这个目标由 `workflow_save` 在**没有路径时自动入库**
  达成，不依赖「新建时就建文件」。

于是「显式保存 + 未保存标记」的语义一点没变，还少了一类噪音条目。

## 界面

- 菜单：`新建 | 工作流库 | 保存` + 当前工作流名（未保存时后面跟一个 `●`）。
  标记做成名字的一部分而不是一个独立圆点：6 像素的点在浅色标题栏上很容易被当成
  渲染瑕疵，而用户读的是这行字。
- 库弹层：列表（名称 / 节点数 / 连线数 / 最近修改 / 「当前」徽标）+ 五个动作
  （运行 / 打开 / 更名 / 导出 / 删除）+ 导入。更名是**内联**编辑，回车或失焦提交。
- 删除走应用内确认框，正文写清两件事：文件进了废纸篓（能捞回来）、删的是当前那份时
  画布内容会保留但不再对应文件。
- 运行会**先关掉弹层**：运行是要看着画布的，留着弹层挡在前面等于什么都没发生。

### 为什么用 TDesign 的 DialogPlugin 而不是 `window.confirm`

`window.confirm` 由 WebView 宿主决定要不要接（WKWebView 上要有 UIDelegate 的回调）。
一旦宿主没接，它就是个**静默返回 false 的空函数**——用户点「确定」什么都没发生，
而返回 false 意味着「不改了」，于是「丢弃未保存的改动」「移到废纸篓」永远做不成，
界面上却看不出任何异常。`DialogPlugin` 渲染在应用窗口内，不依赖宿主。

> 注：`RecordPanel.vue` 删除录制时仍在用 `window.confirm`。那处不在本次范围内，
> 但值得一起收掉——同一个风险，同一个修法。

## 已知问题

- **删掉一份工作流不会拦下引用它的东西**。定时运行按**路径**引用（
  `schedule.workflowPath`），删掉之后那个定时会在下次触发时报
  `workflow_missing`（`schedule.fired` 里带 reason），但配置不会自动清掉。
- 库目录**固定**在 `{app_dir}/workflows/`，不能改到别处（如 iCloud、U 盘）。
  跨设备复用只能手工拷目录，或用「导入 / 导出」一份份搬。
- **没有搜索 / 分组**。条目多起来（>50）之后列表会变长，目前只能靠按最近修改排序。
- 运行中从库里切一份会被拒（这是刻意的），但**没有「先停再切」的一键操作**，
  用户得自己先按停止。
- `AUTOFLOW_DATA_DIR` 变化（例如从开发态切到打包态）之后，原先指向老库目录的路径
  不再被 `id_of_path` 认作库条目，那份工作流会退化成「库外文件」——文件本身没坏，
  但它不再出现在列表里。

## 变更记录

- 2026-09-16 新建。存储层 `core/workflow_lib.py` + 七个 `workflow.lib.*` RPC +
  菜单/库弹层 + 未保存标记与保护 + 删除进废纸篓 + 导入导出；顺带修掉
  「新建 → 保存必然失败」和「定时运行要去 `~/Library/Application Support` 里翻文件」。
- 2026-09-16 安装版校验补 8 条产物级 + 13 条 RPC 级断言。同轮修掉
  `check_installed.py::_load_pyz` 的提取清单漏了 `core.workflow_lib`（4 条断言
  因此恒为 ✗，失败信息却指向「模块没打进包」这个假原因），以及三条 RPC 断言
  自己的期望写错（故意留下未保存改动之后又没带 `force` 调 `workflow.lib.open`，
  被后端正确拒掉，而断言按成功算）。
