"""内置工具：knowledge_retrieval / http_request。

内置工具由代码注册，不入库。
"""
from __future__ import annotations

import ipaddress
import json
import socket
from urllib.parse import urlparse

from app.agents.tools.base import ToolContext, ToolResult


class KnowledgeRetrievalTool:
    """知识库检索工具。强制使用 ctx.ps 保证权限感知，不越权。"""

    name = "knowledge_retrieval"
    description = "在企业知识库中检索与问题相关的资料。当需要基于企业文档回答时使用。"
    required_permission = "chat:use"  # 能对话即可检索（viewer 有 chat:use）
    kind = "read"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索用的自然语言问题"},
            "top_k": {"type": "integer", "description": "返回条数，默认 5", "default": 5},
            "kb_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "可选，限定在哪些知识库内检索",
            },
        },
        "required": ["query"],
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.services.retrieval_service import retrieve, to_citations

        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(content="检索失败：query 为空", is_error=True)
        top_k = int(args.get("top_k") or 5)

        # kb 范围：agent 有绑定则限定在绑定范围内；无绑定则全库（由权限过滤兜底）
        allowed = ctx.config.get("kb_ids")
        req_kbs = args.get("kb_ids")
        if allowed:
            # agent 绑定了范围：取 LLM 请求与范围内的交集
            kb_ids = [k for k in req_kbs if k in set(allowed)] if req_kbs else list(allowed)
        else:
            # agent 未绑定：忽略 LLM 指定的 kb，统一全库（防越权，权限过滤内建）
            kb_ids = None

        resp = await retrieve(ctx.db, ps=ctx.ps, query=query, kb_ids=kb_ids, top_k=top_k)
        if not resp.chunks:
            return ToolResult(content="（未检索到相关资料）", data={"count": 0}, citations=[])

        lines = []
        for i, c in enumerate(resp.chunks, start=1):
            src = c.doc_title or "未知文档"
            if c.page:
                src += f" 第{c.page}页"
            lines.append(f"[{i}] 来源：{src}\n{c.content}")
        text = "\n\n".join(lines)
        return ToolResult(
            content=text,
            data={"count": len(resp.chunks), "timing_ms": resp.timing_ms},
            citations=to_citations(resp.chunks),
        )


# ---- SSRF 防护 ----
_BLOCKED_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # 含云元数据 169.254.169.254
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_blocked_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return any(addr in net for net in _BLOCKED_NETS)


