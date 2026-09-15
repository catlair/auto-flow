"""核心逻辑测试：不依赖真实屏幕状态（视觉层用注入 mock）。"""
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.events import (MacroEvent, Node, Workflow, Edge,
                         PORT_OUT, PORT_TRUE, PORT_FALSE, PORT_ELSE, case_port,
                         MAX_GROUP_CONDITIONS,
                         TARGET_SIDES, DEFAULT_TARGET_SIDE, normalize_target_side)
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
    """沿边走 + 注释节点跳过但不断链 + 工作流 repeat 生效。"""
    calls = []
    ex = Executor()
    ex.player.glide_now = lambda t: None  # 不动真鼠标
    wf = _graph(
        ("note", {"text": "skip"}),
        ("delay", {"ms": 1}),
        ("keyboard", {"mode": "text", "text": ""}),
        edges=[(0, PORT_OUT, 1), (1, PORT_OUT, 2)],
        repeat=2,
    )
    ex.run_workflow(wf, on_node=lambda i, t: calls.append(t))
    # note 不执行也不上报（它不是「跑过」的节点），但路径照样往下走
    assert calls == ["delay", "keyboard", "delay", "keyboard"]
    assert ex.running is False


# ---------- delay: 等待至时刻 ----------
def test_delay_seconds_until_parses_and_rolls_to_tomorrow():
    """HH:MM / HH:MM:SS / 宽松空白都能解析；已过或正等于当前时刻 → 明天。"""
    from datetime import datetime as dt
    from tasks.builtin import DelayTask
    f = DelayTask._seconds_until
    now = dt(2026, 9, 15, 10, 0, 0)
    assert f("10:30", now) == 1800
    assert f("9:30", dt(2026, 9, 15, 9, 30, 0)) == 86400      # 等于当前 → 明天
    assert f("09:30:15", dt(2026, 9, 15, 9, 0, 0)) == 1815
    assert f(" 10:30 ", now) == 1800                          # 首尾空白无所谓
    for bad in ("", "9", "9:30:00:00", "aa:bb", "24:00", "09:60", "09:00:61"):
        with pytest.raises(ValueError):
            f(bad, now)


def test_delay_until_mode_waits_in_wall_clock_chunks(monkeypatch):
    """等待至时刻：按墙钟剩余分段等待、**不受 speed 缩放**、剩余为 0 时结束。

    分段的理由：player.wait 走 monotonic，macOS 睡眠期间不走——等到明早 09:00
    的节点在机器睡一觉醒来后会多等一整段睡眠时间。每段结束用墙钟重算剩余才扛得住。
    """
    from core.executor import RunContext
    from tasks.builtin import DelayTask
    ex = Executor()
    waits = []
    monkeypatch.setattr(ex.player, "wait", lambda s: waits.append(s))
    remaining = [7.5]

    def fake_until(at, now=None):
        v, remaining[0] = remaining[0], 0.0
        return v

    monkeypatch.setattr(DelayTask, "_seconds_until", staticmethod(fake_until))
    ctx = RunContext(params={"mode": "等待至时刻", "at": "09:00"},
                     player=ex.player, speed=3.0)
    DelayTask().run(ctx)
    assert waits == [1.0], "7.5s 被 CHUNK_S 截到 1.0；第二段剩余 0 → 结束（speed=3 不得缩放）"


def test_delay_until_mode_stops_mid_wait(monkeypatch):
    """停止请求要在等待中即刻生效，不能等到「时刻到了」才停。"""
    from core.executor import RunContext
    from tasks.builtin import DelayTask
    ex = Executor()
    waits = []

    def wait(s):
        waits.append(s)
        ex.stop_run()

    monkeypatch.setattr(ex.player, "wait", wait)
    monkeypatch.setattr(DelayTask, "_seconds_until",
                        staticmethod(lambda at, now=None: 3600.0))
    # 与 executor 生产接线一致（见 Executor._run_node 的 is_stopping=lambda: ...）
    ctx = RunContext(params={"mode": "等待至时刻", "at": "09:00"}, player=ex.player,
                     is_stopping=lambda: ex._stop.is_set())
    DelayTask().run(ctx)
    assert waits == [1.0]


def test_delay_until_mode_exits_when_only_player_stopped(monkeypatch):
    """只停了 player、没停 executor 时也必须退出。

    回归：`player.wait` 在 player 自身被停时立刻返回，若此时 ctx.stopping 仍为
    False，循环就会**每秒空转**到墙钟自然走完（等 09:00 就是几小时 100% CPU）。
    """
    from core.executor import RunContext
    from tasks.builtin import DelayTask
    ex = Executor()
    waits = []

    def wait(s):
        waits.append(s)
        # 兜底失效时的失败模式是**无限空转**（每秒一次 wait，永远退不出），
        # 只靠末尾那句 `waits == [1.0]` 的话，整个 pytest 进程会先被超时杀掉，
        # 报出来的是「测试超时」而不是「是谁坏了」。在第二次 wait 就断掉，
        # 失败信息直接点名原因。（反向验证：删掉 builtin 里那道兜底 → 本用例失败）
        assert len(waits) == 1, "兜底失效：只停 player 时在空转，循环退不出去"
        ex.player.stop_playback()      # 只停 player，不动 executor._stop

    monkeypatch.setattr(ex.player, "wait", wait)
    monkeypatch.setattr(DelayTask, "_seconds_until",
                        staticmethod(lambda at, now=None: 3600.0))
    ctx = RunContext(params={"mode": "等待至时刻", "at": "09:00"}, player=ex.player)
    DelayTask().run(ctx)
    assert waits == [1.0], "应当立刻退出，而不是每秒空转"


def test_delay_duration_mode_keeps_speed_scaling():
    """老的「延时毫秒」用法不能被改坏：仍按 speed 缩放。"""
    from core.executor import RunContext
    from tasks.builtin import DelayTask
    waits = []
    player = Executor().player
    player.wait = lambda s: waits.append(s)  # type: ignore[method-assign]
    ctx = RunContext(params={"ms": 1000}, player=player, speed=2.0)
    DelayTask().run(ctx)
    assert waits == [0.5]


def test_delay_params_carry_mode_and_show_if():
    """参数面板按等待方式只显示相关字段（毫秒/时刻互斥）。"""
    from tasks.builtin import DelayTask
    by_key = {p.key: p for p in DelayTask().params}
    assert by_key["mode"].options == ["延时毫秒", "等待至时刻"]
    assert by_key["ms"].show_if == {"key": "mode", "eq": "延时毫秒"}
    assert by_key["at"].show_if == {"key": "mode", "eq": "等待至时刻"}


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


def _graph(*nodes, edges=(), **kw) -> Workflow:
    """按 (type, params) 建节点，边用**下标**写，省得每处手搓 uid。

    `edges` 每项是 `(源下标, 出口名, 目标下标)`。
    """
    ns = [Node(type=t, params=dict(p)) for t, p in nodes]
    es = [Edge(src=ns[a].uid, port=port, dst=ns[b].uid) for a, port, b in edges]
    return Workflow(nodes=ns, edges=es, **kw)


def _patch_wait(monkeypatch, ex, seen):
    """把 player.wait 换成记录器：delay 节点于是变成「记一笔 + 立即返回」。"""
    monkeypatch.setattr(ex.player, "wait", lambda s: seen.append(s))


def test_executor_follows_edges_not_list_order(monkeypatch):
    """执行顺序由**边**决定，不是由节点在列表里的顺序决定。"""
    ex = Executor()
    seen: list = []
    _patch_wait(monkeypatch, ex, seen)
    # 列表顺序是 A,B,C；边却要求 A → C → B
    wf = _graph(("delay", {"ms": 1}), ("delay", {"ms": 2}), ("delay", {"ms": 3}),
                edges=[(0, PORT_OUT, 2), (2, PORT_OUT, 1)])
    ex.run_workflow(wf)
    assert seen == [0.001, 0.003, 0.002]


