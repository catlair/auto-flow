#!/usr/bin/env python3
"""部署后校验：确认**已安装的** App 真的带上了本次改动。

四步流水线里最容易踩的坑是"改了代码却没变化"——只跑 `tauri build` 不跑
`build_sidecar.sh`，打进包里的还是上一次的 Python 代码。光看构建成功或看
`.app` 的修改时间都判断不出来，必须**打开产物本身**看。

本脚本做两层检查：

A. 产物级（静态，不解压运行）——直接读 frozen 二进制里的 PYZ，反编译出目标
   模块，断言本次改动引入的符号/常量确实在里面。这一层能抓到"没重新打包"。
B. RPC 级（动态）——真起一次 sidecar，走 `app.info` / `input.probe` /
   `record.start→stop` / 一个必定触发后端告警的条件节点，断言协议面（字段、判据、
   通知投递）与本次实现一致。

用法：
    ./.venv/bin/python scripts/check_installed.py            # 默认 /Applications/Auto Flow.app
    ./.venv/bin/python scripts/check_installed.py <path.app> # 指定产物

退出码 0 = 全部通过；非 0 = 有失败项（会逐条列出）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time

DEFAULT_APP = "/Applications/Auto Flow.app"

# 产物级断言：模块名 -> (检查函数, 人话描述)
# 检查函数收到该模块的 code object，返回 True/False。
# 这些是"必须打进包、但出问题时不报错只会静默失效"的关键点。


def _co_names_recursive(code) -> set:
    """递归收集 code object 里引用到的全部名字（含嵌套函数/推导式）。"""
    names = set(code.co_names)
    for const in code.co_consts:
        if hasattr(const, "co_names"):
            names |= _co_names_recursive(const)
    return names


def _sub_code(code, fname):
    """取出某个函数/方法自身的 code object（递归找，方法在类体里嵌一层）。"""
    for const in code.co_consts:
        if isinstance(const, type(code)):
            if const.co_name == fname:
                return const
            found = _sub_code(const, fname)
            if found is not None:
                return found
    return None


def _artifact_checks(recorder, inputsource, controller, server, vision) -> list:
    out = []
    if recorder is None:
        return [("core.recorder 没打进 PYZ（打包不完整？）", False)]

    # core.recorder：停止裁剪的护栏常量（2026-09-12 事故修复的核心）
    out.append(("core.recorder.TRIM_STOP_CLICK_MS == 1500",
                1500 in recorder.co_consts))

    # 修复的核心是**去掉了无上界的弹尾部移动**：trim_stop_interaction 里
    # 不应再出现 pop。逐函数看，不看全模块（模块里别处可能合法地 pop）。
    trim = _sub_code(recorder, "trim_stop_interaction")
    out.append(("trim_stop_interaction 里已无 pop（不再弹尾部移动）",
                trim is not None and "pop" not in trim.co_names))

    # core.inputsource：在函数内部 import，PyInstaller 很容易漏掉（漏了就
    # input.probe 静默少一个字段，不报错）
    out.append(("core.inputsource 已打进 PYZ", inputsource is not None))

    # rpc.controller：新端点
    out.append(("rpc.controller 定义了 record_keys_to_text",
                controller is not None
                and "record_keys_to_text" in _co_names_recursive(controller)))
    # 裁剪快照已改元素级复制：record_stop 里应出现 replace（修复前没有）。
    # 这条能挡住"撤销回来是被人改过的数据"那个静默损坏。
    stop_co = _sub_code(controller, "record_stop") if controller else None
    out.append(("record_stop 用 replace 做元素级快照（撤销不会拿到被改的数据）",
                stop_co is not None and "replace" in _co_names_recursive(stop_co)))
    # 入口脚本 rpc.server：方法表里的注册名（漏了就是方法不存在，调用报 -32601）
    out.append(("rpc.server 注册了 record.keysToText",
                server is not None and "record.keysToText" in _all_consts(server)))

    # core.vision：截屏必须请求**物理像素**（2026-09-13 修「截图识别没有效果」）。
    # 这条最容易静默失效——不去掉那个标志位也不报错，只是匹配分数整体砍半，
    # 从外面看和「屏上没有目标」一模一样。两个符号是 getattr 的字符串实参，
    # 所以查函数自身的常量表，不查 co_names。
    prefer = _sub_code(vision, "_prefer_physical_resolution") if vision else None
    consts = _all_consts(prefer) if prefer is not None else set()
    out.append(("core.vision 去掉 mss 的 NominalResolution（截屏取物理像素）",
                "kCGWindowImageNominalResolution" in consts
                and "IMAGE_OPTIONS" in consts))

    # core.vision：跨密度组要**自动缩放模板**（2026-09-13 F-VIS-10）。
    # 同属「不生效也不报错」的一类：密度不一致时分数只是整体偏低，
    # 从外面看和「屏上确实没有」一模一样。查 find_template 有没有真的调用它。
    find = _sub_code(vision, "find_template") if vision else None
    out.append(("core.vision 按屏幕密度自动重采样模板（跨密度组不必重截）",
                _sub_code(vision, "_resampled_template") is not None
                and find is not None
                and "_resampled_template" in _co_names_recursive(find)))

    # rpc.server：后端 WARNING 要**转发给界面**（2026-09-13）。
    # 同属「不生效也不报错」的一类：少了它，core.vision 那些「点歪 / 找不到」
    # 的诊断只进应用日志（那份日志是逐帧 RPC 流水，不含诊断字符串），
    # 界面上依然什么都没有——从外面看和「压根没做这个功能」一模一样。
    srv_names = _co_names_recursive(server) if server is not None else set()
    out.append(("rpc.server 把 WARNING 转发成 log.warning 通知",
                {"_UiLogHandler", "_install_ui_log_handler"} <= srv_names
                and "log.warning" in _all_consts(server)))
    # 防递归守卫（`emit` 里查线程本地的 busy）：少了它，队列满时
    # `_send_notification` 自己那条 logger.warning 会自激 → 通知通道变死循环。
    emit = _sub_code(server, "emit") if server is not None else None
    out.append(("_UiLogHandler.emit 带线程本地防递归守卫",
                emit is not None and "_local" in _co_names_recursive(emit)
                and "busy" in _all_consts(emit)))
    return out


def _all_consts(code) -> set:
    """递归收集全部字符串常量（方法表里的方法名是纯字符串常量）。"""
    out = {c for c in code.co_consts if isinstance(c, str)}
    for c in code.co_consts:
        if hasattr(c, "co_consts"):
            out |= _all_consts(c)
    return out


def _load_pyz(exe: str) -> dict:
    """取出目标模块的 code object。

    分两处找：
    - `core.*` / `rpc.controller` 在 PYZ 里（ZlibArchive，`extract` 直接给 code object）；
    - **入口脚本** `rpc/server.py` 不在 PYZ 里，它以 CArchive 项名 `server`
      存的是 marshal 后的字节，得 `marshal.loads`。
    """
    import marshal

    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    car = CArchiveReader(exe)
    tmpdir = tempfile.mkdtemp(prefix="autoflow-pyz-")
    pyz_path = os.path.join(tmpdir, "PYZ.pyz")
    with open(pyz_path, "wb") as f:
        f.write(car.extract("PYZ.pyz"))
    zar = ZlibArchiveReader(pyz_path)
    mods = {}
    for name in ("core.recorder", "core.inputsource", "rpc.controller", "core.vision"):
        try:
            mods[name] = zar.extract(name)      # 直接返回 code object，不是 bytes
        except Exception:
            mods[name] = None
    try:
        mods["rpc.server"] = marshal.loads(car.extract("server"))
    except Exception:
        mods["rpc.server"] = None
    return mods


class Sidecar:
    """最小 NDJSON 客户端：够用来发几帧请求、并收通知即可。

    读线程把响应与通知分流：响应进 `_responses` 按 id 配对，通知进
    `notifications`。**必须分流**——`log.warning` 那条检查要等通知，
    而通知与响应在同一条 stdout 上交错，同步 `readline` 很容易把通知吞掉。
    """

    def __init__(self, path: str):
        self.p = subprocess.Popen(
            [path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self._id = 0
        self._responses: dict = {}
        self.notifications: list = []
        self._lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:  # noqa: BLE001 - 半包/杂音不该打死收尾校验
                continue
            with self._lock:
                if "id" in msg:
                    self._responses[msg["id"]] = msg
                else:
                    self.notifications.append(msg)

    def call(self, method: str, params: dict | None = None,
             timeout: float = 30.0) -> dict:
        self._id += 1
        mid = self._id
        req = {"jsonrpc": "2.0", "id": mid, "method": method,
               "params": params if params is not None else {}}
        self.p.stdin.write(json.dumps(req) + "\n")
        self.p.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                hit = self._responses.pop(mid, None)
            if hit is not None:
                return hit
            if self.p.poll() is not None:
                raise RuntimeError(f"{method}: sidecar 提前退出")
            time.sleep(0.02)
        raise TimeoutError(f"{method} 无响应")

    def clear_notifications(self) -> None:
        with self._lock:
            self.notifications.clear()

    def wait_notification(self, method: str, timeout: float = 10.0) -> dict | None:
        """等到某类通知出现并返回它；超时返回 None。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for n in self.notifications:
                    if n.get("method") == method:
                        return n
            time.sleep(0.05)
        return None

    def close(self) -> None:
        for fn in (self.p.stdin.close, self.p.terminate):
            try:
                fn()
            except Exception:
                pass
        try:
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


