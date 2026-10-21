"""工作流库：流程图落盘成库里可管理的资产（列表 / 打开 / 更名 / 删除 / 导入导出）。

测试重心在**不会报错但结果不对**的几条：
- 按 id 取到了别的文件（id 直接拼路径，不校验就能写到任意位置）；
- 「新建 → 保存」静默失败，用户以为存下了；
- 删除把用户数据真删了（而不是进废纸篓）；
- 未保存的改动被「新建 / 打开另一份」无声丢掉；
- 改名让条目在列表里跳位（看着像多了一份）。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core import workflow_lib
from rpc.controller import AppController, ControllerError


def _wf(name="测试流程", nodes=None):
    return {"version": 4, "name": name, "speed": 1.0, "repeat": 1, "start": "",
            "nodes": nodes if nodes is not None else [], "edges": []}


def _touch(path, offset):
    t = time.time() + offset
    os.utime(path, (t, t))


# --------------------------------------------------------------------------- #
# 存储层
# --------------------------------------------------------------------------- #
def test_save_list_load_roundtrip():
    meta = workflow_lib.save("登录流程", _wf("登录流程", [{"type": "delay", "params": {"ms": 5}}]))
    assert meta["name"] == "登录流程"
    assert meta["node_count"] == 1
    assert meta["created_at"]
    assert [m["id"] for m in workflow_lib.list_meta()] == [meta["id"]]
    data = workflow_lib.load(meta["id"])
    assert data["name"] == "登录流程" and len(data["nodes"]) == 1


def test_list_meta_does_not_carry_nodes():
    """列表只给元信息：一份流程几十个节点，节点参数里还可能内嵌几 MB 的录制事件。"""
    workflow_lib.save("a", _wf(nodes=[{"type": "delay", "params": {"ms": i}} for i in range(30)]))
    items = workflow_lib.list_meta()
    assert items and all("nodes" not in m and "edges" not in m for m in items)
    assert items[0]["node_count"] == 30


def test_id_is_whitelisted_against_path_traversal():
    """id 是直接拼进路径的：`../recordings/x` 能覆盖录制库里的文件。

    生成端只产生 [0-9a-f] 与 `-`，但校验必须在**读**这一侧——手工改过的文件名、
    将来换了生成方式，都不能绕过。
    """
    for bad in ["../evil", "../../recordings/x", "a/b", "", "a b", ".", "..",
                "a" * 200, "x.json/index"]:
        with pytest.raises(ValueError):
            workflow_lib.load(bad)
        with pytest.raises(ValueError):
            workflow_lib.remove(bad)
        with pytest.raises(ValueError):
            workflow_lib.path_of(bad)
    for bad in [None, 123, ["a"]]:
        with pytest.raises(ValueError):
            workflow_lib.load(bad)


def test_id_of_path_only_accepts_direct_children_of_the_library():
    """`templates/` 就在库目录下面——把它当库条目会让「另存为到模板」变成覆盖某条流程。"""
    d = workflow_lib.workflows_dir()
    wid = workflow_lib.save("甲", _wf())["id"]
    assert workflow_lib.id_of_path(workflow_lib.path_of(wid)) == wid
    for p in ["", None, "/tmp/elsewhere.json", os.path.join(d, "templates", f"{wid}.json"),
              os.path.join(d, "nested", f"{wid}.json"), os.path.join(d, "no-extension"),
              os.path.join(d, "not a valid id.json"), os.path.join(d, f"{wid}.json.bak")]:
        assert workflow_lib.id_of_path(p) == "", p


def test_load_missing_raises_valueerror():
    with pytest.raises(ValueError):
        workflow_lib.load("nope-nope")


def test_corrupt_entry_is_skipped_not_fatal():
    """一条坏文件不能让整个库打不开——列表全空比少一条严重得多。"""
    good = workflow_lib.save("好的", _wf())
    d = workflow_lib.workflows_dir()
    with open(os.path.join(d, "broken0000.json"), "w", encoding="utf-8") as f:
        f.write("{ this is not json")
    assert [m["id"] for m in workflow_lib.list_meta()] == [good["id"]]
    with pytest.raises(ValueError):
        workflow_lib.load("broken0000")


def test_save_leaves_no_tmp_files():
    """写了临时文件不清理会在库里堆垃圾；而库里任何东西都会出现在列表上。"""
    workflow_lib.save("x", _wf())
    assert [f for f in os.listdir(workflow_lib.workflows_dir()) if ".tmp" in f] == []


def test_save_with_same_id_overwrites_in_place_and_keeps_created_at():
    """「保存」必须是覆盖**同一条**：总新建的话，用户改完一按保存就多出一份。"""
    a = workflow_lib.save("原名", _wf("原名"))
    created = workflow_lib.load(a["id"])["created_at"]
    b = workflow_lib.save("新名", _wf("新名", [{"type": "delay", "params": {"ms": 1}}]), wid=a["id"])
    assert b["id"] == a["id"]
    assert b["node_count"] == 1
    assert len(workflow_lib.list_meta()) == 1
    assert workflow_lib.load(a["id"])["created_at"] == created, "覆盖把创建时间冲掉了"


def test_list_is_sorted_by_recent_modification():
    """列表用文件 mtime 而不是 JSON 里的时间戳：用户完全可能用 Finder / 编辑器
    直接改库里的文件，那时 JSON 里的时间戳是陈旧的，而列表会显示一个骗人的时间。"""
    a = workflow_lib.save("甲", _wf())
    b = workflow_lib.save("乙", _wf())
    _touch(workflow_lib.path_of(a["id"]), -100)
    _touch(workflow_lib.path_of(b["id"]), -50)
    assert [m["id"] for m in workflow_lib.list_meta()] == [b["id"], a["id"]]
    _touch(workflow_lib.path_of(a["id"]), +100)      # 甲变成最近改过的
    assert [m["id"] for m in workflow_lib.list_meta()] == [a["id"], b["id"]]
    assert all(m["updated_at"] for m in workflow_lib.list_meta())


def test_rename_keeps_position_and_does_not_dirty_the_file():
    """改名不是修改内容，不该让它在列表里跳到最前——跳位看着像新建了一份。"""
    a = workflow_lib.save("甲", _wf())
    b = workflow_lib.save("乙", _wf())
    _touch(workflow_lib.path_of(a["id"]), -100)
    _touch(workflow_lib.path_of(b["id"]), -50)
    before = [m["id"] for m in workflow_lib.list_meta()]
    assert before == [b["id"], a["id"]]

    meta = workflow_lib.rename(a["id"], "甲改名了")
    assert meta["name"] == "甲改名了"
    data = workflow_lib.load(a["id"])
    assert data["name"] == "甲改名了"
    assert len(data["nodes"]) == 0, "改名把节点弄丢了"
    assert [m["id"] for m in workflow_lib.list_meta()] == before


def test_meta_of_tolerates_garbage():
    """元信息是列表的输入，不能因为一条手改坏的记录把整个列表打崩。"""
    m = workflow_lib.meta_of({"nodes": "not-a-list", "edges": 3})
    # `len("not-a-list")` 也是 10——不判类型就会凭空造出一个「10 个节点」的条目
    assert m["node_count"] == 0 and m["edge_count"] == 0
    assert workflow_lib.meta_of({})["name"] == "(未命名)"
    assert workflow_lib.meta_of("junk")["node_count"] == 0
    # 兼容「裸事件脚本」：加载后会变成一个录制回放节点，列表也该显示 1 个节点
    assert workflow_lib.meta_of({"events": [{"ts_ms": 0}]})["node_count"] == 1
    assert workflow_lib.meta_of({"events": []})["node_count"] == 0


def test_meta_id_comes_from_the_filename():
    """JSON 里的 `id` 只是副本：用户手工复制一份文件时它必然和文件名对不上，
    而对不上的表现是「点开列表项打开了另一份流程」。"""
    m = workflow_lib.meta_of({"id": "other-id", "nodes": []}, "real-id")
    assert m["id"] == "real-id"


# --------------------------------------------------------------------------- #
# 删除：进废纸篓
# --------------------------------------------------------------------------- #
def test_remove_moves_the_file_to_trash_instead_of_deleting():
    """这是**用户数据**：直接删掉没有第二次机会。"""
    meta = workflow_lib.save("要删的", _wf())
    path = workflow_lib.path_of(meta["id"])
    dest = workflow_lib.remove(meta["id"])
    assert not os.path.exists(path), "原文件还在"
    assert os.path.exists(dest), "废纸篓里没有——那就是真删了"
    assert os.path.dirname(dest) == workflow_lib.trash_dir()
    assert workflow_lib.list_meta() == []
    # 内容完整地留在废纸篓里，用户捞回来还能用
    with open(dest, "r", encoding="utf-8") as f:
        assert json.load(f)["name"] == "要删的"


def test_remove_never_touches_the_real_trash():
    """兜底自检：conftest 必须把废纸篓指到临时目录。

    这条断言看着像在测 conftest，但它守的是一个**会污染用户机器**的失误：
    少了这个隔离，跑一次测试就往 `~/.Trash` 里塞一堆文件，而用户不会知道
    那些是测试留下的。
    """
    assert workflow_lib.trash_dir() != os.path.expanduser("~/.Trash")


def test_remove_missing_raises():
    with pytest.raises(ValueError):
        workflow_lib.remove("nope-nope")


def test_remove_twice_raises_the_second_time():
    """第二次删不能静默成功：列表里已经没有了，界面却报「已删除」会让人以为
    自己删掉了别的东西。"""
    meta = workflow_lib.save("甲", _wf())
    workflow_lib.remove(meta["id"])
    with pytest.raises(ValueError):
        workflow_lib.remove(meta["id"])


# --------------------------------------------------------------------------- #
# 导入 / 导出
# --------------------------------------------------------------------------- #
def test_import_copies_the_file(tmp_path):
    """拷贝而不是引用：引用的话用户之后挪动/删除原文件，列表里就留下一条
    点不开的条目——而「打开失败」看起来像库里那条坏了。"""
    src = tmp_path / "外部流程.json"
    src.write_text(json.dumps(_wf("外部流程", [{"type": "delay", "params": {"ms": 1}}])),
                   encoding="utf-8")
    meta = workflow_lib.import_file(str(src))
    assert meta["name"] == "外部流程" and meta["node_count"] == 1

    src.unlink()                                     # 原文件没了
    assert workflow_lib.load(meta["id"])["nodes"], "库里那份跟着原文件一起没了"


def test_import_rejects_files_that_are_not_workflows(tmp_path):
    """选错文件必须当场报错：导进一条坏数据，列表里就多一个点开是空的条目。"""
    bad = tmp_path / "随便.json"
    bad.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    with pytest.raises(ValueError):
        workflow_lib.import_file(str(bad))
    junk = tmp_path / "坏.json"
    junk.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError):
        workflow_lib.import_file(str(junk))
    with pytest.raises(ValueError):
        workflow_lib.import_file(str(tmp_path / "不存在.json"))


def test_export_strips_library_metadata_and_round_trips(tmp_path):
    """导出文件必须与旧行为（`Workflow.save`）写出来的同构——否则导出再导入
    会带进一堆库里才认识的字段。"""
    meta = workflow_lib.save("导出的", _wf("导出的", [{"type": "delay", "params": {"ms": 1}}]))
    dst = tmp_path / "out.json"
    workflow_lib.export_file(meta["id"], str(dst))
    raw = json.loads(dst.read_text(encoding="utf-8"))
    assert "schema" not in raw and "id" not in raw and "created_at" not in raw
    assert raw["name"] == "导出的" and len(raw["nodes"]) == 1
    # 再导进来还是同一份东西（只是换了个 id）
    again = workflow_lib.import_file(str(dst))
    assert again["node_count"] == 1 and again["name"] == "导出的"


# --------------------------------------------------------------------------- #
# 控制器：新建 → 保存 闭环 / 未保存保护 / 库操作
# --------------------------------------------------------------------------- #
def _ctrl():
    return AppController()


def test_new_then_save_creates_a_library_entry():
    """**这条是这次要修的核心 bug**：旧行为下「新建 → 保存」必然失败
    （`workflow.new` 把 path 清成 None，`workflow.save` 再抛「尚未指定保存路径」），
    用户必须先「另存为」才能存下第一版。"""
    ctrl = _ctrl()
    ctrl.workflow_new()
    assert ctrl.path is None
    out = ctrl.workflow_save()
    assert out["lib_id"], out
    assert ctrl.path == workflow_lib.path_of(out["lib_id"])
    assert ctrl.workflow_dirty() is False
    assert len(ctrl.workflow_lib_list()["items"]) == 1


def test_save_twice_keeps_one_entry():
    ctrl = _ctrl()
    ctrl.workflow_new()
    first = ctrl.workflow_save()["lib_id"]
    ctrl.node_add("delay")
    assert ctrl.workflow_dirty() is True
    second = ctrl.workflow_save()["lib_id"]
    assert second == first
    assert len(ctrl.workflow_lib_list()["items"]) == 1
    assert ctrl.workflow_dirty() is False


def test_dirty_flag_follows_every_kind_of_edit():
    """判据钩在 `_broadcast_workflow` 上（16 个改动接口全经由它）。

    逐个接口记「我改过没有」的话，漏掉一个就会出现「明明改了、界面显示已保存」
    ——保存按钮不会亮，用户的改动静默丢失。
    """
    ctrl = _ctrl()
    ctrl.workflow_new()
    ctrl.workflow_save()
    assert ctrl.workflow_dirty() is False
    for label, mutate in [
        ("加节点", lambda: ctrl.node_add("delay")),
        ("改参数", lambda: ctrl.node_params_set(0, "ms", 42)),
        ("改名", lambda: ctrl.node_rename(0, "换个名")),
        ("停用", lambda: ctrl.node_toggle(0, False)),
        ("移动", lambda: ctrl.node_set_pos(ctrl.workflow.nodes[0].uid, 10, 10)),
        ("改工作流名", lambda: ctrl.workflow_update({"name": "新名字"})),
        ("加连线", lambda: ctrl.edge_add(ctrl.workflow.nodes[0].uid, "ghost", "out")),
        ("删节点", lambda: ctrl.node_remove(0)),
    ]:
        before = ctrl.workflow_dirty()
        try:
            mutate()
        except ControllerError:
            pass          # 有些改动（如连到不存在的节点）本就该被拒
        assert ctrl.workflow_dirty() is True, f"{label} 之后没标脏（之前是 {before}）"


def test_new_refuses_to_discard_unsaved_changes():
    """后端这一层是**保证**，前端确认框只是体验：前端漏了就会静默丢掉用户
    刚连好的一整张图，而那之后没有任何地方能找回来。"""
    ctrl = _ctrl()
    ctrl.workflow_new()
    ctrl.workflow_save()
    ctrl.node_add("delay")
    with pytest.raises(ControllerError) as ei:
        ctrl.workflow_new()
    assert ei.value.code == -32006 and ei.value.message == "unsaved_changes"
    # 确认过之后再调一次（force）就放行
    ctrl.workflow_new(force=True)
    assert ctrl.workflow_dirty() is False
    assert ctrl.path is None


def test_lib_open_refuses_to_discard_unsaved_changes():
    ctrl = _ctrl()
    ctrl.workflow_new()
    wid = ctrl.workflow_save()["lib_id"]
    ctrl.node_add("delay")
    with pytest.raises(ControllerError) as ei:
        ctrl.workflow_lib_open(wid)
    assert ei.value.code == -32006
    ctrl.workflow_lib_open(wid, force=True)
    assert ctrl.workflow_dirty() is False
    assert ctrl.path == workflow_lib.path_of(wid)


def test_lib_open_switches_the_current_workflow():
    ctrl = _ctrl()
    ctrl.workflow_new()
    ctrl.workflow_update({"name": "甲"})
    ctrl.node_add("delay")
    a = ctrl.workflow_save()["lib_id"]
    ctrl.workflow_new(force=True)
    ctrl.workflow_update({"name": "乙"})
    b = ctrl.workflow_save()["lib_id"]

    cur = ctrl.workflow_lib_open(a)
    assert cur["lib_id"] == a and cur["dirty"] is False
    assert cur["workflow"]["name"] == "甲"
    assert len(cur["workflow"]["nodes"]) == 1
    assert {m["id"] for m in ctrl.workflow_lib_list()["items"]} == {a, b}


def test_lib_open_rejects_bad_id_and_missing_entry():
    ctrl = _ctrl()
    with pytest.raises(ControllerError) as ei:
        ctrl.workflow_lib_open("../evil")
    assert ei.value.code == -32602
    with pytest.raises(ControllerError) as ei2:
        ctrl.workflow_lib_open("ghost-000000")
    assert ei2.value.code == -32005


def test_lib_rename_of_current_updates_the_title_without_dirtying():
    """改的正好是当前这份：标题栏要跟着变，但**不能**平白多出一个未保存标记
    ——磁盘上已经是最新的了。"""
    ctrl = _ctrl()
    ctrl.workflow_new()
    wid = ctrl.workflow_save()["lib_id"]
    out = ctrl.workflow_lib_rename(wid, "改过的名字")
    assert out["item"]["name"] == "改过的名字"
    assert ctrl.workflow.name == "改过的名字"
    assert ctrl.workflow_dirty() is False
    assert ctrl.workflow_current()["workflow"]["name"] == "改过的名字"


def test_lib_rename_of_another_entry_leaves_current_alone():
    ctrl = _ctrl()
    ctrl.workflow_new()
    a = ctrl.workflow_save()["lib_id"]
    ctrl.workflow_new(force=True)
    ctrl.workflow_update({"name": "当前这份"})
    ctrl.workflow_save()
    ctrl.workflow_lib_rename(a, "别人")
    assert ctrl.workflow.name == "当前这份"
    assert ctrl.workflow_dirty() is False


def test_lib_delete_of_current_keeps_the_tree_but_clears_the_path():
    """文件没了但用户连的那张图还在，他仍可以继续改、再「保存」回库。
    直接清空工作流的话，一次误点就白丢一张图。"""
    ctrl = _ctrl()
    ctrl.workflow_new()
    ctrl.node_add("delay")
    wid = ctrl.workflow_save()["lib_id"]
    out = ctrl.workflow_lib_delete(wid)
    assert out["deleted"] == wid and out["current"] == ""
    assert ctrl.path is None
    assert len(ctrl.workflow.nodes) == 1, "误点删除把画布上的图也清了"
    assert ctrl.workflow_lib_list()["items"] == []
    # 还能再存回库（自动新建一条）
    again = ctrl.workflow_save()
    assert again["lib_id"] and again["lib_id"] != wid


def test_lib_export_of_current_uses_the_canvas_state(tmp_path):
    """导出的必须是**画布上现在这样**，不是上次保存时那样；但也不该顺手写库。"""
    ctrl = _ctrl()
    ctrl.workflow_new()
    ctrl.node_add("delay")
    ctrl.workflow_save()
    ctrl.node_add("delay")                     # 保存之后又改的，没落盘
    assert ctrl.workflow_dirty() is True
    dst = tmp_path / "out.json"
    ctrl.workflow_lib_export(str(dst))
    assert len(json.loads(dst.read_text(encoding="utf-8"))["nodes"]) == 2
    # 库里的那份没被顺手改掉
    assert len(workflow_lib.load(ctrl.workflow_lib_list()["current"])["nodes"]) == 1
    assert ctrl.workflow_dirty() is True


def test_lib_export_before_any_save_is_refused():
    ctrl = _ctrl()
    ctrl.workflow_new()
    with pytest.raises(ControllerError) as ei:
        ctrl.workflow_lib_export("/tmp/whatever.json")
    assert ei.value.code == -32602


def test_save_to_an_external_path_does_not_land_in_the_library(tmp_path):
    """外部路径必须**写到指定的地方**，不能因为「文件名长得像 id」就拐进库里。

    `id_of_path` 若只看 basename 不看父目录，`/tmp/saved.json` 会被判成库条目
    `saved`——用户按「导出到桌面」，文件却静默落进了 `workflows/`，而界面报的是
    「已导出到 /Users/…/Desktop/saved.json」。反向验证时这条是从
    `test_workflow_load_reports_list_migration` 的 FileNotFoundError 里顺出来的。
    """
    ctrl = _ctrl()
    ctrl.node_add("delay")
    out = tmp_path / "saved.json"
    res = ctrl.workflow_save(str(out))
    assert out.exists(), "导出路径没写到指定的地方"
    assert res["lib_id"] == "" and res["path"] == str(out)
    assert ctrl.workflow_lib_list()["items"] == [], "外部路径被当成库条目了"
    # 外部路径写的是**普通工作流文件**，不带库的元信息
    raw = json.loads(out.read_text(encoding="utf-8"))
    assert "schema" not in raw and "id" not in raw and "created_at" not in raw
    assert len(raw["nodes"]) == 1
    # 保存之后 `workflow.current` 也要认得这个外部路径（定时运行按路径找它）
    assert ctrl.workflow_current()["path"] == str(out)


def test_lib_save_names_the_entry_and_the_workflow():
    """在库里给名字保存：库条目和画布标题必须是**同一个**名字，
    否则用户会看到两处写着不同的名字，不知道该信哪个。"""
    ctrl = _ctrl()
    ctrl.workflow_new()
    out = ctrl.workflow_lib_save("给它起个名")
    assert out["item"]["name"] == "给它起个名"
    assert ctrl.workflow.name == "给它起个名"
    assert out["workflow"]["name"] == "给它起个名"
    assert out["dirty"] is False


def test_lib_import_does_not_switch_the_current_workflow(tmp_path):
    """用户按的是「导入」，期望是「它出现在列表里」，不是「画布上正在编辑的
    东西被换掉了」。"""
    ctrl = _ctrl()
    ctrl.workflow_new()
    ctrl.workflow_update({"name": "正在编辑的"})
    ctrl.workflow_save()
    src = tmp_path / "别处来的.json"
    src.write_text(json.dumps(_wf("别处来的")), encoding="utf-8")
    out = ctrl.workflow_lib_import(str(src))
    assert out["item"]["name"] == "别处来的"
    assert ctrl.workflow.name == "正在编辑的"
    assert len(out["items"]) == 2
    with pytest.raises(ControllerError) as ei:
        ctrl.workflow_lib_import(str(tmp_path / "不存在.json"))
    assert ei.value.code == -32005


def test_current_reports_lib_id_and_dir():
    ctrl = _ctrl()
    cur = ctrl.workflow_current()
    assert cur["lib_id"] == "" and cur["dirty"] is False
    assert cur["lib_dir"] == workflow_lib.workflows_dir()
    ctrl.workflow_new()
    wid = ctrl.workflow_save()["lib_id"]
    assert ctrl.workflow_current()["lib_id"] == wid


def test_broadcast_carries_the_dirty_flag():
    """用户改完一个参数后标题栏的未保存标记要亮起来——改动是经
    `workflow.changed` 广播过去的，dirty 必须跟着那一帧走。"""
    ctrl = _ctrl()
    seen = []
    ctrl.set_notifier(lambda m, p: seen.append((m, p)) if m == "workflow.changed" else None)
    ctrl.node_add("delay")
    assert seen[-1][1]["dirty"] is True
    ctrl.workflow_save()
    ctrl.node_add("delay")
    assert seen[-1][1]["dirty"] is True
    ctrl.workflow_save()
    ctrl.workflow_update({"name": "x"})        # 又改了一次
    assert seen[-1][1]["dirty"] is True
