# Auto Flow 迁移计划书：PySide6 单体 → Python 后端 + Tauri 前端

> 版本 v1.1 · 2026-08-31 初稿 / 2026-09-01 评审补充 · 状态：待评审
> 现状仓库：本仓库（PySide6 单体，功能已全部可用）
>
> **v1.1 变更**：对照现有代码逐项复核后，补齐协议缺口、修正架构矛盾（工作流真源归属）、
> 新增第 9~17 章（stdio 洁净性、第三项权限、sidecar 打包现实、已知缺陷、持久化分工、
> 测试与验收、工程策略）。**实施阶段以第 18 章的修订排期为准**。

---

## 1. 为什么迁移

| 维度 | 现状（PySide6 单体） | 迁移后（Python 后端 + Tauri 前端） |
| --- | --- | --- |
| 安装包体积 | 135 MB（Qt 全家桶） | 预计 15~25 MB（前端静态文件 + 精简 Python sidecar） |
| UI 技术栈 | PySide6（手写 QSS，控件粗糙） | Vue 3 + TDesign（团队熟悉栈，组件质量高，改样式快） |
| 打包/签名 | PyInstaller 黑盒，签名坑多 | Tauri 标准打包 + Python sidecar，签名流程可控 |
| 跨平台 | macOS 优先，Windows 需重做 UI | 前端天然跨平台，后端按平台抽象（Windows 后续版本） |
| 已踩过的坑可复用 | — | CGEventTap 绕崩溃、MacDev 签名稳授权、/Applications 固定路径 |

**迁移范围**：只换 UI 层和进程形态；**录制/回放/视觉/OCR/YOLO/执行器等核心逻辑 100% 复用**现有 Python 代码。

---

## 2. 目标架构

```
┌─────────────────────────────────────────────┐
│ Tauri 2 Shell (Rust)                        │
│  ├─ WebView：Vue 3 + Vite + TDesign 前端    │
│  ├─ sidecar 管理：拉起/守护 Python 后端      │
│  └─ 原生能力：文件对话框、通知、开机自启      │
└───────────────┬─────────────────────────────┘
                │ stdio（JSON-RPC 2.0，换行分隔）
┌───────────────▼─────────────────────────────┐
│ Python 后端 sidecar（PyInstaller onedir）    │
│  ├─ rpc/server.py        协议层（本次新增）  │
│  ├─ core/                全部复用            │
│  │   events recorder player executor        │
│  │   maclistener mackeys keymap permissions │
│  │   vision ocr yolo paths                  │
│  ├─ tasks/               节点插件全部复用     │
│  └─ ui/                  废弃（迁移期保留）  │
└─────────────────────────────────────────────┘
```

### 2.1 通信选型：stdio JSON-RPC（推荐）vs 本地 WebSocket

| | stdio JSON-RPC ✅ | WebSocket 127.0.0.1 |
| --- | --- | --- |
| 生命周期 | 随 Tauri 进程，无需管端口 | 需管端口冲突/占用 |
| 防火墙 | 无提示 | 可能弹本地网络权限 |
| 双向推送 | stdout 通知行，天然支持 | 同样支持 |
| 实现复杂度 | 低（tauri-plugin-shell pipe） | 中（起服务、心跳、token） |

**结论：采用 stdio JSON-RPC**。录制事件高频推送用「100ms 批量刷新」节流。

### 2.2 关键决策

- **后端是有状态单例**：运行时状态（录制中/运行中/条件值）只存在后端，前端是纯视图，通过通知同步。
- **权限主体是 sidecar 二进制**：调用 CGEventTap/CGEventPost 的是 Python 可执行文件，辅助功能+输入监控授权挂在它身上 → **必须 MacDev 签名 + 固定安装路径**（macro-recorder 已验证的组合，授权跨版本保持）。
- **Tauri 侧不碰输入模拟**：避免 Rust 侧再实现一遍 CGEvent 逻辑；热键也由 Python 后端监听后以通知推给前端（与现有 `maclistener` 一致）。
- **文件对话框走 Tauri**（plugin-dialog），文件读写走后端（保持工作流目录/模板目录逻辑在 `core/paths.py`）。

---

## 3. 协议设计（JSON-RPC 2.0 over stdio）

### 3.1 请求/响应（前端 → 后端）

| 方法 | 参数 | 返回 |
| --- | --- | --- |
| `app.info` | — | 版本、权限状态、平台 |
| `app.openPermissionSettings` | `{"panel": "accessibility"\|"input_monitoring"}` | — |
| `nodes.definitions` | — | 全部节点定义（含参数 schema，驱动前端动态表单） |
| `workflow.load` | `{"path"}` | Workflow JSON |
| `workflow.save` | `{"path", "workflow"}` | — |
| `workflow.pickFile` | `{"mode": "open"\|"save"}` | path（经 Tauri 对话框） |
| `run.start` | `{"workflow", "baseX", "baseY"}` | — |
| `run.stop` | — | — |
| `record.start` | — | — |
| `record.stop` | — | RecordResult（事件+原点） |
| `record.toNode` | `{"targetNodeId"?}` | 更新后的节点（等价 UI 两个按钮） |
| `base.pick` | — | `{x, y}`（F11 取点，读全局光标位置） |
| `hotkey.set` | `{"actions":["record","run","pick"]}` | 三键绑定快照 `[record,run,pick]`（元素可 null 解绑；也接受 `{record,run,pick}` 对象） |
| `workflow.current` | — | 后端当前工作流树（重连/重启后恢复用） |
| `workflow.update` | `{"workflow"}` | —（结构变更后整棵提交，后端为唯一真源） |
| `node.add` | `{"type","index"?}` | 新节点（已填 defaults） |
| `node.remove` / `node.move` / `node.toggle` / `node.params.set` | `{"index", …}` | 更新后的工作流 |
| `template.capture` | `{"paramKey"?}` | —（异步，调 `screencapture -i`） |
| `key.capture` | — | —（异步，按键捕获回填参数；捕获期间暂挂热键分发） |
| `key.capture.stop` | — | 取消捕获（恢复热键分发，无 `key.captured` 回填） |
| `hotkey.clear` | — | 解除全部热键绑定并停监听线程 |
| `record.subscribe` | `{"on": bool}` | —（仅录制面板打开时订阅事件流） |
| `schedule.get` / `schedule.configure` | 见 §9.2 | 当前定时配置 / `nextFire` |
| `app.shutdown` | — | —（优雅停机：停录制/运行、松键、退出） |
| `app.diagnose` | — | 版本对、权限三项、数据目录、最近日志尾部 |

