"""浏览器控制工具：连接本机已登录的 Chrome/Edge，抓取与操作网页。

适用场景：
- 需要登录态才能看到的页面（内部系统、后台、订阅内容）
- 单页应用（SPA）渲染后的内容，普通 HTTP 抓取拿不到

前提：用户以 `--remote-debugging-port=9222` 启动浏览器。
未开启时给出明确指引，而不是静默失败。

## 为什么要有 browser_snapshot

AI 看不到页面，只能读文本。让它自己写 `button.submit` 这种选择器，
命中率低，失败时也无从修正。快照把可交互元素**编号列出**，AI 用 `[3]` 回指。

## 编号怎么落到真实元素（关键，勿凭直觉改）

快照时给每个元素打 `data-rea-snap=<token>` + `data-rea-idx=<n>` 两个属性，
动作执行时按这对属性精确查回同一个元素 —— 元素身份由浏览器自己维护，
不怕重名、不怕文案被页面改写。

只有一种情况标记会失效：页面在两次调用间把 DOM 整体重建（标记随之丢失）。
此时退回 `browser_ops.match_candidates`：按「角色 + 名称 + 位置」重新认人。
**不要**退化成"按名称拼选择器"——列表页里同名按钮常有几十个，必然点错。
"""
from __future__ import annotations

import asyncio
import base64
import uuid
import time as _t

from app.agents.tools.base import ToolContext, ToolResult, WriteToolMixin
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.services import browser_manager as BM
from app.services import browser_ops as O
from app.services import cdp_client as C

logger = get_logger("browser_tools")

_START_HINT = (
    "可在「系统设置 → 浏览器」一键启动（会自动检测本机 Chrome/Edge 或已安装的内置运行时）；"
    "本机没装浏览器时，可在「工具 → 可选组件」安装内置浏览器运行时。"
    "也可手动以 --remote-debugging-port=9222 启动浏览器。"
)

# ---- 最近一次快照缓存 ----
# AI 看到快照 `[3]` 后才会发起动作，两次工具调用之间进程可能已重算页面。
# 缓存的是「AI 当时看到的那份快照」，动作执行时用它把 ref 翻译成 token+idx。
# 只留最近一次（按标签），并带 TTL：过期快照比没有更危险（会指到旧元素）。
_SNAP_TTL = 180.0
_snap_cache: dict[str, tuple[float, O.PageSnapshot]] = {}


def _cache_put(tab_id: str, snap: O.PageSnapshot) -> None:
    _snap_cache[tab_id] = (_t.monotonic(), snap)
    # 同一浏览器开过的标签不会很多，超量时丢最旧的
    if len(_snap_cache) > 32:
        oldest = min(_snap_cache.items(), key=lambda kv: kv[1][0])[0]
        _snap_cache.pop(oldest, None)


def _cache_get(tab_id: str) -> O.PageSnapshot | None:
    item = _snap_cache.get(tab_id)
    if not item:
        return None
    at, snap = item
    if (_t.monotonic() - at) > _SNAP_TTL:
        _snap_cache.pop(tab_id, None)
        return None
    return snap


async def _ensure_browser() -> tuple[str | None, dict | None]:
    """确保调试端口可用；需要时自动拉起。

    返回 (错误信息, 结构化提示)。第二个值只在「本机一个浏览器都没装」时非空，
    供前端渲染成带「去安装」按钮的卡片 —— 纯文本指引在聊天里点不动，用户还得自己找菜单。

    `ensure_running` 内部是同步的（要起子进程、轮询端口，最长等 15 秒），
    直接 await 会卡住整个事件循环、连带其它会话一起卡死，必须丢到线程里。
    """
    ok, note = await asyncio.to_thread(BM.ensure_running, auto_launch=True)
    if not ok:
        if BM.detect_browsers():
            return f"{note}\n{_START_HINT}", None
        # 没装浏览器：把「去哪装」的结构化指引一并回给前端
        guide = BM.missing_browser_guide()
        return f"{note}\n{_START_HINT}", {"browser_missing": True, "install_guide": guide}
    if note:
        logger.info("browser_auto_launched", note=note)
    return None, None


# ---- 当前工作标签（按会话隔离）----
# 点击 target=_blank 的链接会新开标签，AI 的后续动作若还落在旧标签上，
# 它会"看不到"刚打开的那一页（快照全是旧内容），进而判断成"点了没反应"。
# 这里记住「该会话上一次实际操作过的标签」，未指定 target 时优先回到它。
#
# **不能做成进程级单例**：服务是多用户的，A 的会话记下的标签会把 B 的
# 无 target 调用带偏到别人的页面上 —— 那不只是选错标签，是越权读取。
# 键取 conversation_id（无会话时退回 user_id），带 TTL 防止无限增长。
_ACTIVE_TTL = 1800.0
_active_tab: dict[str, tuple[float, str]] = {}


def _scope_key(ctx: ToolContext | None) -> str:
    if ctx is None:
        return ""
    if ctx.conversation_id:
        return f"conv:{ctx.conversation_id}"
    return f"user:{ctx.user_id}"


def _get_active_tab(ctx: ToolContext | None) -> str | None:
    key = _scope_key(ctx)
    item = _active_tab.get(key) if key else None
    if not item:
        return None
    at, tab_id = item
    if (_t.monotonic() - at) > _ACTIVE_TTL:
        _active_tab.pop(key, None)
        return None
    return tab_id


