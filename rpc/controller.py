"""后端应用控制器：持有 current_workflow 作为唯一真源（§10）。

前端近乎无状态：任何结构操作（增删改移/启停/改参）→ 调用这里 → 改树 →
通过 on_notify 广播 `workflow.changed`。运行/录制/热键/定时等状态也在这里。

本模块只装业务逻辑，不碰 stdio/JSON-RPC 传输（见 rpc/server.py）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from core.events import (MacroEvent, Node, Workflow, Edge, PORT_OUT,
                         DEFAULT_TARGET_SIDE, normalize_target_side)
from core import executor as _executor_mod  # 仅类型/构造；真正运行在 P1-2
from core import recording_lib, workflow_lib

logger = logging.getLogger(__name__)

# 节点菜单顺序（§9.4）**不再在这里维护**：唯一真源是各节点的 order
# （tasks/base.py::all_definitions）。此前这里有一份 _NODE_ORDER 映射，
# 正好把 tasks/ 里错误的注册顺序掩盖住了。

# 通用参数（所有节点尾部渲染，后端为真源）。
#
# v4 起**故意为空**：原来的「执行条件 run_when」（总是/条件成立/条件不成立）是
# 「线性列表 + 一个全局条件标志」时代的产物，流程图模式下这个语义由**边**表达
# （条件的 true/false 出口），留着它会出现两套互相打架的分支机制。
# 旧文件里残留的 `run_when` 参数值**不做清理**——它只是不再渲染、不再被读取，
# 清掉反而会让用户以为自己的配置被吞了。
COMMON_PARAMS: list = []

# 热键物理键顺序：F9/F10/F11 依次对应 record/run/pick（与现状 ui/main_window 一致）。
_HOTKEY_KEYS = ["F9", "F10", "F11"]
_HOTKEY_ACTIONS = {"record", "run", "pick"}
_VALID_SCHEDULE_MODES = {"每天时刻", "固定间隔"}

# 文本链路自检的探针载荷：零宽空格。
# 选它是因为**非 ASCII**——会被 is_text_commit 判为文本提交，正是要验证的分支；
# 同时它在任何应用里都不显示任何内容，投递到当前焦点窗口没有可见副作用。
_PROBE_TEXT = "\u200b"


class ControllerError(Exception):
    """业务错误，携带 JSON-RPC 错误码。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _err(code: int, message: str, data: Any = None) -> tuple[bool, dict]:
    """handler 失败约定：返回 (False, {code,message,data})，由 server 转 _send_error。"""
    return (False, {"code": code, "message": message, "data": data})


