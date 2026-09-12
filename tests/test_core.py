"""核心逻辑测试：不依赖真实屏幕状态（视觉层用注入 mock）。"""
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.events import MacroEvent, Node, Workflow
from core.executor import Executor, RunContext
from core.player import PlayOptions, Player
from core import keymap
import tasks.builtin  # noqa: F401
from tasks.base import all_definitions, get_task
import tasks.base as tb


# ---------- events ----------
def test_workflow_roundtrip(tmp_path):
    wf = Workflow(name="t", speed=2.0, repeat=3, nodes=[
        Node(type="delay", params={"ms": 100}),
        Node(type="note", params={"text": "hi"}, enabled=False),
    ])
    p = str(tmp_path / "wf.json")
    wf.save(p)
    wf2 = Workflow.load(p)
    assert wf2.name == "t" and wf2.speed == 2.0 and wf2.repeat == 3
    assert len(wf2.nodes) == 2 and wf2.nodes[1].enabled is False
    assert wf2.nodes[0].params["ms"] == 100


def test_legacy_tauri_script_import(tmp_path):
    old = {"version": 1, "name": "old", "origin_x": 100, "origin_y": 50,
           "events": [{"ts_ms": 0, "kind": "move", "x": 100, "y": 50},
                      {"ts_ms": 200, "kind": "mouse", "button": "left", "pressed": True,
                       "x": 120, "y": 70}]}
    p = str(tmp_path / "old.json")
    json.dump(old, open(p, "w"))
    wf = Workflow.load(p)
    assert len(wf.nodes) == 1
    node = wf.nodes[0]
    assert node.type == "record_replay"
    assert node.params["origin_x"] == 100
    assert len(node.params["events"]) == 2


def test_tolerant_event():
    from tasks.builtin import tolerant_event
    ev = tolerant_event({"ts_ms": 5, "kind": "key", "key": "a", "pressed": True, "junk": 1})
    assert ev.ts_ms == 5 and ev.key == "a" and ev.x == 0
    assert tolerant_event({}).kind == "move"


# ---------- keymap ----------
def test_keymap_roundtrip():
    for name in ["Space", "Return", "Escape", "F9", "UpArrow", "LeftArrow", "Command"]:
        obj = keymap.name_to_key(name)
        assert obj is not None, name
        assert keymap.key_to_name(obj) == name, (name, keymap.key_to_name(obj))


def test_keymap_single_char():
    assert keymap.name_to_key("a") == "a"
    assert keymap.name_to_key("vk:65") is not None
    assert keymap.name_to_key("") is None
    assert keymap.name_to_key("no-such-key") is None


# ---------- executor ----------
def test_executor_order_and_repeat():
    calls = []
    ex = Executor()
    ex.player.glide_now = lambda t: None  # 不动真鼠标
    wf = Workflow(repeat=2, nodes=[
        Node(type="note", params={"text": "skip"}),
        Node(type="delay", params={"ms": 1}),
        Node(type="keyboard", params={"mode": "text", "text": ""}),
    ])
    ex.run_workflow(wf, on_node=lambda i, t: calls.append(t))
    assert calls == ["delay", "keyboard", "delay", "keyboard"]
    assert ex.running is False


@pytest.mark.parametrize("node_repeat,workflow_repeat", [(1, 1), (3, 1), (3, 2), (0, 2)])
def test_record_replay_repeats_once_per_executor_iteration(monkeypatch, node_repeat, workflow_repeat):
    """repeat 统一由执行器处理：总回放次数为节点次数 × 工作流次数。"""
    ex = Executor()
    plays, errors = [], []
    monkeypatch.setattr(ex.player, "play", lambda evs, opt, on_progress=None: plays.append((evs, opt)))
    wf = Workflow(speed=2, repeat=workflow_repeat, nodes=[
        Node(type="record_replay", params={
            "repeat": node_repeat, "speed": 1.5,
            "events": [{"ts_ms": 0, "kind": "move", "x": 10, "y": 20}],
            "use_relative": True, "origin_x": 10, "origin_y": 20,
        }),
    ])
    ex.run_workflow(wf, base_x=100, base_y=200, on_error=lambda *args: errors.append(args))
    assert not errors
    assert len(plays) == max(node_repeat, 1) * workflow_repeat
    assert all(opt.speed == 3 and opt.base_x == 100 and opt.base_y == 200 for _, opt in plays)
    assert all(opt.origin_x == 10 and opt.origin_y == 20 and opt.use_relative for _, opt in plays)