def set_active_tab(ctx: ToolContext | None, tab_id: str | None) -> None:
    key = _scope_key(ctx)
    if not key:
        return
    if not tab_id:
        _active_tab.pop(key, None)
        return
    _active_tab[key] = (_t.monotonic(), tab_id)
    if len(_active_tab) > 64:
        oldest = min(_active_tab.items(), key=lambda kv: kv[1][0])[0]
        _active_tab.pop(oldest, None)


def _pick_tab(
    tabs: list[C.Tab], target: str | None, preferred: str | None = None,
) -> C.Tab | None:
    """按 URL 子串或标题挑选标签。

    未指定 target 时：优先回到 `preferred`（该会话上一次操作的标签，
    可能正是新开出来的那个），它已不在列表里再退回第一个普通页面。
    """
    if not tabs:
        return None
    if not target:
        if preferred:
            for tab in tabs:
                if tab.id == preferred:
                    return tab
        return tabs[0]
    t = target.strip().lower()
    for tab in tabs:
        if t in (tab.url or "").lower() or t in (tab.title or "").lower():
            return tab
    return None


def _new_tabs(before: list[C.Tab], after: list[C.Tab]) -> list[C.Tab]:
    """按 id 差集找出新开的页面标签（弹出窗口不算，它不承载可操作内容）。"""
    seen = {t.id for t in before}
    return [t for t in after if t.id not in seen and t.type == "page"]


async def _tabs_now() -> list[C.Tab]:
    """取标签列表；拿不到就当空（用于前后对比，不是主流程）。"""
    try:
        return await C.list_tabs()
    except ValidationError:
        return []


async def _open_tab(target: str | None, ctx: ToolContext | None = None
                    ) -> tuple[C.Tab | None, str | None]:
    """连上前置检查：确保浏览器在、挑到标签。返回 (标签, 错误)。"""
    derr, _hint = await _ensure_browser()
    if derr:
        return None, derr
    try:
        tabs = await C.list_tabs()
    except ValidationError as e:
        return None, f"{e}\n\n{_START_HINT}"
    tab = _pick_tab(tabs, target, _get_active_tab(ctx))
    if tab is None:
        return None, f"没有匹配的标签页（target={target}）。{_START_HINT}"
    set_active_tab(ctx, tab.id)
    return tab, None


def _dialog_note(sess: C.CDPSession) -> tuple[str, list[dict]]:
    """把会话内自动应答过的 JS 对话框说清楚。

    为什么必须回显：`CDPSession` 会自动点掉 alert/confirm（不点整个标签就卡死），
    但那个「确定」是**替 AI 做的决定**。电商下单、清空购物车这类 confirm 一旦
    被静默确认，AI 会以为自己只是点了下按钮，后续推理全建立在错误前提上。
    """
    if not sess.dialogs:
        return "", []
    parts: list[str] = []
    for d in sess.dialogs:
        kind = d.get("type") or "dialog"
        msg = (d.get("message") or "").strip()
        if kind == "recovered":
            # 上一会话遗留的对话框：只知道「曾卡住、已点掉」，不知道它写了什么
            parts.append("进入页面前曾有一个对话框卡住通道，已自动应答")
            continue
        if d.get("policy") == "accept":
            verb = "已自动点「确定」"
        elif d.get("policy") == "dismiss":
            verb = "已自动点「取消」"
        else:
            verb = "未应答（该标签可能已卡住）"
        parts.append(f"{kind}「{msg}」{verb}" if msg else f"{kind} {verb}")
    return "页面弹出了对话框，但浏览器端已自动处理：" + "；".join(parts), list(sess.dialogs)


# ---------------------------------------------------------------- 只读工具


class BrowserTabsTool:
    """列出浏览器里打开的标签页。"""

    name = "browser_tabs"
    description = (
        "列出本机浏览器（需开启 9222 调试端口）里已打开的标签页，返回标题与 URL。"
        "在需要操作某个已登录页面之前先调用它，拿到可用的标签。"
    )
    required_permission = "tool:invoke"
    kind = "read"
    parameters = {"type": "object", "properties": {}}

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        derr, hint = await _ensure_browser()
        if derr:
            return ToolResult(content=derr, is_error=True, data=hint)
        try:
            tabs = await C.list_tabs()
        except ValidationError as e:
            return ToolResult(content=f"{e}\n\n{_START_HINT}", is_error=True)
        if not tabs:
            return ToolResult(content=f"浏览器已连接，但没有可操作的标签页。\n{_START_HINT}")
        lines = [f"共 {len(tabs)} 个标签页："]
        for i, t in enumerate(tabs, 1):
            lines.append(f"{i}. {t.title[:60] or '(无标题)'} — {t.url[:120]}")
        return ToolResult(
            content="\n".join(lines),
            data={"tabs": [{"title": t.title, "url": t.url} for t in tabs]},
        )


