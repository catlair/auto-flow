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


_BUNDLED_MODEL = "yolo11n.onnx"


def models_dir() -> str:
    """模型目录；打包后首次调用时把随包模型播种到用户目录。"""
    d = os.path.join(app_dir(), "models")
    os.makedirs(d, exist_ok=True)
    target = os.path.join(d, _BUNDLED_MODEL)
    if not os.path.exists(target):
        for base in (getattr(sys, "_MEIPASS", ""), os.path.dirname(app_dir())):
            src = os.path.join(base, "models", _BUNDLED_MODEL) if base else ""
            if src and os.path.exists(src):
                try:
                    import shutil
                    shutil.copy(src, target)
                except Exception:
                    pass
                break
    return d


def default_model() -> str:
    """默认 YOLO 模型路径；找不到返回空串。"""
    p = os.path.join(models_dir(), _BUNDLED_MODEL)
    return p if os.path.exists(p) else ""