class AppController:
    def __init__(self) -> None:
        self.workflow = Workflow()
        self.path: Optional[str] = None
        # ---- 「有没有未保存的改动」（见 workflow_dirty） ----
        # `_wf_rev` 由 `_broadcast_workflow` 自增，`_wf_saved_rev` 在每次落盘后对齐。
        # 不靠前端自己记「我改过没有」：改动接口有十几个，漏掉一个就会出现
        # 「明明改了、界面显示已保存」——而保存按钮不会亮，用户的改动静默丢失。
        self._wf_rev = 0
        self._wf_saved_rev = 0
        self.executor = _executor_mod.Executor()
        self.recorder = None  # 在 P1-2 装入 Recorder
        self._last_record = None
        self._record_undo: list = []   # 事件编辑的撤销栈（快照，上限见 _UNDO_LIMIT）
        # ---- 录制库（见 core/recording_lib.py） ----
        # `_record_rev` 每编辑一次 +1，用来判断「库里的那份还是不是最新的」：
        # 节点按 id 引用库，编辑完不推送更新的话，回放跑的还是旧序列——
        # 界面上看不出任何异常。
        self._record_rev = 0
        self._record_saved: Optional[dict] = None   # {id, name, rev} 最近一次入库
        # 非空 = 正在「直接编辑库里的这条」（见 record_lib_edit）：编辑收尾会把
        # 结果写回库里那条，而不是只留在内存里等用户点「更新到库」。
        self._record_lib_edit_id: Optional[str] = None
        self._lib_cache: Optional[list] = None      # 库列表缓存，写操作置空（见 _lib_metas）
        self._rec_poller = None
        self._rec_poller_rec = None
        self._run_thread = None  # 运行线程引用：shutdown 需要等它收尾
        self.running = False
        self.recording = False
        self._record_subscribed = False  # §3.2：仅订阅后推送 record.event
        self._lock = threading.RLock()
        self._notify: Callable[[str, Any], None] = lambda _m, _p: None

        # ---- 热键 / 键盘捕获（P1-3，§3.2）----
        # 单一 MacKeyboardListener，按 _key_mode 在「热键分发」与「按键捕获」间切换，
        # 捕获期间暂挂热键分发（复用同一 listener，避免捕获键被当热键触发）。
        self._key_listener = None
        self._key_mode = "idle"        # idle | hotkey | capture
        self._hotkey_map: dict = {}    # 物理键名 -> 动作（record/run/pick）
        self._capturing = False
        self._last_hotkey_key: Optional[str] = None   # key-repeat 去抖
        self._last_hotkey_at = 0.0
        self._snipping = False         # screencapture -i 框选中
        self._probing = False          # 输入监控自检进行中
        self._probe_text_hit = False   # 自检的文本段命中标记（见 input_probe）
        self._probe_hit = False
        self._picking_once = False     # 「取点」armed：下一个按键即基点

        # ---- 取点（F11，base.pick）由 pynput 直接读全局光标位置 ----

        # ---- 定时（P1-3，§9.2）：配置持久化到 config.json，后端 threading.Timer 触发 ----
        self._schedule: dict = self._schedule_load()
        self._schedule_timer = None
        if self._schedule:
            # 启动时若已有持久化配置则恢复装定（仅 enabled 且路径有效才真正起计时器）
            self._schedule_arm(self._schedule)

    # ---- 通知 ----
    def set_notifier(self, fn: Callable[[str, Any], None]) -> None:
        self._notify = fn

    def _public_node(self, node: Node) -> dict:
        """对外摘要：events 数组（可达数十 MB）替换为 {count}。

        前端从不读写事件数组（录制结果只看条数，保存/回放都由后端完成），
        全量数组会随 workflow.changed 广播，每次改参数都重传几十 MB——
        既拖慢管道又挤占通知队列。真源仍是后端内存树，save 时完整落盘。

        引用录制库的节点额外补一个 `recording_name`：节点里只存 id，画布上直接
        显示一串 `20260915-190433-a1b2c3` 没人认得出来是哪段操作（走缓存，
        不会因为每次改参数都全库扫一遍）。
        """
        params = node.params
        evs = params.get("events")
        if isinstance(evs, list):
            # 【不可变更原树】node.to_dict() 的 params 是原引用，直接改会摧毁
            # 真源里的 events（运行报 'str' object has no attribute 'get'，
            # 且保存即数据丢失）——必须另建浅拷贝视图。
            params = {**params, "events": {"count": len(evs)}}
        if node.type == "record_replay" and str(params.get("source") or "") == "录制库":
            rid = str(params.get("recording_id") or "")
            hit = next((m for m in self._lib_metas() if m.get("id") == rid), None)
            params = {**params,
                      "recording_name": (hit or {}).get("name") or "",
                      "recording_count": (hit or {}).get("count") or 0}
        return {"type": node.type, "params": params, "enabled": node.enabled,
                "uid": node.uid, "name": node.name, "x": node.x, "y": node.y}

    def _public_workflow(self) -> dict:
        with self._lock:
            return {
                **{k: v for k, v in self.workflow.to_dict().items() if k != "nodes"},
                "nodes": [self._public_node(n) for n in self.workflow.nodes],
                # 迁移标记**不落盘**（to_dict 刻意不带），但必须让界面看到——
                # 否则用户打开旧文件后会发现「条件不再门控了」而完全不知道为什么。
                "migrated_from_list": self.workflow.migrated_from_list,
                # 同上：dirty 不属于 Workflow 的数据模型、不落盘，但**每次广播都
                # 必须带上**。它只在这里出现的话，用户改完一个参数后标题栏的
                # 未保存标记不会亮（改动是经 workflow.changed 广播过去的）。
                "dirty": self.workflow_dirty(),
            }

    def workflow_dirty(self) -> bool:
        """当前工作流有没有未落盘的改动。

        判据是**后端自增的版本号**，不是前端记的账：前端只在收到 RPC 响应时
        知道「刚做了个改动」，而后台也有改工作流的路径（定时任务直接换掉整棵树，
        见 `_schedule_fire`），那条路上前端什么都没发过。
        """
        return self._wf_rev != self._wf_saved_rev

    def _broadcast_workflow(self) -> None:
        # 自增放在广播这里，而不是逐个改动接口：16 个改动接口**全部**经由它广播，
        # 钩一处就全盖住了。放进各个接口的话，将来加第 17 个接口时忘了加一行，
        # 症状是「改了但保存按钮不亮」——静默的数据丢失，最难查。
        self._wf_rev += 1
        self._notify("workflow.changed", self._public_workflow())

    def _mark_saved(self) -> None:
        """把「已保存」的基准对齐到当前版本（落盘后、载入后调用）。"""
        self._wf_saved_rev = self._wf_rev

    def _guard_unsaved(self, force: bool) -> None:
        """丢弃当前工作流之前（新建 / 换一份打开）挡住未保存的改动。

        这个判断刻意放在**后端**：前端确认框是体验，后端这一层是保证。前端漏了
        （或将来多出第二个调用方）就会静默丢掉用户刚连好的一整张图，而那之后
        没有任何地方能找回来——`workflow.new` 不落盘、也没有撤销。
        """
        if self.workflow_dirty() and not force:
            raise ControllerError(-32006, "unsaved_changes",
                                  {"path": self.path, "name": self.workflow.name})

    # ---- workflow.* ----
    def workflow_current(self) -> dict:
        return {
            "workflow": self._public_workflow(),
            "path": self.path,
            "running": self.running,
            "recording": self.recording,
            "dirty": self.workflow_dirty(),
            # 当前这份对应库里哪一条（空 = 还没入库，保存时会自动建一条）
            "lib_id": workflow_lib.id_of_path(self.path),
            "lib_dir": workflow_lib.workflows_dir(),
        }

    def workflow_load(self, path: str) -> dict:
        """按路径打开一份工作流。**刻意不做未保存保护**。

        这是低层的「按路径打开」原语，界面上已经不再直接调它了（「打开」走
        `workflow_lib_open`，那里有保护）。在这层加保护会波及所有按路径加载的
        调用方——`scripts/check_installed.py` 就要在一段会话里连续 load 好几个
        样本文件，中途被一个确认框式的错误码挡下来，表现为「安装版校验突然红了」，
        而原因跟被测功能毫无关系。
        """
        if not path or not isinstance(path, str):
            raise ControllerError(-32602, "参数无效：path 必填")
        if not os.path.exists(path):
            raise ControllerError(-32602, "文件不存在", {"path": path})
        with self._lock:
            self.workflow = Workflow.load(path)
            self.path = path
        self._broadcast_workflow()
        self._mark_saved()      # 刚从磁盘读上来的，此刻是干净的
        return self.workflow_current()

    def workflow_save(self, path: Optional[str] = None) -> dict:
        """保存当前工作流。

        没给 path 时：
        - 当前这份**已经在库里** → 覆盖库里那一条；
        - 全新的（`workflow_new` 之后）→ **先在库里建一条**。

        最后这条是这次改动的核心：「新建 → 保存」此前**必然失败**——`workflow_new`
        把 `self.path` 清成 None，这里再抛「尚未指定保存路径」，用户必须先
        「另存为」才能存下第一版。现在保存按钮在任何时候都可用。
        """
        target = path or self.path
        with self._lock:
            if target and not workflow_lib.id_of_path(target):
                # 外部路径（导入/导出通道）：写普通工作流文件，不带库的元信息
                self.workflow.save(target)
                self.path = target
                item = None
            else:
                wid = workflow_lib.id_of_path(target) or ""
                item = workflow_lib.save(self.workflow.name, self.workflow.to_dict(),
                                         wid=wid or None)
                self.path = workflow_lib.path_of(item["id"])
            self._mark_saved()
        return {"path": self.path, "lib_id": workflow_lib.id_of_path(self.path),
                "dirty": False, "item": item}

    def workflow_new(self, force: bool = False) -> dict:
        """新建一份空工作流。

        **不立刻落盘**：新建即入库的话，「点开看看」也会在库里留下一个空文件，
        试几次就攒出一堆「未命名工作流」把列表糊住。真正的落盘推迟到第一次
        「保存」——由 `workflow_save` 自动建一条，所以保存按钮照样一直可用。
        """
        self._guard_unsaved(force)
        with self._lock:
            self.workflow = Workflow()
            self.path = None
        self._broadcast_workflow()
        self._mark_saved()
        return self.workflow_current()

    def workflow_update(self, patch: dict) -> dict:
        if not isinstance(patch, dict):
            raise ControllerError(-32602, "参数无效：patch 须为对象")
        with self._lock:
            if "name" in patch and isinstance(patch["name"], str):
                self.workflow.name = patch["name"]
            if "speed" in patch:
                try:
                    self.workflow.speed = float(patch["speed"])
                except (TypeError, ValueError):
                    raise ControllerError(-32602, "speed 须为数字")
            if "repeat" in patch:
                try:
                    self.workflow.repeat = max(int(patch["repeat"]), 1)
                except (TypeError, ValueError):
                    raise ControllerError(-32602, "repeat 须为整数")
        self._broadcast_workflow()
        return self.workflow_current()

    # ---- workflow.lib.*（core/workflow_lib.py）：流程图变成库里可管理的资产 ----
    def workflow_lib_list(self) -> dict:
        """列出库里的工作流。

        `current` 只给 id（空 = 还没入库），名字由前端从 `workflow.name` 取：
        库里那份列表项也可能对不上（用户手工改过文件名），再传一份名字就有了
        第二份真源，而两份对不上的表现是「标题栏和列表里显示的是两份不同的流程」。
        """
        return {"items": workflow_lib.list_meta(),
                "current": workflow_lib.id_of_path(self.path),
                "dirty": self.workflow_dirty(),
                "dir": workflow_lib.workflows_dir()}

    def workflow_lib_save(self, name: str = "") -> dict:
        """把**当前画布上的**这份存进库。

        已经在库里 → 覆盖那一条（保持 id 不变）；还没入库 → 新建一条。
        覆盖而不是总新建，是为了让「保存」符合直觉：用户改完一份流程按保存，
        期望的是那一份被更新，不是库里多出一份几乎一样的。
        """
        with self._lock:
            wid = workflow_lib.id_of_path(self.path)
            new_name = str(name or "").strip() or self.workflow.name
            self.workflow.name = new_name
            meta = workflow_lib.save(new_name, self.workflow.to_dict(), wid=wid or None)
            self.path = workflow_lib.path_of(meta["id"])
        # 名字可能变了（用户在库里改了名再保存），标题栏要跟着走
        self._broadcast_workflow()
        self._mark_saved()
        return {**self.workflow_current(), "item": meta}

    def workflow_lib_open(self, wid: str = "", force: bool = False) -> dict:
        """打开库里的一条。未保存的改动会先被拦下（`force=True` 才丢弃）。"""
        try:
            path = workflow_lib.path_of(wid)
        except ValueError as e:
            raise ControllerError(-32602, str(e)) from None
        if not os.path.exists(path):
            raise ControllerError(-32005, f"工作流不存在：{wid}")
        self._guard_unsaved(force)
        with self._lock:
            self.workflow = Workflow.load(path)
            self.path = path
        self._broadcast_workflow()
        self._mark_saved()
        return self.workflow_current()

    def workflow_lib_rename(self, wid: str = "", name: str = "") -> dict:
        if not wid:
            raise ControllerError(-32602, "参数无效：缺 id")
        try:
            meta = workflow_lib.rename(wid, name)
        except ValueError as e:
            raise ControllerError(-32005, str(e)) from None
        # 改的正好是当前这份：标题栏得跟着变，否则用户会以为没改成功
        if workflow_lib.id_of_path(self.path) == meta["id"]:
            with self._lock:
                self.workflow.name = meta["name"]
            self._broadcast_workflow()
            # 改名不改内容——上面那次广播把 rev 推高了，但磁盘上已经是最新的，
            # 不重新对齐的话标题栏会平白多出一个「未保存」标记。
            self._mark_saved()
        return {"item": meta, "current": workflow_lib.id_of_path(self.path),
                "dirty": self.workflow_dirty()}

    def workflow_lib_delete(self, wid: str = "") -> dict:
        """删除库里的一条（移进废纸篓）。

        删掉的正好是当前这份时，**只把路径清空、不动内存里的树**：文件没了但
        用户连的那张图还在，他仍可以继续改、再「保存」回库（会自动新建一条）。
        直接清空工作流的话，一次误点就白丢一张图——而废纸篓里那份的 id 和内存里
        这份已经对不上了，恢复出来也只是个陌生文件。
        """
        if not wid:
            raise ControllerError(-32602, "参数无效：缺 id")
        try:
            dest = workflow_lib.remove(wid)
        except ValueError as e:
            raise ControllerError(-32005, str(e)) from None
        with self._lock:
            if workflow_lib.id_of_path(self.path) == wid:
                self.path = None
        return {"deleted": wid, "trash": dest,
                "current": workflow_lib.id_of_path(self.path),
                "dirty": self.workflow_dirty()}

    def workflow_lib_import(self, path: str = "", name: str = "") -> dict:
        """把一个外部 JSON 工作流文件拷进库。**不切换当前工作流**。

        用户按的是「导入」，期望是「它出现在列表里」，不是「画布上正在编辑的东西
        被换掉了」。要打开就在列表里再点一下「打开」。
        """
        try:
            meta = workflow_lib.import_file(path, name)
        except ValueError as e:
            raise ControllerError(-32005, str(e)) from None
        return {"item": meta, "items": workflow_lib.list_meta()}

    def workflow_lib_export(self, path: str = "", wid: str = "") -> dict:
        """把库里的一条写成普通工作流文件。`wid` 空 = 导出**当前**这份。

        导出的当前那份走**内存里的树**，不是库里那份：用户按导出时想拿到的是
        「画布上现在这样」。同时**不顺手写库**——按「导出」不该把库里那份也改掉，
        用户可能只是想发一份中间稿出去。
        """
        cur = workflow_lib.id_of_path(self.path)
        wid = wid or cur
        if not wid:
            raise ControllerError(-32602, "当前工作流还没入库，请先保存")
        if not path or not isinstance(path, str):
            raise ControllerError(-32602, "参数无效：path 必填")
        try:
            if wid == cur:
                workflow_lib.write_plain(self.workflow.to_dict(), path)
            else:
                workflow_lib.export_file(wid, path)
        except ValueError as e:
            raise ControllerError(-32005, str(e)) from None
        return {"path": path, "id": wid}

    # ---- node.* ----
    def _node_at(self, index: int) -> Node:
        if not isinstance(index, int) or index < 0 or index >= len(self.workflow.nodes):
            raise ControllerError(-32602, "节点索引越界", {"index": index})
        return self.workflow.nodes[index]

    def _index_of(self, index: int = -1, uid: str = "") -> int:
        """把 (uid, index) 解析成**当前**列表下标。**uid 优先**。

        v4 起节点有稳定 uid，前端不该再把它换算成下标送过来。下标是「当前
        这一份列表里的位置」，而前端手上那份可能比后端旧一步——例如定时任务
        刚换掉整份工作流、`workflow.changed` 还没被前端处理完。这时前端按
        旧列表算出的下标会指向**另一个节点**，于是删掉/改错节点，**不报任何错**。
        uid 在节点真被删掉之前一直有效，没有这个窗口。

        画布上一次删多个节点时更明显：`onNodesChange` 会把每个 remove 都
        发一次请求，而它们都按**同一份旧状态**算下标——第二笔起必然错位。
        """
        if uid:
            for i, n in enumerate(self.workflow.nodes):
                if n.uid == uid:
                    return i
            raise ControllerError(-32602, "节点不存在", {"uid": uid})
        if not isinstance(index, int) or index < 0 or index >= len(self.workflow.nodes):
            raise ControllerError(-32602, "节点索引越界", {"index": index})
        return index

    def node_add(self, type_name: str, index: Optional[int] = None,
                 x: Optional[int] = None, y: Optional[int] = None) -> dict:
        import tasks.builtin  # 确保节点已注册
        from tasks.base import get_task
        if not get_task(type_name):
            raise ControllerError(-32602, "未知节点类型", {"type": type_name})
        with self._lock:
            node = Node(type=type_name, params={}, enabled=True)
            if index is None or index >= len(self.workflow.nodes):
                self.workflow.nodes.append(node)
                inserted = len(self.workflow.nodes) - 1
            else:
                inserted = max(0, index)
                self.workflow.nodes.insert(inserted, node)
            # 坐标：前端没给就**错开摆**。这不是布局，只是避免新节点全叠在
            # 同一点上（画布上会看起来像「加了没反应」）。真正的布局在前端。
            node.x = int(x) if x is not None else 80 + (inserted % 4) * 200
            node.y = int(y) if y is not None else 80 + (inserted // 4) * 130
        self._broadcast_workflow()
        return {**self.workflow_current(), "index": inserted, "node": self._public_node(node)}

    def node_remove(self, index: int = -1, uid: str = "") -> dict:
        with self._lock:
            i = self._index_of(index, uid)
            node = self.workflow.nodes[i]
            self.workflow.nodes.pop(i)
            # **必须同时清掉连着它的边**：留着的话，用户看到的是画布上悬空的
            # 连线（视觉上像还在连），而执行器走到那儿会因为找不到节点直接断掉
            # 整条路径——两种表现对不上，是最难查的那类不一致。
            self.workflow.edges = [e for e in self.workflow.edges
                                   if e.src != node.uid and e.dst != node.uid]
            if self.workflow.start == node.uid:
                self.workflow.start = ""
        self._broadcast_workflow()
        return self.workflow_current()

    def node_move(self, index: int, to: int) -> dict:
        with self._lock:
            node = self._node_at(index)
            if not isinstance(to, int) or to < 0 or to >= len(self.workflow.nodes):
                raise ControllerError(-32602, "目标索引越界", {"to": to})
            self.workflow.nodes.pop(index)
            self.workflow.nodes.insert(to, node)
        self._broadcast_workflow()
        return self.workflow_current()

    def node_rename(self, index: int = -1, name: str = "", uid: str = "") -> dict:
        name = str(name or "").strip()[:50]
        with self._lock:
            self.workflow.nodes[self._index_of(index, uid)].name = name
        self._broadcast_workflow()
        return self.workflow_current()

    def node_toggle(self, index: int = -1, enabled: bool = True, uid: str = "") -> dict:
        with self._lock:
            node = self.workflow.nodes[self._index_of(index, uid)]
            node.enabled = bool(enabled)
        self._broadcast_workflow()
        return self.workflow_current()

    def node_params_set(self, index: int = -1, key: str = "", value: Any = None,
                        uid: str = "") -> dict:
        if not isinstance(key, str):
            raise ControllerError(-32602, "参数 key 须为字符串")
        with self._lock:
            node = self.workflow.nodes[self._index_of(index, uid)]
            node.params[key] = value
        self._broadcast_workflow()
        return self.workflow_current()

    # ---- 画布：节点坐标（v4 起节点有 x/y，前端画布用） ----
    def _node_by_uid(self, uid: str) -> Node:
        for n in self.workflow.nodes:
            if n.uid == uid:
                return n
        raise ControllerError(-32602, "节点不存在", {"uid": uid})

    def node_set_pos(self, uid: str, x: Any, y: Any) -> dict:
        """保存节点在画布上的坐标。

        **只该在拖拽结束时调一次**（前端挂在 drag-stop 上）。逐帧上报会把整份
        工作流广播几十次，`workflow.changed` 刷满通知队列，把运行状态类的通知
        挤掉——那条丢一条界面就永久停在旧状态。
        """
        try:
            px, py = int(round(float(x))), int(round(float(y)))
        except (TypeError, ValueError):
            raise ControllerError(-32602, "坐标须为数字", {"x": x, "y": y})
        with self._lock:
            node = self._node_by_uid(str(uid or ""))
            node.x, node.y = px, py
        self._broadcast_workflow()
        return self.workflow_current()

    # ---- 画布：边 ----
    def edge_add(self, src: str, dst: str, port: str = PORT_OUT,
                 dst_side: str = DEFAULT_TARGET_SIDE) -> dict:
        """连一条边。同一个 (源节点, 出口) 只保留一条——**后连的替换先连的**。

        不允许一个出口扇出多条：那要引入并行执行，而并行和「顺序执行 + 汇合」
        是两套模型，混在一起没人能预测行为。要并行请拆成两条路径再用汇合节点收回。

        `dst_side` 只影响画布从哪一侧画这条线（用户在哪一侧松手），**不参与执行**；
        脏值收敛到默认侧，否则前端找不到手柄、这条边根本画不出来。
        """
        src, dst, port = str(src or ""), str(dst or ""), str(port or PORT_OUT)
        side = normalize_target_side(dst_side)
        if not src or not dst:
            raise ControllerError(-32602, "src / dst 必填")
        if src == dst:
            raise ControllerError(-32602, "不能连到自己", {"uid": src})
        with self._lock:
            self._node_by_uid(src)
            self._node_by_uid(dst)
            self.workflow.edges = [e for e in self.workflow.edges
                                   if not (e.src == src and e.port == port)]
            self.workflow.edges.append(Edge(src=src, port=port, dst=dst, dst_side=side))
        self._broadcast_workflow()
        return self.workflow_current()

    def edge_remove(self, src: str, port: str = PORT_OUT) -> dict:
        src, port = str(src or ""), str(port or PORT_OUT)
        with self._lock:
            self.workflow.edges = [e for e in self.workflow.edges
                                   if not (e.src == src and e.port == port)]
        self._broadcast_workflow()
        return self.workflow_current()

    def workflow_set_start(self, uid: str) -> dict:
        """指定起始节点；传空串 = 回到「第一个 start 节点 / 第一个节点」的默认规则。"""
        uid = str(uid or "")
        with self._lock:
            if uid:
                self._node_by_uid(uid)
            self.workflow.start = uid
        self._broadcast_workflow()
        return self.workflow_current()

    # ---- nodes.definitions（含 common_params，顺序见 §9.3/§9.4） ----
    def nodes_definitions(self) -> list:
        """节点定义列表，顺序直接沿用 `all_definitions()`（已按 order 排好）。

        这里**不再**自己排一次：菜单顺序的唯一真源是各节点的 `order`（§9.4），
        后端再维护一份映射只会和 tasks/ 悄悄不一致。
        """
        import tasks.builtin  # 确保节点已注册
        from tasks.base import all_definitions
        out = []
        for d in all_definitions():
            d = dict(d)
            d["common_params"] = list(COMMON_PARAMS)
            out.append(d)
        return out

    # ---- run.*（Executor 包装，§5/§9.5） ----
    @staticmethod
    def node_digest(node) -> str:
        """节点参数的一句话摘要，给界面「现在在干什么」用。

        只挑对「辨认这一步」最有用的一个字段：条件/分支看判据取值，点击看要找的文字/模板，
        延时看时长，键盘看按键。**不做全参数展开**——面板一行放不下，而且真正需要看细节时
        用户会点开节点看参数面板。
        """
        if node is None:
            return ""
        p = node.params or {}
        t = node.type
        try:
            if t in ("branch", "condition_group"):
                n = int(p.get("case_count" if t == "branch" else "cond_count", 2) or 2)
                key = "case%d_value" if t == "branch" else "cond%d_value"
                vals = [str(p.get(key % i, "") or "") for i in range(1, n + 1)]
                vals = [v for v in vals if v]
                if t == "branch":
                    return "判据：" + " / ".join(vals)
                return "（" + str(p.get("combine", "")) + "）" + " / ".join(vals)
            if t == "condition":
                kind = str(p.get("check", ""))
                val = str(p.get("text") or p.get("image_path") or p.get("label") or "")
                return (kind + "：" + val).strip("：")
            if t == "ocr_click":
                return "文字「" + str(p.get("text", "")) + "」"
            if t == "image_click":
                import os as _os
                return "模板 " + _os.path.basename(str(p.get("image_path", "")))
            if t == "yolo_click":
                return "目标 " + str(p.get("label", "") or "任意")
            if t == "delay":
                ms = float(p.get("ms", 0) or 0)
                if str(p.get("mode", "")) == "等待至时刻":
                    return "等到 " + str(p.get("at", ""))
                return ("%.1fs" % (ms / 1000.0)) if ms >= 1000 else ("%dms" % int(ms))
            if t == "keyboard":
                return "按键 " + str(p.get("keys", "") or p.get("text", ""))
            if t == "mouse":
                return "%s (%s,%s)" % (p.get("action", ""), p.get("x", 0), p.get("y", 0))
        except Exception:  # noqa: BLE001  脏参数不该让状态上报失败
            return ""
        return ""

    def run_start(self, base_x: int = 0, base_y: int = 0) -> dict:
        import tasks.builtin  # 确保节点已注册
        with self._lock:
            if self.running:
                raise ControllerError(-32001, "already_running")
            if self.recording:
                raise ControllerError(-32002, "busy_recording")
            runnable = [n for n in self.workflow.nodes if n.enabled and n.type != "note"]
            if not runnable:
                raise ControllerError(-32004, "workflow_empty")
            wf = self.workflow
            self.running = True

        def on_node(idx, ntype):
            # 带出节点的**自定义名**与参数摘要：界面要回答的是「现在在干什么」，
            # 而 type 只能说「找文字点击」——用户建了六个点击节点，光看类型分不清是哪一个。
            # idx 可能为 -1（异常路径），越界一律当空处理，不能让状态上报把运行搞挂。
            node = wf.nodes[idx] if 0 <= idx < len(wf.nodes) else None
            self._notify("run.node", {
                "index": idx,
                "type": ntype,
                "name": (node.name if node else "") or "",
                "detail": self.node_digest(node),
            })

        def on_progress(done, total):
            self._notify("run.progress", {"done": done, "total": total})

        def on_done(stopped):
            self.running = False
            self._notify("run.finished", {"stopped": bool(stopped)})

        def on_error(idx, ntype, exc):
            self._notify("run.error", {"index": idx, "type": ntype, "message": str(exc)})

        t = threading.Thread(
            target=self.executor.run_workflow,
            args=(wf, base_x, base_y),
            kwargs={"on_node": on_node, "on_progress": on_progress,
                    "on_done": on_done, "on_error": on_error},
            name="run-workflow", daemon=True,
        )
        self._run_thread = t
        t.start()
        return {"running": True}

    def run_stop(self) -> dict:
        self.executor.stop_run()
        t = self._run_thread
        if t is not None and t.is_alive():
            t.join(timeout=5.0)   # 等运行线程真正退出，响应里给出最终状态
        return {"running": self.running}

    # ---- record.*（Recorder 包装，100ms 批量推 record.event，§5 节流） ----
    def record_start(self, window_bounds: Any = None,
                     drop_in_window: bool = False) -> dict:
        """开始录制。

        `window_bounds` 只用于停止时定位"停止按钮那次点击"（尾部裁剪），
        **不再**用于丢弃落于窗口内的事件——v2 的矩形过滤会在边界过期时
        成片吞掉真实操作，是"操作被莫名其妙裁掉"的主因。
        """
        from core.recorder import Recorder
        with self._lock:
            if self.recording:
                raise ControllerError(-32001, "already_running")
            if self.running:
                raise ControllerError(-32002, "busy_recording")
            self.recorder = Recorder(skip_keys=set(self._hotkey_map.keys()),
                                     window_bounds=self._parse_bounds(window_bounds),
                                     drop_in_window=bool(drop_in_window))
            self._record_undo.clear()   # 新录制开始：旧序列的撤销栈已无意义
            self._record_lib_edit_id = None   # 同理，新序列不再属于任何库条目
            self.recorder.start()
            self.recording = True
            self._rec_poller_rec = self.recorder
        self._rec_poller = threading.Thread(target=self._record_poll, name="rec-poll", daemon=True)
        self._rec_poller.start()
        return {"recording": True}

    @staticmethod
    def _parse_bounds(b: Any) -> Optional[tuple]:
        """窗口边界 [x,y,w,h]（逻辑点）。非法输入返回 None（不过滤）。"""
        try:
            if isinstance(b, (list, tuple)) and len(b) == 4:
                return tuple(int(v) for v in b)
        except (TypeError, ValueError):
            pass
        return None

    def record_bounds(self, bounds: Any) -> dict:
        """录制期间窗口移动/缩放时更新边界。"""
        with self._lock:
            if self.recorder is not None:
                self.recorder.set_window_bounds(self._parse_bounds(bounds))
        return {"ok": True}

    def record_subscribe(self, on: bool) -> dict:
        """§3.2：仅当录制面板订阅时才向 stdout 推送 record.event，避免高频事件打爆管道。"""
        with self._lock:
            self._record_subscribed = bool(on)
        return {"subscribed": self._record_subscribed}

    def _record_poll(self) -> None:
        # 仅用启动时刻捕获的 rec 引用；record_stop 会置空 self.recorder，这里绝不回读属性
        rec = self._rec_poller_rec
        while self.recording:
            time.sleep(0.1)
            if not self.recording:
                break
            evs = rec.poll()
            if evs and self._record_subscribed:
                self._notify("record.event", {"events": [asdict(e) for e in evs]})
        # 退出前再 flush 一次残留（仍受订阅门控）
        evs = rec.poll()
        if evs and self._record_subscribed:
            self._notify("record.event", {"events": [asdict(e) for e in evs]})

    def record_stop(self, trim: bool = False, window_bounds: Any = None) -> dict:
        """停止录制。

        `trim=True`（**仅按钮停止**）：把"点停止按钮"这一次点击从序列尾部裁掉，
        否则回放会复现它、点到回放当时该位置上的任意东西。快捷键停止不裁——
        F9 已在 skip_keys 里，根本不进序列，没有"停止点击"可裁。

        `window_bounds` 由前端在**停止那一刻**下发（而不是录制开始时）——
        录制期间用户可能移动过窗口，用开始时的旧边界会裁错位置。
        """
        from core.recorder import trim_stop_interaction
        with self._lock:
            if not self.recording or self.recorder is None:
                raise ControllerError(-32003, "not_recording")
            rec = self.recorder
            self.recorder = None
            self.recording = False
        if window_bounds is not None:
            rec.set_window_bounds(self._parse_bounds(window_bounds))
        result = rec.stop()
        n_trimmed = 0
        if trim:
            kept = trim_stop_interaction(
                result.events, rec._win_bounds, rec.elapsed_ms())
            n_trimmed = len(result.events) - len(kept)
            if n_trimmed:
                # 裁剪是**破坏性**的（还发生在用户看到统计之前），押一份裁剪前的快照，
                # 让 record.undo 能把它整段还原。拿不回数据的静默裁剪不接受。
                #
                # 快照必须是**元素级**副本，不能只 `list(...)`：紧随其后的"轨迹终点
                # 补全"是**原地**改 `events[-1].ts_ms`，而裁剪后最后那条移动在裁剪前
                # 也在序列里（同一个对象）。浅拷贝会让补全同时改到快照，撤销回来就是
                # `[0,10,20,45,40,45]`——被改过的那条时间戳超过了它后面的停止点击，
                # 序列倒退，回放时序出错。只在真裁时才复制，避免给超大序列常态买单。
                before = [replace(e) for e in result.events]
            result.events = kept
        if n_trimmed:
            self._record_undo.append(before)
            if len(self._record_undo) > self._UNDO_LIMIT:
                self._record_undo.pop(0)
        if result.events and result.events[-1].kind == "move":
            elapsed = rec.elapsed_ms()
            if elapsed > result.events[-1].ts_ms:
                result.events[-1].ts_ms = elapsed
        self._last_record = result
        # 丢帧定位计量一并上报：captured = 系统投递数，count = 实际入库数。
        #
        # `unaccounted` 是**唯一**的一致性判据：captured 减去所有"有意移除"的类别
        # 之后必须正好剩 count，不为 0 才是真的系统层（CGEventTap）丢事件。
        #
        # 这个等式以前由前端自己拼（captured − filtered − limit_dropped），于是每
        # 新增一类"有意移除"就会漏扣一次：`trimmed`（4b16c52 修过，v3 重写时把
        # filtered+trimmed 拆开又丢了）和 `text_merged`（新增文本聚合时）都各自
        # 制造过一次"检测到系统层丢事件，请反馈"的假警报。现在只在后端算一次。
        count = len(result.events)
        accounted = (count + result.n_filtered + result.n_limit_dropped
                     + result.n_text_merged + n_trimmed)
        stats = {
            "count": count,
            "origin": [result.origin_x, result.origin_y],
            "stopped_by_limit": result.stopped_by_limit,
            "captured": result.n_captured,
            "filtered": result.n_filtered,
            "decimated": result.n_decimated,
            "window_dropped": result.n_window_dropped,
            "limit_dropped": result.n_limit_dropped,
            "text_merged": result.n_text_merged,
            "trimmed": n_trimmed,
            "unaccounted": result.n_captured - accounted,
            "mouse_died": result.mouse_listener_died,
            "kb_died": result.kb_listener_died,
        }
        # 自动入库：只留在内存里的话，「刚才那段不错」常常是关掉面板之后才发现的，
        # 而那时候已经没了。空录制不入——那只是点了一下按钮，放进库里纯属噪音。
        # 入不进库**不能让「停止录制」本身失败**：事件还在内存里，用户仍能写入节点。
        self._record_rev += 1
        self._record_saved = None
        # 新录制与任何「正在直接编辑的库条目」都无关了：不清的话后续编辑会把
        # 这段新序列写回到旧那条库条目上（`_record_saved` 已换成新的，
        # 而 `_record_lib_edit_id` 还指着旧的）。
        self._record_lib_edit_id = None
        if count:
            try:
                meta = recording_lib.save(recording_lib.default_name(),
                                          [asdict(e) for e in result.events],
                                          (result.origin_x, result.origin_y))
                self._record_saved = {"id": meta["id"], "name": meta["name"],
                                      "rev": self._record_rev}
                stats["library"] = meta
            except Exception as e:  # noqa: BLE001
                logger.warning("录制入库失败：%s", e)
        self._notify("record.stopped", stats)
        return stats

    def record_to_node(self) -> dict:
        res = self._last_record
        if res is None:
            raise ControllerError(-32003, "no_record_result")
        node = Node(type="record_replay", params={
            "events": [asdict(e) for e in res.events],
            "origin_x": res.origin_x, "origin_y": res.origin_y,
            "use_relative": True,
        }, enabled=True)
        with self._lock:
            self.workflow.nodes.append(node)
            inserted = len(self.workflow.nodes) - 1
        self._broadcast_workflow()
        return {**self.workflow_current(), "index": inserted, "node": self._public_node(node)}

    # ---- 录制事件编辑（对 _last_record 就地增删改，写入节点即取编辑后的结果） ----
    # 编辑的是**权威序列**本身：面板删掉一条，写入节点的就是删后的序列，
    # 不存在"面板显示一份、实际写入另一份"的空间。
    _UNDO_LIMIT = 20

    # ---- 事件字段编辑的白名单（见 record_patch / record_insert）----
    # 按 kind 列出**可以改**的字段，不在这里的一律拒绝。`kind` 本身不在任何
    # 白名单里：改 kind 不是「改字段」而是「换一条事件」（坐标/按键的语义完全
    # 不同），要换就用 insert + remove 组合。
    _PATCH_FIELDS = {
        "move": ("ts_ms", "x", "y", "dragged"),
        "mouse": ("ts_ms", "x", "y", "button", "pressed", "clicks"),
        "wheel": ("ts_ms", "x", "y", "wheel_dx", "wheel_dy", "wheel_unit"),
        "key": ("ts_ms", "x", "y", "key", "pressed", "flags"),
        "text": ("ts_ms", "x", "y", "text"),
    }
    _KINDS = tuple(_PATCH_FIELDS)
    _BUTTONS = ("left", "right", "middle")
    _WHEEL_UNITS = ("line", "pixel")
    # 坐标上限：录制来自真实屏幕，不会有这么大的值——挡住手滑输入的天文数字
    # （它会让回放把光标甩到屏幕外，表现成「什么都没发生」）。
    _COORD_LIMIT = 100000

    def _record_or_raise(self):
        res = self._last_record
        if res is None:
            raise ControllerError(-32003, "no_record_result")
        return res

    def _record_snapshot(self) -> None:
        """编辑前存一份快照，供 record.undo 回滚（编辑是破坏性的）。

        ⚠️ 这是**浅拷贝**：快照与真源共享同一批事件对象，所以编辑**不许原地改
        元素**（`ev.text = ...`），必须替换元素（`replace(ev, ...)`）或重建列表，
        否则快照会被一起改掉、撤销永远回不去。
        `record_stop` 的"轨迹终点补全"是唯一会原地改元素的地方，它为此单独做了
        元素级复制（见那里的注释）。新增会原地改事件的代码时，同样要自己复制。

        ⚠️ 这里顺手把 `_record_rev` 加一：所有编辑都必须先调本函数（撤销是唯一
        例外，它自己加），而「库里那份是否已过期」就是拿 rev 比的。新增编辑类
        接口时别绕过这里，否则库里存着旧序列却不提示。
        """
        self._record_rev += 1
        res = self._last_record
        if res is None:
            return
        self._record_undo.append(list(res.events))
        if len(self._record_undo) > self._UNDO_LIMIT:
            self._record_undo.pop(0)

    def _lib_info(self) -> Optional[dict]:
        """当前这段录制对应的库条目（若已入库）。

        `dirty` 由 rev 比对得出，**不靠前端自己记「我改过没有」**：编辑接口有六七个，
        漏掉一个就会出现「明明改了、库里还是旧的，而界面显示已同步」——
        而按 id 引用它的节点回放的是库里那份，用户完全无从察觉。
        """
        s = self._record_saved
        if not s:
            return None
        return {"id": s["id"], "name": s["name"], "dirty": s["rev"] != self._record_rev,
                # 正在「直接编辑库里的这条」（每次编辑自动写回，见 record_lib_edit）。
                # 界面据此显示「编辑中：xxx」和「完成编辑」入口。
                "editing": self._record_lib_edit_id == s["id"]}

    def _record_payload(self) -> dict:
        res = self._last_record
        if res is None:
            return {"count": 0, "events": [], "origin": [0, 0], "duration_ms": 0,
                    "can_undo": False, "lib": None}
        return {
            "count": len(res.events),
            "events": [asdict(e) for e in res.events],
            "origin": [res.origin_x, res.origin_y],
            "duration_ms": res.events[-1].ts_ms if res.events else 0,
            "can_undo": bool(self._record_undo),
            "lib": self._lib_info(),
        }

    def _sync_lib_edit(self) -> None:
        """「直接编辑库条目」模式下，把当前缓冲写回库里那条（见 record_lib_edit）。

        放在**后端**而不是让前端每次编辑后补一刀：编辑接口有十几个，靠前端记账
        **漏一个就会**「库里还是旧的、界面却显示已同步」——而按 id 引用它的节点
        回放的是库里那份，用户完全无从察觉（与 `_lib_info` 用 rev 比对同理）。
        """
        rid = self._record_lib_edit_id
        res = self._last_record
        if not rid or res is None:
            return
        try:
            meta = recording_lib.save(
                (self._record_saved or {}).get("name") or recording_lib.default_name(),
                [asdict(e) for e in res.events],
                (res.origin_x, res.origin_y), rid=rid)
        except ValueError as e:
            # 写不进去（比如文件被手工删了）不该让**编辑本身**失败：事件还在内存里，
            # 用户仍能继续编辑、或另存成新的一条。所以只记 warning。
            logger.warning("直接编辑库条目时写回失败：%s", e)
            return
        self._record_saved = {"id": meta["id"], "name": meta["name"],
                              "rev": self._record_rev}
        self._lib_invalidate()

    def _record_commit(self) -> dict:
        """编辑操作的统一收尾：必要时同步回库，再返回新的权威序列。

        **新增编辑接口时用它收尾，不要直接 `return self._record_payload()`**，
        否则「直接编辑库条目」模式下那一次改动只留在内存里，而界面会因为 rev
        没对齐显示「改动未更新」——用户以为已经改到库里了。
        """
        self._sync_lib_edit()
        return self._record_payload()

    @staticmethod
    def _as_indexes(indexes: Any, count: int) -> list:
        if indexes is None:
            return []
        if isinstance(indexes, int):
            indexes = [indexes]
        if not isinstance(indexes, (list, tuple)):
            raise ControllerError(-32602, "参数无效：indexes 须为整数或数组")
        out = set()
        for v in indexes:
            try:
                i = int(v)
            except (TypeError, ValueError):
                raise ControllerError(-32602, "参数无效：indexes 元素须为整数")
            if 0 <= i < count:
                out.add(i)
        return sorted(out)

    def _coerce_field(self, name: str, value: Any) -> Any:
        """把一个字段值收敛到 `MacroEvent` 能接受的范围；不合法就抛 -32602。

        **非法值一律拒绝，不静默纠正**：坐标填成 "abc" 时若悄悄当成 0，
        回放会把光标甩到屏幕左上角，而用户以为自己填的是对的——这种「看起来
        成功了」的失败最难查。枚举类字段（按钮/滚轮单位）同理，拼错的
        `wheel_unit="px"` 会让回放把「行」当「像素」投递，滚轮几乎不动。
        """
        if name in ("ts_ms", "x", "y", "wheel_dx", "wheel_dy", "flags", "clicks"):
            try:
                v = int(value)
            except (TypeError, ValueError):
                raise ControllerError(-32602, f"参数无效：{name} 须为整数") from None
            if name == "ts_ms" and v < 0:
                raise ControllerError(-32602, "参数无效：时间不能为负")
            if name == "clicks" and v < 1:
                raise ControllerError(-32602, "参数无效：点击次数至少为 1")
            if name in ("x", "y") and abs(v) > self._COORD_LIMIT:
                raise ControllerError(-32602, f"参数无效：{name} 超出合理范围")
            return v
        if name in ("pressed", "dragged"):
            return bool(value)
        if name == "button":
            s = str(value or "")
            if s not in self._BUTTONS:
                raise ControllerError(-32602, "参数无效：按钮须为 left / right / middle")
            return s
        if name == "wheel_unit":
            s = str(value or "")
            if s not in self._WHEEL_UNITS:
                raise ControllerError(-32602, "参数无效：滚轮单位须为 line / pixel")
            return s
        if name == "key":
            s = str(value or "")
            if not s:
                raise ControllerError(-32602, "参数无效：键名不能为空")
            return s
        if name == "text":
            return "" if value is None else str(value)
        raise ControllerError(-32602, f"参数无效：未知字段 {name}")

    def record_remove(self, indexes: Any = None) -> dict:
        """删除指定下标的事件（单个整数或整数数组）。"""
        res = self._record_or_raise()
        idxs = self._as_indexes(indexes, len(res.events))
        if idxs:
            self._record_snapshot()
            drop = set(idxs)
            res.events = [e for i, e in enumerate(res.events) if i not in drop]
        return self._record_commit()

    def record_remove_moves_before(self, index: Any = 0) -> dict:
        """删除 `index` 之前的所有**移动**事件（点击/按键/滚轮保留）。

        用途：录制开头往往有一段"从别处把光标移过来"的移动，回放时会先
        划一道长线。删掉它们即可让回放从第一次实质操作处开始。
        """
        res = self._record_or_raise()
        try:
            idx = int(index)
        except (TypeError, ValueError):
            raise ControllerError(-32602, "参数无效：index 须为整数")
        idx = max(0, min(idx, len(res.events)))
        if any(e.kind == "move" for e in res.events[:idx]):
            self._record_snapshot()
            res.events = [e for i, e in enumerate(res.events)
                          if i >= idx or e.kind != "move"]
        return self._record_commit()

    def record_set_origin(self, index: Any = None) -> dict:
        """把相对坐标的原点设为指定事件的位置（默认取第一个鼠标事件）。

        录制回放节点默认 `use_relative=True`，回放偏移 = 基点 − 原点。
        换原点即换锚，用于把整条轨迹对齐到另一个参照位置。
        """
        res = self._record_or_raise()
        idx = None
        if index is not None:
            try:
                idx = int(index)
            except (TypeError, ValueError):
                raise ControllerError(-32602, "参数无效：index 须为整数")
            if not (0 <= idx < len(res.events)):
                raise ControllerError(-32602, "下标越界")
        if idx is None:
            idx = next((i for i, e in enumerate(res.events)
                        if e.kind in ("move", "mouse", "wheel")), None)
        if idx is None:
            raise ControllerError(-32603, "序列里没有带坐标的事件")
        self._record_snapshot()
        res.origin_x, res.origin_y = res.events[idx].x, res.events[idx].y
        return self._record_commit()

    def record_set_text(self, index: Any = None, text: Any = "") -> dict:
        """改写某条 text 事件的内容。

        用途：输入法提交的内容有时不是想要的（同音字、多打的字），或者自动识别
        把一段输入法编码拆成了多个片段。回放前在这里改掉，比"重录一遍"省事。

        必须**替换元素**而不是原地 `ev.text = ...`：撤销栈存的是
        `list(res.events)`（浅拷贝，元素是同一批对象），原地改会把快照一起改掉，
        撤销就永远回不到旧内容了。
        """
        res = self._record_or_raise()
        try:
            idx = int(index)
        except (TypeError, ValueError):
            raise ControllerError(-32602, "参数无效：index 须为整数")
        if not (0 <= idx < len(res.events)):
            raise ControllerError(-32602, "下标越界")
        ev = res.events[idx]
        if ev.kind != "text":
            raise ControllerError(-32603, "该事件不是文本事件")
        new = "" if text is None else str(text)
        if new == ev.text:
            return self._record_payload()
        self._record_snapshot()
        res.events[idx] = replace(ev, text=new)
        return self._record_commit()

    def record_keys_to_text(self, index: Any = None, text: Any = "") -> dict:
        """把选中的一段**连续按键**换成一条 `text` 事件。

        用途：中文录制走的是"录下拼音按键、回放时重新驱动输入法上屏"这条路
        （2026-09-12 端到端验证成立：录 nihao+空格，回放得到「你好」）。它能用，
        但**不确定**——依赖回放时输入法处于同样状态、且候选顺序一致；输入法没开
        就会打出字面量 "nihao"。

        换成一条 `text` 事件后，回放走 Unicode 通道 `type_text`，不经过输入法，
        结果确定；事件表里也终于能读懂在打什么字。

        范围：从选中下标向两侧扩到**最大连续按键段**（只吞 `kind="key"`），
        整段替换为一条 text 事件，时间戳/坐标取该段第一条（回放起点不变）。
        用户要提供实际文字——IME 提交了什么，事件层无从得知，不能猜。
        """
        res = self._record_or_raise()
        try:
            idx = int(index)
        except (TypeError, ValueError):
            raise ControllerError(-32602, "参数无效：index 须为整数")
        if not (0 <= idx < len(res.events)):
            raise ControllerError(-32602, "下标越界")
        if res.events[idx].kind != "key":
            raise ControllerError(-32603, "该事件不是按键事件")
        new = "" if text is None else str(text)
        if not new:
            raise ControllerError(-32602, "文本不能为空")
        lo = hi = idx
        while lo > 0 and res.events[lo - 1].kind == "key":
            lo -= 1
        while hi + 1 < len(res.events) and res.events[hi + 1].kind == "key":
            hi += 1
        run = res.events[lo:hi + 1]
        # 至少要有一个"按下"：否则这段只是按键抬起（例如上一个动作的尾巴），
        # 把它当成一次输入会张冠李戴。
        if not any(e.pressed for e in run):
            raise ControllerError(-32603, "这段按键里没有按下事件")
        self._record_snapshot()
        first = run[0]
        res.events = res.events[:lo] + [
            MacroEvent(ts_ms=first.ts_ms, kind="text", text=new,
                       x=first.x, y=first.y)] + res.events[hi + 1:]
        return self._record_commit()

    def record_patch(self, index: Any = None, fields: Any = None) -> dict:
        """改一条事件的字段：坐标 / 按键 / 时间 / 按钮 / 点击次数 / 滚轮增量 / 文本。

        按 `_PATCH_FIELDS` 白名单校验，**越界字段直接报错而不是忽略**——
        静默忽略会让人以为改成功了，而回放用的还是旧值。

        必须**替换元素**（`replace(ev, ...)`）而不是原地改：撤销栈存的是浅拷贝，
        原地改会把快照一起改掉，撤销就回不去了（见 `_record_snapshot`）。
        """
        res = self._record_or_raise()
        try:
            idx = int(index)
        except (TypeError, ValueError):
            raise ControllerError(-32602, "参数无效：index 须为整数") from None
        if not (0 <= idx < len(res.events)):
            raise ControllerError(-32602, "下标越界")
        if not isinstance(fields, dict) or not fields:
            raise ControllerError(-32602, "参数无效：fields 须为非空对象")
        ev = res.events[idx]
        allowed = self._PATCH_FIELDS.get(ev.kind)
        if allowed is None:
            raise ControllerError(-32603, f"不支持编辑这类事件：{ev.kind}")
        unknown = sorted(k for k in fields if k not in allowed)
        if unknown:
            raise ControllerError(
                -32602, f"{ev.kind} 事件不能改这些字段：{'、'.join(unknown)}")
        new_vals = {k: self._coerce_field(k, v) for k, v in fields.items()}
        if all(getattr(ev, k) == v for k, v in new_vals.items()):
            return self._record_payload()   # 值没变：不动撤销栈
        self._record_snapshot()
        res.events[idx] = replace(ev, **new_vals)
        return self._record_commit()

    def record_insert(self, index: Any = None, event: Any = None) -> dict:
        """在 `index` 处插入一条新事件（`index == len` 即追加到末尾）。

        时间戳缺省时对齐**前一条**：新插进来的一步不该早于它上面那步，
        否则回放会把它提到前面去执行（排期是绝对时间线，见 `record_move_event`）。
        """
        res = self._record_or_raise()
        try:
            idx = int(index)
        except (TypeError, ValueError):
            raise ControllerError(-32602, "参数无效：index 须为整数") from None
        if not (0 <= idx <= len(res.events)):
            raise ControllerError(-32602, "下标越界")
        if not isinstance(event, dict):
            raise ControllerError(-32602, "参数无效：event 须为对象")
        kind = str(event.get("kind") or "")
        allowed = self._PATCH_FIELDS.get(kind)
        if allowed is None:
            raise ControllerError(-32602, f"不支持的事件类型：{kind or '(空)'}")
        unknown = sorted(k for k in event if k not in allowed and k != "kind")
        if unknown:
            raise ControllerError(-32602, f"不支持的字段：{'、'.join(unknown)}")
        vals = {k: self._coerce_field(k, v) for k, v in event.items() if k != "kind"}
        if "ts_ms" not in vals:
            vals["ts_ms"] = res.events[idx - 1].ts_ms if idx > 0 else 0
        self._record_snapshot()
        res.events = (res.events[:idx] + [MacroEvent(kind=kind, **vals)]
                      + res.events[idx:])
        return self._record_commit()

    def record_duplicate(self, index: Any = None) -> dict:
        """复制一条事件，插在它**后面**。

        复制品沿用同一个 `ts_ms`（忠实复制）——要错开时间请用 `record.patch` 改。
        两条会在同一时刻发出这件事由用户决定，不替他改。
        """
        res = self._record_or_raise()
        try:
            idx = int(index)
        except (TypeError, ValueError):
            raise ControllerError(-32602, "参数无效：index 须为整数") from None
        if not (0 <= idx < len(res.events)):
            raise ControllerError(-32602, "下标越界")
        self._record_snapshot()
        res.events = (res.events[:idx + 1] + [replace(res.events[idx])]
                      + res.events[idx + 1:])
        return self._record_commit()

    def record_move_event(self, index: Any = None, delta: Any = 0) -> dict:
        """把一条事件与**相邻**那条对调（`delta` = -1 上移 / +1 下移）。

        位置与 `ts_ms` **一起交换**。回放按 `due = t0 + ts_ms/speed` 排期，也就是
        **绝对时间线、与数组顺序无关**：只换位置不换时间的话时间线不再单调，
        被挪到前面的那步会因为 `due` 已过而**立刻执行**（甚至赶在它本该排在其后的
        那步之前）。一起换掉时间才是用户要的「把这两步调个个儿」。

        到边界（首条上移 / 末条下移）时**无事发生**，不污染撤销栈。
        """
        res = self._record_or_raise()
        try:
            idx = int(index)
            d = int(delta)
        except (TypeError, ValueError):
            raise ControllerError(-32602, "参数无效：index / delta 须为整数") from None
        if d not in (-1, 1):
            raise ControllerError(-32602, "参数无效：一次只能上移或下移一格")
        j = idx + d
        if not (0 <= idx < len(res.events)) or not (0 <= j < len(res.events)):
            return self._record_payload()
        self._record_snapshot()
        a, b = res.events[idx], res.events[j]
        evs = list(res.events)
        evs[idx], evs[j] = replace(b, ts_ms=a.ts_ms), replace(a, ts_ms=b.ts_ms)
        res.events = evs
        return self._record_commit()

    def record_undo(self) -> dict:
        """回滚上一次编辑。"""
        if not self._record_undo:
            raise ControllerError(-32004, "nothing_to_undo")
        self._last_record.events = self._record_undo.pop()
        self._record_rev += 1   # 撤销同样让库里的那份过期（见 _record_snapshot）
        return self._record_commit()

    def record_current(self) -> dict:
        """返回最近一次录制的**权威**事件序列。

        面板此前只靠 `record.event` 增量推送维护自己的缓冲（上限截断），
        与写入节点的后端全量数据是两份来源，会出现"看到的和写入的不一致"。
        改为面板在录制结束后（以及每次编辑后）拉一次本接口。
        """
        return self._record_payload()

    # ---- 录制库（core/recording_lib.py）：录下来的东西变成可复用资产 ----
    #
    # 节点按 **id 引用**库里的录制，回放时才去取。好处是「在库里改一次，
    # 所有引用它的节点一起变」；代价是节点自己看不出那条录制长什么样，
    # 所以 `_public_node` 会补一个 `recording_name` 给画布摘要用。
    def _lib_metas(self) -> list:
        """库列表（**带缓存**）。

        `_public_node` 每次广播工作流都要查名字，而 `list_meta()` 要读目录下
        所有文件的元信息——放画布每次改参数都全库扫一遍太亏。写操作会置空缓存。
        """
        if self._lib_cache is None:
            self._lib_cache = recording_lib.list_meta()
        return self._lib_cache

    def _lib_invalidate(self) -> None:
        self._lib_cache = None

    def record_lib_list(self) -> dict:
        """列出库里的录制（**不含 events**）。

        一条录制几百 KB，列表把它一起传过去会让「打开面板」变成几 MB 的传输，
        而列表页根本用不到事件内容。
        """
        return {"items": recording_lib.list_meta(),
                "current": self._lib_info(),
                "dir": recording_lib.recordings_dir()}

    def record_lib_save(self, name: str = "", rid: str = "") -> dict:
        """把当前编辑缓冲存进库：`rid` 空 = 新建一条，给了 = 覆盖那一条。

        用「覆盖」而不是「新建」，是为了让「改完推给所有引用它的节点」有落点：
        节点按 id 引用，只有**同一个 id** 被更新，引用才会跟着变。
        """
        res = self._last_record
        if res is None or not res.events:
            raise ControllerError(-32003, "no_record_result")
        meta = recording_lib.save(
            name or (self._record_saved or {}).get("name") or recording_lib.default_name(),
            [asdict(e) for e in res.events],
            (res.origin_x, res.origin_y),
            rid=rid or None,
        )
        self._record_saved = {"id": meta["id"], "name": meta["name"],
                              "rev": self._record_rev}
        # 「另存为新的」（rid 为空）之后当前缓冲就是新那一条了：直接编辑模式的目标
        # 要跟着切，否则继续编辑会写回到**旧**条目上——用户以为在改刚存的那条。
        if self._record_lib_edit_id and not rid:
            self._record_lib_edit_id = meta["id"]
        self._lib_invalidate()
        return {"item": meta, "current": self._lib_info()}

    def record_lib_rename(self, rid: str = "", name: str = "") -> dict:
        if not rid:
            raise ControllerError(-32602, "参数无效：缺 id")
        try:
            meta = recording_lib.rename(rid, name)
        except ValueError as e:
            raise ControllerError(-32005, str(e)) from None
        if self._record_saved and self._record_saved["id"] == rid:
            self._record_saved["name"] = meta["name"]
        self._lib_invalidate()
        return {"item": meta, "current": self._lib_info()}

    def record_lib_delete(self, rid: str = "") -> dict:
        if not rid:
            raise ControllerError(-32602, "参数无效：缺 id")
        try:
            recording_lib.remove(rid)
        except ValueError as e:
            raise ControllerError(-32005, str(e)) from None
        if self._record_saved and self._record_saved["id"] == rid:
            # 库里那条没了，但内存里的事件还在——用户仍可「写入节点」或重新存一条。
            self._record_saved = None
        self._lib_invalidate()
        return {"deleted": rid, "current": self._lib_info()}

    def record_from_lib(self, rid: str = "") -> dict:
        """把库里的一条**载入编辑缓冲**，之后可继续编辑 / 更新 / 写入节点。

        这会替换掉 `_last_record`（「载入」就是这个意思），并把 `_record_saved`
        指到这条上——rev 与 saved rev 因此对齐，界面显示「已同步」。
        """
        if not rid:
            raise ControllerError(-32602, "参数无效：缺 id")
        try:
            data = recording_lib.load(rid)
        except ValueError as e:
            raise ControllerError(-32005, str(e)) from None
        # 与回放走**同一个**容忍构造：载入面板看到的条数必须等于回放实际用的条数，
        # 否则「面板上删到 20 条、回放却跑了 22 条」这种对不上的账没处查。
        from core.events import RecordResult
        from tasks.builtin import tolerant_event
        events = [e for e in (tolerant_event(x) for x in data["events"]) if e]
        ox, oy = data.get("origin") or [0, 0]
        self._last_record = RecordResult(events=events,
                                         origin_x=int(ox), origin_y=int(oy))
        self._record_undo.clear()
        # 载入会**退出**「直接编辑库条目」模式。调用方若是 record_lib_edit，
        # 它会紧接着再设回来（见那里）——否则用户点「载入」换了一条之后，
        # 后续编辑还在往旧那条上写回。
        self._record_lib_edit_id = None
        self._record_saved = {"id": rid, "name": data.get("name") or "(未命名)",
                              "rev": self._record_rev}
        return self._record_payload()

    def record_lib_edit(self, rid: str = "") -> dict:
        """进入「直接编辑库里这条」模式：载入它，之后**每次编辑自动写回**。

        与「载入」的区别只在收尾：载入之后编辑只改内存（界面显示「改动未更新」，
        要再点「更新到库」才落盘）；本接口把落盘也接管了，于是库里点开就能改，
        不必「载入 → 改 → 更新到库」三步。写回在后端的编辑收尾统一做，
        前端不用（也不能）记账——见 `_sync_lib_edit`。
        """
        payload = self.record_from_lib(rid)
        # 先记下写回目标，**再**重建 payload 里的 lib。
        # 顺序反了就是个静默 bug：record_from_lib 构造 payload 时 _record_lib_edit_id
        # 还是 None（纯载入会清掉它），于是响应里 `editing=False` —— 前端那条
        # 「正在直接编辑…」提示条**根本不会出现**，用户看不到"改动会立刻写回"，
        # 而写回其实已经在生效了。最难发现的那类不一致。
        self._record_lib_edit_id = rid
        payload["lib"] = self._lib_info()
        return payload

    def record_lib_end_edit(self) -> dict:
        """退出「直接编辑库条目」模式，回到「改了要手动更新到库」的语义。"""
        self._record_lib_edit_id = None
        return self._record_payload()

    def record_lib_to_node(self, rid: str = "") -> dict:
        """新建一个「录制回放」节点，按 id 引用库里这条录制。

        这是「在流程图中选择录制」的快路径：库里点一下就落到画布上，
        不用先载入缓冲再写入节点。节点只存 id，所以之后在库里改这条录制，
        所有引用它的节点一起生效。
        """
        if not rid:
            raise ControllerError(-32602, "参数无效：缺 id")
        try:
            meta = recording_lib.meta_of(recording_lib.load(rid))
        except ValueError as e:
            raise ControllerError(-32005, str(e)) from None
        node = Node(type="record_replay", params={
            "source": "录制库",
            "recording_id": rid,
            "events": [],
            "origin_x": meta["origin"][0], "origin_y": meta["origin"][1],
            "use_relative": True,
        }, enabled=True)
        with self._lock:
            self.workflow.nodes.append(node)
            inserted = len(self.workflow.nodes) - 1
        self._broadcast_workflow()
        return {**self.workflow_current(), "index": inserted,
                "node": self._public_node(node)}

    # ---- 热键 / 键盘捕获（P1-3，§3.2 + §9 说明） ----
    def _ensure_key_listener(self) -> None:
        """确保全局键盘监听线程存活（CGEventTap 失败则返回，线程随即结束）。"""
        if self._key_listener is None or not self._key_listener.is_alive():
            from core.maclistener import MacKeyboardListener
            self._key_listener = MacKeyboardListener(self._on_key, self._on_text)
            self._key_listener.start()

    def _on_text(self, text: str, x: int = 0, y: int = 0) -> None:
        """单一 listener 的文本回调：文本链路自检的命中标记。

        这个 listener 服务于热键/取点，文本提交本身对它没有语义——只有自检期间
        才关心（见 `input_probe`）。必须校验探针载荷，否则自检过程中用户自己敲的
        中文也会把标记点亮，让"链路通"的结论变成假阳性。
        """
        with self._lock:
            if self._probing and _PROBE_TEXT in (text or ""):
                self._probe_text_hit = True

    def _stop_key_listener(self) -> None:
        lis = self._key_listener
        self._key_listener = None
        if lis is not None:
            try:
                lis.stop()
            except Exception:  # noqa: BLE001
                pass

    def _on_key(self, name: str, pressed: bool, x: int = 0, y: int = 0,
                flags: int = 0) -> None:
        """单一 listener 回调：按 _key_mode 在捕获 / 热键 / 取点间分发。

        热键去抖：CGEventTap 会收到系统 key-repeat 的连续 KeyDown，长按 F9
        会被当成多次按下反复 toggle，故同名键 400ms 内只触发一次。
        `flags` 是按键时刻的 CGEventFlags 快照，此处不参与判定，仅为与
        MacKeyboardListener 的新签名对齐。"""
        if not name:
            return
        if self._probing:
            self._probe_maybe_hit(name)
        if self._picking_once and pressed:
            # 一次性取点：用按键事件自带的光标坐标回填（CLI 进程读不到全局光标）
            with self._lock:
                self._picking_once = False
            self._notify("base.picked", {"x": x, "y": y})
            return
        if self._key_mode == "hotkey" and pressed:
            now = time.monotonic()
            with self._lock:
                if name == self._last_hotkey_key and now - self._last_hotkey_at < 0.4:
                    return
                action = self._hotkey_map.get(name)
                if action:
                    self._last_hotkey_key = name
                    self._last_hotkey_at = now
            if action:
                payload = {"action": action}
                if action == "pick":
                    payload.update({"x": x, "y": y})
                self._notify("hotkey.triggered", payload)
            return
        if self._key_mode == "capture":
            if pressed:
                with self._lock:
                    self._capturing = False
                    self._key_mode = "hotkey" if self._hotkey_map else "idle"
                self._notify("key.captured", {"name": name, "x": x, "y": y})
            return

    def hotkey_set(self, actions: Any) -> dict:
        """绑定 F9/F10/F11 → record/run/pick。

        actions 可以是数组（按 F9/F10/F11 顺序，元素为动作名或 None 解除绑定），
        也可以是对象 {record, run, pick}。返回当前三键绑定快照。
        """
        if isinstance(actions, dict):
            actions = [actions.get("record"), actions.get("run"), actions.get("pick")]
        if not isinstance(actions, (list, tuple)):
            raise ControllerError(-32602, "参数无效：actions 须为数组或对象")
        mapping: dict = {}
        for i, act in enumerate(actions[:3]):
            if act and str(act) in _HOTKEY_ACTIONS:
                mapping[_HOTKEY_KEYS[i]] = str(act)
        with self._lock:
            self._hotkey_map = mapping
            if not self._capturing:
                self._key_mode = "hotkey" if mapping else "idle"
        if mapping or self._capturing:
            self._ensure_key_listener()
        else:
            self._stop_key_listener()
        return {"hotkeys": [mapping.get(k) for k in _HOTKEY_KEYS]}

    def hotkey_clear(self) -> dict:
        with self._lock:
            self._hotkey_map = {}
            if not self._capturing:
                self._key_mode = "idle"
        if not self._capturing:
            self._stop_key_listener()
        return {"hotkeys": [None, None, None]}

    def key_capture_start(self) -> dict:
        """进入按键捕获模式（暂挂热键分发），捕获到的键经 `key.captured` 异步回填。"""
        with self._lock:
            self._capturing = True
            self._key_mode = "capture"
        self._ensure_key_listener()
        return {"capturing": True}

    def key_capture_stop(self) -> dict:
        with self._lock:
            self._capturing = False
            self._key_mode = "hotkey" if self._hotkey_map else "idle"
        return {"capturing": False}

    def base_pick(self) -> dict:
        """「取点」：arm 一次性取点，下一个按键事件（自带光标坐标）经
        `base.picked` 通知回填。

        CLI sidecar 进程里读不到全局光标（CGEventGetLocation 恒 (0,0)），
        而键盘事件自带 location，事件即坐标，天然精准。"""
        self._ensure_key_listener()
        with self._lock:
            self._picking_once = True
        return {"armed": True}

    def template_snip(self) -> dict:
        """「截取模板」：系统框选截图（screencapture -i），完成后 `template.snipped` 回填。

        screencapture 会阻塞到用户框选完成，故放后台线程，RPC 立即返回。"""
        import subprocess
        from core.paths import templates_dir
        if self._snipping:
            raise ControllerError(-32001, "already_snipping")
        path = os.path.join(templates_dir(), f"tpl_{int(time.time())}.png")

        def work():
            self._snipping = True
            try:
                subprocess.run(["screencapture", "-i", "-o", path], timeout=120)
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    self._notify("template.snipped", {"path": path, "ok": True})
                else:
                    self._notify("template.snipped", {"path": "", "ok": False})
            except Exception as e:  # noqa: BLE001
                self._notify("template.snipped", {"path": "", "ok": False, "error": str(e)})
            finally:
                self._snipping = False

        threading.Thread(target=work, name="template-snip", daemon=True).start()
        return {"started": True}

    def input_probe(self) -> dict:
        """输入监控「实际可收性」自检：两段回环，缺一不可。

        CGEventTapCreate 对未授权进程也可能成功（事件静默不投递），预检 API 不可靠，
        权限快照全是 true 但热键/录制无效正是这种状态。做法：确保 tap 后由本进程
        post 一对 F18 合成键（几乎无应用响应、无副作用），300ms 内 tap 收到即链路通。

        **文本段**（零宽空格）：验证「投递出去的 Unicode 文本能否被 tap 读回来」。
        中文/emoji 录制完全依赖这条链路，而本地造一个事件读得出来**不代表** tap 收到的
        也读得出来——必须真投递一次才算数。两段结论要分开看：

        - `alive=false`：tap 根本收不到合成事件，先修输入监控权限。
        - `alive=true, text_alive=false`：tap 活着但收不到文本提交，本实现覆盖不了。
        - 两者皆 true：**本实现的链路完好**。

        ⚠️ `text_alive=true` **不等于"中文能录"**。2026-09-12 实测确认：系统拼音
        （`com.apple.inputmethod.SCIM.ITABC`）上屏走 `insertText:`，全程不投递任何
        CGEvent——它录不到，而本自检照样全绿地通过（自检投递的是我们自己合成的
        Unicode 事件，与输入法无关）。所以这里把**当前输入源**一并报出来，
        并标注该输入源是否已实测确认不经过事件层，避免"自检通过 ⇒ 中文没问题"
        这个错误的推论。

        录制/运行期间拒绝：自检会投递真实输入，不能污染正在录的事件序列，
        也不能把字符打进正在回放的工作流。
        """
        if self.recording or self.running:
            raise ControllerError(-32002, "busy")
        self._ensure_key_listener()
        with self._lock:
            self._probe_hit = False
            self._probe_text_hit = False
            self._probing = True
        try:
            from pynput.keyboard import Controller as Kb, Key
            kb = Kb()
            kb.press(Key.f18)
            kb.release(Key.f18)
            from core.mactype import type_text
            type_text(_PROBE_TEXT)
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._probing = False
            return {"alive": False, "text_alive": False,
                    "input_source": self._input_source_info(), "error": str(e)}
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            with self._lock:
                if self._probe_hit and self._probe_text_hit:
                    break
            time.sleep(0.03)
        with self._lock:
            alive = self._probe_hit
            text_alive = self._probe_text_hit
            self._probing = False
        self._notify("permission.probe",
                     {"inputAlive": bool(alive), "textAlive": bool(text_alive)})
        return {"alive": bool(alive), "text_alive": bool(text_alive),
                "input_source": self._input_source_info()}

    @staticmethod
    def _input_source_info() -> dict:
        """当前输入源 + 它是否已实测确认不经过事件层（探测失败则为空，不影响自检）。"""
        from core.inputsource import current_input_source, event_channel_unsupported
        src = current_input_source()
        if not src:
            return {}
        unsupported = event_channel_unsupported(src.get("id"))
        return {"id": src.get("id", ""), "name": src.get("name", ""),
                "event_channel_unsupported": unsupported}


    def _probe_maybe_hit(self, name: str) -> None:
        """_on_key 早期调用：F18 自检键命中标记（仅在探测进行中生效）。"""
        with self._lock:
            if self._probing and name == "F18":
                self._probe_hit = True

    # ---- 定时运行（P1-3，§9.2） ----
    def _schedule_path(self) -> str:
        from core.paths import app_dir
        return os.path.join(app_dir(), "config.json")

    def _schedule_load(self) -> dict:
        try:
            with open(self._schedule_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        return data.get("schedule", {}) or {}

    def _schedule_save(self, cfg: dict) -> None:
        from core.paths import app_dir
        os.makedirs(app_dir(), exist_ok=True)
        try:
            with open(self._schedule_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        data["schedule"] = cfg
        try:
            with open(self._schedule_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _schedule_next_fire(self, cfg: dict) -> Optional[datetime]:
        if not cfg:
            return None
        now = datetime.now()
        if cfg.get("mode") == "固定间隔":
            iv = max(int(cfg.get("intervalMin", 1)), 1)
            return now + timedelta(minutes=iv)
        at = str(cfg.get("atTime", "09:00"))
        try:
            hh, mm = map(int, at.split(":"))
        except ValueError:
            hh, mm = 9, 0
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        return nxt

    def _schedule_cancel_timer(self) -> None:
        t = self._schedule_timer
        self._schedule_timer = None
        if t is not None:
            t.cancel()

    def _schedule_arm(self, cfg: dict) -> None:
        self._schedule_cancel_timer()
        if not (cfg.get("enabled") and cfg.get("workflowPath")):
            return
        nxt = self._schedule_next_fire(cfg)
        if nxt is None:
            return
        delay = max((nxt - datetime.now()).total_seconds(), 0.1)
        self._schedule_timer = threading.Timer(delay, self._schedule_fire)
        self._schedule_timer.daemon = True
        self._schedule_timer.start()

    def schedule_configure(self, cfg: Any) -> dict:
        if not isinstance(cfg, dict):
            raise ControllerError(-32602, "参数无效：cfg 须为对象")
        mode = cfg.get("mode", "每天时刻")
        if mode not in _VALID_SCHEDULE_MODES:
            raise ControllerError(-32602, "mode 须为 每天时刻 或 固定间隔", {"mode": mode})
        at_time = str(cfg.get("atTime", "09:00"))
        try:
            interval = int(cfg.get("intervalMin", 30))
        except (TypeError, ValueError):
            raise ControllerError(-32602, "intervalMin 须为整数")
        path = cfg.get("workflowPath", "") or ""
        enabled = bool(cfg.get("enabled", False)) and bool(path)
        clean = {
            "mode": mode, "atTime": at_time, "intervalMin": interval,
            "workflowPath": path, "enabled": enabled,
        }
        with self._lock:
            self._schedule = clean
        self._schedule_save(clean)
        self._schedule_arm(clean)
        nxt = self._schedule_next_fire(clean)
        return {"schedule": clean, "nextFire": nxt.strftime("%Y-%m-%d %H:%M") if nxt else ""}

    def schedule_get(self) -> dict:
        with self._lock:
            cfg = dict(self._schedule)
        nxt = self._schedule_next_fire(cfg) if cfg else None
        return {"schedule": cfg, "nextFire": nxt.strftime("%Y-%m-%d %H:%M") if nxt else ""}

    def _schedule_fire(self) -> None:
        """定时触发：从磁盘重载工作流并真正运行。

        顺序很关键：**先做忙检查与加载校验，再替换 self.workflow**。
        此前是「先替换、再 run_start」——忙时虽然跳过了运行，但用户正在编辑的
        工作流已被定时脚本覆盖；文件缺失/损坏时更糟：直接 run_start 跑的是内存里
        那个完全不相干的工作流。现在任一前置校验不过就只上报原因、不运行、不覆盖。
        """
        with self._lock:
            cfg = dict(self._schedule)
            # 忙检查放在最前：运行中/录制中不改动当前工作流，也不触发运行
            busy = self.running or self.recording
        path = cfg.get("workflowPath")
        reason: Optional[str] = None
        wf: Optional[Workflow] = None

        if busy:
            reason = "already_running" if self.running else "busy_recording"
        elif not path or not os.path.exists(path):
            reason = "workflow_missing"
        else:
            try:
                wf = Workflow.load(path)
            except Exception:  # noqa: BLE001
                wf = None
            if wf is None:
                reason = "workflow_load_failed"

        if reason is None and wf is not None:
            with self._lock:
                self.workflow = wf
                self.path = path
            self._broadcast_workflow()
            try:
                self.run_start()
            except ControllerError as e:
                reason = e.message

        if reason:
            self._notify("schedule.fired", {"path": path, "ran": False, "reason": reason})
        else:
            self._notify("schedule.fired", {"path": path, "ran": True})
        self._schedule_arm(cfg)

    # ---- 停机清理（app.shutdown 时调用） ----
    def shutdown(self, timeout: float = 3.0) -> None:
        """进程退出前的收尾：定时器、热键、**执行器与录制器**。

        此前只停定时器与热键监听，于是退出时可能出现：回放线程还在跑、
        按下的键/鼠标键没有被 finally 释放（残留「卡键」），
        录制中的 CGEventTap 也没释放。这里按「先让线程自己收尾、再兜底」的顺序处理。
        """
        self._schedule_cancel_timer()
        self._stop_key_listener()

        # 1) 运行中：置停标志并等运行线程走完 finally（播放器会补发 release）
        run_thread = self._run_thread
        if run_thread is not None and run_thread.is_alive():
            self.executor.stop_run()
            run_thread.join(timeout)

        # 2) 录制中：停监听器（释放 CGEventTap），再等轮询线程退出
        with self._lock:
            rec = self.recorder
            self.recorder = None
            was_recording = self.recording
            self.recording = False
        if was_recording and rec is not None:
            try:
                rec.stop()
            except Exception:  # noqa: BLE001
                pass
        poller = self._rec_poller
        if poller is not None and poller.is_alive():
            poller.join(timeout)
        self._rec_poller = None
        self._rec_poller_rec = None
        self._run_thread = None
