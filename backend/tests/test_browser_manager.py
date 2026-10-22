"""浏览器管理 / CDP 端点 / 浏览器工具：不启动真实浏览器进程的单元测试。

为什么要这一层：`browser_manager` 里最危险的不是「能不能启动」，而是
「该不该动它」——stop() 关错进程会直接打断用户正在用的浏览器；
launch() 在没装浏览器时若不给出可照做的指引，用户只能看到一句报错。
这里把归属判断、幂等、无浏览器引导都固化成断言。

全部用 monkeypatch：不 spawn 进程、不连真实端口、不联网。
"""
from __future__ import annotations

import sys

import pytest

from app.core.errors import NotFoundError, ValidationError
from app.services import browser_manager as BM
from app.services import cdp_client as C


# ------------------------------------------------------------------ 夹具


@pytest.fixture()
def bm_env(tmp_path, monkeypatch):
    """隔离模块级状态：profile 目录指向临时目录，进程句柄与状态复位。

    注意只清空「候选来源」（内置运行时 + 系统路径），不替换 detect_browsers 本身：
    那几个用例正是要验证它的去重与优先级。
    """
    profile = tmp_path / "browser_profile"
    profile.mkdir()
    monkeypatch.setattr(BM, "PROFILE_DIR", profile, raising=False)
    monkeypatch.setattr(BM, "_proc", None, raising=False)
    monkeypatch.setattr(BM, "_state", BM.BrowserState(profile_dir=str(profile)), raising=False)
    # 默认：没有任何浏览器、端口不通（各用例按需覆盖）
    monkeypatch.setattr(BM, "_bundled_browsers", lambda: [])
    monkeypatch.setattr(BM, "_CANDIDATES", {})
    monkeypatch.setattr(BM, "_port_open", lambda *a, **k: False)
    yield profile
    BM._proc = None


class _FakeProc:
    """够用的 Popen 替身：只需要 pid / poll / terminate / wait / kill。"""

    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def _info(path: str, source: str = "system", name: str = "Google Chrome") -> BM.BrowserInfo:
    return BM.BrowserInfo(path=path, name=name, family="chrome", source=source)


# ------------------------------------------------------------------ CDP 端点白名单


def test_endpoint_rejects_non_loopback():
    """调试端口暴露到外网等于把浏览器整个交出去，必须拒绝。"""
    with pytest.raises(ValidationError):
        C._check_endpoint("http://192.168.1.5:9222")
    with pytest.raises(ValidationError):
        C._check_endpoint("https://example.com:9222")


def test_endpoint_accepts_loopback_and_fills_defaults():
    assert C._check_endpoint("http://127.0.0.1:9222") == "http://127.0.0.1:9222"
    # 省略 scheme 与端口时补默认值，避免调用方各自拼串出错
    assert C._check_endpoint("127.0.0.1") == "http://127.0.0.1:9222"
    assert C._check_endpoint("localhost:9333") == "http://localhost:9333"


# ------------------------------------------------------------------ 检测与引导


def test_detect_browsers_prefers_bundled_and_dedupes(bm_env, tmp_path, monkeypatch):
    exe_sys = tmp_path / "chrome.exe"
    exe_sys.write_bytes(b"MZ")
    bundled = _info(str(exe_sys), source="bundled", name="内置 Chromium")

    # 内置与系统指向同一路径：只应出现一次，且标记为内置（优先级更高）
    monkeypatch.setattr(BM, "_bundled_browsers", lambda: [bundled])
    monkeypatch.setattr(BM, "_CANDIDATES", {"win32": [str(exe_sys)]})
    monkeypatch.setattr(sys, "platform", "win32")

    found = BM.detect_browsers()
    assert len(found) == 1
    assert found[0].source == "bundled"


