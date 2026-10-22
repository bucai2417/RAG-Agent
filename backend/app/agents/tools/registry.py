"""工具注册表：内置工具注册 + 按 Agent/Mode 动态组装可用工具集。"""
from __future__ import annotations

from typing import Any

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.tools.base import Tool, ToolContext, ToolResult
from app.agents.tools.builtin import HttpRequestTool, KnowledgeRetrievalTool


class _Registry:
    def __init__(self) -> None:
        self._builtin: dict[str, Tool] = {}
        self._admin: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._builtin[tool.name] = tool

    def register_admin(self, tool: Tool) -> None:
        self._admin[tool.name] = tool

    def get_builtin(self, name: str) -> Tool | None:
        return self._builtin.get(name)

    def get_admin(self, name: str) -> Tool | None:
        return self._admin.get(name)

    def all_builtin(self) -> list[Tool]:
        return list(self._builtin.values())

    def all_admin(self) -> list[Tool]:
        return list(self._admin.values())


registry = _Registry()
registry.register(KnowledgeRetrievalTool())
registry.register(HttpRequestTool())

# 平台能力/权限查询工具（全员可用，只读自身权限）
from app.agents.tools.builtin import CheckCapabilityTool  # noqa: E402

registry.register(CheckCapabilityTool())

# 工具路由元工具：按需检索管理/运维工具（问答页默认不下发全部工具以省 token）
from app.agents.tools.builtin import FindToolsTool  # noqa: E402

registry.register(FindToolsTool())

# 技能按需加载：技能集合在 system 只给目录，AI 用本工具加载子技能正文
from app.agents.tools.builtin import LoadSkillTool  # noqa: E402

registry.register(LoadSkillTool())

# 文件处理工具（read/write；read 直接执行，write 走 HITL）
from app.agents.tools.file_tools import FILE_TOOLS  # noqa: E402

for _ft in FILE_TOOLS:
    registry.register(_ft)

# 办公文档生成工具（结构化模板：会议纪要/周报/待办/公文）
from app.agents.tools.office_tools import OFFICE_TOOLS  # noqa: E402

for _ot in OFFICE_TOOLS:
    registry.register(_ot)

# 办公文档结构化读写（按元素地址精确定位与编辑）
from app.agents.tools.office_struct_tools import OFFICE_STRUCT_TOOLS  # noqa: E402

for _ost in OFFICE_STRUCT_TOOLS:
    registry.register(_ost)

# 浏览器控制（CDP 直连本机调试端口，复用已登录会话）
from app.agents.tools.browser_tools import BROWSER_TOOLS  # noqa: E402

for _bt in BROWSER_TOOLS:
    registry.register(_bt)

# 用户级个人工具（待办提醒 / 我的会话）：高频，常驻下发，无需 find_tools 两跳
from app.agents.tools.complement_tools import PERSONAL_TOOLS  # noqa: E402

for _pt in PERSONAL_TOOLS:
    registry.register(_pt)

# 产出型工具：把「用户提供的数据」转成结果（如文本→结构化台账）。与 analyze_table 同类，
# 必须常驻——否则模型看不到工具，只能手工排版+反问（实测踩过的坑）。
from app.agents.tools.ops_tools2 import ExtractRecordsTool  # noqa: E402

registry.register(ExtractRecordsTool())

# 个人邮件管理（用户级：读写自己的邮箱）：常驻，AI 默认能收发邮件
from app.agents.tools.mail_tools import ManageMailTool  # noqa: E402

registry.register(ManageMailTool())

# 管理类工具（read/write；需 agent.tool_config.admin.enabled 显式启用）
from app.agents.tools.admin_tools import ADMIN_TOOLS  # noqa: E402

for _t in ADMIN_TOOLS:
    registry.register_admin(_t)


