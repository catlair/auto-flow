# 节点体系

> 状态：✅
> 一句话：十三种内置节点，自描述参数定义驱动 UI 动态渲染，新增节点零 UI 改动；其中 5 种（start / condition / condition_group / branch / end）承载流程图的出口。

## 代码位置

- `tasks/base.py` — BaseTask / ParamDef（含 `show_if` / `pick`）/ 注册表（register / all_definitions）
- `tasks/builtin.py` — 十三种内置节点实现 + `tolerant_event` + `_detect`（条件与分支共用的检测）+ `click_point_in_box`（点击节点的框内取点）
- `rpc/controller.py` — nodes_definitions()（含 COMMON_PARAMS 与菜单顺序）
- `tests/test_core.py` — `BUILTIN_MENU_ORDER`（菜单顺序的第二道锁，见设计要点 6）
- 出口名与遍历语义见 [flow.md](flow.md)

## 功能清单

| 编号 | 功能 | 状态 | 说明 |
| --- | --- | --- | --- |
| F-NOD-01 | 自描述参数定义 | ✅ | ParamDef(ptype/默认值/min/max) → 前端动态表单 |
| F-NOD-02 | 节点改名 | ✅ | Node.name 持久化，node.rename RPC，双击行内编辑 |
| F-NOD-03 | 节点 uid | ✅ | uuid4；边的两端也是 uid，不能用 type 或 index |
| F-NOD-04 | 条件显示参数 | ✅ | `ParamDef.show_if`，分支节点按 `case_count` 收起多余 case |
| F-NOD-05 | 参数旁挂文件选择 | ✅ | `ParamDef.pick`，给可编辑文本框加「选择」按钮 |
| F-NOD-06 | 注册表防漏 | ✅ | executor._run_node 内 import tasks.builtin 兜底 |
| F-NOD-07 | 画布坐标 | ✅ | `Node.x/y`；新增节点落在视口中心并错开 |
| F-NOD-08 | 通用参数已清空 | ✅ | `COMMON_PARAMS = []`——`run_when` 的语义搬到了边上 |
| F-NOD-09 | 点击位置（框内取点） | ✅ | `image_click` / `ocr_click` 命中后可选点中心 / 框内随机 / 框内自定义；默认中心 = 老行为 |

### 节点清单（内置节点一览）

`order` 列即添加菜单里的顺序，唯一真源是各节点的 `order`。

| order | 节点 | type | 状态 | 要点 |
| --- | --- | --- | --- | --- |
| 5 | 开始 | start | ✅ | 无参数；入口标记，不能有入边 |
| 10 | 鼠标操作 | mouse | ✅ | click/double_click/move/press/release，相对偏移 |
| 20 | 键盘输入 | keyboard | ✅ | text / 单键 / 组合键（keys 参数 ptype=`keys` 带捕获按钮） |
| 30 | 延时等待 | delay | ✅ | 两种等待方式：延时毫秒（`ptype=duration`，时/分/秒/毫秒四格）/ 等待至时刻（`ptype=time`，时刻选择器）；两者按 `show_if` 互斥显示；存下来仍是毫秒整数 + `HH:MM:SS` 字符串 |
| 40 | 录制回放 | record_replay | ✅ | 事件可**内嵌**（跟流程文件走）或**引用录制库**（只存 id，回放时现取）；速度/重复/相对坐标独立可调 |
| 50 | 图像匹配点击 | image_click | ✅ | mss 截屏 + cv2 模板匹配；命中后按 `click_pos` 在**匹配框内**取点（见设计要点 12） |
| 60 | 找文字点击 | ocr_click | ✅ | macOS Vision 离线 OCR，中英文；命中后按 `click_pos` 在**文字框内**取点，与 image_click 同一套实现 |
| 70 | YOLO 找目标点击 | yolo_click | ✅ | onnxruntime/CoreML，yolo11n.onnx（COCO 80 类）；**暂无 `click_pos`**，见「已知问题」 |
| 80 | 条件判断 | condition | ✅ | 图像存在 / 文字存在 / 目标存在(YOLO)；出口 `true` / `false` |
| 82 | 条件组 | condition_group | ✅ | 最多 6 项检测按「与 / 或」合成一个判断；出口沿用 `true` / `false`；求值**短路** |
| 85 | 多路分支 | branch | ✅ | 2~6 个情形，按顺序取第一个命中；出口 `case:N` / `else` |
| 90 | 注释 | note | ✅ | 不执行，仅说明；执行时**跳过并继续** |
| 95 | 结束 | end | ✅ | 无参数；终止当前路径，没有出口 |

