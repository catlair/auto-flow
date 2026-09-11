# Auto Flow — macOS 工作流自动化

Python + PySide6 实现的本地键鼠录制 / 回放 / 工作流工具（macOS 优先）。
架构参考 [LCA](https://github.com/wuzhijing88/LCA)（Windows 版），砍掉 Windows 专属能力后的精简核心版。

## 功能

- **工作流节点**：录制回放 / 图像匹配点击 / 找文字点击(OCR) / YOLO找目标点击 / 条件判断 / 鼠标操作 / 键盘输入 / 延时 / 注释，节点可排序、启停
- **OCR**：macOS Vision 框架离线识别（支持中英文），按文字找位置点击，或作为条件判断的「文字存在」检测
- **YOLO 目标检测**：onnxruntime + CoreML 加速，跑标准 YOLO ONNX 模型（自带 `models/yolo11n.onnx`，COCO 80 类，可放同名 .txt 换自定义类别）；支持指定类别、置信度、命中序号，也可作为条件判断
- **录制回放**：全局监听键鼠 → 轨迹插值回放（10px 步长平滑移动，不瞬移）
- **图像匹配**：截屏找图 → 点击/双击/移动到目标，可设置信度、超时重试、找不到时跳过或停止；参数面板里可直接「截取模板」框选屏幕取图（Retina 坐标自动换算）
- **相对坐标**：以录制时首个鼠标位置为原点，回放时设定新基点，整条轨迹平移
- **循环与调速**：整体循环次数、全局速度倍率，录制回放节点还可单独设速度/重复
- **全局热键**：F9 录制开关 / F10 运行与停止 / F11 取基点
- **定时运行**：菜单「工具→定时运行」，支持每天固定时刻或固定间隔自动运行指定工作流
- **脚本管理**：工作流保存为 JSON（兼容 Tauri 版 macro-recorder 的裸事件脚本，自动导入为录制回放节点）

## 运行

```bash
./run.sh          # 首次会自动创建 venv 并按 requirements.txt 安装依赖
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
core/
  events.py      # MacroEvent / Node / Workflow 数据模型与 JSON 序列化
  recorder.py    # pynput 监听录制（移动阈值过滤、原点记录、10 万事件上限自停）
  player.py      # 插值回放引擎（速度倍率、相对偏移、停止标志）
  executor.py    # 工作流执行器（整体循环、单节点重复、热停）
  vision.py      # mss 截屏 + OpenCV 模板匹配（Retina 坐标换算）
  permissions.py # 辅助功能权限检测与系统设置跳转
  keymap.py      # 键名映射
tasks/
  base.py        # 节点基类 + 注册表（节点自描述参数，UI 通用渲染）
  builtin.py     # 内置节点：mouse / keyboard / delay / record_replay / note
ui/
  main_window.py # 主窗口（节点列表 / 参数面板 / 运行控制 / 录制面板 / 热键）
  params_panel.py# 依据节点定义通用渲染的参数表单
workflows/       # 工作流 JSON 存放处
```

## 开发

```bash
./.venv/bin/python -m pytest tests/ -q   # Python 测试（49 例，已隔离键鼠/配置副作用）
npm --prefix tauri test                  # 前端测试（node --test，20 例：store + RPC 客户端）
npm --prefix tauri run build             # vue-tsc 类型检查 + 前端构建
QT_QPA_PLATFORM=offscreen ./.venv/bin/python main.py  # 无界面冒烟
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

## 打包

```bash
./scripts/build_app.sh          # 产出 dist/Auto Flow.app（MacDev 自签，授权跨构建保持）
./scripts/build_app.sh --dmg    # 额外产出 dist/Auto Flow.dmg
```

> ⚠️ `scripts/build_app.sh` 默认 `--osx-bundle-identifier com.example.autoflow`，发布前请改为你自己的 bundle id。

## 后续规划

窗口绑定、多窗口调度、脚本市场/插件体系。
