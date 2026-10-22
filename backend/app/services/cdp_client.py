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
    """单标签的 CDP 会话：发送命令、接收结果。"""

    def __init__(self, ws_url: str, *, timeout: float = 20.0) -> None:
        self.ws_url = ws_url
        self.timeout = timeout
        self._ws = None
        self._seq = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None

    async def __aenter__(self) -> "CDPSession":
        import websockets

        self._ws = await websockets.connect(self.ws_url, max_size=32 * 1024 * 1024)
        self._reader = asyncio.create_task(self._read_loop())
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

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
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
        await self.call("Page.enable")
        await self.call("Page.navigate", {"url": url})
        # 等待 load 事件；超时不致命（页面可能只是慢）
        try:
            await asyncio.sleep(0.2)
            await self.call("Runtime.evaluate", {
                "expression": "document.readyState",
                "returnByValue": True,
            })
        except Exception:  # noqa: BLE001
            pass

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
