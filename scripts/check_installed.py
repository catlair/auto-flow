#!/usr/bin/env python3
"""部署后校验：确认**已安装的** App 真的带上了本次改动。

四步流水线里最容易踩的坑是"改了代码却没变化"——只跑 `tauri build` 不跑
`build_sidecar.sh`，打进包里的还是上一次的 Python 代码。光看构建成功或看
`.app` 的修改时间都判断不出来，必须**打开产物本身**看。

本脚本做两层检查：

A. 产物级（静态，不解压运行）——直接读 frozen 二进制里的 PYZ，反编译出目标
   模块，断言本次改动引入的符号/常量确实在里面。这一层能抓到"没重新打包"。
B. RPC 级（动态）——真起一次 sidecar，走 `app.info` / `input.probe` /
   `record.start→stop` / 录制库六个方法 / 一个必定触发后端告警的条件节点，
   断言协议面（字段、判据、通知投递）与本次实现一致。
   录制库那几项**自己造、自己删**，不给用户的 `recordings/` 留测试垃圾。

用法：
    ./.venv/bin/python scripts/check_installed.py            # 默认 /Applications/Auto Flow.app
    ./.venv/bin/python scripts/check_installed.py <path.app> # 指定产物

退出码 0 = 全部通过；非 0 = 有失败项（会逐条列出）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

DEFAULT_APP = "/Applications/Auto Flow.app"

# 产物级断言：模块名 -> (检查函数, 人话描述)
# 检查函数收到该模块的 code object，返回 True/False。
# 这些是"必须打进包、但出问题时不报错只会静默失效"的关键点。


def _co_names_recursive(code) -> set:
    """递归收集 code object 里引用到的全部名字（含嵌套函数/推导式）。"""
    names = set(code.co_names)
    for const in code.co_consts:
        if hasattr(const, "co_names"):
            names |= _co_names_recursive(const)
    return names


def _sub_code(code, fname):
    """取出某个函数/方法自身的 code object（递归找，方法在类体里嵌一层）。"""
    for const in code.co_consts:
        if isinstance(const, type(code)):
            if const.co_name == fname:
                return const
            found = _sub_code(const, fname)
            if found is not None:
                return found
    return None


def _method_code(code, cls: str, meth: str):
    """取 `cls.meth` **自身**的 code object。

    为什么非要缩到方法这一层：`_co_names_recursive` 扫的是整模块，名字只要在
    **任何地方**出现过就算数。若某次改动把 `run()` 里的调用还原成旧写法、
    只留下一个没人调用的辅助函数，整模块扫描照样找得到那个名字——
    断言于是**假绿**。缩到 `run` 自己的 `co_names` 才答得了「这个节点现在
    真的走那条路径吗」。
    """
    cls_code = _sub_code(code, cls)
    return None if cls_code is None else _sub_code(cls_code, meth)


def _artifact_checks(recorder, inputsource, controller, server, vision,
                     events=None, executor=None, lib=None, builtin=None,
                     wflib=None) -> list:
    out = []
    if recorder is None:
        return [("core.recorder 没打进 PYZ（打包不完整？）", False)]

    # core.recorder：停止裁剪的护栏常量（2026-09-12 事故修复的核心）
    out.append(("core.recorder.TRIM_STOP_CLICK_MS == 1500",
                1500 in recorder.co_consts))

    # 修复的核心是**去掉了无上界的弹尾部移动**：trim_stop_interaction 里
    # 不应再出现 pop。逐函数看，不看全模块（模块里别处可能合法地 pop）。
    trim = _sub_code(recorder, "trim_stop_interaction")
    out.append(("trim_stop_interaction 里已无 pop（不再弹尾部移动）",
                trim is not None and "pop" not in trim.co_names))

    # core.inputsource：在函数内部 import，PyInstaller 很容易漏掉（漏了就
    # input.probe 静默少一个字段，不报错）
    out.append(("core.inputsource 已打进 PYZ", inputsource is not None))

    # rpc.controller：新端点
    out.append(("rpc.controller 定义了 record_keys_to_text",
                controller is not None
                and "record_keys_to_text" in _co_names_recursive(controller)))
    # 裁剪快照已改元素级复制：record_stop 里应出现 replace（修复前没有）。
    # 这条能挡住"撤销回来是被人改过的数据"那个静默损坏。
    stop_co = _sub_code(controller, "record_stop") if controller else None
    out.append(("record_stop 用 replace 做元素级快照（撤销不会拿到被改的数据）",
                stop_co is not None and "replace" in _co_names_recursive(stop_co)))
    # 入口脚本 rpc.server：方法表里的注册名（漏了就是方法不存在，调用报 -32601）
    out.append(("rpc.server 注册了 record.keysToText",
                server is not None and "record.keysToText" in _all_consts(server)))

    # core.vision：截屏必须请求**物理像素**（2026-09-13 修「截图识别没有效果」）。
    # 这条最容易静默失效——不去掉那个标志位也不报错，只是匹配分数整体砍半，
    # 从外面看和「屏上没有目标」一模一样。两个符号是 getattr 的字符串实参，
    # 所以查函数自身的常量表，不查 co_names。
    prefer = _sub_code(vision, "_prefer_physical_resolution") if vision else None
    consts = _all_consts(prefer) if prefer is not None else set()
    out.append(("core.vision 去掉 mss 的 NominalResolution（截屏取物理像素）",
                "kCGWindowImageNominalResolution" in consts
                and "IMAGE_OPTIONS" in consts))

    # core.vision：跨密度组要**自动缩放模板**（2026-09-13 F-VIS-10）。
    # 同属「不生效也不报错」的一类：密度不一致时分数只是整体偏低，
    # 从外面看和「屏上确实没有」一模一样。查 find_template 有没有真的调用它。
    find = _sub_code(vision, "find_template") if vision else None
    out.append(("core.vision 按屏幕密度自动重采样模板（跨密度组不必重截）",
                _sub_code(vision, "_resampled_template") is not None
                and find is not None
                and "_resampled_template" in _co_names_recursive(find)))

    # rpc.server：后端 WARNING 要**转发给界面**（2026-09-13）。
    # 同属「不生效也不报错」的一类：少了它，core.vision 那些「点歪 / 找不到」
    # 的诊断只进应用日志（那份日志是逐帧 RPC 流水，不含诊断字符串），
    # 界面上依然什么都没有——从外面看和「压根没做这个功能」一模一样。
    srv_names = _co_names_recursive(server) if server is not None else set()
    out.append(("rpc.server 把 WARNING 转发成 log.warning 通知",
                {"_UiLogHandler", "_install_ui_log_handler"} <= srv_names
                and "log.warning" in _all_consts(server)))
    # 防递归守卫（`emit` 里查线程本地的 busy）：少了它，队列满时
    # `_send_notification` 自己那条 logger.warning 会自激 → 通知通道变死循环。
    emit = _sub_code(server, "emit") if server is not None else None
    out.append(("_UiLogHandler.emit 带线程本地防递归守卫",
                emit is not None and "_local" in _co_names_recursive(emit)
                and "busy" in _all_consts(emit)))

    # ---- v4 流程图（2026-09-13）----
    # 这一组全是「不生效也不报错」的类型，正是本脚本存在的理由。
    #
    # SCRIPT_VERSION 必须与源码一致。打进包的若是**上一版** sidecar，打开 v4 文件
    # 不会报任何错：旧版不认识 edges，会退化成「按 nodes 列表顺序跑」——
    # 界面看着正常、执行顺序却是错的。这是最难从外部发现的一类偏差。
    ev_consts = _all_consts(events) if events is not None else set()
    # 版本号是 **int**，`_all_consts` 只收字符串，所以这里直接看模块的 co_consts。
    # 同时要求名字也在（`co_consts` 只有值没有名，光判值会放过「别处也有个 4」）。
    out.append(("core.events.SCRIPT_VERSION == 4（打进来的不是上一版 sidecar）",
                events is not None
                and "SCRIPT_VERSION" in events.co_names
                and 4 in events.co_consts))
    out.append(("core.events 定义了出口名常量（out/true/false/else）",
                {"out", "true", "false", "else"} <= ev_consts))
    # 迁移摆位：缺了它旧文件打开是一摞完全重叠的卡片，看起来像工作流被毁了
    out.append(("core.events 有 _linear_positions（旧文件迁移会摆开坐标）",
                events is not None
                and "_linear_positions" in _co_names_recursive(events)))
    # 边的落点侧（2026-09-15）：`dst_side` 是**纯展示字段**，旧 sidecar 不认识它，
    # 读文件时会**静默丢掉**——表现是「打开文件后连线全跑到左边」，不报任何错。
    # 所以既要判三个侧名在，也要判 `right` **不在**（右边是输出侧，会与出口手柄
    # 同点重合，Vue Flow 判定落点不稳定）。`core/events.py` 里没有别的 "right" 字面量。
    out.append(("core.events 定义了落点侧常量 left/top/bottom，且没有 right",
                events is not None
                and {"left", "top", "bottom"} <= ev_consts
                and "right" not in ev_consts))
    # 收敛函数是「脏值不炸」的唯一保证：少了它，前端 targetHandle 会指向不存在的
    # 手柄，Vue Flow 直接画不出那条边（只在控制台刷告警）——用户看到「连线莫名消失」。
    out.append(("core.events 有 normalize_target_side（落点侧脏值收敛）",
                events is not None
                and "normalize_target_side" in _co_names_recursive(events)))
    # 循环兜底：缺了它，连成环的图会让界面永远停在「运行中」，只能强杀进程。
    # 查 Executor 类体自身的常量表，而不是整个模块（模块里别处也可能有 10000）。
    ex_body = _sub_code(executor, "Executor") if executor is not None else None
    out.append(("core.executor.Executor.MAX_STEPS == 10000（死循环兜底）",
                ex_body is not None
                and "MAX_STEPS" in ex_body.co_names
                and 10000 in ex_body.co_consts))
    out.append(("core.executor 走图遍历（_walk + next_map + entry_uid）",
                executor is not None
                and {"_walk", "next_map", "entry_uid"}
                <= _co_names_recursive(executor)))

    ctrl_names = _co_names_recursive(controller) if controller is not None else set()
    out.append(("rpc.controller 有画布方法实现（edge_add/edge_remove/node_set_pos/set_start）",
                {"edge_add", "edge_remove", "node_set_pos", "workflow_set_start"}
                <= ctrl_names))
    # 节点改动按 uid 寻址（2026-09-15）：_index_of 是那批方法共用的解析入口。
    # 只看方法表名字是不够的——uid 支持是**行为**，所以下面 RPC 级还实测一次。
    out.append(("rpc.controller 有 _index_of（节点改动按 uid 优先寻址）",
                "_index_of" in ctrl_names))
    srv_consts = _all_consts(server) if server is not None else set()
    out.append(("rpc.server 注册了画布方法（edge.add/edge.remove/node.setPos/workflow.setStart）",
                {"edge.add", "edge.remove", "node.setPos", "workflow.setStart"}
                <= srv_consts))

    # ---- 录制库（2026-09-15）----
    # 打进来的若是**上一版** sidecar，界面上「存入库 / 落到画布」会报 unknown method，
    # 而 `record.stop` 只是**悄悄不再自动入库**——用户不会看到任何异常，
    # 只会发现"我刚录的那段不在库里"。所以库的存储层与六个方法都要在这里点名。
    lib_consts = _all_consts(lib) if lib is not None else set()
    lib_names = _co_names_recursive(lib) if lib is not None else set()
    out.append(("core.recording_lib 打进了包（save/load/list_meta 在）",
                lib is not None
                and {"save", "load", "list_meta"} <= lib_names))
    # id 白名单是**路径穿越**的唯一防线（id 直接拼路径写文件）。正则模式是常量，
    # 用**字面量**判（`re.compile` 的模式串进 co_consts，见 _all_consts），
    # 而不是在测试里重跑一遍正则——这条要答的是"打包进来的那份带没带它"。
    out.append(("core.recording_lib 带 id 白名单正则（^[A-Za-z0-9_-]{1,64}$）",
                lib is not None and "_ID_RE" in lib.co_names
                and "^[A-Za-z0-9_-]{1,64}$" in lib_consts))
    # 原子替换：少了它，覆盖写一半崩掉会留下半截 JSON，那条录制永久损坏
    out.append(("core.recording_lib 走临时文件 + os.replace（原子替换）",
                lib is not None and ".tmp" in lib_consts
                and "replace" in lib_names))
    out.append(("rpc.controller 有录制库方法实现（record_lib_save/from_lib/to_node）",
                {"record_lib_save", "record_from_lib", "record_lib_to_node"}
                <= ctrl_names))
    out.append(("rpc.server 注册了录制库方法（record.lib.* / record.fromLib）",
                {"record.lib.list", "record.lib.save", "record.lib.rename",
                 "record.lib.delete", "record.fromLib", "record.lib.toNode"}
                <= srv_consts))
    # 自动入库 + 过期可侦测：`_record_saved` 是"当前这段对应库里哪一条"的唯一记录，
    # `_lib_info` 拿它比 rev 得出 dirty。少了它，界面会一直显示「已同步」。
    out.append(("rpc.controller 有 _record_saved / _lib_info（自动入库 + 过期可侦测）",
                {"_record_saved", "_lib_info"} <= ctrl_names))
    bi_names = _co_names_recursive(builtin) if builtin is not None else set()
    bi_consts = _all_consts(builtin) if builtin is not None else set()
    out.append(("tasks.builtin 的录制回放支持引用录制库（tolerant_event/_from_library）",
                {"tolerant_event", "_from_library"} <= bi_names))
    # `内嵌` / `录制库` 是**跨语言协议**（前端 flow/recording.ts 里同名常量）。
    # 这条断言的意义是「后端这份确实带上了新取值」，不是「两边一致」——
    # 两边一致由 tauri/tests/recording.test.mjs 直接读 builtin.py 来保证。
    out.append(("tasks.builtin 里能看到「内嵌」「录制库」两个字面量",
                {"内嵌", "录制库"} <= bi_consts))

    # ---- 录制事件编辑 + 库条目直接编辑（2026-09-17）----
    # 打进来的若是**上一版** sidecar，界面上「复制 / 上移 / 下移 / 插入」会报
    # unknown method，而字段编辑区改完只弹「编辑失败」——用户会以为是自己填错了。
    out.append(("rpc.server 注册了事件编辑方法（patch/insert/duplicate/moveEvent）",
                {"record.patch", "record.insert", "record.duplicate",
                 "record.moveEvent"} <= srv_consts))
    out.append(("rpc.server 注册了库条目直接编辑（record.lib.edit / endEdit）",
                {"record.lib.edit", "record.lib.endEdit"} <= srv_consts))
    out.append(("rpc.controller 有事件编辑实现（record_patch/insert/duplicate/move_event）",
                {"record_patch", "record_insert", "record_duplicate",
                 "record_move_event"} <= ctrl_names))
    # 字段白名单是**跨语言协议**（前端把字段名当字符串发过来，后端按它校验），
    # 也是**权限边界**（不是白名单里的字段一律 -32602）。少了它，改字段会全被拒。
    # 五种类型名逐个点名，不是只判 `_PATCH_FIELDS` 在不在。
    out.append(("rpc.controller 带字段白名单 _PATCH_FIELDS（五种类型齐全）",
                "_PATCH_FIELDS" in ctrl_names
                and {"move", "mouse", "wheel", "key", "text"}
                <= _all_consts(controller)))
    # 写回必须在**后端**统一做（`_record_commit` → `_sync_lib_edit`）：编辑接口有
    # 十几个，靠前端记账漏一个就是「库里还是旧的、界面却显示已同步」。
    out.append(("rpc.controller 有 _record_commit / _sync_lib_edit（编辑收尾统一写回）",
                {"_record_commit", "_sync_lib_edit", "_record_lib_edit_id"}
                <= ctrl_names))
    # 上移 / 下移要**连 ts_ms 一起换**：回放排期是绝对时间线，只挪数组元素会让被
    # 提前的那条 `due` 已过、立刻执行。`record_move_event` 里应出现 replace。
    mv = _sub_code(controller, "record_move_event") if controller is not None else None
    out.append(("record_move_event 用 replace 交换（连 ts_ms 一起换，不只是挪数组）",
                mv is not None and "replace" in _co_names_recursive(mv)))

    # ---- 点击位置 = 框内取点（2026-09-16）----
    # 纯函数本身由 tests/test_core.py 的 14 条用例 + 两次反向验证保证；这里要答的是
    # 另一个问题：**打包进来的这份，两个点击节点是不是真的走了共用取点**。
    # 断言缩到 `run` 自己的 co_names（不能扫全模块，理由见 `_method_code`）——
    # 若有人把 run() 还原成 `m.x + offset_x` 而把 `_click_target` 留成死代码，
    # 整模块扫描会假绿，这里不会。
    out.append(("tasks.builtin 有 click_point_in_box（框内取点纯函数）",
                "click_point_in_box" in bi_names and "_click_target" in bi_names))
    for cls in ("ImageClickTask", "OcrClickTask"):
        run = _method_code(builtin, cls, "run") if builtin is not None else None
        out.append((f"{cls}.run 真的走共用取点（_click_target 在它的 co_names 里）",
                    run is not None and "_click_target" in run.co_names))
    # 三个取值是**跨语言协议**：它们会被写进用户文件，前端 ParamsPanel 按字面量
    # 渲染下拉。改了字面量 = 老文件的 select 显示为空、show_if 把字段藏掉。
    out.append(("tasks.builtin 里能看到三种「点击位置」取值",
                {"目标中心", "框内随机", "框内自定义"} <= bi_consts))

    # ---- 工作流库（2026-09-16）----
    # 落点是**打包进来的那份**：单测跑的是源码树，装进去的是 frozen PYZ。
    # 少打一个模块、或者某个函数被换回旧写法，单测全绿而安装版行为不同。
    out.append(("core.workflow_lib 已打进 PYZ", wflib is not None))
    # 自身窗口遮罩（F-VIS-12）：漏进包的表现是「识别照常、但会点穿到桌面」，
    # 正是那种「看着正常、后果很糟」的静默失败，必须由产物断言兜住。
    selfmask = mods.get("core.selfmask")
    names = set()
    if selfmask is not None:
        def _walk(c):
            names.update(c.co_names)
            for k in c.co_consts:
                if hasattr(k, "co_names"):
                    _walk(k)
        _walk(selfmask)
    out.append(("core.selfmask 已打进 PYZ（含 mask_capture）",
                "mask_capture" in names))
    # 删除必须是**移动**（进废纸篓），不能是直接删：这是用户数据，删掉没有第二次
    # 机会。单测能验行为，这条验的是「装进去的这份也是这么写的」。
    rm_co = _sub_code(wflib, "remove") if wflib is not None else None
    out.append(("workflow_lib.remove 走 shutil 移动（删除 = 进废纸篓，不是真删）",
                rm_co is not None and "shutil" in rm_co.co_names
                and "remove" not in rm_co.co_names))
    # `nodes: []` 是「新建后还没加节点」的正常状态。写成 `nodes or events` 的
    # or 链会把**每一份空工作流**判成格式损坏——症状是新建的条目改不了名、打不开。
    hl_co = _sub_code(wflib, "_has_node_list") if wflib is not None else None
    out.append(("workflow_lib._has_node_list 用 any(...) 判类型（空列表不算损坏）",
                hl_co is not None and "any" in hl_co.co_names))
    out.append(("workflow_lib 有 id_of_path / path_of（库归属的唯一判据 + 唯一校验点）",
                {"id_of_path", "path_of"} <=
                (_co_names_recursive(wflib) if wflib is not None else set())))
    # 这条答的是**老包 vs 新包**——也就是这条流水线的本职：「改了代码却没重新
    # 打包」。更细的问题（保存到底有没有真的入库、那条死路有没有回来）交给 RPC 级
    # 的**行为**断言和 `tests/test_workflow_lib.py`：在字节码上断不出来——
    # 把 `wid=wid or None` 去掉、甚至把 `raise` 加回来，`co_names` 都不变；
    # 而改成断字符串又会被**函数自己的文档字符串**绊住（那里恰好写着这个短语，
    # 且 `_all_consts` 返回的是集合、`in` 是精确匹配而不是子串）。
    wsave = (_method_code(controller, "AppController", "workflow_save")
             if controller is not None else None)
    out.append(("controller.workflow_save 里出现了 workflow_lib（老包里没有）",
                wsave is not None and "workflow_lib" in set(wsave.co_names)))
    # dirty 的唯一判据是**两个版本号比出来的**，不是写死的常量、也不在前端记账。
    wd = (_method_code(controller, "AppController", "workflow_dirty")
          if controller is not None else None)
    out.append(("controller.workflow_dirty 比的是 _wf_rev / _wf_saved_rev",
                wd is not None and {"_wf_rev", "_wf_saved_rev"} <= set(wd.co_names)))
    # 注意：「改动之后 dirty 真的会变 true」由 RPC 级的 `_workflow_lib_checks`
    # **行为**验证（加一个节点再查一次）。这里只断 `_broadcast_workflow` 自增了
    # 版本号是断不出来的——`+= 0` 的 co_names 里照样有 `_wf_rev`。
    # -32006 是**跨语言协议**：前端 WorkflowMenu.vue / WorkflowLibDialog.vue 里
    # 各写了一份 UNSAVED_CHANGES。对不上不会报错，只会让「新建 / 打开」在改过
    # 东西之后莫名其妙点不动（用户读到的是一句英文错误码）。
    guard = (_method_code(controller, "AppController", "_guard_unsaved")
             if controller is not None else None)
    out.append(("controller._guard_unsaved 用 -32006 拒绝丢弃未保存的改动",
                guard is not None and -32006 in guard.co_consts))
    out.append(("rpc.server 注册了工作流库七个方法（workflow.lib.*）",
                {"workflow.lib.list", "workflow.lib.save", "workflow.lib.open",
                 "workflow.lib.rename", "workflow.lib.delete",
                 "workflow.lib.import", "workflow.lib.export"} <= srv_consts))
    return out


def _all_consts(code) -> set:
    """递归收集全部字符串常量（方法表里的方法名是纯字符串常量）。

    **必须钻进 tuple / frozenset**：CPython 编译字典字面量时，会把一批**常量键**
    打包成**一个 tuple 常量**（实测：`_HANDLERS` 里约每 15 个键打成一包）。
    只扫顶层 `co_consts` 的话，落在某一包里的键就查不到——而这件事**只跟字典
    多大、键排在第几个有关**：往同一个字典里追加几个方法，可能刚好把新键推进
    "被打包"的那一段，于是新加的断言莫名其妙地红/绿，看起来像打包漏了。
    """
    out = {c for c in code.co_consts if isinstance(c, str)}
    for c in code.co_consts:
        if isinstance(c, (tuple, frozenset, list)):
            out |= {x for x in c if isinstance(x, str)}
        if hasattr(c, "co_consts"):
            out |= _all_consts(c)
    return out


def _load_pyz(exe: str) -> dict:
    """取出目标模块的 code object。

    分两处找：
    - `core.*` / `rpc.controller` 在 PYZ 里（ZlibArchive，`extract` 直接给 code object）；
    - **入口脚本** `rpc/server.py` 不在 PYZ 里，它以 CArchive 项名 `server`
      存的是 marshal 后的字节，得 `marshal.loads`。
    """
    import marshal

    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    car = CArchiveReader(exe)
    tmpdir = tempfile.mkdtemp(prefix="autoflow-pyz-")
    pyz_path = os.path.join(tmpdir, "PYZ.pyz")
    with open(pyz_path, "wb") as f:
        f.write(car.extract("PYZ.pyz"))
    zar = ZlibArchiveReader(pyz_path)
    mods = {}
    for name in ("core.recorder", "core.inputsource", "rpc.controller",
                 "core.vision", "core.events", "core.executor",
                 "core.recording_lib", "core.workflow_lib", "tasks.builtin",
                 "core.selfmask"):
        try:
            mods[name] = zar.extract(name)      # 直接返回 code object，不是 bytes
        except Exception:
            mods[name] = None
    try:
        mods["rpc.server"] = marshal.loads(car.extract("server"))
    except Exception:
        mods["rpc.server"] = None
    return mods


class Sidecar:
    """最小 NDJSON 客户端：够用来发几帧请求、并收通知即可。

    读线程把响应与通知分流：响应进 `_responses` 按 id 配对，通知进
    `notifications`。**必须分流**——`log.warning` 那条检查要等通知，
    而通知与响应在同一条 stdout 上交错，同步 `readline` 很容易把通知吞掉。
    """

    def __init__(self, path: str):
        # 「删除进废纸篓」的断言要能**验证并清理**，所以给这次会话一个**临时废纸篓**。
        # 不这么做的话有两条都很难发现的问题：
        # ① 每跑一次校验，用户废纸篓里就多出 `2026xxxxxxxx-xxxxxx.json`，而用户
        #    根本不知道那是什么、也不知道能不能删；
        # ② 下面那句 `os.remove(trash_path)` 是**静默失效**的——macOS 对 `~/.Trash`
        #    的程序化删除与列目录都有 TCC 保护（`os.listdir` 直接
        #    `PermissionError: Operation not permitted`；`os.unlink` 不报错、
        #    文件却还在）。于是「自己造的自己清」这条自检在真废纸篓上**永远假绿**。
        # `core/workflow_lib.py` 本来就支持 `AUTOFLOW_TRASH_DIR` 覆盖（单测也用它），
        # 这里借它把校验会话隔离掉：断言仍验「真的被移走、内容完整」，只是移到了
        # 临时目录，收尾能真删干净。
        self.trash_dir = tempfile.mkdtemp(prefix="autoflow-chk-trash-")
        env = dict(os.environ, AUTOFLOW_TRASH_DIR=self.trash_dir)
        self.p = subprocess.Popen(
            [path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)
        self._id = 0
        self._responses: dict = {}
        self.notifications: list = []
        self._lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:  # noqa: BLE001 - 半包/杂音不该打死收尾校验
                continue
            with self._lock:
                if "id" in msg:
                    self._responses[msg["id"]] = msg
                else:
                    self.notifications.append(msg)

    def call(self, method: str, params: dict | None = None,
             timeout: float = 30.0) -> dict:
        self._id += 1
        mid = self._id
        req = {"jsonrpc": "2.0", "id": mid, "method": method,
               "params": params if params is not None else {}}
        self.p.stdin.write(json.dumps(req) + "\n")
        self.p.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                hit = self._responses.pop(mid, None)
            if hit is not None:
                return hit
            if self.p.poll() is not None:
                raise RuntimeError(f"{method}: sidecar 提前退出")
            time.sleep(0.02)
        raise TimeoutError(f"{method} 无响应")

    def clear_notifications(self) -> None:
        with self._lock:
            self.notifications.clear()

    def wait_notification(self, method: str, timeout: float = 10.0) -> dict | None:
        """等到某类通知出现并返回它；超时返回 None。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for n in self.notifications:
                    if n.get("method") == method:
                        return n
            time.sleep(0.05)
        return None

    def close(self) -> None:
        for fn in (self.p.stdin.close, self.p.terminate):
            try:
                fn()
            except Exception:
                pass
        try:
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()
        shutil.rmtree(self.trash_dir, ignore_errors=True)