# ---- 从 Skill.tool_def 构造工具 ----
class HttpDefTool:
    """由技能 tool_def 定义的 HTTP 工具。"""

    required_permission = "tool:invoke"
    kind = "write"

    def __init__(self, name: str, description: str, parameters: dict, http: dict) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self._http = http

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        import httpx

        from app.agents.tools.builtin import _check_url

        url = self._http.get("url", "").format(**args) if args else self._http.get("url", "")
        method = self._http.get("method", "GET").upper()
        headers = self._http.get("headers") or {}
        body_tpl = self._http.get("body_template")
        body = body_tpl.format(**args) if body_tpl and args else body_tpl

        err = _check_url(url, self._http.get("allow_hosts"))
        if err:
            return ToolResult(content=f"请求被拒绝：{err}", is_error=True)
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                resp = await client.request(
                    method, url, headers=headers, content=body.encode() if body else None
                )
            return ToolResult(content=resp.text[:65536], data={"status": resp.status_code})
        except Exception as e:  # noqa: BLE001
            return ToolResult(content=f"请求失败：{str(e)[:200]}", is_error=True)


def build_tool_from_def(slug: str, tool_def: dict) -> Tool | None:
    impl = tool_def.get("impl", "http")
    if impl != "http":
        return None
    name = tool_def.get("name") or f"skill_{slug}"
    return HttpDefTool(
        name=name,
        description=tool_def.get("description", ""),
        parameters=tool_def.get("parameters") or {"type": "object", "properties": {}},
        http=tool_def.get("http") or {},
    )


def _mcp_slug(name: str) -> str:
    import re as _re

    s = _re.sub(r"[^a-zA-Z0-9]+", "_", name or "").strip("_").lower()
    return s or "server"