class BrowserReadTool:
    """读取网页正文。"""

    name = "browser_read"
    description = (
        "用本机浏览器打开/读取一个网页，返回标题、正文与链接。"
        "**适合需要登录态或 JS 渲染的页面**；普通公开网页优先用 http_request 更省资源。\n"
        "可指定 url 直接打开，或用 target 匹配已打开的标签（URL 或标题子串）。\n"
        "想知道页面上有哪些按钮/输入框可操作时，用 browser_snapshot 更合适。"
    )
    required_permission = "tool:invoke"
    kind = "read"
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要打开的网址（可选）"},
            "target": {"type": "string", "description": "已打开标签的 URL/标题子串（可选，url 为空时用）"},
            "max_chars": {"type": "integer", "description": "正文最多返回多少字，默认 8000"},
        },
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        url = str(args.get("url") or "").strip()
        target = str(args.get("target") or "").strip() or None
        max_chars = max(500, min(int(args.get("max_chars") or 8000), 40000))

        derr, hint = await _ensure_browser()
        if derr:
            return ToolResult(content=derr, is_error=True, data=hint)
        try:
            tabs = await C.list_tabs()
        except ValidationError as e:
            return ToolResult(content=f"{e}\n\n{_START_HINT}", is_error=True)

        # 选标签：指定 target 时匹配；否则用第一个页面（打开新 URL 也复用它）
        tab = _pick_tab(tabs, target, _get_active_tab(ctx)) if target else (
            _pick_tab(tabs, None, _get_active_tab(ctx))
        )
        if tab is None:
            return ToolResult(
                content=f"没有可用的标签页（target={target or '未指定'}）。\n{_START_HINT}",
                is_error=True,
            )

        try:
            async with C.CDPSession(tab.ws_url) as sess:
                if url:
                    await sess.navigate(url)
                page = await sess.get_text(max_chars=max_chars)
                dnote, dialogs = _dialog_note(sess)
        except ValidationError as e:
            return ToolResult(content=str(e), is_error=True)
        set_active_tab(ctx, tab.id)

        head = f"# {page['title']}\n{page['url']}\n\n"
        if dnote:
            head = f"{dnote}\n\n{head}"
        body = page["text"] or "（页面无正文）"
        if page.get("truncated"):
            body += f"\n\n…（已截断，全文 {page['char_count']} 字）"
        links = page.get("links") or []
        if links:
            body += "\n\n## 页面链接\n" + "\n".join(
                f"- {l['text'] or '(无文字)'} → {l['href']}" for l in links[:30]
            )
        data = {"title": page["title"], "url": page["url"],
                "char_count": page["char_count"], "links": links[:30]}
        if dialogs:
            data["dialogs"] = dialogs
        return ToolResult(content=head + body, data=data)


class BrowserSnapshotTool:
    """抓取页面结构：可交互元素编号、标题层级、正文。"""

    name = "browser_snapshot"
    description = (
        "抓取当前页面结构，把可交互元素（按钮/链接/输入框/下拉/复选框等）"
        "**逐个编号**列出，例如：\n"
        "  [0] button \"搜索\"\n"
        "  [3] textbox \"关键词\" (value='')\n"
        "**操作网页前先调这个**，再用编号调用 browser_action（ref=3）。"
        "不要自己猜 CSS 选择器。\n"
        "同源 iframe（支付/登录/验证码框）里的元素会一并编号，并标注 in iframe；"
        "跨域 iframe 读不到内容，返回里会明确说明有几个、地址是什么"
        "——那部分是浏览器强制的边界，不是页面没有。\n"
        "编号只在本次快照内有效；页面变动后请重新调用以获取新编号。"
    )
    required_permission = "tool:invoke"
    kind = "read"
    parameters = {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "已打开标签的 URL/标题子串（可选）"},
            "url": {"type": "string", "description": "先跳转到该网址再抓取（可选）"},
            "max_elements": {"type": "integer", "description": "最多列多少个可交互元素，默认 120，上限 150"},
            "max_text": {"type": "integer", "description": "正文最多返回多少字，默认 6000"},
        },
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        target = str(args.get("target") or "").strip() or None
        url = str(args.get("url") or "").strip()
        max_elements = max(10, min(int(args.get("max_elements") or 120), O.MAX_ELEMENTS))
        max_text = max(500, min(int(args.get("max_text") or O.TEXT_MAX), 40000))

        tab, err = await _open_tab(target, ctx)
        if err:
            return ToolResult(content=err, is_error=True)

        token = uuid.uuid4().hex[:12]
        dialogs: list[dict] = []
        dnote = ""
        try:
            async with C.CDPSession(tab.ws_url) as sess:
                if url:
                    if not url.lower().startswith(("http://", "https://")):
                        return ToolResult(content="只支持 http/https 地址", is_error=True)
                    await sess.navigate(url)
                raw = await sess.snapshot(max_elements=max_elements, token=token)
                # 页面加载时弹的 alert/confirm 已被自动点掉，但那是替 AI 做的决定，
                # 必须让它知道，否则后续判断建立在一个它不知道的「确定」上。
                dnote, dialogs = _dialog_note(sess)
        except ValidationError as e:
            return ToolResult(content=str(e), is_error=True)

        snap = O.PageSnapshot.from_raw(raw)
        if not snap.token:
            snap.token = token
        if not snap.url and not snap.title:
            return ToolResult(content="抓取页面结构失败（页面可能正在跳转，请重试）", is_error=True)

        _cache_put(tab.id, snap)
        set_active_tab(ctx, tab.id)
        text = O.format_snapshot(snap, max_text=max_text, max_elements=max_elements)
        if dnote:
            text = f"{dnote}\n\n{text}"
        data: dict = {
            "title": snap.title,
            "url": snap.url,
            "element_count": len(snap.elements),
            "truncated": snap.truncated,
            "elements": [
                {"ref": e.idx, "role": e.role, "name": e.name,
                 "disabled": e.disabled, "checked": e.checked}
                for e in snap.elements
            ],
        }
        # 前端面板 / 上层逻辑要知道"这页有读不到的框"，不能只看 element_count
        if snap.iframe_count or snap.cross_iframe_count:
            data["iframes"] = {
                "same_origin": snap.iframe_count,
                "cross_origin": snap.cross_iframe_count,
                "cross_srcs": snap.cross_iframe_srcs,
            }
        if dialogs:
            data["dialogs"] = dialogs
        return ToolResult(content=text, data=data)