### 3.2 通知（后端 → 前端，stdout 推送）

| 通知 | 载荷 | 说明 |
| --- | --- | --- |
| `record.event` | `{events: [MacroEvent…]}（100ms 批）` | 录制事件流 |
| `record.stopped` | `{count, originX, originY, byLimit}` | 录制结束 |
| `run.node` | `{index, type}` | 节点开始 |
| `run.progress` | `{done, total}` | 回放进度 |
| `run.done` | `{stopped: bool}` | 运行结束 |
| `run.error` | `{type, message}` | 节点异常（现状已兜底） |
| `permission.changed` | `{accessibility, inputMonitoring, screenRecording}` | 权限横幅自动重检（后端 2s 轮询，见 §11） |
| `hotkey.triggered` | `{action: "record"\|"run"\|"pick"}` | 后端 CGEventTap 捕获 F9/F10/F11 |
| `template.captured` | `{path}` | 框选截图完成，回填文件类参数 |
| `key.captured` | `{name}` | 按键捕获完成，回填 `keys` 参数 |
| `workflow.changed` | `{reason, workflow}` | 定时运行/录制结果写回导致后端树变化 |
| `schedule.fired` | `{path}` | 定时触发，前端同步界面状态 |
| `log` | `{level, message}` | 后端日志（协议走 stdout，日志另走 stderr/文件） |

### 3.3 消息示例

```json
→ {"jsonrpc":"2.0","id":7,"method":"run.start","params":{"workflow":{…},"baseX":900,"baseY":600}}
← {"jsonrpc":"2.0","id":7,"result":{}}
← {"jsonrpc":"2.0","method":"run.node","params":{"index":0,"type":"record_replay"}}
← {"jsonrpc":"2.0","method":"run.done","params":{"stopped":false}}
```

---

## 4. 前端设计（Vue 3 + Vite + TDesign）

- 技术栈沿用 macro-recorder：Vue 3 + TS + Vite + TDesign Vue Next。
- 页面即现有主窗三栏结构，组件化拆分：

| 组件 | 对应现状 | 说明 |
| --- | --- | --- |
| `RunPanel.vue` | 运行 GroupBox | 速度/循环/基点/取点/运行停止 |
| `NodeList.vue` | 节点列表+操作按钮 | 拖拽排序（vuedraggable）、启停、增删 |
| `ParamsPanel.vue` | 参数面板 | **由 nodes.definitions 的 schema 动态渲染**（int/float/bool/select/text/file/events），与后端 `ParamDef` 一一对应 |
| `RecordPanel.vue` | 录制面板 | 事件流虚拟滚动、录制结果两按钮 |
| `PermissionBanner.vue` | 权限横幅 | 订阅 permission.changed |
| `ScheduleDialog.vue` | 定时运行 | 配置存后端 |
| `WorkflowMenu.vue` | 文件菜单 | 打开/保存/另存（Tauri 对话框） |

- 状态管理：Pinia 一个 `useAppStore`（连接状态、运行态、当前工作流）。
- RPC 客户端：`src/rpc/client.ts`，id 映射 Promise，通知进事件总线。

---

## 5. 打包与分发

```
前端:  npm run build → dist/ → Tauri frontendDist
后端:  pyinstaller --onedir sidecar  → src-tauri/bin/autoflow-sidecar-{target-triple}/
Tauri: tauri build → Auto Flow.app（embedded sidecar）
签名:  MacDev 签 Tauri app + sidecar 二进制（两者都要）
安装:  scripts/sync_app.sh → /Applications（固定路径，授权稳定）
```

- sidecar 声明为 Tauri `externalBin`，随 app 退出被收割；后端崩溃时 Tauri 重启它（守护循环）。
- PyInstaller 打包 Python 后端不再含 PySide6，体积大幅下降；opencv/onnxruntime 仍占大头（可选：视觉功能拆独立按需下载包，二期）。

---

## 6. 实施计划（预估 2.5~3 个工作日）

> ⚠️ 此排期为 v1.0 初估，评审后已修订为 **5~5.5 个工作日**，请以 §18 为准。

| 阶段 | 内容 | 产出 | 预估 |
| --- | --- | --- | --- |
| P0 协议与骨架 | rpc/server.py（帧解析/分发/通知）+ Tauri 脚手架 + sidecar 拉起 + `app.info` 打通 | 双端 hello world | 0.5 天 |
| P1 后端服务化 | 全部 RPC 方法接现有 core/tasks；录制事件节流推送；权限轮询通知 | 后端功能完备（pytest 覆盖 RPC 层） | 0.5 天 |
| P2 前端移植 | 7 个组件 + RPC client + Pinia；动态参数表单 | 功能对齐现 UI | 1 天 |
| P3 打包签名 | externalBin + MacDev 双签 + sync_app.sh 更新 + 权限引导页 | /Applications 可装可用 | 0.5 天 |
| P4 打磨 | 拖拽排序、事件流虚拟滚动、定时运行对话框、设置持久化 | 体验对齐+超出现状 | 0.5 天（可裁剪） |

**验收标准**（对齐现有功能，不缩水）：
1. 录制（含键盘/修饰键/方向键）→ 结果进节点 → 相对坐标回放落点精确；
2. 图像匹配 / OCR / YOLO 三种查找节点可用（含「截取模板」框选）；
3. 条件门控、循环、调速、F9/F10/F11 热键；
4. 未授权时横幅自动重检；打包后 /Applications 双击即用，授权一次长期有效；
5. 安装包 ≤ 30 MB（不含视觉可选包）。

---

## 7. 风险与对策

