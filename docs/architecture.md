# 系统架构总览

> 更新：2026-09-12 · 迁移历史见《迁移计划书-python后端+tauri前端.md》（已归档）

## 进程模型

```
┌──────────────────────────────────────────────────┐
│ Auto Flow.app（Tauri 2 / Rust）                  │
│  ├─ WebView：Vue 3 + TDesign 前端（8 组件）      │
│  ├─ spawn / 守护重启 Python sidecar              │
│  └─ rpc_event 事件转发 + send_rpc 命令           │
└──────────────┬───────────────────────────────────┘
               │ stdio NDJSON（JSON-RPC 2.0）
┌──────────────▼───────────────────────────────────┐
│ autoflow-sidecar（PyInstaller onedir，MacDev 签）│
│  ├─ rpc/server.py    帧解析/分发/通知队列（写锁）│
│  ├─ rpc/controller.py 工作流真源 + 状态机        │
│  ├─ core/             录制/回放/执行器/视觉/热键 │
│  └─ tasks/           十二种内置节点（自描述参数）│
└──────────────────────────────────────────────────┘
```

- **Tauri 壳**：`tauri/src-tauri/src/lib.rs` — sidecar 候选路径解析（Resources → 开发态回退）、
  stdout 逐行 `rpc_event`、stderr 尾部缓冲随 `rpc_down` 上报、EOF 守护重启（2s）。
- **前端**：`tauri/src/` — `rpc/client.ts`（按 id 配对响应、通知订阅、半包粘包切分）、
  Pinia `stores/app.ts`（后端唯一真源的前端镜像）、8 个组件。

## 通信协议

NDJSON JSON-RPC 2.0 over stdio。请求/响应按 id 配对；通知（无 id）广播。
**结构操作以响应为准刷新 UI，通知仅作广播冗余**——通知经 Tauri 事件转发偶发丢失，
不能作为 UI 刷新的唯一依据（教训见 modules/frontend.md）。

协议方法与通知全清单 → [modules/rpc-protocol.md](modules/rpc-protocol.md)。

## 目录

```
core/      events(流程图模型) recorder player executor maclistener mackeys
           keymap vision ocr yolo permissions paths
tasks/     base(注册表/自描述参数) builtin(十二种节点)
rpc/       server controller
tauri/     前端 + Rust 壳
models/    yolo11n.onnx（默认 YOLO 模型）
tests/     test_core.py + test_rpc.py（173 例，RPC 用真实子进程）
scripts/   build_sidecar / sign_tauri_app / sync_app / make_dmg / check_installed
```

## 硬约束（踩坑沉淀，不可违反）

1. **stdout 洁净性**：协议帧只走原始 fd 1 且全局写锁；任何日志/print 一律 stderr。
   （无锁并发写 → 字节交错 → 前端随机解析失败）
2. **广播不得改真源**：对外摘要必须另建视图；`to_dict()` 的 dict 是原引用。
   （曾把真源 events 摧毁成 {count}，运行报 'str' has no 'get'）
3. **结构操作响应驱动刷新**：通知会丢。
4. **pyobjc 懒加载预热**：keyboard/mouse 两个 tap 线程并发首访 Quartz 符号会
   `KeyError('CGEventGetLocation')`，鼠标点击全部丢失（导入期单线程预热）。
5. **键盘监听禁用 pynput**：macOS 15 的 TSMGetInputSourceProperty 主线程断言
   会 EXC_BREAKPOINT 崩溃（与旧 Tauri 版 rdev 同源）。用 `core/maclistener.py`
   自建只读 CGEventTap，绝不查 TIS。
6. **TCC 按二进制授权**：CGEventTap 权限挂在调用进程上。sidecar 必须由已授权的
   Auto Flow.app 拉起；MacDev 签名 + 固定 /Applications 路径保证授权跨构建有效。
7. **修完必须部署**：sidecar/前端改完要跑完整 `build_sidecar → tauri build →
   sign → sync_app`，否则用户跑的还是旧包（曾致旧包带病运行 6 天）。

## 开发命令

```bash
.venv/bin/python -m pytest tests/ -q     # 91 例
./scripts/build_sidecar.sh               # 打包 Python sidecar（MacDev 签名）
cd tauri && npm run tauri build          # 前端 + .app
./scripts/sign_tauri_app.sh              # 双签 + sidecar 启动冒烟
./scripts/sync_app.sh                    # 部署 /Applications 并启动
```