def test_detect_browsers_names_family_by_filename(bm_env, tmp_path, monkeypatch):
    edge = tmp_path / "msedge.exe"
    edge.write_bytes(b"MZ")
    chromium = tmp_path / "chromium"
    chromium.write_bytes(b"\x7fELF")
    monkeypatch.setattr(BM, "_bundled_browsers", lambda: [])
    monkeypatch.setattr(BM, "_CANDIDATES", {"win32": [str(edge), str(chromium)]})
    monkeypatch.setattr(sys, "platform", "win32")

    found = BM.detect_browsers()
    assert [b.family for b in found] == ["edge", "chromium"]
    assert all(b.source == "system" for b in found)


def test_state_reports_no_browser_with_install_guide(bm_env):
    """一个浏览器都没有时，状态里必须直接带可点击的安装引导。"""
    st = BM.get_state()
    assert st["no_browser"] is True
    guide = st["install_guide"]
    assert guide and guide["options"]
    kinds = [o["kind"] for o in guide["options"]]
    assert kinds == ["bundled", "system"]
    # 推荐项必须是应用内一键装，且带跳转路由
    first = guide["options"][0]
    assert first["recommended"] is True
    assert first["route"] == "/admin/tools"
    assert first["query"]["key"] == "browser_chromium"
    # 兜底项要给出可点的官方下载链接
    assert guide["options"][1]["links"]


def test_state_omits_guide_when_browser_exists(bm_env, tmp_path, monkeypatch):
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(BM, "detect_browsers", lambda: [_info(str(exe))])
    st = BM.get_state()
    assert st["no_browser"] is False
    assert st["install_guide"] is None
    assert st["browsers"][0]["path"].endswith("chrome.exe")


# ------------------------------------------------------------------ launch 的失败路径


def test_launch_without_any_browser_explains_both_paths(bm_env):
    with pytest.raises(NotFoundError) as ei:
        BM.launch()
    msg = str(ei.value)
    # 报错要能让用户照着做：应用内一键装 + 系统浏览器下载地址
    assert "可选组件" in msg
    assert "http" in msg


def test_launch_rejects_missing_executable_path(bm_env, tmp_path, monkeypatch):
    # 有一个可用浏览器才轮得到「指定路径不存在」这条校验
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(BM, "detect_browsers", lambda: [_info(str(exe))])
    with pytest.raises(ValidationError):
        BM.launch(executable=str(tmp_path / "nope.exe"))


def test_launch_builds_expected_flags(bm_env, tmp_path, monkeypatch):
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(BM, "detect_browsers", lambda: [_info(str(exe))])

    captured: dict = {}
    started = {"v": False}

    def fake_popen(args, **kw):
        captured["args"] = args
        captured["kw"] = kw
        started["v"] = True
        return _FakeProc()

    monkeypatch.setattr(BM.subprocess, "Popen", fake_popen)
    # 起进程之前端口不通（否则 launch 会判定「已在运行」直接复用），之后立刻就绪
    monkeypatch.setattr(BM, "_port_open", lambda *a, **k: started["v"])

    ret = BM.launch(port=9333)
    args = captured["args"]
    assert args[0] == str(exe)
    assert "--remote-debugging-port=9333" in args
    # 调试端口默认只绑 loopback
    assert "--remote-debugging-address=127.0.0.1" in args
    assert any(a.startswith("--user-data-dir=") for a in args)
    # 独立 profile 目录必须在启动前建好，否则浏览器会自己造一个
    assert bm_env.is_dir()
    assert ret["running"] is True and ret["managed"] is True


def test_launch_is_idempotent_when_port_ready(bm_env, monkeypatch):
    """端口已就绪时不得再起第二个实例（重复点击会开出两个浏览器）。"""
    monkeypatch.setattr(BM, "_port_open", lambda *a, **k: True)
    called: list = []
    monkeypatch.setattr(BM.subprocess, "Popen", lambda *a, **k: called.append(1))
    ret = BM.launch()
    assert called == []
    assert ret["port_ready"] is True