def test_executor_condition_routes_by_port(monkeypatch):
    """条件成立走 true 出口，只执行挂在 true 上的那条分支。"""
    from core import vision
    monkeypatch.setattr(vision, "find_template",
                        lambda conf, template_path="": type("M", (), {"found": True})())
    ex = Executor()
    ran: list = []
    wf = _graph(
        ("condition", {"image_path": "x.png"}),   # 0
        ("delay", {"ms": 1}),                     # 1 ← true
        ("delay", {"ms": 2}),                     # 2 ← false
        edges=[(0, PORT_TRUE, 1), (0, PORT_FALSE, 2)],
    )
    ex.run_workflow(wf, on_node=lambda i, t: ran.append(i))
    assert ran == [0, 1]


def test_executor_condition_false_routes_to_other_branch(monkeypatch):
    """不成立走 false 出口——与上一条互为对照，证明真的在看条件结果。"""
    from core import vision
    monkeypatch.setattr(vision, "find_template",
                        lambda conf, template_path="": type("M", (), {"found": False})())
    ex = Executor()
    ran: list = []
    wf = _graph(
        ("condition", {"image_path": "x.png"}),
        ("delay", {"ms": 1}),
        ("delay", {"ms": 2}),
        edges=[(0, PORT_TRUE, 1), (0, PORT_FALSE, 2)],
    )
    ex.run_workflow(wf, on_node=lambda i, t: ran.append(i))
    assert ran == [0, 2]


def test_executor_branch_routes_to_matching_case(monkeypatch):
    """多路分支按序号取第一个命中的 case（顺序即优先级）。"""
    from core import vision
    seen_paths: list = []

    def fake(conf, template_path=""):
        seen_paths.append(template_path)
        return type("M", (), {"found": template_path == "b.png"})()

    monkeypatch.setattr(vision, "find_template", fake)
    ex = Executor()
    ran: list = []
    wf = _graph(
        ("branch", {"case_count": 3, "case1_value": "a.png", "case2_value": "b.png",
                    "case3_value": "c.png"}),
        ("delay", {"ms": 1}),      # case:1
        ("delay", {"ms": 2}),      # case:2
        ("delay", {"ms": 3}),      # case:3
        ("delay", {"ms": 4}),      # else
        edges=[(0, case_port(1), 1), (0, case_port(2), 2), (0, case_port(3), 3),
               (0, PORT_ELSE, 4)],
    )
    ex.run_workflow(wf, on_node=lambda i, t: ran.append(i))
    assert ran == [0, 2]
    # 命中之后**不再**继续测后面的 case —— 顺序就是优先级
    assert seen_paths == ["a.png", "b.png"]


def test_executor_branch_falls_back_to_else(monkeypatch):
    from core import vision
    monkeypatch.setattr(vision, "find_template",
                        lambda conf, template_path="": type("M", (), {"found": False})())
    ex = Executor()
    ran: list = []
    wf = _graph(
        ("branch", {"case_count": 2, "case1_value": "a.png", "case2_value": "b.png"}),
        ("delay", {"ms": 1}),
        ("delay", {"ms": 2}),
        ("delay", {"ms": 3}),
        edges=[(0, case_port(1), 1), (0, case_port(2), 2), (0, PORT_ELSE, 3)],
    )
    ex.run_workflow(wf, on_node=lambda i, t: ran.append(i))
    assert ran == [0, 3]


def _group_wf(combine, values, count=None, **extra):
    """建一张「条件组 + true 分支(delay 1) + false 分支(delay 2)」的图。

    返回 (workflow, seen)：`seen` 由 fake 检测器填，用来断言**到底测了哪几项**。
    """
    p = {"cond_count": count if count is not None else len(values),
         "combine": combine}
    p.update({f"cond{i}_value": v for i, v in enumerate(values, 1)})
    p.update(extra)
    return _graph(
        ("condition_group", p),   # 0
        ("delay", {"ms": 1}),     # 1 ← true
        ("delay", {"ms": 2}),     # 2 ← false
        edges=[(0, PORT_TRUE, 1), (0, PORT_FALSE, 2)],
    )


def _fake_detect(monkeypatch, hits, seen):
    """把 find_template 换成「路径在 hits 里才算找到」，并记录检测顺序。"""
    from core import vision

    def fake(conf, template_path=""):
        seen.append(template_path)
        return type("M", (), {"found": template_path in hits})()

    monkeypatch.setattr(vision, "find_template", fake)


def test_executor_condition_group_and_short_circuits(monkeypatch):
    """「与」：碰到第一个不成立就出结果，**后面那项不再检测**。

    短路不只是省时间（一次检测 = 截屏 + 匹配，是本节点里最贵的操作）：它还
    让「顺序」有了含义——用户把哪项排前面就是让它先被看。不短路的话顺序对
    结果毫无影响，用户就没法用它表达优先级（与 branch 的「先到先得」同理）。
    """
    seen: list = []
    _fake_detect(monkeypatch, {"a.png"}, seen)      # b、c 都不在
    ex = Executor()
    ran: list = []
    wf = _group_wf("全部成立(与)", ["a.png", "b.png", "c.png"])
    ex.run_workflow(wf, on_node=lambda i, t: ran.append(i))
    assert ran == [0, 2], "与：有项不成立 → 走 false"
    assert seen == ["a.png", "b.png"], "b 不成立后就不该再测 c"


def test_executor_condition_group_or_short_circuits(monkeypatch):
    """「或」：碰到第一个成立就出结果，后面那项不再检测。"""
    seen: list = []
    _fake_detect(monkeypatch, {"b.png"}, seen)      # 只有第二项成立
    ex = Executor()
    ran: list = []
    wf = _group_wf("任一成立(或)", ["a.png", "b.png", "c.png"])
    ex.run_workflow(wf, on_node=lambda i, t: ran.append(i))
    assert ran == [0, 1], "或：有项成立 → 走 true"
    assert seen == ["a.png", "b.png"], "b 成立后就不该再测 c"


def test_executor_condition_group_and_all_true(monkeypatch):
    """「与」全部成立走 true，而且**每一项都测过**（没有提前停）。"""
    seen: list = []
    _fake_detect(monkeypatch, {"a.png", "b.png", "c.png"}, seen)
    ex = Executor()
    ran: list = []
    wf = _group_wf("全部成立(与)", ["a.png", "b.png", "c.png"])
    ex.run_workflow(wf, on_node=lambda i, t: ran.append(i))
    assert ran == [0, 1]
    assert seen == ["a.png", "b.png", "c.png"]


def test_executor_condition_group_or_all_false(monkeypatch):
    """「或」全不成立走 false——与上一条互为对照，证明真的在看组合结果。"""
    seen: list = []
    _fake_detect(monkeypatch, set(), seen)
    ex = Executor()
    ran: list = []
    wf = _group_wf("任一成立(或)", ["a.png", "b.png", "c.png"])
    ex.run_workflow(wf, on_node=lambda i, t: ran.append(i))
    assert ran == [0, 2]
    assert seen == ["a.png", "b.png", "c.png"]


def test_condition_group_clamps_cond_count(monkeypatch):
    """cond_count 被钳制在 [2, MAX]：超上限不会去读不存在的 condN_* 键。

    用「或 + 全不成立」当探针：它不会短路，所以**检测次数就等于实际项数**。
    """
    seen: list = []
    _fake_detect(monkeypatch, set(), seen)
    ex = Executor()
    wf = _group_wf("任一成立(或)", ["a.png"] * MAX_GROUP_CONDITIONS, count=99)
    ex.run_workflow(wf)
    assert len(seen) == MAX_GROUP_CONDITIONS, "超上限要钳到 MAX，不能照 99 项去读"

    seen.clear()
    wf2 = _group_wf("任一成立(或)", ["a.png", "b.png"], count=1)
    ex2 = Executor()
    ex2.run_workflow(wf2)
    assert len(seen) == 2, "低于下限要抬到 2（只有 1 项的条件组等价于条件节点）"


