"""测试全局隔离：禁止测试触碰真实键鼠、真实屏幕与用户定时配置。

背景：`test_schedule_fire_reloads_workflow` 会真的跑一个含鼠标节点的工作流，
`AppController` 构造时又会加载用户目录下的 config.json。未隔离时跑一次测试
就会移动用户光标、并可能改写真实定时配置。

副作用边界只在**当前进程**生效；`test_rpc.py` 里拉起的 sidecar 子进程
（`python -m rpc.server`）有自己的模块状态，不受此 fixture 影响。
"""
from __future__ import annotations

import pytest


class FakeMouse:
    """替代 pynput 鼠标：只记录动作，不产生任何系统输入。"""

    def __init__(self) -> None:
        self.pos = (0.0, 0.0)
        self.presses = []
        self.releases = []
        self.scrolls = []

    @property
    def position(self):
        return self.pos

    @position.setter
    def position(self, v):
        self.pos = (float(v[0]), float(v[1]))

    def press(self, b):
        self.presses.append(str(b))

    def release(self, b):
        self.releases.append(str(b))

    def click(self, b, count=1):
        self.presses.append(str(b))
        self.releases.append(str(b))

    def scroll(self, dx, dy):
        self.scrolls.append((dx, dy))


class FakeKeyboard:
    """替代 pynput 键盘：只记录按键，不产生任何系统输入。"""

    def __init__(self) -> None:
        self.log = []

    def press(self, k):
        self.log.append(("down", k))

    def release(self, k):
        self.log.append(("up", k))

    def type(self, t):
        self.log.append(("type", t))


@pytest.fixture(autouse=True)
def isolate_desktop_and_schedule(monkeypatch, tmp_path):
    """隔离副作用：假键鼠 + 独立数据目录。

    AUTOFLOW_DATA_DIR 会写进环境变量，故 `test_rpc.py` 拉起的 sidecar 子进程
    同样落在临时目录——它再也读不到、也不会删除用户仓库根目录下的 config.json。
    """
    from core import player
    from rpc.controller import AppController

    monkeypatch.setenv("AUTOFLOW_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setattr(player, "MouseController", FakeMouse)
    monkeypatch.setattr(player, "KeyboardController", FakeKeyboard)
    # 不读取、也不写回用户真实 config.json（定时配置）
    monkeypatch.setattr(AppController, "_schedule_load", lambda self: {})
    monkeypatch.setattr(AppController, "_schedule_save", lambda self, cfg: None)
