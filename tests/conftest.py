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


@pytest.fixture(autouse=True)
def block_real_screen_capture(monkeypatch):
    """默认禁止真实截屏。

    截屏本身是只读的，但在**别人的机器上跑测试**时它会：① 触发「屏幕录制」
    TCC 授权弹窗；② 在 headless 会话里阻塞或抛底层错误；③ 把测试结果绑定到
    当时的桌面内容上（不可复现）。

    需要截图的用例请显式替换 `vision._raw_screens`（见 `tests/test_vision.py`
    的假显示器写法），它会覆盖这里的默认桩。
    """
    from core import vision

    def blocked(*_args, **_kwargs):
        raise RuntimeError(
            "测试环境禁止真实截屏：请 monkeypatch core.vision._raw_screens "
            "返回假显示器（参考 tests/test_vision.py）")

    monkeypatch.setattr(vision, "_raw_screens", blocked)
    monkeypatch.setattr(vision, "_quartz_capture", blocked)


@pytest.fixture(autouse=True)
def block_real_text_typing(monkeypatch):
    """默认禁止真实键盘投递。

    `CGEventPost` 会把字符打进**用户当前聚焦的窗口**——跑测试时那可能是他的
    编辑器或聊天框。文本输入的用例请注入 `mactype.type_text(text, post=记录器)`
    收集事件，不要真投递。
    """
    from core import mactype

    def blocked(*_args, **_kwargs):
        raise RuntimeError(
            "测试环境禁止真实键盘投递：请用 mactype.type_text(text, post=记录器)"
            " 或直接断言事件构造，不要 CGEventPost")

    monkeypatch.setattr(mactype, "_default_post", blocked)