def test_record_replay_stop_prevents_remaining_repeats(monkeypatch):
    ex = Executor()
    plays, done = [], []

    def play(evs, opt, on_progress=None):
        plays.append(evs)
        ex.stop_run()

    monkeypatch.setattr(ex.player, "play", play)
    wf = Workflow(repeat=2, nodes=[Node(type="record_replay", params={"repeat": 3})])
    ex.run_workflow(wf, on_done=done.append)
    assert len(plays) == 1
    assert done == [True]


def test_generic_node_repeat_remains_supported(monkeypatch):
    ex = Executor()
    waits = []
    monkeypatch.setattr(ex.player, "wait", waits.append)
    wf = Workflow(repeat=2, nodes=[Node(type="delay", params={"ms": 10, "repeat": 3})])
    ex.run_workflow(wf)
    assert waits == [0.01] * 6


def test_executor_condition_gating(monkeypatch):
    """run_workflow 每次重置条件；由条件节点写入后门控后续节点。"""
    ran = []
    ex = Executor()

    # 无条件节点：默认条件 False → 条件成立节点被跳过，条件不成立节点执行
    wf = Workflow(nodes=[
        Node(type="delay", params={"ms": 1, "run_when": "条件成立"}),
        Node(type="delay", params={"ms": 1, "run_when": "条件不成立"}),
    ])
    ex.run_workflow(wf, on_node=lambda i, t: ran.append(t))
    assert ran == ["delay"]

    # 条件节点（mock 视觉命中）→ 条件成立节点执行
    from core import vision
    monkeypatch.setattr(vision, "find_template",
                        lambda conf, template_path="": type("M", (), {"found": True})())
    ran.clear()
    wf2 = Workflow(nodes=[
        Node(type="condition", params={"image_path": "x.png"}),
        Node(type="delay", params={"ms": 1, "run_when": "条件成立"}),
        Node(type="delay", params={"ms": 1, "run_when": "条件不成立"}),
    ])
    ex.run_workflow(wf2, on_node=lambda i, t: ran.append(t))
    assert ran == ["condition", "delay"]


def test_executor_stop_flag_stops_loop():
    ex = Executor()
    count = []

    def slow(ctx):
        for _ in range(50):
            if ctx.stopping:
                return
            time.sleep(0.01)

    import tasks.base as tb
    class _T(tb.BaseTask):
        type = "_test_slow"
        name = "slow"
        def run(self, ctx):
            slow(ctx)
    tb.register(_T())
    wf = Workflow(repeat=100, nodes=[Node(type="_test_slow")])
    import threading
    threading.Timer(0.1, ex.stop_run).start()
    t0 = time.time()
    ex.run_workflow(wf)
    assert time.time() - t0 < 2.0
    assert ex.running is False


def test_executor_node_exception_stops_gracefully(capsys):
    import tasks.base as tb

    class _Bad(tb.BaseTask):
        type = "_test_bad"
        name = "bad"
        def run(self, ctx):
            raise RuntimeError("boom")

    tb.register(_Bad())
    errors = []
    ex = Executor()
    wf = Workflow(nodes=[Node(type="_test_bad"), Node(type="delay", params={"ms": 1})])
    done = []
    ex.run_workflow(wf, on_error=lambda i, t, e: errors.append((t, str(e))),
                    on_done=lambda s: done.append(s))
    assert errors and errors[0][0] == "_test_bad" and "boom" in errors[0][1]
    assert done == [True]  # 异常会置停标志


# ---------- player ----------
class FakeMouse:
    def __init__(self):
        self.pos = (0.0, 0.0)
        self.moves = []      # (x, y, 保持中的按键) —— button 非空表示拖拽事件
        self.presses = []
        self.releases = []
        self.scrolls = []    # (dx, dy, unit)

    @property
    def position(self):
        return self.pos

    @position.setter
    def position(self, v):
        self.move_to(v)

    def move_to(self, v, button=None):
        self.pos = (float(v[0]), float(v[1]))
        self.moves.append((self.pos[0], self.pos[1], button))

    def press(self, b, clicks=1):
        self.presses.append(("down", str(b), clicks))

    def release(self, b, clicks=1):
        self.releases.append(("up", str(b), clicks))

    def scroll(self, dx, dy, unit="line"):
        self.scrolls.append((dx, dy, unit))


