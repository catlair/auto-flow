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
                 tooltip: str = "", min_value: float = None, max_value: float = None,
                 show_if: Optional[dict] = None, pick: bool = False) -> None:
        self.key = key
        self.label = label
        self.ptype = ptype          # text / int / float / bool / select / events / point / keys / file
        self.default = default
        self.options = options or []
        self.tooltip = tooltip
        self.min_value = min_value
        self.max_value = max_value
        # 条件显示：`{"key": "case_count", "gte": 2}` —— 仅当另一个参数满足条件时才渲染。
        # 多路分支节点有 6 路 × 2 个字段，不平铺的话参数面板会长得没法看。
        # 只支持 gte / lte / eq 三种比较，够用且不用在前端塞一个表达式求值器。
        self.show_if = show_if
        # text 类型是否额外给一个「选择文件」按钮。多路分支的取值既可能是
        # 模板图路径、也可能是要找的文字，不能固定成 file 类型（那个输入框是
        # 只读的，文字没法填），所以给可编辑输入框补一个挑选按钮。
        self.pick = pick

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "ptype": self.ptype,
                "default": self.default, "options": self.options,
                "tooltip": self.tooltip, "min": self.min_value, "max": self.max_value,
                "show_if": self.show_if, "pick": self.pick}


class BaseTask:
    """节点基类：子类设置 type/name/order/params，实现 run(ctx)。

    `order` 决定「添加节点」菜单里的排列次序（§9.4）：值小的靠前，同值再按
    注册先后。**新增节点必须显式给 order**；不给就用默认 100 落到菜单末尾。

    内置节点按 10 递增留出空档，插新节点时不必重排既有值。新增内置节点时
    记得把它补进 `tests/test_core.py::BUILTIN_MENU_ORDER`——那里只覆盖已知的
    9 个内置节点，漏加不会报错。
    """
    type: str = ""
    name: str = ""
    order: int = 100

    def __init__(self) -> None:
        self.params: list[ParamDef] = []

    def definition(self) -> dict:
        return {"type": self.type, "name": self.name, "order": self.order,
                "params": [p.to_dict() for p in self.params]}

    def defaults(self) -> dict:
        return {p.key: p.default for p in self.params}

    def run(self, ctx) -> None:
        raise NotImplementedError


def register(task: "BaseTask | type[BaseTask]") -> "BaseTask | type[BaseTask]":
    """注册节点，接受**类**（`@register` 装饰器用法）或**实例**（显式注册）。

    装饰器拿到的永远是类对象，所以这里统一实例化后再入表：`_REGISTRY` 必须
    只存实例，否则 `all_definitions()` 会拿到未绑定的 `definition` 方法
    （`TypeError: missing 1 required positional argument: 'self'`），
    `get_task()` 也会返回类而非可 `run()` 的对象。

    返回值原样透传，这样 `@register` 不会把类名替换成实例、破坏类本身的引用。
    """
    inst = task() if isinstance(task, type) else task
    _REGISTRY[inst.type] = inst
    return task


def get_task(type_name: str) -> Optional["BaseTask"]:
    return _REGISTRY.get(type_name)


def all_definitions() -> list[dict]:
    """按 `order` 升序返回全部节点定义。

    菜单顺序的唯一真源在这里：`sorted` 是稳定排序，同 `order` 保持注册顺序，
    所以结果与 `_REGISTRY` 的插入顺序无关。调用方（RPC `nodes.definitions`、
    旧 Qt 菜单）**不要**再各自维护一份顺序表——此前 `rpc/controller.py`
    就有一份重复的 `_NODE_ORDER`，掩盖了注册顺序本身就是错的这一事实。
    """
    return [t.definition() for t in sorted(_REGISTRY.values(), key=lambda t: t.order)]
