# 回放引擎

> 状态：✅
> 一句话：以虚拟时钟驱动事件时间线，按剩余时间采样插值，落后时跳帧追赶而不塌缩；
> 支持速度倍率、相对坐标与随时热停。

## 代码位置

- `core/player.py` — Player：play() 主循环、`_travel` 时间采样调度、QuartzMouse 输出层
- `core/events.py` — `MacroEvent`（v3）中的 `dragged` / `clicks` / `wheel_unit`

## 功能清单

| 编号 | 功能 | 状态 | 说明 |
| --- | --- | --- | --- |
| F-PLY-01 | 虚拟时钟回放 | ✅ | `due = t0 + ts_ms/speed`，误差不累积 |
| F-PLY-02 | 时间采样插值 | ✅ | 按 ~6ms 一个采样点铺开，点数由**时间**决定而非像素 |
| F-PLY-03 | 速度倍率 | ✅ | 全局 × 节点级相乘 |
| F-PLY-04 | 相对坐标 | ✅ | 基点未设(0,0)时回退原位回放，不飞左上角 |
| F-PLY-05 | 点击时序 | ✅ | 已到位也等计划时间点再 press/release |
| F-PLY-06 | 热停安全 | ✅ | 中断时 finally 补 release 鼠标键/按键 |
| F-PLY-07 | 热键抑制 | ✅ | suppress_keys 跳过 F9/F10/F11 回声 |
| F-PLY-08 | 纯键盘脚本 | ✅ | 无鼠标事件时不挪动光标 |
| F-PLY-09 | **三档追赶** | ✅ | 剩余 >4ms 正常插值；≤4ms 单次投递；≤0 跳帧并计数（v3） |
| F-PLY-10 | **拖拽事件类型** | ✅ | 持键期间的移动投 `kCGEvent*MouseDragged`（v3） |
| F-PLY-11 | **双击/三击** | ✅ | 按 `clicks` 写入 `kCGMouseEventClickState`（v3） |
| F-PLY-12 | **滚轮单位与双轴** | ✅ | 按 `wheel_unit` 选 line/pixel，横纵两轴一起创建（v3） |
| F-PLY-13 | **长空档不平滑爬行** | ✅ | 单段滑行封顶 0.25s，长等待先静止再走（v3） |

## 验收记录

- **F-PLY-09**（2026-09-12）：`test_player_catches_up_by_skipping_instead_of_bursting`
  ——61 个"计划时刻已过去"的事件全部按各自位置投递，落点精确（1770），
  `skipped > 0` 可观测；不存在"剩余事件瞬时连发"。
- **F-PLY-02/13**（2026-09-12）：`test_player_slow_move_is_sampled_over_time`
  ——12px/200ms 的慢速移动铺出多个采样点，不因固定像素步长而跳变。
- **F-PLY-10**（2026-09-12）：`test_player_sends_dragged_event_type`。
- **F-PLY-11**（2026-09-12）：`test_player_passes_click_sequence_state`。
- **F-PLY-12**（2026-09-12）：`test_player_wheel_uses_recorded_unit`。
- **F-PLY-04**（2026-09-05）：`test_player_relative_base_unset_falls_back_to_origin`；
  真机：基点 (900,700) + 原点 (650,420) → 光标精确 (1000,780)。
- **F-PLY-06**（2026-09-05）：`test_player_releases_keys_on_stop`。

## 设计要点

1. **v2 的三个结构性缺陷**（"回放丢帧严重"的直接成因）：
   - `MIN_STEP_S = 8ms` 是**每步硬地板**，播放器上限约 125 事件/秒，而快速拖拽的
     录制事件率远超此数 → 结构性落后，且落后量只增不减；
   - 预算 `ts/1000/speed - 已耗时` 转负后 `max(budget, 0)` 让剩余事件**全部瞬时
     连发**，整条时间线塌成一坨；
   - 插值按 10px 步长、上限 120 步，快速长距离移动被压成十几个上百像素的跳变。
2. **v3 调度**：`_travel(pos, target, due, button)` 是唯一的时间消费点。
   - `dist < 1`：原地事件，只 `_sleep_until(due)`（保证 press/release 落在计划
     时间点，F-PLY-05）；
   - `剩余 ≤ 4ms`：**跳帧**——一次投递到目标，返回 `jumped=1`，**绝不追加延迟**。
     这就是"落后"的兜底：任何情况下单事件开销上限是一次投递，落后自然被追平；
   - 否则：`travel = min(剩余, 0.25s)`，先等到 `due - travel`，再按
     `steps = travel / 6ms` 铺采样点，位置线性插值。
3. **为什么按时间采样而不是按像素步长**：像素步长在快速移动时点数不足（跳变），
   在慢速移动时又无法保证平滑；按时间采样天然与"每秒投递多少个点"对齐，
   视觉平滑度与事件率解耦。
4. **输出层 `QuartzMouse`**：v2 用 pynput Controller，但在 PyInstaller frozen
   sidecar 里 `press/release` 会**静默失效**（不抛异常、事件不出现），表现为
   "回放移动正常但点击无效果"，故改为 Quartz 直发。相对 pynput Controller
   额外补上它内部才有的两件事：持键期间切换 Dragged 事件类型、写 ClickState。
5. **投递到 HID 层**（`kCGHIDEventTap`）：比 session 层更接近真实硬件，兼容性更好。
6. **滚轮必须两轴一起创建**：`CGEventCreateScrollWheelEvent(..., 2, dy, dx)`。
   v2 只创建 1 个轴再回填 `kCGScrollWheelEventDeltaAxis2` 字段，横向滚轮被丢弃；
   且把"行"增量按 pixel 单位投递，量级差一个数量级，表现为滚轮几乎不动。
7. **热路径不写日志**：v2 每次点击都 `logger.info` 落盘，高频点击/拖拽时是
   实打实的 I/O 拖累。

## 已知问题

- 播放中若用户手动抢鼠标，轨迹会跟上真实光标重新出发（未做独占）。
- `skipped` 只上报计数，尚未在 UI 上展示（回放结束可读 `player.last_skipped`）。
- 多显示器且各屏缩放因子不同（混合 Retina/非 Retina）时，绝对坐标场景尚未做
  逐屏换算；相对坐标模式不受影响。

## 变更记录

- 2026-09-05 中断卡键保护 + 基点回退 + 纯键盘不动光标
- 2026-09-12 点击时序修复
- 2026-09-12 **v3 重写**：虚拟时钟 + 时间采样插值 + 三档追赶；拖拽事件类型、
  点击序列号、滚轮单位与双轴、HID 层投递、热路径去日志