class FakeKb:
    def __init__(self):
        self.log = []

    def press(self, k):
        self.log.append(("down", k))

    def release(self, k):
        self.log.append(("up", k))

    def type(self, t):
        self.log.append(("type", t))


def make_player(monkeypatch):
    p = Player()
    fm, fk = FakeMouse(), FakeKb()
    monkeypatch.setattr(p, "mouse", fm)
    monkeypatch.setattr(p, "kb", fk)
    return p, fm, fk


def test_player_relative_offsets(monkeypatch):
    p, fm, fk = make_player(monkeypatch)
    events = [
        MacroEvent(ts_ms=0, kind="move", x=100, y=100),
        MacroEvent(ts_ms=100, kind="move", x=200, y=200),
        MacroEvent(ts_ms=150, kind="mouse", x=200, y=200, button="left", pressed=True),
        MacroEvent(ts_ms=250, kind="mouse", x=200, y=200, button="left", pressed=False),
    ]
    p.play(events, PlayOptions(use_relative=True, base_x=900, base_y=500,
                                origin_x=100, origin_y=100))
    assert fm.pos == (1000.0, 600.0)
    assert fm.presses and fm.releases


def test_player_releases_keys_on_stop(monkeypatch):
    p, fm, fk = make_player(monkeypatch)
    events = [MacroEvent(ts_ms=0, kind="key", key="a", pressed=True),
              MacroEvent(ts_ms=10**6, kind="key", key="a", pressed=False)]
    threading_stop = __import__("threading").Timer(0.05, p.stop_playback)
    threading_stop.start()
    p.play(events, PlayOptions())
    threading_stop.join()
    downs = [k for op, k in fk.log if op == "down"]
    ups = [k for op, k in fk.log if op == "up"]
    assert "a" in downs
    assert "a" in ups  # 中断后补 release，不卡键


def test_player_suppress_hotkeys(monkeypatch):
    p, fm, fk = make_player(monkeypatch)
    events = [MacroEvent(ts_ms=0, kind="key", key="F9", pressed=True),
              MacroEvent(ts_ms=5, kind="key", key="F9", pressed=False),
              MacroEvent(ts_ms=10, kind="key", key="a", pressed=True)]
    p.play(events, PlayOptions(suppress_keys={"F9", "F10", "F11"}))
    keys = [k for _, k in fk.log]
    assert "a" in keys and not any(getattr(k, "name", "") == "f9" for k in keys)


def test_player_relative_base_unset_falls_back_to_origin(monkeypatch):
    """基点未设置（0,0 默认值）→ 在原位置回放，不飞到屏幕左上角。"""
    p, fm, fk = make_player(monkeypatch)
    fm.pos = (500.0, 400.0)
    events = [
        MacroEvent(ts_ms=0, kind="move", x=100, y=100),
        MacroEvent(ts_ms=100, kind="move", x=200, y=150),
    ]
    p.play(events, PlayOptions(use_relative=True, base_x=0, base_y=0,
                                origin_x=100, origin_y=100))
    assert fm.pos == (200.0, 150.0)  # 原位回放：偏移 0


def test_player_keyboard_only_keeps_cursor(monkeypatch):
    """纯键盘脚本：不得挪动光标。"""
    p, fm, fk = make_player(monkeypatch)
    fm.pos = (777.0, 888.0)
    events = [MacroEvent(ts_ms=0, kind="key", key="a", pressed=True),
              MacroEvent(ts_ms=30, kind="key", key="a", pressed=False)]
    p.play(events, PlayOptions(use_relative=True, base_x=0, base_y=0))
    assert fm.pos == (777.0, 888.0)
    downs = [k for op, k in fk.log if op == "down"]
    assert "a" in downs


def test_glide_reaches_target(monkeypatch):
    p, fm, fk = make_player(monkeypatch)
    p._glide((0, 0), (990, 630), 0.5)
    assert fm.pos == (990.0, 630.0)


# ---------- tasks ----------
def test_all_nodes_have_definitions():
    types = [d["type"] for d in all_definitions()]
    for t in ["record_replay", "image_click", "ocr_click", "yolo_click",
              "condition", "mouse", "keyboard", "delay", "note"]:
        assert t in types, t
        d = [x for x in all_definitions() if x["type"] == t][0]
        assert d["name"] and isinstance(d["params"], list)


def test_node_defaults_applied():
    task = get_task("delay")
    assert task.defaults()["ms"] == 500