| 风险 | 对策 |
| --- | --- |
| sidecar 崩溃带崩 UI | Tauri 守护重启 + 前端断线横幅；后端顶层 try/except 全兜底（现状已有） |
| CGEventTap 权限主体变化 | sidecar 二进制固定文件名+MacDev 签名+固定路径；首次启动引导页 |
| 高频事件打爆 stdout | 100ms 批量 + 仅录制面板打开时订阅（`record.subscribe`） |
| pyobjc/opencv 在 PyInstaller 下漏依赖 | 现有打包脚本已验证 onnxruntime/Vision；CI 前置冒烟 `--check` |
| 双份状态（前端表单 vs 后端节点数据） | **改为后端持有 current workflow**（§10），前端近乎无状态 |
| stdout 被 print/日志/traceback 污染，帧解析错位 | 入口重定向 stdout、协议走原始 fd、日志全走 stderr + 文件、CI 冒烟断言「stdout 恰好 N 行 JSON」（§13） |
| 重新构建后 TCC 授权失效（cdhash 变化） | P0 先做授权持久性 spike 验证；退路是稳定 sidecar 内容或改为带 Info.plist 的 bundle（§12.1） |
| 体积目标 30 MB 不可达 | 先测 onedir 基线再定目标；30 MB 作为二期拆分视觉包后的目标（§12.2） |
| macOS App Nap 节流后台定时器 | 定时器迁后端；长时间运行期间申请 `NSProcessInfo` activity 或前端声明禁用 App Nap（§9.2） |

---

## 8. 现有资产处置

| 资产 | 处置 |
| --- | --- |
| `core/`、`tasks/`、`models/`、`workflows/`、测试 | **原样复用** |
| `core/player.py` 插值算法、`maclistener`、权限方案 | 核心资产，不重写 |
| `ui/`（PySide6） | 迁移期保留作对照；P2 完成后删除 |
| Tauri 版 macro-recorder 经验（CGEventTap 绕崩、MacDev、sync_app） | 直接搬方案 |
| `scripts/build_app.sh` | 改为仅打 sidecar；`.app` 打包移交 Tauri |

---

# 评审补充（v1.1）

> 以下章节为 2026-09-01 对照 `main.py` / `core/` / `tasks/` / `ui/` 源码逐项复核后的补充，
> 包含 3 处**原方案必须修正项**（第 10、12、13 章）和若干遗漏项。

## 9. 协议缺口补齐

### 9.1 工作流真源（见第 10 章）带来的一组增量方法

原方案「前端只暂存编辑中值，保存/运行时整棵提交」与 §7 的「后端唯一真源」自相矛盾：
`record.toNode` 要写回节点，但后端不持有工作流就无从下手。补齐 `workflow.update` /
`node.add` / `node.remove` / `node.move` / `node.toggle` / `node.params.set` 一组方法后矛盾消除。

### 9.2 `schedule.configure` 参数

现状 `ui/scheduler.py` 的配置项原样搬到后端：

```json
{"mode": "每天时刻"|"固定间隔", "atTime": "09:00",
 "intervalMin": 30, "workflowPath": "/abs/x.json", "enabled": true}
```

- 调度器**必须在后端**（`threading.Timer` / `APScheduler`）：前端 WebView 退到后台会被
  macOS 的 App Nap 与定时器节流拉长，10s 轮询的 `QTimer` 语义在前端不可靠。
- 触发时后端从磁盘重新加载工作流（与现状 `_scheduled_run` 一致），并推 `schedule.fired` +
  `workflow.changed`，前端据此刷新节点列表。
- 配置持久化到 `~/Library/Application Support/AutoFlow/config.json`，启动时自动恢复。

### 9.3 通用参数「执行条件」要进协议

`run_when`（总是 / 条件成立 / 条件不成立）**不在任何 `ParamDef` 里**，是 `ParamsPanel.build()`
硬编码追加的尾部下拉。若前端自行硬编码，后端就不再是唯一真源。

**做法**：`nodes.definitions` 返回体增加 `common_params` 段：

```json
{"type":"mouse","name":"鼠标操作","params":[…],
 "common_params":[{"key":"run_when","label":"执行条件","ptype":"select",
   "default":"总是","options":["总是","条件成立","条件不成立"]}]}
```

前端把 `common_params` 渲染在参数表单末尾，语义与现状完全一致。

### 9.4 节点菜单顺序要显式化

`tasks/builtin.py` 中 5 个节点用了 `@register` 类装饰器（注册的是**类对象**）、又在文件末尾
`register(实例)` 覆盖，导致 `_REGISTRY` 插入顺序为「先被装饰的 5 个、后末尾的 4 个」，
菜单顺序是：录制回放 → 图像匹配点击 → 找文字点击 → YOLO → 条件判断 → 鼠标 → 键盘 → 延时 → 注释。

**做法**：`definition()` 增加 `order: int`，`all_definitions()` 按 order 排序；顺带清掉
「装饰器 + 末尾重复注册」的双注册。菜单顺序定为：鼠标、键盘、延时、录制回放、图像、OCR、YOLO、条件、注释。

### 9.5 错误码约定

| 码 | 含义 | 触发 |
| --- | --- | --- |
| `-32700` | 解析失败 | 帧不是合法 JSON（后端跳过并记 stderr） |
| `-32601` | 方法不存在 | — |
| `-32602` | 参数无效 | 缺字段 / 类型错 |
| `-32001` | `already_running` | 运行/录制已在进行 |
| `-32002` | `busy_recording` | 录制中不允许运行（互斥，同现状 `toggle_run`） |
| `-32003` | `not_recording` / `no_record_result` | `record.toNode` 无结果 |
| `-32004` | `workflow_empty` | 空工作流运行（现状弹提示） |
| `-32005` | `permission_missing` | 缺辅助功能/输入监控，`data` 指名缺哪项 |

前端只按 `code` 分支，`message` 仅用于展示。

---

## 10. 【修正】工作流真源归后端，前端近乎无状态

**原方案矛盾点**：§2.2 与 §7 写「后端是唯一真源」，但 §3.1 只有 `workflow.load/save`，
等于把编辑态真源放在前端。这会让 `record.toNode`、定时运行写回、sidecar 重启恢复三件事都落空。

**修订后**：

```
后端持有：current_workflow（内存）+ 磁盘路径 + 定时器 + 录制结果 + 运行/录制状态
前端持有：纯视图 + 表单草稿（未提交的值）+ UI 偏好（localStorage）
```

- 任何结构操作（增删改移/启停/改参）→ 立即发 RPC → 后端改树 → 广播 `workflow.changed`。
- 保存 = `workflow.save{path}`；另存 = `workflow.pickFile` + `workflow.save`。
- 前端只在**表单未失焦/未提交**时持有脏值；切换节点、保存、运行前强制提交（对齐现状
  `_commit_params` 的「面板必须正对当前节点」保护）。
