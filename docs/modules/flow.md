# 流程图模型

> 状态：✅
> 一句话：v4 的工作流是**节点 + 有向边**的流程图——出口名决定下一跳，多条边指向同一节点即汇合，允许回边（有步数上限兜底），旧的有序列表文件自动迁移。

## 代码位置

- `core/events.py` — `Workflow{nodes,edges,start}` / `Edge` / 出口名常量 / `_linear_edges` + `_linear_positions`（迁移）
- `core/executor.py` — `entry_uid` / `next_map` / `_walk`（图遍历）
- `tasks/builtin.py` — 承载出口的节点：`start` / `condition` / `branch` / `end`
- `rpc/controller.py` — `edge.add` / `edge.remove` / `node.setPos` / `workflow.setStart`
- `tauri/src/flow/ports.ts` — 前端唯一的出口名定义（与后端逐字对齐）
- `tauri/src/components/FlowCanvas.vue` — 画布（@vue-flow/core）
- `tauri/src/flow/fitOnce.ts` — 首次适应视野的时机状态机（等 `nodes-initialized` 才 fit）
- `tauri/tests/flow-ports.test.mjs` — **跨语言常量对齐测试**（直接读 `core/events.py` 比对）

## 功能清单

| 编号 | 功能 | 状态 | 说明 |
| --- | --- | --- | --- |
| F-FLW-01 | 节点 + 有向边数据模型 | ✅ | `Edge(src, port, dst)`，两端都是 **uid**（不是下标） |
| F-FLW-02 | 出口名（port） | ✅ | `out` / `true` / `false` / `case:N` / `else`；`end` 没有出口 |
| F-FLW-03 | 一个出口一条边（无扇出） | ✅ | `edge.add` 替换同 `(src, port)` 的旧边，并向调用方回报「发生了替换」 |
| F-FLW-04 | 汇合 | ✅ | 两条边指向同一节点即汇合，无特殊语法 |
| F-FLW-05 | 条件分派 | ✅ | `condition` 节点按检测结果写 `true` / `false` 出口 |
| F-FLW-06 | 多路分支 | ✅ | `branch` 按顺序取第一个命中的 `case:N`，都不命中走 `else` |
| F-FLW-07 | 显式终点 | ✅ | `end` 节点终止当前路径并上报一次 |
| F-FLW-08 | 循环 + 步数上限 | ✅ | 允许回边；`MAX_STEPS=10000` 兜底，撞上限即停机并报错 |
| F-FLW-09 | 起始节点解析 | ✅ | 显式 `start`（且节点还在）> 第一个 `start` 节点 > 第一个节点 |
| F-FLW-10 | 停用/注释节点是「跳过」不是「截断」 | ✅ | 返回 `PORT_OUT` 继续走；`None` 保留给「执行失败」 |
| F-FLW-11 | 节点画布坐标 | ✅ | `Node.x/y`，后端只存不算；拖拽**结束**才提交一次 |
| F-FLW-12 | 旧文件迁移 | ✅ | 线性边链 + 横向摆开坐标；`run_when` 失效并由界面提示重连 |
| F-FLW-13 | 删除节点同时清边 | ✅ | `node.remove` 一并清掉连着它的边与指向它的 `start` |
| F-FLW-14 | 出口名跨语言一致 | ✅ | 前端常量由测试直接读 `core/events.py` 比对，防止静默漂移 |

## 验收记录

- **F-FLW-01/02/09**（2026-09-13）：`pytest tests/test_core.py -k "start_uid or falls_back_to_first_node"`
- **F-FLW-04/05**（2026-09-13）：`test_executor_condition_routes_by_port`、
  `test_executor_condition_false_routes_to_other_branch`、
  `test_executor_merges_when_two_edges_point_at_one_node`
- **F-FLW-06**（2026-09-13）：`test_executor_branch_routes_to_matching_case`、
  `test_executor_branch_falls_back_to_else`
- **F-FLW-07**（2026-09-13）：`test_executor_end_node_stops_that_path`
- **F-FLW-08**（2026-09-13）：`test_executor_runaway_cycle_is_stopped_and_reported`
  （断言**至少**跑了 `MAX_STEPS - 2` 步，不只是「停了」）、
  `test_executor_cycle_terminates_when_edge_is_dropped`
- **F-FLW-10**（2026-09-13）：`test_executor_disabled_node_passes_through`
- **F-FLW-11/13**（2026-09-13）：`pytest tests/test_rpc.py -k flowchart`；
  前端 `npm --prefix tauri test` 的 `节点坐标只在拖拽结束时提交一次`、
  `删除别的节点时，选中跟着 uid 走而不是跟着下标漂移`
- **F-FLW-13 补充**（2026-09-15）：节点改动按 **uid** 寻址（下标在「一次删多个」
  时会指向别人，删错且不报错）——`test_node_mutations_prefer_uid_over_stale_index`
  （故意送错的下标 + 正确的 uid，断言死的是 uid 指的那个；反向验证：让 `_index_of`
  忽略 uid 后该用例失败并删掉了 a）、前端
  `节点改动按 uid 寻址：多选删除不会删错人`（反向验证：`removeNode` 改回送 index
  后 2 个用例失败）
