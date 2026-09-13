# 前端界面（Tauri + Vue 3 + TDesign）

> 状态：✅
> 一句话：三栏卡片式主界面——工作流节点列表 / 动态参数面板 / 运行与录制控制。

## 代码位置

- `tauri/src/App.vue` — 布局（header + 三栏卡片 + 底部状态栏）
- `tauri/src/components/` — NodeList / ParamsPanel / RunPanel / RecordPanel /
  WorkflowMenu / PermissionBanner / ScheduleDialog / DiagnosticsPanel（8 个）
- `tauri/src/stores/app.ts` — Pinia：连接/运行/录制态、工作流镜像、全部 RPC 调用
- `tauri/src/rpc/client.ts` — NDJSON 客户端（id 配对、通知订阅、半包切分）

## 功能清单

| 编号 | 功能 | 状态 | 说明 |
| --- | --- | --- | --- |
| F-UI-01 | 三栏卡片布局 | ✅ | 工作流 / 参数 / 运行+录制；面板标题统一在 App.vue |
| F-UI-02 | 节点列表 | ✅ | 序号徽标 + 启停开关 + 删除；拖拽排序（uid key） |
| F-UI-03 | 节点改名 | ✅ | 双击行内编辑，Enter/Esc/失焦 |
| F-UI-04 | 动态参数面板 | ✅ | 按 nodes.definitions 渲染 8 种控件 |
| F-UI-05 | 运行状态机 | ✅ | 运行/停止按钮切换 + 运行状态卡（当前节点+进度） |
| F-UI-06 | 录制状态机 | ✅ | 录制中红点提示 + 按钮切换；写入节点/清空 |
| F-UI-07 | 快捷键提示 | ✅ | header 徽标 + 按钮副文案 + RunPanel 底部一行 |
| F-UI-08 | 权限横幅+自检 | ✅ | 快照 ✗ 或自检失败分别引导 |
| F-UI-09 | 事件流 | ✅ | 虚拟滚动、最近 200 条、录制中红色边框 |
| F-UI-10 | 断连诊断 | ✅ | rpc_down 详情 / 诊断面板（sidecar 挂了也能开） |
| F-UI-11 | 底部状态栏 | ✅ | 就绪 / ● 录制中 / ▶ 运行中 + 进度 |
| F-UI-12 | 回放防误触 | ✅ | 运行期间主窗鼠标穿透（set_click_through），结束自动恢复 |
| F-UI-13 | 后端告警可见 | ✅ | `log.warning` 通知 → `backendNotes`（最近 20 条）→ 诊断面板成块显示 + 无横幅时弹 warn 横幅 |

## 验收记录

- **F-UI-05**（2026-09-12 修复）：根因是 `toggleRun` 未把 running 置位（通知也不置）
  ——按钮永不变、F10 再按被拒。现开始前置位、停止用响应收敛、run.node/progress
  通知兜底置位；真机点运行按钮变「■ 停止运行 (F10)」。
- **F-UI-05 验收**（2026-09-12 真机）：录制 2011 条 → 写入节点 → 运行，回放 85 秒
  完整复现用户浏览器操作（点击全部生效，run.finished stopped:false）；再次运行后
  F10 中途停止（run.finished stopped:true），按钮复位。
- **F-UI-02/03**（2026-09-12）：真机写入节点 → 列表出现（带序号）→ 双击改名 →
  node.rename 落库；删除后列表立即刷新（响应驱动）。
- **F-UI-06**（2026-09-12）：真机录制中按钮变「■ 停止录制 (F9)」+ 红点 + 状态栏同步。

## 设计要点

1. **响应驱动刷新**：凡改变后端状态的操作，以响应中的 workflow_current 收敛
   本地镜像；`workflow.changed` 通知只作广播冗余。曾依赖通知，偶发丢失导致
   「删除不刷新」「写入节点列表不动」。
2. **通知处理隔离**：handleNotification 外层 try/catch——单条通知异常不打死
   后续通知。
3. **热键屏蔽**：输入框聚焦时忽略 hotkey.triggered（用户在打字时 F10 不应抢焦点）。
4. **事件流性能**：5000 条截断 + 仅渲染最近 200 条 + 录制事件 100ms 批量推送。
5. **后端告警要能被用户看见**（F-UI-13）：后端（尤其 `core.vision`）的 WARNING 只进
   `~/Library/Logs/autoflow-tauri.log`，那份日志是逐帧 RPC 收发记录、不含诊断字符串，
   用户「为什么没匹配上」只能自己翻日志。现由后端 `log.warning` 通知送来，
   前端存 `backendNotes` 并**同时**做两件事：诊断面板成块显示（可整段复制），
   以及**仅在当前无横幅时**弹一条 warn 横幅——已有 error 横幅时不能覆盖
   （告警是补充信息，不是更高优先级）。
   注意 `log.warning` 是可丢类通知（见 rpc-protocol.md 设计要点 4），
   所以它**只能**作为「锦上添花」的诊断线索，前端不得依赖它收敛任何状态。

## 已知问题

- WebView 输入框无法被系统级合成键盘可靠注入（自动化测试限制，人工输入正常）。
- 断连重连后 2s 内的 UI 状态可能短暂陈旧（依赖后端重连握手重放 workflow.current）。
- workflow.changed 通知经 Tauri 事件转发偶发丢失（未定位帧级原因）——结构操作
  已全部改为响应驱动刷新，通知丢失不再影响 UI。

## 变更记录

- 2026-09-12 三栏卡片重排 + 状态机 + 改名 + 快捷键提示
- 2026-09-12 事件流虚拟滚动、诊断面板（外部 AI 贡献轮）
- 2026-09-13 诊断面板新增「后端告警」块（F-UI-13）：`log.warning` → `backendNotes`
  （上限 20 条、新的在前），并接进 `formatDiagnostics` 报告文本；
  新增 6 个前端用例（store 4 + 报告 2）
