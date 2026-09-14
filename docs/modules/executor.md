# 工作流执行器

> 状态：✅
> 一句话：从起始节点沿有向边遍历流程图，承载出口分派、汇合、循环（带步数上限）、整体循环、单节点重复与热停。

## 代码位置

- `core/executor.py` — Executor：`entry_uid` / `next_map` / `_walk`（图遍历）、RunContext、停止传播
- `rpc/controller.py` — run.start/run.stop 的线程管理（_run_thread 引用 + join）
- 模型与出口名的定义在 `core/events.py`，见 [flow.md](flow.md)

## 功能清单

| 编号 | 功能 | 状态 | 说明 |
| --- | --- | --- | --- |
| F-EXE-01 | 按边遍历 | ✅ | 从 `entry_uid` 出发，按节点返回的出口名查 `(uid, port) -> dst` |
| F-EXE-02 | 整体循环 × 单节点重复 | ✅ | 两级 repeat 相乘（整体 repeat 重跑整张图） |
| F-EXE-03 | 出口分派 | ✅ | 条件走 `true`/`false`，分支走 `case:N`/`else`，其余走 `out` |
| F-EXE-04 | 汇合 | ✅ | 多条边指向同一节点即汇合，无特殊语法 |
| F-EXE-05 | 步数上限兜底 | ✅ | `MAX_STEPS=10000`，撞上限置停 + `on_error` 上报 |
| F-EXE-06 | 停用/注释节点跳过 | ✅ | 不执行、不上报，但路径继续（返回 `PORT_OUT`） |
| F-EXE-07 | 缺边即路径结束 | ✅ | `(uid, port)` 没有边 → 该路径正常结束（不是错误） |
| F-EXE-08 | `end` 节点终止 | ✅ | 上报一次后结束这条路径 |
| F-EXE-09 | 节点异常兜底 | ✅ | 异常置停标志 + on_error 上报，不拖垮进程 |
| F-EXE-10 | 热停传播 | ✅ | 停标志 → player.stop_playback → 节点循环检查 |
| F-EXE-11 | 停止即等收尾 | ✅ | run_stop join 运行线程（≤5s），响应含最终状态 |

## 验收记录

- **F-EXE-01**（2026-09-13）：`test_executor_follows_edges_not_list_order`
  （故意把列表顺序与边的指向做成不一致，断言按边走）
- **F-EXE-02**（2026-09-13）：`test_executor_order_and_repeat`
- **F-EXE-03**（2026-09-13）：`test_executor_condition_routes_by_port`、
  `test_executor_condition_false_routes_to_other_branch`、
  `test_executor_branch_routes_to_matching_case`、`test_executor_branch_falls_back_to_else`
- **F-EXE-04**（2026-09-13）：`test_executor_merges_when_two_edges_point_at_one_node`
- **F-EXE-05**（2026-09-13）：`test_executor_runaway_cycle_is_stopped_and_reported`
  （断言至少跑了 `MAX_STEPS - 2` 步——只断言「停了」的话，执行器跑一步就退出也能过）、
  `test_executor_cycle_terminates_when_edge_is_dropped`
- **F-EXE-06**（2026-09-13）：`test_executor_disabled_node_passes_through`
- **F-EXE-07**（2026-09-13）：`test_executor_missing_edge_ends_path`
- **F-EXE-08**（2026-09-13）：`test_executor_end_node_stops_that_path`
- **F-EXE-09**（2026-09-05，v4 后仍适用）：`test_executor_node_exception_stops_gracefully`
- **F-EXE-10**（2026-09-05）：`test_executor_stop_flag_stops_loop`（100 循环 0.1s 内停）
- **F-EXE-11**（2026-09-12）：真机 F10 中途停止，`run.finished stopped:true` 且按钮复位

## 设计要点

1. **执行器不再看节点列表顺序**。v4 起「下一步走哪儿」只由边决定；
   `nodes` 数组的顺序仅用于「没有 `start` 也没有 `start` 节点」时的兜底入口。
   回归测试刻意把列表顺序与边的指向做成不一致，就是为了锁住这一点。

2. **出口名是节点与执行器之间的唯一契约**。执行器不判断节点类型——
   它只问「你走哪个出口」。所以新增一种会分叉的节点**不需要改执行器**，
   只要它的 `run` 在合适的时候调 `ctx.set_port(...)`。
   `_run_node` 在调用任务**之前**把 `self._port` 复位成 `PORT_OUT`：
   不复位的话，上一轮条件节点写下的 `true`/`false` 会泄漏到下一个普通节点上。

3. **`_walk` 里 `on_node` 的位置很关键**。`end` 与「跳过」两条分支都在
   `on_node` **之前** `return` / `continue`：
   曾经把上报放在判断之前，于是停用节点和注释节点也会被上报成「正在运行」，
   界面上看到一个明明停用了的节点在闪。

4. **「跳过」与「失败」用不同返回值区分**。跳过返回 `PORT_OUT`（继续走），
   异常返回 `None`（结束这条路径，且 `_run_node` 已经置停 + 上报）。
   合并成一个返回值的话，停用一个节点会顺手把整条路径断掉，而且不报任何错。

5. **缺边是正常结束，不是错误**。用户把某个出口留空（或者压根没连）时，
   走到那里就该停。报错会把「还没连完」变成一种要处理的故障，
   而画布上本来就一眼能看出哪些出口是空的。

6. **`is_stopping` 动态化**：RunContext.stopping 是属性，读 executor 的停标志。
   曾做成快照 bool，任务内循环永远看不到停止（已修）。

7. **异常即停**：节点抛异常（如 YOLO 模型路径无效）曾让运行按钮永久卡死——
   现在捕获 → on_error 通知 → 置停标志 → on_done(stopped=true)。

8. **run_stop join**：停止是异步的，旧实现立刻返回导致前端按钮状态猜不准；
   现在 join（≤5s）后返回最终 running，前端以此收敛。

## 已知问题

- 运行中修改节点参数会即时生效（真源树同引用）——算特性，但值得知晓。
- **没有校验「节点返回的出口名」是否合法**。执行器只做
  `nxt.get((uid, port), "")`，所以一个返回了拼错出口名的节点（只有自研节点会
  这样，内置节点不会）会**静默地**把这条路径结束掉，不报任何错。
  理论上可以在后端加一份「每类节点有哪些出口」的表来校验，但那会多出一个
  与 `tasks/builtin.py` 平行的真源——目前选择不加，而是让 `end` 与「缺边」
  在画布上都可见（空的出口一眼能看出来）。
- 步数上限是「单次遍历」的上限，**不跨整体 `repeat` 累加**。所以
  `repeat=1000` 且每轮都撞不到上限的图，总步数可以远超 10000——这是刻意的
  （整体 repeat 是用户明确要求的次数），但如果用户把 repeat 设得很大又连了环，
  停止要等得久一些。

## 变更记录

- 2026-09-05 条件门控 + 异常兜底 + is_stopping 动态化
- 2026-09-12 run_stop join 运行线程
- 2026-09-13 **重写为图遍历**：`run_when` 条件门控 → 出口分派（F-EXE-01/03/04/05/06/07/08），
  新增步数上限兜底；「跳过」与「失败」的返回值分离；`on_node` 只在真正执行的节点上触发
