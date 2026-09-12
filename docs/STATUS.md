# 模块状态总览

> 更新：2026-09-12 · 明细见各模块文档，本表只做全局速览

| 模块 | 文档 | 状态 | 验收 | 备注 |
| --- | --- | --- | --- | --- |
| 键鼠录制 | [recorder.md](modules/recorder.md) | ✅ | 111 测试 + 真机 | v3 重写：保轨采样 + 拖拽/双击语义 + 事件编辑 |
| 回放引擎 | [player.md](modules/player.md) | ✅ | 111 测试 + 真机 | v3 重写：虚拟时钟 + 三档追赶 |
| 工作流执行器 | [executor.md](modules/executor.md) | ✅ | 测试 | 异常兜底 + 条件门控 + 热停 |
| 节点体系 | [nodes.md](modules/nodes.md) | ✅ | 测试 + 真机 | 9 种内置节点，自描述参数 |
| 视觉（图像/OCR/YOLO） | [vision.md](modules/vision.md) | ✅ | 测试 + 真机 | Retina 坐标换算；锁屏时 Quartz 兜底 |
| 全局热键/取点 | [hotkeys.md](modules/hotkeys.md) | ✅ | 真机 | 取点用按键事件坐标（CLI 读不到光标） |
| 定时运行 | [schedule.md](modules/schedule.md) | ✅ | 测试 | 后端到点真跑，非仅通知 |
| macOS 权限 | [permissions.md](modules/permissions.md) | ✅ | 真机 | TCC 按二进制授权；F18 回环自检 |
| RPC 协议 | [rpc-protocol.md](modules/rpc-protocol.md) | ✅ | 91 测试 | 写锁防交错；广播 events 摘要化 |
| 前端界面 | [frontend.md](modules/frontend.md) | ✅ | 真机 | 响应驱动刷新；通知仅广播冗余 |
| 打包/签名/部署 | [packaging.md](modules/packaging.md) | ✅ | 真机 | ⚠️ 修复后必须重打包部署（曾致旧包跑 6 天） |
| 旧 PySide6 UI | — | ⛔ | — | 2026-09-12 删除（ui/、main.py、旧脚本、PySide6 依赖） |

## 近期计划（无主次排序）

- 📋 条件分支扩展：比较类条件（变量/上节点结果）而非仅「存在性检测」
- 📋 工作流变量系统：跨节点传值（计划书 §workflow_vars 曾有占位）
- 📋 Windows 支持：core 逻辑平台无关，输入层需按平台抽象（pynput 可跨）
- 📋 视觉模型按需分发：opencv/onnxruntime 占 sidecar 体积 200MB+，可拆可选包
- 📋 录制类型化文本：中文/emoji 目前录成按键序列，可按输入法组合结果聚合为文本事件
- 🧹 构建残留清理：`./scripts/clean_old_builds.sh`（预演）/ `--apply`（移入废纸篓）