# ---------------------------------------------------------------- 动作解析


# 元素定位的重试节奏（毫秒）。
#
# 为什么需要重试：懒加载/骨架屏页面上，快照抓完到动作执行之间页面可能还在
# 往上追加（列表滚动、分批渲染）。第一次找不到元素就直接报错，AI 会得出
# 「按钮不存在」的结论，然后去猜选择器或放弃 —— 而它只需要再等 300ms。
# 三次的总等待控制在 ~1s：足够覆盖一次异步渲染，又不会让用户觉得卡住。
_LOCATE_RETRY_MS = (0, 300, 700)


async def _locate(
    sess: C.CDPSession, step: O.Step, cached: O.PageSnapshot | None,
) -> tuple[O.Element | None, str, str]:
    """把步骤的 ref/text 解析成当前页面上的元素。

    返回 (元素, 标记选择器, 错误)。标记选择器为空表示只能用坐标点击。

    解析顺序：
    1. **标记回指** —— `[data-rea-snap=token][data-rea-idx=n]`，零歧义
    2. 标记失效（DOM 被整体重建）时刷新快照，按角色/名称/位置重新匹配
    3. 没给 ref 时按名称子串找

    2/3 会按 `_LOCATE_RETRY_MS` 重试：动态渲染的页面上「暂时还没有」
    和「真的没有」看起来完全一样，只差一次等待。
    """
    fresh: O.PageSnapshot | None = None

    async def _fresh(force: bool = False) -> O.PageSnapshot:
        nonlocal fresh
        if fresh is None or force:
            fresh = O.PageSnapshot.from_raw(await sess.snapshot(max_elements=O.MAX_ELEMENTS))
        return fresh

    if step.ref is not None:
        want = cached.by_ref(step.ref) if cached else None

        # 1) 标记回指
        if cached and cached.token and want is not None:
            sel = O.marked_selector(cached.token, step.ref)
            rect = await sess.rect_of(sel)
            if rect is not None:
                return (
                    O.Element(
                        idx=want.idx, role=want.role, name=want.name, tag=want.tag,
                        type=want.type, disabled=want.disabled, checked=want.checked,
                        x=float(rect["x"]), y=float(rect["y"]),
                        w=float(rect["w"]), h=float(rect["h"]),
                    ),
                    sel,
                    "",
                )

        # 2) 标记失效 → 重新匹配（带重试）
        page = await _fresh()
        for i, delay in enumerate(_LOCATE_RETRY_MS):
            if delay:
                await asyncio.sleep(delay / 1000.0)
                page = await _fresh(force=True)
            if want is None:
                want = page.by_ref(step.ref)
            if want is None:
                continue
            hit = O.match_candidates(want, page.elements)
            if hit is None:
                continue
            # 重新匹配命中后重新打标，让后续步骤又能走零歧义路径
            sel = O.marked_selector(page.token, hit.idx) if page.token else ""
            if sel and await sess.rect_of(sel) is not None:
                return hit, sel, ""
            return hit, "", ""

        if want is None:
            return None, "", (
                f"编号 [{step.ref}] 超出当前页面可交互元素范围"
                f"（共 {len(page.elements)} 个）。请重新调用 browser_snapshot 取新编号。"
            )
        return None, "", (
            f"编号 [{step.ref}]（{want.role} \"{want.name}\"）在页面上已找不到，"
            "可能已被移除。请重新调用 browser_snapshot。"
        )

    # 3) 按名称找（同样带重试：列表可能还在往上追加）
    page = await _fresh()
    for i, delay in enumerate(_LOCATE_RETRY_MS):
        if delay:
            await asyncio.sleep(delay / 1000.0)
            page = await _fresh(force=True)
        hit = page.find(step.text)
        if hit is None:
            continue
        sel = O.marked_selector(page.token, hit.idx) if page.token else ""
        if sel and await sess.rect_of(sel) is not None:
            return hit, sel, ""
        return hit, "", ""
    return None, "", f"页面上找不到名称包含「{step.text}」的可交互元素"


async def _center_now(sess: C.CDPSession, el: O.Element, sel: str) -> tuple[float, float]:
    """取元素**此刻**的中心坐标（必要时先滚到视口内）。

    快照里的坐标是抓取那一刻的；页面滚动或重排后就过期了。
    用旧坐标点击会点偏，所以点击前一律重新取。
    """
    if sel:
        await sess.scroll_into_view(sel)
        rect = await sess.rect_of(sel)
        if rect is not None:
            return (
                float(rect["x"]) + float(rect["w"]) / 2,
                float(rect["y"]) + float(rect["h"]) / 2,
            )
    return el.center


