# 模块状态总览

> 更新：2026-09-14 · 明细见各模块文档，本表只做全局速览

| 模块 | 文档 | 状态 | 验收 | 备注 |
| --- | --- | --- | --- | --- |
| 键鼠录制 | [recorder.md](modules/recorder.md) | ✅ | 173 测试 + 真机 | v3 重写：保轨采样 + 拖拽/双击语义 + 事件编辑 + 文本聚合 |
| 回放引擎 | [player.md](modules/player.md) | ✅ | 173 测试 + 真机 | v3 重写：虚拟时钟 + 三档追赶 + 文本投递 |
| **流程图模型** | [flow.md](modules/flow.md) | ✅ | 173 测试 + 70 前端用例 | **v4：节点 + 有向边**；出口名分派、汇合、循环上限、旧文件迁移 |
| 工作流执行器 | [executor.md](modules/executor.md) | ✅ | 测试 | v4 图遍历（出口分派 + 步数上限）+ 异常兜底 + 热停 |
| 节点体系 | [nodes.md](modules/nodes.md) | ✅ | 测试 + 真机 | 12 种内置节点（+ start / branch / end），自描述参数 + show_if |
| 视觉（图像/OCR/YOLO） | [vision.md](modules/vision.md) | ✅ | 测试 + 真机 | 截屏取物理像素；模板密度自动对齐，改分辨率不必重截模板 |
| 全局热键/取点 | [hotkeys.md](modules/hotkeys.md) | ✅ | 真机 | 取点用按键事件坐标（CLI 读不到光标） |
| 定时运行 | [schedule.md](modules/schedule.md) | ✅ | 测试 | 后端到点真跑，非仅通知 |
| macOS 权限 | [permissions.md](modules/permissions.md) | ✅ | 真机 | TCC 按二进制授权；F18 回环自检 |
| RPC 协议 | [rpc-protocol.md](modules/rpc-protocol.md) | ✅ | 173 测试 | 写锁防交错；广播 events 摘要化；后端 WARNING 转发为 `log.warning` |
| 前端界面 | [frontend.md](modules/frontend.md) | ✅ | 真机 + 70 前端用例 | v4 画布（@vue-flow/core）；响应驱动刷新；诊断面板显示后端告警 |
| 打包/签名/部署 | [packaging.md](modules/packaging.md) | ✅ | 34 项产物/RPC 断言 | ⚠️ 修复后必须重打包部署；第 5 步 `check_installed.py` 收口 |
| 旧 PySide6 UI | — | ⛔ | — | 2026-09-12 删除（ui/、main.py、旧脚本、PySide6 依赖） |
| v3 有序列表模型 | — | ⛔ | — | 2026-09-13 被流程图取代（旧文件仍可打开并自动迁移，见 flow.md） |

## 近期计划（无主次排序）

- 📋 条件分支扩展：比较类条件（变量 / 上一节点结果）而非仅「存在性检测」
- 📋 回边静态提示：连成环时给出「确认这是循环而不是连错」的提醒
  （现在只在运行时撞 10000 步上限才报，见 flow.md 已知问题）
- 📋 画布编辑增强：框选、复制粘贴、对齐吸附、节点分组
- 📋 工作流变量系统：跨节点传值（计划书 §workflow_vars 曾有占位）
- 📋 Windows 支持：core 逻辑平台无关，输入层需按平台抽象（pynput 可跨）
- 📋 视觉模型按需分发：opencv/onnxruntime 占 sidecar 体积 200MB+，可拆可选包
- 📋 公证：需 `APPLE_ID / APPLE_APP_PASSWORD / APPLE_TEAM_ID`，脚本参数位已留
- 📋 录制侧直接拿汉字：唯一方向是 AX 轮询 `kAXValueAttribute` 差分，但终端/
  画布类应用不适用且不知道插入位置，暂不做（见 recorder.md 已知问题）

> 已实测定性（2026-09-12，见 recorder.md 已知问题）：系统简体拼音走 `insertText:`
> 通道，事件层录不到汉字、只有拼音按键；但**拼音按键回放会重新驱动输入法、
> 中文照样上屏**（往返成立但不确定）。要确定性回放用 `record.keysToText`
> 把按键段换成文本事件。
