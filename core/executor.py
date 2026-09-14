"""流程图执行器：从起始节点沿有向边遍历，支持条件分派、多路分支、汇合与循环。

**v4 起不再是「按顺序遍历节点列表」**。模型换成 节点 + 有向边（见 `core/events.py`）：

- 每个节点有若干**出口名**（port）。操作节点只有 `out`；条件节点有 `true`/`false`；
  分支节点有 `case:1..N` 与 `else`；`end` 节点没有出口。
- 执行 = 从起始节点开始，跑一个节点、算出它走哪个出口、顺着那条边到下一个节点。
- **多条边指向同一节点就是汇合**——不需要特殊语法，到达就执行。
- 允许回边形成循环，因此必须有步数上限：没有它，一个连错成环的图会让界面永远
  停在「运行中」而且停不下来（用户只能强杀进程）。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional

from core.player import Player
from core.events import Workflow, Node, PORT_OUT


@dataclass
class RunContext:
    params: dict
    player: Player
    speed: float = 1.0
    base_x: int = 0
    base_y: int = 0
    on_progress: Optional[Callable[[int, int], None]] = None
    stop_workflow: Callable[[], None] = lambda: None
    set_port: Callable[[str], None] = lambda p: None
    is_stopping: Callable[[], bool] = lambda: False

    @property
    def stopping(self) -> bool:
        return bool(self.is_stopping())

    def offset_for(self, use_relative: bool, x: int, y: int) -> tuple:
        if use_relative:
            return (self.base_x + x, self.base_y + y)
        return (x, y)


class Executor:
    """流程图执行器。回调（UI 更新）由调用方保证线程安全。"""

    # 单次遍历的步数上限。正常流程图几十步就到底了；撞到上限一定是连成了环。
    # 取 10000 是因为「循环 + 内部还有重试」的正当用法也可能跑几百步，
    # 但真死循环时 10000 步在纯计算节点上也就毫秒级，停得下来。
    MAX_STEPS = 10000

    def __init__(self) -> None:
        self.player = Player()
        self._stop = threading.Event()
        self.running = False
        self._port = PORT_OUT

    def stop_run(self) -> None:
        self._stop.set()
        self.player.stop_playback()

    def set_port(self, port: str) -> None:
        """供条件/分支节点写入「本次该走哪个出口」。"""
        self._port = port

    def reset(self) -> None:
        self._stop.clear()
        self.player._stop.clear()

    # ---- 遍历 ----

    @staticmethod
    def entry_uid(wf: Workflow) -> str:
        """起始节点：显式指定的 `start` 优先，其次第一个 start 节点，最后退回第一个节点。"""
        uids = {n.uid for n in wf.nodes}
        if wf.start and wf.start in uids:
            return wf.start
        for n in wf.nodes:
            if n.type == "start":
                return n.uid
        return wf.nodes[0].uid if wf.nodes else ""

    @staticmethod
    def next_map(wf: Workflow) -> dict:
        """(源 uid, 出口名) -> 目标 uid。

        同一 (源, 出口) 只保留一条：`edge.add` 在写入时就替换旧的，所以这里
        后写覆盖不会丢信息。**不支持一个出口扇出多条**——那要引入并行执行，
        和「顺序执行 + 汇合」是两套模型。
        """
        out: dict[tuple[str, str], str] = {}
        for e in wf.edges:
            if e.src and e.dst:
                out[(e.src, e.port)] = e.dst
        return out

    def run_workflow(self, wf: Workflow, base_x: int = 0, base_y: int = 0,
                     on_node: Optional[Callable[[int, str], None]] = None,
                     on_progress: Optional[Callable[[int, int], None]] = None,
                     on_done: Optional[Callable[[bool], None]] = None,
                     on_error: Optional[Callable[[int, str, Exception], None]] = None) -> None:
        """在工作流线程执行；结束/被停止后调用 on_done(stopped)。"""
        self.reset()
        self.running = True
        try:
            repeat = max(wf.repeat, 1)
            for _ in range(repeat):
                if self._stop.is_set():
                    break
                self._walk(wf, base_x, base_y, on_node, on_progress, on_error)
        finally:
            self.running = False
            if on_done:
                on_done(self._stop.is_set())

    def _walk(self, wf: Workflow, base_x: int, base_y: int,
              on_node: Optional[Callable], on_progress: Optional[Callable],
              on_error: Optional[Callable]) -> None:
        by_uid = {n.uid: n for n in wf.nodes}
        index_of = {n.uid: i for i, n in enumerate(wf.nodes)}
        nxt = self.next_map(wf)

        cur = self.entry_uid(wf)
        steps = 0
        while cur and not self._stop.is_set():
            steps += 1
            if steps > self.MAX_STEPS:
                # 撞上限 = 图里有环没收敛。必须**主动停**并报错，不能继续转，
                # 否则界面永远停在「运行中」，用户除了强杀没有别的办法。
                self._stop.set()
                self.player.stop_playback()
                if on_error:
                    node_type = by_uid[cur].type if cur in by_uid else "?"
                    on_error(-1, node_type, RuntimeError(
                        f"流程图执行步数超过上限 {self.MAX_STEPS}（节点 {node_type}），"
                        f"多半是连成了环。请检查回边。"))
                return

            node = by_uid.get(cur)
            if node is None:
                return
            if node.type == "end":
                # 显式终点。上报一次，让界面能显示「走到这里结束了」——
                # 不报的话最后一步会停在 end 的**前一个**节点上，看起来像没跑完。
                if on_node:
                    on_node(index_of.get(cur, -1), node.type)
                return
            if not node.enabled or node.type == "note":
                # 停用 / 注释：**不执行、也不上报**（它们不是「跑过」的节点），
                # 但路径必须继续往下走——等价于「跳过」，不是「截断」。
                cur = nxt.get((node.uid, PORT_OUT), "")
                continue
            if on_node:
                on_node(index_of.get(cur, -1), node.type)

            port = self._run_node(node, wf.speed, base_x, base_y, on_progress, on_error)
            if port is None:            # 节点抛异常，_run_node 已停止工作流
                return
            cur = nxt.get((node.uid, port), "")

    def _run_node(self, node: Node, wf_speed: float, base_x: int, base_y: int,
                  on_progress: Optional[Callable],
                  on_error: Optional[Callable] = None) -> Optional[str]:
        import tasks.builtin  # noqa: F401  确保节点已注册
        from tasks.base import get_task
        task = get_task(node.type)
        if task is None:
            return PORT_OUT
        self._port = PORT_OUT           # 默认出口；条件/分支节点会覆盖它
        ctx = RunContext(
            params={**task.defaults(), **(node.params or {})},
            player=self.player,
            speed=max(wf_speed, 0.01),
            base_x=base_x, base_y=base_y,
            on_progress=on_progress,
            stop_workflow=self.stop_run,
            set_port=self.set_port,
            is_stopping=lambda: self._stop.is_set(),
        )
        node_repeat = max(int(ctx.params.get("repeat", 1)), 1)
        for _ in range(node_repeat):
            if self._stop.is_set():
                break
            try:
                task.run(ctx)
            except Exception as e:  # 节点异常不拖垮整个工作流
                self._stop.set()
                self.player.stop_playback()
                if on_error:
                    on_error(-1, node.type, e)
                return None
        return self._port