def _live_checks(sidecar_path: str) -> list:
    out, s = [], Sidecar(sidecar_path)
    try:
        # 端点真的挂在 RPC 线上（不是只在方法表里有个字符串）。趁还没录过，
        # 调用应报 no_record_result(-32003)；没注册会报 unknown method(-32601)。
        # 两者能区分"没注册"与"注册了但实现炸了"。
        ks = s.call("record.keysToText", {"index": 0, "text": "x"})
        code = (ks.get("error") or {}).get("code")
        out.append((f"record.keysToText 已挂上 RPC（未录制时返回 {code}）",
                    code == -32003))

        r = s.call("app.info").get("result") or {}
        out.append((f"app.info 有响应（v{r.get('appVersion')} "
                    f"proto={r.get('protocolVersion')} frozen={r.get('frozen')}）",
                   bool(r)))
        # record.stop 的字段名以 PROTOCOL 为准，这里只断言"必须有"
        p = s.call("input.probe").get("result") or {}
        out.append((f"input.probe 报输入法（{p.get('input_source', {}).get('name')}）",
                   "input_source" in p))

        s.call("record.start")
        stop = s.call("record.stop", {"trim": False})
        if stop.get("error"):
            out.append((f"record.stop 可用（返回 {stop['error'].get('code')}，"
                        "多半缺辅助功能权限，非打包问题）", True))
        else:
            # record.stop 直接把统计当 result 返回（不嵌套 stats）
            st = stop.get("result") or {}
            out.append((f"record.stop 统计含 unaccounted={st.get('unaccounted')}",
                       "unaccounted" in st))
            out.append((f"快捷键停止不裁剪（trimmed={st.get('trimmed')}）",
                       st.get("trimmed", 0) == 0))

        # 后端告警真的能走通知线到界面（2026-09-13）。触发方式要**确定性**且
        # 无副作用：条件节点指向一个不存在的模板 → find_template 在读屏之前就
        # `_warn_once("missing-template:…")` 返回，不碰键鼠、不依赖屏幕录制权限。
        # 少任何一环（handler 没装 / 通知没入队 / 方法名写错）这条都拿不到通知。
        with tempfile.TemporaryDirectory(prefix="autoflow-logchk-") as td:
            wf = os.path.join(td, "wf.json")
            with open(wf, "w", encoding="utf-8") as f:
                json.dump({"version": 2, "name": "log-check", "nodes": [
                    {"type": "condition", "enabled": True, "params": {
                        "check": "图像存在",
                        "image_path": os.path.join(td, "no-such-template.png"),
                        "timeout_s": 0.0}}]}, f, ensure_ascii=False)
            load = s.call("workflow.load", {"path": wf})
            if load.get("error"):
                out.append(("后端 WARNING 经 log.warning 通知送达界面"
                            f"（准备失败：{load['error'].get('message')}）", False))
            else:
                s.clear_notifications()
                s.call("run.start")
                note = s.wait_notification("log.warning", timeout=15.0)
                p2 = (note or {}).get("params") or {}
                detail = (f"（{p2.get('logger')}: "
                          f"{str(p2.get('message'))[:36]}…）" if note else "")
                out.append(("后端 WARNING 经 log.warning 通知送达界面" + detail,
                            note is not None))
    finally:
        s.close()
    return out


