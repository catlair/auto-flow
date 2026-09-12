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
| F-REC-09 | 停止点击裁剪 | ✅ | `trim_stop_interaction`：**仅按钮停止**，只丢最后一次窗口内按下及其之后 |
| F-REC-10 | **拖拽语义记录** | ✅ | 自行跟踪按键保持状态，移动事件带 `dragged`（v3） |
| F-REC-11 | **双击/三击归并** | ✅ | 相邻快速同位置按下归并为 `clicks=1/2/3`（v3） |
| F-REC-12 | **滚轮单位标注** | ✅ | `wheel_unit="line"`（pynput 取的是 DeltaAxis 行增量）（v3） |
| F-REC-13 | **轨迹终点补全** | ✅ | 停止时补回最后一个被降采样的位置（v3） |
| F-REC-14 | **按键事件带坐标** | ✅ | key 事件带上 listener 提供的 (x,y) 与 flags（v3） |
| F-REC-15 | **事件编辑** | ✅ | 面板选中行即可删除 / 删此前的移动 / 设为原点 / 撤销（v3） |
| F-REC-16 | **文本事件聚合** | ✅ | 输入法/Unicode 提交聚合为 `kind="text"`，间隔 ≤500ms 视为同一段输入（v3） |

## 验收记录

- **F-REC-16**（2026-09-12）：判据 `test_is_text_commit_classification`
  （中文/emoji/多字符 → 文本；`keycode 0` 的可打印 ASCII、Return/Tab/Escape、
  方向键私有区、普通键的布局字符 → 不是文本）；派发与合成配对抑制
  `test_maclistener_dispatches_text_and_suppresses_synthetic_pair`、
  `test_maclistener_never_swallows_hardware_keyup`；聚合与保序
  `test_recorder_merges_consecutive_text_commits`、
  `test_recorder_starts_new_text_event_after_pause`、
  `test_recorder_flushes_text_before_other_events_keeping_order`、
  `test_recorder_flushes_text_when_input_pauses`、
  `test_recorder_text_merge_keeps_captured_invariant`。
  ⚠️ **待真机确认**：系统拼音输入法是否也走事件层（见计划书 §15.8）。
- **F-REC-15**（2026-09-12）：`test_record_remove_by_index_and_array`、
  `test_record_remove_ignores_out_of_range_indexes`、
  `test_record_remove_moves_before_keeps_real_actions`、
  `test_record_set_origin_follows_selected_event`、
  `test_record_undo_restores_previous_state`、
  `test_record_edit_without_recording_raises`、
  `test_record_to_node_uses_edited_events`。
- **F-REC-10/11/12/13/14**（2026-09-12，v3 重写）：测试
  `test_recorder_keeps_drag_trajectory_without_decimation`、
  `test_recorder_merges_double_click_sequence`、
  `test_recorder_next_clicks_resets_when_position_moves`、
  `test_recorder_flushes_last_decimated_position` 通过。
- **F-REC-03/08**（2026-09-12）：`test_recorder_decimates_redundant_moves_but_not_information`
  ——1px 且 1ms 的微移动被降采样，距上次保留 100ms 的移动必留。
- **F-REC-09**（2026-09-12）：`test_trim_stop_interaction_drops_only_the_stop_click` /
  `test_trim_keeps_the_whole_recording_when_it_is_pure_movement` /
  `test_trim_refuses_click_that_is_not_recent` /
  `test_trim_never_destroys_real_work_without_window_hit` /
  `test_record_stop_without_trim_keeps_everything`（快捷键停止一条不丢）。
- **F-REC-02/04**（2026-09-12）：连续 5 轮 venv 录制（移动+点击），每轮均捕获
  `mouse left down/up`；修复前 5 轮中点击全丢（pyobjc 符号竞争）。
- **F-REC-05**（2026-08-31）：崩溃报告确认 pynput 键盘监听 TSMGetInputSourceProperty
  断言崩溃；替换自建 tap 后录制链路无崩溃（真机多日使用）。