- sidecar 崩溃重启后，前端用 `workflow.current` 一键恢复整棵树，不用用户重开文件。

> 取舍：RPC 调用变多（每次改参一次），但录制事件流本来就走通知，这不会成为瓶颈；
> 换来的是「崩溃可恢复」和「定时运行/录制写回」两件事真正成立。

---

## 11. 【修正】权限是三项，不是两项

`core/permissions.py` 只检测了辅助功能与输入监控，但**图像匹配 / OCR / YOLO 三个节点的
`grab_screen_bgr()` 依赖屏幕捕获**，macOS 上对应第三项权限「屏幕录制」。

| 权限 | 谁需要 | 缺失后果 |
| --- | --- | --- |
| 辅助功能 | `player.py`（pynput 模拟输入）+ `recorder.py`（pynput 鼠标监听） | 回放/录制鼠标无效果 |
| 输入监控 | `maclistener.py`（CGEventTap 键盘） | 录不到键盘、热键失灵 |
| **屏幕录制** | `vision.grab_screen_bgr`（mss / Quartz 兜底）、`screencapture -i` | 图像/OCR/YOLO 节点全部失效 |

**要改的**：

1. `core/permissions.py` 增加 `check_screen_recording(prompt)`（`CGPreflightScreenCaptureAccess`
   / `CGRequestScreenCaptureAccess`）与 `open_screen_recording_settings()`；
2. `permission.changed` 载荷加 `screenRecording`，`app.openPermissionSettings` 的 `panel`
   枚举加 `screen_recording`；
3. 权限横幅改为**分条展示三项**，缺哪项高亮哪项（现状只提示辅助功能）；
4. `main.py --check` 诊断输出增加屏幕录制状态 —— 打包后先在真实环境跑一次确认。

---

## 12. 【修正】sidecar 必须 onedir + 固定路径，`externalBin` 不能直接吃目录

原 §5 写 `pyinstaller --onedir sidecar → src-tauri/bin/autoflow-sidecar-{triple}/`，
但 Tauri 的 `bundle.externalBin` 约定是**单个可执行文件**（构建时按 target triple 找副本），
放目录不会被正确处理。而 `--onefile` 是**绝不可行**的：每次启动解压到随机的
`/var/folders/.../_MEIxxxx`，进程路径不固定 → **TCC 授权每次失效**，与 §2.2 的权限方案直接冲突。

**修订后的打包链路**：

```
pyinstaller --onedir autoflow-sidecar（console=True，不要 --windowed，产物是目录）
        ↓
把整个 onedir 目录放进 src-tauri/resources/autoflow-sidecar/
        ↓
tauri.conf.json: bundle.resources = ["resources/autoflow-sidecar/**"]
（不使用 externalBin，Rust 侧用 resolved_resource_path() 拼出可执行文件绝对路径后 spawn）
        ↓
codesign --force --deep --sign "MacDev" 整个 .app（含嵌套 .so，逐一被签）
```

- **固定路径** = `.app/Contents/Resources/autoflow-sidecar/autoflow-sidecar`，与现状
  `sync_app.sh` 装到 `/Applications/Auto Flow.app` 组合，路径跨构建稳定。
- **signed 的 nested 二进制**：macOS 要求 .app 内所有 Mach-O 都有签名，否则 TCC/Gatekeeper 拒绝。
  `codesign --deep` 会处理嵌套，但要确保**签名顺序**（先内后外）与 entitlements 一致。
- **Apple Silicon**：裸 Mach-O 至少要有 ad-hoc 签名；用 MacDev 身份签更好（cdhash 稳定）。

### 12.1 授权持久性 spike（P0 必做，半天）

TCC 对辅助功能的记录与二进制的 cdhash 相关，而 cdhash 随代码内容变化。
现状 `build_app.sh` 的注释认为「MacDev 稳定签名身份 → 授权跨构建保持」，但 sidecar 的形态变了
（.app bundle → 裸可执行 / 嵌套资源），**必须先验证再往下做**：

> 用 MacDev 签 sidecar → 装 /Applications → 授权 → 改一行代码重新构建签名 → 覆盖安装 →
> 验证三项权限是否仍为已授权。
>
> - 保持 → 按上述方案执行；
> - 失效 → 退路：① sidecar 内容尽量不做无谓变更（版本号不变则不重建）；
>   ② 首次启动引导页 + 授权状态自动重检（已有）；③ 极端情况改回「sidecar 也打成 .app bundle」
>   （带 Info.plist 与 CFBundleIdentifier，TCC 用 bundle id 标识，最稳）。

**这个 spike 的结论决定 §2.2 的核心假设是否成立，必须放在 P0 最前面。**

### 12.2 体积目标要重估

「≤ 30 MB」偏乐观。sidecar 依赖里的大头：`opencv-python`（约 60~90 MB）、
`onnxruntime`（约 15~20 MB）、`numpy`（约 20 MB）、`pyobjc`（约 10 MB）、`pynput`/`mss`。

**做法**：P0 先打一版**去掉 PySide6 的 sidecar onedir**，实测基线体积，再定目标。
建议改为两级目标：

- 主包目标：**在基线基础上不再增长**，且显著小于 135 MB（预期 40~70 MB）；
- ≤ 30 MB 只作为**二期拆分视觉可选包（opencv + onnxruntime 按需下载）后**的目标。

---

## 13. stdio 洁净性：JSON-RPC over stdio 最大的坑（原方案未提）

协议要求 **stdout 上只有协议帧**，但 Python 侧有大量东西想往 stdout 写：
`print()`、`logging` 默认 `StreamHandler`、未捕获 traceback、PyInstaller 的启动信息、
onnxruntime / OpenCV / CoreML 的 C++ 层告警。任一字节混入都会让前端帧解析错位。

**后端强制约束**：

1. 入口第一件事：`sys.stdout = sys.stderr`（或写日志文件），协议输出改用**原始 fd**：
   `OUT = os.fdopen(1, "wb", buffering=0)`，只由 `OUT` 写帧；
2. `logging.basicConfig(stream=sys.stderr)`，另加 `RotatingFileHandler` 写
   `~/Library/Logs/AutoFlow/backend.log`（前端「复制诊断信息」从这里取尾部）；
