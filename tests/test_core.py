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
        self.releases = []
        self.presses = []

    @property
    def position(self):
        return self.pos

    @position.setter
    def position(self, v):
        self.pos = (float(v[0]), float(v[1]))

    def press(self, b):
        self.presses.append(("down", str(b)))

    def release(self, b):
        self.releases.append(("up", str(b)))

    def scroll(self, dx, dy):
        pass


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
    """不创建真实 tap，验证 _dispatch 对无名键码的过滤（含坐标透传）。"""
    from core import maclistener
    got = []
    lis = maclistener.MacKeyboardListener(lambda n, p, x=0, y=0: got.append((n, p, x, y)))
    lis._dispatch("a", True, 10, 20)
    lis._dispatch(None, True, 10, 20)   # 未知键码应被丢弃
    lis._dispatch("Return", False, 30, 40)
    assert got == [("a", True, 10, 20), ("Return", False, 30, 40)]



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


def test_trim_stop_interaction():
    """按钮停止时必须裁掉停止交互：末次点击 + 移向按钮的移动。"""
    from core.recorder import trim_stop_interaction
    E = lambda **kw: MacroEvent(**kw)
    events = [
        E(ts_ms=0, kind="move", x=100, y=100),
        E(ts_ms=200, kind="mouse", x=200, y=200, button="left", pressed=True),   # 打开 dock
        E(ts_ms=300, kind="mouse", x=200, y=200, button="left", pressed=False),
        E(ts_ms=400, kind="mouse", x=300, y=300, button="left", pressed=True),   # 关闭应用
        E(ts_ms=500, kind="mouse", x=300, y=300, button="left", pressed=False),
        E(ts_ms=600, kind="move", x=900, y=420),                                 # 移向停止按钮
        E(ts_ms=700, kind="mouse", x=950, y=430, button="left", pressed=True),   # 停止点击
        E(ts_ms=800, kind="mouse", x=950, y=430, button="left", pressed=False),
    ]
    out, _ = trim_stop_interaction(events)
    # 回放终止于「关闭应用」点击；移向停止按钮的路径与停止点击一并移除
    assert len(out) == 5 and out[-1].pressed is False and out[-1].x == 300
    # 热键停止（F9 已被 skip_keys 过滤）不应误裁：末次点击是实质动作
    hotkey_stop = events[:-2]  # 假设 F9 停止：无停止点击，尾部是移动
    out2, _ = trim_stop_interaction(hotkey_stop)
    # 无 trim 标志时调用方不会调用；但函数自身对「末尾无按下」的情形裁移动——
    # 热键路径根本不调用本函数，这里仅验证纯函数行为可预期
    assert len(out2) <= len(hotkey_stop)
    # 全移动序列：末次按下为停止点击，裁后为空
    only_moves = [E(ts_ms=0, kind="move", x=1, y=1),
                  E(ts_ms=100, kind="mouse", x=2, y=2, button="left", pressed=True),
                  E(ts_ms=150, kind="mouse", x=2, y=2, button="left", pressed=False)]
    assert trim_stop_interaction(only_moves)[0] == []
