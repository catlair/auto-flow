# 工作流执行器

> 状态：✅
> 一句话：按顺序驱动节点运行，承载整体循环、单节点重复、条件门控与热停。

## 代码位置

- `core/executor.py` — Executor：run_workflow 主循环、RunContext、停止传播
- `rpc/controller.py` — run.start/run.stop 的线程管理（_run_thread 引用 + join）

## 功能清单

| 编号 | 功能 | 状态 | 说明 |
| --- | --- | --- | --- |
| F-EXE-01 | 顺序执行 | ✅ | note 跳过，enabled=false 跳过 |
| F-EXE-02 | 整体循环 × 单节点重复 | ✅ | 两级 repeat 相乘 |
| F-EXE-03 | 条件门控 | ✅ | run_when: 总是/条件成立/条件不成立 |
| F-EXE-04 | 节点异常兜底 | ✅ | 异常置停标志 + on_error 上报，不拖垮进程 |
| F-EXE-05 | 热停传播 | ✅ | 停标志 → player.stop_playback → 节点循环检查 |
| F-EXE-06 | 停止即等收尾 | ✅ | run_stop join 运行线程（≤5s），响应含最终状态 |

## 验收记录

- **F-EXE-01/02**（2026-09-05）：`test_executor_order_and_repeat`
- **F-EXE-03**（2026-09-05）：`test_executor_condition_gating`（mock 视觉命中/未命中）
- **F-EXE-04**（2026-09-05）：`test_executor_node_exception_stops_gracefully`
- **F-EXE-05**（2026-09-05）：`test_executor_stop_flag_stops_loop`（100 循环 0.1s 内停）

## 设计要点

1. **条件门控而非跳转**：顺序列表里不做 goto，条件节点写状态，每个节点用
   「执行条件」决定跑不跑——重排安全，UI 免连线。
2. **is_stopping 动态化**：RunContext.stopping 是属性，读 executor 的停标志。
   曾做成快照 bool，任务内循环永远看不到停止（已修）。
3. **异常即停**：节点抛异常（如 YOLO 模型路径无效）曾让运行按钮永久卡死——
   现在捕获 → on_error 通知 → 置停标志 → on_done(stopped=true)。
4. **run_stop join**：停止是异步的，旧实现立刻返回导致前端按钮状态猜不准；
   现在 join（≤5s）后返回最终 running，前端以此收敛。

## 已知问题

- 运行中修改节点参数会即时生效（真源树同引用）——算特性，但值得知晓。

## 变更记录

- 2026-09-05 条件门控 + 异常兜底 + is_stopping 动态化
- 2026-09-12 run_stop join 运行线程
