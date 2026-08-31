# Auto Flow 迁移计划书：PySide6 单体 → Python 后端 + Tauri 前端

> 版本 v1.0 · 2026-08-31 · 状态：待评审
> 现状仓库：`/Users/catlair/mobile/workspace/auto-flow`（PySide6 单体，功能已全部可用）

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
| `base.pick` | — | `{x, y}`（F11 取点） |
| `hotkey.set` | `{"record","run","pick"}` | — |

### 3.2 通知（后端 → 前端，stdout 推送）

| 通知 | 载荷 | 说明 |
| --- | --- | --- |
| `record.event` | `{events: [MacroEvent…]}（100ms 批）` | 录制事件流 |
| `record.stopped` | `{count, originX, originY, byLimit}` | 录制结束 |
| `run.node` | `{index, type}` | 节点开始 |
| `run.progress` | `{done, total}` | 回放进度 |
| `run.done` | `{stopped: bool}` | 运行结束 |
| `run.error` | `{type, message}` | 节点异常（现状已兜底） |
| `permission.changed` | `{accessibility, inputMonitoring}` | 权限横幅自动重检（后端 2s 轮询） |
| `log` | `{level, message}` | 后端日志 |

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
| 双份状态（前端表单 vs 后端节点数据） | 前端只暂存编辑中值，保存/运行时整棵工作流提交后端，后端唯一真源 |

---

## 8. 现有资产处置

| 资产 | 处置 |
| --- | --- |
| `core/`、`tasks/`、`models/`、`workflows/`、测试 | **原样复用** |
| `core/player.py` 插值算法、`maclistener`、权限方案 | 核心资产，不重写 |
| `ui/`（PySide6） | 迁移期保留作对照；P2 完成后删除 |
| Tauri 版 macro-recorder 经验（CGEventTap 绕崩、MacDev、sync_app） | 直接搬方案 |
| `scripts/build_app.sh` | 改为仅打 sidecar；`.app` 打包移交 Tauri |