def _library_checks(s: "Sidecar") -> list:
    """录制库（2026-09-15，2026-09-17 扩到直接编辑）：库方法都走真实的 RPC 往返。

    **为什么自己写一条库文件**（而不是先录一段再入库）：live checks 里
    `record.start` 后立刻 `record.stop`，抓到的事件数是 0，而空录制**刻意不入库**
    （见 controller.record_stop）——靠它验不到自动入库。直接按存储格式写一条，
    正好也验证了「(冻结后的) 安装版读得懂库文件」这件事，比绕一圈更直接。

    **自己造的东西自己删**：用户的 `recordings/` 里不该留下 `chk-` 开头的测试垃圾。
    末尾会把两侧新增的条目全删掉，并断言列表回到基线。
    """
    out: list = []
    base = s.call("record.lib.list").get("result") or {}
    items = base.get("items") or []
    if not base:
        return [("record.lib.list 可用", False)]
    out.append((f"record.lib.list 可用（{len(items)} 条，"
                f"dir={str(base.get('dir'))[-28:]}）", "dir" in base))

    d = base.get("dir") or ""
    made: list = []
    try:
        # 按 recording_lib.save 的落盘格式手工写一条
        rid = "chk-lib-" + uuid.uuid4().hex[:8]
        evs = [{"ts_ms": 0, "kind": "move", "x": 1, "y": 2},
               {"ts_ms": 40, "kind": "mouse", "x": 3, "y": 4, "pressed": True,
                "button": "left", "clicks": 1}]
        with open(os.path.join(d, f"{rid}.json"), "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "id": rid, "name": "check-installed 临时条目",
                       "created_at": "2000-01-01 00:00:00", "events": evs,
                       "origin": [7, 8]}, f, ensure_ascii=False)
        made.append(rid)

        after = s.call("record.lib.list").get("result") or {}
        got = [m for m in (after.get("items") or []) if m.get("id") == rid]
        out.append(("record.lib.list 认得新写入的条目（名字/条数/原点都对）",
                    bool(got) and got[0].get("count") == 2
                    and got[0].get("origin") == [7, 8]
                    and got[0].get("name") == "check-installed 临时条目"))

        # 载入编辑缓冲：条数必须等于文件里的条数（容忍构造不能让两边对不上）
        load = s.call("record.fromLib", {"id": rid}).get("result") or {}
        out.append((f"record.fromLib 载入并带上 events（count={load.get('count')}）",
                    load.get("count") == 2))
        lib = load.get("lib") or {}
        out.append((f"载入后界面显示「已同步」（dirty={lib.get('dirty')}）",
                    lib.get("id") == rid and lib.get("dirty") is False))

        # ---- 单条事件的编辑 + 库条目直接编辑（2026-09-17）----
        # 这一段验的是**整条写回链**：edit 进模式 → patch 改坐标 → 自动落盘。
        # 少了任何一环，界面表现都是「改完显示已同步、库里还是旧的」——按 id 引用
        # 它的节点回放的是库里那份，用户完全无从察觉。
        ed = s.call("record.lib.edit", {"id": rid}).get("result") or {}
        elib = ed.get("lib") or {}
        out.append((f"record.lib.edit 进入直接编辑模式（editing={elib.get('editing')}）",
                    elib.get("editing") is True and elib.get("id") == rid))
        patched = s.call("record.patch", {"index": 0,
                                          "fields": {"x": 111, "y": 222}}).get("result") or {}
        pev = (patched.get("events") or [{}])[0]
        out.append((f"record.patch 改坐标（x={pev.get('x')}, y={pev.get('y')}）",
                    pev.get("x") == 111 and pev.get("y") == 222))
        # 非法值必须报错、不能静默当 0（坐标收敛成 0 会把回放光标甩到屏幕左上角）
        badv = s.call("record.patch", {"index": 0, "fields": {"x": "abc"}})
        out.append((f"record.patch 拒绝非法值（{(badv.get('error') or {}).get('code')}）",
                    (badv.get("error") or {}).get("code") == -32602))
        badk = s.call("record.patch", {"index": 0, "fields": {"button": "left"}})
        out.append((f"record.patch 拒绝该类型没有的字段（{(badk.get('error') or {}).get('code')}）",
                    (badk.get("error") or {}).get("code") == -32602))
        # 上移 / 下移**连 ts_ms 一起换**：判据不是「时间戳跟着事件走」，而是
        # 「**事件换了位置、时间线仍单调**」——挪到哪个槽就取哪个槽的时刻。
        # 只挪数组不换 ts 的话会得到 ts=[40,0]：被挪到前面的那条 `due` 已过、
        # 立刻执行（用户看到的是「上移一步，那一步当场就跑了」）。
        # ⚠️ 必须在**只有这两条**的时候验：先 insert/duplicate 会插进同刻的条目，
        # 交换前后时间戳一样，断言就没有区分度了。
        s.call("record.moveEvent", {"index": 1, "delta": -1})
        cur = (s.call("record.current").get("result") or {}).get("events", [])
        kinds = [e.get("kind") for e in cur]
        ts = [e.get("ts_ms") for e in cur]
        out.append((f"record.moveEvent 事件换位且时间线仍单调（{kinds} ts={ts}）",
                    kinds[:2] == ["mouse", "move"] and ts == [0, 40]))
        s.call("record.moveEvent", {"index": 0, "delta": 1})   # 换回去，便于按下标定位
        cur2 = (s.call("record.current").get("result") or {}).get("events", [])
        out.append((f"record.moveEvent 换回去（{[e.get('kind') for e in cur2][:2]}）",
                    [e.get("kind") for e in cur2][:2] == ["move", "mouse"]))
        # 插入 / 复制：时间戳缺省对齐前一条、复制沿用同一时刻（都是"忠实"的语义）
        ins = s.call("record.insert", {"index": 0, "event": {"kind": "move"}}).get("result") or {}
        out.append((f"record.insert 插一条（count={ins.get('count')}）", ins.get("count") == 3))
        dup = s.call("record.duplicate", {"index": 0}).get("result") or {}
        out.append((f"record.duplicate 复制一条（count={dup.get('count')}）",
                    dup.get("count") == 4))
        # 退出编辑模式；**库里那份必须已经变了**（这才是"直接编辑"的意义）。
        # 改过的那条现在在**下标 2**（前面插了一条 + 复制了一条）。
        end = s.call("record.lib.endEdit").get("result") or {}
        out.append((f"record.lib.endEdit 退出编辑模式"
                    f"（editing={(end.get('lib') or {}).get('editing')}）",
                    (end.get("lib") or {}).get("editing") is False))
        reread = s.call("record.fromLib", {"id": rid}).get("result") or {}
        revs = reread.get("events") or []
        out.append((f"直接编辑已自动写回库里（重读 count={reread.get('count')}、"
                    f"第 3 条 x={revs[2].get('x') if len(revs) > 2 else None}）",
                    reread.get("count") == 4 and len(revs) > 2
                    and revs[2].get("x") == 111 and revs[2].get("y") == 222))

        # 覆盖同一条：id 不变，引用它的节点才会跟着变
        saved = s.call("record.lib.save", {"id": rid,
                                          "name": "check-installed 改名"}).get("result") or {}
        out.append(("record.lib.save 覆盖同一条（id 不变、改名生效）",
                    (saved.get("item") or {}).get("id") == rid
                    and (saved.get("item") or {}).get("name") == "check-installed 改名"))

        # 新建一条（不给 id）
        newp = s.call("record.lib.save", {"name": "check-installed 新建"}).get("result") or {}
        new_id = (newp.get("item") or {}).get("id")
        if new_id:
            made.append(new_id)
        out.append(("record.lib.save 不给 id 时新建一条（拿到新 id）",
                    bool(new_id) and new_id != rid))

        # 落到画布：节点只存 id，**不复制事件**（"库里改一次处处变"全靠这条）
        node = s.call("record.lib.toNode", {"id": rid}).get("result") or {}
        nd = node.get("node") or {}
        p = nd.get("params") or {}
        # 节点里**只存 id**：事件留在库里（"库里改一次处处变"全靠这条）。
        # 注意对外视图把 events 数组换成了 `{count}`（_public_node 的摘要化），
        # 所以这里判 `{"count": 0}`——真源是 `[]`，两者都是"节点里没有事件"。
        ev = p.get("events")
        empty_ev = (ev == [] or ev == {"count": 0})
        out.append(("record.lib.toNode 落成节点且只存 id（events 空、source=录制库）",
                    p.get("source") == "录制库" and p.get("recording_id") == rid
                    and empty_ev))
        out.append((f"落到画布的节点带上录制名（{p.get('recording_name')}）",
                    p.get("recording_name") == "check-installed 改名"))

        # 非法 id：必须报错而不是静默成功（id 直接拼路径，放过去就能删任意文件）
        bad = s.call("record.lib.delete", {"id": "../../etc/hosts"})
        out.append((f"record.lib.delete 拒绝非法 id（{(bad.get('error') or {}).get('code')}）",
                    (bad.get("error") or {}).get("code") == -32005))
        miss = s.call("record.fromLib", {"id": "no-such-id-zzz"})
        out.append((f"record.fromLib 对不存在的 id 报错（{(miss.get('error') or {}).get('code')}）",
                    (miss.get("error") or {}).get("code") == -32005))
    finally:
        for rid in made:
            s.call("record.lib.delete", {"id": rid})
        left = s.call("record.lib.list").get("result") or {}
        ids = [m.get("id") for m in (left.get("items") or [])]
        out.append(("录制库清理干净（没有留下测试条目）",
                    not [i for i in ids if str(i).startswith("chk-lib-")]))
    return out