- **F-REC-08**（2026-09-05）：真机录制 stats：captured=95 = count 51 + filtered 44，
  无系统层丢失。
- **F-REC-16 口径变更**：`captured − filtered − limit_dropped − text_merged == count`。
  聚合会让 `count` 小于 `captured`，前端一致性校验必须扣掉 `text_merged`，
  否则每录一次中文都会误报「系统层丢事件」。

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
6. **裁剪只丢"停止点击"本身**：按钮停止时，序列尾部的
   `[..., 用户内容, 移动, 按下(停止按钮), 抬起, 零碎移动]` 只把**按下及其之后**
   去掉，**不碰它之前的任何事件**。
   - 曾在这里额外"弹出尾部连续移动"（理由是"移向停止按钮的路径"）。但录制内容
     本身经常就是一段鼠标移动；那段"路径"与用户真正想录的轨迹在数据上无法区分，
     于是整段录制被裁空（2026-09-12 事故：`captured=470 / decimated=151 /
     trimmed=319 / count=0`，一次 7.8 秒的纯移动录制归零）。**移动轨迹是用户的
     数据，不是噪声**；要删由用户在面板上显式删（`record.remove`）。
   - 另一条护栏：**停止点击必须刚刚发生**（`TRIM_STOP_CLICK_MS=1500`）。最后那个
     窗口内按下离录制结束太久 ⇒ 它是用户的真实操作，不是停止点击 ⇒ 一律不裁。
   - **只有按钮停止才裁**：F9 已在 `skip_keys` 里、根本不进序列，没有停止动作
     可裁。`toggleRecord` 因此要求调用方显式给出 `by: "button" | "hotkey"`——
     曾经两个入口共用默认 `trim: true`，导致快捷键停止也去裁（见事故）。
   - 裁剪是破坏性的且发生在用户看到统计之前，故**押一份裁剪前快照进撤销栈**，
     `record.undo` 可整段还原。
   - 识别不到（边界缺失 / 边界内无按下）时一律不裁：宁可让一次停止点击被回放
     （顶多多点一下），也不做静默的数据损坏。
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
11. **文本提交不能用 keycode 判定**：输入法上屏与 `CGEventKeyboardSetUnicodeString`
    投递的文本，在事件层都是"一次带 Unicode 的按键"，keycode 通常是 0。但
    **keycode 0 就是物理 `A` 键**，且没挂字符串的事件读出来是按 keycode 换算的
    布局字符（实测 0 → `'a'`、0x24 → `'\r'`）而**不是**空串——所以既不能
    "keycode==0 即文本"，也不能"文本为空即非文本"。判据只能是**内容在物理上
    是不是单键能产生的**（`core.mackeys.is_text_commit`），且刻意设计成
    **每一条误判都对应等价的回放方式**，不存在"判错就丢信息"的分支。
12. **合成配对 keyUp 要抑制**：文本提交后紧跟一个 keycode=0 的合成 keyUp。
    它会被读成 `'a'`，不抑制就会在事件流里凭空多出一条"A 键释放"。用
    `kCGEventSourceUnixProcessID`（硬件为 0、合成方为投递进程 PID）区分；
    该字段**只用于抑制**，不参与文本判定——万一某系统上硬件事件也带 PID，
    最坏也只是少抑制一个无害的 keyUp，不会把正常按键误判成文本。
13. **聚合窗口 500ms，时间戳取首段**：输入法一次上屏常只提交一个字/词，
    逐条记录会让事件流退化成"每字一行"。同一段连续输入是一个用户意图，
    回放时一次投递（与「键盘输入」节点同语义）。时间戳取**首段**时刻，
    回放才从正确的时刻开始；停顿超过窗口另起一条，保留打字节奏。
14. **`poll()` 负责把停顿的文本落盘**：没有这一步，最后一段输入要等到
    "下一个别的事件"或"停止录制"才出现，录制面板看着像卡住了。
15. **键盘事件自带坐标**：`CGEventGetLocation(event)` 可用于取点；但 CLI 进程
    `CGEventGetLocation(CGEventCreate(None))` 恒 (0,0)，不能用来读全局光标。