## 验收记录

- **F-NOD-01**（2026-09-05，v4 后仍适用）：`test_all_nodes_have_definitions` + 真机添加各类节点参数面板动态渲染。
- **F-NOD-02**（2026-09-12）：RPC 探针 node.rename → 树更新 + JSON 落盘含 name；真机双击节点出现行内编辑框。
- **F-NOD-03**（2026-09-12）：同类型多节点拖拽不再错乱（item-key=uid）；
  v4 起边的两端也是 uid，见 `test_flowchart_edges_and_positions`。
- **F-NOD-04**（2026-09-13）：真机选中分支节点，`case_count` 调到 2 时只显示
  `case1_*` / `case2_*`；调到 4 时多出两组。参数默认值缺失时也正确显示（见设计要点 4）。
  同一条机制也用在 `delay` 上（2026-09-15）：「延时毫秒 / 等待至时刻」两个字段
  按 `mode` 互斥显示。
- **F-NOD-05**（2026-09-13）：真机在 `case1_value`（可编辑文本框）旁点「选择」，
  挑到的路径写回该字段——`file` 类型是只读的，装不下「也可能是文字」的取值。
- **F-NOD-06**（2026-09-13）：`test_all_nodes_have_definitions` +
  `test_node_menu_order_is_explicit`（顺序切片 + `len(defs) == n` 双重比对；
  反向验证：把 `BUILTIN_MENU_ORDER` 末尾的 `end` 删掉，切片断言照过、
  新加的数量断言失败并点名 `['end']`）。
- **F-NOD-08**（2026-09-13）：`test_rpc.py` 断言 `nodes.definitions` 的
  `common_params == []`——把「已清空」锁住，防止有人顺手加回一个全局参数。
- **F-NOD-09**（2026-09-16）：`tests/test_core.py` 14 例 + `tests/test_vision.py` 4 例
  （全量 232 passed）。覆盖：纯函数（默认中心 / 四角 / 随机覆盖整框 / 内缩 /
  内缩大到把框缩没要钳制 / 框退化与脏模式回落中心 / 百分比越界钳制）、
  `_click_target`（偏移最后叠加、`0` 是合法值、脏参数不抛错）、
  两个节点真跑一遍（monkeypatch `vision.find_template` / `ocr.find_text` 返回带框的
  结果，断言 `glide_now` 收到的就是框内点 + 偏移）、参数定义（默认值 / `show_if` /
  min-max）、检测层的框尺寸是**逻辑点**。
  - 反向验证 ①：把 `click_point_in_box` 改成恒返回中心 → **9 条失败**，
    而「默认中心」「回落中心」等该过的仍通过（证明失败的是「框内取点」这件事本身）。
  - 反向验证 ②：把 `vision` / `ocr` 里框尺寸的 `/ scale` 去掉 → 两条 2x 断言失败、
    1x 的对照断言仍通过（证明「除以 scale」是那两条的唯一变量）。
  - **真机验收**（2026-09-16，改了 Python ⇒ 走完整链路重打 sidecar 并部署到
    `/Applications/Auto Flow.app`）：`scripts/check_installed.py` **66 项全过**，
    其中本次新增 6 项——
    - 产物级 4 条（读 frozen PYZ）：`click_point_in_box` / `_click_target` 在包里、
      `ImageClickTask.run` 与 `OcrClickTask.run` 的 `co_names` 含 `_click_target`、
      三种「点击位置」取值字面量在包里。
    - RPC 级 2 条（真起 sidecar）：两个节点的 `nodes.definitions` 都暴露了
      「点击位置」（默认 `目标中心`、3 档、`rel_x`/`rel_y` 的 `show_if` 正确、
      `inset.min == 0`、老偏移未被挤掉）。
    - **区分度**：对**上一版** sidecar（`build_sidecar.sh` 每次重打前会把上一份改名成
      `dist/.autoflow-sidecar.old.<时间戳>`，本次是 `…20260916-203918`，即重打前部署的
      那份）跑同 6 条 → **全部 ✗**，且失败信息点名的就是缺失本身（RPC 两条报
      「默认 None，0 档」）。⇒ 这 6 条不是恒真断言。
      ⚠️ 那些 `.old.*` 会被 `clean_old_builds.sh --apply` 移入废纸篓，**要复现得自己
      先留一份上一版 sidecar**。
    - 产物级那两条 `run` 断言**刻意缩到方法自身的 `co_names`**：把源码在内存里改成
      「`run()` 还原成旧的偏移写法、`_click_target` 留成死代码」后，**整模块递归扫描
      仍然找得到（假绿）**，而 `run` 自身的 `co_names` 找不到（抓到）。所以别退回
      整模块扫描。
    - ⚠️ 这次重打**顺带把工作区里在途的录制库改动一起打进了安装版**（已与用户确认）。
      `check_installed.py` 里那 6 条录制库断言也一并转绿。
    - **目视验收（面板渲染）**：测试与 RPC 断言都覆盖不到渲染，所以在安装版上
      真机走了一遍（`open -a` 把窗口提到最前，纯鼠标点击驱动）：
      加一个「图像匹配点击」→ 面板里**确实出现「点击位置」下拉，默认 `目标中心`**，
      且此时 `横向/纵向位置(%)` **不显示**（`show_if` 的**隐藏**方向）；
      下拉里正好是 `目标中心 / 框内随机 / 框内自定义` 三档；
      改成 `框内自定义` 后 **`横向位置(%) 50` 与 `纵向位置(%) 50` 当场出现**
      （**显示**方向）。画布上该节点的摘要也跟着变成「图像匹配点击 / 框内自定义」。
      ⇒ `show_if` 的**两个方向**都验过了，不是只验了参数定义。
      ⚠️ 驱动界面的坑（面板会自己滚、细线伪影被当成文字行）见项目 skill
      `auto-flow-verify` 的「已安装版画布」一节。

