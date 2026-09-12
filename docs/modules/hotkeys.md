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
| F-HOT-07 | 输入监控自检 | ✅ | input.probe：post F18 回环，300ms 内收到即链路通 |

## 验收记录

- **F-HOT-02**（2026-09-05）：真机 F11 → `hotkey.triggered {action:"pick",x,y}` →
  前端 `base.pick` 回填；日志全程可见。
- **F-HOT-06**（2026-09-12）：鼠标置 (555,444) → 合成 F11 → 回填 (555,444)
  （合成键盘事件 location=(0,0)，鼠标位置兜底生效）。
- **F-HOT-07**（2026-09-12）：venv `input_probe()` 返回 alive=true（F18 回环命中）。

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
4. **热键装载时机**：前端 init 显式 hotkey.set——曾有版本从未装定，
   热键静默无效。

## 已知问题

- F11 在部分 macOS 上默认绑定「显示桌面」——用户可在系统设置改掉，
  或后续版本提供热键自定义 UI（hotkey.set 协议已支持）。
- 连按 F11 过快时鼠标位置兜底可能取到旧值（间隔 <300ms）。

## 变更记录

- 2026-08-31 随 maclistener 落地
- 2026-09-12 key-repeat 防抖、事件坐标取点、F18 自检