def test_condition_group_missing_combine_defaults_to_and(monkeypatch):
    """老文件/手工编辑的 json 没有 combine 键时按默认的「与」算。

    落到「或」是静默的行为漂移：面板上显示的是「全部成立(与)」，执行却按
    「任一成立」——用户看到的和跑出来的不一致，且没有任何报错。
    """
    seen: list = []
    _fake_detect(monkeypatch, {"a.png"}, seen)
    ex = Executor()
    ran: list = []
    wf = _graph(
        ("condition_group", {"cond_count": 2,
                             "cond1_value": "a.png", "cond2_value": "b.png"}),  # 无 combine
        ("delay", {"ms": 1}),
        ("delay", {"ms": 2}),
        edges=[(0, PORT_TRUE, 1), (0, PORT_FALSE, 2)],
    )
    ex.run_workflow(wf, on_node=lambda i, t: ran.append(i))
    assert ran == [0, 2], "缺 combine 应按「与」算：b 不成立 → false"


def test_condition_group_stopping_goes_false(monkeypatch):
    """停机检查在检测**之前**：已停机时一次检测都不做，直接走 false。

    顺序反了的话，用户按下停止后还要等一整轮检测（截屏 + 匹配）才退出，
    而这一轮的结论还会被用来选出口——停机期间的动作全是白做的。
    """
    seen: list = []
    _fake_detect(monkeypatch, {"a.png"}, seen)
    ex = Executor()
    ex.stop_run()                      # 与真实停机同一条路径
    ports: list = []
    ctx = RunContext(params={"cond_count": 2, "combine": "全部成立(与)",
                             "cond1_value": "a.png", "cond2_value": "b.png"},
                     player=ex.player, set_port=ports.append)
    get_task("condition_group").run(ctx)
    assert ports == [PORT_FALSE]
    assert seen == [], "已停机就不该再做任何检测"


def test_condition_group_timeout_zero_evaluates_once(monkeypatch):
    """timeout_s=0：只求值一轮，不成立即走 false，不会反复检测。

    这是「不等待」的语义，也是默认值。若把它当成「无限等」，默认建出来的
    条件组会一直卡住不往下走。
    """
    seen: list = []
    _fake_detect(monkeypatch, set(), seen)
    ex = Executor()
    waits: list = []
    monkeypatch.setattr(ex.player, "wait", waits.append)
    ports: list = []
    ctx = RunContext(params={"cond_count": 3, "combine": "任一成立(或)",
                             "cond1_value": "a.png", "cond2_value": "b.png",
                             "cond3_value": "c.png"},
                     player=ex.player, set_port=ports.append)
    get_task("condition_group").run(ctx)
    assert ports == [PORT_FALSE]
    assert len(seen) == 3, "只求值一轮（3 项各一次）"
    assert waits == [], "超时为 0 时不该进入等待循环"


def test_condition_group_definition_shape():
    """节点定义：order 夹在 condition 与 branch 之间，条件数量带上限。"""
    defs = {d["type"]: d for d in all_definitions()}
    g = defs["condition_group"]
    assert g["name"] == "条件组"
    assert defs["condition"]["order"] < g["order"] < defs["branch"]["order"]

    params = {p["key"]: p for p in g["params"]}
    assert params["cond_count"]["max"] == MAX_GROUP_CONDITIONS
    assert params["cond_count"]["min"] == 2
    assert params["combine"]["options"] == ["全部成立(与)", "任一成立(或)"]
    # 第 N 项的 show_if 必须挂在 cond_count 上：面板平铺 6 项，不收起会太长
    assert params[f"cond{MAX_GROUP_CONDITIONS}_value"]["show_if"] == {
        "key": "cond_count", "gte": MAX_GROUP_CONDITIONS}
    assert params["cond1_value"]["pick"] is True
    # 超出上限的键不该存在——它们永远不会被渲染，只会误导读文件的人
    assert f"cond{MAX_GROUP_CONDITIONS + 1}_kind" not in params


def test_executor_merges_when_two_edges_point_at_one_node(monkeypatch):
    """两条边指向同一节点就是汇合——不需要特殊语法，到达即执行。"""
    from core import vision
    monkeypatch.setattr(vision, "find_template",
                        lambda conf, template_path="": type("M", (), {"found": True})())
    ex = Executor()
    seen: list = []
    _patch_wait(monkeypatch, ex, seen)
    wf = _graph(
        ("condition", {"image_path": "x.png"}),   # 0
        ("delay", {"ms": 1}),                     # 1  true 分支
        ("delay", {"ms": 2}),                     # 2  false 分支
        ("delay", {"ms": 9}),                     # 3  汇合点
        edges=[(0, PORT_TRUE, 1), (0, PORT_FALSE, 2),
               (1, PORT_OUT, 3), (2, PORT_OUT, 3)],
    )
    ex.run_workflow(wf)
    # 只走了 true 那条，但汇合点仍然执行到（且只执行一次）
    assert seen == [0.001, 0.009]


def test_executor_end_node_stops_that_path(monkeypatch):
    """end 节点终止本路径，不会再顺着列表往下跑。"""
    ex = Executor()
    ran: list = []
    wf = _graph(
        ("end", {}),                 # 0
        ("delay", {"ms": 1}),        # 1 排在 end 后面，但没有任何边指向它
        edges=[],
    )
    ex.run_workflow(wf, on_node=lambda i, t: ran.append(i))
    assert ran == [0]


def test_executor_disabled_node_passes_through(monkeypatch):
    """停用节点 = 跳过它自己，但**继续往下走**（不是截断路径）。"""
    ex = Executor()
    seen: list = []
    _patch_wait(monkeypatch, ex, seen)
    ns = [Node(type="delay", params={"ms": 1}),
          Node(type="delay", params={"ms": 2}, enabled=False),
          Node(type="delay", params={"ms": 3})]
    wf = Workflow(nodes=ns, edges=[
        Edge(src=ns[0].uid, port=PORT_OUT, dst=ns[1].uid),
        Edge(src=ns[1].uid, port=PORT_OUT, dst=ns[2].uid),
    ])
    ex.run_workflow(wf)
    assert seen == [0.001, 0.003]      # 中间那个没执行，但路径没断


def test_executor_missing_edge_ends_path(monkeypatch):
    """出口没有边 = 路径到此为止，不会「接着跑列表里的下一个」。"""
    ex = Executor()
    ran: list = []
    wf = _graph(("delay", {"ms": 1}), ("delay", {"ms": 2}), edges=[])
    ex.run_workflow(wf, on_node=lambda i, t: ran.append(i))
    assert ran == [0]


def test_executor_runaway_cycle_is_stopped_and_reported(monkeypatch):
    """成环的图必须被步数上限拦住并**报错停止**，而不是永远转下去。

    这是循环能力的必要代价：允许回边就等于允许写出死循环，没有上限的话
    界面会一直停在「运行中」，用户除了强杀进程没有别的办法。
    """
    ex = Executor()
    seen: list = []
    _patch_wait(monkeypatch, ex, seen)
    errors: list = []
    # 两个节点互指成环
    wf = _graph(("delay", {"ms": 1}), ("delay", {"ms": 1}),
                edges=[(0, PORT_OUT, 1), (1, PORT_OUT, 0)])
    ex.run_workflow(wf, on_error=lambda i, t, e: errors.append(str(e)))
    assert errors and "步数超过上限" in errors[0]
    assert ex._stop.is_set()
    # 必须**真的转了很多圈**才被砍：否则「一步就停」也能让上面两条通过，
    # 那守的就不是「死循环被拦住」而是别的东西了。
    assert len(seen) >= Executor.MAX_STEPS - 2


def test_executor_cycle_terminates_when_edge_is_dropped(monkeypatch):
    """反向对照：把回边去掉，同一张图就能正常跑完。

    没有这条，上面那条用例即使把「所有循环都当错误」也能通过。
    """
    ex = Executor()
    seen: list = []
    _patch_wait(monkeypatch, ex, seen)
    errors: list = []
    wf = _graph(("delay", {"ms": 1}), ("delay", {"ms": 2}),
                edges=[(0, PORT_OUT, 1)])
    ex.run_workflow(wf, on_error=lambda i, t, e: errors.append(str(e)))
    assert errors == []
    assert seen == [0.001, 0.002]