def _check_url(url: str, allow_hosts: list[str] | None) -> str | None:
    """返回错误信息，或 None 表示通过。**同步阻塞**（含 DNS 解析）。

    只想在 async 代码里调用时请用 `_check_url_async`：这里的
    `socket.getaddrinfo` 在网络抖动/DNS 不通时要等 5~30 秒，直接在事件循环里
    调用会把整个进程钉住（表现为「其它接口全部无响应」）。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"不支持的协议: {parsed.scheme}"
    host = parsed.hostname
    if not host:
        return "URL 缺少主机名"
    if allow_hosts and not any(host == h or host.endswith("." + h) for h in allow_hosts):
        return f"主机不在白名单内: {host}"
    # 解析所有 IP，任一为内网即拒绝（防 DNS rebinding）
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return f"域名解析失败: {e}"
    for info in infos:
        ip = info[4][0]
        if _is_blocked_ip(ip):
            return f"拒绝访问内网/保留地址: {host} -> {ip}"
    return None


async def _check_url_async(
    url: str, allow_hosts: list[str] | None, *, timeout: float = 10.0
) -> str | None:
    """`_check_url` 的异步封装：DNS 解析丢到线程池，不阻塞事件循环。

    不缓存结果是有意的：SSRF 守卫的价值就在「请求前现查一次」，缓存等于把
    DNS rebinding 的窗口重新打开。
    """
    import asyncio

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_check_url, url, allow_hosts), timeout=timeout
        )
    except asyncio.TimeoutError:
        return f"域名解析超时（>{timeout:.0f}s）"


class HttpRequestTool:
    """HTTP 请求工具（默认关闭，需在 agent.tool_config 启用）。含 SSRF 防护。"""

    name = "http_request"
    description = "发起 HTTP 请求调用外部接口。仅在明确需要访问外部服务时使用。"
    required_permission = "tool:invoke"
    kind = "write"
    auto_approve = True  # 高频工具，免人工确认；仍保留 write 以拦截外部客户
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "完整 URL"},
            "method": {"type": "string", "enum": ["GET", "POST"], "default": "GET"},
            "headers": {"type": "object", "description": "请求头"},
            "body": {"type": "string", "description": "请求体（POST）"},
        },
        "required": ["url"],
    }

    MAX_BYTES = 256 * 1024

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        import httpx

        url = str(args.get("url") or "").strip()
        method = str(args.get("method") or "GET").upper()
        allow_hosts = ctx.config.get("allow_hosts") or None
        timeout = int(ctx.config.get("timeout") or 20)

        err = _check_url(url, allow_hosts)
        if err:
            return ToolResult(content=f"请求被拒绝：{err}", is_error=True)

        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                resp = await client.request(
                    method,
                    url,
                    headers=args.get("headers") or {},
                    content=(args.get("body") or None).encode() if args.get("body") else None,
                )
            body = resp.text[: self.MAX_BYTES]
            return ToolResult(
                content=f"HTTP {resp.status_code}\n{body}",
                data={"status": resp.status_code, "length": len(body)},
            )
        except Exception as e:  # noqa: BLE001
            return ToolResult(content=f"请求失败：{str(e)[:200]}", is_error=True)


class CheckCapabilityTool:
    """查询平台功能与当前账号权限。让 AI 准确回答"能做什么/有无权限"。"""

    name = "check_my_capabilities"
    description = "查询本平台的功能清单，以及当前账号有哪些权限、能做哪些操作。当用户问“你能做什么”“我能不能管理成员”等关于功能或权限的问题时使用。"
    required_permission = None  # 全员可用（只读自身权限）
    kind = "read"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "可选，查询某个具体功能/操作是否有权限，如“成员管理”“上传文档”。留空则返回全部权限摘要。",
            },
        },
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.agents.capabilities import check_permission_brief

        perms = set()
        try:
            from app.middleware.auth_dep import get_user_permission_codes
            from app.models import User

            u = await ctx.db.get(User, ctx.user_id)
            if u:
                perms = await get_user_permission_codes(ctx.db, u)
        except Exception:  # noqa: BLE001
            perms = set()
        is_admin = bool(getattr(ctx.ps, "is_admin", False))
        brief = await check_permission_brief(perms, is_admin, query=str(args.get("query") or ""))
        return ToolResult(content=brief)


def _match_terms(query: str) -> list[str]:
    """把查询切成匹配词：ASCII 词 + 中文 2-gram（中文无空格，用二元组召回）。"""
    import re

    q = (query or "").lower().strip()
    terms: list[str] = [w for w in re.split(r"[^\w一-鿿]+", q) if len(w) >= 2]
    for run in re.findall(r"[一-鿿]+", q):
        terms.append(run)  # 整段（长词命中加权）
        terms += [run[i:i + 2] for i in range(len(run) - 1)]
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


class FindToolsTool:
    """按需检索管理/运维工具：让 AI 先发现、再调用。只返回当前用户有权调用的工具。

    问答页默认只常驻核心工具（知识检索/文件/办公），管理/运维工具不随请求下发以省 token；
    AI 需要时调用本工具，命中工具名写入 data.enable_tools，由 react 循环动态启用。
    """

    name = "find_tools"
    description = (
        "检索本平台可用的管理/运维操作工具。当你被要求执行平台管理操作"
        "（建知识库/上传文档、管理用户/角色/部门、配置定时任务、查用量、改模型配置等）"
        "但当前工具列表里没有对应工具时，先用自然语言描述你要做的事调用本工具；"
        "它会返回匹配工具的名称与参数，**下一轮**你就能直接调用它们。"
    )
    required_permission = None  # 只做检索过滤，全员可用；真正准入由 tool_allowed 把关
    kind = "read"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "你要执行的操作，如「创建定时任务」「批量删除文档」「改用户角色」"},
            "limit": {"type": "integer", "description": "最多返回几个，默认 8"},
        },
        "required": ["query"],
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.agents.tools.registry import registry, tool_allowed

        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(content="请提供你要执行的操作描述", is_error=True)
        try:
            limit = int(args.get("limit") or 8)
        except (TypeError, ValueError):
            limit = 8
        limit = max(1, min(limit, 20))

        perms: set[str] = set()
        try:
            from app.middleware.auth_dep import get_user_permission_codes
            from app.models import User

            u = await ctx.db.get(User, ctx.user_id)
            if u:
                perms = await get_user_permission_codes(ctx.db, u)
        except Exception:  # noqa: BLE001
            perms = set()
        is_external = bool(getattr(ctx.ps, "is_external", False))

        terms = _match_terms(query)
        scored: list[tuple[int, object]] = []
        for tool in registry.all_admin():
            if not tool_allowed(tool, perms):  # 越权过滤（第一层，不带出无权工具名）
                continue
            if is_external and getattr(tool, "kind", "read") != "read":
                continue
            hay = (tool.name + " " + (tool.description or "")).lower()
            score = sum(1 for t in terms if t in hay)
            if score:
                scored.append((score, tool))
        scored.sort(key=lambda x: (-x[0], getattr(x[1], "name", "")))
        picked = [t for _, t in scored[:limit]]

        if not picked:
            return ToolResult(
                content=(
                    f"（未找到与「{query}」相关的平台管理工具。）\n"
                    "提示：本工具只检索**平台管理/运维**类工具。若你要做的是普通能力——"
                    "如调用外部 HTTP 接口、读写/生成文件、知识库检索——请**直接使用你已拥有的内置工具**"
                    "（如 http_request / generate_file / knowledge_retrieval 等），不要再调用 find_tools；"
                    "若系统提示里给了某技能的说明，请按该说明的步骤直接执行。"
                ),
                data={"enable_tools": []},
            )
        lines = ["以下工具已为你启用，下一轮可直接调用："]
        for t in picked:
            tag = "写操作(需确认)" if getattr(t, "kind", "read") == "write" else "只读"
            lines.append(f"- {t.name} [{tag}]：{t.description}")
            lines.append(f"  参数: {json.dumps(t.parameters or {}, ensure_ascii=False)[:800]}")
        return ToolResult(
            content="\n".join(lines),
            data={"enable_tools": [t.name for t in picked]},
        )


class LoadSkillTool:
    """按需加载技能集合的子技能：先列目录，再加载完整正文（progressive disclosure）。

    挂载技能集合时，system 里只给"子技能名 + 描述"目录；AI 判断需要哪个后调用本工具
    加载其完整 SKILL.md 正文。也支持不带 parent_id 时列出所有可用技能。
    """

    name = "load_skill"
    description = (
        "加载技能集合中的某个子技能的完整说明。技能集合在系统提示里只给出子技能目录"
        "（名称+描述）；当你判断需要某个子技能的详细说明时，用本工具加载它的完整正文，"
        "然后再按其说明操作。参数传 skill_id（加载正文）或 parent_id（列出该集合的子技能目录）。"
    )
    required_permission = "skill:read"
    kind = "read"
    parameters = {
        "type": "object",
        "properties": {
            "skill_id": {"type": "integer", "description": "要加载正文的子技能 id"},
            "parent_id": {"type": "integer", "description": "列出该技能集合的子技能目录"},
        },
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        from sqlalchemy import select

        from app.models import Skill

        # 列目录
        if args.get("parent_id") is not None:
            try:
                pid = int(args["parent_id"])
            except (TypeError, ValueError):
                return ToolResult(content="parent_id 非法", is_error=True)
            rows = (await ctx.db.execute(
                select(Skill).where(Skill.parent_id == pid, Skill.tenant_id == ctx.tenant_id,
                                    Skill.status == "active")
            )).scalars().all()
            if not rows:
                return ToolResult(content=f"技能集合 #{pid} 没有可用子技能", is_error=True)
            lines = [f"技能集合 #{pid} 的子技能："]
            for s in rows:
                lines.append(f"- [{s.id}] {s.name}：{s.description or ''}")
            lines.append("用 load_skill(skill_id) 加载其完整正文。")
            return ToolResult(content="\n".join(lines))

        # 加载正文
        sid = args.get("skill_id")
        if sid is None:
            return ToolResult(content="请提供 skill_id（加载正文）或 parent_id（列目录）", is_error=True)
        try:
            sid = int(sid)
        except (TypeError, ValueError):
            return ToolResult(content="skill_id 非法", is_error=True)
        sk = await ctx.db.get(Skill, sid)
        if not sk or sk.tenant_id != ctx.tenant_id or sk.status != "active":
            return ToolResult(content=f"技能 #{sid} 不存在或无权访问", is_error=True)
        if not sk.body_md and not sk.prompt_template:
            return ToolResult(content=f"技能「{sk.name}」没有可加载的正文", is_error=True)
        body = sk.body_md or sk.prompt_template or ""
        return ToolResult(content=f"【技能：{sk.name}】\n{body}", data={"skill_id": sk.id, "name": sk.name})