## 设计要点

1. **自描述参数**（借鉴 LCA）：节点类声明 `ParamDef(key,label,ptype,default,…)`，
   `nodes.definitions` 下发，前端 ParamsPanel 按 ptype 渲染
   （int/float/bool/select/text/keys/file/events/duration/time/recording）。加节点不改 UI。
   `ptype` 是**跨文件协议**：后端声明、前端按字面量选控件。改名字不会报错，
   只会静默退化成「一个手打文本框」——`tauri/tests/duration.test.mjs` 里有一条
   读 `tasks/builtin.py` 的反向断言专门盯这个。
   `duration` / `time` 这两个是 2026-09-15 加的：`duration` 的值仍是**毫秒整数**，
   面板上摊成时/分/秒/毫秒四格；`time` 的值仍是 `HH:MM:SS` 字符串，面板上是
   只能选不能手打的选择器。**换算与清洗全在 `tauri/src/flow/duration.ts`**，
   面板只管渲染——`ParamsPanel.vue` 是 SFC，Node 用例跑不了它，纯逻辑必须外置。
   同一天加的 `recording`（第 11 种）的值是一个**录制库 id**，面板上是从库里
   选一条的下拉；选项拼装与「值不在库里时补兜底项」在 `tauri/src/flow/recording.ts`
   （见 [recording-lib.md](recording-lib.md)）。

2. **`COMMON_PARAMS` 现在是空的**（v4）。它原来放的是 `run_when`
   （总是 / 条件成立 / 条件不成立）——那是「有序列表 + 全局条件标志」时代的产物。
   语义搬到边之后，一个节点跑不跑由「有没有边走进来」决定，`run_when` 无处安放。
   **旧文件里残留的 `run_when` 参数刻意不清理**：清理掉的话用户打开旧文件会发现
   自己的配置凭空少了东西，而留着它至少能在参数面板上看到「这个字段还在，
   只是不再起作用」。前端不渲染它（定义里没有了），后端也不读它。

3. **`show_if` 是必需的，不是优化**。分支节点有 6 组 case 字段，全平铺出来是
   14 个字段的一堵墙，而用户通常只用 2~3 个。`show_if: {key:"case_count", gte:N}`
   让第 N 组只在 case 数够时才出现。

4. **`show_if` 求值必须回落到 ParamDef 的默认值**。节点刚添加时 `params` 里
   根本没有 `case_count` 这个 key，直接读 `node.params[key]` 会得到 `undefined`，
   `undefined >= 1` 为 false，于是**所有** case 字段都不显示——用户看到的是一个
   只剩「情形个数」的分支节点。`ParamsPanel.paramValue()` 因此先读节点、再读默认值
   （默认值正是后端实际会用的值）。

