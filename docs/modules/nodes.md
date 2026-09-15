# 节点体系

> 状态：✅
> 一句话：十二种内置节点，自描述参数定义驱动 UI 动态渲染，新增节点零 UI 改动；其中 4 种（start / condition / branch / end）承载流程图的出口。

## 代码位置

- `tasks/base.py` — BaseTask / ParamDef（含 `show_if` / `pick`）/ 注册表（register / all_definitions）
- `tasks/builtin.py` — 十二种内置节点实现 + `tolerant_event` + `_detect`（条件与分支共用的检测）
- `rpc/controller.py` — nodes_definitions()（含 COMMON_PARAMS 与菜单顺序）
- `tests/test_core.py` — `BUILTIN_MENU_ORDER`（菜单顺序的第二道锁，见设计要点 6）
- 出口名与遍历语义见 [flow.md](flow.md)

## 功能清单

| 编号 | 功能 | 状态 | 说明 |
| --- | --- | --- | --- |
| F-NOD-01 | 自描述参数定义 | ✅ | ParamDef(ptype/默认值/min/max) → 前端动态表单 |
| F-NOD-02 | 节点改名 | ✅ | Node.name 持久化，node.rename RPC，双击行内编辑 |
| F-NOD-03 | 节点 uid | ✅ | uuid4；边的两端也是 uid，不能用 type 或 index |
| F-NOD-04 | 条件显示参数 | ✅ | `ParamDef.show_if`，分支节点按 `case_count` 收起多余 case |
| F-NOD-05 | 参数旁挂文件选择 | ✅ | `ParamDef.pick`，给可编辑文本框加「选择」按钮 |
| F-NOD-06 | 注册表防漏 | ✅ | executor._run_node 内 import tasks.builtin 兜底 |
| F-NOD-07 | 画布坐标 | ✅ | `Node.x/y`；新增节点落在视口中心并错开 |
| F-NOD-08 | 通用参数已清空 | ✅ | `COMMON_PARAMS = []`——`run_when` 的语义搬到了边上 |

### 节点清单（内置节点一览）

`order` 列即添加菜单里的顺序，唯一真源是各节点的 `order`。

| order | 节点 | type | 状态 | 要点 |
| --- | --- | --- | --- | --- |
| 5 | 开始 | start | ✅ | 无参数；入口标记，不能有入边 |
| 10 | 鼠标操作 | mouse | ✅ | click/double_click/move/press/release，相对偏移 |
| 20 | 键盘输入 | keyboard | ✅ | text / 单键 / 组合键（keys 参数 ptype=`keys` 带捕获按钮） |
| 30 | 延时等待 | delay | ✅ | 两种等待方式：延时毫秒（毫秒）/ 等待至时刻（HH:MM[:SS]，已过则等明天）；两者按 `show_if` 互斥显示 |
| 40 | 录制回放 | record_replay | ✅ | 事件序列内嵌；速度/重复/相对坐标独立可调 |
| 50 | 图像匹配点击 | image_click | ✅ | mss 截屏 + cv2 模板匹配 |
| 60 | 找文字点击 | ocr_click | ✅ | macOS Vision 离线 OCR，中英文 |
| 70 | YOLO 找目标点击 | yolo_click | ✅ | onnxruntime/CoreML，yolo11n.onnx（COCO 80 类） |
| 80 | 条件判断 | condition | ✅ | 图像存在 / 文字存在 / 目标存在(YOLO)；出口 `true` / `false` |
| 82 | 条件组 | condition_group | ✅ | 最多 6 项检测按「与 / 或」合成一个判断；出口沿用 `true` / `false`；求值**短路** |
| 85 | 多路分支 | branch | ✅ | 2~6 个情形，按顺序取第一个命中；出口 `case:N` / `else` |
| 90 | 注释 | note | ✅ | 不执行，仅说明；执行时**跳过并继续** |
| 95 | 结束 | end | ✅ | 无参数；终止当前路径，没有出口 |

## 验收记录

- **F-NOD-01**（2026-09-05，v4 后仍适用）：`test_all_nodes_have_definitions` + 真机添加各类节点参数面板动态渲染。
- **F-NOD-02**（2026-09-12）：RPC 探针 node.rename → 树更新 + JSON 落盘含 name；真机双击节点出现行内编辑框。
- **F-NOD-03**（2026-09-12）：同类型多节点拖拽不再错乱（item-key=uid）；
  v4 起边的两端也是 uid，见 `test_flowchart_edges_and_positions`。