def _condition_group_checks(s: "Sidecar") -> list:
    """条件组（2026-09-15）：定义能在菜单里查到，且**装进去的这份真的会短路**。

    为什么值得单独验一条行为：`condition_group` 的价值一半在「多项」，另一半在
    「短路」——不短路的话用户排的顺序完全不影响结果，等于把「先看哪一项」这个
    表达力丢了（与 `branch` 的「先到先得」同理）。而单测跑的是源码，证明不了
    打包进去的这份也是这样。

    **短路用可观测的副作用证明**：条件项指向不存在的模板 → `find_template` 在
    读屏之前就返回「找不到」并记一次 WARNING。于是
    「与（第 1 项就不成立）」应当只报 **1** 次、「或（两项都不成立）」应当报 **2** 次
    ——同一个节点、同一批输入，只有求值顺序不同，条数就必须不同。
    不依赖屏幕录制权限，也不碰键鼠。

    ⚠️ 告警文案是「模板图读不出来：<path>」，**别按 `missing-template` 找**：
    那只是 `_warn_once` 的去重键，不在文案里（按它找会永远数到 0，看着像没短路）。
    """
    out: list = []
    defs = s.call("nodes.definitions").get("result") or []
    by = {d["type"]: d for d in defs}
    g = by.get("condition_group")
    out.append(("nodes.definitions 含 condition_group（「添加节点」里会有它）",
                g is not None))
    if not g:
        return out
    ps = {p["key"]: p for p in g["params"]}
    out.append((f"condition_group order={g['order']} 夹在 condition/branch 之间，"
                f"cond_count {ps['cond_count']['min']}..{ps['cond_count']['max']}",
                by["condition"]["order"] < g["order"] < by["branch"]["order"]
                and (ps["cond_count"]["min"], ps["cond_count"]["max"]) == (2, 6)))

    def run_case(combine: str, tag: str) -> tuple:
        with tempfile.TemporaryDirectory(prefix="autoflow-cgchk-") as td:
            wf = os.path.join(td, "wf.json")
            with open(wf, "w", encoding="utf-8") as f:
                json.dump({"version": 4, "name": "cg", "start": "", "edges": [],
                           "nodes": [
                               {"type": "condition_group", "enabled": True, "params": {
                                   "cond_count": 2, "combine": combine,
                                   "cond1_kind": "图像存在",
                                   "cond1_value": os.path.join(td, f"{tag}-a.png"),
                                   "cond2_kind": "图像存在",
                                   "cond2_value": os.path.join(td, f"{tag}-b.png"),
                                   "timeout_s": 0.0}},
                               {"type": "delay", "enabled": True, "params": {"ms": 1}},
                               {"type": "delay", "enabled": True, "params": {"ms": 1}}]},
                          f, ensure_ascii=False)
            load = s.call("workflow.load", {"path": wf})
            if load.get("error"):
                return f"加载失败：{load['error'].get('message')}", -1
            uids = [n["uid"] for n in (load["result"]["workflow"] or {})["nodes"]]
            ea = s.call("edge.add", {"src": uids[0], "port": "true", "dst": uids[1]})
            eb = s.call("edge.add", {"src": uids[0], "port": "false", "dst": uids[2]})
            if ea.get("error") or eb.get("error"):
                return f"edge.add 失败：{ea.get('error') or eb.get('error')}", -1
            s.clear_notifications()
            if s.call("run.start").get("error"):
                return "run.start 失败", -1
            if s.wait_notification("run.finished", timeout=20.0) is None:
                return "等不到 run.finished", -1
            time.sleep(0.3)          # 让落在 run.finished 后面的通知也进队列
            with s._lock:
                return None, sum(
                    1 for n in s.notifications
                    if n.get("method") == "log.warning"
                    and "模板图读不出来" in json.dumps(n, ensure_ascii=False))

    for combine, tag, want in (("全部成立(与)", "and", 1), ("任一成立(或)", "or", 2)):
        err, n = run_case(combine, tag)
        how = "第 1 项不成立即停（短路）" if want == 1 else "两项都测完（不短路）"
        out.append((f"condition_group「{combine}」真跑一遍：2 项、{how} "
                    f"→ 报 {n} 次「模板图读不出来」",
                    err is None and n == want))
        if err:
            out.append((f"condition_group「{combine}」失败原因：{err}", False))
    return out


