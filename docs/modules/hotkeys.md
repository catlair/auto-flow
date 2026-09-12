# 全局热键 / 按键捕获 / 取点

> 状态：✅
> 一句话：F9/F10/F11 全局热键、参数面板的按键捕获、基点取点，
> 全部走自建 CGEventTap 键盘监听（禁用 pynput，见 recorder.md 设计要点）。

## 代码位置

- `core/maclistener.py` — MacKeyboardListener（tap 线程 + 事件坐标透传）
- `rpc/controller.py` — _on_key 分发（hotkey/capture/pick_once 三态）、
  hotkey_set / key_capture_start / base_pick / input_probe
- `tauri/src/stores/app.ts` — hotkey.triggered 处理（输入框聚焦屏蔽）

## 功能清单

| 编号 | 功能 | 状态 | 说明 |
| --- | --- | --- | --- |
| F-HOT-01 | 默认热键装定 | ✅ | 前端 init 时 hotkey.set(["record","run","pick"]) |
| F-HOT-02 | 热键触发通知 | ✅ | hotkey.triggered {action, pick 时带 x,y} |
| F-HOT-03 | key-repeat 防抖 | ✅ | 同名键 400ms 内只触发一次 |
| F-HOT-04 | 输入框聚焦屏蔽 | ✅ | 前端 isInputFocused() 时忽略热键 |
| F-HOT-05 | 按键捕获 | ✅ | key.capture → key.captured 回填（暂挂热键分发） |
| F-HOT-06 | F11 取点 | ✅ | 按键事件自带坐标 + 鼠标位置兜底 |
| F-HOT-07 | 输入监控自检 | ✅ | input.probe：F18 按键回环 + 零宽空格文本回环，两段结论分开上报 |

## 验收记录

- **F-HOT-02**（2026-09-05）：真机 F11 → `hotkey.triggered {action:"pick",x,y}` →
  前端 `base.pick` 回填；日志全程可见。
- **F-HOT-06**（2026-09-12）：鼠标置 (555,444) → 合成 F11 → 回填 (555,444)
  （合成键盘事件 location=(0,0)，鼠标位置兜底生效）。
- **F-HOT-07**（2026-09-12）：venv `input_probe()` 返回 alive=true（F18 回环命中）。
  同日补**文本段**：`test_input_probe_refuses_while_busy`、
  `test_probe_text_callback_requires_marker`、
  `test_input_probe_keeps_key_listener_wired_to_text`。

## 设计要点

1. **三态单监听**：idle / hotkey / capture 共用一个 MacKeyboardListener，
   捕获期间暂挂热键分发——避免捕获键误触发热键。
2. **取点 = 事件坐标，不是读光标**：CLI sidecar 里
   `CGEventGetLocation(CGEventCreate(None))` 恒 (0,0)（venv 实测同样如此，
   与 frozen 无关）；pynput MouseController().position 在 frozen 里也 (0,0)。
   键盘事件自带 location 是唯一可靠来源；合成键盘事件 location 缺失时用
   监听器内跟踪的最近鼠标事件位置兜底（tap mask 含 mouseMoved）。
3. **F18 回环自检**：tap 创建成功 ≠ 事件可达（TCC 未授权时创建成功但静默不投递）。
   预检 API 不可靠。post F18（无应用绑定）→ tap 收到 → 输入监控真实可用。
   这用于区分「权限快照全 true 但热键/录制无效」的状态。
4. **自检的第二段（文本回环）**：只有 F18 一段时，"链路通"无法回答中文能不能录。
   文本路径（tap 读 `CGEventKeyboardGetUnicodeString` → `is_text_commit` 判定 →
   派发）与按键路径是两条代码路径，本地造事件读得出来**不代表** tap 收到的也读得出来。
   故再 post 一次零宽空格：**非 ASCII**（会被判为文本提交，正是要验证的分支）
   却在任何应用里都不显示内容。两段结论必须分开报：
   `alive=false` → 先修权限；`alive=true, text_alive=false` → 本实现覆盖不了文本；
   两者皆通而中文仍录成拼音字母 → 该输入法走 `insertText:`，不经事件层。
   命中标记必须校验探针载荷，否则自检期间用户敲的中文会把它点亮成假阳性。
5. **自检期间拒绝录制/运行**：自检会投递真实输入，录制中会污染事件序列，
   运行中会把字符打进正在回放的流程。
6. **热键装载时机**：前端 init 显式 hotkey.set——曾有版本从未装定，
   热键静默无效。

## 已知问题

- F11 在部分 macOS 上默认绑定「显示桌面」——用户可在系统设置改掉，
  或后续版本提供热键自定义 UI（hotkey.set 协议已支持）。
- 连按 F11 过快时鼠标位置兜底可能取到旧值（间隔 <300ms）。

## 变更记录

- 2026-08-31 随 maclistener 落地
- 2026-09-12 key-repeat 防抖、事件坐标取点、F18 自检
