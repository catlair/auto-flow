"""应用数据目录：开发时=项目目录；打包后=~/Library/Application Support/AutoFlow。"""
from __future__ import annotations

import os
import sys


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        d = os.path.expanduser("~/Library/Application Support/AutoFlow")
    else:
        d = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(d, exist_ok=True)
    return d


def workflows_dir() -> str:
    d = os.path.join(app_dir(), "workflows")
    os.makedirs(d, exist_ok=True)
    return d


def templates_dir() -> str:
    d = os.path.join(workflows_dir(), "templates")
    os.makedirs(d, exist_ok=True)
    return d