# §9.4：菜单顺序 = 鼠标 → 键盘 → 延时 → 录制回放 → 图像 → OCR → YOLO → 条件 → 注释
BUILTIN_MENU_ORDER = [
    "mouse", "keyboard", "delay", "record_replay",
    "image_click", "ocr_click", "yolo_click", "condition", "note",
]


def test_node_menu_order_is_explicit():
    """内置节点的菜单顺序 = §9.4 规定顺序（产品需求的回归护栏）。

    注意：**这条用例无法区分「按 order 排」与「按注册顺序排」**——给 9 个类都
    补上 @register 之后，源码里的定义顺序恰好就等于期望顺序，两种机制结果相同，
    删掉排序它照样通过（实测假绿）。真正验证「顺序由 order 决定」的是下面
    test_order_overrides_registration_order。
    """
    defs = all_definitions()
    orders = [d["order"] for d in defs]
    assert orders == sorted(orders), "all_definitions() 必须按 order 升序返回"
    assert all(isinstance(o, int) for o in orders)

    n = len(BUILTIN_MENU_ORDER)
    assert [d["type"] for d in defs[:n]] == BUILTIN_MENU_ORDER
    # order 撞车会让菜单顺序退化成注册顺序，必须唯一
    builtin_orders = orders[:n]
    assert len(set(builtin_orders)) == n, f"order 重复：{builtin_orders}"


def test_order_overrides_registration_order():
    """注册顺序与 order 相反时按 order 排——这条才真正锁住排序机制。

    两个探针节点的 order 与注册先后**故意相反**：只有 all_definitions() 真的
    按 order 排序，结果才会是 early 在前。去掉排序即失败（已反向验证）。
    """

    class _Late(tb.BaseTask):
        type = "_test_order_late"
        name = "late"
        order = 200

    class _Early(tb.BaseTask):
        type = "_test_order_early"
        name = "early"
        order = 190

    tb.register(_Late)   # 先注册 order 大的
    tb.register(_Early)  # 后注册 order 小的
    try:
        types = [d["type"] for d in all_definitions()]
        assert types.index("_test_order_early") < types.index("_test_order_late")
    finally:
        tb._REGISTRY.pop("_test_order_late", None)
        tb._REGISTRY.pop("_test_order_early", None)


def test_every_builtin_declares_its_own_order():
    """内置节点必须显式声明 order；默认 100 是留给第三方/测试节点的兜底。"""
    for t in BUILTIN_MENU_ORDER:
        task = get_task(t)
        assert task is not None, f"节点未注册：{t}"
        assert task.order < 100, f"{t} 未声明 order，会落到菜单末尾"


def test_register_accepts_class_and_instance():
    """`@register` 装饰器拿到的永远是**类**，注册表必须只存实例。

    历史坑：装饰器把类对象直接塞进 _REGISTRY，靠 builtin.py 末尾 9 行
    `register(实例)` 覆盖回实例——所以那 9 行删掉后 all_definitions() 立刻炸
    `TypeError: definition() missing 1 required positional argument: 'self'`。
    """
    class _ByClass(tb.BaseTask):
        type = "_test_reg_by_class"
        name = "by class"
        order = 100

    returned = tb.register(_ByClass)
    assert returned is _ByClass, "装饰器必须返回类本身，否则类名会被换成实例"
    assert isinstance(tb.get_task("_test_reg_by_class"), _ByClass)

    class _ByInstance(tb.BaseTask):
        type = "_test_reg_by_instance"
        name = "by instance"

    inst = _ByInstance()
    assert tb.register(inst) is inst
    assert tb.get_task("_test_reg_by_instance") is inst
    try:
        assert all(not isinstance(o, type) for o in tb._REGISTRY.values())
    finally:
        tb._REGISTRY.pop("_test_reg_by_class", None)
        tb._REGISTRY.pop("_test_reg_by_instance", None)


def test_runcontext_stopping_property():
    ctx = RunContext(params={}, player=None, is_stopping=lambda: True)
    assert ctx.stopping is True
    ctx2 = RunContext(params={}, player=None)
    assert ctx2.stopping is False


