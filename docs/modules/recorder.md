# 键鼠录制

> 状态：✅
> 一句话：全局监听键鼠输入，只消除冗余采样、不删除信息，记录为可回放的语义完整时间线。

## 代码位置

- `core/recorder.py` — Recorder：双监听器生命周期、保轨采样、点击序列归并、尾部裁剪
- `core/maclistener.py` — 自建 CGEventTap 键盘监听（**替代 pynput 键盘监听**）
- `core/mackeys.py` — 虚拟键码表 + 修饰键 flags 边沿检测
- `core/events.py` — `MacroEvent`（**v3**）字段定义

## 功能清单

| 编号 | 功能 | 状态 | 说明 |
| --- | --- | --- | --- |
| F-REC-01 | 鼠标移动/点击/滚轮录制 | ✅ | pynput mouse.Listener（鼠标侧无 TIS 风险） |
| F-REC-02 | 键盘按下/释放录制 | ✅ | 自建只读 CGEventTap（F-REC-05） |
| F-REC-03 | 保轨采样（降冗余） | ✅ | 位移 ≥2px **或** 距上次保留 ≥20ms **或** 拖拽中，三者之一即保留 |
| F-REC-04 | 点击捕获稳定性 | ✅ | pyobjc 符号预热（见设计要点 2） |
| F-REC-05 | macOS 15 兼容 | ✅ | 键盘监听禁用 pynput（TSM 崩溃） |
| F-REC-06 | 热键回声抑制 | ✅ | Recorder(skip_keys)——F9/F10/F11 不入事件 |
| F-REC-07 | 事件上限自停 | ✅ | 10 万条上限，stopped_by_limit 上报 |
| F-REC-08 | 丢帧定位计量 | ✅ | captured / decimated / window_dropped / limit_dropped / 监听器存活 |
| F-REC-09 | 停止交互裁剪 | ✅ | `trim_stop_interaction`：按**停止那一刻**的窗口边界定位停止点击 |
| F-REC-10 | **拖拽语义记录** | ✅ | 自行跟踪按键保持状态，移动事件带 `dragged`（v3） |
| F-REC-11 | **双击/三击归并** | ✅ | 相邻快速同位置按下归并为 `clicks=1/2/3`（v3） |
| F-REC-12 | **滚轮单位标注** | ✅ | `wheel_unit="line"`（pynput 取的是 DeltaAxis 行增量）（v3） |
| F-REC-13 | **轨迹终点补全** | ✅ | 停止时补回最后一个被降采样的位置（v3） |
| F-REC-14 | **按键事件带坐标** | ✅ | key 事件带上 listener 提供的 (x,y) 与 flags（v3） |

## 验收记录

- **F-REC-10/11/12/13/14**（2026-09-12，v3 重写）：测试
  `test_recorder_keeps_drag_trajectory_without_decimation`、
  `test_recorder_merges_double_click_sequence`、
  `test_recorder_next_clicks_resets_when_position_moves`、
  `test_recorder_flushes_last_decimated_position` 通过。
- **F-REC-03/08**（2026-09-12）：`test_recorder_decimates_redundant_moves_but_not_information`
  ——1px 且 1ms 的微移动被降采样，距上次保留 100ms 的移动必留。
- **F-REC-09**（2026-09-12）：`test_trim_stop_interaction_uses_window_bounds` /
  `test_trim_never_destroys_real_work_without_window_hit`。
- **F-REC-02/04**（2026-09-12）：连续 5 轮 venv 录制（移动+点击），每轮均捕获
  `mouse left down/up`；修复前 5 轮中点击全丢（pyobjc 符号竞争）。
- **F-REC-05**（2026-08-31）：崩溃报告确认 pynput 键盘监听 TSMGetInputSourceProperty
  断言崩溃；替换自建 tap 后录制链路无崩溃（真机多日使用）。
- **F-REC-08**（2026-09-05）：真机录制 stats：captured=95 = count 51 + filtered 44，
  无系统层丢失。

## 设计要点

1. **键盘监听禁用 pynput**：macOS 15.5 上 pynput 键盘监听启动/输入源变化时经
   ctypes 调 `TSMGetInputSourceProperty`，撞 HIToolbox 主线程断言
   （`dispatch_assert_queue_fail` → EXC_BREAKPOINT）。自建 `MacKeyboardListener`
   只读 keycode/flags，绝不查 TIS。鼠标监听无此风险，保留 pynput。