16. **事件编辑改的是权威序列本身**（`rpc/controller.py` 的 `record.remove` /
    `removeMovesBefore` / `setOrigin` / `undo`）：面板删掉一条，写入节点的就是
    删后的序列，不存在"面板一份、实际写入另一份"的空间。编辑前存快照进撤销栈
    （上限 20），因为编辑是破坏性的。越界下标**忽略而非报错**——UI 可能因并发
    刷新拿到过期下标，为此中断用户操作不值得。
17. **新增"有意移除"类别时必须同步丢帧判据**：`captured` 与入库 `count` 之间
    隔着 decimate / 窗口过滤 / 上限 / 文本聚合 / 尾部裁剪五类有意丢弃。判据
    `unaccounted`（见 rpc-protocol.md 设计要点 4）在后端按这五类求和，
    **在前端重拼会漏扣**——`trimmed` 与 `text_merged` 各制造过一次假警报。
    加第六类丢弃时，改 `rpc/controller.py` 的 `record_stop` 一处即可。

## 已知问题

- **中文录入：系统拼音录不到（已实测定性，2026-09-12）**。macOS 输入法上屏有两条
  通道，本实现只覆盖到 ① ：

  | 通道 | 机制 | 本实现 |
  | --- | --- | --- |
  | ① 事件通道 | 输入法用 `CGEventKeyboardSetUnicodeString` 投递（脚本投递、
      少数第三方输入法） | ✅ 覆盖 |
  | ② `insertText:` | IMK 把文本直接交给聚焦应用，**不投递任何 CGEvent** | ❌ 覆盖不到 |

  实测方法与结果（`/tmp/ime_probe_raw.py` 思路）：自建 NSWindow + NSTextField
  （焦点在自己窗口，不影响用户应用），给生产代码用的 `_unicode_of` 打桩记录
  每条 keyDown 的原始读数，然后投递 `nihao` + 空格：

  ```
  输入框内容: '你好'                     ← 阳性对照：输入法确实上屏了
  keycode 45/34/4/0/31/49 → 'n','i','h','a','o',' '   ← 事件通道只看到拼音字母
  含 CJK 的事件数: 0；含 Unicode 的上屏事件: 0
  ```

  **结论：`com.apple.inputmethod.SCIM.ITABC`（系统简体拼音）走通道 ②，
  事件通道原理上覆盖不到。** 回放中文正常（`type_text` 走 Unicode 通道），
  所以是"录不到、放得出"。

  务实做法：中文用「**键盘输入**」节点（`mode="text"`）直接填，别靠录制。
  若将来要做录制侧中文，唯一可行方向是 AX 轮询 `kAXValueAttribute` 差分，
  但它对终端/画布类应用不适用，且无法知道插入位置——需单独评估。
- **自检 `text_alive=true` 不等于"中文能录"**：自检投递的是**我们自己合成的**
  Unicode 事件，与输入法走哪条通道无关，所以系统拼音下它也恒为绿。
  `input.probe` 因此一并返回 `input_source`（含
  `event_channel_unsupported`），UI 按实际输入法给结论。
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
- 2026-09-12 **事件编辑**：面板选中行 → 删除 / 删此前的移动 / 设为原点 / 撤销；
  编辑直接作用于权威序列，写入节点即取编辑结果
- 2026-09-12 **裁剪事故修复**：停止裁剪从"截断+无上界弹尾部移动"改为
  "只丢最后一次窗口内按下及其之后"，加"停止点击必须刚刚发生"护栏；
  快捷键停止不再裁剪（`toggleRecord({by})` 强制调用方表态）；裁剪可撤销
- 2026-09-12 **输入法通道定性**：实测定论系统拼音走 `insertText:`（通道②），
  事件通道覆盖不到中文。自检补 `input_source`，避免"自检通过 ⇒ 中文没问题"
  的错误推论；中文改用「键盘输入」节点（见已知问题）