- **F-FLW-12**（2026-09-13）：`test_workflow_load_migrates_linear_list`、
  `test_migration_lays_nodes_out_instead_of_stacking_them`、
  `test_migration_keeps_hand_written_coordinates`、`test_v4_load_does_not_touch_positions`、
  `test_migrated_flag_is_not_persisted`；前端
  `旧版迁移提示只弹一次，换成非迁移工作流后撤掉`
- **F-FLW-14**（2026-09-13）：`npm --prefix tauri test` 的
  `出口名与 core/events.py 逐一对齐`（反向验证：把 `PORT_TRUE` 改成 `"yes"`
  会让 5 个用例失败）
- **整体**：`pytest tests/ -q` → **174 passed**；`npm --prefix tauri test` → **71 passed**

## 设计要点

1. **为什么必须从「有序列表」换成图**：v3 只有「有序列表 + 全局 `run_when` 标志」。
   `condition` 节点写一个**全局**的「最近一次条件结果」，之后每个节点按自己的
   `run_when`（总是 / 条件成立 / 条件不成立）决定跑不跑。问题在于那个标志是**一个**，
   所以「条件成立走 A 组、不成立走 B 组、两组再汇合」根本表达不出来——
   所有在条件之后的节点都被同一个标志门控。v4 把「走哪条」变成边上的显式信息。

2. **边的两端是 uid 而不是下标**。下标会随增删移动漂移：删掉第 3 个节点，
   所有 `index > 3` 的连线就会整体接到别人身上——**静默接错人**，比报错难查得多。
   `uid` 由后端在创建时生成（`uuid4().hex`），只在节点真的被删时才消失。

   同一条理由**同样适用于改节点本身**（`node.remove` / `rename` / `toggle` /
   `params.set`）。2026-09-15 修掉了一处漏网：这几个方法原本收下标，前端在调用前
   把 uid 换算成下标送过去——而画布上「多选后按 Delete」会给每个 remove 各发一次
   请求，它们**都按同一份删除前的列表**算下标，于是第二笔起指向别的节点，
   **删错且不报错**（uid→下标的换算在这里是纯损失，前端本来就握着 uid）。
   现在后端 uid 优先、前端直接送 uid；多个删除仍然**串行 await**，
   因为并发的 `workflow.changed` 会互相覆盖前端状态、让画布闪一下。

3. **一个出口只允许一条边（不允许扇出）**。扇出意味着「一个出口同时跑两条路径」，
   那要引入并行执行；而并行与「顺序执行 + 汇合」是两套模型，混在一起没人能预测
   行为。要并行请拆成两条路径再用汇合收回。`edge.add` 因此**替换**同 `(src, port)`
   的旧边——并在返回值里告诉前端「替换发生了」，否则旧连线无声消失，用户会以为
   自己连了两条。

4. **汇合不需要语法**：两条边指向同一节点就是汇合，到达即执行。
   加一个「汇合节点」类型只会让用户多一层要维护的东西，而语义上不需要它。

5. **循环必须有步数上限**。允许回边就等于允许死循环。没有上限时，一个连错成环的
   图会让界面**永远**停在「运行中」，用户除了强杀进程没有别的办法。
   `MAX_STEPS = 10000`：正常流程图几十步到底，而真死循环在纯计算节点上
   10000 步也就毫秒级，停得下来。撞上限时**主动置停并上报 `run.error`**，
   不能只是「悄悄不跑了」——用户必须知道是环的问题。

6. **`end` 节点要上报一次**。不报的话最后一步会停在 `end` 的**前一个**节点上，
   界面看起来像「没跑完就结束了」。它是一个真实的执行位置，不是一个语法标记。

7. **停用 / 注释节点 = 跳过，不是截断**。它们不执行、也不上报（它们不是「跑过」的
   节点），但路径**必须继续往下走**，所以固定返回 `PORT_OUT`。
   于是 `None` 这个返回值被专门保留给「执行失败」——把「跳过」和「失败」用同一个
   返回值表达，会让停用一个节点顺手把整条路径断掉，且不报任何错。

8. **起始节点三级回退**：显式 `start`（且该节点还在）> 第一个 `start` 类型的节点 >
   `nodes[0]`。第二级让「加了 start 节点」立刻生效而不必再点一次「设为起点」。
   前端 `store.entryUid` **必须与此完全一致**，否则画布上的「起」徽标会标在一个
   后端并不从这里开始跑的节点上，用户照着画布排查会彻底跑偏——所以两边各有一份
   实现，靠用例锁定规则（`test_...` + 前端 `起始节点解析与后端 Executor.entry_uid 同规则`）。

9. **迁移只保证「还能跑」，不试图还原分支**。v3 的 `run_when` 依赖一个全局标志，
   同一条件下可以有任意多节点挂不同的 `run_when`，**无法一对一映射成边**。
   所以 `_linear_edges` 只把列表接成一条链，并把条件节点的 `true`/`false`
   **两个出口都接到下一个节点**：只接一条的话执行到条件节点就走不下去了
   （它没有 `out` 出口，流程图直接断在那儿）。两条都接 = 条件不门控，
   与「`run_when` 失效」的承诺一致，而且画布上能一眼看出「这里该重连」。

