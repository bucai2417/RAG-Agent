"""浏览器实时画面（CDP screencast）接口。

用途：把 `browser_manager` 拉起的那个浏览器**镜像进前端面板**，
让用户能看到 AI 正在操作什么，也能自己直接接管（点击/滚动/输入/登录）。

设计要点（勿凭直觉改动）：

1. **一条 WS 对应一条 CDP 连接**，不每个动作重连。
   重连会丢 screencast 状态、丢页面加载中的事件；用户拖拽时更是每秒多次。
   切标签才换连接（CDP 的 screencast 是 per-target 的）。

2. **帧走 WebSocket 二进制帧**，不走 JSON base64。
   base64 会多 33% 体积并要两次解码；二进制帧前端直接 `createImageBitmap`。
   控制类消息（标签列表、当前 URL、错误）仍走 JSON 文本帧，用 `type` 区分。

3. **ack 背压是节拍器**。收到一帧先 ack 再推给前端：
   CDP 只有在收到 ack 后才继续推下一帧，天然不会堆积。
   再叠一层 `FrameThrottle` 丢弃迟到帧（网络抖时它们已过时）。

4. **坐标用归一化**。前端报指针在画面中的相对位置（0~1），
   这里乘页面 CSS 视口尺寸。理由见 `browser_live.scale_normalized`。

5. **动作白名单，不透传任意 CDP**。
   否则这个 WS 等于给前端一个能 `Runtime.evaluate` 的后门。
   所有动作经 `validate_message` 校验后才落到固定方法上。
"""
from __future__ import annotations

import asyncio
import base64
import json
import time

import jwt
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.models import User
from app.middleware.auth_dep import user_has_permission
from app.services import browser_live as L
from app.services import browser_manager as BM
from app.services import cdp_client as C

logger = get_logger("browser_live_api")

router = APIRouter(prefix="/browser", tags=["browser"])

# 页面视口尺寸的缓存时长：每次点击都问一次 getLayoutMetrics 是浪费，
# 窗口拖动/缩放后 1 秒内自愈即可
_VIEWPORT_TTL = 1.0


async def _ws_user(token: str) -> User | None:
    """WS 鉴权：与 chat_room 一致，token 走 query（浏览器原生 WS 不能带 header）。

    逐项校验 token 版本、账号状态、审核状态 —— 少一项就等于让被踢下线的
    旧 token 继续持有浏览器控制权。
    """
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except jwt.PyJWTError:
        return None
    if payload.get("type") != "access":
        return None
    async with AsyncSessionLocal() as db:
        user = await db.get(User, int(payload.get("sub", 0) or 0))
        if not user or user.status != "active":
            return None
        if int(payload.get("tv", 0)) != int(getattr(user, "token_version", 0) or 0):
            return None
        if getattr(user, "approval_status", "approved") != "approved":
            return None
        # 复用与 HTTP 接口同一个权限码，避免"能看不能开"的双标
        if not await user_has_permission(db, user, "tool:invoke"):
            return None
        return user


