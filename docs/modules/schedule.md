# 定时运行

> 状态：✅
> 一句话：后端计时器到点从磁盘重载工作流并**真正运行**（曾只发通知不运行）。

## 代码位置

- `rpc/controller.py` — schedule_configure / _schedule_arm / _schedule_fire /
  config.json 持久化（app_dir）
- `tauri/src/components/ScheduleDialog.vue` — 配置对话框

## 功能清单

| 编号 | 功能 | 状态 | 说明 |
| --- | --- | --- | --- |
| F-SCH-01 | 每天时刻模式 | ✅ | 错过跨天顺延 |
| F-SCH-02 | 固定间隔模式 | ✅ | 触发后立即装定下一次 |
| F-SCH-03 | 触发即运行 | ✅ | 重载磁盘工作流 → run_start；忙时通知 skipped |
| F-SCH-04 | 配置持久化 | ✅ | ~/Library/Application Support/AutoFlow/config.json |
| F-SCH-05 | 启动恢复 | ✅ | sidecar 启动时按持久化配置重新装定 |

## 验收记录

- **F-SCH-01/02**（2026-09-05）：`test_schedule_configure_and_get`、
  `test_schedule_fire_reloads_workflow`（直接驱动 _schedule_fire，无真实等待）。
- **F-SCH-03**（2026-09-12 修复）：此前 _schedule_fire 只广播 schedule.fired，
  前端也只弹横幅——**定时功能形同虚设**；改为后端直接 run_start（忙时
  schedule.fired 携 ran:false + reason）。

## 设计要点

1. **后端真跑而非前端代跑**：定时触发不依赖前端在线；通知只是给 UI 看的。
2. **计时器粒度**：threading.Timer 一次性 + 触发后重装定（相对 now 计算），
   避免 Interval 定时器漂移累积。
3. **配置存用户数据目录**（`core/paths.app_dir()`），打包后不写程序目录。

## 已知问题

- 系统休眠期间错过的每天时刻不补跑（醒来后顺延到下一次）。
- 无「停止已装定的下一次」独立开关（关闭 enabled 即取消全部）。

## 变更记录

- 2026-09-05 落地（含只通知不运行的缺陷）
- 2026-09-12 修为触发即真跑