5. **`pick` 而不是把字段改成 `file`**。分支的取值既可能是模板图路径、
   也可能是一段要找的文字，取决于它上面的「检测方式」。`file` 类型是**只读**的
   （只能靠文件对话框写），装不下文字。所以给可编辑文本框加一个「选择」按钮：
   想点选就点选，想手打就手打。

6. **菜单顺序的两道锁，以及第二道原来有个洞**。唯一真源是各节点的 `order`
   （`rpc/controller.nodes_definitions` 直接沿用 `all_definitions()` 的顺序，
   不再自己排一次）。`tests/test_core.py::BUILTIN_MENU_ORDER` 是手写的期望顺序，
   供两个用例比对。
   原来的断言是 `defs[:len(BUILTIN_MENU_ORDER)] == BUILTIN_MENU_ORDER`——
   **新节点的 `order` 若比现有都大（追加到菜单末尾，最常见），这个切片仍然等于
   旧列表，测试照过**，新节点完全没被覆盖。2026-09-13 补上 `len(defs) == n`
   才把这个洞堵住（断言消息会直接点名漏掉的 type）。
   所以新增内置节点时要同时动两处：节点的 `order`，以及 `BUILTIN_MENU_ORDER`
   （放在它应有的位置，不是追加到末尾——放错位置切片断言会抓，放末尾现在也能抓）。
   ⚠️ 这条 `len(defs) == n` 断言**刚加上就抓到了另一类问题**：它只在**全量**
   运行时失败、单跑该用例永远通过。真因是 `tasks.base._REGISTRY` 是进程级全局，
   前面 `test_executor_*` 里注册的探针节点（`_test_slow` / `_test_bad`，
   order 都是默认的 100）会一直留到进程结束，`all_definitions()` 就多出两条。
   已在 `tests/conftest.py` 加 autouse 的 `restore_task_registry` 兜住
   （快照 → 还原，且快照前先 `import tasks.builtin`，否则可能快照到空注册表）。
   用例里注册探针节点不必再自己写清理。

7. **`_detect()` 由 condition 与 branch 共用**。两个节点的「怎么算命中」
   必须完全一致——否则同一种检测在条件节点里成立、在分支节点里不成立，
   用户完全无从理解。共用同一个函数是唯一的保证方式。

8. **`branch` 按顺序取第一个命中的 case，不做「挑最好的」**。
   顺序本身就表达优先级（用户把更具体的检测放前面），
   「挑置信度最高的」会让排列顺序失去意义，而用户排顺序时以为它有。

9. **events 摘要边界**：`record_replay.params.events` 数组在对外视图里替换为
   `{count}`（rpc/controller._public_node，浅拷贝视图不改真源）。前端从不读写
   事件数组；保存/回放都在后端完成。

10. **tolerant_event**：事件构造容忍缺/多字段；非 dict（损坏文件、摘要对象）返回
    None 跳过——防御 'str' has no 'get' 一类崩法。

11. **`record_replay` 的 `source` 是跨语言协议**（2026-09-15 加）：取值
    `内嵌`（默认，**缺字段也按这个算**）/ `录制库`。判成「录制库」会让老流程文件
    去查一个空 id、取不到就抛错——所有历史流程一次性全废。`录制库` 时
    `events` 是空数组、真正的事件按 `recording_id` 现取；**取不到必须抛**，
    不能静默回放空序列（"跑完了但什么都没做"比报错难查得多）。
    `origin_x/origin_y` 的 `show_if` 只对 `内嵌` 开放：用库里的录制时原点是
    **那条录制自己的属性**，节点再存一份就成了第二份真源（库里重设原点后
    节点不跟着变，回放位置会偏）。详见 [recording-lib.md](recording-lib.md)。