3. `sys.excepthook` 全部转 stderr + `log` 通知；
4. 帧格式 **NDJSON，compact 输出**（`separators=(",", ":")`，JSON 会把 `\n` 转义为 `\\n`，
   天然无裸换行）；每条写完立即 flush；
5. 环境 `PYTHONUNBUFFERED=1`，并显式 `sys.stdout.reconfigure(encoding="utf-8", newline="\n")`；
6. 禁止任何第三方库在业务路径上 `print`——CI 冒烟加一条断言：
   启动 sidecar 跑 `app.info`，**stdout 必须恰好是 1 行 JSON**，否则失败。

**前端侧同样要做缓冲切分**：tauri-plugin-shell 的 stdout 事件按数据块投递，**不保证行边界**，
必须自行累积缓冲、按 `\n` 切分，残留半行留到下一块。

**背压**：通知不能阻塞业务线程（回放线程被 IO 卡住会拖慢时序）→ 后端用「通知队列 + 独立写线程」，
队列上限丢弃策略：日志类丢弃，`run.*` / `record.*` 保留。

---

## 14. 热键与按键捕获的细节

- 热键（F9/F10/F11）由后端 CGEventTap 捕获 → 推 `hotkey.triggered` → 前端执行动作。
  原 §3.2 只列了 `hotkey.set`，**没有触发通知**，已补。
- **输入框聚焦时要屏蔽热键**：后端无法知道前端焦点 → 由前端收到 `hotkey.triggered` 后判断
  `document.activeElement` 是否为输入控件，是则忽略（现状 Qt 版也没处理，属顺带修复项）。
- 参数面板的「捕获」按钮（填 `keys` 参数）复用同一个 listener：走 `key.capture` →
  `key.captured{name}`，捕获期间后端**暂挂热键分发**，避免把待捕获的键当热键触发。
- `HOTKEY_NAMES = {"F9","F10","F11"}` 的回放抑制逻辑在 `tasks/builtin.py`，保持现状。

---

## 15. 现状代码的已知缺陷

> ✅ **2026-09-01 更新**：前两项（功能性故障）与第四项的连带缺陷已修复并验证通过，见 §15.1。

复核中发现的**实际会报错**的问题，建议在 P1 前单独一个 commit 修掉（与迁移解耦，便于回滚对照）：

| 位置 | 问题 | 后果 | 状态 |
| --- | --- | --- | --- |
| `ui/params_panel.py::_snip_template` | 用了 `os` 与 `QTimer`，但文件顶部只 import 了 `QtCore.Signal` 和若干 `QtWidgets`，两个名字都未导入 | 参数面板「截取模板」按钮一点就 `NameError`，功能实际不可用 | ✅ 已修 |
| `ui/scheduler.py` 第 79 行 | 用 `QTime(hh, mm)`，但 `QtCore` 只导入了 `Qt, QTimer, Signal, QObject` | 「定时运行」对话框点确定即 `NameError`，定时功能实际不可用 | ✅ 已修 |
| `ui/scheduler.py::_apply` | 未选工作流时 `path_edit.text()` 是占位文本「（未选择）」，为 truthy 会绕过 `bool(workflow_path)` 校验 | 定时器被启用但路径非法，到点触发必然 `Workflow.load` 失败 | ✅ 已修 |
| `tasks/builtin.py` | 5 个节点同时用 `@register` 类装饰器与末尾 `register(实例)`，注册两次 | 无害但冗余，且决定菜单顺序（见 §9.4） | ⬜ 待办 |
| `core/vision.py::grab_screen_bgr` | 只取 `sct.monitors[1]`（主屏），scale 也只按主屏算 | 多屏环境下图像/OCR/YOLO 只在主屏生效（已知限制，迁移后保持，写进帮助） |
| `tasks/builtin.py::KeyboardInputTask` | `pynput` 的 `kb.type()` 只支持 ASCII | 「键盘输入-文本」填中文会失败；改进方案：走 `CGEventKeyboardSetUnicodeString` 或剪贴板粘贴 |
| `ui/main_window.py` | `self.use_rel_check = None`、`EventsEditMixin` 空类 | 死代码，清理 |

> 前两项是**用户可见的功能性故障**，优先级高于迁移本身，建议先修。

### 15.1 已修内容与验证（2026-09-01）

**改动**

- `ui/params_panel.py`：补 `import os / subprocess / time`，`QtCore` 增加 `QTimer`，
  `QtWidgets` 增加 `QHBoxLayout`（原为两处函数内导入，统一提到顶部）；`_snip_template`
  在用户取消框选（未落盘）时改为提示「未生成模板图（已取消或框选无效）」而不是静默失败。
- `ui/scheduler.py`：`QtCore` 补 `QTime`；`QtWidgets` 补 `QPushButton`、`QMessageBox`
  （原为函数内导入）；对话框改用成员变量 `_path` 保存真实路径，未选工作流时点确定弹出提示并拒绝启用。

**验证方式**：`QT_QPA_PLATFORM=offscreen` 下起 `QApplication`，mock 掉 `subprocess.Popen`
模拟 `screencapture` 成功/取消两种结局，断言回填路径、文件落盘、`params_changed` 触发、
取消时不覆盖原值；定时对话框断言构造成功、`_apply` 在空路径时拒绝启用、正常路径下
`nextFire` 计算正确。10 项断言全部通过；`pytest tests/` 24 例全通过（未回归）。

**未做**：多屏支持（`vision` 只取 `monitors[1]`）、`pynput kb.type()` 中文输入、
节点双注册与菜单顺序（§9.4）——这三项属行为变更，放在 P0.5/P4 与迁移一并处理。

### 15.2 授权持久性 spike（P0-S）构建与状态（2026-09-01）

**结论（P0-S 已通过）**：spike 后端可正常以「onedir + 固定路径 + MacDev 自签」形式运行并
通过 stdio JSON-RPC 通信。**授权持久性已验证**：连续两次「同源代码重建 + 覆盖安装」后
CDHash 不变（`97ebf3ec…e1c819`），辅助功能授权保持为 `true`；屏幕录制由用户在 GUI 会话
确认 `true`（agent 后台会话该值为会话敏感假阴性，见坑 3）。即 §12.1 假设成立，P0 解锁。

