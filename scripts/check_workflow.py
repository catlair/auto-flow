#!/usr/bin/env python3
"""校验工作流文件：端口接没接、边有没有悬空、节点是否可达。

为什么要单独有这个脚本：改工作流时最容易犯的错是**改节点 uid 忘了同步边**
（2026-10-21 就干过一次：把 press_esc 改名 click_exit 只改了边的 src，
另一条边还指着旧 uid，于是「观战中」那一路直接断掉）。这种错误在界面上
不一定看得出来——画布会把悬空的边画成一条断线，但跑起来就是「走一步就停」。

用法：
    .venv/bin/python scripts/check_workflow.py workflows/xxx.json [more.json ...]
    .venv/bin/python scripts/check_workflow.py --lib      # 检查工作流库里的全部

退出码 0 = 全部通过；1 = 有问题（逐条列出）。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.events import PORT_ELSE, PORT_OUT, Workflow, case_port  # noqa: E402


def check(path: str) -> list[str]:
    errs: list[str] = []
    wf = Workflow.load(path)
    by = {n.uid: n for n in wf.nodes}
    if wf.start not in by:
        errs.append("入口 %r 不存在" % wf.start)

    nxt: dict[tuple[str, str], str] = {}
    for ed in wf.edges:
        if ed.src not in by:
            errs.append("边起点不存在：%s" % ed.src)
            continue
        if ed.dst not in by:
            errs.append("边终点不存在：%s → %s（改 uid 忘了同步边？）" % (ed.src, ed.dst))
            continue
        key = (ed.src, ed.port)
        if key in nxt:
            errs.append("同一出口接了两条边：%s/%s" % (ed.src, ed.port))
        nxt[key] = ed.dst

    def ports(nd):
        if nd.type == "branch":
            n = int(nd.params.get("case_count", 0) or 0)
            return [case_port(i) for i in range(1, n + 1)] + [PORT_ELSE]
        if nd.type == "end":
            return []
        return [PORT_OUT]

    for nd in wf.nodes:
        if nd.type == "note":
            continue
        for p in ports(nd):
            if (nd.uid, p) not in nxt:
                errs.append("出口没接线：%s（%s）的 %s" % (nd.name, nd.uid, p))
        if nd.type == "branch":
            n = int(nd.params.get("case_count", 0) or 0)
            for i in range(1, n + 1):
                if not str(nd.params.get("case%d_value" % i, "")).strip():
                    errs.append("分支 %s 的第 %d 路没填判据" % (nd.name, i))

    seen: set[str] = set()
    stack = [wf.start]
    while stack:
        cur = stack.pop()
        if cur in seen or cur not in by:
            continue
        seen.add(cur)
        for p in ports(by[cur]):
            d = nxt.get((cur, p))
            if d:
                stack.append(d)
    for u in by:
        if u not in seen and by[u].type != "note":   # note 是画布注释，本来就不接线
            errs.append("节点不可达：%s（%s）" % (by[u].name, u))
    return errs


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    if argv[0] == "--lib":
        base = os.path.expanduser("~/Library/Application Support/AutoFlow/workflows")
        paths = [os.path.join(base, f) for f in sorted(os.listdir(base)) if f.endswith(".json")]
    else:
        paths = argv
    bad = 0
    for p in paths:
        try:
            errs = check(p)
        except Exception as e:  # noqa: BLE001
            print("✗ %s：读不了（%s）" % (os.path.basename(p), e))
            bad += 1
            continue
        if errs:
            bad += 1
            print("✗ %s" % os.path.basename(p))
            for e in errs:
                print("    - " + e)
        else:
            print("✓ %s" % os.path.basename(p))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