def test_launch_headless_flag(bm_env, tmp_path, monkeypatch):
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(BM, "detect_browsers", lambda: [_info(str(exe))])
    captured: dict = {}
    started = {"v": False}

    def fake_popen(args, **kw):
        captured["args"] = args
        started["v"] = True
        return _FakeProc()

    monkeypatch.setattr(BM.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(BM, "_port_open", lambda *a, **k: started["v"])

    BM.launch(headless=True)
    assert "--headless=new" in captured["args"]


# ------------------------------------------------------------------ stop / 归属判断


def test_stop_without_running_reports_noop(bm_env):
    ret = BM.stop()
    assert "没有在运行" in ret["message"]


def test_stop_terminates_owned_process(bm_env, monkeypatch):
    proc = _FakeProc()
    monkeypatch.setattr(BM, "_proc", proc, raising=False)
    monkeypatch.setattr(BM, "_state", BM.BrowserState(running=True, pid=proc.pid,
                                                    profile_dir=str(bm_env)), raising=False)
    ret = BM.stop()
    assert proc.terminated is True
    assert "已停止" in ret["message"]
    assert BM._proc is None


def test_stop_leaves_foreign_browser_alone(bm_env, monkeypatch):
    """端口通但句柄丢失且 force_cdp=False：绝不能动（可能是用户自己开的调试浏览器）。"""
    monkeypatch.setattr(BM, "_port_open", lambda *a, **k: True)
    called: list = []
    monkeypatch.setattr(BM, "_run_cdp_shutdown", lambda port: called.append(port) or True)

    ret = BM.stop(force_cdp=False)
    assert called == []
    assert "不是由本服务启动" in ret["message"]


def test_stop_uses_cdp_when_handle_lost(bm_env, monkeypatch):
    """后端重启后句柄丢失，但实例用的是我们的 profile：走 CDP 优雅关闭。"""
    monkeypatch.setattr(BM, "_port_open", lambda *a, **k: True)
    monkeypatch.setattr(BM, "_run_cdp_shutdown", lambda port: True)

    ret = BM.stop()
    assert "CDP" in ret["message"]
    assert BM._state.running is False


def test_stop_reports_failure_when_cdp_refuses(bm_env, monkeypatch):
    monkeypatch.setattr(BM, "_port_open", lambda *a, **k: True)
    monkeypatch.setattr(BM, "_run_cdp_shutdown", lambda port: False)
    ret = BM.stop()
    assert "请手动关闭" in ret["message"]


def test_adopt_existing_requires_our_profile_lock(bm_env, monkeypatch):
    """后端重启后句柄丢失：只有 profile 锁文件存在才认作「我们的」实例。"""
    monkeypatch.setattr(BM, "_port_open", lambda *a, **k: True)
    assert BM._adopt_existing(9222) is False

    # 两种平台的锁文件名都要覆盖（CI 与开发机平台不同）
    (bm_env / "SingletonLock").write_text("")
    (bm_env / "lockfile").write_text("")
    assert BM._adopt_existing(9222) is True
    assert BM._state.running is True
    assert BM._state.port == 9222


def test_cleanup_on_shutdown_only_touches_owned(bm_env, monkeypatch):
    proc = _FakeProc()
    monkeypatch.setattr(BM, "_proc", proc, raising=False)
    BM.cleanup_on_shutdown()
    assert proc.terminated is True
    assert BM._proc is None

    # 没有句柄时不能去杀任何东西（用户自己开的浏览器必须留着）
    BM._proc = None
    BM.cleanup_on_shutdown()   # 不抛错即可


# ------------------------------------------------------------------ ensure_running


def test_ensure_running_without_auto_launch_gives_hint(bm_env, monkeypatch):
    ok, msg = BM.ensure_running(auto_launch=False)
    assert ok is False
    assert "浏览器" in msg and "可选组件" in msg


def test_ensure_running_ok_when_port_ready(bm_env, monkeypatch):
    monkeypatch.setattr(BM, "_port_open", lambda *a, **k: True)
    assert BM.ensure_running() == (True, "")


def test_ensure_running_reports_launch_failure(bm_env, monkeypatch):
    def boom(**kw):
        raise NotFoundError("本机未检测到 Chrome / Edge / Chromium。")

    monkeypatch.setattr(BM, "launch", boom)
    ok, msg = BM.ensure_running(auto_launch=True)
    assert ok is False
    assert "未检测到" in msg


# ------------------------------------------------------------------ 工具层


def test_ensure_browser_emits_structured_guide_when_none_installed(bm_env, monkeypatch):
    """纯文本指引在聊天里点不动，必须回结构化 data 供前端渲染安装卡片。"""
    import asyncio

    from app.agents.tools import browser_tools as BT

    monkeypatch.setattr(BM, "ensure_running", lambda **kw: (False, "调试端口未开启"))
    monkeypatch.setattr(BM, "detect_browsers", lambda: [])

    err, hint = asyncio.run(BT._ensure_browser())
    assert err and "调试端口未开启" in err
    assert hint and hint["browser_missing"] is True
    assert hint["install_guide"]["options"]


def test_ensure_browser_has_no_guide_when_browser_exists(bm_env, monkeypatch):
    """装了浏览器但没启动：只给启动指引，不该再劝用户去安装。"""
    import asyncio

    from app.agents.tools import browser_tools as BT

    monkeypatch.setattr(BM, "ensure_running", lambda **kw: (False, "调试端口未开启"))
    monkeypatch.setattr(BM, "detect_browsers", lambda: [_info("C:/chrome.exe")])

    err, hint = asyncio.run(BT._ensure_browser())
    assert err and "系统设置 → 浏览器" in err
    assert hint is None


def test_pick_tab_matches_url_or_title_substring():
    from app.agents.tools.browser_tools import _pick_tab

    tabs = [
        C.Tab(id="1", title="后台首页", url="https://admin.example.com/home", ws_url="ws://a"),
        C.Tab(id="2", title="Orders — 订单", url="https://admin.example.com/orders", ws_url="ws://b"),
    ]
    assert _pick_tab(tabs, None) is tabs[0]
    assert _pick_tab(tabs, "orders").id == "2"      # URL 子串
    assert _pick_tab(tabs, "订单").id == "2"         # 标题子串
    assert _pick_tab(tabs, "不存在的页面") is None
    assert _pick_tab([], None) is None


def test_browser_tabs_tool_returns_structured_tabs(bm_env, monkeypatch):
    import asyncio

    from app.agents.tools import browser_tools as BT

    monkeypatch.setattr(BM, "ensure_running", lambda **kw: (True, ""))

    async def fake_tabs():
        return [C.Tab(id="1", title="首页", url="https://x.test/", ws_url="ws://x")]

    monkeypatch.setattr(C, "list_tabs", fake_tabs)

    res = asyncio.run(BT.BrowserTabsTool().run({}, None))
    assert res.is_error is False
    assert "1 个标签页" in res.content
    assert res.data["tabs"][0]["url"] == "https://x.test/"


# ------------------------------------------------------------------ CDP 会话


class _FakeWS:
    """websockets 连接替身：记录 send，recv 由用例控制。"""

    def __init__(self, reply: dict | None = None, hang: bool = False) -> None:
        self.sent: list[dict] = []
        self._reply = reply
        self._hang = hang
        self.closed = False

    async def send(self, raw: str):
        import json

        msg = json.loads(raw)
        self.sent.append(msg)
        # 模拟浏览器：只回显 id，不回内容
        if not self._hang:
            self._reply_id = msg["id"]

    async def recv(self):
        import asyncio

        await asyncio.sleep(3600)   # 永不返回，用于触发超时

    async def close(self):
        self.closed = True


def _session_with_reply(reply: dict) -> C.CDPSession:
    """建一个不碰网络的会话：send 立刻把预设响应塞进 pending。"""
    sess = C.CDPSession("ws://127.0.0.1:9222/devtools/page/1", timeout=0.05)
    ws = _FakeWS()
    sent = ws.sent

    async def send(raw: str):
        import asyncio
        import json

        msg = json.loads(raw)
        sent.append(msg)
        await asyncio.sleep(0)
        fut = sess._pending.get(msg["id"])
        if fut and not fut.done():
            fut.set_result({"id": msg["id"], **reply})

    ws.send = send  # type: ignore[assignment]
    sess._ws = ws
    return sess


def test_cdp_call_returns_result():
    import asyncio

    sess = _session_with_reply({"result": {"value": 42}})
    out = asyncio.run(sess.call("Runtime.evaluate", {"expression": "1"}))
    assert out == {"value": 42}
    assert sess._ws.sent[0]["method"] == "Runtime.evaluate"  # type: ignore[union-attr]


def test_cdp_call_raises_on_error_payload():
    import asyncio

    sess = _session_with_reply({"error": {"message": "Node not found"}})
    with pytest.raises(ValidationError) as ei:
        asyncio.run(sess.call("DOM.getDocument"))
    assert "Node not found" in str(ei.value)


def test_cdp_call_times_out_without_reply():
    """浏览器不回包时必须超时报错，否则工具调用会一直挂着。"""
    import asyncio

    sess = C.CDPSession("ws://127.0.0.1:9222/devtools/page/1", timeout=0.05)

    async def noop(raw):
        return None

    sess._ws = _FakeWS()
    sess._ws.send = noop  # type: ignore[assignment]

    with pytest.raises(ValidationError) as ei:
        asyncio.run(sess.call("Page.navigate", {"url": "about:blank"}))
    assert "超时" in str(ei.value)
    # 超时后不得留下悬挂的 future，否则后续 call 的 id 会串
    assert sess._pending == {}


def test_cdp_get_text_handles_non_dict_result():
    """页面返回非对象（如被导航打断）时给空结果，而不是抛 TypeError。"""
    import asyncio

    sess = C.CDPSession("ws://x", timeout=0.05)
    sess.evaluate = lambda expr, **kw: _async_val("oops")  # type: ignore[assignment]
    out = asyncio.run(sess.get_text())
    assert out == {"title": "", "url": "", "text": "", "links": []}


async def _async_val(v):
    return v


def test_cdp_get_text_truncates():
    import asyncio

    sess = C.CDPSession("ws://x", timeout=0.05)
    payload = {"title": "T", "url": "u", "text": "x" * 100, "links": []}
    sess.evaluate = lambda expr, **kw: _async_val(payload)  # type: ignore[assignment]
    out = asyncio.run(sess.get_text(max_chars=10))
    assert out["truncated"] is True
    assert out["char_count"] == 100
    assert len(out["text"]) == 10


def test_list_tabs_raises_validation_error_when_unreachable(monkeypatch):
    """端口没开时要抛带指引的 ValidationError，而不是裸 ConnectionError。"""
    import asyncio

    import httpx

    class _BoomClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "AsyncClient", _BoomClient)
    with pytest.raises(ValidationError) as ei:
        asyncio.run(C.list_tabs())
    assert "--remote-debugging-port" in str(ei.value)


def test_list_tabs_skips_non_page_targets(monkeypatch):
    import asyncio

    import httpx

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"type": "page", "id": "a", "title": "T", "url": "https://x/", "webSocketDebuggerUrl": "ws://a"},
                {"type": "service_worker", "id": "b", "webSocketDebuggerUrl": "ws://b"},
                {"type": "page", "id": "c", "title": "no-ws", "url": "https://y/"},
            ]

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    tabs = asyncio.run(C.list_tabs())
    # 只保留有 ws 的 page/iframe：service_worker 与缺 ws 的条目都跳过
    assert [t.id for t in tabs] == ["a"]