- **F-NOD-04**（2026-09-13）：真机选中分支节点，`case_count` 调到 2 时只显示
  `case1_*` / `case2_*`；调到 4 时多出两组。参数默认值缺失时也正确显示（见设计要点 4）。
  同一条机制也用在 `delay` 上（2026-09-15）：「延时毫秒 / 等待至时刻」两个字段
  按 `mode` 互斥显示。
- **F-NOD-05**（2026-09-13）：真机在 `case1_value`（可编辑文本框）旁点「选择」，
  挑到的路径写回该字段——`file` 类型是只读的，装不下「也可能是文字」的取值。
- **F-NOD-06**（2026-09-13）：`test_all_nodes_have_definitions` +
  `test_node_menu_order_is_explicit`（顺序切片 + `len(defs) == n` 双重比对；
  反向验证：把 `BUILTIN_MENU_ORDER` 末尾的 `end` 删掉，切片断言照过、
  新加的数量断言失败并点名 `['end']`）。
- **F-NOD-08**（2026-09-13）：`test_rpc.py` 断言 `nodes.definitions` 的
  `common_params == []`——把「已清空」锁住，防止有人顺手加回一个全局参数。

## 设计要点

1. **自描述参数**（借鉴 LCA）：节点类声明 `ParamDef(key,label,ptype,default,…)`，
   `nodes.definitions` 下发，前端 ParamsPanel 按 ptype 渲染
   （int/float/bool/select/text/keys/file/events）。加节点不改 UI。

2. **`COMMON_PARAMS` 现在是空的**（v4）。它原来放的是 `run_when`
   （总是 / 条件成立 / 条件不成立）——那是「有序列表 + 全局条件标志」时代的产物。
   语义搬到边之后，一个节点跑不跑由「有没有边走进来」决定，`run_when` 无处安放。
   **旧文件里残留的 `run_when` 参数刻意不清理**：清理掉的话用户打开旧文件会发现
   自己的配置凭空少了东西，而留着它至少能在参数面板上看到「这个字段还在，
   只是不再起作用」。前端不渲染它（定义里没有了），后端也不读它。

3. **`show_if` 是必需的，不是优化**。分支节点有 6 组 case 字段，全平铺出来是
   14 个字段的一堵墙，而用户通常只用 2~3 个。`show_if: {key:"case_count", gte:N}`
   让第 N 组只在 case 数够时才出现。

4. **`show_if` 求值必须回落到 ParamDef 的默认值**。节点刚添加时 `params` 里
   根本没有 `case_count` 这个 key，直接读 `node.params[key]` 会得到 `undefined`，
   `undefined >= 1` 为 false，于是**所有** case 字段都不显示——用户看到的是一个
   只剩「情形个数」的分支节点。`ParamsPanel.paramValue()` 因此先读节点、再读默认值
   （默认值正是后端实际会用的值）。

5. **`pick` 而不是把字段改成 `file`**。分支的取值既可能是模板图路径、
   也可能是一段要找的文字，取决于它上面的「检测方式」。`file` 类型是**只读**的
   （只能靠文件对话框写），装不下文字。所以给可编辑文本框加一个「选择」按钮：
   想点选就点选，想手打就手打。

6. **菜单顺序的两道锁，以及第二道原来有个洞**。唯一真源是各节点的 `order`
   （`rpc/controller.nodes_definitions` 直接沿用 `all_definitions()` 的顺序，
   不再自己排一次）。`tests/test_core.py::BUILTIN_MENU_ORDER` 是手写的期望顺序，
   供两个用例比对。
   原来的断言是 `defs[:len(BUILTIN_MENU_ORDER)] == BUILTIN_MENU_ORDER`——
   **新节点的 `order` 若比现有都大（追加到菜单末尾，最常见），这个切片仍然等于
   旧列表，测试照过**，新节点完全没被覆盖。2026-09-13 补上 `len(defs) == n`
   才把这个洞堵住（断言消息会直接点名漏掉的 type）。
   所以新增内置节点时要同时动两处：节点的 `order`，以及 `BUILTIN_MENU_ORDER`
   （放在它应有的位置，不是追加到末尾——放错位置切片断言会抓，放末尾现在也能抓）。
   ⚠️ 这条 `len(defs) == n` 断言**刚加上就抓到了另一类问题**：它只在**全量**
   运行时失败、单跑该用例永远通过。真因是 `tasks.base._REGISTRY` 是进程级全局，
   前面 `test_executor_*` 里注册的探针节点（`_test_slow` / `_test_bad`，
   order 都是默认的 100）会一直留到进程结束，`all_definitions()` 就多出两条。
   已在 `tests/conftest.py` 加 autouse 的 `restore_task_registry` 兜住
   （快照 → 还原，且快照前先 `import tasks.builtin`，否则可能快照到空注册表）。
   用例里注册探针节点不必再自己写清理。