def test_record_replay_task_builds_options(monkeypatch):
    """录制回放节点：事件容错构造 + 相对偏移计算。"""
    task = get_task("record_replay")
    plays = []
    p = Player()
    monkeypatch.setattr(p, "play", lambda evs, opt, on_progress=None: plays.append((evs, opt)))
    ctx = RunContext(params={
        "events": [{"ts_ms": 0, "kind": "move", "x": 10, "y": 20, "junk": 1}],
        "origin_x": 10, "origin_y": 20, "use_relative": True,
        "speed": 1.0, "repeat": 1,
    }, player=p, base_x=110, base_y=120)
    task.run(ctx)
    assert len(plays) == 1
    evs, opt = plays[0]
    assert len(evs) == 1 and evs[0].x == 10
    assert opt.suppress_keys == {"F9", "F10", "F11"}


# ---------- macOS 键盘监听（自建 tap，替代 pynput） ----------
def test_vk_table_covers_script_names():
    from core.mackeys import VK_NAMES, vk_to_name
    specials = ["Space", "Return", "Escape", "Tab", "Backspace", "Delete",
                "Home", "End", "PageUp", "PageDown", "UpArrow", "DownArrow",
                "LeftArrow", "RightArrow", "Command", "Option", "Control",
                "Shift", "CapsLock", "F1", "F9", "F11", "F12"]
    named = set(VK_NAMES.values())
    for s in specials:
        assert s in named, f"{s} 不在 VK 表"
    assert vk_to_name(0x00) == "a"
    assert vk_to_name(9999) is None


def test_vk_names_playable():
    """VK 表里的每个名字都必须能被回放侧还原（否则录制后无法回放）。"""
    from core.mackeys import VK_NAMES
    for name in set(VK_NAMES.values()):
        if name.startswith("Keypad") or name == "Help":
            continue  # 小键盘/Help：pynput 无对应键，回放时静默跳过
        assert keymap.name_to_key(name) is not None, name


def test_modifier_edge():
    from core.mackeys import modifier_edge
    assert modifier_edge(0x38, 0x20000, 0x0) is True      # shift 按下
    assert modifier_edge(0x38, 0x20000, 0x20000) is None  # 无变化
    assert modifier_edge(0x38, 0x0, 0x20000) is False     # 释放
    assert modifier_edge(0x00, 0x20000, 0x0) is None      # 非修饰键
    assert modifier_edge(0x39, 0x10000, 0x0) is True      # capslock 开
    assert modifier_edge(0x37, 0x100000, 0x0) is True     # command 按下


def test_maclistener_callback_logic(monkeypatch):
    """不创建真实 tap，验证 _dispatch 对无名键码的过滤（含坐标与 flags 透传）。"""
    from core import maclistener
    got = []
    lis = maclistener.MacKeyboardListener(
        lambda n, p, x=0, y=0, fl=0: got.append((n, p, x, y, fl)))
    lis._dispatch("a", True, 10, 20, 0x100000)
    lis._dispatch(None, True, 10, 20)   # 未知键码应被丢弃
    lis._dispatch("Return", False, 30, 40)
    assert got == [("a", True, 10, 20, 0x100000), ("Return", False, 30, 40, 0)]



def test_maclistener_callback_error_is_logged_once(monkeypatch, caplog):
    """回调异常必须留痕（只记首次）。

    回归：`_callback` 原先静默 `except Exception: pass`，消费方签名不匹配时
    表现为「热键/按键捕获完全无效，日志毫无线索」，排查成本极高。
    """
    from core import maclistener

    monkeypatch.setattr(maclistener.Quartz, "CGEventGetIntegerValueField", lambda ev, f: 0)
    monkeypatch.setattr(
        maclistener.Quartz, "CGEventGetLocation",
        lambda ev: type("Loc", (), {"x": 1.0, "y": 2.0})(),
    )
    monkeypatch.setattr(maclistener, "vk_to_name", lambda kc: "a")

    def bad_callback(name, pressed):  # 故意只声明 2 个参数
        raise AssertionError("不应走到这里")

    lis = maclistener.MacKeyboardListener(bad_callback)
    with caplog.at_level(logging.ERROR, logger="autoflow.keyboard"):
        lis._callback(None, maclistener._KEY_DOWN, object(), None)
        lis._callback(None, maclistener._KEY_DOWN, object(), None)
    assert "键盘 tap 回调异常" in caplog.text
    assert caplog.text.count("键盘 tap 回调异常") == 1  # 只记首次，不刷屏


def test_paths_dirs(tmp_path, monkeypatch):
    from core import paths
    monkeypatch.setattr(paths, "app_dir", lambda: str(tmp_path))
    d = paths.workflows_dir()
    t = paths.templates_dir()
    assert os.path.isdir(d) and os.path.isdir(t)


