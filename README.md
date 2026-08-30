# Auto Flow — macOS 工作流自动化

Python + PySide6 实现的本地键鼠录制 / 回放 / 工作流工具（macOS 优先）。
架构参考 [LCA](https://github.com/wuzhijing88/LCA)（Windows 版），砍掉 Windows 专属能力后的精简核心版。

## 功能

- **工作流节点**：录制回放 / 鼠标操作 / 键盘输入 / 延时 / 注释，节点可排序、启停
- **录制回放**：全局监听键鼠 → 轨迹插值回放（10px 步长平滑移动，不瞬移）
- **相对坐标**：以录制时首个鼠标位置为原点，回放时设定新基点，整条轨迹平移
- **循环与调速**：整体循环次数、全局速度倍率，录制回放节点还可单独设速度/重复
- **全局热键**：F9 录制开关 / F10 运行与停止 / F11 取基点
- **脚本管理**：工作流保存为 JSON（兼容 Tauri 版 macro-recorder 的裸事件脚本，自动导入为录制回放节点）

## 运行

```bash
./run.sh          # 首次会自动创建 venv 并安装依赖
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
  recorder.py    # pynput 监听录制（移动阈值过滤、原点记录、2 万事件上限自停）
  player.py      # 插值回放引擎（速度倍率、相对偏移、停止标志）
  executor.py    # 工作流执行器（整体循环、单节点重复、热停）
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

## 后续规划

图像匹配点击、OCR、条件分支、窗口绑定、打包为独立 .app（pyinstaller）。