async def _after_nav(sess: C.CDPSession, act: str, url: str) -> tuple[dict, str]:
    """导航类动作后的就绪检查。

    `CDPSession.navigate` 内部已经等过 DOM 稳定；这里再取一次结果是为了
    把「可能还没加载完」**明确告诉 AI**——否则它看到一个空页面会判断成
    「页面上没有目标元素」，然后去猜选择器或放弃，而真实原因只是慢。
    """
    try:
        r = await sess.wait_ready(timeout_ms=8000)
    except ValidationError:
        r = {"ok": True, "ready_state": "", "waited_ms": 0}
    res = {"action": act, "url": url, "ready_state": r.get("ready_state") or ""}
    if not r.get("ok"):
        return res, (
            f"页面在 {r.get('waited_ms')}ms 内未加载完成（readyState="
            f"{r.get('ready_state') or '未知'}）。内容可能还没渲染出来，"
            "请先 wait 或重新 browser_snapshot 再操作。"
        )
    return res, ""


async def _run_step(
    sess: C.CDPSession, step: O.Step, cached: O.PageSnapshot | None,
) -> tuple[dict, str]:
    """执行单个步骤，返回 (结果字典, 错误信息)。"""
    act = step.action

    # ---- 不需要元素定位的动作 ----
    if act == "navigate":
        await sess.navigate(step.url)
        return await _after_nav(sess, act, step.url)
    if act == "back":
        await sess.go_back()
        return await _after_nav(sess, act, "")
    if act == "reload":
        await sess.reload()
        return await _after_nav(sess, act, "")
    if act == "wait":
        await asyncio.sleep(step.wait_ms / 1000.0)
        return {"action": act, "waited_ms": step.wait_ms}, ""
    if act == "scroll":
        # rounds 由 parse_step 校验过（只有 scroll 允许），0 / 缺省 = 只滚一次
        if not step.rounds:
            page = O.PageSnapshot.from_raw(await sess.snapshot(max_elements=10))
            dy = step.dy or page.viewport_h * 0.8
            await sess.wheel(page.viewport_w / 2, page.viewport_h / 2, delta_y=dy)
            return {"action": act, "dy": round(dy)}, ""
        r = await sess.scroll_load(
            rounds=step.rounds, dy=step.dy, interval_ms=step.interval_ms,
        )
        # 是否真的加载出新内容，必须明确回给 AI。它看不到滚动，只能看结果；
        # 一句「已滚动」会让它以为列表已经完整——实际可能还停在首屏。
        if r.get("reason") == "bottom":
            note = (
                f"已滚动 {r.get('rounds_done')} 轮，到达页面底部"
                f"（高度 {r.get('height_before')} → {r.get('height_after')}）"
            )
        elif r.get("reason") == "no_new_content":
            note = (
                f"已滚动 {r.get('rounds_done')} 轮，此后没有新内容加载"
                f"（可能已到列表末尾，或该站限流）"
            )
        else:
            note = (
                f"已滚动 {r.get('rounds_done')} 轮（达到本次轮数上限），"
                "列表可能还有更多内容，可再次调用"
            )
        return {"action": act, "dy": round(step.dy), "note": note, **r}, ""
    if act == "read":
        page = await sess.page_text()
        return {"action": act, "text": str(page.get("text") or "")[:4000]}, ""
    if act == "assert_text":
        page = await sess.page_text()
        found = step.text.lower() in str(page.get("text") or "").lower()
        return {"action": act, "found": found}, (
            "" if found else f"页面正文中未找到「{step.text}」"
        )
    if act == "wait_for":
        # ref 与 text 都支持：等编号对应的那个元素真的可见，通常比等文案更准确
        # （"提交成功"这类文案在页面别处也常有，按元素等不会误判）。
        script = ""
        if step.ref is not None:
            el, sel, err = await _locate(sess, step, cached)
            if err:
                return {}, err
            if sel:
                script = O.wait_script(selector=sel)
        if not script and step.text:
            script = O.wait_script(text=step.text)
        if not script:
            return {}, "wait_for 需要 text 或 ref"
        r = await sess.wait_until(script, timeout_ms=step.timeout_ms or 10000)
        if not r.get("ok"):
            return {"action": act, "ok": False, "waited_ms": r.get("waited_ms")}, (
                f"等待「{step.text or f'[{step.ref}]'}」出现超时（{r.get('waited_ms')}ms）"
            )
        return {"action": act, "ok": True, "waited_ms": r.get("waited_ms")}, ""

    # ---- 需要元素定位的动作 ----
    el, sel, err = await _locate(sess, step, cached)
    if err:
        return {}, err
    assert el is not None
    x, y = await _center_now(sess, el, sel)

    if act in ("click", "dblclick"):
        await sess.click_at(x, y, click_count=2 if act == "dblclick" else 1)
        return {"action": act, "ref": step.ref, "role": el.role, "name": el.name}, ""

    if act == "hover":
        await sess.hover_at(x, y)
        return {"action": act, "ref": step.ref, "name": el.name}, ""

    if act == "fill":
        # 先聚焦并全选，再 insertText：直接赋值会让受控组件认为字段"从未编辑过"，
        # 而先全选能保证是**替换**而不是往原值后面追加。
        if not sel or not await sess.focus_and_select(sel):
            await sess.click_at(x, y)
        await sess.insert_text(step.value)
        return {"action": act, "ref": step.ref, "name": el.name, "value": step.value}, ""

    if act == "type":
        if not sel or not await sess.focus_and_select(sel):
            await sess.click_at(x, y)
        await sess.insert_text(step.text)
        key = step.key.strip().lower()
        if key in ("enter", "tab"):
            await sess.press_key("Enter" if key == "enter" else "Tab")
        return {"action": act, "ref": step.ref, "name": el.name, "text": step.text}, ""

    if act == "select":
        if not sel:
            return {}, f"下拉选择需要可定位的目标（{el.role}），请重新 snapshot 后再试"
        r = await sess.select_option(sel, step.value)
        if r.get("ok"):
            return {"action": act, "ref": step.ref, "value": r.get("value")}, ""
        opts = r.get("options") or []
        if opts:
            return {}, (
                f"下拉里找不到选项 {step.value!r}；可用选项："
                + "、".join(str(o) for o in opts[:10])
            )
        return {}, f"下拉选择失败：{r.get('reason') or '未知原因'}"

    if act in ("check", "uncheck"):
        if not sel:
            return {}, "勾选需要可定位的目标，请重新 snapshot 后再试"
        r = await sess.set_checked(sel, act == "check")
        if r.get("ok"):
            return {"action": act, "ref": step.ref, "checked": r.get("checked")}, ""
        if r.get("reason"):
            return {}, f"勾选失败：{r['reason']}"
        return {}, f"操作后勾选状态仍为 {r.get('checked')}，页面可能拦截了点击"

    if act == "press":
        await sess.press_key(step.key)
        return {"action": act, "key": step.key}, ""

    return {}, f"未实现的 action：{act}"


