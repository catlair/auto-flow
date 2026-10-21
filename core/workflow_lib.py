"""工作流库：把流程图落盘成一条条可管理的资产，供「列表 / 打开 / 更名 / 删除 / 运行」使用。

**为什么需要它**：此前的「新建 / 打开 / 保存 / 另存为」是四个直通系统文件对话框的按钮，
流程文件散落在用户随手挑的位置。于是：

- 「我上周那个流程存哪了」只能回 Finder 里翻；
- **「新建 → 保存」必然失败**：`workflow.new` 把路径清成 `None`，`workflow.save`
  不传 path 就报「尚未指定保存路径」——用户必须先「另存为」才能存下第一版；
- 改个名要回 Finder 里改文件名（还可能顺手改坏扩展名），删一个同理。

存进库里之后一条一个文件、列表里直接管，路径不再需要用户操心。文件对话框降级成
「导入 / 导出」——只用来跟外部交换，不再是日常操作。

**为什么一条一个文件**（而不是一个总 JSON）：与录制库同理——改个名、删一条都要整体
重写；而且一个字节损坏就整个库读不出来。一条一个文件，坏一条只影响那一条
（`list_meta` 会跳过读不出来的，不让整个列表打不开）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from typing import Any, Optional

from . import paths

# id 直接参与拼路径，必须白名单：`../` 能写到任意位置（甚至覆盖录制库里的文件）。
# 生成端只产生 [0-9a-f] 与 `-`，这里再校验一次是为了挡住**手工改过**的文件名或将来
# 换了生成方式——校验放在读路径上，比信任调用方可靠。
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_SCHEMA = 1

# 废纸篓根目录。默认 `~/.Trash`；`AUTOFLOW_TRASH_DIR` 可覆盖——
# **测试必须覆盖它**，否则跑一次测试就往用户真实废纸篓里塞一堆文件。
_ENV_TRASH_DIR = "AUTOFLOW_TRASH_DIR"

# 库里那条 JSON 里由**库自己**维护的键，不属于 `Workflow` 的数据模型。
# 导出时会被剥掉，让导出文件与「旧的 workflow.save 写出来的文件」逐字节同构。
_META_KEYS = ("schema", "id", "created_at")


def workflows_dir() -> str:
    return paths.workflows_dir()


def trash_dir() -> str:
    return os.path.abspath(os.environ.get(_ENV_TRASH_DIR)
                           or os.path.expanduser("~/.Trash"))


def new_id() -> str:
    """时间前缀 + 随机后缀：既能按名字排序，又不会同秒冲突。"""
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def default_name() -> str:
    """自动名：新建后第一次保存时用的名字，用户可在库里改名。"""
    return "工作流 " + time.strftime("%Y-%m-%d %H:%M:%S")


def path_of(wid: str) -> str:
    """id -> 绝对路径。**唯一的 id 校验点**，所有读写都从它过。"""
    if not isinstance(wid, str) or not _ID_RE.match(wid):
        raise ValueError(f"工作流 id 非法：{wid!r}")
    return os.path.join(workflows_dir(), f"{wid}.json")


def id_of_path(path: Optional[str]) -> str:
    """路径 -> 库里的 id；不在库里（含 None / 空串 / 外部路径）返回空串。

    **「当前这份在不在库里」的唯一判据**，不另外记一份 `_lib_id` 状态：
    两份状态总有一天会对不上，而对不上的表现是「保存写进了 A 文件、界面显示
    已保存到 B」——用户的改动看着存了、实际进了另一个文件。

    必须是 `workflows_dir()` 的**直接**子文件：`workflows/templates/` 也在库目录
    下面，把它当库条目会让「另存为到模板」变成覆盖某条工作流。
    """
    if not path or not isinstance(path, str):
        return ""
    try:
        base = os.path.abspath(workflows_dir())
        p = os.path.abspath(path)
    except (OSError, ValueError):
        return ""
    if os.path.dirname(p) != base:
        return ""
    fn = os.path.basename(p)
    if not fn.endswith(".json"):
        return ""
    wid = fn[:-5]
    return wid if _ID_RE.match(wid) else ""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _mtime_iso(path: str) -> str:
    t = _mtime(path)
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""


def meta_of(data: Any, wid: str = "", updated_at: str = "") -> dict:
    """只取列表要用的字段。**不含 nodes / edges**——列表页不该为了显示节点数
    去搬一整棵树（节点参数里可能内嵌录制事件，单条可达 MB）。
    """
    if not isinstance(data, dict):
        data = {}
    # 必须判 list：`len("not-a-list")` 也是 10，会凭空造出一个「10 个节点」的
    # 条目，而列表上根本看不出它是假的（点进去才发现是空的）。
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        # 兼容「裸事件脚本」（没有 nodes，只有一个 events 数组）——它加载后会
        # 变成一个录制回放节点，列表上也该显示成 1 个节点而不是 0。
        legacy = data.get("events")
        nodes = legacy if isinstance(legacy, list) else []
        n_nodes = 1 if nodes else 0
    else:
        n_nodes = len(nodes)
    edges = data.get("edges")
    if not isinstance(edges, list):
        edges = []
    return {
        # id 以**文件名**为准：JSON 里的 `id` 只是副本，用户手工编辑/复制文件
        # 都会让它和文件名对不上，而对不上的表现是「点开列表项打开了别的流程」。
        "id": wid or (data.get("id") if isinstance(data.get("id"), str) else ""),
        "name": str(data.get("name") or "").strip() or "(未命名)",
        "created_at": str(data.get("created_at") or ""),
        "updated_at": updated_at,
        "node_count": n_nodes,
        "edge_count": len(edges),
    }


def save(name: str, data: Any, wid: Optional[str] = None) -> dict:
    """写一份工作流。`wid` 给了就覆盖（「保存」），否则新建（「存为新的一份」）。

    先写临时文件再 `os.replace` 原子替换：直接覆盖写，写到一半崩掉会留下半截
    JSON——那一份流程就**永久损坏**了（几十个节点的图没法重画一遍）。
    """
    wid = wid or new_id()
    path = path_of(wid)
    payload = dict(data) if isinstance(data, dict) else {}
    payload["schema"] = _SCHEMA
    payload["id"] = wid
    payload["name"] = (str(name or "").strip()
                       or str(payload.get("name") or "").strip()
                       or default_name())
    payload["created_at"] = _now_iso()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                old = json.load(f)
            # 覆盖写入不该把创建时间改掉：库里按时间排，改名/更新后跳到最前会很怪
            payload["created_at"] = old.get("created_at") or payload["created_at"]
        except Exception:  # noqa: BLE001
            pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return meta_of(payload, wid, _mtime_iso(path))


def _has_node_list(data: Any) -> bool:
    """`nodes`（v4）或 `events`（裸事件脚本）至少有一个是 list。

    **不能写成 `data.get("nodes") or data.get("events")`**：空列表是 falsy，
    而 `nodes: []` 正是「新建后还没加节点」的正常状态——写成 or 链会把**每一份
    空工作流**判成格式损坏，症状是新建的条目在库里改不了名、也打不开。
    """
    if not isinstance(data, dict):
        return False
    return any(isinstance(data.get(k), list) for k in ("nodes", "events"))


def load(wid: str) -> dict:
    """读整份（含 nodes）。文件不存在/损坏都抛 ValueError，调用方负责转 RPC 错误。"""
    path = path_of(wid)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"工作流不存在：{wid}") from None
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"工作流读不出来（{e}）") from e
    if not _has_node_list(data):
        raise ValueError(f"工作流格式不对：{wid}")
    data.setdefault("id", wid)
    return data


def list_meta() -> list:
    """按**最近修改**倒序（刚动过的在前）。坏文件静默跳过，不让一条坏数据毁掉整个列表。

    用文件 mtime 而不是 JSON 里的时间戳：用户完全可能用 Finder / 编辑器直接改
    库里的文件，那时 JSON 里的时间戳是陈旧的，而列表会显示一个骗人的时间。
    """
    d = workflows_dir()
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return []
    for fn in names:
        if not fn.endswith(".json"):
            continue          # 含 `.json.tmp` 残留
        wid = fn[:-5]
        if not _ID_RE.match(wid):
            continue
        p = os.path.join(d, fn)
        try:
            with open(p, "r", encoding="utf-8") as f:
                out.append(meta_of(json.load(f), wid, _mtime_iso(p)))
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda m: (m.get("updated_at") or "", m.get("id") or ""), reverse=True)
    return out


def rename(wid: str, name: str) -> dict:
    """只改名字。**保持 mtime 不变**——改名不是修改内容，不该让它在列表里跳到最前。"""
    path = path_of(wid)
    data = load(wid)
    keep = _mtime(path)
    data["name"] = (str(name or "").strip()
                    or str(data.get("name") or "").strip()
                    or default_name())
    meta = save(data["name"], data, wid=wid)
    if keep:
        try:
            os.utime(path, (keep, keep))
        except OSError:
            pass
        meta["updated_at"] = _mtime_iso(path)
    return meta


def _trash_dest(name: str) -> str:
    d = trash_dir()
    os.makedirs(d, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(d, f"{name}.{stamp}")
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(d, f"{name}.{stamp}.{n}")
        n += 1
    return dest


def remove(wid: str) -> str:
    """删除一份工作流，**移进废纸篓**而不是直接删，返回废纸篓里的路径。

    这是**用户数据**，不是可再生的构建产物。项目里 `clean_old_builds.sh` 在跨卷
    `mv` 失败时会退化成 `rm -rf`（那些是构建残留，可再生）；这里**明确不允许**
    那个退化——`shutil.move` 跨卷会自己退化成「拷贝 + 删除」，真失败了就抛错，
    让用户知道文件还在哪儿，绝不静默删掉别人辛苦连出来的一张图。
    """
    path = path_of(wid)
    if not os.path.exists(path):
        raise ValueError(f"工作流不存在：{wid}")
    dest = _trash_dest(os.path.basename(path))
    try:
        shutil.move(path, dest)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"移入废纸篓失败（{e}），文件仍在 {path}") from e
    return dest


def import_file(src: str, name: str = "") -> dict:
    """把一个外部 JSON 工作流文件**拷进库**。

    拷贝而不是引用：库里那份从此归库管。引用的话用户之后挪动/删除原文件，
    列表里就会留下一条点不开的条目——而「打开失败」看起来像库里那条坏了。
    """
    data = _read_workflow_file(src)
    return save(name or str(data.get("name") or "").strip()
                or os.path.splitext(os.path.basename(src))[0], data)


def write_plain(data: Any, dst: str) -> str:
    """把一份工作流写成**普通工作流文件**（不含库的元信息）。

    剥掉 `schema` / `id` / `created_at`：导出文件必须与**旧行为**（`Workflow.save`）
    写出来的文件同构，否则「导出再导入」会带进一堆库里才认识的字段，而这个文件
    也可能被别处（定时任务、手工编辑）当普通流程文件打开。
    """
    out = {k: v for k, v in (data or {}).items() if k not in _META_KEYS}
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return dst


def export_file(wid: str, dst: str) -> str:
    """把库里的一份写成普通工作流文件。"""
    return write_plain(load(wid), dst)


def _read_workflow_file(src: str) -> dict:
    """读一个外部工作流文件并做最低限度校验（长得不像流程就直接拒）。"""
    if not isinstance(src, str) or not src:
        raise ValueError("未指定文件")
    try:
        with open(src, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"文件不存在：{src}") from None
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"不是合法的工作流文件（{e}）") from e
    if not _has_node_list(data):
        raise ValueError("不是合法的工作流文件（缺 nodes / events 数组）")
    return data
