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

## 传输要点

- 帧 = 单行 compact JSON + `\n`；sidecar 的 `sys.stdout` 被重定向到 stderr，
  协议只走 `os.fdopen(1, "wb", buffering=0)`。
- **全局写锁**：响应（stdin 读线程直写）与通知（writer 线程）并发写同一 fd，
  无锁时大帧字节交错 → 前端随机解析失败（「丢帧」错觉的真凶，2026-09-12 修）。
- 通知队列 maxsize=1024，满时丢最旧；后端进程退出前同步排空。
- Rust 壳逐行 `rpc_event` 转发（补 `\n`）；EOF 后清 stdin 句柄并发 rpc_down，
  2s 后守护重启。

## 方法清单（请求/响应）

| 方法 | 参数（摘要） | 返回 |
| --- | --- | --- |
| app.info | — | 版本/协议/平台/权限 |
| app.diagnose | — | 数据目录/权限/日志尾 |
| app.openPermissionSettings | panel | — |
| app.requestPermissions | — | 权限快照（弹系统提示） |
| workflow.current / new / load / save / update | — | workflow_current（统一真源视图） |
| node.add / remove / move / toggle / rename | index… | workflow_current（add 附 index/node） |
| node.params.set | index,key,value | workflow_current |
| nodes.definitions | — | 节点定义（含 common_params，驱动动态表单） |
| run.start / run.stop | base_x/base_y | {running}；stop 已 join 运行线程 |
| record.start / stop / subscribe / toNode | — | stop 返回统计（count/captured/filtered/limit_dropped/监听器存活） |
| template.snip | — | {started}，完成走通知 |
| input.probe | — | {alive}（F18 回环） |
| hotkey.set / clear | actions | 绑定快照 |
| key.capture / key.capture.stop | — | {capturing} |
| base.pick | — | {armed}，结果走通知 |
| schedule.configure / get | cfg | {schedule, nextFire} |
| app.shutdown | — | 退出 |

## 通知清单（后端 → 前端）

| 通知 | 载荷 | 说明 |
| --- | --- | --- |
| workflow.changed | 公共视图工作流 | **events 摘要为 {count}**（真源在后端） |
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

## 验收记录

- **传输洁净性**：`test_stdout_is_clean_json_only`（整生命周期 stdout 全为合法 JSON 行）
- **错误码**：`test_unknown_method_and_parse_error`、`test_run_and_record_error_codes`
- **录制→写回节点**：`test_record_to_node_roundtrip`（真实起停 Recorder）
- **并发写**：91 例全绿 + 真机长录（数百事件）无解析失败（2026-09-12 写锁后）

## 设计要点

1. **响应驱动 UI**：通知会丢（Tauri 事件转发链路），凡影响 UI 状态的操作
   （结构变更/录制停止/运行停止）一律以响应收敛，通知只作广播冗余。
2. **events 摘要**：广播里 `params.events` → `{count}`；真源在后端，
   保存/回放不经过前端。（首版摘要曾共享引用摧毁真源，见 controller._public_node 注释）
3. **错误归一**：ControllerError 带业务码（-32001…-32004 等）；前端 errMessage
   兼容 string/Error/对象（Tauri invoke 的 Err 是裸字符串）。

## 已知问题

- 通知经 Rust → WebView 转发偶发丢失（未定位到帧级原因，已用响应驱动策略免疫）。
- 大工作流（10 万事件）的 workflow.current 响应仍较大（首次加载一次，可接受）。

## 变更记录

- 2026-09-12 写锁、events 摘要、node.rename、template.snip、input.probe