12. **「点击位置」是「在命中框内取点」，不是「限定查找区域」**（2026-09-16 加）。
    `image_click` / `ocr_click` 新增四个参数：`click_pos`
    （`目标中心` / `框内随机` / `框内自定义`）、`rel_x` / `rel_y`（自定义时的百分比，
    `show_if` 只在选「框内自定义」时出现）、`inset`（把框四边各往里收多少像素）。
    - **默认「目标中心」= 改动前的行为**，所以已保存的流程逐字不变——
      这是刻意的：老流程里点击点就是中心 + 偏移，改了默认值会让所有历史流程
      的点位一起漂。
    - **为什么需要它**：目标是一整块卡片、一整行菜单文字时，中心可能压在图标或
      不可交互处；而原来的「偏移X/Y」替代不了——框有多大取决于匹配到的模板尺寸或
      识别出的文字长度，偏移量**事先不知道**，用户只能试。有了框内取点，
      「点这行的最右边」就是 `rel_x = 100`，与框多大无关。
    - **取点实现只写一处**：`tasks/builtin.click_point_in_box`（纯函数，可注入
      `rand` 以便用例钉死随机），由 `_click_target` 供两个节点共用。同一个目标在
      「找图」与「找文字」上必须点到同一处，各写一份迟早会漂——与 `_detect` 被
      三个检测节点共用是同一个理由。
    - **拿不到框（`w<=0` / `h<=0`）或 `mode` 不认识 → 回落中心，不抛错**。
      检测层有些路径给不出尺寸（老数据、退化结果）。此时抛错会让本来能跑的流程
      失败，返回 `(0,0)` 则会点向屏幕左上角——两者都比「点中心」糟得多。
    - **跨度取「框宽 - 1」**，不是框宽：这样 100% 恰好落在框内最后一列，
      而不是越界一像素（越界一像素正是「点了但没点中」）。
    - **`inset` 要钳制**（`min(inset, (w-1)//2, (h-1)//2)`）：不钳的话内缩填大了
      `left` 会跑到框外很远，用户只看到「点歪了」，联想不到是内缩填太大。
      钳制后至少留 1 像素，所以「填太大」只会退化成点框心附近。
    - **固定偏移最后叠加**：`最终点 = 框内点 + offset_x/offset_y`。以前偏移直接
      加在中心上，所以这个顺序对老流程是**逐字等价**的。
    - **检测层要给出框的尺寸**：`vision.MatchResult.w/h` 与 `ocr.TextHit.w/h`
      （`ocr._box_size`）。单位必须是**逻辑点**（物理像素 / 该屏 `scale`），
      与 `x/y` 同一套换算——忘了除 scale 时坐标仍然对、只有框大一倍，
      「框内随机」的点就有一半落到目标外，而只断言坐标的用例抓不到。
      `ocr._box_size` 单独一个函数而不是让 `_center_from_box` 多返回两个值：
      后者已被用例直接断言成二元组。

## 已知问题

- 小键盘键（Keypad0…）可录制但 pynput 无对应键，回放时静默跳过。
- keyboard text 模式中文走 CGEvent Unicode 通道（2026-09-12 修复），长文本注入
  速率受系统限制。
- **把 `case_count` 调小不会清理超出范围的 `caseN_*` 参数**，它们只是被
  `show_if` 藏起来（与边同理：调回去还在）。文件里会留下看不见的字段。
- `condition_group` 把 `cond_count` 调小同样不清理超出范围的 `condN_*` 参数
  （与上一条同理：只是被 `show_if` 藏起来，文件里还在）。
  另外它**刻意没有**「至少 N 项成立」这类参数——那属于多路分支的范畴，
  两个节点各管一件事比在一个节点里堆参数好理解。
- ⚠️ **`yolo_click` 没有 `click_pos`**：同批只给 `image_click` / `ocr_click` 加了
  框内取点（用户当时点的就是这两个）。这是**已知的不一致**，用户会奇怪
  「为什么找图能框内取点、找目标不能」。补上的成本很小——
  `core/yolo.py::detect_bgr` 里 `Detection.w/h` **已经是逻辑点**
  （第 112 行 `int(pw / scale)`），所以 `tasks/builtin.py` 第 544 行
  `d.x + offset_x, d.y + offset_y` 换成 `_click_target(p, d.x, d.y, d.w, d.h)`
  + 补四个 ParamDef 即可，取点逻辑本身不用动。**没做，不是做不了。**
- `click_pos` 的取值字面量（`目标中心` / `框内随机` / `框内自定义`）**是存进文件的
  值**，改名等于让老文件的 select 显示为空、`show_if` 把字段藏掉
  （与 `delay` 的 `mode` 同理）。

## 变更记录