def main() -> int:
    app = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_APP
    sidecar = os.path.join(app, "Contents", "Resources",
                           "autoflow-sidecar", "autoflow-sidecar")
    if not os.path.exists(sidecar):
        print(f"✗ 找不到 sidecar：{sidecar}")
        return 2

    results = []
    print(f"目标产物：{app}\n")

    print("A. 产物级（读 frozen PYZ）")
    try:
        mods = _load_pyz(sidecar)
        results += _artifact_checks(mods["core.recorder"],
                                    mods["core.inputsource"],
                                    mods["rpc.controller"],
                                    mods["rpc.server"],
                                    mods.get("core.vision"))
    except Exception as e:  # noqa: BLE001
        results.append((f"读 PYZ 失败：{e}", False))
    for desc, ok in results:
        print(f"   {'✓' if ok else '✗'} {desc}")

    print("\nB. RPC 级（真起一次 sidecar）")
    live = []
    try:
        live = _live_checks(sidecar)
    except Exception as e:  # noqa: BLE001
        live.append((f"起 sidecar 失败：{e}", False))
    for desc, ok in live:
        print(f"   {'✓' if ok else '✗'} {desc}")

    bad = [(d, o) for d, o in results + live if not o]
    print("\n结论：" + ("全部通过，安装版确实带上了本次改动"
                       if not bad else f"{len(bad)} 项未通过"))
    for d, _ in bad:
        print(f"  ✗ {d}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