def test_executor_start_uid_wins_over_list_order(monkeypatch):
    """显式 start 指向谁就从谁开始，与列表顺序无关。"""
    ex = Executor()
    seen: list = []
    _patch_wait(monkeypatch, ex, seen)
    wf = _graph(("delay", {"ms": 1}), ("delay", {"ms": 2}),
                edges=[(1, PORT_OUT, 0)])
    wf.start = wf.nodes[1].uid
    ex.run_workflow(wf)
    assert seen == [0.002, 0.001]


def test_executor_falls_back_to_first_node_without_start(monkeypatch):
    """没有 start 节点 / 没指定 start 时退回第一个节点，保证旧图仍能跑。"""
    ex = Executor()
    seen: list = []
    _patch_wait(monkeypatch, ex, seen)
    wf = _graph(("delay", {"ms": 7}), ("delay", {"ms": 8}),
                edges=[(0, PORT_OUT, 1)])
    ex.run_workflow(wf)
    assert seen == [0.007, 0.008]


def test_workflow_load_migrates_linear_list(tmp_path):
    """v3 旧文件：接成线性边链，条件节点两个出口都接上（否则执行到它就断了）。"""
    old = {"version": 3, "name": "old", "nodes": [
        {"type": "delay", "params": {"ms": 1}, "uid": "a"},
        {"type": "condition", "params": {}, "uid": "b"},
        {"type": "delay", "params": {"ms": 2}, "uid": "c"},
    ]}
    p = str(tmp_path / "v3.json")
    json.dump(old, open(p, "w"))
    wf = Workflow.load(p)
    assert wf.migrated_from_list is True
    ports = {(e.src, e.port, e.dst) for e in wf.edges}
    assert ports == {("a", PORT_OUT, "b"),
                     ("b", PORT_TRUE, "c"), ("b", PORT_FALSE, "c")}


def test_migration_lays_nodes_out_instead_of_stacking_them(tmp_path):
    """迁移必须给节点摆开位置。

    x/y 是 v4 才有的字段，v3 文件里一个坐标都没有，全部落在 (0, 0)——
    画布上就是一摞完全重叠的卡片，看起来像「打开旧文件之后工作流被毁了」。
    """
    old = {"version": 3, "name": "old", "nodes": [
        {"type": "delay", "params": {}, "uid": f"n{i}"} for i in range(4)
    ]}
    p = str(tmp_path / "v3-pos.json")
    json.dump(old, open(p, "w"))
    wf = Workflow.load(p)

    coords = [(n.x, n.y) for n in wf.nodes]
    assert len(set(coords)) == len(coords), f"节点仍叠在一起：{coords}"
    # 横向排开（手柄在左右两侧，横向链的边才是直的），间距要大于卡片宽度
    assert [c[1] for c in coords] == [coords[0][1]] * len(coords), "应在同一行"
    xs = [c[0] for c in coords]
    assert xs == sorted(xs), "顺序应与原来的列表顺序一致"
    gaps = [b - a for a, b in zip(xs, xs[1:])]
    assert all(g > 180 for g in gaps), f"间距太挤，卡片会相叠：{gaps}"


def test_migration_keeps_hand_written_coordinates(tmp_path):
    """手工给旧文件补过坐标的人不该被覆盖。"""
    old = {"version": 3, "name": "old", "nodes": [
        {"type": "delay", "params": {}, "uid": "a", "x": 999, "y": 888},
        {"type": "delay", "params": {}, "uid": "b"},
    ]}
    p = str(tmp_path / "v3-hand.json")
    json.dump(old, open(p, "w"))
    wf = Workflow.load(p)
    assert (wf.nodes[0].x, wf.nodes[0].y) == (999, 888)
    # 没给坐标的那个仍然要被摆开，不能留在 (0, 0) 和别的节点叠
    assert (wf.nodes[1].x, wf.nodes[1].y) != (0, 0)


def test_v4_load_does_not_touch_positions(tmp_path):
    """已经是 v4 的文件：坐标是用户亲手摆的，一个都不能动。"""
    p = str(tmp_path / "v4-pos.json")
    json.dump({"version": 4, "name": "v4", "nodes": [
        {"type": "delay", "params": {}, "uid": "a", "x": 0, "y": 0},
        {"type": "delay", "params": {}, "uid": "b", "x": 0, "y": 0},
    ], "edges": []}, open(p, "w"))
    wf = Workflow.load(p)
    assert [(n.x, n.y) for n in wf.nodes] == [(0, 0), (0, 0)]


def test_workflow_load_keeps_v4_edges_untouched(tmp_path):
    """已经是 v4 的文件不能被迁移逻辑改写——包括「用户故意删光了所有边」这种情况。"""
    p = str(tmp_path / "v4.json")
    json.dump({"version": 4, "name": "v4", "nodes": [
        {"type": "delay", "params": {}, "uid": "a"},
        {"type": "delay", "params": {}, "uid": "b"},
    ], "edges": []}, open(p, "w"))
    wf = Workflow.load(p)
    assert wf.edges == []
    assert wf.migrated_from_list is False


def test_migrated_flag_is_not_persisted(tmp_path):
    """迁移标记只在本次会话里提示用，不该被写进文件（否则每次打开都提示一次）。"""
    wf = Workflow(nodes=[Node(type="delay", params={})], migrated_from_list=True)
    assert "migrated_from_list" not in wf.to_dict()


def test_edge_dst_side_round_trips_and_defaults(tmp_path):
    """边的落点侧（画布上从目标节点哪一侧画进去）必须能存能读。

    它是**纯展示字段**、不参与执行，所以最容易在改数据结构时被漏掉——而漏掉的
    表现不是报错，是「打开文件后连线全跑到左边去了」，没人会当成 bug 去查。
    """
    p = str(tmp_path / "side.json")
    wf = Workflow(nodes=[Node(type="delay", uid="a"), Node(type="delay", uid="b")],
                  edges=[Edge(src="a", port=PORT_OUT, dst="b", dst_side="bottom")])
    wf.save(p)
    back = Workflow.load(p)
    assert [e.dst_side for e in back.edges] == ["bottom"]

    # 旧文件没有这个字段 → 读到默认侧，与「边一律从左边进」的历史行为一致
    old = str(tmp_path / "old.json")
    json.dump({"version": 4, "name": "old", "nodes": [
        {"type": "delay", "params": {}, "uid": "a"},
        {"type": "delay", "params": {}, "uid": "b"},
    ], "edges": [{"src": "a", "port": PORT_OUT, "dst": "b"}]}, open(old, "w"))
    assert [e.dst_side for e in Workflow.load(old).edges] == [DEFAULT_TARGET_SIDE]


def test_normalize_target_side_converges_dirty_values():
    """脏值必须收敛到默认侧。

    存了脏值前端就找不到对应手柄，Vue Flow 直接画不出这条边（只在控制台刷告警），
    用户看到的是「连线莫名消失」——比画错一侧严重得多。

    「right」是**故意不在词表里**的：右边是输出侧（出口手柄都在卡片右边缘），
    单出口节点的出口手柄正好在右边缘中线，与右目标手柄同点重合——Vue Flow 按
    「离指针最近的手柄」判定落点，两点重合时行为不稳定。这条断言是防止有人
    为了「四边对称」把它加回来。
    """
    assert "right" not in TARGET_SIDES, "右侧会与出口手柄同点重合，不能加进落点词表"
    assert TARGET_SIDES[0] == DEFAULT_TARGET_SIDE, "第一个即默认侧，前端按这个顺序渲染"
    for good in TARGET_SIDES:
        assert normalize_target_side(good) == good
    assert normalize_target_side(" left ") == "left", "前后空白应被吃掉"
    for dirty in ["right", "middle", "LEFT", "", None, 123, "out", True]:
        assert normalize_target_side(dirty) == DEFAULT_TARGET_SIDE, dirty


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
    for t in ["start", "end", "branch", "record_replay", "image_click", "ocr_click",
              "yolo_click", "condition", "mouse", "keyboard", "delay", "note"]:
        assert t in types, t
        d = [x for x in all_definitions() if x["type"] == t][0]
        assert d["name"] and isinstance(d["params"], list)


