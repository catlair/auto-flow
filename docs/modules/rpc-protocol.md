# RPC 协议

> 状态：✅
> 一句话：Tauri 壳 ↔ Python sidecar 的 stdio NDJSON JSON-RPC 2.0，
> 全部方法与通知的权威清单。

## 代码位置

- `rpc/server.py` — 帧解析/分发/通知队列（**协议帧走原始 fd 1 + 全局写锁**，
  日志一律 stderr）
- `rpc/controller.py` — AppController（工作流唯一真源 + 全部业务方法）
- `tauri/src/rpc/client.ts` / `types.ts` — 前端客户端
- `tests/test_rpc.py` — 真实子进程端到端协议测试

## 功能清单

| 编号 | 功能 | 状态 | 说明 |
| --- | --- | --- | --- |
| F-RPC-01 | 帧格式与协议通道 | ✅ | 单行 compact JSON + `\n`；协议走 `os.dup(1)` 的独立 fd，fd 1 让给 stderr |
| F-RPC-02 | 全局写锁 | ✅ | 响应与通知并发写同一 fd 不交错（「丢帧」错觉的真凶，2026-09-12 修） |
| F-RPC-03 | 通知队列背压 | ✅ | maxsize=1024，满时只丢可丢类；退出前同步排空 |
| F-RPC-04 | 请求/响应方法集 | ✅ | 见下方「方法清单」 |
| F-RPC-05 | 通知广播 | ✅ | 见下方「通知清单」 |
| F-RPC-06 | 响应驱动 UI 收敛 | ✅ | 状态类操作一律以响应为准，通知只作广播冗余 |
| F-RPC-07 | 错误归一 | ✅ | ControllerError 业务码（-32001…）；前端 errMessage 兼容三种形态 |
| F-RPC-08 | 后端告警转发 | ✅ | WARNING+ → `log.warning` 通知 → 诊断面板 |
| F-RPC-09 | 丢帧判据后端计算 | ✅ | `record.stop.unaccounted`，前端只判 `!= 0` |
| F-RPC-10 | 画布方法按 uid 寻址 | ✅ | `node.setPos` / `edge.add` / `edge.remove` / `workflow.setStart` |
| F-RPC-11 | 结构变更连带清理 | ✅ | `node.remove` 一并清掉连着它的边与指向它的 `start` |

### 方法清单（请求/响应）

| 方法 | 参数（摘要） | 返回 |
| --- | --- | --- |
| app.info | — | 版本/协议/平台/权限 |
| app.diagnose | — | 数据目录/权限/日志尾 |
| app.openPermissionSettings | panel | — |
| app.requestPermissions | — | 权限快照（弹系统提示） |
| workflow.current / new / load / save / update | — | workflow_current（统一真源视图） |
| workflow.setStart | uid | workflow_current；`uid=""` 回到「第一个 start 节点 / 第一个节点」 |
| node.add | type, **x, y** | workflow_current + index/node（不给坐标则落在视口外错开处） |
| node.remove / toggle / rename / move | index… | workflow_current；**remove 同时清掉连着它的边与指向它的 start** |
| node.params.set | index,key,value | workflow_current |
| **node.setPos** | uid,x,y | workflow_current（**只在拖拽结束时调一次**，见设计要点 6） |
| **edge.add** | src,dst,port | workflow_current；同 `(src,port)` **替换**旧边，`src==dst` 报错 |
| **edge.remove** | src,port | workflow_current |
| nodes.definitions | — | 节点定义（`common_params` 现为空数组；ParamDef 含 `show_if`/`pick`） |
| run.start / run.stop | base_x/base_y | {running}；stop 已 join 运行线程 |
| record.start / stop / subscribe / toNode | — | stop 返回统计（count/captured/filtered/limit_dropped/text_merged/trimmed/**unaccounted**/监听器存活） |
| record.current / remove / removeMovesBefore / setOrigin / setText / keysToText / undo | index,text | 编辑权威事件序列，全部可 `record.undo` 回滚；`keysToText` 把选中处的**最大连续按键段**换成一条 text 事件 |
| template.snip | — | {started}，完成走通知 |
| input.probe | — | {alive, text_alive, input_source{id,name,event_channel_unsupported}}（F18 + 零宽空格两段回环） |
| hotkey.set / clear | actions | 绑定快照 |
| key.capture / key.capture.stop | — | {capturing} |
| base.pick | — | {armed}，结果走通知 |
| schedule.configure / get | cfg | {schedule, nextFire} |
| app.shutdown | — | 退出 |

### 通知清单（后端 → 前端）

