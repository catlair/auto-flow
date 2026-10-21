"""录制库：把每次录制落盘成可复用的资产，供流程图里的节点按 id 引用。

**为什么需要它**：录制原先只活在内存里（`controller._last_record`），
「写入节点」把事件序列**内嵌**进节点。于是同一段操作要用在两个地方就得录两遍，
改了一处另一处不同步——而「先在 A 场景录好，再到 B、C 两个流程里复用」正是
最常见的用法。存进库里之后节点只记一个 id，回放时现取：改一次，所有引用它的
节点一起变。

**为什么一条一个文件**（而不是一个总 JSON）：录制动辄几百 KB，总文件方式下
改个名、删一条都要整体重写；而且一个字节损坏就整个库读不出来。一条一个文件，
坏一条只影响那一条（`list_meta` 会跳过读不出来的，不让整个面板打不开）。
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, Optional

from . import paths

# id 直接参与拼路径，必须白名单：`../` 能写到任意位置（甚至覆盖 workflows/ 下的文件）。
# 用 uuid4().hex 生成，天然只含 [0-9a-f]；这里再校验一次是为了挡住**手工改过**的文件名
# 或将来换了生成方式——校验放在读路径上，比信任调用方可靠。
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_SCHEMA = 1


def recordings_dir() -> str:
    return paths.recordings_dir()


def new_id() -> str:
    """时间前缀 + 随机后缀：既能按名字排序，又不会同秒冲突。"""
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _path(rid: str) -> str:
    if not isinstance(rid, str) or not _ID_RE.match(rid):
        raise ValueError(f"录制 id 非法：{rid!r}")
    return os.path.join(recordings_dir(), f"{rid}.json")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def default_name() -> str:
    """自动名：录制完立刻入库时的名字，用户可在库里改名。"""
    return "录制 " + _now_iso()


def meta_of(data: dict) -> dict:
    """只取列表要用的字段。**不含 events**——列表页不该为了显示条数去读 MB 级数据。"""
    # 必须判 list：`len("not-a-list")` 也是 10，会凭空造出一个「10 条事件」的条目，
    # 而面板上根本看不出它是假的（点进去才发现回放什么都没发生）。
    events = data.get("events")
    if not isinstance(events, list):
        events = []
    dur = 0.0
    if events:
        try:
            dur = float(events[-1].get("ts_ms") or 0)
        except (AttributeError, TypeError, ValueError):
            dur = 0.0
    origin = data.get("origin") or [0, 0]
    try:
        origin = [int(origin[0]), int(origin[1])]
    except (IndexError, TypeError, ValueError):
        origin = [0, 0]
    return {
        "id": data.get("id", ""),
        "name": data.get("name") or "(未命名)",
        "created_at": data.get("created_at") or "",
        "count": len(events),
        "duration_ms": dur,
        "origin": origin,
    }


def save(name: str, events: list, origin: Any = (0, 0),
         rid: Optional[str] = None) -> dict:
    """写一条录制。`rid` 给了就覆盖（「更新库里的这条」），否则新建。

    先写临时文件再 `os.replace` 原子替换：直接覆盖写，写到一半崩掉会留下
    半截 JSON——那一条录制就永久损坏了（用户的原始操作没法重来）。
    """
    rid = rid or new_id()
    path = _path(rid)
    origin_v = [int(origin[0]), int(origin[1])] if origin else [0, 0]
    payload = {
        "schema": _SCHEMA,
        "id": rid,
        "name": str(name or "").strip() or default_name(),
        "created_at": _now_iso(),
        # 覆盖写入不该把创建时间改掉：库里按时间排序，改名/更新后跳到最后会很怪
        "events": list(events or []),
        "origin": origin_v,
    }
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload["created_at"] = json.load(f).get("created_at") or payload["created_at"]
        except Exception:  # noqa: BLE001
            pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)
    return meta_of(payload)


def load(rid: str) -> dict:
    """读整条（含 events）。文件不存在/损坏都抛 ValueError，调用方负责转成 RPC 错误。"""
    path = _path(rid)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"录制不存在：{rid}") from None
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"录制读不出来（{e}）") from e
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError(f"录制格式不对：{rid}")
    data.setdefault("id", rid)
    return data


def list_meta() -> list:
    """按创建时间**倒序**（新的在前）。损坏的文件静默跳过，不让一条坏数据毁掉整个列表。"""
    d = recordings_dir()
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return []
    for fn in names:
        if not fn.endswith(".json") or fn.endswith(".tmp.json"):
            continue
        rid = fn[:-5]
        if not _ID_RE.match(rid):
            continue
        try:
            with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                out.append(meta_of(json.load(f)))
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda m: (m.get("created_at") or "", m.get("id") or ""), reverse=True)
    return out


def rename(rid: str, name: str) -> dict:
    data = load(rid)
    data["name"] = str(name or "").strip() or data.get("name") or default_name()
    return save(data["name"], data["events"], data.get("origin"), rid=rid)


def remove(rid: str) -> None:
    path = _path(rid)
    try:
        os.remove(path)
    except FileNotFoundError:
        raise ValueError(f"录制不存在：{rid}") from None