**后续注意（改源码会换 CDHash，但授权仍持久）**：见坑 4——编辑 Python 源码会改变嵌入 PYZ →
CDHash 变化，但 **MacDev 自签身份稳定时 TCC 授权按证书+identifier 匹配、不绑定 CDHash**，
故授权跨源码改动仍持久，无需重授权。只有 ad-hoc 签名（`codesign -`）才会把 CDHash 写进
授权要求，那种才需在换 CDHash 后重授权。

**打包脚本（新增）**

- `scripts/build_sidecar.sh`：PyInstaller `--onedir` 打包 `rpc/server.py` →
  `dist/autoflow-sidecar/`（体积基线 23 MB），MacDev 自签。`--hidden-import` 仅含
  ApplicationServices/AppKit/Foundation/CoreFoundation/Quartz（P1 接视觉节点后体积重测）。
- `scripts/build_spike_app.sh`：按 PyInstaller 真实 `.app` 模式打包——`Contents/MacOS/autoflow-sidecar`
  即**实际运行的 sidecar（也是 CFBundleExecutable、被 TCC 授权的二进制）**，`Contents/Frameworks/`
  放 libpython + PyObjC 绑定 + `base_library.zip` 软链（`→ ../Resources/base_library.zip`），
  `Contents/Resources/base_library.zip` 为真实 stdlib。MacDev 自签。产物
  `dist/Auto Flow RPC Spike.app`，bundle id `com.example.autoflow.rpc-spike`。

**踩坑（spike 经 `/Applications` pipe 运行无 stdout 的根因 + TCC 授权口径）**

1. 手动把 onedir 包成 `.app` 时，若把 sidecar 当 `CFBundleExecutable` 放在
   `Contents/MacOS/`，PyInstaller bootloader 进入「.app 模式」并把 `PYTHONHOME` 指向
   `Contents/Frameworks`，需要：libpython 在 `Frameworks`、`base_library.zip` 软链
   `Frameworks/base_library.zip → ../Resources/base_library.zip`、PyObjC 绑定
   （objc/AppKit/CoreFoundation/…）也得在 `Frameworks`。漏掉任一都会「启动即退出且无输出」
   （stdout 被重定向、stderr 又常被吞，极难定位）。`scripts/build_spike_app.sh` 已按此
   正确布局打包，且让 `Contents/MacOS/autoflow-sidecar` 就是**实际运行的 sidecar**。

2. **TCC 授权口径（关键）**：系统设置里手动给「屏幕录制 / 辅助功能 / 输入监控」授权时，
   `+` 只能选 **`.app` 包本身**，TCC 授权的是 `.app` 的主可执行文件
   （`Contents/MacOS/CFBundleExecutable`）。因此 spike 必须让「被授权的二进制」=
   「实际运行的二进制」= `Contents/MacOS/autoflow-sidecar`，否则会出现「钻进包里选嵌套
   二进制被拒 / 加整个 .app 却授权到另一份副本」的尴尬。早期把 sidecar 放到
   `Contents/Resources/sidecar` 的版本虽能跑，但 TCC 授权对不上，已弃用。

3. **生产侧提醒（§12 设计要修正）**：Tauri `.app` 里把 sidecar 放 `Contents/Resources/`
   时，**同样无法用系统设置手动 `+` 选中那个嵌套二进制**。正确做法是由 sidecar 自身在
   启动时调用请求 API 弹系统提示让用户点允许（辅助功能 `AXIsProcessTrustedWithOptions`、
   屏幕录制 `CGRequestScreenCaptureAccess`），TCC 按调用进程的签名记录授权。本 spike 的
   `app.requestPermissions` 已演示该模式，P1 接真实视觉节点时应照搬。

4. **改源码会换 CDHash，但 MacDev 自签下授权仍持久（本会话踩到并纠正）**：PyInstaller 把
   Python 字节码打进 `autoflow-sidecar` 可执行文件的 PYZ 段，该段在 codesign 覆盖范围内，故
   编辑 `core/permissions.py` 等源码会让整份可执行文件 CDHash 变化（修复 -32000 时实测
   `97ebf3ec…` → `8eedd926…`）。**但 TCC 授权并不绑定裸 CDHash**：用 MacDev 自签（稳定身份）
   时，designated requirement 锁定 `identifier + 证书(MacDev)`、不含 CDHash，所以换 CDHash 后
   授权依旧匹配、三项仍 `true`（用户在 GUI 会话实测确认，且未重新授权）。**只有 ad-hoc 签名
   `codesign -` 才把 CDHash 写进授权要求，那种才需在换 CDHash 后重授权。** 因此 P1 任何源码
   改动都无需重授权，只要 MacDev 身份与 bundle id 不变。配套修正：`scripts/build_spike_app.sh`
   改为**总是先 `build_sidecar.sh` 重建 onedir**（不再「存在即跳过」），避免装旧二进制而排查无果。

5. **PyObjC 首次懒加载 `-32000`（已修）**：`ApplicationServices.AXIsProcessTrusted` 等符号
   在进程内首次访问时，`objc/_lazyimport.py` 的 `get_constant` 可能抛 `KeyError`
   （框架尚未完全载入），**第二次访问即成功**（符号被缓存）。未修前 `app.info` 第一次调用会
   在 `core/permissions.py` 炸出 `-32000 Internal error, detail 'AXIsProcessTrusted'`
   （用户首跑即中招，第二跑正常）。修复：在 `core/permissions.py` 加 `_retry_call(fn)`，
   对 `check_accessibility / check_input_monitoring / check_screen_recording` 的 PyObjC 调用
   失败重试一次；并对 `rpc/server.py` 的 `_perm_loop` 轮询线程加 try/except，避免单轮快照异常
   打死 daemon 线程。修后首跑即返回干净结果（不再 -32000）。

**已验证的 RPC 面**

`app.info` / `app.diagnose` / `app.shutdown` / `app.requestPermissions` /
`app.openPermissionSettings` 均可用；未知方法 → `-32601`，非法 JSON → `-32700`，
stdout 仅含 compact NDJSON（§13 洁净性成立）。另已接齐 §9 全部业务方法：
`workflow.*` / `node.*` / `nodes.definitions` / `run.*` / `record.*` /
`hotkey.set` / `hotkey.clear` / `key.capture`(+`.stop`) / `base.pick` /
`schedule.get` / `schedule.configure`，详见 §15.3。`tests/test_rpc.py` 11 例全过。

