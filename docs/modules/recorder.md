# 键鼠录制

> 状态：✅
> 一句话：全局监听键鼠输入，按时间戳记录为可回放的事件序列。

## 代码位置

- `core/recorder.py` — Recorder：双监听器生命周期、事件队列、阈值过滤、统计
- `core/maclistener.py` — 自建 CGEventTap 键盘监听（**替代 pynput 键盘监听**）
- `core/mackeys.py` — 虚拟键码表 + 修饰键 flags 边沿检测

## 功能清单

| 编号 | 功能 | 状态 | 说明 |
| --- | --- | --- | --- |
| F-REC-01 | 鼠标移动/点击/滚轮录制 | ✅ | pynput mouse.Listener（鼠标侧无 TIS 风险） |
| F-REC-02 | 键盘按下/释放录制 | ✅ | 自建只读 CGEventTap（F-REC-05） |
| F-REC-03 | 移动阈值过滤 | ✅ | 4px 内微移动丢弃，防文件爆炸 |
| F-REC-04 | 点击捕获稳定性 | ✅ | pyobjc 符号预热（见设计要点 2） |
| F-REC-05 | macOS 15 兼容 | ✅ | 键盘监听禁用 pynput（TSM 崩溃） |
| F-REC-06 | 热键回声抑制 | ✅ | Recorder(skip_keys)——F9/F10/F11 不入事件 |
| F-REC-07 | 事件上限自停 | ✅ | 10 万条上限，stopped_by_limit 上报 |
| F-REC-08 | 丢帧定位计量 | ✅ | captured/filtered/limit_dropped/监听器存活 |

## 验收记录

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
3. **stop 顺序**：先停监听器、后置 `_stopping` 闸、再排空队列——顺序反了会把
   最后一批在途事件误判为丢弃（录制尾部丢帧）。
4. **修饰键 flags 用规范位**：Shift=0x20000 / Control=0x40000 / Option=0x80000 /
   Command=0x100000 / CapsLock=0x10000。低 0x1F 是设备相关左右键位。
   （曾错用低字节导致 Shift 录不到）
5. **键盘事件自带坐标**：`CGEventGetLocation(event)` 可用于取点；但 CLI 进程
   `CGEventGetLocation(CGEventCreate(None))` 恒 (0,0)，不能用来读全局光标。

## 已知问题

- 合成键盘事件（脚本注入）的 location 为 (0,0)，靠鼠标位置兜底（见 hotkeys.md）。
- CapsLock 录制为 flags 开关沿（按住不产生额外事件），与硬件行为一致但
  事件对可能不成对。

## 变更记录

- 2026-08-31 键盘监听弃用 pynput，改自建 CGEventTap
- 2026-09-05 丢帧计量 + 热键 skip_keys + stop 顺序修正
- 2026-09-06 pyobjc 符号预热，修复点击全部丢失