def test_node_defaults_applied():
    task = get_task("delay")
    assert task.defaults()["ms"] == 500


# §9.4：菜单顺序 = 开始 → 鼠标 → 键盘 → 延时 → 录制回放 → 图像 → OCR → YOLO
#         → 条件 → 条件组 → 多路分支 → 注释 → 结束
BUILTIN_MENU_ORDER = [
    "start", "mouse", "keyboard", "delay", "record_replay",
    "image_click", "ocr_click", "yolo_click", "condition", "condition_group",
    "branch", "note", "end",
]


def test_node_menu_order_is_explicit():
    """内置节点的菜单顺序 = §9.4 规定顺序（产品需求的回归护栏）。

    注意：**这条用例无法区分「按 order 排」与「按注册顺序排」**——给所有类都
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
    # 数量也必须对上。**这一条才是「新增节点忘了登记」的唯一护栏**：
    # 只用 defs[:n] == BUILTIN_MENU_ORDER 的话，新节点的 order 若比现有都大
    # （追加到菜单末尾，最常见），这个切片仍然等于旧列表 → 测试通过，
    # 而新节点完全没被任何用例覆盖。
    assert len(defs) == n, (
        "注册的节点数与 BUILTIN_MENU_ORDER 不一致（新增内置节点时必须同步它）："
        f"{sorted({d['type'] for d in defs} ^ set(BUILTIN_MENU_ORDER))}"
    )
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


def test_trim_stop_interaction_drops_only_the_stop_click():
    """按钮停止：只丢"点停止按钮"这一次点击，它之前的事件一律原样保留。

    曾经的实现会在截断后再"弹出尾部连续移动"（当时认为是移向按钮的路径），
    结果把 [move(30)] 也删了。移动轨迹是用户的数据，不能当噪声。
    """
    from core.recorder import trim_stop_interaction
    ev = lambda **kw: MacroEvent(**kw)  # noqa: E731
    events = [
        ev(ts_ms=0, kind="move", x=300, y=300),
        ev(ts_ms=10, kind="mouse", x=300, y=300, button="left", pressed=True),
        ev(ts_ms=20, kind="mouse", x=300, y=300, button="left", pressed=False),
        ev(ts_ms=30, kind="move", x=900, y=400),          # 用户内容，必须保留
        ev(ts_ms=40, kind="mouse", x=1000, y=400, button="left", pressed=True),   # 点停止
        ev(ts_ms=45, kind="mouse", x=1000, y=400, button="left", pressed=False),
    ]
    out = trim_stop_interaction(events, (900, 300, 400, 300), end_ts_ms=45)
    assert [e.ts_ms for e in out] == [0, 10, 20, 30]   # 只丢停止点击及其后的残余


def test_trim_keeps_the_whole_recording_when_it_is_pure_movement():
    """回归 2026-09-12：一段纯移动的录制 + 末尾一次停止点击，裁完不能变空。

    真实数据：captured=470 / decimated=151 / trimmed=319 / count=0。
    即 7.8 秒的鼠标移动全被"弹出尾部移动"吃掉了。
    """
    from core.recorder import trim_stop_interaction
    ev = lambda **kw: MacroEvent(**kw)  # noqa: E731
    moves = [ev(ts_ms=i * 16, kind="move", x=600 + i, y=400) for i in range(300)]
    stop_click = ev(ts_ms=4800, kind="mouse", x=1000, y=400,
                    button="left", pressed=True)
    events = moves + [stop_click]
    out = trim_stop_interaction(events, (900, 300, 400, 300), end_ts_ms=4850)
    assert len(out) == 300                     # 移动一条不少
    assert all(e.kind == "move" for e in out)


def test_trim_refuses_click_that_is_not_recent():
    """停止点击必须刚刚发生；最后那个窗口内按下离结束太久 → 那是真实操作，不裁。"""
    from core.recorder import trim_stop_interaction
    ev = lambda **kw: MacroEvent(**kw)  # noqa: E731
    events = [
        ev(ts_ms=0, kind="move", x=600, y=400),
        ev(ts_ms=100, kind="mouse", x=1000, y=400, button="left", pressed=True),
        ev(ts_ms=110, kind="mouse", x=1000, y=400, button="left", pressed=False),
        ev(ts_ms=5000, kind="move", x=610, y=400),
    ]
    # 按下发生在 100ms，录制到 5000ms 才停 → 距结束 4900ms，远超窗口
    assert trim_stop_interaction(events, (900, 300, 400, 300),
                                end_ts_ms=5000) == events


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


# ---------- 录制事件编辑 ----------
def _ctrl_with_events():
    from rpc.controller import AppController
    from core.events import RecordResult
    ctrl = AppController()
    ctrl._last_record = RecordResult(events=[
        MacroEvent(ts_ms=0, kind="move", x=500, y=500),
        MacroEvent(ts_ms=10, kind="move", x=501, y=500),
        MacroEvent(ts_ms=20, kind="mouse", x=501, y=500, button="left", pressed=True),
        MacroEvent(ts_ms=40, kind="mouse", x=501, y=500, button="left", pressed=False),
        MacroEvent(ts_ms=50, kind="key", key="a", pressed=True),
    ], origin_x=500, origin_y=500)
    return ctrl


def test_record_remove_by_index_and_array():
    ctrl = _ctrl_with_events()
    r = ctrl.record_remove(0)
    assert r["count"] == 4 and r["can_undo"] is True
    # 删掉第一条后序列为 [move, down, up, key]，再批量删 [1,2] 即删掉按下/释放
    r = ctrl.record_remove([1, 2])
    assert r["count"] == 2
    assert [e["kind"] for e in r["events"]] == ["move", "key"]
    ctrl.shutdown()


def test_record_remove_ignores_out_of_range_indexes():
    """越界下标忽略而不是报错——UI 可能因为并发刷新拿到过期下标。"""
    ctrl = _ctrl_with_events()
    r = ctrl.record_remove([-1, 99, 0])
    assert r["count"] == 4
    ctrl.shutdown()


def test_record_remove_moves_before_keeps_real_actions():
    """删掉开头"把光标移过去"的移动，点击/按键/滚轮保留。"""
    ctrl = _ctrl_with_events()
    r = ctrl.record_remove_moves_before(2)
    assert [e["kind"] for e in r["events"]] == ["mouse", "mouse", "key"]
    ctrl.shutdown()


def test_record_set_origin_follows_selected_event():
    ctrl = _ctrl_with_events()
    r = ctrl.record_set_origin(3)
    assert r["origin"] == [501, 500]
    ctrl.shutdown()


def test_record_undo_restores_previous_state():
    ctrl = _ctrl_with_events()
    ctrl.record_remove(0)
    assert ctrl.record_current()["count"] == 4
    r = ctrl.record_undo()
    assert r["count"] == 5
    assert r["can_undo"] is False           # 栈已空
    try:
        ctrl.record_undo()
        raise AssertionError("空栈应报错")
    except Exception as e:  # noqa: BLE001
        assert "nothing_to_undo" in str(e)
    ctrl.shutdown()


def test_record_edit_without_recording_raises():
    from rpc.controller import AppController
    ctrl = AppController()
    try:
        ctrl.record_remove(0)
        raise AssertionError("无录制结果应报错")
    except Exception as e:  # noqa: BLE001
        assert "no_record_result" in str(e)
    ctrl.shutdown()


def test_record_to_node_uses_edited_events():
    """写入节点必须取编辑后的序列——编辑的就是权威序列本身。"""
    ctrl = _ctrl_with_events()
    ctrl.record_remove([0, 1])
    ctrl.record_to_node()
    node = ctrl.workflow.nodes[-1]
    assert node.type == "record_replay"
    assert [e["kind"] for e in node.params["events"]] == ["mouse", "mouse", "key"]
    ctrl.shutdown()


# ---------- 文本提交（输入法 / Unicode 投递） ----------
def test_is_text_commit_classification():
    """文本提交判据：每一条"误判"都必须对应等价的回放方式，不能有丢信息的分支。"""
    from core.mackeys import is_text_commit

    # 是文本提交
    assert is_text_commit(0, "你好")        # 输入法上屏
    assert is_text_commit(0, "🎯")          # emoji
    assert is_text_commit(0, "nihao")       # 单次按键产生不了多字符
    assert is_text_commit(0, "你")
    assert is_text_commit(0x0A, "你")       # 键码未知 + 非 ASCII

    # 不是文本提交
    assert not is_text_commit(0, "")        # 无内容
    assert not is_text_commit(0, "a")       # keycode 0 就是物理 A 键，按按键回放等价
    assert not is_text_commit(0, "1")
    assert not is_text_commit(0x24, "\r")   # Return
    assert not is_text_commit(0x30, "\t")   # Tab
    assert not is_text_commit(0x35, "\x1b")  # Escape
    assert not is_text_commit(0x7E, "\x1e")  # 方向键的控制码
    assert not is_text_commit(0x7E, "\uf700")  # 方向键的私有区表示（回放会打出乱码）
    assert not is_text_commit(0x21, "å")    # 普通键的布局字符：按物理键回放更忠实


def _patch_tap_quartz(monkeypatch, maclistener, text, pid):
    """把 tap 回调依赖的 Quartz 调用换成可控假实现，返回可变状态（可改 pid）。"""
    q = maclistener.Quartz
    state = {"pid": pid, "text": text}
    monkeypatch.setattr(q, "CGEventGetIntegerValueField",
                        lambda ev, f: state["pid"] if f == q.kCGEventSourceUnixProcessID else 0)
    monkeypatch.setattr(q, "CGEventGetLocation",
                        lambda ev: type("Loc", (), {"x": 5.0, "y": 6.0})())
    monkeypatch.setattr(q, "CGEventGetFlags", lambda ev: 0)
    monkeypatch.setattr(q, "CGEventKeyboardGetUnicodeString",
                        lambda ev, n, a, b: (len(state["text"]), state["text"]))
    monkeypatch.setattr(maclistener, "vk_to_name",
                        lambda kc: {0: "a", 0x24: "Return"}.get(kc))
    return state


def test_maclistener_dispatches_text_and_suppresses_synthetic_pair(monkeypatch):
    """文本提交走 on_text；其后由进程投递的合成配对 keyUp 必须被抑制。

    回归：keycode 0 同时是物理 A 键的键码，合成配对 keyUp 若不过滤会被录成
    一次"A 键释放"，在事件流里凭空多出一条脏事件。
    """
    from core import maclistener
    keys, texts = [], []
    lis = maclistener.MacKeyboardListener(
        lambda n, p, x=0, y=0, fl=0: keys.append((n, p)),
        lambda t, x, y: texts.append((t, x, y)))
    state = _patch_tap_quartz(monkeypatch, maclistener, "你好", pid=4242)

    lis._callback(None, maclistener._KEY_DOWN, object(), None)   # 文本提交
    lis._callback(None, maclistener._KEY_UP, object(), None)     # 合成配对 → 抑制

    assert texts == [("你好", 5, 6)]
    assert keys == []

    # 真实 a 键：keycode 0、内容是可打印 ASCII → 是按键，不是文本
    state["text"] = "a"
    lis._callback(None, maclistener._KEY_DOWN, object(), None)
    lis._callback(None, maclistener._KEY_UP, object(), None)
    assert texts == [("你好", 5, 6)]
    assert keys == [("a", True), ("a", False)]


def test_maclistener_never_swallows_hardware_keyup(monkeypatch):
    """来源 PID 为 0（硬件）时不抑制——宁可多录一条 keyUp，也不吞掉真实按键。"""
    from core import maclistener
    keys = []
    lis = maclistener.MacKeyboardListener(lambda n, p, x=0, y=0, fl=0: keys.append((n, p)))
    state = _patch_tap_quartz(monkeypatch, maclistener, "你好", pid=0)

    lis._callback(None, maclistener._KEY_DOWN, object(), None)   # 文本提交（无 on_text）
    lis._callback(None, maclistener._KEY_UP, object(), None)     # 硬件来源 → 照常派发
    assert keys == [("a", False)]


def test_recorder_merges_consecutive_text_commits():
    """连续上屏聚合为一条 text 事件，时间戳取首段（回放从正确时刻开始）。"""
    from core.recorder import Recorder
    rec = Recorder()
    for i, ch in enumerate("你好世界"):
        rec._accept(MacroEvent(ts_ms=i * 100, kind="text", text=ch, x=10, y=20))
    rec._flush_text()
    r = rec.result()
    assert len(r.events) == 1
    assert r.events[0].kind == "text" and r.events[0].text == "你好世界"
    assert r.events[0].ts_ms == 0
    assert r.n_text_merged == 3


def test_recorder_starts_new_text_event_after_pause():
    """停顿超过合流窗口即另起一条，保留"打字中间停过"的节奏。"""
    from core.recorder import Recorder, TEXT_JOIN_MS
    rec = Recorder()
    rec._accept(MacroEvent(ts_ms=0, kind="text", text="你", x=1, y=2))
    rec._accept(MacroEvent(ts_ms=TEXT_JOIN_MS + 1, kind="text", text="好", x=1, y=2))
    rec._flush_text()
    r = rec.result()
    assert [e.text for e in r.events] == ["你", "好"]
    assert r.n_text_merged == 0


def test_recorder_flushes_text_before_other_events_keeping_order():
    """非文本事件到达前必须先落盘，否则顺序颠倒（变成先点击后打字）。"""
    from core.recorder import Recorder
    rec = Recorder()
    rec._accept(MacroEvent(ts_ms=0, kind="text", text="你好", x=5, y=5))
    rec._accept(MacroEvent(ts_ms=10, kind="mouse", x=5, y=5, button="left", pressed=True))
    r = rec.result()
    assert [(e.kind, e.text) for e in r.events] == [("text", "你好"), ("mouse", "")]


def test_recorder_text_merge_keeps_captured_invariant():
    """captured − filtered − limit − text_merged == count（前端一致性校验的口径）。

    回归：聚合会让 count 小于 captured，若前端不扣掉 text_merged，
    每录一次中文都会误报"系统层丢事件"。
    """
    from core.recorder import Recorder
    rec = Recorder()
    for ts in (0, 10, 20):
        rec._push(MacroEvent(ts_ms=ts, kind="text", text="你", x=1, y=1))
    rec._push(MacroEvent(ts_ms=30, kind="mouse", x=1, y=1, button="left", pressed=True))
    rec.poll()
    r = rec.result()
    assert [e.kind for e in r.events] == ["text", "mouse"]
    assert r.n_text_merged == 2
    assert r.n_captured - r.n_filtered - r.n_limit_dropped - r.n_text_merged == len(r.events)


def test_recorder_flushes_text_when_input_pauses(monkeypatch):
    """停顿超过合流窗口即落盘——否则最后一段输入要等"下一个事件"才出现在流里，
    录制面板看着像卡住了。"""
    from core import recorder as rec_mod
    rec = rec_mod.Recorder()
    rec._start_ms = 1000
    clock = {"t": 1000}
    monkeypatch.setattr(rec_mod, "now_ms", lambda: clock["t"])

    rec._push(MacroEvent(ts_ms=0, kind="text", text="你好", x=1, y=1))
    assert rec.poll() == []                                  # 刚打完：还在等后续提交
    clock["t"] = 1000 + rec_mod.TEXT_JOIN_MS
    assert [e.text for e in rec.poll()] == ["你好"]            # 停顿到位 → 落盘


def test_player_types_text_events_without_moving_cursor(monkeypatch):
    """文本事件走 Unicode 通道投递，且不挪动光标（与 key 事件一致）。"""
    from core import player as player_mod
    p, fm, fk = make_player(monkeypatch)
    typed = []
    monkeypatch.setattr(player_mod, "type_text", lambda t: typed.append(t))
    fm.pos = (321.0, 654.0)
    events = [
        MacroEvent(ts_ms=0, kind="text", text="你好", x=10, y=10),
        MacroEvent(ts_ms=5, kind="key", key="a", pressed=True),
        MacroEvent(ts_ms=10, kind="text", text="🎯", x=10, y=10),
    ]
    p.play(events, PlayOptions(use_relative=True, base_x=0, base_y=0))
    assert typed == ["你好", "🎯"]
    assert fm.pos == (321.0, 654.0)


def test_tolerant_event_carries_text_and_v3_defaults():
    """v3 新增字段全部有默认值：旧脚本导入不报错，text 原样保留。"""
    from tasks.builtin import tolerant_event
    ev = tolerant_event({"ts_ms": 5, "kind": "text", "text": "你好"})
    assert ev.kind == "text" and ev.text == "你好"
    old = tolerant_event({"ts_ms": 1, "kind": "key", "key": "a"})
    assert old.text == "" and old.dragged is False and old.clicks == 1
    assert old.flags == 0 and old.wheel_unit == "line"


def test_record_set_text_edits_only_text_events():
    """改写文本事件内容，且撤销能回到旧内容。

    回归：撤销栈存的是 list(events)（浅拷贝，元素同一批对象）。若原地
    `ev.text = ...`，快照会被一起改掉，撤销永远回不到旧内容。
    """
    from rpc.controller import AppController
    from core.events import RecordResult
    ctrl = AppController()
    ctrl._last_record = RecordResult(events=[
        MacroEvent(ts_ms=0, kind="text", text="你好", x=1, y=2),
        MacroEvent(ts_ms=5, kind="key", key="a", pressed=True),
    ])
    r = ctrl.record_set_text(0, "您好")
    assert r["events"][0]["text"] == "您好"
    assert r["can_undo"] is True
    assert ctrl.record_undo()["events"][0]["text"] == "你好"

    for bad, msg in ((1, "不是文本事件"), (99, "下标越界")):
        try:
            ctrl.record_set_text(bad, "x")
            raise AssertionError("应报错")
        except Exception as e:  # noqa: BLE001
            assert msg in str(e)
    ctrl.shutdown()


def test_record_keys_to_text_collapses_a_pinyin_run():
    """把一段连续按键整段换成一条 text 事件，让 IME 中文走上确定性回放。

    2026-09-12 实测：录 `nihao`+空格、回放时重新驱动输入法确实能得到「你好」，
    但结果依赖回放瞬间的输入法状态与候选顺序。换成一条 text 事件后走 Unicode
    通道（`type_text`），不经过输入法，结果确定；事件表里也能读懂在打什么。

    范围规则：从选中下标向两侧扩到**最大连续按键段**，只能吞 `kind="key"`，
    前后非按键事件必须原样保留；新事件的时间戳与坐标取该段第一条，回放起点不变。
    """
    from rpc.controller import AppController
    from core.events import RecordResult

    def pinyin_run(ts0=100):
        """n i h a o Space 的按下/抬起对，坐标沿用同一个值。"""
        evs, ts = [], ts0
        for k in ("n", "i", "h", "a", "o", "space"):
            for pressed in (True, False):
                evs.append(MacroEvent(ts_ms=ts, kind="key", key=k,
                                      pressed=pressed, x=640, y=360))
                ts += 30
        return evs

    ctrl = AppController()
    ctrl._last_record = RecordResult(events=(
        [MacroEvent(ts_ms=0, kind="move", x=1, y=2)]
        + pinyin_run()
        + [MacroEvent(ts_ms=500, kind="mouse", button="left",
                      pressed=True, x=9, y=9)]
    ))
    total = len(ctrl._last_record.events)          # 1 + 12 + 1
    assert total == 14

    r = ctrl.record_keys_to_text(5, "你好")          # 下标落在按键段中间
    evs = r["events"]
    assert len(evs) == 3, "整段按键应塌成一条 text，前后非按键事件原样保留"
    assert evs[0]["kind"] == "move" and evs[0]["ts_ms"] == 0
    assert evs[2]["kind"] == "mouse" and evs[2]["ts_ms"] == 500
    mid = evs[1]
    assert mid["kind"] == "text" and mid["text"] == "你好"
    assert mid["ts_ms"] == 100 and (mid["x"], mid["y"]) == (640, 360)
    assert r["can_undo"] is True

    back = ctrl.record_undo()["events"]
    assert len(back) == total
    assert [e["kind"] for e in back].count("key") == 12

    for call, msg in (
        (lambda: ctrl.record_keys_to_text(0, "x"), "不是按键事件"),  # move
        (lambda: ctrl.record_keys_to_text(99, "x"), "下标越界"),
        (lambda: ctrl.record_keys_to_text(2, ""), "文本不能为空"),
    ):
        try:
            call()
            raise AssertionError("应报错")
        except Exception as e:  # noqa: BLE001
            assert msg in str(e), f"期望 {msg}，实际 {e}"

    # 全是「抬起」的一段（例如上一个动作的尾巴）不能当成一次输入
    ctrl._last_record = RecordResult(events=[
        MacroEvent(ts_ms=0, kind="key", key="a", pressed=False),
        MacroEvent(ts_ms=10, kind="key", key="b", pressed=False),
    ])
    try:
        ctrl.record_keys_to_text(0, "x")
        raise AssertionError("应报错")
    except Exception as e:  # noqa: BLE001
        assert "没有按下事件" in str(e)
    ctrl.shutdown()


def test_input_probe_refuses_while_busy():
    """自检会投递真实输入：录制中/运行中必须拒绝。

    录制中投递会污染正在录的事件序列（凭空多一条文本事件），运行中投递会把字符
    打进正在回放的流程里。
    """
    from rpc.controller import AppController
    ctrl = AppController()
    for attr in ("recording", "running"):
        setattr(ctrl, attr, True)
        try:
            ctrl.input_probe()
            raise AssertionError(f"{attr} 期间应拒绝自检")
        except Exception as e:  # noqa: BLE001
            assert "busy" in str(e)
        setattr(ctrl, attr, False)
    ctrl.shutdown()


def test_probe_text_callback_requires_marker():
    """自检的文本段必须校验探针载荷。

    否则自检那零点几秒里用户自己敲的中文也会把标记点亮，"链路通"变成假阳性——
    而这正是最需要判断准的一次结论。
    """
    from rpc.controller import AppController, _PROBE_TEXT
    ctrl = AppController()
    ctrl._probing = True
    ctrl._on_text("你好", 0, 0)                 # 用户自己打的 → 不算命中
    assert ctrl._probe_text_hit is False
    ctrl._on_text(f"x{_PROBE_TEXT}y", 0, 0)     # 探针载荷 → 命中
    assert ctrl._probe_text_hit is True

    ctrl._probing = False
    ctrl._probe_text_hit = False
    ctrl._on_text(_PROBE_TEXT, 0, 0)            # 非自检期间一律忽略
    assert ctrl._probe_text_hit is False
    ctrl.shutdown()


def test_input_probe_keeps_key_listener_wired_to_text():
    """自检用的热键 listener 必须接上文本回调，否则文本段永远不命中。"""
    from rpc.controller import AppController
    from core.maclistener import MacKeyboardListener
    ctrl = AppController()
    ctrl._ensure_key_listener()
    assert isinstance(ctrl._key_listener, MacKeyboardListener)
    assert ctrl._key_listener._on_text == ctrl._on_text
    ctrl._key_listener.stop()
    ctrl.shutdown()


def test_event_channel_unsupported_only_covers_verified_ime():
    """只有**实测确认**的输入法才被判为"不经过事件层"。

    2026-09-12 实测：系统拼音（SCIM）上屏走 insertText:，不投递任何 CGEvent。
    但"不在名单里"**不等于**"支持"——UI 必须把"未知"和"确认不支持"分开说，
    否则又会造出一个"看起来有答案"的假结论。
    """
    from core.inputsource import event_channel_unsupported as unsup
    assert unsup("com.apple.inputmethod.SCIM.ITABC") is True    # 系统拼音：已实测
    assert unsup("com.apple.inputmethod.SCIM") is True
    assert unsup("com.apple.keylayout.ABC") is False            # 未实测，只能说"不在名单"
    assert unsup("com.thirdparty.unknown.IME") is False
    assert unsup(None) is None and unsup("") is None            # 未知


def test_input_probe_reports_input_source_and_never_dies_on_it():
    """自检要一并报出当前输入法（并标注是否确认不支持事件通道）。

    只报 text_alive=true 会被读成"中文能录"，而系统拼音下它恒为 true、中文却恒
    录不到。输入源探测只是诊断信息，**失败也不能让自检整体失败**。
    """
    from rpc.controller import AppController
    import core.inputsource as ins

    ctrl = AppController()
    orig_src = ins.current_input_source
    try:
        ins.current_input_source = lambda: {
            "id": "com.apple.inputmethod.SCIM.ITABC", "name": "Pinyin – Simplified"}
        got = ctrl._input_source_info()
        assert got["id"].endswith("ITABC") and got["name"]
        assert got["event_channel_unsupported"] is True
        # 未在名单里的输入法：报 False（= 未经实测），不能报 True
        ins.current_input_source = lambda: {
            "id": "com.apple.keylayout.ABC", "name": "ABC"}
        assert ctrl._input_source_info()["event_channel_unsupported"] is False
        # 探测失败 → 空 dict，且不抛
        ins.current_input_source = lambda: None
        assert ctrl._input_source_info() == {}
    finally:
        ins.current_input_source = orig_src
        ctrl.shutdown()


def test_record_stop_unaccounted_is_zero_after_trim():
    """按钮停止（trim 为真）裁掉停止点击后，一致性等式仍必须配平。

    回归：4b16c52 曾把 trimmed 并进 filtered 修过一次；v3 重写把 filtered 与
    trimmed 拆成两个字段，等式又被打破——于是**每次用按钮停止录制**（且尾部有
    可识别的停止点击）都会误报「检测到系统层丢事件，请反馈」。

    另回归 2026-09-12：这里同时锁住"裁掉的只有停止点击那 2 条"，以及裁剪
    可被 record.undo 整段还原（拿不回来的裁剪不接受）。
    """
    from rpc.controller import AppController
    from core.events import RecordResult

    class _FakeRec:
        def __init__(self, result):
            self._result = result
            self._win_bounds = (900, 300, 400, 300)

        def stop(self):
            return self._result

        def set_window_bounds(self, b):
            self._win_bounds = b

        def elapsed_ms(self):
            return 45          # 停止点击(40ms)距录制结束(45ms)很近 → 是停止点击

    ev = lambda **kw: MacroEvent(**kw)  # noqa: E731
    result = RecordResult(events=[
        ev(ts_ms=0, kind="move", x=300, y=300),
        ev(ts_ms=10, kind="mouse", x=300, y=300, button="left", pressed=True),
        ev(ts_ms=20, kind="mouse", x=300, y=300, button="left", pressed=False),
        ev(ts_ms=30, kind="move", x=1000, y=400),                               # 用户内容，保留
        ev(ts_ms=40, kind="mouse", x=1000, y=400, button="left", pressed=True),  # 点停止
        ev(ts_ms=45, kind="mouse", x=1000, y=400, button="left", pressed=False),
    ], n_captured=6)

    ctrl = AppController()
    ctrl.recording = True
    ctrl.recorder = _FakeRec(result)
    stats = ctrl.record_stop(trim=True, window_bounds=(900, 300, 400, 300))
    assert stats["trimmed"] == 2          # 只有停止点击的按下+抬起
    assert stats["count"] == 4
    assert stats["unaccounted"] == 0
    # 裁剪是破坏性的，必须能撤销回原样
    assert ctrl._record_undo, "裁剪前应押入撤销快照"
    ctrl.record_undo()
    assert len(ctrl._last_record.events) == 6
    ctrl.shutdown()


def test_record_undo_after_trim_restores_monotonic_timestamps():
    """裁剪后撤销，必须拿回**未被改动**的原序列（含时间戳）。

    回归 2026-09-12：撤销快照曾是 `list(events)` 浅拷贝，而紧随裁剪之后的
    "轨迹终点补全"是**原地**改 `events[-1].ts_ms`。裁剪后最后那条移动在裁剪前
    也在序列里（同一个对象），于是补全把快照里的它一起改了。撤销回来得到
    `[0, 10, 20, 45, 40, 45]`——第 3 条的 45 超过了它后面的停止点击 40，
    **序列时间戳倒退**，回放时序错乱。撤消承诺的是"整段还原"，就不该留改动。
    """
    from rpc.controller import AppController
    from core.events import RecordResult

    class _FakeRec:
        def __init__(self, result):
            self._result = result
            self._win_bounds = (900, 300, 400, 300)

        def stop(self):
            return self._result

        def set_window_bounds(self, b):
            self._win_bounds = b

        def elapsed_ms(self):
            return 45          # 终点补全会把最后那条移动的时间戳推到 45

    ev = lambda **kw: MacroEvent(**kw)  # noqa: E731
    original_ts = [0, 10, 20, 30, 40, 45]
    result = RecordResult(events=[
        ev(ts_ms=0, kind="move", x=300, y=300),
        ev(ts_ms=10, kind="mouse", x=300, y=300, button="left", pressed=True),
        ev(ts_ms=20, kind="mouse", x=300, y=300, button="left", pressed=False),
        ev(ts_ms=30, kind="move", x=1000, y=400),                              # 用户内容
        ev(ts_ms=40, kind="mouse", x=1000, y=400, button="left", pressed=True),
        ev(ts_ms=45, kind="mouse", x=1000, y=400, button="left", pressed=False),
    ], n_captured=6)

    ctrl = AppController()
    ctrl.recording = True
    ctrl.recorder = _FakeRec(result)
    ctrl.record_stop(trim=True, window_bounds=(900, 300, 400, 300))

    # 裁剪后的序列：终点补全把最后那条移动推到 45 → [0, 10, 20, 45]，单调
    kept_ts = [e.ts_ms for e in ctrl._last_record.events]
    assert kept_ts == [0, 10, 20, 45]
    assert all(a <= b for a, b in zip(kept_ts, kept_ts[1:]))

    back = [e["ts_ms"] for e in ctrl.record_undo()["events"]]
    assert back == original_ts, f"撤销应原样还原，实际 {back}"
    assert all(a <= b for a, b in zip(back, back[1:])), f"时间戳倒退: {back}"
    ctrl.shutdown()


def test_record_stop_without_trim_keeps_everything():
    """快捷键停止（trim=False）一条都不能丢：F9 已在 skip_keys 里，没有停止点击。"""
    from rpc.controller import AppController
    from core.events import RecordResult

    class _FakeRec:
        def __init__(self, result):
            self._result = result
            self._win_bounds = (900, 300, 400, 300)

        def stop(self):
            return self._result

        def set_window_bounds(self, b):
            self._win_bounds = b

        def elapsed_ms(self):
            return 4850

    ev = lambda **kw: MacroEvent(**kw)  # noqa: E731
    moves = [ev(ts_ms=i * 16, kind="move", x=600 + i, y=400) for i in range(20)]
    # 窗口内的真实点击（用户自己的操作），快捷键停止时绝不能动它
    result = RecordResult(events=moves + [
        ev(ts_ms=1300, kind="mouse", x=1000, y=400, button="left", pressed=True),
        ev(ts_ms=1310, kind="mouse", x=1000, y=400, button="left", pressed=False),
    ], n_captured=22)

    ctrl = AppController()
    ctrl.recording = True
    ctrl.recorder = _FakeRec(result)
    stats = ctrl.record_stop(trim=False, window_bounds=(900, 300, 400, 300))
    assert stats["trimmed"] == 0
    assert stats["count"] == 22
    assert stats["unaccounted"] == 0
    ctrl.shutdown()


def test_record_to_node_keeps_text_events():
    """文本事件必须原样写进节点（含 text 字段），否则回放时中文整段消失。"""
    from rpc.controller import AppController
    from core.events import RecordResult
    ctrl = AppController()
    ctrl._last_record = RecordResult(events=[
        MacroEvent(ts_ms=0, kind="text", text="你好", x=1, y=2),
    ])
    ctrl.record_to_node()
    events = ctrl.workflow.nodes[-1].params["events"]
    assert events[0]["kind"] == "text" and events[0]["text"] == "你好"
    ctrl.shutdown()