def test_broadcast_summary_never_mutates_tree(tmp_path):
    """回归：_public_node 摘要化 events 曾因共享引用摧毁真源
    （运行报 'str' object has no attribute 'get'，保存即丢数据）。"""
    from rpc.controller import AppController
    ctrl = AppController()
    collected = []
    ctrl.set_notifier(lambda m, prm: collected.append((m, prm)))
    real_events = [{"ts_ms": 0, "kind": "move", "x": 1, "y": 2},
                   {"ts_ms": 5, "kind": "key", "key": "a", "pressed": True}]
    ctrl.workflow.nodes.append(Node(type="record_replay", params={
        "events": list(real_events), "origin_x": 0, "origin_y": 0,
        "use_relative": True}))
    ctrl._broadcast_workflow()
    node = ctrl.workflow.nodes[0]
    # 真源必须是完整 list，且元素仍为 dict
    assert isinstance(node.params["events"], list) and len(node.params["events"]) == 2
    # 通知 params 直接是工作流（不是 {workflow: ...}），事件仅发送摘要。
    method, payload = collected[-1]
    assert method == "workflow.changed"
    assert "workflow" not in payload
    assert payload["nodes"][0]["params"]["events"] == {"count": 2}
    assert payload == ctrl.workflow_current()["workflow"]
    # 再广播一次并保存，事件不丢；文件仅写入 pytest 临时目录。
    ctrl._broadcast_workflow()
    path = str(tmp_path / "broadcast-roundtrip.json")
    ctrl.workflow_save(path)
    wf = Workflow.load(path)
    evs = wf.nodes[0].params["events"]
    assert isinstance(evs, list) and len(evs) == 2 and evs[1]["key"] == "a"
    # RecordReplayTask 在真源上可正常运行（不再触发 'str' has no get）
    from tasks.builtin import tolerant_event
    evs2 = [tolerant_event(x) for x in node.params["events"]]
    assert all(e is not None for e in evs2)
    ctrl.shutdown()


def test_recorder_window_filter_is_opt_in():
    """窗口过滤默认关闭。

    v2 默认按窗口矩形丢弃事件，且边界只在录制开始时下发一次、前端监听从不
    注销——边界一旦过期就成片吞掉真实操作，正是"操作被莫名其妙裁掉"的主因。
    v3 改为显式开关，默认不丢任何事件。
    """
    from core.recorder import Recorder
    rec = Recorder(window_bounds=(90, 50, 1100, 720))
    rec._accept(MacroEvent(ts_ms=0, kind="move", x=500, y=400))       # 窗口内
    rec._accept(MacroEvent(ts_ms=1, kind="mouse", x=1000, y=400, button="left", pressed=True))
    rec._accept(MacroEvent(ts_ms=2, kind="wheel", x=200, y=200, wheel_dy=1))
    r = rec.result()
    assert r.n_window_dropped == 0
    assert len(r.events) == 3


def test_recorder_drops_events_inside_window_bounds_when_enabled():
    """显式开启窗口过滤时才丢弃落在窗口内的鼠标事件，键盘不受影响。"""
    from core.recorder import Recorder
    rec = Recorder(window_bounds=(90, 50, 1100, 720), drop_in_window=True)
    rec._accept(MacroEvent(ts_ms=0, kind="move", x=500, y=400))      # 窗口内 → 丢
    rec._accept(MacroEvent(ts_ms=1, kind="mouse", x=1000, y=400, button="left", pressed=True))  # 内 → 丢
    rec._accept(MacroEvent(ts_ms=2, kind="move", x=1300, y=400))     # 外 → 留
    rec._accept(MacroEvent(ts_ms=3, kind="mouse", x=1300, y=400, button="left", pressed=True))
    rec._accept(MacroEvent(ts_ms=4, kind="key", key="a", pressed=True))  # 键盘不受边界影响
    rec._accept(MacroEvent(ts_ms=5, kind="wheel", x=200, y=200, wheel_dy=1))  # 内 → 丢
    r = rec.result()
    kinds = [(e.kind, e.x, e.y) for e in r.events]
    assert ("move", 1300, 400) in kinds and ("mouse", 1300, 400) in kinds
    assert not any(k == "wheel" for k, _, _ in kinds)
    assert any(e.kind == "key" for e in r.events)
    assert r.n_window_dropped == 3


