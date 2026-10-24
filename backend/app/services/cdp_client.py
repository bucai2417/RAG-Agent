"""极简 CDP 客户端：直连 Chrome/Edge 调试端口，复用用户已登录的浏览器。

为什么不用 Playwright：
- Playwright 需要额外安装 ~300MB 浏览器与 Python 包
- 而「复用登录态」这个需求本身就是**连到用户已经开着的浏览器**，
  用 CDP 直连最直接，不需要另起一个干净的浏览器实例

安全边界（重要）：
- 只连 loopback 地址（127.0.0.1 / localhost），不连远程调试端口
- 只做「读取与受控操作」，不执行任意 JS 字符串
  （evaluate 仅用于读取 DOM 文本，不开放给 AI 传任意脚本）
- 所有操作有超时，不留悬挂连接

使用前提：用户以 `--remote-debugging-port=9222` 启动 Chrome。
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

from app.core.errors import ValidationError
from app.core.logging import get_logger

logger = get_logger("cdp")

DEFAULT_ENDPOINT = "http://127.0.0.1:9222"
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _check_endpoint(endpoint: str) -> str:
    """只允许 loopback：调试端口暴露在外网等于把浏览器完全交出去。"""
    p = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
    host = (p.hostname or "").lower()
    if host not in _LOOPBACK:
        raise ValidationError(f"CDP 端点必须是本机回环地址（当前：{host}）")
    return f"{p.scheme or 'http'}://{host}:{p.port or 9222}"


@dataclass
class Tab:
    id: str
    title: str
    url: str
    ws_url: str
    type: str = "page"


async def browser_version(endpoint: str = DEFAULT_ENDPOINT, *, timeout: float = 3.0) -> dict:
    """读取 /json/version：浏览器产品与版本。端口未开时返回空字典。"""
    import httpx

    base = _check_endpoint(endpoint)
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(f"{base}/json/version")
            r.raise_for_status()
            d = r.json()
            return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


async def shutdown_browser(endpoint: str = DEFAULT_ENDPOINT, *, timeout: float = 5.0) -> bool:
    """通过 CDP 优雅关闭浏览器（Browser.close）。

    这是回收「本服务启动、但后端已重启丢失 PID」实例的标准做法：
    不依赖进程句柄，也不需要杀用户自己的浏览器。
    """
    import websockets
    from websockets.exceptions import WebSocketException

    base = _check_endpoint(endpoint)
    ver = await browser_version(base, timeout=timeout)
    ws = str(ver.get("webSocketDebuggerUrl") or "")
    if not ws:
        return False
    try:
        async with websockets.connect(ws, max_size=4 * 1024 * 1024) as sock:
            await sock.send(json.dumps({"id": 1, "method": "Browser.close"}))
            try:
                await asyncio.wait_for(sock.recv(), timeout=2.0)
            except (asyncio.TimeoutError, WebSocketException):
                pass
        return True
    except Exception:  # noqa: BLE001
        return False


async def list_tabs(endpoint: str = DEFAULT_ENDPOINT, *, timeout: float = 5.0) -> list[Tab]:
    """列出浏览器里所有页面标签。"""
    import httpx

    base = _check_endpoint(endpoint)
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(f"{base}/json/list")
            r.raise_for_status()
            items = r.json()
    except Exception as e:  # noqa: BLE001
        raise ValidationError(
            f"无法连接浏览器调试端口 {base}：{type(e).__name__}。"
            "请用 --remote-debugging-port=9222 启动 Chrome/Edge"
        ) from e

    out: list[Tab] = []
    for it in items:
        if it.get("type") not in ("page", "iframe"):
            continue
        ws = it.get("webSocketDebuggerUrl")
        if not ws:
            continue
        out.append(Tab(
            id=it.get("id", ""), title=it.get("title", ""),
            url=it.get("url", ""), ws_url=ws, type=it.get("type", "page"),
        ))
    return out


class CDPSession:
    """单标签的 CDP 会话：发送命令、接收结果、分发事件。

    `call()` 是请求/响应式；但 screencast 帧是**事件**（`Page.screencastFrame`），
    没有对应的 id，因此读循环必须同时处理 `method` 字段，否则推帧永远收不到。
    """

    def __init__(self, ws_url: str, *, timeout: float = 20.0) -> None:
        self.ws_url = ws_url
        self.timeout = timeout
        self._ws = None
        self._seq = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        # 事件名 → 回调列表。回调在读循环里同步调用，必须是非阻塞的：
        # 推帧路径上挂一个 await 会拖慢 ack，帧就会积压。
        self._handlers: dict[str, list[Callable[[dict], None]]] = {}
        # 自动处理 JS 对话框的开关与记录（见 _on_dialog）
        self.dialog_policy = "accept"   # accept | dismiss | ignore
        self.dialogs: list[dict] = []
        self._dialog_armed = False

    async def __aenter__(self) -> "CDPSession":
        import websockets

        self._ws = await websockets.connect(self.ws_url, max_size=32 * 1024 * 1024)
        self._reader = asyncio.create_task(self._read_loop())
        # 必须在任何导航/点击前装好：对话框一弹出来，该标签的 CDP 就整体停摆，
        # 连 `Page.handleJavaScriptDialog` 的响应都发不出去（实测）。
        await self._arm_dialogs()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def _arm_dialogs(self) -> None:
        """开启 Page 域并挂上对话框自动处理。

        为什么必须自动处理：JS 的 `alert/confirm/prompt` 一旦弹出且没人应答，
        这个标签的 CDP 通道会**整体停摆**——后续任何 `Runtime.evaluate`、
        甚至 `Page.enable` 都会超时（本机实测）。表现出来就是"AI 卡死/后续全失败"，
        而原因只是电商站点在下单或清空购物车时弹了个确认框。

        处理策略取 `dialog_policy`：默认 accept（确认），电商流程里
        「确定要删除吗」这类默认按钮通常就是继续动作。
        """
        try:
            await self.call("Page.enable")
        except ValidationError as e:
            # `Page.enable` 失败几乎只有一个原因：**上一次调用结束后**页面才弹出
            # 对话框（例如 setTimeout 里弹的确认框）。这时挂在旧会话上的处理器
            # 已经随会话关闭，没人点掉它，整个标签就卡住了。
            # 只对「超时」做恢复尝试：其它 CDP 错误说明是别的问题，
            # 盲目发应答只会记下一条假的 recovered 记录。
            if "超时" not in str(e):
                self._dialog_armed = False
                return
            # 先盲发一次应答把通道救回来，再重试 enable；救不回来就照常降级。
            if not await self._recover_stalled_dialog():
                self._dialog_armed = False
                return
            try:
                await self.call("Page.enable")
            except ValidationError:
                # 应答没起作用：通道仍然卡住。此前那条「已恢复」的记录不成立，
                # 回滚掉，否则会给 AI 一条假的「对话框已处理」。
                if self.dialogs and self.dialogs[-1].get("type") == "recovered":
                    self.dialogs.pop()
                self._dialog_armed = False
                return
        self.on("Page.javascriptDialogOpening", self._on_dialog_opening)
        self._dialog_armed = True

    async def _recover_stalled_dialog(self) -> bool:
        """尝试应答一个「上一会话遗留」的对话框，成功返回 True。

        `Page.handleJavaScriptDialog` 号称在对话框打开时仍可处理，但实测它的
        响应常常发不回来，所以这里**不等待响应**：发出即返回，由随后的
        `Page.enable` 判定通道是否恢复。
        """
        if self._ws is None or self.dialog_policy == "ignore":
            return False
        self._seq += 1
        try:
            await self._ws.send(json.dumps({
                "id": self._seq,
                "method": "Page.handleJavaScriptDialog",
                "params": {"accept": self.dialog_policy == "accept"},
            }))
        except Exception:  # noqa: BLE001
            return False
        self.dialogs.append({
            "type": "recovered",
            "message": "（在本次操作开始前弹出，已自动应答）",
            "default_prompt": "",
            "policy": self.dialog_policy,
        })
        await asyncio.sleep(0.05)
        return True

    def _on_dialog_opening(self, params: dict) -> None:
        """收到对话框打开事件：记录内容并投递应答命令。

        回调在读循环里同步执行，不能 await；`_seq`/`_ws.send` 是同步的，
        直接发一条**不带等待**的应答即可（应答的返回值我们不关心）。
        """
        info = {
            "type": params.get("type") or "",
            "message": str(params.get("message") or "")[:300],
            "default_prompt": str(params.get("defaultPrompt") or "")[:200],
            "policy": self.dialog_policy,
        }
        self.dialogs.append(info)
        if self.dialog_policy == "ignore" or self._ws is None:
            return
        accept = self.dialog_policy == "accept"
        self._seq += 1
        payload = json.dumps({
            "id": self._seq,
            "method": "Page.handleJavaScriptDialog",
            "params": {"accept": accept},
        })
        try:
            # 事件回调里无法 await，用 task 排进事件循环
            asyncio.ensure_future(self._ws.send(payload))
        except Exception:  # noqa: BLE001
            pass

    def dialog_summary(self) -> list[str]:
        """本次会话遇到过的对话框（给别人话描述用）。"""
        out: list[str] = []
        for d in self.dialogs:
            t = d.get("type") or "dialog"
            msg = (d.get("message") or "").strip()
            out.append(f"{t}: {msg}" if msg else t)
        return out

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                method = msg.get("method")
                if method:
                    # 事件与响应共用一条连接：先按 method 分发，再处理 id
                    for cb in self._handlers.get(method, ()):
                        try:
                            cb(msg.get("params") or {})
                        except Exception:  # noqa: BLE001
                            # 单个回调出错不能拖死整条读循环（否则 ack 停摆、帧卡住）
                            logger.warning("cdp_event_handler_failed", method=method)
                    continue
                mid = msg.get("id")
                if mid is not None and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        fut.set_result(msg)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # 连接断开：把所有等待中的请求置为失败
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(ValidationError("浏览器连接已断开"))
            self._pending.clear()

    def on(self, method: str, cb: Callable[[dict], None]) -> None:
        """订阅 CDP 事件（回调同步执行，禁止阻塞）。"""
        self._handlers.setdefault(method, []).append(cb)

    def off(self, method: str, cb: Callable[[dict], None]) -> None:
        cbs = self._handlers.get(method)
        if not cbs:
            return
        try:
            cbs.remove(cb)
        except ValueError:
            pass
        if not cbs:
            self._handlers.pop(method, None)

    async def call(self, method: str, params: dict | None = None) -> dict:
        """发送一条 CDP 命令，返回 result。"""
        loop = asyncio.get_running_loop()
        self._seq += 1
        mid = self._seq
        fut: asyncio.Future = loop.create_future()
        self._pending[mid] = fut
        await self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        try:
            msg = await asyncio.wait_for(fut, timeout=self.timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise ValidationError(f"浏览器操作超时（{method}）")
        if "error" in msg:
            raise ValidationError(f"CDP 错误：{msg['error'].get('message', '未知')}")
        return msg.get("result") or {}

    # ---- 常用组合 ----

    async def evaluate(self, expression: str, *, await_promise: bool = False) -> object:
        """执行表达式并返回求值结果（仅用于内部固定的读取脚本）。"""
        res = await self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        result = res.get("result") or {}
        if result.get("subtype") == "error":
            raise ValidationError(str(result.get("description") or "脚本执行失败")[:300])
        return result.get("value")

    async def navigate(self, url: str) -> None:
        """跳转到 URL 并等待页面可交互。

        为什么不只 `sleep(0.2)` 就读 readyState：
        实测（example.com，本机）：`Page.navigate` 返回约 1.3 秒后 readyState 仍是
        `loading`，body 只有 156 字。此时抓快照，AI 拿到的是个近乎空白的页面，
        于是「找不到要点的按钮」——这类失败在真机上极难定位，因为页面最终是对的。

        SPA 还有个更隐蔽的问题：`readyState=complete` 之后 React/Vue 才挂载内容。
        所以判据是「DOM 稳定」：同一份关键指标连续两次采样不变才算就绪。
        """
        await self.call("Page.enable")
        await self.call("Page.navigate", {"url": url})
        try:
            await self.wait_ready()
        except ValidationError:
            # 页面可能还在跑长任务：超时不致命，交给上层决定是否重试
            pass

    async def wait_ready(self, *, timeout_ms: int = 15000, settle_ms: int = 250) -> dict:
        """等页面「真的可以操作」：加载完成 + DOM 连续两次采样稳定。

        返回 {ok, waited_ms, ready_state, stable}。ok=False 表示超时，
        调用方应把「页面可能没加载完」明确告诉 AI，而不是让它以为页面就是空的。
        """
        import time as _t

        t0 = _t.monotonic()
        deadline = t0 + max(0.5, timeout_ms / 1000.0)
        probe = (
            "(() => {"
            " const b = document.body;"
            " return {"
            "  state: document.readyState || '',"
            "  nodes: document.getElementsByTagName('*').length || 0,"
            "  text: b ? (b.innerText || '').length : 0,"
            "  url: location.href || ''"
            " };})()"
        )
        prev: tuple | None = None
        last: dict = {}
        while _t.monotonic() < deadline:
            try:
                v = await self.evaluate(probe)
            except ValidationError:
                # 导航中的瞬态错误（执行上下文被销毁）属正常，继续等
                await asyncio.sleep(0.1)
                continue
            if not isinstance(v, dict):
                await asyncio.sleep(0.1)
                continue
            last = v
            # 只算「正文有内容」的稳定：白屏页面（nodes 很少）也算稳定，
            # 否则纯 canvas/SPA 首屏会一直等不到。
            sig = (v.get("state"), v.get("nodes"), v.get("text"), v.get("url"))
            if v.get("state") == "complete" and prev == sig:
                return {
                    "ok": True,
                    "waited_ms": int((_t.monotonic() - t0) * 1000),
                    "ready_state": v.get("state"),
                    "stable": True,
                }
            prev = sig
            await asyncio.sleep(max(0.05, settle_ms / 1000.0))
        return {
            "ok": False,
            "waited_ms": int((_t.monotonic() - t0) * 1000),
            "ready_state": last.get("state") or "",
            "stable": False,
        }

    async def get_text(self, *, max_chars: int = 20000) -> dict:
        """抓取页面正文（去脚本样式），返回 title + text + 链接。

        用「克隆 DOM → 移除干扰节点 → 取 innerText」的路径：
        - 直接对 body 取 innerText 在 headless 下可能漏掉未布局的节点
        - 但纯 textContent 会把 <style> 内容也带出来
        折中：移除 script/style/noscript/iframe 后取 innerText，
        innerText 为空时回退 textContent。
        """
        script = """
        (() => {
          const src = document.body || document.documentElement;
          if (!src) return {title: document.title || '', url: location.href, text: '', links: []};

          // 关键：不能在克隆节点上取 innerText —— 克隆不在文档流中，
          // innerText 会返回空，进而回退到 textContent 把 CSS/JS 源码全带出来。
          // 正确做法是在原 DOM 上临时隐藏干扰元素，取完再还原。
          const noise = Array.from(src.querySelectorAll('script,style,noscript,svg,canvas,iframe,template'));
          const saved = noise.map(el => [el, el.style.display]);
          noise.forEach(el => { el.style.display = 'none'; });
          let text = '';
          try {
            text = (src.innerText || '').trim();
            if (!text) text = (src.textContent || '').trim();
          } finally {
            saved.forEach(([el, prev]) => { el.style.display = prev; });
          }

          const links = Array.from(document.querySelectorAll('a[href]')).slice(0, 200)
            .map(a => ({ text: (a.innerText || '').trim().slice(0, 80), href: a.href }));
          return {
            title: document.title || '',
            url: location.href,
            text: text.replace(/\\n{3,}/g, '\\n\\n').trim(),
            links,
          };
        })()
        """
        v = await self.evaluate(script)
        if not isinstance(v, dict):
            return {"title": "", "url": "", "text": "", "links": []}
        text = str(v.get("text") or "")
        truncated = len(text) > max_chars
        return {
            "title": v.get("title") or "",
            "url": v.get("url") or "",
            "text": text[:max_chars],
            "truncated": truncated,
            "char_count": len(text),
            "links": v.get("links") or [],
        }

    async def click(self, selector: str) -> dict:
        """按 CSS 选择器点击第一个匹配元素。"""
        expr = (
            f"(() => {{ const el = document.querySelector({json.dumps(selector)});"
            " if (!el) return {ok:false, reason:'未找到元素'};"
            " el.scrollIntoView({block:'center'}); el.click();"
            " return {ok:true, tag: el.tagName, text:(el.innerText||'').slice(0,60)}; })()"
        )
        v = await self.evaluate(expr)
        return v if isinstance(v, dict) else {"ok": False, "reason": "执行异常"}

    async def fill(self, selector: str, value: str) -> dict:
        """给输入框赋值并触发 input/change 事件（兼容 React 受控组件）。"""
        expr = (
            f"(() => {{ const el = document.querySelector({json.dumps(selector)});"
            " if (!el) return {ok:false, reason:'未找到元素'};"
            f" const v = {json.dumps(value)};"
            " const setter = Object.getOwnPropertyDescriptor(el.__proto__, 'value')?.set;"
            " if (setter) setter.call(el, v); else el.value = v;"
            " el.dispatchEvent(new Event('input', {bubbles:true}));"
            " el.dispatchEvent(new Event('change', {bubbles:true}));"
            " return {ok:true, tag: el.tagName}; })()"
        )
        v = await self.evaluate(expr)
        return v if isinstance(v, dict) else {"ok": False, "reason": "执行异常"}

    async def screenshot(self, *, quality: int = 70) -> str:
        """截取当前视口，返回 base64 PNG/JPEG。"""
        res = await self.call("Page.captureScreenshot", {
            "format": "jpeg", "quality": max(10, min(quality, 100)),
        })
        return str(res.get("data") or "")

    # ---- 实时画面（screencast）----

    async def screencast_start(
        self,
        *,
        quality: int = 60,
        max_width: int = 1600,
        max_height: int = 1200,
        every_nth_frame: int = 1,
    ) -> None:
        """开始推送画面帧。

        `everyNthFrame=1` 是「能推多快推多快」，再由 ack 背压限速：
        一帧不 ack，浏览器就不再推下一帧 —— 这比固定 fps 更不容易积压。
        """
        await self.call("Page.enable")
        await self.call("Page.startScreencast", {
            "format": "jpeg",
            "quality": max(10, min(quality, 100)),
            "maxWidth": max_width,
            "maxHeight": max_height,
            "everyNthFrame": max(1, every_nth_frame),
        })

    async def screencast_stop(self) -> None:
        try:
            await self.call("Page.stopScreencast")
        except ValidationError:
            # 页面已在跳转/关闭时 stop 可能失败，无副作用
            pass

    async def screencast_ack(self, session_id: int) -> None:
        """确认收到一帧，浏览器才会继续推下一帧。

        用 fire-and-forget：ack 是流的节拍器，等它的响应会白白多一个 RTT，
        高帧率下反而成为瓶颈。失败也无害（浏览器会在超时后继续）。
        """
        self._seq += 1
        try:
            await self._ws.send(json.dumps({
                "id": self._seq,
                "method": "Page.screencastFrameAck",
                "params": {"sessionId": session_id},
            }))
        except Exception:  # noqa: BLE001
            pass

    # ---- 输入注入（面板里直接操作页面）----

    async def mouse(self, kind: str, x: float, y: float, *, button: str = "none",
                    click_count: int = 1, modifiers: int = 0) -> None:
        """`Input.dispatchMouseEvent`（按下/抬起/移动）。

        坐标是**页面 CSS 像素**，换算由调用方按帧尺寸完成（见 browser_live.scale_to_page）。
        滚轮走 `wheel()`，那条路径要带 delta，参数形状不同。
        """
        await self.call("Input.dispatchMouseEvent", {
            "type": kind, "x": float(x), "y": float(y),
            "button": button, "clickCount": int(click_count), "modifiers": int(modifiers),
        })

    async def wheel(self, x: float, y: float, *, delta_y: float, delta_x: float = 0,
                    modifiers: int = 0) -> None:
        await self.call("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": float(x), "y": float(y),
            "deltaX": float(delta_x), "deltaY": float(delta_y),
            "button": "none", "clickCount": 0, "modifiers": int(modifiers),
        })

    async def key(self, kind: str, *, key: str, code: str = "", key_code: int = 0,
                  text: str | None = None, modifiers: int = 0) -> None:
        """`Input.dispatchKeyEvent`；`kind` 取 `keyDown` / `keyUp` / `char`。"""
        params: dict = {
            "type": kind,
            "key": key,
            "code": code or "",
            "windowsVirtualKeyCode": int(key_code),
            "nativeVirtualKeyCode": int(key_code),
            "modifiers": int(modifiers),
        }
        if text is not None:
            params["text"] = text
            params["unmodifiedText"] = text
        await self.call("Input.dispatchKeyEvent", params)

    async def insert_text(self, text: str) -> None:
        """直接插入文本（绕过按键事件，中文/emoji 都可用，也兼容 React 受控组件）。"""
        await self.call("Input.insertText", {"text": text})

    # ---- 懒加载列表 ----

    async def load_metrics(self) -> dict:
        """读取当前加载量（高度 / 节点数 / 滚动位置）。

        用于判断多轮滚动是否真的滚出了新内容 —— 只看一次高度无法区分
        「已经到底」和「加载被限流了」。
        """
        from app.services.browser_ops import load_probe_script

        v = await self.evaluate(load_probe_script())
        return v if isinstance(v, dict) else {}

    async def scroll_load(self, *, rounds: int = 1, dy: float = 0.0,
                          interval_ms: int = 0) -> dict:
        """滚动加载列表：重复滚动直到没有新内容，返回每轮的结果。

        为什么不能只滚一次：电商品列表首屏 12 条、滚到底再追加 12 条，
        只滚一次的话 AI 会得出「只有 24 条」的结论 —— 它不是没找到，是没看到。

        为什么要有「无变化即停」：无限滚动页面上时间戳、轮播动画会让元素数
        持续抖动，只按轮数滚会把每一轮都跑满、白烧几十秒。连续两轮指标不变
        就停 —— 允许一轮抖动（懒加载常有一轮延迟）。

        滚动落点优先找可滚容器：很多列表放在 `overflow:auto` 的 div 里，
        对 window 发滚轮**完全没反应**（页面高度不变），现象就是
        「AI 滚了，但一条都没加载」。
        """
        from app.services.browser_ops import MAX_SCROLL_ROUNDS, SCROLL_TARGET_SCRIPT

        rounds = max(1, min(int(rounds or 1), MAX_SCROLL_ROUNDS))
        step_ms = max(0, min(int(interval_ms or 0), 10000))
        # 默认滚动量：一屏的 80%。留一点重叠，避免正好漏掉跨屏边界的那一条。
        metrics0 = await self.load_metrics()
        vh = float(metrics0.get("vh") or 800)
        delta = float(dy) if dy else vh * 0.8

        history: list[dict] = []
        prev_sig = metrics0.get("sig")
        # 用集合记账而不是「跟上一轮比」：懒加载的追加在浏览器里是异步的，
        # 两轮读到同一个值不代表没有更多内容（可能只是这一轮还没回来）。
        # 只有「见到过的所有指标都重复」才算真的没有新内容。
        seen_sigs = {prev_sig}
        idle = 0
        reason = "max_rounds"
        for i in range(rounds):
            target = await self.evaluate(SCROLL_TARGET_SCRIPT)
            if not isinstance(target, dict):
                target = {"x": vh / 2, "y": vh / 2}
            await self.wheel(float(target.get("x") or 0), float(target.get("y") or 0),
                             delta_y=delta)
            # 懒加载的追加是异步的：滚完立刻读数会看到「没变化」而提前收工
            await asyncio.sleep(max(0.12, step_ms / 1000.0 if step_ms else 0.35))

            m = await self.load_metrics()
            sig = m.get("sig")
            grew = sig not in seen_sigs
            seen_sigs.add(sig)
            history.append({
                "round": i + 1,
                "height": m.get("h"),
                "nodes": m.get("n"),
                "at_bottom": bool(m.get("atBottom")),
                "grew": grew,
                "target": target.get("target") or "window",
            })
            if grew:
                idle = 0
                continue
            idle += 1
            # 到底且没新内容：再滚也不会有东西，立刻收工别空转
            if m.get("atBottom") and idle >= 1:
                reason = "bottom"
                break
            if idle >= 2:
                reason = "no_new_content"
                break

        final = history[-1] if history else {}
        return {
            "rounds_done": len(history),
            "reason": reason,
            "height_before": metrics0.get("h"),
            "height_after": final.get("height"),
            "nodes_before": metrics0.get("n"),
            "nodes_after": final.get("nodes"),
            "at_bottom": bool(final.get("at_bottom")),
            "history": history,
        }

    # ---- 元素级自动化 ----
    #
    # 下面这组方法服务的对象是 AI：它看不到 DOM，只拿得到浏览器快照里的编号。
    # 所以这里不做「猜选择器」，而是**先定位到元素、再按几何位置点它**——
    # 用真实鼠标事件点，能被页面的 isTrusted 校验接受；el.click() 不行。

    async def snapshot(self, *, max_elements: int = 150, token: str = "") -> dict:
        """抓取页面结构 + 可交互元素编号（含坐标）。供 AI 决定下一步点哪。

        `token` 会写进每个元素的 `data-rea-snap` 属性，后续动作据此精确回指。
        """
        from app.services.browser_ops import MAX_ELEMENTS, snapshot_script

        cap = max(10, min(int(max_elements or MAX_ELEMENTS), MAX_ELEMENTS))
        v = await self.evaluate(snapshot_script(cap, token=token))
        return v if isinstance(v, dict) else {}

    async def rect_of(self, selector: str) -> dict | None:
        """查选择器命中的元素当前矩形（**顶层视口坐标**）；不可见时返回 None。

        滚动之后坐标会变，所以每次点击前都要重新取，不能复用快照里的旧坐标。

        会跨同源 iframe 查找：`document.querySelector` 只看顶层文档，
        而支付/登录框里的按钮全在子文档里 —— 只看顶层会得到"元素不存在"，
        进而让上层走上"换选择器"的错路。
        """
        from app.services.browser_ops import frame_script

        body = (
            "const hit = __reaFind(%s);"
            " if (!hit) return null;"
            " const r = __reaRect(hit);"
            " if (r.w < 1 || r.h < 1) return null;"
            " return {x:r.x, y:r.y, w:r.w, h:r.h};" % json.dumps(selector)
        )
        v = await self.evaluate(frame_script(body))
        return v if isinstance(v, dict) else None

    async def scroll_into_view(self, selector: str) -> bool:
        """把选择器命中的元素滚到视口中央。返回是否找到元素。

        嵌套 iframe 要**由外向内逐层滚**：只滚里层文档的滚动容器时，
        iframe 本身可能还在父页面视口之外，坐标落在窗口外，点击直接失效。
        """
        from app.services.browser_ops import frame_script

        body = (
            "const hit = __reaFind(%s);"
            " if (!hit) return false;"
            " try {"
            "   for (const f of hit.chain) f.scrollIntoView({block:'center', inline:'nearest'});"
            "   hit.el.scrollIntoView({block:'center', inline:'nearest'});"
            " } catch (e) {}"
            " return true;" % json.dumps(selector)
        )
        return await self.evaluate(frame_script(body)) is True

    async def focus_and_select(self, selector: str) -> bool:
        """聚焦并全选内容：随后 insertText 会**替换**原值而不是追加。"""
        from app.services.browser_ops import frame_script

        body = (
            "const hit = __reaFind(%s);"
            " if (!hit) return false;"
            " const el = hit.el;"
            " try { el.focus(); } catch (e) {}"
            " try { if (el.select) el.select();"
            " else if (el.setSelectionRange) el.setSelectionRange(0, (el.value||'').length);"
            " } catch (e) {}"
            " return true;" % json.dumps(selector)
        )
        return await self.evaluate(frame_script(body)) is True

    async def click_at(self, x: float, y: float, *, click_count: int = 1,
                       button: str = "left") -> None:
        """在页面坐标处发真实鼠标点击（按下 + 抬起）。

        与 `click(selector)` 的区别：那个走 `el.click()`，不产生 mousedown/mouseup，
        对很多前端框架（监听 pointerdown、判断 event.isTrusted）完全无效。
        """
        await self.mouse("mouseMoved", x, y)
        await self.mouse("mousePressed", x, y, button=button, click_count=click_count)
        await self.mouse("mouseReleased", x, y, button=button, click_count=click_count)

    async def hover_at(self, x: float, y: float) -> None:
        await self.mouse("mouseMoved", x, y)

    async def press_key(self, key: str, *, text: str | None = None) -> None:
        """按键（keyDown → 可选 char → keyUp），用于 Enter/Tab/Escape 等。"""
        from app.services.browser_live import map_key

        info = map_key(key)
        if info is None:
            # 未知键没有可信 keyCode，只插入文本，不瞎猜
            if text:
                await self.insert_text(text)
            return
        await self.call("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": info.key, "code": info.code,
            "windowsVirtualKeyCode": info.key_code, "nativeVirtualKeyCode": info.key_code,
        })
        if text:
            await self.call("Input.dispatchKeyEvent", {
                "type": "char", "key": info.key, "code": info.code,
                "windowsVirtualKeyCode": info.key_code, "nativeVirtualKeyCode": info.key_code,
                "text": text, "unmodifiedText": text,
            })
        await self.call("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": info.key, "code": info.code,
            "windowsVirtualKeyCode": info.key_code, "nativeVirtualKeyCode": info.key_code,
        })

    async def select_option(self, selector: str, value: str) -> dict:
        """设置 <select> 的值并触发事件（兼容 React 受控组件）。"""
        from app.services.browser_ops import frame_script

        body = (
            "const hit = __reaFind(%s);"
            " if (!hit) return {ok:false, reason:'未找到元素'};"
            " const el = hit.el;"
            " if (el.tagName.toLowerCase() !== 'select')"
            "   return {ok:false, reason:'目标不是 select'};"
            " const v = %s;"
            " const opt = Array.from(el.options).find(o => o.value === v || (o.text||'').trim() === v);"
            " if (!opt) {"
            "   const opts = Array.from(el.options).slice(0,10).map(o => o.value);"
            "   return {ok:false, reason:'没有匹配的选项', options: opts};"
            " }"
            # 必须用元素**自己文档**的构造器：iframe 里的 Event 由子文档创建，
            # 拿顶层 window 的 Event 派发到子文档元素上，部分浏览器会静默丢弃。
            " const W = el.ownerDocument.defaultView || window;"
            " const proto = (W.HTMLSelectElement || HTMLSelectElement).prototype;"
            " const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;"
            " if (setter) setter.call(el, opt.value); else el.value = opt.value;"
            " el.dispatchEvent(new W.Event('input', {bubbles:true}));"
            " el.dispatchEvent(new W.Event('change', {bubbles:true}));"
            " return {ok:true, value: opt.value, text: (opt.text||'').trim()};"
            % (json.dumps(selector), json.dumps(value))
        )
        v = await self.evaluate(frame_script(body))
        return v if isinstance(v, dict) else {"ok": False, "reason": "执行异常"}

    async def set_checked(self, selector: str, checked: bool) -> dict:
        """勾选/取消勾选，只在状态不符时才点（重复勾选会翻转成反效果）。"""
        from app.services.browser_ops import frame_script

        body = (
            "const hit = __reaFind(%s);"
            " if (!hit) return {ok:false, reason:'未找到元素'};"
            " const el = hit.el;"
            " const want = %s;"
            " const isChecked = (el.type === 'checkbox' || el.type === 'radio')"
            "   ? !!el.checked"
            "   : el.getAttribute('aria-checked') === 'true';"
            " if (isChecked === want) return {ok:true, already: true, checked: want};"
            " el.click();"
            " const now = (el.type === 'checkbox' || el.type === 'radio')"
            "   ? !!el.checked"
            "   : el.getAttribute('aria-checked') === 'true';"
            " return {ok: now === want, checked: now};"
            % (json.dumps(selector), "true" if checked else "false")
        )
        v = await self.evaluate(frame_script(body))
        return v if isinstance(v, dict) else {"ok": False, "reason": "执行异常"}

    async def wait_until(self, script: str, *, timeout_ms: int = 10000,
                         interval_ms: int = 120) -> dict:
        """轮询布尔脚本直到为真；返回 {ok, waited_ms, error?}。

        为什么在浏览器侧轮询判据、在这里只等结果：
        判据（可见性/文本出现）必须用真实布局计算，Python 侧拿不到；
        而轮询节奏放在这里才能被超时精确掐断。
        """
        import time as _t

        t0 = _t.monotonic()
        deadline = t0 + max(0.2, timeout_ms / 1000.0)
        step = max(0.05, interval_ms / 1000.0)
        last_err = ""
        while _t.monotonic() < deadline:
            try:
                ok = await self.evaluate(script)
                if ok is True:
                    return {"ok": True, "waited_ms": int((_t.monotonic() - t0) * 1000)}
            except ValidationError as e:
                # 页面正在导航时 evaluate 会失败，属正常，继续等
                last_err = str(e)[:200]
            await asyncio.sleep(step)
        return {
            "ok": False,
            "waited_ms": int((_t.monotonic() - t0) * 1000),
            "error": last_err or "等待超时",
        }

    async def page_text(self) -> dict:
        """读当前可见正文（与快照同源的轻量版，供 assert_text 用）。"""
        v = await self.evaluate(
            "(() => { const b = document.body;"
            " return {title: document.title||'', url: location.href,"
            " text: (b ? (b.innerText||'') : '')}; })()"
        )
        return v if isinstance(v, dict) else {"title": "", "url": "", "text": ""}

    async def go_back(self) -> None:
        """后退一页。走浏览历史而不是 `history.back()`：
        面板与自动化共用同一个 CDP target，历史条目才是双方一致的事实来源。"""
        hist = await self.call("Page.getNavigationHistory")
        idx = int(hist.get("currentIndex") or 0)
        entries = hist.get("entries") or []
        if idx <= 0 or idx - 1 >= len(entries):
            raise ValidationError("没有可后退的历史记录")
        await self.call("Page.navigateToHistoryEntry", {
            "entryId": entries[idx - 1].get("id"),
        })
        # 后退同样是导航：不等待就抓快照会拿到上一页残留的 DOM
        try:
            await self.wait_ready()
        except ValidationError:
            pass

    async def reload(self) -> None:
        await self.call("Page.reload", {"ignoreCache": False})
        # 刷新后立即操作会点到正在卸载的旧节点，必须等新页面就绪
        try:
            await self.wait_ready()
        except ValidationError:
            pass


