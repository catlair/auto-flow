"""应用数据目录：开发时=项目目录；打包后=~/Library/Application Support/AutoFlow。

可用环境变量 AUTOFLOW_DATA_DIR 覆盖（测试隔离、多实例并行、把数据放到自定义盘）。
"""
from __future__ import annotations

import os
import sys

_ENV_DATA_DIR = "AUTOFLOW_DATA_DIR"


def app_dir() -> str:
    override = os.environ.get(_ENV_DATA_DIR)
    if override:
        d = os.path.abspath(override)
    elif getattr(sys, "frozen", False):
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
_SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def models_dir() -> str:
    """模型目录；打包后首次调用时把随包模型播种到用户目录。"""
    d = os.path.join(app_dir(), "models")
    os.makedirs(d, exist_ok=True)
    target = os.path.join(d, _BUNDLED_MODEL)
    if not os.path.exists(target):
        # 依次尝试：打包资源、源码树（数据目录被 AUTOFLOW_DATA_DIR 覆盖时仍可用）、旧版回退
        for base in (getattr(sys, "_MEIPASS", ""), _SRC_ROOT, os.path.dirname(app_dir())):
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