def _click_pos_checks(s: "Sidecar") -> list:
    """「点击位置 = 框内取点」（2026-09-16）：装进去的这份确实把选项给了界面。

    **只查定义、不真跑**：真跑需要屏幕上出现确定的模板/文字命中，headless 下
    不可复现（框内取点本身是纯函数，由 `tests/test_core.py` 的 14 条用例 +
    两次反向验证保证）。这条要答的是另一个问题——**打包进去的后端有没有把这
    几个参数暴露出来**：少了 `click_pos`，参数面板上根本不会出现「点击位置」
    下拉，用户只会以为「功能没做」，而所有单测照样全绿。

    与 `_condition_group_checks` 不同，这一组**不碰工作流状态**，所以放在哪儿都
    安全；仍然跟在它后面，保持「新加的一组放最后」这个约定。
    """
    out: list = []
    defs = s.call("nodes.definitions").get("result") or []
    by = {d["type"]: d for d in defs}
    for t in ("image_click", "ocr_click"):
        node = by.get(t)
        ps = {p["key"]: p for p in (node or {}).get("params") or []}
        cp = ps.get("click_pos") or {}
        out.append((
            f"{t} 暴露了「点击位置」（默认 {cp.get('default')}，"
            f"{len(cp.get('options') or [])} 档，百分比只在自定义时出现）",
            node is not None
            and cp.get("default") == "目标中心"
            and cp.get("options") == ["目标中心", "框内随机", "框内自定义"]
            and (ps.get("rel_x") or {}).get("show_if") == {
                "key": "click_pos", "eq": "框内自定义"}
            and (ps.get("rel_y") or {}).get("show_if") == {
                "key": "click_pos", "eq": "框内自定义"}
            and (ps.get("inset") or {}).get("min") == 0
            # 老的固定偏移不能被这次改动挤掉
            and (ps.get("offset_x") or {}).get("default") == 0))
    return out