- 2026-09-05 图像/OCR/YOLO/条件节点逐个落地（见 git 历史）
- 2026-09-12 节点改名 + uid + keys ptype 修正
- 2026-09-13 文档结构对齐 `_template.md`：原独立的「节点清单」章节降为
  「功能清单」下的 `###` 子节（`docs/README.md` 要求所有模块文档一律用模板结构）
- 2026-09-13 **v4 节点体系**：新增 `start`(5) / `branch`(85) / `end`(95)；
  `condition` 改用出口名；`COMMON_PARAMS` 清空（`run_when` 退场）；
  ParamDef 新增 `show_if` / `pick`（F-NOD-04/05/07/08）
- 2026-09-15 **`delay` 增加「等待至时刻」**：`mode` 选「延时毫秒 / 等待至时刻」，
  后者接 `at`（`HH:MM` 或 `HH:MM:SS`，已过则等明天的同一时刻），两个字段按
  `show_if` 互斥显示。墙钟语义**不随速度倍率缩放**；等待按 1s 分段、每段用墙钟
  重算剩余（`player.wait` 走 monotonic，macOS 睡眠期间不走，一次睡到底会在
  系统睡醒后多等一整段）。新增 6 个用例
- 2026-09-15 **`delay` 的等待时间改成「选」而不是「填」**：`ms` 由 `int` 改
  `duration`（label 改「等待时长」，面板上是时/分/秒/毫秒四格），`at` 由 `text`
  改 `time`（时刻选择器，只能选不能手打）。**key 与存下来的格式一个都没动**
  （`ms` 仍是毫秒整数、`at` 仍是字符串），所以已保存的流程零迁移——
  老文件里的 `"09:00"` 由前端补成 `"09:00:00"` 再交给选择器（TDesign 拿 `format`
  校验 value，`"09:00"` 对 `HH:mm:ss` 是非法值、会被静默重置成空）。
  `mode` 的**取值**字面量（`"延时毫秒"`）刻意没改名：它已经写进用户文件，
  改了老文件的 select 会显示为空、`show_if` 也会把字段藏掉。
  换算逻辑抽成 `tauri/src/flow/duration.ts`（面板是 SFC，Node 用例跑不了）；
  新增 19 个前端用例 + 2 个摘要用例，Python 侧 1 个（见 frontend.md F-UI-17）
- 2026-09-15 **新增 `condition_group`（条件组，order 82）**：最多 6 项检测按
  `combine`（「全部成立(与) / 任一成立(或)」）合成一个判断，出口沿用
  `true` / `false`——与 `condition` 同形，所以**后端的边表不用为新节点做任何
  特殊处理**。求值短路（「与」遇假即停、「或」遇真即停），用户的排序因此获得
  「优先被检测」的含义。新增 9 个用例（`tests/test_core.py`）+ 1 个前端出口断言
- 2026-09-15 **`record_replay` 支持引用录制库**：新增 `source`（select，
  「内嵌 / 录制库」，默认内嵌）与 `recording_id`（`ptype=recording`，`show_if`
  只在选「录制库」时出现），`events`/`origin_x`/`origin_y` 反向只对「内嵌」显示。
  **老流程文件零迁移**：`source` 缺失按内嵌算，`events` 照旧在节点里。
  新增第 11 种 ptype `recording`。新增 22 个 Python 用例 + 14 个前端用例
  （`tests/test_recording_lib.py`、`tauri/tests/recording.test.mjs`）
- 2026-09-16 **`image_click` / `ocr_click` 支持「点击位置」框内取点**（F-NOD-09）：
  见设计要点 12。检测层为此带出框尺寸（`vision.MatchResult.w/h`、
  `ocr.TextHit.w/h` + `_box_size`），节点侧新增纯函数 `click_point_in_box` 与
  共用助手 `_click_target`。**默认值与老流程逐字一致**，零迁移。
  新增 14 个 Python 用例（`tests/test_core.py`）+ 4 个（`tests/test_vision.py`），
  全量 232 passed；两次反向验证（恒返回中心 → 9 红；去掉 `/ scale` → 2 红 1 绿）。
  真机验收：重打 sidecar → `tauri build` → 签名 → 部署，`check_installed.py`
  新增 6 条断言、66 项全过，并对上一版 sidecar 复跑确认 6 条全部 ✗（有区分度）。
  `yolo_click` **未加**该选项（见「已知问题」）