| 通知 | 载荷 | 说明 |
| --- | --- | --- |
| workflow.changed | 公共视图工作流 | **events 摘要为 {count}**（真源在后端）；含 `edges` / `start` / `migrated_from_list` |
| record.event | {events:[…]}（100ms 批） | 仅订阅时推送 |
| record.stopped | 统计 | 响应携带同数据（响应为准） |
| run.node / run.progress / run.finished / run.error | — | 运行生命周期 |
| hotkey.triggered | {action, pick 带 x,y} | 输入框聚焦时前端屏蔽 |
| key.captured | {name,x,y} | 按键捕获回填 |
| base.picked | {x,y} | 取点 armed 后的回填 |
| template.snipped | {path,ok} | 截取模板完成 |
| permission.changed | 三项布尔 | 2s 轮询变化才发 |
| permission.probe | {inputAlive} | 自检结果 |
| schedule.fired | {path,ran,reason?} | 定时触发 |
| log.warning | {level, logger, message} | **后端 WARNING+ 转发给界面**（见设计要点 4） |

## 验收记录

- **传输洁净性**：`test_stdout_is_clean_json_only`（整生命周期 stdout 全为合法 JSON 行）
- **错误码**：`test_unknown_method_and_parse_error`、`test_run_and_record_error_codes`
- **录制→写回节点**：`test_record_to_node_roundtrip`（真实起停 Recorder）
- **并发写**：91 例全绿 + 真机长录（数百事件）无解析失败（2026-09-12 写锁后）
- **F-RPC-10/11**（2026-09-13）：`test_flowchart_edges_and_positions`
  （边增删、坐标、起点、删节点连带清边）；前端 `npm --prefix tauri test` 的
  `连线：同一出口只留一条，替换时回报给调用方`、`设置与恢复起始节点`、
  `节点坐标只在拖拽结束时提交一次，后端负责取整`
- **F-RPC-10 迁移可见性**（2026-09-13）：`test_workflow_load_reports_list_migration`
  （`workflow.load` 的响应里 `migrated_from_list` 为真，且不落盘）

## 设计要点

### 传输要点

- 帧 = 单行 compact JSON + `\n`；**协议不走 fd 1**——`_init_output()` 先 `os.dup(1)`
  拿到独立 fd 作协议通道，再把 fd 1 `dup2` 到 stderr。只重定向 `sys.stdout`
  拦不住 C 层写入（opencv/onnxruntime 告警、printf），字节会混进 NDJSON 帧流。
- **全局写锁**：响应（stdin 读线程直写）与通知（writer 线程）并发写同一 fd，
  无锁时大帧字节交错 → 前端随机解析失败（「丢帧」错觉的真凶，2026-09-12 修）。
- 通知队列 maxsize=1024；满时**只丢 `_DROPPABLE_NOTIFICATIONS` 里的可丢类**
  （`record.event` / `run.progress` / `log.warning`），其余是状态跃迁、丢一条
  前端可能永久停在旧状态，必须送达（挤掉最旧一条并 `logger.warning` 留痕）。
  后端进程退出前同步排空。
- Rust 壳逐行 `rpc_event` 转发（补 `\n`）；EOF 后清 stdin 句柄并发 rpc_down，
  2s 后守护重启。

### 设计决策

1. **响应驱动 UI**：通知会丢（Tauri 事件转发链路），凡影响 UI 状态的操作
   （结构变更/录制停止/运行停止）一律以响应收敛，通知只作广播冗余。
2. **events 摘要**：广播里 `params.events` → `{count}`；真源在后端，
   保存/回放不经过前端。（首版摘要曾共享引用摧毁真源，见 controller._public_node 注释）
3. **错误归一**：ControllerError 带业务码（-32001…-32004 等）；前端 errMessage
   兼容 string/Error/对象（Tauri invoke 的 Err 是裸字符串）。
4. **后端告警要能被界面看见**：`core.vision` 等模块的 WARNING/INFO 原本只进
   `~/Library/Logs/autoflow-tauri.log`（那份日志是逐帧 RPC 收发记录，5396 行里
   **一条诊断字符串都没有**），诊断面板看不到 → 用户只能自己翻日志。
   现由 `_UiLogHandler` 挂在 root logger（级别 WARNING）转发成 `log.warning` 通知，
   前端存进 `backendNotes`（最近 20 条）并在诊断面板成块显示。
   两个必须的实现细节：
   - **防递归**：`_send_notification` 在队列满时会自己 `logger.warning`，那条记录
     又会回到本 handler → 无限放大。用线程本地标记（`_local.busy`）掐断。
   - **`log.warning` 属可丢类**：它是诊断信息、不是状态跃迁，丢一条不会让界面
     永久停在旧状态，因此加进 `_DROPPABLE_NOTIFICATIONS`。否则一次刷屏告警会把
     `run.finished` 这类状态通知挤掉。