2. **pyobjc 懒加载预热**：pyobjc 框架符号表懒加载**非线程安全**。keyboard tap
   线程（maclistener）与 pynput 鼠标 tap 线程并发首访 Quartz 符号时，
   `CGEventGetLocation` 抛 `KeyError` 被 pynput 吞掉——**每条鼠标点击全部丢失**
   （move 分支不访问该符号故幸存，即「录到移动录不到点击」）。修复：导入期
   （单线程）from-import 预热全部后续符号（recorder.py / maclistener.py 顶部）。
3. **拖拽必须自己跟踪按键状态**：pynput 的 `mouse.Listener` 回调签名是
   `on_move(x, y, injected)`——第三个参数是**「事件是否由事件源合成」**，不是
   `dragged`（darwin 后端把 `kCGEventLeftMouseDragged` 也路由到 `on_move`）。
   v2 误把它当 dragged 且未使用，拖拽信息从未入库。v3 在 `_on_click` 维护
   `_held_buttons`：有键按住时到达的移动即 `dragged=True`。鼠标事件全部由
   同一个 tap 线程投递，无需加锁。
4. **只删冗余、不删信息**：v2 的 4px 阈值让慢速/精细操作出现大段空洞，
   且轨迹终点经常丢失。v3 改成"位移/时间/拖拽"三选一即保留，并在停止时
   补回最后一个被降采样的位置。
5. **窗口过滤默认关闭**：v2 按 Auto Flow 窗口矩形丢弃事件，边界只在录制开始时
   下发一次（`outer_*` 还含标题栏，比内容区外扩），前端监听又从不注销——
   边界一旦过期就成片吞掉真实操作，这是「操作被莫名其妙裁掉」的主因。
   v3 只把窗口边界用于**停止时定位停止点击**，且边界在停止那一刻由前端重新下发。
6. **裁剪宁可不做也不做错**：识别不到落在窗口内的鼠标按下时一律不裁——
   宁可让一次停止点击被回放（顶多多点一下），也不要把用户真实的最后一次拖拽
   （同样以按下开始）误判成停止操作而静默删除。
7. **stop 顺序**：先停监听器、后置 `_stopping` 闸、再排空队列——顺序反了会把
   最后一批在途事件误判为丢弃（录制尾部丢帧）。
8. **监听器存活在 stop 之前采样**：pynput 的 `stop()` 不 join、自建 tap 的
   `CFRunLoopStop` 是异步的，stop 之后再读 `is_alive()` 是竞态结果，
   会把健康监听误报成"中途死亡"。
9. **修饰键 flags 用规范位**：Shift=0x20000 / Control=0x40000 / Option=0x80000 /
   Command=0x100000 / CapsLock=0x10000。低 0x1F 是设备相关左右键位。
   （曾错用低字节导致 Shift 录不到）
10. **滚轮取到的是"行"**：pynput darwin 后端读 `kCGScrollWheelEventDeltaAxis1/2`，
    是行增量（触控板同样走这两字段），不是像素。事件因此标注 `wheel_unit="line"`。
11. **键盘事件自带坐标**：`CGEventGetLocation(event)` 可用于取点；但 CLI 进程
    `CGEventGetLocation(CGEventCreate(None))` 恒 (0,0)，不能用来读全局光标。

## 已知问题

- 合成键盘事件（脚本注入）的 location 为 (0,0)，靠鼠标位置兜底（见 hotkeys.md）。
- CapsLock 录制为 flags 开关沿（按住不产生额外事件），与硬件行为一致但
  事件对可能不成对。
- 双击归并按"同位置 + 时间间隔"推断，与系统 ClickState 可能存在极端边界差异
  （如系统双击间隔被用户改过）。

## 变更记录

- 2026-08-31 键盘监听弃用 pynput，改自建 CGEventTap
- 2026-09-05 丢帧计量 + 热键 skip_keys + stop 顺序修正
- 2026-09-06 pyobjc 符号预热，修复点击全部丢失
- 2026-09-12 **v3 重写**：事件模型升级（dragged/clicks/flags/wheel_unit/全事件坐标）、
  保轨采样取代固定阈值、窗口过滤改为显式开关 + 停止时定位裁剪、轨迹终点补全、
  监听器存活判定去竞态