def _workflow_lib_checks(s: "Sidecar") -> list:
    """工作流库（2026-09-16）：列表 / 保存 / 打开 / 更名 / 删除 / 导入导出走真实 RPC。

    **会换掉当前工作流**（新建 → 保存 → 打开），所以必须排在 `_live_checks` 的
    最后：上面那些断言全是按 uid 寻址的，中途换树会让它们一起假失败。

    **自己造的东西自己清**：在库里建两条 `check-installed 临时条目`，验完删除，
    连「废纸篓」里那两份也删掉——这次会话的废纸篓是**临时目录**
    （`Sidecar` 起进程时用 `AUTOFLOW_TRASH_DIR` 指过去的，见那里的注释），
    所以删得掉，也保证**用户的真废纸篓一个文件都不会多**。

    「删除进废纸篓」这条顺手验到了实处：拿到 `trash` 路径后先断言①它落在本次的
    临时废纸篓里（证明没碰用户真废纸篓）②**确实存在**（= 文件完整地躺在里面、
    可恢复），再自己清掉它。全程不碰用户原有的条目。
    """
    out: list = []
    base = s.call("workflow.lib.list").get("result") or {}
    if not base:
        return [("workflow.lib.list 可用", False)]
    d = base.get("dir") or ""
    out.append((f"workflow.lib.list 可用（{len(base.get('items') or [])} 份，"
                f"dir={d[-30:]}）", bool(d)))

    cur0 = s.call("workflow.current").get("result") or {}
    out.append(("workflow.current 带 lib_id / dirty / lib_dir（老 sidecar 没有）",
                all(k in cur0 for k in ("lib_id", "dirty", "lib_dir"))))

    wid = imp_id = ""
    try:
        # 新建（force：前面几组可能留下了未保存的改动）→ 加节点 → 存进库
        s.call("workflow.new", {"force": True})

        # dirty 的判据是**后端自增的版本号**。这里用**行为**验它：加一个节点，
        # dirty 必须从 false 变 true。断字节码是断不出来的——把 `self._wf_rev += 1`
        # 改成 `+= 0`，`_wf_rev` 照样在 co_names 里，那条断言不会红（实测过）。
        clean = s.call("workflow.current").get("result") or {}
        s.call("node.add", {"type": "delay"})
        dirty = s.call("workflow.current").get("result") or {}
        out.append((f"改一个节点后 dirty 从 {clean.get('dirty')} 变 {dirty.get('dirty')}"
                    f"（判据是后端自增的版本号）",
                    clean.get("dirty") is False and dirty.get("dirty") is True))

        saved = s.call("workflow.lib.save",
                       {"name": "check-installed 临时条目"}).get("result") or {}
        item = saved.get("item") or {}
        wid = item.get("id") or ""
        out.append((f"workflow.lib.save 落库（id={wid}，{item.get('node_count')} 个节点）",
                    bool(wid) and item.get("node_count") == 1
                    and saved.get("lib_id") == wid and saved.get("dirty") is False))

        # 「保存」是覆盖同一条，不是又建一条（总新建的话用户一按保存就多一份）
        s.call("node.add", {"type": "delay"})
        again = s.call("workflow.save").get("result") or {}
        listed = s.call("workflow.lib.list").get("result") or {}
        mine = [m for m in (listed.get("items") or []) if m.get("id") == wid]
        out.append((f"「保存」覆盖同一条（库里仍只有它，节点数 "
                    f"{(mine[0] if mine else {}).get('node_count')}）",
                    again.get("lib_id") == wid and len(mine) == 1
                    and mine[0].get("node_count") == 2))

        # 未保存保护：改了不存，新建要报 -32006（前端据此弹「丢弃改动」确认框）
        s.call("node.add", {"type": "delay"})
        blocked = s.call("workflow.new")
        out.append((f"有未保存改动时 workflow.new 被拒（返回 "
                    f"{(blocked.get('error') or {}).get('code')}）",
                    (blocked.get("error") or {}).get("code") == -32006))

        # 「打开」同样受未保存保护（上面刚加了第三个节点没存）。这条**必须**先验：
        # 少了它，「打开会把用户刚连好的一整张图静默换掉」在这层就是盲区——
        # 而这正是加保护的唯一理由。
        blocked2 = s.call("workflow.lib.open", {"id": wid})
        out.append((f"有未保存改动时 workflow.lib.open 被拒（返回 "
                    f"{(blocked2.get('error') or {}).get('code')}）",
                    (blocked2.get("error") or {}).get("code") == -32006))

        op = s.call("workflow.lib.open", {"id": wid, "force": True}).get("result") or {}
        nodes = (op.get("workflow") or {}).get("nodes") or []
        out.append((f"workflow.lib.open 带 force 打开并清掉未保存标记（{len(nodes)} 个节点）",
                    op.get("lib_id") == wid and op.get("dirty") is False
                    and len(nodes) == 2))

        rn = s.call("workflow.lib.rename",
                    {"id": wid, "name": "check-installed 改名"}).get("result") or {}
        out.append((f"workflow.lib.rename 生效（{(rn.get('item') or {}).get('name')}）"
                    f"且不标脏", (rn.get("item") or {}).get("name") == "check-installed 改名"
                    and rn.get("dirty") is False))

        # 导出当前那份：走**内存里的树**，且写出来的是普通工作流文件
        dst = os.path.join(tempfile.gettempdir(),
                           "af-chk-" + uuid.uuid4().hex[:8] + ".json")
        ex = s.call("workflow.lib.export", {"path": dst}).get("result") or {}
        with open(dst, "r", encoding="utf-8") as f:
            raw = json.load(f)
        out.append((f"workflow.lib.export 写出普通工作流文件（{len(raw.get('nodes') or [])} 个节点，"
                    f"无库元信息）",
                    ex.get("id") == wid and len(raw.get("nodes") or []) == 2
                    and not ({"schema", "created_at"} & set(raw))))

        # 导入：拷进库，且**不切换**当前工作流
        imp = s.call("workflow.lib.import", {"path": dst}).get("result") or {}
        os.remove(dst)
        imp_id = (imp.get("item") or {}).get("id") or ""
        after = s.call("workflow.lib.list").get("result") or {}
        out.append((f"workflow.lib.import 拷进库且不切换当前（库里 "
                    f"{len(after.get('items') or [])} 份，current 没变）",
                    bool(imp_id) and after.get("current") == wid))

        miss = s.call("workflow.lib.delete", {"id": "ghost-000000"})
        out.append((f"workflow.lib.delete 对不存在的 id 报错（返回 "
                    f"{(miss.get('error') or {}).get('code')}）",
                    (miss.get("error") or {}).get("code") == -32005))

        # 删除自己建的两条：进废纸篓 + 原文件消失 + 删当前那份不清画布
        # 废纸篓必须是**本次的临时目录**：不然这条会往用户的真废纸篓里丢文件，
        # 而 macOS 的 TCC 让那个目录既列不出来也删不掉（见 `Sidecar.__init__`）。
        tmp_trash = os.path.abspath(s.trash_dir)
        d1 = s.call("workflow.lib.delete", {"id": wid}).get("result") or {}
        t1 = d1.get("trash") or ""
        ok_rm = (bool(t1) and os.path.exists(t1)
                 and os.path.abspath(os.path.dirname(t1)) == tmp_trash
                 and not os.path.exists(os.path.join(d, wid + ".json")))
        if t1 and os.path.exists(t1):
            os.remove(t1)
        d2 = s.call("workflow.lib.delete", {"id": imp_id}).get("result") or {}
        t2 = d2.get("trash") or ""
        ok_rm = ok_rm and bool(t2) and os.path.exists(t2)
        if t2 and os.path.exists(t2):
            os.remove(t2)
        wid = imp_id = ""          # 已清干净，finally 不必再动手
        out.append(("workflow.lib.delete 移进废纸篓（原文件消失、废纸篓里是完整一份、"
                    "且落在本次临时废纸篓里，没碰用户真废纸篓）", ok_rm))

        # 删掉的正好是当前这份时，**只清路径、不动内存里的树**：文件没了但用户连的
        # 那张图还在，他仍能继续改、再「保存」回库（会自动新建一条）。
        # 直接清空工作流的话，一次误点就白丢一张图。
        cur2 = s.call("workflow.current").get("result") or {}
        out.append(("删掉当前那份不清画布（树还在 2 个节点、路径已清空）",
                    len((cur2.get("workflow") or {}).get("nodes") or []) == 2
                    and not cur2.get("lib_id")))
    finally:
        # 中途失败也别在用户的库里留下测试垃圾
        for x in (wid, imp_id):
            if not x:
                continue
            r = s.call("workflow.lib.delete", {"id": x}).get("result") or {}
            t = r.get("trash") or ""
            if t and os.path.exists(t):
                try:
                    os.remove(t)
                except OSError:
                    pass
    return out