class _Live:
    """单个 WS 的画布会话：持有当前标签的 CDP 连接与帧循环。"""

    def __init__(self, ws: WebSocket, port: int) -> None:
        self.ws = ws
        self.port = port
        self.sess: C.CDPSession | None = None
        self.tab: C.Tab | None = None
        self.throttle = L.FrameThrottle()
        self.quality = 60
        self._viewport: tuple[float, float] | None = None
        self._viewport_at = 0.0
        # 正在发送的那一帧。始终只保留一帧在途：新一轮帧到来时若上一帧还没发完，
        # 说明客户端解码跟不上，丢掉旧帧比排队更不容易累积延迟。
        self._latest_frame: str | None = None
        self._inflight: asyncio.Task | None = None
        self._closed = False
        # 帧推送串行化：并发 send 同一 WS 会踩到"另一个发送正在进行"
        self._send_lock = asyncio.Lock()

    # ---- 基础收发 ----

    async def send_json(self, payload: dict) -> None:
        if self._closed:
            return
        async with self._send_lock:
            try:
                await self.ws.send_text(json.dumps(payload, ensure_ascii=False, default=str))
            except Exception:  # noqa: BLE001
                self._closed = True

    async def send_frame(self, data: bytes) -> None:
        if self._closed:
            return
        async with self._send_lock:
            try:
                await self.ws.send_bytes(data)
            except Exception:  # noqa: BLE001
                self._closed = True

    # ---- 视口 ----

    async def viewport(self) -> tuple[float, float]:
        now = time.monotonic()
        if self._viewport and (now - self._viewport_at) < _VIEWPORT_TTL:
            return self._viewport
        if self.sess is None:
            return 1280.0, 800.0
        try:
            m = await self.sess.call("Page.getLayoutMetrics")
            self._viewport = L.viewport_of(m)
            self._viewport_at = now
        except ValidationError:
            # 页面正在导航时取不到，沿用上一次的值（没有就用默认）
            if not self._viewport:
                self._viewport = (1280.0, 800.0)
            self._viewport_at = now
        return self._viewport

    def invalidate_viewport(self) -> None:
        self._viewport = None

    # ---- 连接 / 切标签 ----

    async def close_session(self) -> None:
        sess, self.sess = self.sess, None
        if self._inflight:
            self._inflight.cancel()
            self._inflight = None
        if sess is None:
            return
        try:
            await sess.screencast_stop()
        except Exception:  # noqa: BLE001
            pass
        await sess.__aexit__(None, None, None)

    async def attach(self, tab: C.Tab) -> None:
        """连到指定标签并开始推帧。切标签时先干净地拆掉旧连接。

        只接受顶层标签：跨进程 iframe 在 `/json/list` 里同样是 page 且带 ws
        地址，但 screencast 之类的命令在子帧上会被浏览器拒绝
        （`Command can only be executed on top-level targets`）。
        子帧 id 由 `_do_switch` 先归到父标签再进来。
        """
        if tab.parent_id or tab.type != "page":
            raise ValidationError("只能显示顶层标签页；iframe 请切到它所在的标签")

        await self.close_session()
        self.tab = tab
        self.invalidate_viewport()
        self.throttle.reset()

        sess = C.CDPSession(tab.ws_url)
        try:
            await sess.__aenter__()
        except Exception as e:  # noqa: BLE001
            # 连不上就干净退出：此时 self.sess 还没赋值，不能把半开对象留在字段上
            raise ValidationError(f"无法连接该标签页：{str(e)[:200]}") from e
        self.sess = sess
        sess.on("Page.screencastFrame", self._on_frame)
        # 页面跳转/重载后视口可能变（响应式布局），清缓存重新量
        sess.on("Page.frameNavigated", self._on_navigated)
        sess.on("Page.loadEventFired", self._on_loaded)
        try:
            await sess.call("Page.enable")
            await sess.screencast_start(quality=self.quality)
        except ValidationError as e:
            if self.sess is sess:
                self.sess = None
            try:
                await sess.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            raise ValidationError(f"无法开始实时画面：{e}") from e
        except Exception as e:  # noqa: BLE001
            # 连接中途断开、协议库异常等：同样不能留下半开会话，否则下一次
            # attach 会先对着一个死连接发 stopScreencast（还要等超时）
            if self.sess is sess:
                self.sess = None
            try:
                await sess.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            raise ValidationError(f"无法开始实时画面：{str(e)[:200]}") from e

        await self.send_json({
            "type": "attached", "tab_id": tab.id, "url": tab.url, "title": tab.title,
        })

    def _on_navigated(self, params: dict) -> None:
        """主框架跳转：地址栏要跟着变，视口缓存作废。

        只关心主框架（没有 parentId）。iframe 跳转同样会触发本事件，
        跟着改地址栏会把用户显示的地址换成广告框的 URL。
        """
        self.invalidate_viewport()
        frame = params.get("frame") or {}
        if frame.get("parentId"):
            return
        url = frame.get("url")
        if isinstance(url, str) and url:
            asyncio.ensure_future(self.send_json({"type": "navigated", "url": url}))

    def _on_loaded(self, _params: dict) -> None:
        """加载完成：标题这时才准，推一次给标签栏。"""
        self.invalidate_viewport()
        asyncio.ensure_future(self._push_title())

    async def _push_title(self) -> None:
        sess = self.sess
        if sess is None or self._closed:
            return
        try:
            v = await sess.evaluate(
                "({title: document.title || '', url: location.href})"
            )
        except Exception:  # noqa: BLE001
            return
        if isinstance(v, dict):
            await self.send_json({
                "type": "title",
                "title": str(v.get("title") or "")[:200],
                "url": str(v.get("url") or "")[:500],
            })

    def _on_frame(self, params: dict) -> None:
        """screencastFrame 回调（读循环内同步执行，不能 await）。

        ack 必须立刻发出，否则浏览器停推下一帧 —— 所以这里只做转移：
        ack 与推帧都丢到任务里，回调本身不阻塞。
        """
        if self._closed:
            return
        sid = int(params.get("sessionId") or 0)

        # 无论这一帧是否会被丢弃都要 ack：这是流继续的前提
        if self.sess is not None and sid:
            asyncio.ensure_future(self.sess.screencast_ack(sid))

        data_b64 = params.get("data")
        if not isinstance(data_b64, str) or not data_b64:
            return
        if not self.throttle.should_send(time.monotonic() * 1000):
            return

        # 只留最新一帧：上一帧还在发就往覆盖掉，客户端慢时不会越积越旧
        self._latest_frame = data_b64
        if self._inflight is None or self._inflight.done():
            self._inflight = asyncio.ensure_future(self._pump_frame())

    async def _pump_frame(self) -> None:
        """把最新帧发出去；发送期间到达的新帧会被本循环接着发。"""
        while not self._closed:
            data_b64, self._latest_frame = self._latest_frame, None
            if not data_b64:
                return
            try:
                raw = base64.b64decode(data_b64)
            except Exception:  # noqa: BLE001
                continue
            await self.send_frame(raw)

    # ---- 动作 ----

    async def handle(self, raw: str) -> None:
        try:
            msg = L.validate_message(json.loads(raw))
        except json.JSONDecodeError:
            await self.send_json({"type": "error", "message": "消息不是合法 JSON"})
            return
        except L.BadMessage as e:
            await self.send_json({"type": "error", "message": str(e)})
            return

        t = msg["type"]
        if t == "ping":
            await self.send_json({"type": "pong"})
            return
        if t == "tabs":
            # 只刷新列表，不动当前连接：用户在看某个页面时不该被切走
            top = _top_level(await self._tabs())
            await self.send_json({
                "type": "tabs", "tabs": _tabs_payload(top),
                "active": self.tab.id if self.tab else None,
            })
            return
        if t == "attach":
            await self._safe_attach(self._do_attach)
            return
        if t == "switch_tab":
            await self._safe_attach(lambda: self._do_switch(msg["tab_id"]))
            return

        # 其余动作都要求已连上标签
        sess = self.sess
        if sess is None:
            await self.send_json({"type": "error", "message": "尚未连接任何标签页"})
            return

        try:
            if t == "mouse":
                await self._do_mouse(msg)
            elif t == "wheel":
                await self._do_wheel(msg)
            elif t == "key":
                await self._do_key(msg)
            elif t == "text":
                await sess.insert_text(msg["text"])
            elif t == "navigate":
                await self._do_navigate(msg["url"])
            elif t == "reload":
                await sess.call("Page.reload", {"ignoreCache": False})
                self.invalidate_viewport()
            elif t == "quality":
                self.quality = msg["quality"]
                # 改质量要重启 screencast，CDP 不支持中途改参数
                await sess.screencast_stop()
                await sess.screencast_start(quality=self.quality)
            elif t == "resize":
                await self._do_resize(msg["width"], msg["height"])
        except ValidationError as e:
            await self.send_json({"type": "error", "message": str(e)[:300]})
        except Exception as e:  # noqa: BLE001
            logger.warning("live_action_failed", action=t, error=str(e)[:200])
            await self.send_json({"type": "error", "message": f"操作失败：{str(e)[:200]}"})

    async def _safe_attach(self, fn) -> None:
        """attach/switch 的失败必须回给前端，不能掀掉整条 WS。

        之前 attach 抛出的 ValidationError 会一路冒到 browser_live 的
        receive 循环外，被外层 except 记成 live_ws_error 并直接关连接，
        前端只能看到"断开"，拿不到原因，也无法重选别的标签。
        """
        try:
            await fn()
        except ValidationError as e:
            await self.send_json({"type": "error", "message": str(e)[:300]})
        except Exception as e:  # noqa: BLE001
            logger.warning("live_attach_failed", error=str(e)[:200])
            await self.send_json({"type": "error", "message": f"无法切换标签页：{str(e)[:200]}"})

    async def _do_attach(self) -> None:
        tabs = await self._tabs()
        top = _top_level(tabs)
        if not top:
            await self.send_json({"type": "error", "message": "浏览器里没有可显示的标签页"})
            return
        await self.send_json({"type": "tabs", "tabs": _tabs_payload(top), "active": top[0].id})
        await self.attach(top[0])

    def _endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _tabs(self) -> list[C.Tab]:
        """含跨进程子帧的完整 target 列表。

        为什么要带子帧：用户在前端点到的 id 可能是 `Page.getFrameTree` 里
        的某个 frame（或收到过时的 iframe 条目），只在顶层列表里找会误报
        「该标签页已关闭」。有了完整列表才能把它归回父标签。
        """
        return await C.list_tabs(self._endpoint(), include_frames=True)

    async def _do_switch(self, tab_id: str) -> None:
        tabs = await self._tabs()
        top = _top_level(tabs)
        target = next((t for t in tabs if t.id == tab_id), None)
        if target is None:
            await self.send_json({"type": "error", "message": "该标签页已关闭"})
            await self.send_json({"type": "tabs", "tabs": _tabs_payload(top)})
            return
        if target.parent_id:
            # 子帧不能直接接 screencast（浏览器会拒绝）：显示它所在的顶层标签
            parent = C.top_level_of(tabs, tab_id)
            if parent is None:
                await self.send_json({"type": "error", "message": "该 iframe 所在的标签页已关闭"})
                await self.send_json({"type": "tabs", "tabs": _tabs_payload(top)})
                return
            target = parent
        await self.attach(target)

    async def _do_mouse(self, msg: dict) -> None:
        assert self.sess is not None
        w, h = await self.viewport()
        x, y = L.scale_normalized(msg["nx"], msg["ny"], page_w=w, page_h=h)
        await self.sess.mouse(
            msg["event"], x, y,
            button=msg["button"], click_count=msg["click_count"],
            modifiers=msg["modifiers"],
        )

    async def _do_wheel(self, msg: dict) -> None:
        assert self.sess is not None
        w, h = await self.viewport()
        x, y = L.scale_normalized(msg["nx"], msg["ny"], page_w=w, page_h=h)
        await self.sess.wheel(x, y, delta_x=msg["delta_x"], delta_y=msg["delta_y"],
                             modifiers=msg["modifiers"])

    async def _do_key(self, msg: dict) -> None:
        """三种情况分开处理，混在一起会丢输入：

        - **修饰键**（Shift/Ctrl/…）：按下与抬起是**两次独立到达**，只发
          rawKeyDown/rawKeyUp。若按普通键走"按下+抬起"三元组，页面在整个
          按住期间都收不到修饰状态，`Ctrl+A` 会退化成单独的 'a'。
        - **可打印字符**：keyDown → char → keyUp。只发 keyDown 时很多输入框
          收不到字符（char 才是真正产生文本的事件）。
        - **控制键**（Enter/Tab/方向键…）：keyDown + keyUp，**不带** char，
          否则会往输入框里塞一个回车符。
        """
        assert self.sess is not None
        mods = msg["modifiers"]

        # 修饰键自身：只发 raw 事件（不改文本、不触发快捷键）
        cmd = L.command_for(msg["key"], msg["mod"])
        if cmd is not None:
            info = L.map_key(msg["key"])
            if info is not None:
                await self.sess.key(cmd, key=info.key, code=info.code,
                                    key_code=info.key_code, modifiers=mods)
            return

        info = L.map_key(msg["key"])
        if info is None:
            # 未知键：没有可信 keyCode，只能按文本插入，不瞎猜
            if msg.get("text"):
                await self.sess.insert_text(msg["text"])
            return

        text = msg.get("text")
        await self.sess.key("keyDown", key=info.key, code=info.code,
                            key_code=info.key_code, modifiers=mods)
        # 带 Ctrl/Meta/Alt 时是快捷键而非输入，不能补 char
        if text and not (mods & (L.MOD_CTRL | L.MOD_META | L.MOD_ALT)):
            await self.sess.key("char", key=info.key, code=info.code,
                                key_code=info.key_code, text=text, modifiers=mods)
        await self.sess.key("keyUp", key=info.key, code=info.code,
                            key_code=info.key_code, modifiers=mods)

    async def _do_navigate(self, url: str) -> None:
        assert self.sess is not None
        # 只允许 http(s)：面板的地址栏不该成为 file:// 或 chrome:// 的入口
        if not url.lower().startswith(("http://", "https://")):
            await self.send_json({"type": "error", "message": "只支持 http/https 地址"})
            return
        await self.sess.call("Page.navigate", {"url": url})
        self.invalidate_viewport()

    async def _do_resize(self, width: int, height: int) -> None:
        """用 `Emulation.setDeviceMetricsOverride` 改视口。

        不改真实窗口尺寸：那个浏览器窗口可能正被用户拖拽/最大化，
        我们只影响"这一路自动化看到多大"，对用户的肉眼窗口是侵入更小的做法。
        """
        assert self.sess is not None
        await self.sess.call("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height, "deviceScaleFactor": 1,
            "mobile": False,
        })
        self.invalidate_viewport()