class BrowserActionTool(WriteToolMixin):
    """在网页上执行点击/填写等操作。"""

    name = "browser_action"
    description = (
        "在本机浏览器里对已打开页面执行操作。仅供需要登录态的交互式任务使用。\n"
        "\n"
        "**推荐流程**：先 browser_snapshot 拿到元素编号，再用 ref 操作：\n"
        "  {action:'click', ref:3}\n"
        "\n"
        "单个动作：\n"
        "- click / dblclick：点击 {ref}\n"
        "- fill：填写输入框 {ref, value}（先聚焦全选再输入，即替换原值）\n"
        "- type：输入文本 {ref, text}，可带 key='Enter' 直接提交\n"
        "- select：下拉选择 {ref, value}（value 可填 option 的 value 或显示文字）\n"
        "- check / uncheck：勾选/取消勾选 {ref}\n"
        "- hover：鼠标悬停 {ref}\n"
        "- press：按键 {key}（Enter/Tab/Escape/ArrowDown 等）\n"
        "- scroll：滚动 {dy}（负数向上，省略则向下滚一屏）；"
        "懒加载列表用 {rounds:5} 连续滚动直到没有新内容，"
        "可选 {interval_ms} 调每轮间隔\n"
        "- navigate：跳转 {url}；back / reload：后退 / 刷新\n"
        "- wait_for：等某元素可见 {ref} 或等某文本出现 {text, timeout_ms}；wait：固定等待 {ms}\n"
        "- assert_text：断言正文包含某文本 {text}；read：读取当前正文\n"
        "- screenshot：截图当前视口，存为图片并返回下载链接\n"
        "\n"
        "**批量执行**：用 steps 数组一次提交多步，按顺序执行、遇错即停：\n"
        "  {steps:[{action:'fill',ref:1,value:'张三'},\n"
        "          {action:'click',ref:3},\n"
        "          {action:'wait_for',text:'提交成功'}]}\n"
        "比逐次调用省往返，也避免中途被别的操作插队。\n"
        "操作后建议再调 browser_snapshot 确认页面已变化。"
    )
    required_permission = "tool:invoke"
    kind = "write"   # 会产生外部副作用，走 HITL 确认
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "单个动作名；用 steps 批量时可省略"},
            "ref": {"type": "integer", "description": "元素编号（browser_snapshot 返回的 [n]）"},
            "text": {"type": "string", "description": "要输入的文本 / 要等待或断言的文本（按名称定位亦可）"},
            "value": {"type": "string", "description": "填写内容（fill）或下拉选项（select）"},
            "url": {"type": "string", "description": "目标网址（navigate）"},
            "key": {"type": "string", "description": "按键名（press）或提交键（type）"},
            "dy": {"type": "number", "description": "滚动像素（scroll，负数向上）"},
            "rounds": {"type": "integer",
                       "description": "多轮滚动次数（scroll），用于懒加载列表；"
                                      "滚到没有新内容即自动停止，上限 30"},
            "interval_ms": {"type": "integer",
                            "description": "多轮滚动时每轮之间的等待毫秒（scroll，默认 350）"},
            "ms": {"type": "integer", "description": "等待毫秒（wait）"},
            "timeout_ms": {"type": "integer", "description": "等待超时（wait_for），默认 10000"},
            "target": {"type": "string", "description": "已打开标签的 URL/标题子串（可选）"},
            "steps": {
                "type": "array",
                "description": "批量步骤数组，元素形状同本参数表",
                "items": {"type": "object"},
            },
        },
    }

    # 只读步骤不需要人工确认：看一眼正文不该弹审批卡片
    _READONLY_ACTIONS = {"read", "screenshot", "wait", "wait_for", "assert_text", "scroll"}

    def needs_confirm(self, args: dict) -> bool:
        """只读步骤免确认；只要有任何一个写步骤就要确认。"""
        steps = args.get("steps")
        if isinstance(steps, list) and steps:
            acts = {
                str(s.get("action") or "").strip().lower()
                for s in steps if isinstance(s, dict)
            }
            return any(a not in self._READONLY_ACTIONS for a in acts)
        act = str(args.get("action") or "").strip().lower()
        return act not in self._READONLY_ACTIONS

    def summarize(self, args: dict) -> str:
        steps = args.get("steps")
        if isinstance(steps, list) and steps:
            preview = "、".join(
                str(s.get("action") or "?") for s in steps[:3] if isinstance(s, dict)
            )
            more = "…" if len(steps) > 3 else ""
            return f"在网页上连续执行 {len(steps)} 步操作（{preview}{more}）"
        a = str(args.get("action") or "")
        ref = args.get("ref")
        tgt = f"[{ref}]" if ref is not None else str(args.get("text") or "")
        table = {
            "click": "点击网页元素", "dblclick": "双击网页元素",
            "fill": "填写网页输入框", "type": "向网页元素输入文本",
            "select": "选择下拉项", "check": "勾选网页元素",
            "uncheck": "取消勾选网页元素", "hover": "悬停网页元素",
        }
        if a in table:
            return f"{table[a]} {tgt}".strip()
        if a == "navigate":
            return f"浏览器跳转到 {args.get('url')}"
        if a == "back":
            return "浏览器后退"
        if a == "reload":
            return "刷新浏览器页面"
        if a == "press":
            return f"按下按键 {args.get('key')}"
        return f"网页操作 {a}"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        target = str(args.get("target") or "").strip() or None
        tab, err = await _open_tab(target, ctx)
        if err:
            return ToolResult(content=err, is_error=True)

        # 先把所有步骤校验完再动手：跑到第 7 步才发现参数缺失时，
        # 前 6 步的副作用已经落到页面上，回滚不了。
        try:
            raw_steps = args.get("steps") if isinstance(args.get("steps"), list) else [args]
            steps = O.parse_steps(raw_steps)
        except ValueError as e:
            return ToolResult(content=f"参数错误：{e}", is_error=True)

        cached = _cache_get(tab.id)
        results: list[dict] = []
        shot: tuple[str, str] | None = None   # (file_key, 下载地址)
        dialogs: list[dict] = []
        followed: C.Tab | None = None
        # 动作前拍一次标签快照：点击 target=_blank 链接会在**没有返回值**的情况下
        # 新开标签，事后只能靠差集认出来。
        tabs_before = await _tabs_now()
        seen_ids = {t.id for t in tabs_before}

        sess: C.CDPSession | None = None
        try:
            sess = C.CDPSession(tab.ws_url)
            await sess.__aenter__()
            for i, step in enumerate(steps):
                if step.action == "screenshot":
                    b64 = await sess.screenshot()
                    if not b64:
                        return ToolResult(content="截图失败", is_error=True)
                    shot = await _save_shot(ctx, b64)
                    results.append({"action": "screenshot", "url": shot[1]})
                    continue

                res, step_err = await _run_step(sess, step, cached)
                if step_err:
                    done = "、".join(str(r.get("action")) for r in results) or "无"
                    dnote, _ = _dialog_note(sess)
                    return ToolResult(
                        content=(
                            f"第 {i + 1} 步（{step.label()}）失败：{step_err}\n"
                            f"已完成 {i} 步：{done}\n"
                            + (f"\n{dnote}\n" if dnote else "")
                            + (
                                f"\n注意：期间打开过新标签页 — {followed.url[:120]}\n"
                                if followed else ""
                            )
                            + "页面状态可能已改变，建议重新调用 browser_snapshot 后再继续。"
                        ),
                        is_error=True,
                        data={"failed_step": i + 1, "results": results},
                    )
                results.append(res)

                # 点击/按键可能新开标签：新页面不在当前会话里，
                # 后续步骤若继续在旧标签上执行，会对着一个没变化的页面上找元素，
                # 报出误导性的「找不到元素」。这里就地跟随过去。
                if step.action in ("click", "dblclick", "press"):
                    fresh_tabs = await _tabs_now()
                    opened = [t for t in fresh_tabs
                              if t.id not in seen_ids and t.type == "page"]
                    if opened:
                        followed = opened[0]
                        seen_ids.add(followed.id)
                        await sess.__aexit__(None, None, None)
                        sess = C.CDPSession(followed.ws_url)
                        await sess.__aenter__()
                        # 新标签的标记体系完全不同，旧快照必须作废
                        cached = None
                        set_active_tab(ctx, followed.id)
                        _dia, more = _dialog_note(sess)
                        dialogs.extend(more)
                        if i + 1 < len(steps):
                            # 让新页面先稳定下来，否则下一步就在半渲染的 DOM 上找元素
                            try:
                                await sess.wait_ready(timeout_ms=8000)
                            except ValidationError:
                                pass
                            try:
                                raw_snap = await sess.snapshot(max_elements=O.MAX_ELEMENTS)
                                cached = O.PageSnapshot.from_raw(raw_snap)
                                if cached.token and cached.url:
                                    _cache_put(followed.id, cached)
                                else:
                                    cached = None
                            except ValidationError:
                                cached = None

            # 动作可能改变页面：重抓一次换上新 token，
            # 否则下一步还会按过期标记点（第一次必然失败再回退，白多一轮）
            if any(s.action not in ("wait", "wait_for", "assert_text", "read") for s in steps):
                try:
                    fresh = O.PageSnapshot.from_raw(
                        await sess.snapshot(max_elements=O.MAX_ELEMENTS)
                    )
                    if fresh.token and fresh.url:
                        _cache_put(followed.id if followed else tab.id, fresh)
                except ValidationError:
                    pass
            try:
                final_url = await sess.evaluate("location.href")
            except ValidationError:
                final_url = ""
            dnote, dialogs = _dialog_note(sess)
        except ValidationError as e:
            return ToolResult(content=str(e), is_error=True)
        finally:
            if sess is not None:
                await sess.__aexit__(None, None, None)

        # 批量结束后再查一次：有些页面在加载完成后才开新标签（如支付跳转）
        if not followed:
            late = _new_tabs(tabs_before, await _tabs_now())
            if late:
                followed = late[0]
                set_active_tab(ctx, followed.id)
                # 结果里的 url 是前端面板的跟随依据：不换成新标签，
                # 用户看到的画面就仍停在旧页面上。
                final_url = followed.url or final_url

        if len(results) == 1:
            only = results[0]
            content = _describe_one(only)
            if shot:
                content += f"\n\n已截图：{shot[1]}"
            if followed:
                content += (
                    f"\n\n操作打开了新标签页：{followed.title[:60] or '(无标题)'} — {followed.url[:120]}"
                    "\n后续操作已默认切到该标签；页面内容需要重新 browser_snapshot 才看得到。"
                )
            if dnote:
                content += f"\n\n{dnote}"
            data: dict = {"result": only, "url": final_url}
            if shot:
                data.update({"file_key": shot[0], "artifact_url": shot[1]})
            if followed:
                data.update({"new_tab": {"title": followed.title, "url": followed.url}})
            if dialogs:
                data["dialogs"] = dialogs
            return ToolResult(content=content, data=data)

        lines = [f"已按顺序完成 {len(results)} 步："]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {_describe_one(r)}")
        if final_url:
            lines.append(f"\n当前页面：{final_url}")
        if followed:
            lines.append(
                f"\n操作打开了新标签页：{followed.title[:60] or '(无标题)'} — {followed.url[:120]}"
                "\n后续操作已默认切到该标签；页面内容需要重新 browser_snapshot 才看得到。"
            )
        if shot:
            lines.append(f"已截图：{shot[1]}")
        if dnote:
            lines.append(f"\n{dnote}")
        data = {"results": results, "url": final_url}
        if shot:
            data.update({"file_key": shot[0], "artifact_url": shot[1]})
        if followed:
            data.update({"new_tab": {"title": followed.title, "url": followed.url}})
        if dialogs:
            data["dialogs"] = dialogs
        return ToolResult(content="\n".join(lines), data=data)


