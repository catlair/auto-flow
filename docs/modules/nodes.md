# 节点体系

> 状态：✅
> 一句话：九种内置节点，自描述参数定义驱动 UI 动态渲染，新增节点零 UI 改动。

## 代码位置

- `tasks/base.py` — BaseTask / ParamDef / 注册表（register / all_definitions）
- `tasks/builtin.py` — 九种内置节点实现 + tolerant_event
- `rpc/controller.py` — nodes_definitions()（含 COMMON_PARAMS 与菜单顺序）

## 节点清单

| 节点 | type | 状态 | 要点 |
| --- | --- | --- | --- |
| 鼠标操作 | mouse | ✅ | click/double_click/move/press/release，相对偏移 |
| 键盘输入 | keyboard | ✅ | text / 单键 / 组合键（keys 参数 ptype=`keys` 带捕获按钮） |
| 延时等待 | delay | ✅ | 毫秒；曾把 ms 当 s（回放慢 1000 倍，已修） |
| 录制回放 | record_replay | ✅ | 事件序列内嵌；速度/重复/相对坐标独立可调 |
| 图像匹配点击 | image_click | ✅ | mss 截屏 + cv2 模板匹配 |
| 找文字点击 | ocr_click | ✅ | macOS Vision 离线 OCR，中英文 |
| YOLO 找目标 | yolo_click | ✅ | onnxruntime/CoreML，yolo11n.onnx（COCO 80 类） |
| 条件判断 | condition | ✅ | 图像存在 / 文字存在 / 目标存在(YOLO) |
| 注释 | note | ✅ | 不执行，仅说明 |

## 功能清单

| 编号 | 功能 | 状态 | 说明 |
| --- | --- | --- | --- |
| F-NOD-01 | 自描述参数定义 | ✅ | ParamDef(ptype/默认值/min/max) → 前端动态表单 |
| F-NOD-02 | 节点改名 | ✅ | Node.name 持久化，node.rename RPC，双击行内编辑 |
| F-NOD-03 | 节点 uid | ✅ | uuid4，前端列表/拖拽 key 稳定（type 会撞） |
| F-NOD-04 | 通用参数 run_when | ✅ | 全节点尾部渲染，后端为真源 |
| F-NOD-05 | 注册表防漏 | ✅ | executor._run_node 内 import tasks.builtin 兜底 |

## 验收记录

- **F-NOD-01**（2026-09-05）：`test_all_nodes_have_definitions` + 真机添加各类节点
  参数面板动态渲染。
- **F-NOD-02**（2026-09-12）：RPC 探针 node.rename → 树更新 + JSON 落盘含 name；
  真机双击节点出现行内编辑框。
- **F-NOD-03**（2026-09-12）：同类型多节点拖拽不再错乱（item-key=uid）。

## 设计要点

1. **自描述参数**（借鉴 LCA）：节点类声明 `ParamDef(key,label,ptype,default,…)`，
   `nodes.definitions` 下发，前端 ParamsPanel 按 ptype 渲染
   （int/float/bool/select/text/keys/file/events）。加节点不改 UI。
2. **events 摘要边界**：`record_replay.params.events` 数组在对外视图里替换为
   `{count}`（rpc/controller._public_node，浅拷贝视图不改真源）。前端从不读写
   事件数组；保存/回放都在后端完成。
3. **tolerant_event**：事件构造容忍缺/多字段；非 dict（损坏文件、摘要对象）返回
   None 跳过——防御 'str' has no 'get' 一类崩法。

## 已知问题

- 小键盘键（Keypad0…）可录制但 pynput 无对应键，回放时静默跳过。
- keyboard text 模式中文走 CGEvent Unicode 通道（2026-09-12 修复），长文本注入
  速率受系统限制。

## 变更记录

- 2026-09-05 图像/OCR/YOLO/条件节点逐个落地（见 git 历史）
- 2026-09-12 节点改名 + uid + keys ptype 修正
