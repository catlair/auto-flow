#!/usr/bin/env python3
"""实时看 Auto Flow 在干什么（把日志翻成人话）。

Auto Flow 把每一次节点执行都写进 ~/Library/Logs/autoflow-tauri.log，但只有
`run.node {index, type}`——看不出节点叫什么、卡在哪。这个脚本把它翻成：

    18:08:16  ▶ 开始运行  yw-keytest-r（键盘测试·R 键）
    18:08:16  #1  开始
    18:08:21  #2  按 vk:15（虚拟键码 R）          (+5.0s)
    18:08:29  #3  按字符 R                       (+8.0s)
    18:08:29  ■ 跑完（共 3 步）

**会自动跟着你打开的那份工作流**（靠日志里的 workflow.lib.open 判断），
所以你换一份流程跑，它自己就换名字对照表，不用重开。

用法：
    .venv/bin/python scripts/watch_run.py            # 跟着跑（Ctrl-C 退出）
    .venv/bin/python scripts/watch_run.py --replay   # 只看已经发生的
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

LOG = os.path.expanduser("~/Library/Logs/autoflow-tauri.log")
LIB = os.path.expanduser("~/Library/Application Support/AutoFlow/workflows")

_TS = re.compile(r"^\[(\d+)\]\s*(.*)$")
_INDEX = re.compile(r'"index":(-?\d+)')
_OPEN_ID = re.compile(r'"method":"workflow\.lib\.open","params":\{[^}]*"id":"([^"]+)"')


def load_by_id(wid: str) -> dict | None:
    path = os.path.join(LIB, wid + ".json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["_id"] = wid
        return data
    except Exception:
        return None


def describe(node: dict) -> str:
    t = node.get("type", "?")
    name = (node.get("name") or "").strip()
    p = node.get("params") or {}
    extra = ""
    if t == "branch":
        k = int(p.get("case_count", 2) or 2)
        vals = [str(p.get("case%d_value" % i, "")) for i in range(1, k + 1)]
        extra = "  判据: " + " / ".join(v for v in vals if v)
    elif t == "ocr_click":
        extra = "  文字「%s」" % p.get("text", "")
    elif t == "image_click":
        extra = "  模板 %s" % os.path.basename(str(p.get("image_path", "")))
    elif t == "delay":
        extra = "  %ss" % round(float(p.get("ms", 0) or 0) / 1000, 1)
    elif t == "keyboard":
        extra = "  keys=%s" % p.get("keys", "")
    return (name or t) + extra


def handle(line: str, state: dict) -> None:
    m = _TS.match(line)
    if not m:
        return
    ts, body = int(m.group(1)), m.group(2)
    stamp = time.strftime("%H:%M:%S", time.localtime(ts))

    oid = _OPEN_ID.search(body)
    if oid:
        wf = load_by_id(oid.group(1))
        if wf is not None:
            state["wf"] = wf
            print("%s  ⇄ 打开工作流 %s（%s）" % (stamp, oid.group(1), wf.get("name", "")), flush=True)
        return

    if '"method":"run.start"' in body:
        state["steps"] = 0
        state["last"] = ts
        wf = state.get("wf") or {}
        print("%s  ▶ 开始运行  %s" % (stamp, wf.get("name") or "(未知工作流)"), flush=True)
        return

    if '"method":"run.node"' in body:
        idx = int(_INDEX.search(body).group(1))
        nodes = (state.get("wf") or {}).get("nodes") or []
        node = nodes[idx] if 0 <= idx < len(nodes) else None
        label = describe(node) if node else "(index=%d，对照表里没有)" % idx
        last = state.get("last")
        delta = "" if last is None else "  (+%.1fs)" % (ts - last)
        state["last"] = ts
        state["steps"] = state.get("steps", 0) + 1
        print("%s  #%-3d %s%s" % (stamp, state["steps"], label, delta), flush=True)
        return

    if '"method":"run.finished"' in body:
        stopped = '"stopped":true' in body
        print("%s  ■ %s（共 %d 步）" % (stamp, "手动停止" if stopped else "跑完",
                                        state.get("steps", 0)), flush=True)
        state["steps"] = 0
        return

    if '"method":"run.error"' in body:
        print("%s  ✖ 出错: %s" % (stamp, body[-240:]), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", action="store_true", help="只回放已有日志")
    args = ap.parse_args()
    if not os.path.exists(LOG):
        sys.exit("找不到日志：" + LOG)
    state: dict = {}
    with open(LOG, "r", encoding="utf-8", errors="replace") as f:
        if not args.replay:
            f.seek(0, os.SEEK_END)
            print("盯着 %s ……（先在 Auto Flow 里打开工作流，再按 F10；Ctrl-C 退出）" % LOG)
        while True:
            line = f.readline()
            if not line:
                if args.replay:
                    break
                time.sleep(0.4)
                continue
            handle(line.strip(), state)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n退出。")