def test_recorder_keeps_drag_trajectory_without_decimation():
    """拖拽期间永不降采样，且每条移动带 dragged 标记。

    回归：v2 把 pynput 回调的第三个参数（实为 injected）误当成 dragged 并丢弃，
    拖拽信息从未入库，回放只能发 MouseMoved → 所有拖拽操作失效。
    """
    from core.recorder import Recorder
    rec = Recorder()
    # 每步只有 1px（低于 MOVE_MIN_PX=2），若是普通移动会被降采样丢弃
    for i in range(1, 6):
        rec._accept(MacroEvent(ts_ms=i, kind="move", x=100 + i, y=200, dragged=True))
    r = rec.result()
    assert len(r.events) == 5
    assert all(e.dragged for e in r.events)
    assert r.n_decimated == 0


def test_recorder_decimates_redundant_moves_but_not_information():
    """非拖拽的冗余微移动被降采样，但超过时间间隔阈值时必留一条。"""
    from core.recorder import Recorder
    rec = Recorder()
    rec._accept(MacroEvent(ts_ms=0, kind="move", x=100, y=100))
    rec._accept(MacroEvent(ts_ms=1, kind="move", x=101, y=100))    # 1px 且 1ms → 降采样
    rec._accept(MacroEvent(ts_ms=2, kind="move", x=100, y=101))    # 仍在原地附近 → 降采样
    rec._accept(MacroEvent(ts_ms=100, kind="move", x=100, y=101))  # 距上次保留 100ms → 必留
    r = rec.result()
    assert len(r.events) == 2
    assert r.n_decimated == 2


def test_recorder_flushes_last_decimated_position():
    """停止时补回最后一个被降采样的位置——否则"移过去就停手"的终点会停在中段。"""
    from core.recorder import Recorder
    rec = Recorder()
    rec._accept(MacroEvent(ts_ms=0, kind="move", x=100, y=100))
    rec._accept(MacroEvent(ts_ms=1, kind="move", x=101, y=100))    # 被降采样
    rec._flush_pending_move()
    r = rec.result()
    assert [(e.x, e.y) for e in r.events] == [(100, 100), (101, 100)]


def test_recorder_merges_double_click_sequence():
    """相邻快速按下归并为 clicks=2/3，回放才能写入 ClickState。"""
    from core.recorder import Recorder
    rec = Recorder()
    for ts in (0, 30, 90, 900):
        rec._accept(MacroEvent(ts_ms=ts, kind="mouse", x=10, y=10, button="left",
                               pressed=True, clicks=rec._next_clicks("left", 10, 10, ts)))
    r = rec.result()
    assert [e.clicks for e in r.events] == [1, 2, 3, 1]


def test_recorder_next_clicks_resets_when_position_moves():
    """位置明显变化时重新计数，避免把两次独立点击并成双击。"""
    from core.recorder import Recorder
    rec = Recorder()
    assert rec._next_clicks("left", 10, 10, 0) == 1
    assert rec._next_clicks("left", 10, 10, 50) == 2
    assert rec._next_clicks("left", 80, 80, 100) == 1


def test_trim_stop_interaction_uses_window_bounds():
    """按钮停止时裁掉"点停止按钮"那次点击及其前移向按钮的移动。"""
    from core.recorder import trim_stop_interaction
    ev = lambda **kw: MacroEvent(**kw)  # noqa: E731
    events = [
        ev(ts_ms=0, kind="move", x=300, y=300),
        ev(ts_ms=10, kind="mouse", x=300, y=300, button="left", pressed=True),
        ev(ts_ms=20, kind="mouse", x=300, y=300, button="left", pressed=False),
        ev(ts_ms=30, kind="move", x=900, y=400),          # 移向窗口
        ev(ts_ms=40, kind="mouse", x=1000, y=400, button="left", pressed=True),   # 点停止
        ev(ts_ms=45, kind="mouse", x=1000, y=400, button="left", pressed=False),
    ]
    out = trim_stop_interaction(events, (900, 300, 400, 300))
    assert [e.ts_ms for e in out] == [0, 10, 20]          # 尾部交互整体裁掉