10. **迁移还必须摆开坐标**。`x/y` 是 v4 才有的字段，v3 文件里一个坐标都没有，
    全部落在 `(0, 0)`——画布上就是一摞**完全重叠**的卡片，看起来像
    「打开旧文件之后工作流被毁了」，而数据其实完好。
    `_linear_positions` 按原顺序横向排开（间距 240 > 卡片宽度），
    **横向而不是纵向**：手柄在节点左右两侧，横向链的边才是直的，纵向排列会让每条边
    绕成 S 形。只在 `(0, 0)` 的节点上动手，手工补过坐标的人不该被覆盖。

11. **`migrated_from_list` 不落盘**。它是「本次会话的加载事实」，不是工作流属性。
    写进文件的话每次打开都会提示一次，而用户第一次就已经处理过了。
    但它**必须发给前端**（`_public_workflow` 里单独加）——否则用户打开旧文件后
    会发现条件不再门控却完全不知道为什么。前端只在「从否变是」时弹一次提示，
    且用户关掉之后不再复活（否则改个参数就会重新弹出来，等于关不掉）。

12. **出口名是跨语言协议，靠测试而不是靠注释来锁**。前端按它渲染手柄、提交边，
    后端按它选下一跳。任何一边改了字面量而另一边没跟上，表现都是
    「连线看着正常、执行时静默走错路」——**没有任何报错**。
    `tauri/tests/flow-ports.test.mjs` 直接读 `core/events.py` 的正则比对常量，
    而不是在测试里再抄一遍字面量（抄一遍等于三份副本，照样会漂）。

13. **前端只投影，不乐观改**。画布把 store 里的工作流投影成 `nodes`/`edges`，
    任何交互都发 RPC、等响应回来再刷新。乐观改边之后如果后端拒绝
    （自环、节点已删），画布会停在和后端不一致的状态。
    投影时**每次都重建新对象**：Vue Flow 会往收到的节点上挂 `dimensions` /
    `selected` 等字段，直接传 store 里的对象等于让库改真源。

14. **两类边不渲染**（`FlowCanvas.vue` 的 `vEdges`）：
    `dst` 为空（后端允许存「这个出口留空」，它与「边不存在」在执行上等价，
    画一条没有终点的线只会让人以为连上了）；以及出口名对源节点已不合法
    （如 `case_count` 从 5 调回 3）——那个手柄不存在，Vue Flow 找不到锚点，
    会刷控制台告警且画在奇怪的位置。**边本身留着**：把 `case_count` 调回去它还在，
    只是执行器不会走到（分支不会发出那个出口）。这是「不删数据、只不显示」。

15. **拖拽结束才提交坐标**。逐帧上报会把整份工作流广播几十次，
    `workflow.changed` 刷满 1024 格的通知队列，把 `run.finished` 这类状态通知挤掉
    ——丢一条界面就可能永久停在旧状态。

16. **前端的连接约束比后端严一点，但只严在「连不出来」上**：`end` 不渲染源手柄、
    `start` 不渲染目标手柄，所以这两类边在画布上根本连不出来；
    自环则由 `isValidConnection` 拦掉（后端也会拒，但在那里拒是「先连上再被弹回」，
    画布会闪一下）。**没有额外规则**——不在前端拦后端允许的东西，
    否则用户会遇到「后端接受、界面拒绝」的困惑。

## 已知问题

- **调整 `case_count` 不会清理超出范围的旧边**。把分支从 6 个情形调成 3 个，
  `case:4..6` 上的边仍在文件里（画布上不显示、执行器也不会走到）。
  这是刻意的（调回去还在），但用户看不到它们，容易误以为已经删干净。
- **回边没有任何静态校验**。连成环不会在保存时报错，只在运行时撞到 10000 步上限。
  一个「环检测」提示是后续可做的（但静态判定「这个环是死循环」是不可判定的，
  只能提示「有环，确认这是循环而不是连错」）。
- 画布没有框选、复制粘贴、对齐吸附。节点多了以后手工摆位比较累。
- 前端 bundle 因引入 @vue-flow/core（含 d3-zoom/d3-selection）从约 1.0 MB 涨到
  1.63 MB（gzip 443 KB）。桌面应用从本地加载，可接受；若将来要在意启动时间，
  可以对画布做动态 `import()`。
- `node.move`（列表内换序）后端保留但画布不再使用。在 v4 里节点顺序只影响
  「没有 `start` 也没有 `start` 节点」时的兜底入口，所以它成了一个几乎无用的接口。

## 变更记录

- 2026-09-13 初版：v4 流程图模型（数据模型 / 图遍历 / 出口名 / 分支 / 结束 / 迁移 /
  画布），替换 v3 的「有序列表 + `run_when` 门控」
- 2026-09-15 `node.remove` / `rename` / `toggle` / `params.set` 改为 uid 优先
  （修「多选删除会删错节点且不报错」，见设计要点 2）
