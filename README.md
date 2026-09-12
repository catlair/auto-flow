# Auto Flow — macOS 工作流自动化

本地键鼠录制 / 回放 / 工作流工具（macOS 优先）。
架构参考 [LCA](https://github.com/wuzhijing88/LCA)（Windows 版），砍掉 Windows 专属能力后的精简核心版。

> **界面：Tauri 版**（`tauri/`）——Vue 3 + Pinia + TDesign 前端 + Rust 外壳，
> 经 stdio（NDJSON JSON-RPC 2.0）调用 Python 后端 `rpc/`。
> 旧 PySide6 界面（`ui/`、`main.py`）已于 2026-09-12 删除，见 `docs/modules/frontend.md`。

## 功能

- **工作流节点**：录制回放 / 图像匹配点击 / 找文字点击(OCR) / YOLO找目标点击 / 条件判断 / 鼠标操作 / 键盘输入 / 延时 / 注释，节点可排序、启停
- **OCR**：macOS Vision 框架离线识别（支持中英文），按文字找位置点击，或作为条件判断的「文字存在」检测
- **YOLO 目标检测**：onnxruntime + CoreML 加速，跑标准 YOLO ONNX 模型（自带 `models/yolo11n.onnx`，COCO 80 类，可放同名 .txt 换自定义类别）；支持指定类别、置信度、命中序号，也可作为条件判断
- **录制回放**：全局监听键鼠 → 轨迹插值回放（10px 步长平滑移动，不瞬移）
- **图像匹配**：截屏找图 → 点击/双击/移动到目标，可设置信度、超时重试、找不到时跳过或停止；参数面板里可直接「截取模板」框选屏幕取图（Retina 坐标自动换算）
- **相对坐标**：以录制时首个鼠标位置为原点，回放时设定新基点，整条轨迹平移
- **多显示器**：图像匹配 / OCR / YOLO 遍历**所有**屏幕，坐标按所在屏换算成全局逻辑坐标；混合 Retina（一块 2x 一块 1x）也正确
- **循环与调速**：整体循环次数、全局速度倍率，录制回放节点还可单独设速度/重复
- **全局热键**：F9 录制开关 / F10 运行与停止 / F11 取基点
- **定时运行**：菜单「工具→定时运行」，支持每天固定时刻或固定间隔自动运行指定工作流
- **脚本管理**：工作流保存为 JSON（兼容 Tauri 版 macro-recorder 的裸事件脚本，自动导入为录制回放节点）

## 运行

**Tauri 版（现行）**：

```bash
npm --prefix tauri run tauri dev   # 开发；跑的是已打包 sidecar，改 Python 后需先重新打包
./scripts/build_sidecar.sh         # 改过 rpc/ 或 core/ 后重新打包 Python 后端
```

依赖清单见 `requirements.txt`（运行）与 `requirements-dev.txt`（测试 / 打包）。
手动装：

```bash
pip install -r requirements.txt -r requirements-dev.txt
```

## macOS 权限（首次必读）

程序需要两类系统权限，缺一不可：

| 权限 | 用途 | 设置入口 |
| --- | --- | --- |
| 辅助功能 | 模拟鼠标键盘（回放） | 系统设置 → 隐私与安全性 → 辅助功能 |
| 输入监控 | 全局监听键鼠（录制） | 系统设置 → 隐私与安全性 → 输入监控 |

以脚本方式运行时，需要给 **终端 App**（或运行它的 IDE，如 VS Code）授权，
列表里勾选的是承载 Python 进程的那个应用。未授权时启动会弹引导，不会静默失败。

## 项目结构

```
core/            # 后端核心（无界面依赖）
  events.py      # MacroEvent / Node / Workflow 数据模型与 JSON 序列化
  recorder.py    # 录制：pynput 鼠标 + 自建 CGEventTap 键盘（阈值过滤/统计）
  player.py      # 插值回放引擎（速度倍率、相对偏移、热停安全）
  executor.py    # 工作流执行器（整体循环、单节点重复、条件门控、热停）
  maclistener.py # 自建只读 CGEventTap 键盘监听（macOS 15 兼容）
  mackeys.py     # 虚拟键码表 + 修饰键边沿
  vision.py      # mss/Quartz 截屏 + OpenCV 模板匹配（Retina 换算）
  ocr.py         # Vision 框架离线 OCR
  yolo.py        # onnxruntime/CoreML 目标检测
  permissions.py # 权限检测 / 设置跳转
  keymap.py paths.py
tasks/           # 节点插件
  base.py        # 节点基类 + 注册表（自描述参数，UI 动态渲染）
  builtin.py     # 九种内置节点
rpc/             # 后端协议层
  server.py      # NDJSON JSON-RPC 2.0 帧解析/分发/通知队列（写锁）
  controller.py  # AppController：工作流真源、运行/录制/热键/定时
tauri/           # 前端 + Rust 外壳
  src/           # Vue 3：rpc 客户端 + Pinia store + 8 组件
  src-tauri/     # sidecar 管理、事件转发、守护重启
models/          # yolo11n.onnx
workflows/       # 工作流 JSON
docs/            # 功能模块文档系统（见 docs/README.md）
```

## 功能模块文档

功能的设计、完成情况与验收记录统一存放在 `docs/`：

- `docs/STATUS.md` — 全模块状态总览
- `docs/modules/<模块>.md` — 各模块的功能清单 / 验收记录 / 设计要点
- 新增或修改功能时**同步更新对应模块文档**（规则见 `docs/README.md`）

## 开发

```bash
./.venv/bin/python -m pytest tests/ -q   # Python 测试（75 例，已隔离键鼠/屏幕/配置副作用）
npm --prefix tauri test                  # 前端测试（node --test，20 例：store + RPC 客户端）
npm --prefix tauri run build             # vue-tsc 类型检查 + 前端构建
QT_QPA_PLATFORM=offscreen ./.venv/bin/python main.py  # 旧 Qt 入口无界面冒烟（迁移期保留）
```

> 测试会通过 `AUTOFLOW_DATA_DIR` 指向临时目录，并在进程内替换真实键鼠控制器，
> 因此不会移动光标，也不会读写你仓库根目录下的 `config.json`。

## YOLO 模型

自带 `models/yolo11n.onnx`。换模型：用 [ultralytics](https://docs.ultralytics.com/modes/export/) 导出

```bash
pip install ultralytics
yolo export model=yolov8n.pt format=onnx imgsz=640   # 或 yolo11n.pt / 自己训练的 .pt
```

把 `.onnx` 放进 `models/`，节点参数里选它即可；自定义类别在模型旁放同名 `.txt`（每行一个类别名）。

## 打包（Tauri 版）

```bash
./scripts/build_sidecar.sh            # 1) 打包 Python 后端 onedir（含 models/）
npm --prefix tauri run tauri build    # 2) 打包 .app / .dmg
./scripts/sign_tauri_app.sh           # 3) MacDev 双签 + 强化运行时（先 sidecar 再外壳）
./scripts/sync_app.sh                 # 4) 签名自检后装到 /Applications 并打开
```

> 授权持久性的前提是「sidecar 固定路径 + MacDev 自签」，详见
> `docs/迁移计划书-python后端+tauri前端.md` §12 与 `docs/权限引导.md`。

旧 Qt 版打包脚本 `scripts/build_app.sh`（产出 `dist/Auto Flow.app`）仍在，随 `ui/` 一并迁移期保留。

## 后续规划

窗口绑定、多窗口调度、脚本市场/插件体系。
