# Auto Flow — Tauri 2 前端外壳

Vue 3 + Vite + TDesign + Pinia 前端，内嵌 Python sidecar（`rpc/` 的 NDJSON JSON-RPC 后端）。
后端是唯一真源（§10），前端近乎无状态。

## 目录

```
tauri/
  package.json / vite.config.ts / tsconfig*.json / index.html
  src/                 # Vue 前端
    rpc/{types,client}.ts   # NDJSON 客户端（§13 半包/粘包切分 + id→Promise）
    stores/app.ts           # Pinia（连接态/运行态/当前工作流）
    components/*.vue        # 7 个组件 + App 三栏布局
  src-tauri/           # Rust 外壳
    src/lib.rs              # 拉起 sidecar、stdout 逐行 emit rpc_event、send_rpc 写 stdin、守护重启
    tauri.conf.json         # Resources 固定路径放 sidecar onedir（§12）
    capabilities/default.json
    resources/autoflow-sidecar/   # 构建前放入 sidecar onedir（见下）
    icons/                  # 见 icons/README.md（构建前需 tauri icon 生成）
```

## 前提

1. **安装 Rust 工具链**（本机当前未装）：

   ```bash
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
   source "$HOME/.cargo/env"
   ```

2. **放入 sidecar onedir**：把 `scripts/build_sidecar.sh` 产出的 `dist/autoflow-sidecar/`
   整目录复制到 `tauri/src-tauri/resources/autoflow-sidecar/`，使最终路径为
   `tauri/src-tauri/resources/autoflow-sidecar/autoflow-sidecar`（可执行文件）。

   ```bash
   ./scripts/build_sidecar.sh
   mkdir -p src-tauri/resources
   cp -R ../dist/autoflow-sidecar src-tauri/resources/autoflow-sidecar
   ```

3. **生成图标**（见 `src-tauri/icons/README.md`）：`npm run tauri icon <1024.png>`。

## 运行 / 构建

```bash
npm install
npm run tauri dev      # 开发：Vite + Tauri 窗体
npm run tauri build    # 打包 Auto Flow.app（MacDev 双签见 §5/§12 后续）
```

前端独立校验（不依赖 Rust）：`npm run build`（含 `vue-tsc --noEmit` + `vite build`）。

## 通信契约

- 前端 `send_rpc` invoke → Rust 写 sidecar stdin（NDJSON 帧以 `\n` 结尾）。
- Rust 读 sidecar stdout，逐行 `emit("rpc_event", line)`；前端 `client.ts` 累积缓冲按 `\n`
  切分（§13 半包兜底），按 id 匹配响应、无 id 的帧作为通知分发。
- 全部方法/通知见 `docs/迁移计划书-python后端+tauri前端.md` §3.1 / §3.2。