def _live_checks(sidecar_path: str) -> list:
    out, s = [], Sidecar(sidecar_path)
    try:
        # 端点真的挂在 RPC 线上（不是只在方法表里有个字符串）。趁还没录过，
        # 调用应报 no_record_result(-32003)；没注册会报 unknown method(-32601)。
        # 两者能区分"没注册"与"注册了但实现炸了"。
        ks = s.call("record.keysToText", {"index": 0, "text": "x"})
        code = (ks.get("error") or {}).get("code")
        out.append((f"record.keysToText 已挂上 RPC（未录制时返回 {code}）",
                    code == -32003))

        r = s.call("app.info").get("result") or {}
        out.append((f"app.info 有响应（v{r.get('appVersion')} "
                    f"proto={r.get('protocolVersion')} frozen={r.get('frozen')}）",
                   bool(r)))
        # record.stop 的字段名以 PROTOCOL 为准，这里只断言"必须有"
        p = s.call("input.probe").get("result") or {}
        out.append((f"input.probe 报输入法（{p.get('input_source', {}).get('name')}）",
                   "input_source" in p))

        s.call("record.start")
        stop = s.call("record.stop", {"trim": False})
        if stop.get("error"):
            out.append((f"record.stop 可用（返回 {stop['error'].get('code')}，"
                        "多半缺辅助功能权限，非打包问题）", True))
        else:
            # record.stop 直接把统计当 result 返回（不嵌套 stats）
            st = stop.get("result") or {}
            out.append((f"record.stop 统计含 unaccounted={st.get('unaccounted')}",
                       "unaccounted" in st))
            out.append((f"快捷键停止不裁剪（trimmed={st.get('trimmed')}）",
                       st.get("trimmed", 0) == 0))

        out.extend(_library_checks(s))

        # 后端告警真的能走通知线到界面（2026-09-13）。触发方式要**确定性**且
        # 无副作用：条件节点指向一个不存在的模板 → find_template 在读屏之前就
        # `_warn_once("missing-template:…")` 返回，不碰键鼠、不依赖屏幕录制权限。
        # 少任何一环（handler 没装 / 通知没入队 / 方法名写错）这条都拿不到通知。
        with tempfile.TemporaryDirectory(prefix="autoflow-logchk-") as td:
            wf = os.path.join(td, "wf.json")
            with open(wf, "w", encoding="utf-8") as f:
                json.dump({"version": 2, "name": "log-check", "nodes": [
                    {"type": "condition", "enabled": True, "params": {
                        "check": "图像存在",
                        "image_path": os.path.join(td, "no-such-template.png"),
                        "timeout_s": 0.0}}]}, f, ensure_ascii=False)
            load = s.call("workflow.load", {"path": wf})
            if load.get("error"):
                out.append(("后端 WARNING 经 log.warning 通知送达界面"
                            f"（准备失败：{load['error'].get('message')}）", False))
            else:
                s.clear_notifications()
                s.call("run.start")
                note = s.wait_notification("log.warning", timeout=15.0)
                p2 = (note or {}).get("params") or {}
                detail = (f"（{p2.get('logger')}: "
                          f"{str(p2.get('message'))[:36]}…）" if note else "")
                out.append(("后端 WARNING 经 log.warning 通知送达界面" + detail,
                            note is not None))

        # v4 流程图走一遍真实的 RPC 往返（2026-09-13）。
        # 产物级只能证明「代码进去了」，这里证明「装进去的这份真的按新模型工作」：
        # 边能被存下来、起点能被指定、旧文件迁移会摆开坐标。
        with tempfile.TemporaryDirectory(prefix="autoflow-flowchk-") as td:
            # (1) v4 文件：边与坐标原样保留，且**不**被当成迁移对象
            v4 = os.path.join(td, "v4.json")
            with open(v4, "w", encoding="utf-8") as f:
                json.dump({"version": 4, "name": "flow", "start": "",
                           "nodes": [
                               {"type": "delay", "params": {"ms": 1}, "uid": "a",
                                "enabled": True, "x": 80, "y": 120},
                               {"type": "end", "params": {}, "uid": "b",
                                "enabled": True, "x": 320, "y": 120}],
                           "edges": [{"src": "a", "port": "out", "dst": "b"}]},
                          f, ensure_ascii=False)
            r4 = (s.call("workflow.load", {"path": v4}).get("result") or {}).get("workflow") or {}
            out.append((f"v4 工作流加载后带 1 条边（edges={len(r4.get('edges') or [])}）",
                        len(r4.get("edges") or []) == 1))
            out.append(("v4 工作流不被标记为「由列表迁移」",
                        r4.get("migrated_from_list") is False))
            out.append((f"节点坐标原样带回（x={[n.get('x') for n in r4.get('nodes') or []]}）",
                        [n.get("x") for n in r4.get("nodes") or []] == [80, 320]))

            # (2) v3 文件：自动接成线性边链 **并且摆开坐标**。
            # 只断言「迁移了」是不够的——不摆坐标时画布上是一摞重叠卡片，
            # 用户会以为工作流被毁了，而那正是这次修复要挡住的症状。
            v3 = os.path.join(td, "v3.json")
            with open(v3, "w", encoding="utf-8") as f:
                json.dump({"version": 3, "name": "old", "nodes": [
                    {"type": "delay", "params": {"ms": 1}, "uid": f"n{i}"}
                    for i in range(3)]}, f, ensure_ascii=False)
            r3 = (s.call("workflow.load", {"path": v3}).get("result") or {}).get("workflow") or {}
            coords = [(n.get("x"), n.get("y")) for n in r3.get("nodes") or []]
            out.append(("v3 文件加载后被标记为「由列表迁移」",
                        r3.get("migrated_from_list") is True))
            out.append((f"v3 文件接成线性边链（{len(r3.get('edges') or [])} 条边）",
                        len(r3.get("edges") or []) == 2))
            out.append((f"迁移的节点坐标已摆开、不重叠（{coords}）",
                        len(set(coords)) == 3 and all(c != (0, 0) for c in coords)))

            # (3) 画布方法真的挂在 RPC 线上（方法表里有名字 ≠ 实现能用）。
            #
            # 必须**重新加载 v4 文件**：上面第 (2) 步把当前工作流换成了 v3 那个，
            # 它的节点 uid 是 n0/n1/n2，而下面全部用 a/b 寻址。忘了重载时
            # `_node_by_uid("a")` 会抛「节点不存在」，三条断言一起假失败
            # （实测踩到：node.setPos 落在 None,None、edge.remove 一条没删、
            # start 仍为 None，看着像后端坏了，其实是脚本自己找错了工作流）。
            s.call("workflow.load", {"path": v4})
            rp = s.call("node.setPos", {"uid": "a", "x": 11, "y": 22})
            pos = ((rp.get("result") or {}).get("workflow") or {}).get("nodes") or []
            got = next((n for n in pos if n.get("uid") == "a"), {})
            out.append((f"node.setPos 生效（a 落在 {got.get('x')},{got.get('y')}）",
                        (got.get("x"), got.get("y")) == (11, 22)))
            # 不存在的 uid 必须**报错**，不能静默成功：静默成功意味着用户拖一个
            # 已被删掉的节点时界面毫无反应、也没有任何提示，只能靠猜。
            ghost = s.call("node.setPos", {"uid": "ghost", "x": 1, "y": 1})
            out.append((f"node.setPos 对不存在的 uid 报错（返回 "
                        f"{(ghost.get('error') or {}).get('code')}）",
                        (ghost.get("error") or {}).get("code") == -32602))
            # 边的落点侧必须真能存下来（2026-09-15）。旧 sidecar 会**静默忽略**
            # 这个参数并回一个不含 dst_side 的边——画布于是画在默认侧，
            # 从外面看和「用户自己连到左边」一模一样，不报任何错。
            se = s.call("edge.add", {"src": "a", "dst": "b", "port": "out",
                                     "dst_side": "bottom"})
            se_edges = ((se.get("result") or {}).get("workflow") or {}).get("edges") or []
            got_side = se_edges[0].get("dst_side") if se_edges else None
            out.append((f"edge.add 存下 dst_side（得到 {got_side!r}）",
                        se.get("error") is None and got_side == "bottom"))
            # 脏值必须**收敛**到默认侧而不是原样存：存了前端就找不到对应手柄，
            # 那条边直接画不出来（比画错一侧严重得多）。"right" 也是脏值——
            # 右边是输出侧，会与出口手柄同点重合，故不在词表里。
            sd = s.call("edge.add", {"src": "a", "dst": "b", "port": "out",
                                     "dst_side": "right"})
            sd_edges = ((sd.get("result") or {}).get("workflow") or {}).get("edges") or []
            dirty_side = sd_edges[0].get("dst_side") if sd_edges else None
            out.append((f"edge.add 把脏落点侧收敛到 left（得到 {dirty_side!r}）",
                        sd.get("error") is None and dirty_side == "left"))
            # 自环必须被拒（-32602）；接受了会让执行器原地打转
            loop = s.call("edge.add", {"src": "a", "dst": "a", "port": "out"})
            out.append((f"edge.add 拒绝自环（返回 {(loop.get('error') or {}).get('code')}）",
                        (loop.get("error") or {}).get("code") == -32602))
            rm = s.call("edge.remove", {"src": "a", "port": "out"})
            left = ((rm.get("result") or {}).get("workflow") or {}).get("edges") or []
            # 必须同时判「没报错」：老 sidecar 会返回 -32601，此时 result 为空、
            # 边数为 0，只看条数会**假通过**（实测踩到过）。
            out.append((f"edge.remove 按 (src,port) 删边（剩 {len(left)} 条）",
                        rm.get("error") is None and len(left) == 0))
            st = s.call("workflow.setStart", {"uid": "b"})
            got_start = ((st.get("result") or {}).get("workflow") or {}).get("start")
            out.append((f"workflow.setStart 生效（start={got_start}）", got_start == "b"))

            # 节点改动按 uid 寻址（2026-09-15）。**故意送错的下标 + 正确的 uid**：
            # 老 sidecar 只认下标，会照下标 0 把 a 删掉；新 sidecar uid 优先，
            # 死的是 b。用 b 当靶子是因为它同时是上面 setStart 设的起点——
            # 删掉它之后 start 必须被清空，顺带把那条规则也验了。
            rm2 = s.call("node.remove", {"index": 0, "uid": "b"})
            wf2 = (rm2.get("result") or {}).get("workflow") or {}
            left_uids = [n.get("uid") for n in wf2.get("nodes") or []]
            out.append((f"node.remove 按 uid 寻址而不是下标（剩下 {left_uids}）",
                        rm2.get("error") is None and left_uids == ["a"]))
            out.append((f"删掉起点节点后 start 被清空（start={wf2.get('start')!r}）",
                        wf2.get("start") == ""))
            ghost_rm = s.call("node.remove", {"index": 0, "uid": "nope"})
            out.append((f"node.remove 对不存在的 uid 报错（返回 "
                        f"{(ghost_rm.get('error') or {}).get('code')}）",
                        (ghost_rm.get("error") or {}).get("code") == -32602))

        # 条件组放在**最后**：它会把当前工作流换成自己的夹具，而上面那些断言
        # 全是按 uid 寻址的（a/b/n0…）。夹在中间会让它们一起假失败——
        # 那正是这一节开头记过的坑。
        out.extend(_condition_group_checks(s))
        # 「点击位置」只读定义、不换工作流，放在最后是沿用约定（不是必需）
        out.extend(_click_pos_checks(s))
        # 工作流库**会换掉当前工作流**（新建 → 保存 → 打开），必须在最后
        out.extend(_workflow_lib_checks(s))
    finally:
        s.close()
    return out


