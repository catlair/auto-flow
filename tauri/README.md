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
    tauri.conf.json         # Resources 固定路径放 sidecar onedir（§12）；bundle.resources = autoflow-sidecar/**/*
    capabilities/default.json
    autoflow-sidecar/        # 构建前放入 sidecar onedir（见下；已被 .gitignore 忽略，不入库）
    icons/                  # 见 icons/README.md（构建前需 tauri icon 生成）
```

## 前提

1. **Rust 工具链**：本机已通过 rustup 装在 `~/.cargo/bin`（stable-aarch64-apple-darwin，
   rustc 1.91）。构建时把它加入 PATH 即可（`export PATH="$HOME/.cargo/bin:$PATH"`），无需再装。

2. **放入 sidecar onedir**：把 `scripts/build_sidecar.sh` 产出的 `dist/autoflow-sidecar/`
   整目录复制到 `tauri/src-tauri/autoflow-sidecar/`，使最终路径为
   `tauri/src-tauri/autoflow-sidecar/autoflow-sidecar`（可执行文件）。
   Tauri 的 `bundle.resources` 相对 `src-tauri` 解析并保留目录结构，故必须放在
   `src-tauri/autoflow-sidecar/`（**不要**放进 `src-tauri/resources/`，否则会嵌套成
   `Contents/Resources/resources/...`，与 `lib.rs` 的 `resource_dir().join("autoflow-sidecar")` 错位）。

   ```bash
   ./scripts/build_sidecar.sh
   # 目标已存在时 `cp -R src dst` 会拷成 dst/src（嵌套错位），所以先挪开旧的。
   # 用 mv 而不是 rm -rf：本环境下批量删除会被安全策略拦下。
   [ -e src-tauri/autoflow-sidecar ] && \
     mv src-tauri/autoflow-sidecar "src-tauri/.autoflow-sidecar.old.$(date +%Y%m%d%H%M%S)"
   cp -R ../dist/autoflow-sidecar src-tauri/autoflow-sidecar
   ```

   校验落点（可执行文件与自带模型都要在）：

   ```bash
   ls -l src-tauri/autoflow-sidecar/autoflow-sidecar
   ls -l src-tauri/autoflow-sidecar/_internal/models/yolo11n.onnx
   ```

   旧目录名 `.autoflow-sidecar.old.<时间戳>` 已被 `.gitignore` 的
   `.autoflow-sidecar.old.*/` 覆盖，且**不会**被
   `bundle.resources = ["autoflow-sidecar/**/*"]` 匹配到——留在原地不影响打包，
   确认新产物可用后再自行清理（约 230 MB）。

3. **生成图标**（见 `src-tauri/icons/README.md`）：`npm run tauri icon <1024.png>`。
   （仓库已带 `icon-source.png` 可作源；首次需生成 `icons/` 各尺寸。）

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