def _describe_one(r: dict) -> str:
    """把单步结果写成一句人话（同时回给模型与确认卡片）。"""
    act = r.get("action")
    name = r.get("name") or ""
    ref = r.get("ref")
    tgt = f"[{ref}] {name}".strip() if ref is not None else name
    if act in ("click", "dblclick"):
        return f"已点击 {tgt}".strip()
    if act == "hover":
        return f"已悬停 {tgt}".strip()
    if act == "fill":
        return f"已填写 {tgt}".strip()
    if act == "type":
        return f"已输入文本到 {tgt}".strip()
    if act == "select":
        return f"已选择 {tgt} = {r.get('value')!r}".strip()
    if act in ("check", "uncheck"):
        return f"{'已勾选' if act == 'check' else '已取消勾选'} {tgt}".strip()
    if act == "press":
        return f"已按键 {r.get('key')}"
    if act == "navigate":
        return f"已跳转到 {r.get('url')}"
    if act == "back":
        return "已后退一页"
    if act == "reload":
        return "已刷新页面"
    if act == "scroll":
        if r.get("note"):
            return str(r.get("note"))
        return f"已滚动 {r.get('dy'):+.0f}px"
    if act == "wait":
        return f"已等待 {r.get('waited_ms')}ms"
    if act == "wait_for":
        return f"已等到目标出现（{r.get('waited_ms')}ms）"
    if act == "assert_text":
        return "正文校验通过" if r.get("found") else "正文校验未通过"
    if act == "read":
        return f"当前正文：\n{(r.get('text') or '')[:2000]}"
    if act == "screenshot":
        return f"已截图：{r.get('url')}"
    return f"已完成 {act}"


async def _save_shot(ctx: ToolContext, b64: str) -> tuple[str, str]:
    """截图落成 Artifact 并返回 (file_key, 下载地址)。"""
    from app.agents.tools.file_tools import _save_artifact

    data = base64.b64decode(b64)
    name = f"browser_{_t.strftime('%Y%m%d_%H%M%S')}.jpg"
    file_key, _art, _ = _save_artifact(ctx, name, data, "image/jpeg")
    await ctx.db.flush()
    return file_key, f"/api/v1/chat/attachments/{file_key}"


BROWSER_TOOLS = [BrowserTabsTool(), BrowserReadTool(), BrowserSnapshotTool(), BrowserActionTool()]
