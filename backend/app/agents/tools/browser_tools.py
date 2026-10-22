"""浏览器控制工具：连接本机已登录的 Chrome/Edge，抓取与操作网页。

适用场景：
- 需要登录态才能看到的页面（内部系统、后台、订阅内容）
- 单页应用（SPA）渲染后的内容，普通 HTTP 抓取拿不到

前提：用户以 `--remote-debugging-port=9222` 启动浏览器。
未开启时给出明确指引，而不是静默失败。
"""
from __future__ import annotations

import asyncio

from app.agents.tools.base import ToolContext, ToolResult, WriteToolMixin
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.services import browser_manager as BM
from app.services import cdp_client as C

logger = get_logger("browser_tools")

_START_HINT = (
    "可在「系统设置 → 浏览器」一键启动（会自动检测本机 Chrome/Edge 或已安装的内置运行时）；"
    "本机没装浏览器时，可在「工具 → 可选组件」安装内置浏览器运行时。"
    "也可手动以 --remote-debugging-port=9222 启动浏览器。"
)


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


def _pick_tab(tabs: list[C.Tab], target: str | None) -> C.Tab | None:
    """按 URL 子串或标题挑选标签；未指定时取第一个普通页面。"""
    if not tabs:
        return None
    if not target:
        return tabs[0]
    t = target.strip().lower()
    for tab in tabs:
        if t in (tab.url or "").lower() or t in (tab.title or "").lower():
            return tab
    return None


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
        "可指定 url 直接打开，或用 target 匹配已打开的标签（URL 或标题子串）。"
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
        tab = _pick_tab(tabs, target) if target else (tabs[0] if tabs else None)
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
        except ValidationError as e:
            return ToolResult(content=str(e), is_error=True)

        head = f"# {page['title']}\n{page['url']}\n\n"
        body = page["text"] or "（页面无正文）"
        if page.get("truncated"):
            body += f"\n\n…（已截断，全文 {page['char_count']} 字）"
        links = page.get("links") or []
        if links:
            body += "\n\n## 页面链接\n" + "\n".join(
                f"- {l['text'] or '(无文字)'} → {l['href']}" for l in links[:30]
            )
        return ToolResult(
            content=head + body,
            data={"title": page["title"], "url": page["url"],
                  "char_count": page["char_count"], "links": links[:30]},
        )


class BrowserActionTool(WriteToolMixin):
    """在网页上执行点击/填表等操作。"""

    name = "browser_action"
    description = (
        "在本机浏览器里对已打开页面执行操作。仅供需要登录态的交互式任务使用。\n"
        "action 取值：\n"
        "- click：点击 {selector}\n"
        "- fill：填写输入框 {selector, value}\n"
        "- screenshot：截图当前视口，存为图片文件并返回下载链接\n"
        "- navigate：跳转到 {url}\n"
        "selector 用 CSS 选择器，如 'button.submit'、'#search'、'input[name=q]'。\n"
        "操作后建议再调 browser_read 确认结果。"
    )
    required_permission = "tool:invoke"
    kind = "write"   # 会产生外部副作用，走 HITL 确认
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["click", "fill", "screenshot", "navigate"]},
            "selector": {"type": "string", "description": "CSS 选择器（click/fill）"},
            "value": {"type": "string", "description": "填写内容（fill）"},
            "url": {"type": "string", "description": "目标网址（navigate）"},
            "target": {"type": "string", "description": "已打开标签的 URL/标题子串（可选）"},
        },
        "required": ["action"],
    }

    def summarize(self, args: dict) -> str:
        a = args.get("action")
        if a == "click":
            return f"点击网页元素 {args.get('selector')}"
        if a == "fill":
            return f"填写网页输入框 {args.get('selector')}"
        if a == "navigate":
            return f"浏览器跳转到 {args.get('url')}"
        return "网页截图"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        action = str(args.get("action") or "")
        target = str(args.get("target") or "").strip() or None

        derr, hint = await _ensure_browser()
        if derr:
            return ToolResult(content=derr, is_error=True, data=hint)
        try:
            tabs = await C.list_tabs()
        except ValidationError as e:
            return ToolResult(content=f"{e}\n\n{_START_HINT}", is_error=True)
        tab = _pick_tab(tabs, target)
        if tab is None:
            return ToolResult(content=f"没有匹配的标签页（target={target}）。{_START_HINT}", is_error=True)

        try:
            async with C.CDPSession(tab.ws_url) as sess:
                if action == "navigate":
                    url = str(args.get("url") or "").strip()
                    if not url:
                        return ToolResult(content="navigate 需要 url", is_error=True)
                    await sess.navigate(url)
                    return ToolResult(content=f"已跳转到 {url}", data={"url": url})

                if action == "click":
                    sel = str(args.get("selector") or "").strip()
                    if not sel:
                        return ToolResult(content="click 需要 selector", is_error=True)
                    r = await sess.click(sel)
                    if not r.get("ok"):
                        return ToolResult(content=f"点击失败：{r.get('reason')}", is_error=True)
                    return ToolResult(content=f"已点击 {sel}（{r.get('tag')}）", data=r)

                if action == "fill":
                    sel = str(args.get("selector") or "").strip()
                    if not sel:
                        return ToolResult(content="fill 需要 selector", is_error=True)
                    r = await sess.fill(sel, str(args.get("value") or ""))
                    if not r.get("ok"):
                        return ToolResult(content=f"填写失败：{r.get('reason')}", is_error=True)
                    return ToolResult(content=f"已填写 {sel}", data=r)

                if action == "screenshot":
                    b64 = await sess.screenshot()
                    if not b64:
                        return ToolResult(content="截图失败", is_error=True)
                    # 截图落成 Artifact 供前端展示；tool 消息只把链接回给模型
                    # （模型看不到 base64，直接塞进 data 等于丢弃）
                    import base64
                    import time as _t

                    from app.agents.tools.file_tools import _save_artifact

                    data = base64.b64decode(b64)
                    name = f"browser_{_t.strftime('%Y%m%d_%H%M%S')}.jpg"
                    file_key, art, _ = _save_artifact(ctx, name, data, "image/jpeg")
                    await ctx.db.flush()
                    url = f"/api/v1/chat/attachments/{file_key}"
                    return ToolResult(
                        content=f"已截图当前页面：{name}（{url}）",
                        data={"artifact_id": art.id, "file_key": file_key,
                              "url": url, "media_type": "image/jpeg"},
                    )

                return ToolResult(content=f"未知 action：{action}", is_error=True)
        except ValidationError as e:
            return ToolResult(content=str(e), is_error=True)


BROWSER_TOOLS = [BrowserTabsTool(), BrowserReadTool(), BrowserActionTool()]