**T5 验证步骤（待用户授权）**

1. 授权三项（二选一，推荐写法 B）：
   - **A. 手动加**：系统设置 → 隐私与安全性 → 辅助功能 / 输入监控 / 屏幕录制，分别点 `+`，
     **选中 `.app` 包本身**（`/Applications/Auto Flow RPC Spike.app`），**不要钻进 Contents 选嵌套文件**。
     输入监控无 prompt API，只能走这条。
   - **B. 运行提示（最稳，尤其屏幕录制）**：`./scripts/run_spike.sh app.requestPermissions`
     会弹「辅助功能」「屏幕录制」系统提示，点「允许 / 打开系统设置」即可；输入监控仍需走 A。
2. 验证已授权：`./scripts/run_spike.sh app.info` → 三项权限应为 `true`
   （注意 `inputMonitoring` 的 `true` 在未建 tap 时是「无限制」假阳性，见 §11/§15 说明）。
   **坑**：`CGPreflightScreenCaptureAccess`（屏幕录制预检）对调用进程所在会话敏感，
   非 GUI/Aqua 会话（如 CI、agent 后台 Bash）一律返 false——此时 `app.info` 的
   `screenRecording:false` 是**假阴性**，须以用户在 GUI 会话的终端查询为准。
3. 重建并覆盖安装（MacDev 自签身份稳定时，改源码换 CDHash 也**不影响**已授权项，
   无需重授权；仅 ad-hoc 签名换 CDHash 才需重授权）：
   `./scripts/build_sidecar.sh && ./scripts/build_spike_app.sh`，
   再 `rm -rf "/Applications/Auto Flow RPC Spike.app" && cp -R "dist/Auto Flow RPC Spike.app" /Applications/.`
4. 再次跑 `app.info`，三项仍为 `true` → **§12.1 假设成立，P0 解锁**；否则转 §12.1 退路。

---

### 15.3 RPC 后端方法进度（P1-1 / P1-2 / P1-3，2026-09-01）

**后端 `rpc/controller.py` + `rpc/server.py` 已接齐 §9 全部方法**（前端尚未建，P2）：

- **P1-1 工作流真源（§9/§10）**：`workflow.current/load/save/new/update`、`node.add/remove/move/toggle/params.set`、`nodes.definitions`（含 `common_params` + §9.4 顺序）。结构变更统一广播 `workflow.changed`。
- **P1-2 运行/录制（§5/§9.5）**：`run.start/stop`（Executor 包装，错误码 -32001/-32002/-32004）、`record.start/stop/toNode`（Recorder 包装，100ms 批 `record.event`，错误码 -32003）；修 `Recorder.stop()` 漏 `return` 的潜藏 bug。
- **P1-3 热键/捕获/取点/定时（§3.2 + §9.2）**：
  - `hotkey.set/clear`：单一 `MacKeyboardListener`，F9/F10/F11 → record/run/pick，捕获 `hotkey.triggered {action}`。
  - `key.capture` / `key.capture.stop`：复用同一 listener，捕获期间**暂挂热键分发**，捕获到键经 `key.captured {name}` 异步回填（新增 `key.capture.stop` 取消，原 §3.1 表仅列 `key.capture`）。
  - `base.pick`：F11 取点，读全局光标位置（与 pynput 录制坐标空间一致）。
  - `schedule.configure` / `schedule.get`：后端 `threading.Timer` 触发，触发时从磁盘重载工作流并广播 `schedule.fired` + `workflow.changed`；配置持久化到 `~/Library/Application Support/AutoFlow/config.json`（dev 模式下为项目根 `config.json`），启动时自动恢复。

**错误码表（§9.5）全部落地**：-32000 内部、-32601 未知方法、-32602 参数非法、-32700 解析、-32001 already_running、-32002 busy_recording、-32003 not_recording/no_record_result、-32004 workflow_empty。（`-32005 permission_missing` 已定义但当前未由任何 handler 主动发出。）

**测试**：`tests/test_rpc.py` 由 3 例扩至 **11 例全过**（端到端拉起真实 sidecar，校验协议帧、stdio 洁净性、错误码、热键/捕获/取点/定时闭环与 schedule 触发重载）。

## 15.4 Tauri 2 前端脚手架（P1-5，2026-09-01）

按 §3.2/§4/§12/§13 落地 `tauri/` 子项目（Vue 3 + Vite + TDesign + Pinia + Rust 外壳）：

- **工程**：`tauri/package.json`（Tauri 2 + Vue3 + TDesign + Pinia + vuedraggable + plugin-dialog）、
  `vite.config.ts`（dev 1420）、`tsconfig*.json`、`index.html`、`tauri.conf.json`
  （`bundle.resources = ["resources/autoflow-sidecar/**"]`，Rust 侧用 `resource_dir()` 拼固定路径 spawn，
  **非 externalBin**，见 §12）、`capabilities/default.json`（event + dialog）。
- **通信（§13）**：`src/rpc/client.ts` 订阅 Rust `rpc_event` 事件，按 `\n` 累积缓冲切分（半包/粘包兜底），
  `request()` 返回 Promise 按自增 id 匹配；`send_rpc` invoke 写 sidecar stdin。
- **状态（§10）**：`src/stores/app.ts` 一个 Pinia store，后端为唯一真源；握手 `app.info` 校验
  `protocolVersion===1`；订阅全部通知（workflow.changed / permission.changed / hotkey.triggered /
  key.captured / record.* / run.* / schedule.fired / rpc_up / rpc_down）；输入框聚焦时屏蔽热键（§14）。
- **7 组件**：`RunPanel` / `NodeList`（vuedraggable 拖拽→`node.move`）/ `ParamsPanel`（按
  `nodes.definitions` 的 schema 动态渲染 int/float/bool/select/text/file/keys，keys 走 `key.capture` 回填）/
  `RecordPanel`（事件流）/ `PermissionBanner`（订阅 permission.changed）/ `ScheduleDialog`（配置存后端）/
  `WorkflowMenu`（打开/保存走 Tauri 对话框）；`App.vue` 三栏布局。
- **Rust 外壳**：`src-tauri/src/lib.rs` 用 `resource_dir()` 拼 `Resources/autoflow-sidecar/autoflow-sidecar`
  绝对路径 `Command::spawn`，stdout 逐行 `emit("rpc_event")`，`send_rpc` 命令写 stdin，stdout EOF 后
  守护重启（§5 崩溃恢复）；`Cargo.toml` / `build.rs` / `main.rs` / `capabilities`。