class McpTool:
    """由外部 MCP server 的 tools/list 桥接而来的工具。

    name = mcp_{server_slug}_{tool}（冲突时由调用方加 server_id 后缀）。
    parameters 直接透传 MCP 的 inputSchema。
    """

    required_permission = "mcp:invoke"
    kind = "write"  # 服从现有 allow_auto_write：关闭时走 HITL 确认

    def __init__(
        self, *, name: str, description: str, parameters: dict,
        server_id: int, tool_name: str,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters or {"type": "object", "properties": {}}
        self.server_id = server_id
        self.tool_name = tool_name

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import McpServer
        from app.services.mcp_client import McpError, build_client, content_to_text

        server = await ctx.db.get(McpServer, self.server_id)
        if not server or server.tenant_id != ctx.tenant_id or not server.enabled:
            return ToolResult(content="MCP server 不可用", is_error=True)
        try:
            client = await build_client(server, db=ctx.db)
            result = await client.call_tool(self.tool_name, args or {})
        except McpError as e:
            return ToolResult(content=f"MCP 调用失败：{str(e)[:300]}", is_error=True)
        except Exception as e:  # noqa: BLE001
            return ToolResult(content=f"MCP 调用异常：{str(e)[:200]}", is_error=True)
        is_err = bool(result.get("isError")) if isinstance(result, dict) else False
        return ToolResult(content=content_to_text(result), is_error=is_err)


def build_mcp_tools(server, *, seen: set[str]) -> list[McpTool]:
    """把某个 MCP server 缓存的工具清单构造为 McpTool 列表。"""
    tools: list[McpTool] = []
    base = f"mcp_{_mcp_slug(server.name)}"
    for item in server.tools_cache or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        tname = f"{base}_{_mcp_slug(item['name'])}"
        if tname in seen:
            tname = f"{tname}_{server.id}"
        if tname in seen:
            continue
        seen.add(tname)
        tools.append(McpTool(
            name=tname,
            description=item.get("description") or f"MCP 工具 {item['name']}",
            parameters=item.get("inputSchema") or {"type": "object", "properties": {}},
            server_id=server.id,
            tool_name=item["name"],
        ))
    return tools


def _script_runtime(script_path: str) -> str:
    from app.services.skill_runtime import runtime_of

    return runtime_of(script_path)


def build_bridge_env(*, tenant_id: int, user_id: int, skill_id: int,
                     allowed_tools: list[str] | None = None) -> dict:
    """构造技能脚本回调平台的能力桥接环境变量。

    脚本据此可用 PLATFORM_BASE_URL + PLATFORM_SKILL_TOKEN 调 /skills/bridge/call
    执行白名单内的**只读**平台工具，避免技能内重复实现 HTTP 且绕过 SSRF 守卫。
    """
    import os

    from app.core.security import create_skill_token

    base = os.environ.get("PLATFORM_PUBLIC_BASE_URL") or f"http://127.0.0.1:{os.environ.get('API_PORT', '6677')}"
    token = create_skill_token(tenant_id=tenant_id, user_id=user_id, skill_id=skill_id,
                               allowed_tools=allowed_tools or [])
    return {
        "PLATFORM_BASE_URL": f"{base}/api/v1",
        "PLATFORM_SKILL_TOKEN": token,
        "PLATFORM_SKILL_ID": str(skill_id),
    }


# 技能脚本执行的全局并发闸门：防单个租户/技能打满 CPU。
# 惰性初始化（绑定到当前事件循环），进程内所有技能脚本共用。
_SCRIPT_SEM: "object | None" = None
_SCRIPT_SEM_LIMIT = 4


def _script_semaphore():
    import asyncio

    global _SCRIPT_SEM
    if _SCRIPT_SEM is None:
        _SCRIPT_SEM = asyncio.Semaphore(_SCRIPT_SEM_LIMIT)
    return _SCRIPT_SEM


async def run_pack_script(
    pack_dir: str, script_path: str, args: dict, *, timeout: int = 120,
    extra_args: list[str] | None = None, entry: dict | None = None,
    bridge: dict | None = None,
) -> ToolResult:
    """执行技能包内的入口脚本（受限子进程，供 ScriptTool 与 REST 试跑共用）。

    运行时由 skill_runtime 注册表决定（Python/Node/Shell）；`entry` 为入口元数据
    （含 runtime/args 约定/outputs），缺省按扩展名推断。

    - args 约定：stdin（默认，JSON 经 stdin）| positional（追加到 argv）| none。
    - 超时：entry.timeout 覆盖默认（上限 600s）。
    - 安全：独立子进程、硬超时、全局并发闸门、环境变量白名单、工作目录限定在技能包内。
    - 桥接：bridge 非空时注入 PLATFORM_BASE_URL / PLATFORM_SKILL_TOKEN，供脚本回调平台工具。
    """
    import asyncio
    import json

    from app.core.config import settings
    from app.services import skill_runtime as sr

    root = (settings.skill_pack_path / pack_dir).resolve()
    script = (root / script_path).resolve()
    if root not in script.parents or not script.is_file():
        return ToolResult(content="脚本文件不存在", is_error=True)

    entry = entry or {}
    runtime_name = entry.get("runtime") or sr.runtime_of(script_path)
    rt = sr.get_runtime(runtime_name)
    if rt is None:
        return ToolResult(content=f"不支持的脚本类型：{script_path}", is_error=True)

    # 依赖就绪检查（如 Node 缺 node_modules）
    ok, why = rt.check_deps(root)
    if not ok:
        return ToolResult(content=f"{why}。{rt.install_hint()}", is_error=True)

    # 超时：入口声明优先（夹在 5~600s）
    eff_timeout = timeout
    if entry.get("timeout"):
        try:
            eff_timeout = max(5, min(600, int(entry["timeout"])))
        except (TypeError, ValueError):
            eff_timeout = timeout

    # 参数约定：extra_args 始终追加到 argv；positional 模式下 args 也作为位置参数
    arg_mode = str(entry.get("args") or "stdin")
    eff_extra = [str(x) for x in (extra_args or [])]
    if arg_mode == "positional" and args and not eff_extra:
        eff_extra = [json.dumps(args, ensure_ascii=False)]

    env = sr.build_child_env(runtime=rt, extra={**({"PPT_SKILL_ARGS": json.dumps(args, ensure_ascii=False)}), **(bridge or {})})

    ctx = sr.RunContext(root=root, script=script, args=args, extra_args=eff_extra,
                        timeout=eff_timeout, env_extra=env)

    try:
        cmd = rt.build_cmd(ctx)
    except RuntimeError as e:
        return ToolResult(content=str(e), is_error=True)

    def _exec() -> tuple[int, str]:
        import subprocess

        from app.services.skill_limits import Limits, apply_job_limits, make_popen_kwargs

        lim = Limits(cpu_seconds=max(eff_timeout, 30), mem_mb=2048, max_procs=64)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(root), env=env, **make_popen_kwargs(lim),
        )
        apply_job_limits(proc, lim)  # Windows：Job Object 限内存/进程
        try:
            out_b, err_b = proc.communicate(
                input=json.dumps(args, ensure_ascii=False).encode(), timeout=eff_timeout
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return -9, f"__TIMEOUT__{eff_timeout}"
        out = out_b.decode("utf-8", errors="ignore")
        err = err_b.decode("utf-8", errors="ignore")
        if proc.returncode != 0:
            tail = (err or out)[-2000:]
            extra = "（若为内存/CPU 超限，子进程会被强制终止）" if proc.returncode not in (1,) else ""
            return proc.returncode, f"脚本退出码 {proc.returncode}{extra}\n{tail}"
        return 0, (out or err)[:65536] or "(无输出)"

    try:
        # 全局并发闸门：超出并发上限时排队等待
        async with _script_semaphore():
            code, result = await asyncio.wait_for(
                asyncio.to_thread(_exec), timeout=eff_timeout + 5
            )
        if result.startswith("__TIMEOUT__"):
            return ToolResult(content=f"脚本执行超时（超过 {result[11:]}s 已终止）", is_error=True)
        return ToolResult(content=result, is_error=code != 0)
    except asyncio.TimeoutError:
        return ToolResult(content=f"脚本执行超时（{eff_timeout}s）", is_error=True)
    except Exception as e:  # noqa: BLE001
        return ToolResult(content=f"脚本执行错误: {str(e)[:200]}", is_error=True)


class ScriptTool:
    """技能包内的入口脚本工具（受限子进程执行，默认禁用）。

    支持 Python / Node / Shell（按扩展名）。约定：参数经 stdin(JSON) 与 env 传入，
    向 stdout 输出结果。安全：独立子进程、硬超时、工作目录限定在技能包内。
    """

    required_permission = "skill:execute"
    kind = "write"

    def __init__(self, *, name: str, description: str, pack_dir: str, script_path: str,
                 entry: dict | None = None) -> None:
        self.name = name
        self.description = description
        self.pack_dir = pack_dir
        self.script_path = script_path
        self.entry = entry or {}
        self.parameters = {"type": "object", "properties": {}, "additionalProperties": True}

    async def run(self, args: dict, ctx) -> ToolResult:
        return await run_pack_script(self.pack_dir, self.script_path, args, entry=self.entry)


def tool_allowed(tool: Tool, perms: set[str]) -> bool:
    """工具级权限判定：未声明 required_permission 的工具默认不暴露。"""
    req = getattr(tool, "required_permission", "__deny__")
    if req == "__deny__":
        # 未声明权限的工具（含第三方/历史工具）默认不暴露，需显式声明
        return "*" in perms
    return req is None or "*" in perms or req in perms


def resolve_admin_pool(*, perms: set[str], is_external: bool = False) -> dict[str, object]:
    """构建 find_tools 的待命池：权限/敏感度过滤后的管理/运维工具。

    与 resolve_tools 的 admin 分支同款过滤，但**不下发** schema 给 LLM，
    仅在 AI 调 find_tools 命中后由 react 循环按需启用（省 token）。
    """
    pool: dict[str, object] = {}
    for t in registry.all_admin():
        if is_external and getattr(t, "kind", "read") != "read":
            continue
        if tool_allowed(t, perms):
            pool[t.name] = t
    return pool


async def resolve_tools(
    db: AsyncSession,
    *,
    tool_config: dict | None,
    skill_ids: list[int] | None,
    tenant_id: int,
    perms: set[str] | None = None,
    is_external: bool = False,
) -> list[Tool]:
    """按 agent/mode 的 tool_config 与技能，组装**当前用户有权调用**的工具集。

    is_external=True（外部客户/渠道用户）：只装 **kind=="read"** 的工具，
    任何 write/admin 工具一律不放行——即使被误授权也无法调用写操作。
    """
    from app.models import Skill

    if perms is None:
        perms = set()
    cfg = tool_config or {}
    tools: list[Tool] = []
    seen: set[str] = set()

    def _ok(t) -> bool:
        if is_external and getattr(t, "kind", "read") != "read":
            return False
        return tool_allowed(t, perms)

    # 内置工具
    for name, c in (cfg.get("builtin") or {}).items():
        if isinstance(c, dict) and c.get("enabled"):
            t = registry.get_builtin(name)
            if t and t.name not in seen and _ok(t):
                tools.append(t)
                seen.add(t.name)

    # 管理类工具（仅当 agent 显式启用 admin 工具，且用户有权限；外部客户一律不给）
    if (cfg.get("admin") or {}).get("enabled") and not is_external:
        allow = set((cfg.get("admin") or {}).get("tools") or [])
        for t in registry.all_admin():
            if t.name in seen:
                continue
            if allow and t.name not in allow:
                continue
            if tool_allowed(t, perms):
                tools.append(t)
                seen.add(t.name)

    # 外部 MCP 工具（agent 启用 mcp 且用户有 mcp:invoke）
    mcp_cfg = cfg.get("mcp") or {}
    if mcp_cfg.get("enabled"):
        from sqlalchemy import select as _select

        from app.models import McpServer

        wanted = [int(x) for x in (mcp_cfg.get("server_ids") or [])]
        q = _select(McpServer).where(
            McpServer.tenant_id == tenant_id, McpServer.enabled.is_(True)
        )
        if wanted:
            q = q.where(McpServer.id.in_(wanted))
        servers = (await db.execute(q)).scalars().all()
        for sv in servers:
            for t in build_mcp_tools(sv, seen=seen):
                if _ok(t):
                    tools.append(t)
                else:
                    seen.discard(t.name)

    # 技能带来的工具（kind=tool）+ 技能包脚本
    if skill_ids:
        rows = (
            await db.execute(
                select(Skill).where(
                    Skill.id.in_(skill_ids),
                    Skill.tenant_id == tenant_id,
                    Skill.status == "active",
                )
            )
        ).scalars().all()
        for sk in rows:
            # 挂了技能集合 → 自动装配 load_skill（AI 用它加载子技能正文）
            if sk.kind == "collection":
                lt = registry.get_builtin("load_skill")
                if lt and "load_skill" not in seen and _ok(lt):
                    tools.append(lt)
                    seen.add("load_skill")
            if sk.kind == "tool" and sk.tool_def:
                t = build_tool_from_def(sk.slug, sk.tool_def)
                if t and t.name not in seen and _ok(t):
                    tools.append(t)
                    seen.add(t.name)
            # 技能包脚本（仅当 package.scripts_enabled 才注册为可执行工具）
            if sk.source == "package" and sk.package_id:
                from app.models import SkillPackage

                pkg = await db.get(SkillPackage, sk.package_id)
                if pkg and pkg.scripts_enabled:
                    for scr in pkg.entry_scripts or []:
                        tname = f"skill_{sk.slug}_{scr['name']}"
                        if tname in seen:
                            continue
                        t = ScriptTool(
                            name=tname,
                            description=scr.get("description") or f"技能脚本 {scr['name']}",
                            pack_dir=pkg.pack_dir,
                            script_path=scr["path"],
                            entry=scr,
                        )
                        if not _ok(t):
                            continue
                        tools.append(t)
                        seen.add(tname)
    return tools


def to_openai_schema(tools: list[Tool]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]
