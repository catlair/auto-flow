"""工作流执行器：按节点顺序运行，支持整体循环、单节点重复、随时停止。"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.player import Player
from core.events import Workflow, Node


@dataclass
class RunContext:
    params: dict
    player: Player
    speed: float = 1.0
    base_x: int = 0
    base_y: int = 0
    stopping: bool = False
    on_progress: Optional[Callable[[int, int], None]] = None
    stop_workflow: Callable[[], None] = lambda: None
    set_condition: Callable[[bool], None] = lambda v: None

    def offset_for(self, use_relative: bool, x: int, y: int) -> tuple:
        if use_relative:
            return (self.base_x + x, self.base_y + y)
        return (x, y)


class Executor:
    """工作流级执行器。回调（UI 更新）由调用方保证线程安全。"""

    def __init__(self) -> None:
        self.player = Player()
        self._stop = threading.Event()
        self.running = False
        self._condition = False

    def stop_run(self) -> None:
        self._stop.set()
        self.player.stop_playback()

    def set_condition(self, value: bool) -> None:
        """供条件节点写入最近一次判断结果。"""
        self._condition = value

    def reset(self) -> None:
        self._stop.clear()
        self.player._stop.clear()

    def run_workflow(self, wf: Workflow, base_x: int = 0, base_y: int = 0,
                     on_node: Optional[Callable[[int, str], None]] = None,
                     on_progress: Optional[Callable[[int, int], None]] = None,
                     on_done: Optional[Callable[[bool], None]] = None) -> None:
        """在工作流线程执行；结束/被停止后调用 on_done(stopped)。"""
        self.reset()
        self.running = True
        self._condition = False
        try:
            repeat = max(wf.repeat, 1)
            for r in range(repeat):
                if self._stop.is_set():
                    break
                for idx, node in enumerate(wf.nodes):
                    if self._stop.is_set():
                        break
                    if not node.enabled or node.type in ("note",):
                        continue
                    run_when = (node.params or {}).get("run_when", "总是")
                    if run_when == "条件成立" and not self._condition:
                        continue
                    if run_when == "条件不成立" and self._condition:
                        continue
                    if on_node:
                        on_node(idx, node.type)
                    self._run_node(node, wf.speed, base_x, base_y, on_progress)
        finally:
            self.running = False
            if on_done:
                on_done(self._stop.is_set())

    def _run_node(self, node: Node, wf_speed: float, base_x: int, base_y: int,
                  on_progress: Optional[Callable]) -> None:
        import tasks.builtin  # noqa: F401  确保节点已注册
        from tasks.base import get_task
        task = get_task(node.type)
        if task is None:
            return
        ctx = RunContext(
            params={**task.defaults(), **(node.params or {})},
            player=self.player,
            speed=max(wf_speed, 0.01),
            base_x=base_x, base_y=base_y,
            stopping=self._stop.is_set(),
            on_progress=on_progress,
            stop_workflow=self.stop_run,
            set_condition=self.set_condition,
        )
        node_repeat = max(int(ctx.params.get("repeat", 1)), 1)
        for _ in range(node_repeat):
            if self._stop.is_set():
                break
            task.run(ctx)
