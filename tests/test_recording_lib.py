"""录制库：录下来的东西落盘成可复用资产，流程图节点按 id 引用。

测试重心在**不会报错但结果不对**的几条：
- 按 id 取到了别的文件（id 直接拼路径，不校验就能写到任意位置）；
- 库里改了、节点回放还用旧的（节点存了 origin/events 副本就会有第二份真源）；
- 老流程文件（没有 `source` 字段）的行为被悄悄改掉；
- 编辑之后库里那份过期了却不提示（界面显示已同步、回放跑旧序列）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core import recording_lib
from core.events import MacroEvent, Node, RecordResult
from core.executor import RunContext
from core.player import Player
from tasks.base import get_task


def _ev(ts, x=0, y=0):
    return {"ts_ms": ts, "kind": "move", "x": x, "y": y}


# --------------------------------------------------------------------------- #
# 存储层
# --------------------------------------------------------------------------- #
def test_save_list_load_roundtrip():
    meta = recording_lib.save("测试录制", [_ev(0, 1, 2), _ev(30, 3, 4)], (100, 200))
    assert meta["name"] == "测试录制"
    assert meta["count"] == 2
    assert meta["duration_ms"] == 30
    assert meta["origin"] == [100, 200]
    assert [m["id"] for m in recording_lib.list_meta()] == [meta["id"]]
    data = recording_lib.load(meta["id"])
    assert len(data["events"]) == 2
    assert data["events"][1]["x"] == 3


def test_list_meta_does_not_carry_events():
    """列表只给元信息：一条录制几百 KB，列表一起传过去要几 MB，而且列表用不到。"""
    recording_lib.save("a", [_ev(i) for i in range(50)])
    items = recording_lib.list_meta()
    assert items and all("events" not in m for m in items)
    assert items[0]["count"] == 50


def test_id_is_whitelisted_against_path_traversal():
    """id 是直接拼进路径的：`../workflows/x` 能覆盖用户的流程文件。

    生成端只产生 [0-9a-f]，但校验必须在**读**这一侧——手工改过的文件名、
    将来换了生成方式，都不能绕过。
    """
    for bad in ["../evil", "../../workflows/x", "a/b", "", "a b", ".", "..",
                "a" * 200, "x.json/index"]:
        with pytest.raises(ValueError):
            recording_lib.load(bad)
        with pytest.raises(ValueError):
            recording_lib.remove(bad)
    for bad in [None, 123, ["a"]]:
        with pytest.raises(ValueError):
            recording_lib.load(bad)


def test_load_missing_raises_valueerror():
    with pytest.raises(ValueError):
        recording_lib.load("nope-nope")


def test_corrupt_entry_is_skipped_not_fatal():
    """一条坏文件不能让整个库打不开——面板全空比少一条严重得多。"""
    good = recording_lib.save("好的", [_ev(0)])
    d = recording_lib.recordings_dir()
    with open(os.path.join(d, "broken0000.json"), "w", encoding="utf-8") as f:
        f.write("{ this is not json")
    assert [m["id"] for m in recording_lib.list_meta()] == [good["id"]]
    with pytest.raises(ValueError):
        recording_lib.load("broken0000")


def test_rename_keeps_created_at_so_order_is_stable():
    """改名不该让条目跳到列表最前：列表按创建时间排，跳位看着像新建了一条。"""
    a = recording_lib.save("甲", [_ev(0)])
    recording_lib.save("乙", [_ev(0)])
    before = [m["id"] for m in recording_lib.list_meta()]
    created = recording_lib.load(a["id"])["created_at"]
    recording_lib.rename(a["id"], "甲改名了")
    after = recording_lib.load(a["id"])
    assert after["name"] == "甲改名了"
    assert after["created_at"] == created, "改名把创建时间冲掉了"
    assert [m["id"] for m in recording_lib.list_meta()] == before


def test_save_with_same_id_overwrites_in_place():
    """「改完推给所有引用它的节点」全靠覆盖**同一个 id**——换成新建，引用就断了。"""
    a = recording_lib.save("原名", [_ev(0)])
    b = recording_lib.save("新名", [_ev(0), _ev(5)], (1, 2), rid=a["id"])
    assert b["id"] == a["id"]
    assert b["count"] == 2
    assert len(recording_lib.list_meta()) == 1


def test_save_leaves_no_tmp_files():
    """写了临时文件不清理会在库里堆垃圾；而库里任何东西都会出现在面板上。"""
    recording_lib.save("x", [_ev(0)])
    leftovers = [f for f in os.listdir(recording_lib.recordings_dir())
                 if ".tmp" in f]
    assert leftovers == []


def test_meta_of_tolerates_garbage():
    """元信息是面板的输入，不能因为一条手改坏的记录把面板打崩。"""
    m = recording_lib.meta_of({"events": "not-a-list", "origin": "x"})
    assert m["count"] == 0 and m["origin"] == [0, 0]
    m2 = recording_lib.meta_of({"events": [{"ts_ms": "abc"}], "origin": [1]})
    assert m2["duration_ms"] == 0 and m2["origin"] == [0, 0]
    assert recording_lib.meta_of({})["name"] == "(未命名)"


# --------------------------------------------------------------------------- #
# 节点：按 id 取录制
# --------------------------------------------------------------------------- #
def _player(monkeypatch):
    plays = []
    p = Player()
    monkeypatch.setattr(p, "play", lambda evs, opt, on_progress=None: plays.append((evs, opt)))
    return p, plays


def test_node_reads_events_from_library_so_edits_propagate(monkeypatch):
    """节点只存 id：在库里改这条录制，所有引用它的节点一起变。

    这正是录制库存在的理由——所以这里连「改完再跑一遍」一起验，
    只验「能取到」是测不出「取的是最新那份」的。
    """
    meta = recording_lib.save("库里的", [_ev(0, 7, 8)], (300, 400))
    task = get_task("record_replay")
    p, plays = _player(monkeypatch)
    ctx = RunContext(params={"source": "录制库", "recording_id": meta["id"]},
                     player=p, base_x=0, base_y=0)
    task.run(ctx)
    evs, opt = plays[0]
    assert len(evs) == 1 and evs[0].x == 7
    # 原点取自**库里那条**；节点里根本没有 origin_x/origin_y
    assert (opt.origin_x, opt.origin_y) == (300, 400)

    recording_lib.save("库里的", [_ev(0, 9, 9), _ev(10, 1, 1)], (300, 400), rid=meta["id"])
    task.run(ctx)
    assert len(plays[1][0]) == 2 and plays[1][0][0].x == 9


def test_node_library_source_without_id_raises():
    """选了「录制库」却没选具体哪条 → 必须报错，不能静默跑一个空序列。"""
    task = get_task("record_replay")
    ctx = RunContext(params={"source": "录制库", "recording_id": ""}, player=Player())
    with pytest.raises(ValueError):
        task.run(ctx)


def test_node_library_recording_deleted_raises():
    """库里那条被删了要**响**：静默跳过会表现成「节点跑过了但什么都没发生」。"""
    task = get_task("record_replay")
    ctx = RunContext(params={"source": "录制库", "recording_id": "deleted-000000"},
                     player=Player())
    with pytest.raises(ValueError):
        task.run(ctx)


def test_node_without_source_key_keeps_old_behaviour(monkeypatch):
    """老流程文件里没有 `source` 字段——行为必须一个字都不变。

    若默认值写成「录制库」，所有已保存的流程打开后都会变成「没选录制」而全部报错。
    """
    task = get_task("record_replay")
    p, plays = _player(monkeypatch)
    ctx = RunContext(params={"events": [{"ts_ms": 0, "kind": "move", "x": 10, "y": 20}],
                             "origin_x": 5, "origin_y": 6},
                     player=p, base_x=0, base_y=0)
    task.run(ctx)
    evs, opt = plays[0]
    assert len(evs) == 1 and evs[0].x == 10
    assert (opt.origin_x, opt.origin_y) == (5, 6)


def test_node_params_declare_source_and_recording_id():
    from tasks.builtin import RecordReplayTask
    by_key = {p.key: p for p in RecordReplayTask().params}
    assert by_key["source"].options == ["内嵌", "录制库"]
    assert by_key["source"].default == "内嵌"
    assert by_key["recording_id"].ptype == "recording"
    assert by_key["recording_id"].show_if == {"key": "source", "eq": "录制库"}
    # 事件与原点只在内嵌时显示：库里那条自带事件和原点，摆出来就成了两份真源
    for k in ("events", "origin_x", "origin_y"):
        assert by_key[k].show_if == {"key": "source", "eq": "内嵌"}, k


# --------------------------------------------------------------------------- #
# 控制器：自动入库 / 库操作 / 落节点
# --------------------------------------------------------------------------- #
class _FakeRecorder:
    """替掉真 Recorder：测试环境不该装全局事件 tap（会碰真实键鼠）。"""

    def __init__(self, events=None, **_kw):
        self._events = list(events or [])
        self._win_bounds = None

    def start(self):
        pass

    def poll(self):
        return []

    def stop(self):
        return RecordResult(events=list(self._events), origin_x=11, origin_y=22,
                            n_captured=len(self._events))

    def set_window_bounds(self, b):
        self._win_bounds = b

    def elapsed_ms(self):
        return 0


def _ctrl(monkeypatch, events):
    import core.recorder as rec_mod
    from rpc.controller import AppController
    monkeypatch.setattr(rec_mod, "Recorder", lambda **kw: _FakeRecorder(events, **kw))
    return AppController()


def _mv(ts, x=0, y=0):
    return MacroEvent(ts_ms=ts, kind="move", x=x, y=y)


def test_record_stop_auto_saves_to_library(monkeypatch):
    """录完自动入库：只留在内存里的话，「刚才那段不错」是关掉面板之后才发现的。"""
    ctrl = _ctrl(monkeypatch, [_mv(0, 1, 2)])
    ctrl.record_start()
    stats = ctrl.record_stop()
    assert stats["count"] == 1
    assert stats["library"]["count"] == 1
    assert stats["library"]["name"].startswith("录制 ")
    assert [m["id"] for m in ctrl.record_lib_list()["items"]] == [stats["library"]["id"]]
    assert ctrl._record_payload()["lib"]["dirty"] is False


def test_empty_recording_is_not_saved(monkeypatch):
    """点一下按钮又点回来 = 0 条：放进库里纯属噪音。"""
    ctrl = _ctrl(monkeypatch, [])
    ctrl.record_start()
    stats = ctrl.record_stop()
    assert stats["count"] == 0
    assert "library" not in stats
    assert ctrl.record_lib_list()["items"] == []


def test_editing_marks_library_entry_stale(monkeypatch):
    """编辑后界面必须能看出「库里那份过期了」。

    节点按 id 引用库，不提示的话用户以为改动已经生效，回放跑的却是旧序列——
    而画布、参数面板上都看不出任何异常。
    """
    ctrl = _ctrl(monkeypatch, [_mv(0, 1, 2), _mv(10, 3, 4)])
    ctrl.record_start()
    ctrl.record_stop()
    assert ctrl._record_payload()["lib"]["dirty"] is False

    ctrl.record_remove([0])
    payload = ctrl._record_payload()
    assert payload["count"] == 1
    assert payload["lib"]["dirty"] is True, "删了一条却没标脏"

    saved = ctrl.record_lib_save(rid=payload["lib"]["id"])
    assert saved["current"]["dirty"] is False
    assert saved["item"]["count"] == 1


def test_undo_also_marks_stale(monkeypatch):
    """撤销走的是另一条码路（不经过 _record_snapshot），别漏了标脏。"""
    ctrl = _ctrl(monkeypatch, [_mv(0, 1, 2), _mv(10, 3, 4)])
    ctrl.record_start()
    ctrl.record_stop()
    ctrl.record_remove([0])
    ctrl.record_lib_save(rid=ctrl._record_payload()["lib"]["id"])
    assert ctrl._record_payload()["lib"]["dirty"] is False
    ctrl.record_undo()
    assert ctrl._record_payload()["lib"]["dirty"] is True


def test_from_lib_loads_into_buffer_and_delete_clears_link(monkeypatch):
    ctrl = _ctrl(monkeypatch, [_mv(0, 1, 2), _mv(5, 3, 4)])
    ctrl.record_start()
    meta = ctrl.record_stop()["library"]

    ctrl._last_record = None            # 模拟「面板上已经清空了」
    payload = ctrl.record_from_lib(meta["id"])
    assert payload["count"] == 2
    assert payload["origin"] == [11, 22]
    assert payload["lib"]["id"] == meta["id"] and payload["lib"]["dirty"] is False

    out = ctrl.record_lib_delete(meta["id"])
    assert out["deleted"] == meta["id"]
    assert ctrl._record_payload()["lib"] is None
    assert ctrl.record_lib_list()["items"] == []
    # 库没了但内存里的事件还在——用户仍能写入节点或另存一条
    assert ctrl._record_payload()["count"] == 2


def test_lib_to_node_creates_referencing_node(monkeypatch):
    """从库里落节点：节点只存 id + 来源，**不带事件副本**。"""
    ctrl = _ctrl(monkeypatch, [_mv(0, 1, 2)])
    ctrl.record_start()
    meta = ctrl.record_stop()["library"]
    out = ctrl.record_lib_to_node(meta["id"])
    node = ctrl.workflow.nodes[-1]
    assert node.type == "record_replay"
    assert node.params["source"] == "录制库"
    assert node.params["recording_id"] == meta["id"]
    assert node.params["events"] == []
    # 广播出去的节点要带名字：画布上只显示一串 20260915-190433-a1b2c3 没人认得
    assert out["node"]["params"]["recording_name"] == meta["name"]


def test_public_node_injects_recording_name_only_for_library_nodes(monkeypatch):
    ctrl = _ctrl(monkeypatch, [])
    meta = recording_lib.save("有名字的", [_ev(0)])
    lib_node = Node(type="record_replay",
                    params={"source": "录制库", "recording_id": meta["id"]})
    pub = ctrl._public_node(lib_node)
    assert pub["params"]["recording_name"] == "有名字的"
    assert pub["params"]["recording_count"] == 1

    inline = ctrl._public_node(Node(type="record_replay", params={"events": [_ev(0)]}))
    assert "recording_name" not in inline["params"]
    # 引用了一个不存在的 id：不能抛，只是没名字（否则画布整个刷不出来）
    ghost = ctrl._public_node(Node(type="record_replay",
                                   params={"source": "录制库", "recording_id": "ghost-000000"}))
    assert ghost["params"]["recording_name"] == ""


def test_library_list_is_cached_until_a_write(monkeypatch):
    """`_public_node` 每次广播都查名字，不能每次都去扫目录。"""
    ctrl = _ctrl(monkeypatch, [])
    ctrl._lib_cache = None
    first = ctrl._lib_metas()
    assert ctrl._lib_metas() is first, "没写操作却又扫了一遍目录"
    recording_lib.save("新来的", [_ev(0)])
    ctrl.record_lib_list()          # 读接口不置空缓存（它自己直接扫）
    assert ctrl._lib_metas() is first
    ctrl._lib_invalidate()
    assert ctrl._lib_metas() is not first