def main() -> int:
    app = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_APP
    sidecar = os.path.join(app, "Contents", "Resources",
                           "autoflow-sidecar", "autoflow-sidecar")
    if not os.path.exists(sidecar):
        print(f"✗ 找不到 sidecar：{sidecar}")
        return 2

    results = []
    print(f"目标产物：{app}\n")

    print("A. 产物级（读 frozen PYZ）")
    try:
        mods = _load_pyz(sidecar)
        results += _artifact_checks(mods["core.recorder"],
                                    mods["core.inputsource"],
                                    mods["rpc.controller"],
                                    mods["rpc.server"],
                                    mods.get("core.vision"),
                                    mods.get("core.events"),
                                    mods.get("core.executor"),
                                    mods.get("core.recording_lib"),
                                    mods.get("tasks.builtin"),
                                    mods.get("core.workflow_lib"))
    except Exception as e:  # noqa: BLE001
        results.append((f"读 PYZ 失败：{e}", False))
    for desc, ok in results:
        print(f"   {'✓' if ok else '✗'} {desc}")

    print("\nB. RPC 级（真起一次 sidecar）")
    live = []
    try:
        live = _live_checks(sidecar)
    except Exception as e:  # noqa: BLE001
        live.append((f"起 sidecar 失败：{e}", False))
    for desc, ok in live:
        print(f"   {'✓' if ok else '✗'} {desc}")

    bad = [(d, o) for d, o in results + live if not o]
    print("\n结论：" + ("全部通过，安装版确实带上了本次改动"
                       if not bad else f"{len(bad)} 项未通过"))
    for d, _ in bad:
        print(f"  ✗ {d}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