7. **`_detect()` 由 condition 与 branch 共用**。两个节点的「怎么算命中」
   必须完全一致——否则同一种检测在条件节点里成立、在分支节点里不成立，
   用户完全无从理解。共用同一个函数是唯一的保证方式。

8. **`branch` 按顺序取第一个命中的 case，不做「挑最好的」**。
   顺序本身就表达优先级（用户把更具体的检测放前面），
   「挑置信度最高的」会让排列顺序失去意义，而用户排顺序时以为它有。

9. **events 摘要边界**：`record_replay.params.events` 数组在对外视图里替换为
   `{count}`（rpc/controller._public_node，浅拷贝视图不改真源）。前端从不读写
   事件数组；保存/回放都在后端完成。

10. **tolerant_event**：事件构造容忍缺/多字段；非 dict（损坏文件、摘要对象）返回
    None 跳过——防御 'str' has no 'get' 一类崩法。

## 已知问题

- 小键盘键（Keypad0…）可录制但 pynput 无对应键，回放时静默跳过。
- keyboard text 模式中文走 CGEvent Unicode 通道（2026-09-12 修复），长文本注入
  速率受系统限制。
- **把 `case_count` 调小不会清理超出范围的 `caseN_*` 参数**，它们只是被
  `show_if` 藏起来（与边同理：调回去还在）。文件里会留下看不见的字段。
- `condition_group` 把 `cond_count` 调小同样不清理超出范围的 `condN_*` 参数
  （与上一条同理：只是被 `show_if` 藏起来，文件里还在）。
  另外它**刻意没有**「至少 N 项成立」这类参数——那属于多路分支的范畴，
  两个节点各管一件事比在一个节点里堆参数好理解。

## 变更记录

- 2026-09-05 图像/OCR/YOLO/条件节点逐个落地（见 git 历史）
- 2026-09-12 节点改名 + uid + keys ptype 修正
- 2026-09-13 文档结构对齐 `_template.md`：原独立的「节点清单」章节降为
  「功能清单」下的 `###` 子节（`docs/README.md` 要求所有模块文档一律用模板结构）
- 2026-09-13 **v4 节点体系**：新增 `start`(5) / `branch`(85) / `end`(95)；
  `condition` 改用出口名；`COMMON_PARAMS` 清空（`run_when` 退场）；
  ParamDef 新增 `show_if` / `pick`（F-NOD-04/05/07/08）
- 2026-09-15 **`delay` 增加「等待至时刻」**：`mode` 选「延时毫秒 / 等待至时刻」，
  后者接 `at`（`HH:MM` 或 `HH:MM:SS`，已过则等明天的同一时刻），两个字段按
  `show_if` 互斥显示。墙钟语义**不随速度倍率缩放**；等待按 1s 分段、每段用墙钟
  重算剩余（`player.wait` 走 monotonic，macOS 睡眠期间不走，一次睡到底会在
  系统睡醒后多等一整段）。新增 6 个用例
- 2026-09-15 **新增 `condition_group`（条件组，order 82）**：最多 6 项检测按
  `combine`（「全部成立(与) / 任一成立(或)」）合成一个判断，出口沿用
  `true` / `false`——与 `condition` 同形，所以**后端的边表不用为新节点做任何
  特殊处理**。求值短路（「与」遇假即停、「或」遇真即停），用户的排序因此获得
  「优先被检测」的含义。新增 9 个用例（`tests/test_core.py`）+ 1 个前端出口断言