**本机验证状态**：前端 `npm install` + `vue-tsc --noEmit` + `vite build` 可独立校验（不依赖 Rust）；
Rust 侧需先装 Rust 工具链（`rustup`）并放入 sidecar onedir + `tauri icon` 生成图标后才能
`npm run tauri dev/build`（本机当时未装 cargo，Rust 侧仅源码交付，待用户装好 Rust 后一键构建）。
`tauri/README.md` 写明三步前置与运行命令。

---

## 16. 持久化与配置分工

| 数据 | 存哪 | 说明 |
| --- | --- | --- |
| 工作流 JSON | 磁盘（`~/Library/Application Support/AutoFlow/workflows/`） | **格式零变更**：`SCRIPT_VERSION=2`、`ensure_ascii=False`、兼容 Tauri 版裸事件脚本；现有用户的 `workflows/*.json` 必须原样可用 |
| 模板图 | `workflows/templates/` | 保持现状；`screencapture -i` 产出的 PNG 直接落此目录 |
| 全局配置（热键、定时、最近文件） | 后端 `config.json` | 新增 |
| UI 偏好（窗口尺寸、面板折叠、主题） | 前端 `localStorage` | 与后端无关 |
| 日志 | `~/Library/Logs/AutoFlow/backend.log` + stderr | 供 `app.diagnose` 取用 |

**数据目录不变**是关键：`core/paths.py` 依赖 `sys.frozen`，sidecar 仍是 PyInstaller 产物 →
`frozen=True` 成立 → 目录仍是 `~/Library/Application Support/AutoFlow`，用户的工作流、
模板、YOLO 模型（含播种逻辑）**零迁移成本**。Tauri 的 bundle id 也用 `com.catlair.autoflow` 保持一致。

---

## 17. 测试、工程策略与验收补充

### 17.1 测试

- 现有 `tests/test_core.py`（18 例）**原样保留**，继续覆盖 `core/` 与 `tasks/`。
- 新增 RPC 层测试：起 sidecar 子进程，直接写 stdin / 读 stdout，断言帧格式与错误码；
  **必须在 CI 里跑「stdout 恰好 N 行 JSON」的洁净性断言**（§13.6）。
- 测试环境不能真的动鼠标：加 `AUTOFLOW_DRY_RUN=1`，让 `Player` 把输入调用替换为记录器。
- 前端：Vitest 覆盖 RPC client 的**半包/粘包切分**（构造跨块 JSON 用例）。
- 打包冒烟：`autoflow-sidecar --check` + `app.info` 在**打包后**跑一遍（对齐现状 `--check` 思路）。

### 17.2 工程策略

- 分支：`feat/tauri-migration`，main 保持 PySide6 可发版；P3 验收通过后合入并删 `ui/`。
- 双轨入口：`main.py --ui qt|rpc`，P2 完成前可用旧 UI 对照行为。
- 回滚点：P0 / P1 / P2 / P3 各打 tag；`ui/` 保留到 P3 验收之后。任一阶段阻塞 → 回退到上一个 tag，
  PySide6 版本继续维护。
- 版本号：`app.info` 返回 `{appVersion, sidecarVersion, protocolVersion}` 三元组，
  前端校验不匹配即提示（避免 sidecar 更新滞后导致协议错配）。

### 17.3 验收清单（在原 5 条之外补充）

6. 三项权限全部就绪时无横幅，缺任一项时横幅分条高亮且可一键跳转；
7. 工作流 JSON 与现有文件**双向兼容**：旧文件能开，新存的文件旧版也能开；
8. 「截取模板」与「定时运行」两个对话框可用（§15 两个缺陷已修的直接验证）；
9. sidecar 被 kill 后前端 5s 内重连成功，工作流树与运行/录制状态完整恢复；
10. 运行/录制互斥、运行中停止不卡键（现状已兜底，迁移后回归）；
11. 连续构建两次并覆盖安装后，三项权限仍保持（§12.1 spike 的结论转为回归用例）。

---

## 18. 修订后的排期（以此为准）

| 阶段 | 内容 | 预估 |
| --- | --- | --- |
| **P0-S 验证** | sidecar onedir + MacDev 签名 + 固定路径的**授权持久性 spike**；sidecar 体积基线测量；`app.info` hello world | 0.5 天（**阻塞后续**） |
| P0 | rpc/server.py（NDJSON 帧、stdout 洁净性、错误码、通知队列）+ Tauri 脚手架 + sidecar 拉起与半包切分 | 0.5 天 |
| ~~P0.5~~ | ~~修 §15 两个功能故障（截取模板、定时运行）+ 死代码清理 + 节点顺序~~ | ✅ 已完成（§15.1）；剩余「节点顺序 + 死代码清理」并入 P4 |
| P1 | 后端服务化：§9 全部方法 + 工作流真源迁后端（§10）+ 三项权限（§11）+ 调度器 + 按键/模板捕获 | 1.5 天 |
| P2 | 前端 7 组件 + RPC client + Pinia + 动态表单（含 `common_params`） | 1 天 |
| P3 | 打包：resources 方案 + deep 签名 + notary 流程 + sync_app.sh 更新 + 权限引导页 | 1 天 |
| P4 | 打磨：拖拽排序、事件流虚拟滚动、中文输入改进、诊断面板、崩溃重连提示；节点顺序修正（§9.4）+ 死代码清理 | 0.5~1 天（可裁剪） |

合计 **4.5~5.5 个工作日**（原估 2.5~3 天；§15.1 的缺陷修复已完成，从排期中扣除 0.5）。
增加主要来自：授权 spike（0.5）、工作流真源后端化（0.5）、第三项权限（0.3）、打包方案修正（0.5）。

**进度（2026-09-01）**：P0-S 已收口；P1 后端 RPC 面（§9）全部接齐并 11 例 pytest 通过（§15.3）；
P1-5 Tauri 2 脚手架已完成（`tauri/`，前端可 `vite build` 校验，Rust 侧待用户装 Rust 后 `tauri dev`）。
P2（前端 7 组件）/ P3（打包签名）已随 P1-5 一并铺好骨架，剩余为真实打包签名联调与打磨（P4）。