def _top_level(tabs: list[C.Tab]) -> list[C.Tab]:
    """前端标签栏只该看到顶层标签：子帧不是能独立展示/接管的"标签"。"""
    return [t for t in tabs if not t.parent_id and t.type == "page"]


def _tabs_payload(tabs: list[C.Tab]) -> list[dict]:
    """标签页只回必要字段：标题可能很长，全量回会把控制消息撑大。"""
    return [
        {"id": t.id, "title": (t.title or "")[:80], "url": (t.url or "")[:300]}
        for t in tabs
    ]


@router.websocket("/live")
async def browser_live(ws: WebSocket, token: str = Query("")) -> None:
    """实时画面 + 接管。协议见模块文档。"""
    user = await _ws_user(token)
    if not user:
        # 必须先 accept 再发关闭码：Starlette 在 accept() 之前调用 close()
        # 会直接以 HTTP 403 拒绝握手，前端拿不到 4401，也就无法区分
        # 「登录失效」和「临时网络故障」—— 结果是对着过期 token 无限重连。
        await ws.accept()
        await ws.close(code=4401, reason="unauthorized")
        return

    await ws.accept()

    ok, note = await asyncio.to_thread(BM.ensure_running, auto_launch=True)
    if not ok:
        await ws.send_text(json.dumps({
            "type": "unavailable",
            "message": note or "浏览器未就绪",
            "no_browser": not BM.detect_browsers(),
        }, ensure_ascii=False))
        await ws.close(code=1011)
        return

    live = _Live(ws, BM.DEFAULT_PORT)
    logger.info("live_connected", user_id=user.id, port=live.port)
    try:
        while True:
            raw = await ws.receive_text()
            await live.handle(raw)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("live_ws_error", user_id=user.id)
    finally:
        live._closed = True
        await live.close_session()
        logger.info("live_closed", user_id=user.id)
