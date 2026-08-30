"""核心逻辑测试：不依赖真实屏幕状态（视觉层用注入 mock）。"""
import json
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
    """不创建真实 tap，验证 _dispatch 对无名键码的过滤。"""
    from core import maclistener
    got = []
    lis = maclistener.MacKeyboardListener(lambda n, p: got.append((n, p)))
    lis._dispatch("a", True)
    lis._dispatch(None, True)     # 未知键码应被丢弃
    lis._dispatch("Return", False)
    assert got == [("a", True), ("Return", False)]



def test_paths_dirs(tmp_path, monkeypatch):
    from core import paths
    monkeypatch.setattr(paths, "app_dir", lambda: str(tmp_path))
    d = paths.workflows_dir()
    t = paths.templates_dir()
    assert os.path.isdir(d) and os.path.isdir(t)
