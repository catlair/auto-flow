"""节点插件基类与注册表。

参照 LCA 模式：每个节点类自描述参数定义（get_definition），UI 通用渲染，
新增节点无需改 UI 代码。
"""
from __future__ import annotations

from typing import Any, Optional

_REGISTRY: dict[str, "BaseTask"] = {}


class ParamDef:
    def __init__(self, key: str, label: str, ptype: str = "text",
                 default: Any = None, options: Optional[list] = None,
                 tooltip: str = "", min_value: float = None, max_value: float = None) -> None:
        self.key = key
        self.label = label
        self.ptype = ptype          # text / int / float / bool / select / events / point
        self.default = default
        self.options = options or []
        self.tooltip = tooltip
        self.min_value = min_value
        self.max_value = max_value

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "ptype": self.ptype,
                "default": self.default, "options": self.options,
                "tooltip": self.tooltip, "min": self.min_value, "max": self.max_value}


class BaseTask:
    """节点基类：子类设置 type/name/params，实现 run(ctx)。"""
    type: str = ""
    name: str = ""

    def __init__(self) -> None:
        self.params: list[ParamDef] = []

    def definition(self) -> dict:
        return {"type": self.type, "name": self.name,
                "params": [p.to_dict() for p in self.params]}

    def defaults(self) -> dict:
        return {p.key: p.default for p in self.params}

    def run(self, ctx) -> None:
        raise NotImplementedError


def register(task: "BaseTask") -> "BaseTask":
    _REGISTRY[task.type] = task
    return task


def get_task(type_name: str) -> Optional["BaseTask"]:
    return _REGISTRY.get(type_name)


def all_definitions() -> list[dict]:
    return [t.definition() for t in _REGISTRY.values()]