def test_trim_never_destroys_real_work_without_window_hit():
    """识别不到停止点击时一律不裁——宁可多点一下，也不删掉真实拖拽。"""
    from core.recorder import trim_stop_interaction
    ev = lambda **kw: MacroEvent(**kw)  # noqa: E731
    events = [
        ev(ts_ms=0, kind="mouse", x=100, y=100, button="left", pressed=True),
        ev(ts_ms=10, kind="move", x=120, y=120, dragged=True),
        ev(ts_ms=20, kind="mouse", x=120, y=120, button="left", pressed=False),
        ev(ts_ms=30, kind="move", x=140, y=140, dragged=True),
    ]
    assert trim_stop_interaction(events, (900, 300, 400, 300)) == events
    assert trim_stop_interaction(events, None) == events


# ---------- 回放 v3 ----------
def test_player_sends_dragged_event_type(monkeypatch):
    """按键保持期间的移动必须以 Dragged 类型投递。

    回归：v2 的 position setter 永远发 MouseMoved，拖拽在应用侧等同于
    "光标在动但没按住"，所有拖放操作无效。
    """
    p, fm, fk = make_player(monkeypatch)
    events = [
        MacroEvent(ts_ms=0, kind="move", x=100, y=100),
        MacroEvent(ts_ms=10, kind="mouse", x=100, y=100, button="left", pressed=True),
        MacroEvent(ts_ms=30, kind="move", x=140, y=100, dragged=True),
        MacroEvent(ts_ms=50, kind="mouse", x=140, y=100, button="left", pressed=False),
    ]
    p.play(events, PlayOptions())
    dragged = [m for m in fm.moves if m[2] == "left"]
    assert dragged, "拖拽期间应投递带按键的移动事件"
    assert fm.presses == [("down", "left", 1)]
    assert fm.releases == [("up", "left", 1)]


def test_player_passes_click_sequence_state(monkeypatch):
    """双击/三击的序列号必须透传到输出层（否则被应用识别成多次单击）。"""
    p, fm, fk = make_player(monkeypatch)
    events = [
        MacroEvent(ts_ms=0, kind="mouse", x=10, y=10, button="left", pressed=True, clicks=2),
        MacroEvent(ts_ms=40, kind="mouse", x=10, y=10, button="left", pressed=False, clicks=2),
    ]
    p.play(events, PlayOptions())
    assert fm.presses == [("down", "left", 2)]
    assert fm.releases == [("up", "left", 2)]


def test_player_wheel_uses_recorded_unit(monkeypatch):
    """滚轮按录制单位投递——v2 一律按 pixel 投递"行"增量，量级差一个数量级。"""
    p, fm, fk = make_player(monkeypatch)
    events = [
        MacroEvent(ts_ms=0, kind="wheel", x=10, y=10, wheel_dy=3, wheel_dx=-1,
                   wheel_unit="line"),
        MacroEvent(ts_ms=10, kind="wheel", x=10, y=10, wheel_dy=20, wheel_unit="pixel"),
    ]
    p.play(events, PlayOptions())
    assert fm.scrolls == [(-1, 3, "line"), (0, 20, "pixel")]


def test_player_catches_up_by_skipping_instead_of_bursting(monkeypatch):
    """落后时只跳帧投递目标点，绝不把剩余事件瞬时连发（时间线塌缩）。

    构造一段"计划时刻早已过去"的序列：v2 会因为预算转负而把全部事件挤在
    同一瞬间发出；v3 每个事件仍有各自的位置投递，且 skipped 计数可观测。
    """
    p, fm, fk = make_player(monkeypatch)
    events = [MacroEvent(ts_ms=0, kind="move", x=0, y=0)]
    events += [MacroEvent(ts_ms=i + 1, kind="move", x=(i + 1) * 30, y=0) for i in range(60)]
    stats = p.play(events, PlayOptions())
    assert stats["total"] == 61
    assert stats["skipped"] > 0                 # 确实发生了跳帧
    assert fm.moves[-1][0] == 60 * 30           # 落点仍然精确
    assert len(fm.moves) >= 61                  # 没有"整段塌缩成一次投递"


def test_player_slow_move_is_sampled_over_time(monkeypatch):
    """慢速移动按时间采样铺点，不因固定像素步长而跳变。"""
    p, fm, fk = make_player(monkeypatch)
    events = [MacroEvent(ts_ms=0, kind="move", x=0, y=0),
              MacroEvent(ts_ms=200, kind="move", x=12, y=0)]
    p.play(events, PlayOptions())
    assert fm.moves[-1][0] == 12
    # 12px 位移若按"10px 一步"只会有一点；按时间采样应铺出多个采样点
    assert len(fm.moves) > 3