5. **丢帧判据只在后端算**：`record.stop` 返回的 `unaccounted` =
   `captured − (count + filtered + limit_dropped + text_merged + trimmed)`，
   前端只判 `!= 0`。**等式绝不在前端重拼**——每新增一类「有意移除」就要多扣一项，
   前端重拼必然漏扣：`trimmed`（v3 重写时丢失）与 `text_merged`（新增文本聚合时）
   各制造过一次「检测到系统层丢事件，请反馈」的假警报。判据收敛到一处，
   新增丢弃类别时只改 `record_stop` 一处。
   （回归测试：`test_record_stop_unaccounted_is_zero_after_trim`）
6. **画布方法按 uid 寻址，其余 node.* 仍按下标**。这是刻意的分工：
   边、坐标、起点在语义上就是「某个具体节点」，而 `node.*` 的其余方法
   （改名/启停/改参数）跟着「当前选中项」走，前端本来就用下标寻址。
   风险在于下标会随增删漂移，所以 `node.remove` 会**同时清掉连着它的边**——
   不清的话用户看到画布上悬空的连线（视觉上像还在连），而执行器走到那儿会因为
   找不到节点直接断掉整条路径，两种表现对不上，是最难查的那类不一致。
   前端 `store.removeNode` 另外把「选中」按 uid 跟随，避免下标前移导致
   参数面板悄悄显示另一个节点的参数（改一次就改错了对象）。
7. **`node.setPos` 只能按「拖拽结束」调，不能逐帧调**。逐帧上报会把整份工作流
   广播几十次，`workflow.changed` 刷满 1024 格的通知队列，把 `run.finished`
   这类状态通知挤掉——丢一条界面就可能永久停在旧状态。
   方法本身不做频率限制（后端无法区分「用户拖了一次」与「脚本调了两次」），
   约束写在前端。
8. **`edge.add` 替换而不是报错**。同一个 `(源节点, 出口)` 只保留一条边。
   再连一条时替换旧的并正常返回。响应里**没有**「发生了替换」这个字段——
   前端在**发请求之前**查一遍当前边集就知道（`store.addEdge` 的返回值），
   因为真源就在它手里、再让后端回一个冗余标志只会多一个可能对不上的真源。
   提示是必要的：不说一声的话旧连线无声消失，用户会以为自己连了两条。
   报错也不行——「换个目标」是正常操作，不该逼用户走「先删后连」两步。

## 已知问题

- 通知经 Rust → WebView 转发偶发丢失（未定位到帧级原因，已用响应驱动策略免疫）。
- 大工作流（10 万事件）的 workflow.current 响应仍较大（首次加载一次，可接受）。

## 变更记录

- 2026-09-12 写锁、events 摘要、node.rename、template.snip、input.probe
- 2026-09-12 `record.stop` 增加 `unaccounted`，一致性等式从前端收回后端（假警报修复）
- 2026-09-12 `input.probe` 增加 `input_source`——`text_alive=true` 不等于"中文能录"，
  必须按当前输入法给结论（见 recorder.md 已知问题）
- 2026-09-12 新增 `record.keysToText`：把一段连续按键换成一条 text 事件，让
  IME 中文（事件层只录到拼音）能走 Unicode 通道确定性回放；同时补齐
  `record.current/remove/removeMovesBefore/setOrigin/setText/undo` 的接口记录
- 2026-09-13 新增 `log.warning` 通知：后端 WARNING+ 日志转发给界面（诊断面板可见），
  `_UiLogHandler` 带线程本地防递归守卫；`log.warning` 列入可丢类
- 2026-09-13 修正本文件两处与实际实现不符的描述：协议通道是 `os.dup(1)` 而非
  `fdopen(1)`；队列满时只丢可丢类而非无差别丢最旧
- 2026-09-13 文档结构对齐 `_template.md`：补 `## 功能清单`（F-RPC-01…09），
  原「传输要点 / 方法清单 / 通知清单」三个独立章节降为 `###` 子节
  （分别归入「设计要点」与「功能清单」）；修正通知表里 `log.warning` 的设计要点
  交叉引用（原写「见设计要点 5」，实际是 4）
- 2026-09-13 **v4 画布协议**：新增 `workflow.setStart` / `node.setPos` /
  `edge.add` / `edge.remove`（F-RPC-10）；`node.add` 接受 `x,y`；
  `node.remove` 连带清边与 start（F-RPC-11）；工作流视图增加
  `edges` / `start` / `migrated_from_list`，节点视图增加 `x` / `y`；
  `nodes.definitions` 的 `common_params` 变为空数组（`run_when` 退场），
  ParamDef 新增 `show_if` / `pick`
