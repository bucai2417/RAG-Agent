"""AI 操作工具（第二轮）：智能体版本/模式、技能、工具台、模型、KB、文档ACL、
渠道、RBAC 补全、webhook、工作流。

action 型工具：同类操作合并（manage_*），带清晰 enum 描述，避免 tool 爆炸。
"""
from __future__ import annotations

from sqlalchemy import delete as _delete
from sqlalchemy import select

from app.agents.tools.base import ToolContext, ToolResult, WriteToolMixin


# ==================== 智能体：克隆 / 版本 / 模式 ====================
class CloneAgentTool(WriteToolMixin):
    name = "clone_agent"
    description = "复制智能体（含其行为模式）。"
    required_permission = "agent:edit"
    parameters = {"type": "object", "properties": {"agent_id": {"type": "integer"}}, "required": ["agent_id"]}

    def summarize(self, args: dict) -> str:
        return f"复制智能体 #{args.get('agent_id')}"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import Agent, AgentMode

        src = await ctx.db.get(Agent, int(args.get("agent_id") or 0))
        if not src or src.tenant_id != ctx.tenant_id:
            return ToolResult(content="智能体不存在", is_error=True)
        base_slug = f"{src.slug}-copy"
        slug = base_slug
        n = 1
        while (await ctx.db.execute(select(Agent).where(
                Agent.tenant_id == ctx.tenant_id, Agent.slug == slug))).scalar_one_or_none():
            n += 1
            slug = f"{base_slug}-{n}"
        new = Agent(tenant_id=ctx.tenant_id, owner_id=ctx.user_id, name=f"{src.name} 副本", slug=slug,
                    description=src.description, icon=src.icon, type=src.type,
                    system_prompt=src.system_prompt, model_config_id=src.model_config_id,
                    kb_ids=src.kb_ids, skill_ids=src.skill_ids, tool_config=src.tool_config,
                    config=src.config, status="draft")
        ctx.db.add(new)
        await ctx.db.flush()
        for m in (await ctx.db.execute(select(AgentMode).where(AgentMode.agent_id == src.id))).scalars().all():
            ctx.db.add(AgentMode(tenant_id=ctx.tenant_id, agent_id=new.id, name=m.name,
                                 description=m.description, system_prompt=m.system_prompt,
                                 skill_ids=m.skill_ids, tool_config=m.tool_config, kb_ids=m.kb_ids,
                                 params=m.params, is_default=m.is_default, sort=m.sort))
        await ctx.db.flush()
        return ToolResult(content=f"已复制为智能体 #{new.id}「{new.name}」", data={"id": new.id})


class ManageAgentVersionTool(WriteToolMixin):
    name = "manage_agent_version"
    description = "智能体版本管理：action=snapshot(创建快照) / rollback(回滚到某版本)。"
    required_permission = "agent:edit"
    parameters = {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["snapshot", "rollback"]},
                       "agent_id": {"type": "integer"}, "version_id": {"type": "integer"},
                       "note": {"type": "string"}},
        "required": ["action", "agent_id"],
    }

    def summarize(self, args: dict) -> str:
        return f"{'创建版本快照' if args.get('action') == 'snapshot' else '回滚版本'} 智能体 #{args.get('agent_id')}"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from sqlalchemy import func

        from app.models import Agent, AgentVersion

        a = await ctx.db.get(Agent, int(args.get("agent_id") or 0))
        if not a or a.tenant_id != ctx.tenant_id:
            return ToolResult(content="智能体不存在", is_error=True)
        if args.get("action") == "snapshot":
            cur = (await ctx.db.execute(select(func.max(AgentVersion.version)).where(
                AgentVersion.agent_id == a.id))).scalar_one()
            snap = {"name": a.name, "system_prompt": a.system_prompt, "kb_ids": a.kb_ids,
                    "skill_ids": a.skill_ids, "tool_config": a.tool_config, "config": a.config,
                    "model_config_id": a.model_config_id, "type": a.type}
            v = AgentVersion(tenant_id=ctx.tenant_id, agent_id=a.id, version=(cur or 0) + 1,
                             snapshot=snap, note=args.get("note"), created_by=ctx.user_id)
            ctx.db.add(v)
            await ctx.db.flush()
            return ToolResult(content=f"已创建版本快照 v{v.version}")
        # rollback
        v = await ctx.db.get(AgentVersion, int(args.get("version_id") or 0))
        if not v or v.agent_id != a.id:
            return ToolResult(content="版本不存在", is_error=True)
        snap = v.snapshot or {}
        for f in ("name", "system_prompt", "kb_ids", "skill_ids", "tool_config", "config",
                  "model_config_id", "type"):
            if f in snap:
                setattr(a, f, snap[f])
        await ctx.db.flush()
        return ToolResult(content=f"已回滚到版本 v{v.version}")


class ManageAgentModeTool(WriteToolMixin):
    name = "manage_agent_mode"
    description = "智能体模式的增删改：action=create/update/delete。模式可覆盖提示词/技能/知识库/参数。"
    required_permission = "agent:edit"
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "update", "delete"]},
            "agent_id": {"type": "integer"}, "mode_id": {"type": "integer"},
            "name": {"type": "string"}, "description": {"type": "string"},
            "system_prompt": {"type": "string"}, "kb_ids": {"type": "array", "items": {"type": "integer"}},
            "skill_ids": {"type": "array", "items": {"type": "integer"}},
            "is_default": {"type": "boolean"},
        }, "required": ["action", "agent_id"],
    }

    def summarize(self, args: dict) -> str:
        act = {"create": "创建", "update": "修改", "delete": "删除"}.get(args.get("action"), "")
        return f"{act}智能体 #{args.get('agent_id')} 的模式" + (f"「{args.get('name')}」" if args.get("name") else "")

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import Agent, AgentMode

        a = await ctx.db.get(Agent, int(args.get("agent_id") or 0))
        if not a or a.tenant_id != ctx.tenant_id:
            return ToolResult(content="智能体不存在", is_error=True)
        action = args.get("action")
        if action == "create":
            m = AgentMode(tenant_id=ctx.tenant_id, agent_id=a.id, name=str(args.get("name") or "新模式"),
                          description=args.get("description"), system_prompt=args.get("system_prompt"),
                          kb_ids=args.get("kb_ids"), skill_ids=args.get("skill_ids"),
                          is_default=bool(args.get("is_default")))
            ctx.db.add(m)
            await ctx.db.flush()
            return ToolResult(content=f"已创建模式 #{m.id}", data={"id": m.id})
        m = await ctx.db.get(AgentMode, int(args.get("mode_id") or 0))
        if not m or m.agent_id != a.id:
            return ToolResult(content="模式不存在", is_error=True)
        if action == "delete":
            await ctx.db.delete(m)
            await ctx.db.flush()
            return ToolResult(content="已删除模式")
        for f in ("name", "description", "system_prompt", "kb_ids", "skill_ids", "is_default"):
            if args.get(f) is not None:
                setattr(m, f, args[f])
        await ctx.db.flush()
        return ToolResult(content=f"已更新模式 #{m.id}")


# ==================== 技能 ====================
class UpdateSkillTool(WriteToolMixin):
    name = "update_skill"
    description = "修改技能（name/description/prompt_template/icon）。"
    required_permission = "skill:edit"
    parameters = {
        "type": "object",
        "properties": {"skill_id": {"type": "integer"}, "name": {"type": "string"},
                       "description": {"type": "string"}, "prompt_template": {"type": "string"},
                       "icon": {"type": "string"}},
        "required": ["skill_id"],
    }

    def summarize(self, args: dict) -> str:
        return f"修改技能 #{args.get('skill_id')}"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import Skill

        s = await ctx.db.get(Skill, int(args.get("skill_id") or 0))
        if not s or s.tenant_id != ctx.tenant_id:
            return ToolResult(content="技能不存在", is_error=True)
        for f in ("name", "description", "prompt_template", "icon"):
            if args.get(f) is not None:
                setattr(s, f, args[f])
        await ctx.db.flush()
        return ToolResult(content=f"已更新技能 #{s.id}")


class DeleteSkillTool(WriteToolMixin):
    name = "delete_skill"
    description = "删除技能。"
    required_permission = "skill:edit"
    dangerous = True
    parameters = {"type": "object", "properties": {"skill_id": {"type": "integer"}}, "required": ["skill_id"]}

    def summarize(self, args: dict) -> str:
        return f"删除技能 #{args.get('skill_id')}"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import Skill

        s = await ctx.db.get(Skill, int(args.get("skill_id") or 0))
        if not s or s.tenant_id != ctx.tenant_id:
            return ToolResult(content="技能不存在", is_error=True)
        s.status = "deleted"
        await ctx.db.flush()
        return ToolResult(content=f"已删除技能 #{s.id}")


class FindSkillTool:
    """搜索可安装的技能（GitHub 实时搜索）。只读，直接执行。"""

    name = "find_skill"
    description = (
        "按关键词搜索可安装的 AI 技能（SKILL.md 技能包）。当用户说『找技能/搜索技能/有没有能查XX的技能』时"
        "**先调用本工具**，不要自己拼 GitHub 搜索 URL——自行搜索会因限流与中文召回问题失败。"
        "返回候选仓库（描述/stars/子技能目录）；多技能仓库安装时要带上 subpath。"
    )
    required_permission = "skill:read"
    kind = "read"
    parameters = {
        "type": "object",
        "properties": {
            "keywords": {"type": "string", "description": "要查找的能力，如『油价』『天气查询』『新闻』"},
            "limit": {"type": "integer", "description": "返回候选数，默认 5"},
        },
        "required": ["keywords"],
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.services import skill_search_service as sss

        kw = str(args.get("keywords") or "").strip()
        if not kw:
            return ToolResult(content="请提供搜索关键词（如『油价』）", is_error=True)
        try:
            token = None
            try:
                from app.core.config import settings

                token = (getattr(settings, "github_token", "") or "").strip() or None
            except Exception:  # noqa: BLE001
                token = None
            r = await sss.search_skills(kw, token=token, limit=int(args.get("limit") or 5))
        except Exception as e:  # noqa: BLE001
            return ToolResult(content=f"技能搜索出错：{str(e)[:150]}", is_error=True)

        if r.get("error"):
            return ToolResult(content=f"技能搜索失败：{r['error']}", is_error=True)

        cands = r.get("candidates") or []
        if not cands:
            note = r.get("note") or ""
            return ToolResult(content=f"未找到与「{kw}」相关的技能。{note}", data={"count": 0})

        lines = [f"为「{kw}」找到 {len(cands)} 个候选（按相关度/star 排序）："]
        for i, c in enumerate(cands, 1):
            lines.append(f"{i}. {c.repo} ⭐{c.stars} — {c.description[:120]}")
            if c.sub_skills:
                subs = c.sub_skills
                shown = "、".join(s or "(根)" for s in subs[:6])
                more = f" …等 {len(subs)} 个" if len(subs) > 6 else ""
                lines.append(f"   📁 子技能：{shown}{more}（多技能仓库，安装需带 subpath）")
        lines.append("")
        lines.append("安装：调用 import_skill_from_url(url=候选仓库URL, subpath=子技能目录)。"
                     "单技能仓库不必带 subpath。导入前先让用户确认要装哪个。")
        if r.get("note"):
            lines.append(f"提示：{r['note']}")
        return ToolResult(
            content="\n".join(lines),
            data={"count": len(cands), "candidates": [
                {"repo": c.repo, "stars": c.stars, "description": c.description,
                 "url": c.html_url, "sub_skills": c.sub_skills[:20]} for c in cands]},
        )


class ImportSkillFromUrlTool(WriteToolMixin):
    name = "import_skill_from_url"
    description = ("从 URL / GitHub 仓库导入技能包（需 zip 直链或 https://github.com/用户/仓库）。"
                   "多技能仓库（一个 repo 含多个 SKILL.md）必须用 subpath 指定子技能目录，"
                   "如 subpath='skills/data-query'。可用 find_skill 先搜索候选。")
    required_permission = "skill:edit"
    parameters = {"type": "object", "properties": {
        "url": {"type": "string"},
        "subpath": {"type": "string", "description": "多技能仓库时指定子技能目录（如 skills/data-query）；单技能留空"},
    }, "required": ["url"]}

    def summarize(self, args: dict) -> str:
        sp = args.get("subpath")
        return f"从 URL 导入技能包：{args.get('url')}" + (f"（子技能 {sp}）" if sp else "")

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        url = str(args.get("url") or "").strip()
        if not url:
            return ToolResult(content="url 不能为空", is_error=True)
        subpath = (str(args.get("subpath") or "").strip() or None)
        from app.services import skill_pack_service

        try:
            r = await skill_pack_service.import_from_url(
                ctx.db, tenant_id=ctx.tenant_id, owner_id=ctx.user_id, url=url, subpath=subpath,
            )
        except ValueError as e:
            return ToolResult(content=f"导入失败：{e}", is_error=True)
        if r.get("multi_skill"):
            subs = "、".join(s or "(根)" for s in (r.get("sub_skills") or []))
            return ToolResult(
                content=(f"该仓库含多个技能：{subs}。请让用户选择，然后用 "
                         f"import_skill_from_url(url='{url}', subpath='<选定的子技能目录>') 再导入。"),
                is_error=False,
            )
        if r.get("duplicate"):
            return ToolResult(
                content=f"该技能包已存在（技能 #{r.get('existing_skill_id')} 「{r.get('existing_skill_name')}」），"
                        f"如需覆盖请用 update_skill 或到技能页更新。",
                is_error=False,
            )
        return ToolResult(content=f"已导入技能「{r.get('name')}」(#{r.get('skill_id')})，{r.get('files')} 个文件",
                          data={"skill_id": r.get("skill_id")})


class ToggleSkillScriptsTool(WriteToolMixin):
    name = "toggle_skill_scripts"
    description = "启用/停用技能包脚本执行（skill_id/enabled）。"
    required_permission = "skill:edit"
    parameters = {
        "type": "object",
        "properties": {"skill_id": {"type": "integer"}, "enabled": {"type": "boolean"}},
        "required": ["skill_id", "enabled"],
    }

    def summarize(self, args: dict) -> str:
        return f"{'启用' if args.get('enabled') else '停用'}技能 #{args.get('skill_id')} 的脚本执行"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import SkillPackage

        pkg = (await ctx.db.execute(select(SkillPackage).where(
            SkillPackage.skill_id == int(args.get("skill_id") or 0)))).scalars().first()
        if not pkg:
            return ToolResult(content="技能包不存在", is_error=True)
        pkg.scripts_enabled = bool(args.get("enabled"))
        await ctx.db.flush()
        return ToolResult(content=f"已{'启用' if pkg.scripts_enabled else '停用'}脚本执行")


class GcSkillPacksTool(WriteToolMixin):
    name = "gc_skill_packs"
    description = ("清理技能包磁盘垃圾：回收「磁盘上有、DB 无记录」的孤儿解压目录（异常中断/历史遗留）。"
                   "默认保留 1 小时内新建的目录（可能正在入库）。dry_run=true 仅统计不删除。")
    required_permission = "skill:edit"
    parameters = {
        "type": "object",
        "properties": {
            "min_age_hours": {"type": "integer", "default": 1, "description": "只回收早于该时长的孤儿目录"},
            "dry_run": {"type": "boolean", "default": False, "description": "true=仅统计，不实际删除"},
        },
    }

    def summarize(self, args: dict) -> str:
        return f"{'统计' if args.get('dry_run') else '清理'}孤儿技能包目录"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.services.skill_pack_service import gc_orphan_packs

        r = await gc_orphan_packs(ctx.db, min_age_hours=int(args.get("min_age_hours") or 1),
                                  dry_run=bool(args.get("dry_run")))
        verb = "可回收" if r.get("dry_run") else "已回收"
        return ToolResult(
            content=f"{verb}孤儿技能包 {r['removed']} 个（保留 {r['kept']} 个），释放 {r['freed_mb']} MB",
            data=r,
        )


# ==================== 通知渠道 ====================
class ManageNotifyChannelTool(WriteToolMixin):
    name = "manage_notify_channel"
    description = ("管理通知渠道（定时任务/工作流结果的通知出口）。"
                   "action=create/update/delete/list/test；"
                   "kind=inapp(站内)/webhook/wecom(企业微信)/dingtalk(钉钉)/smtp(邮件)/external(外部渠道)。"
                   "config 按 kind 传：webhook={url}；wecom/dingtalk={webhook_url,secret?}；"
                   "smtp={host,port,username,password,use_ssl,from_addr,to_addrs}；"
                   "external={channel_id(已接入渠道id), target(接收对象id), target_type(group|user)}"
                   "（外部渠道接收对象 id 可让目标在渠道发 /myuid 获取）。")
    required_permission = "notify:manage"
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "update", "delete", "list", "test"]},
            "channel_id": {"type": "integer"},
            "kind": {"type": "string", "enum": ["inapp", "webhook", "wecom", "dingtalk", "smtp", "external"]},
            "name": {"type": "string"},
            "config": {"type": "object"},
            "events": {"type": "string", "description": "订阅事件，逗号分隔（空=全部）"},
            "enabled": {"type": "boolean"},
        },
        "required": ["action"],
    }
    dangerous = True  # delete 有破坏性

    def summarize(self, args: dict) -> str:
        a = args.get("action")
        label = {"create": "新建", "update": "修改", "delete": "删除", "list": "列出", "test": "测试"}.get(a, a)
        return f"{label}通知渠道 {args.get('name') or ('#' + str(args.get('channel_id')) if args.get('channel_id') else '')}"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.api.v1.notify_channels import _encrypt_config, _mask_config
        from app.models import NotifyChannel
        from app.notifiers.base import NotificationMessage
        from app.notifiers.registry import build_notifier

        action = str(args.get("action") or "")
        cid = int(args.get("channel_id") or 0)

        if action == "list":
            rows = (await ctx.db.execute(
                select(NotifyChannel).where(NotifyChannel.tenant_id == ctx.tenant_id)
            )).scalars().all()
            if not rows:
                return ToolResult(content="当前没有通知渠道（站内消息默认可用）")
            lines = [f"#{c.id} {c.name} [{c.kind}] {'启用' if c.enabled else '停用'}"
                     f"{' 事件=' + c.events if c.events else ''}" for c in rows]
            return ToolResult(content="\n".join(lines))

        if action == "create":
            kind = str(args.get("kind") or "webhook")
            if kind not in ("inapp", "webhook", "wecom", "dingtalk", "smtp", "external"):
                return ToolResult(content="不支持的通知渠道类型", is_error=True)
            c = NotifyChannel(
                tenant_id=ctx.tenant_id, kind=kind,
                name=str(args.get("name") or "通知渠道"),
                config=_encrypt_config(args.get("config")), enabled=bool(args.get("enabled", True)),
                events=args.get("events"),
            )
            ctx.db.add(c)
            await ctx.db.flush()
            return ToolResult(content=f"已创建通知渠道 #{c.id}「{c.name}」")

        c = await ctx.db.get(NotifyChannel, cid)
        if not c or c.tenant_id != ctx.tenant_id:
            return ToolResult(content="通知渠道不存在", is_error=True)

        if action == "update":
            if args.get("name") is not None:
                c.name = args["name"]
            if args.get("events") is not None:
                c.events = args["events"]
            if args.get("enabled") is not None:
                c.enabled = bool(args["enabled"])
            if args.get("config"):
                merged = {**(c.config or {}), **args["config"]}
                c.config = _encrypt_config(merged)
            await ctx.db.flush()
            return ToolResult(content=f"已更新通知渠道 #{c.id}")

        if action == "delete":
            await ctx.db.delete(c)
            await ctx.db.flush()
            return ToolResult(content=f"已删除通知渠道 #{cid}")

        if action == "test":
            cfg = dict(c.config or {})
            if c.kind == "inapp":
                cfg.setdefault("user_id", ctx.user_id)
            try:
                ch = build_notifier(c.kind, tenant_id=ctx.tenant_id, config=cfg)
                ok = await ch.send(NotificationMessage(
                    title="测试通知", body="来自 AI 的测试通知", level="info", kind="system",
                    user_id=ctx.user_id,
                ))
            except Exception as e:  # noqa: BLE001
                return ToolResult(content=f"发送失败：{str(e)[:200]}", is_error=True)
            return ToolResult(content="测试通知已发送" if ok else "发送失败（配置可能不完整）")

        return ToolResult(content=f"未知 action：{action}", is_error=True)


# ==================== 定时任务补充 ====================
class ManageMcpServerTool(WriteToolMixin):
    name = "manage_mcp_server"
    description = ("管理外部 MCP（Model Context Protocol）服务器。"
                   "action=create（新建，需 name + transport(http/sse/stdio) + url 或 command）、"
                   "update（改配置）、delete（删除）、test（测连通）、sync（拉取工具清单）、"
                   "list（列出全部）、list_tools（列出某 server 的工具）。")
    required_permission = "mcp:manage"
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["create", "update", "delete", "test", "sync", "list", "list_tools"]},
            "server_id": {"type": "integer"},
            "name": {"type": "string"},
            "transport": {"type": "string", "enum": ["http", "sse", "stdio"]},
            "url": {"type": "string", "description": "http/sse 的服务地址"},
            "command": {"type": "string", "description": "stdio 命令（首 token 需在白名单内）"},
            "args": {"type": "array", "items": {"type": "string"}},
            "auth_token": {"type": "string"},
            "enabled": {"type": "boolean"},
        },
        "required": ["action"],
    }
    dangerous = True  # delete 有破坏性；create/update 也会改外部连接

    def summarize(self, args: dict) -> str:
        a = args.get("action")
        label = {"create": "新建", "update": "修改", "delete": "删除", "test": "测试",
                 "sync": "同步", "list": "列出", "list_tools": "列出工具"}.get(a, a)
        extra = args.get("name") or (f"#{args.get('server_id')}" if args.get("server_id") else "")
        return f"{label} MCP 服务器 {extra}"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        import time

        from app.core.crypto import encrypt
        from app.models import McpServer

        action = str(args.get("action") or "")
        sid = int(args.get("server_id") or 0)

        if action == "list":
            rows = (await ctx.db.execute(
                select(McpServer).where(McpServer.tenant_id == ctx.tenant_id)
            )).scalars().all()
            if not rows:
                return ToolResult(content="当前没有 MCP 服务器")
            lines = [f"#{s.id} {s.name} [{s.transport}] {s.url or s.command or ''} "
                     f"({len(s.tools_cache or [])} 工具, {'启用' if s.enabled else '停用'})" for s in rows]
            return ToolResult(content="\n".join(lines))

        if action == "create":
            transport = (args.get("transport") or "http").lower()
            if transport not in ("http", "sse", "stdio"):
                return ToolResult(content="transport 只能为 http/sse/stdio", is_error=True)
            if transport == "stdio":
                from app.services.mcp_manager import check_stdio_allowed
                try:
                    check_stdio_allowed(args.get("command") or "")
                except Exception as e:  # noqa: BLE001
                    return ToolResult(content=str(e), is_error=True)
            elif not args.get("url"):
                return ToolResult(content="http/sse 型需提供 url", is_error=True)
            s = McpServer(
                tenant_id=ctx.tenant_id, name=str(args.get("name") or "MCP Server"),
                transport=transport, url=args.get("url"), command=args.get("command"),
                args=args.get("args"), enabled=bool(args.get("enabled", True)),
                auth_token=encrypt(args["auth_token"]) if args.get("auth_token") else None,
                status="active",
            )
            ctx.db.add(s)
            await ctx.db.flush()
            return ToolResult(content=f"已创建 MCP 服务器 #{s.id}「{s.name}」")

        # 需要 server_id 的操作
        s = await ctx.db.get(McpServer, sid)
        if not s or s.tenant_id != ctx.tenant_id:
            return ToolResult(content="MCP 服务器不存在", is_error=True)

        if action == "update":
            for f in ("name", "transport", "url", "command", "args", "enabled"):
                if args.get(f) is not None:
                    setattr(s, f, args[f])
            if args.get("auth_token"):
                s.auth_token = encrypt(args["auth_token"])
            await ctx.db.flush()
            return ToolResult(content=f"已更新 MCP 服务器 #{s.id}")

        if action == "delete":
            from app.services.mcp_manager import mcp_manager

            await mcp_manager.stop(sid)
            await ctx.db.delete(s)
            await ctx.db.flush()
            return ToolResult(content=f"已删除 MCP 服务器 #{sid}")

        if action in ("test", "sync", "list_tools"):
            from app.services.mcp_client import McpError, build_client

            try:
                client = await build_client(s, db=ctx.db)
                await client.initialize()
                tools = await client.list_tools()
                if action == "sync":
                    s.tools_cache = tools
                    s.last_synced_at = int(time.time() * 1000)
                    s.status = "active"
                    s.last_error = None
                    await ctx.db.flush()
                    return ToolResult(content=f"已同步 #{sid} 的 {len(tools)} 个工具")
                lines = [f"- {t.get('name')}: {t.get('description') or ''}" for t in tools]
                return ToolResult(content=f"#{sid}「{s.name}」的工具：\n" + "\n".join(lines))
            except (McpError, Exception) as e:  # noqa: BLE001
                s.status = "error"
                s.last_error = str(e)[:500]
                await ctx.db.flush()
                return ToolResult(content=f"操作失败：{str(e)[:300]}", is_error=True)

        return ToolResult(content=f"未知 action：{action}", is_error=True)


# ==================== 工具台 ====================
class RegisterToolTool(WriteToolMixin):
    name = "register_tool"
    description = "注册自定义 HTTP 工具（name/description/parameters(JSON schema)/source）。"
    required_permission = "tool:manage"
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "description": {"type": "string"},
                       "parameters": {"type": "object"}, "source": {"type": "object"},
                       "display_name": {"type": "string"}},
        "required": ["name", "description"],
    }

    def summarize(self, args: dict) -> str:
        return f"注册工具「{args.get('name')}」"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import Tool

        t = Tool(tenant_id=ctx.tenant_id, name=str(args.get("name")), display_name=args.get("display_name") or "",
                 description=str(args.get("description")), parameters=args.get("parameters"),
                 source=args.get("source"))
        ctx.db.add(t)
        await ctx.db.flush()
        return ToolResult(content=f"已注册工具 #{t.id}", data={"id": t.id})


class DeleteToolTool(WriteToolMixin):
    name = "delete_tool"
    description = "删除自定义工具。"
    required_permission = "tool:manage"
    dangerous = True
    parameters = {"type": "object", "properties": {"tool_id": {"type": "integer"}}, "required": ["tool_id"]}

    def summarize(self, args: dict) -> str:
        return f"删除工具 #{args.get('tool_id')}"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import Tool

        t = await ctx.db.get(Tool, int(args.get("tool_id") or 0))
        if not t or t.tenant_id != ctx.tenant_id:
            return ToolResult(content="工具不存在", is_error=True)
        await ctx.db.delete(t)
        await ctx.db.flush()
        return ToolResult(content=f"已删除工具 #{t.id}")


class TestToolTool:
    name = "test_tool"
    description = "试跑一个自定义工具（tool_id + args）。"
    required_permission = "tool:manage"
    kind = "read"
    parameters = {
        "type": "object",
        "properties": {"tool_id": {"type": "integer"}, "args": {"type": "object"}},
        "required": ["tool_id"],
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.agents.tools.base import ToolContext as TC
        from app.agents.tools.builtin import HttpRequestTool
        from app.models import Tool

        t = await ctx.db.get(Tool, int(args.get("tool_id") or 0))
        if not t or t.tenant_id != ctx.tenant_id:
            return ToolResult(content="工具不存在", is_error=True)
        src = t.source or {}
        http = src.get("http") or {}
        http_tool = HttpRequestTool()
        tctx = TC(db=ctx.db, ps=ctx.ps, tenant_id=ctx.tenant_id, user_id=ctx.user_id,
                  config={"allow_hosts": http.get("allow_hosts"), "timeout": http.get("timeout", 20)})
        return await http_tool.run(args.get("args") or {}, tctx)


# ==================== 模型：Provider / 配置 ====================
class ManageProviderTool(WriteToolMixin):
    name = "manage_provider"
    description = "模型供应商的增删改：action=create/update/delete。"
    required_permission = "model:manage"
    dangerous = True
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "update", "delete"]},
            "provider_id": {"type": "integer"}, "name": {"type": "string"},
            "kind": {"type": "string", "description": "openai/anthropi/ollama/local_hash 等"},
            "base_url": {"type": "string"}, "api_key": {"type": "string"},
            "timeout": {"type": "integer"},
        }, "required": ["action"],
    }

    def summarize(self, args: dict) -> str:
        act = {"create": "创建", "update": "修改", "delete": "删除"}.get(args.get("action"), "")
        return f"{act}模型供应商" + (f"「{args.get('name')}」" if args.get("name") else f" #{args.get('provider_id')}")

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import ModelProvider
        from app.providers.registry import invalidate_cache

        action = args.get("action")
        if action == "create":
            if not args.get("name") or not args.get("base_url"):
                return ToolResult(content="创建 Provider 需要 name 与 base_url", is_error=True)
            p = ModelProvider(tenant_id=ctx.tenant_id, name=str(args["name"]),
                              kind=str(args.get("kind") or "openai"), base_url=str(args["base_url"]),
                              api_key=args.get("api_key"), timeout=int(args.get("timeout") or 60))
            ctx.db.add(p)
            await ctx.db.flush()
            invalidate_cache()
            return ToolResult(content=f"已创建 Provider #{p.id}", data={"id": p.id})
        p = await ctx.db.get(ModelProvider, int(args.get("provider_id") or 0))
        if not p or p.tenant_id != ctx.tenant_id:
            return ToolResult(content="Provider 不存在", is_error=True)
        if action == "delete":
            await ctx.db.delete(p)
            await ctx.db.flush()
            invalidate_cache()
            return ToolResult(content=f"已删除 Provider #{p.id}")
        for f in ("name", "kind", "base_url", "api_key", "timeout"):
            if args.get(f) is not None:
                setattr(p, f, args[f])
        await ctx.db.flush()
        invalidate_cache()
        return ToolResult(content=f"已更新 Provider #{p.id}")


class ManageModelConfigTool(WriteToolMixin):
    name = "manage_model_config"
    description = "模型配置的增删改：action=create/update/delete。"
    required_permission = "model:manage"
    dangerous = True
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "update", "delete"]},
            "config_id": {"type": "integer"}, "provider_id": {"type": "integer"},
            "purpose": {"type": "string", "enum": ["chat", "embedding", "rerank"]},
            "model_name": {"type": "string"}, "display_name": {"type": "string"},
            "embedding_dim": {"type": "integer"}, "is_default": {"type": "boolean"},
        }, "required": ["action"],
    }

    def summarize(self, args: dict) -> str:
        act = {"create": "创建", "update": "修改", "delete": "删除"}.get(args.get("action"), "")
        return f"{act}模型配置" + (f" {args.get('purpose')}/{args.get('model_name')}" if args.get("model_name") else f" #{args.get('config_id')}")

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import ModelConfig
        from app.providers.registry import invalidate_cache

        action = args.get("action")
        if action == "create":
            if not args.get("provider_id") or not args.get("model_name"):
                return ToolResult(content="创建模型配置需要 provider_id 与 model_name", is_error=True)
            mc = ModelConfig(tenant_id=ctx.tenant_id, provider_id=int(args["provider_id"]),
                             purpose=str(args.get("purpose") or "chat"), model_name=str(args["model_name"]),
                             display_name=str(args.get("display_name") or args["model_name"]),
                             embedding_dim=args.get("embedding_dim"),
                             is_default=bool(args.get("is_default")))
            ctx.db.add(mc)
            await ctx.db.flush()
            invalidate_cache()
            return ToolResult(content=f"已创建模型配置 #{mc.id}", data={"id": mc.id})
        mc = await ctx.db.get(ModelConfig, int(args.get("config_id") or 0))
        if not mc or mc.tenant_id != ctx.tenant_id:
            return ToolResult(content="模型配置不存在", is_error=True)
        if action == "delete":
            await ctx.db.delete(mc)
            await ctx.db.flush()
            invalidate_cache()
            return ToolResult(content=f"已删除模型配置 #{mc.id}")
        for f in ("model_name", "display_name", "embedding_dim", "is_default", "purpose"):
            if args.get(f) is not None:
                setattr(mc, f, args[f])
        await ctx.db.flush()
        invalidate_cache()
        return ToolResult(content=f"已更新模型配置 #{mc.id}")


# ==================== 知识库：连接器测试 ====================
class TestKbConnectorTool:
    name = "test_kb_connector"
    description = "测试外部知识库连接（kb_id）。"
    required_permission = "kb:read"
    kind = "read"
    parameters = {"type": "object", "properties": {"kb_id": {"type": "integer"}}, "required": ["kb_id"]}

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.channels.http_guard import assert_safe_url
        from app.connectors.registry import from_kb
        from app.models import KnowledgeBase

        kb = await ctx.db.get(KnowledgeBase, int(args.get("kb_id") or 0))
        if not kb or kb.tenant_id != ctx.tenant_id:
            return ToolResult(content="知识库不存在", is_error=True)
        if kb.source_type != "external":
            return ToolResult(content="该知识库不是外部来源", is_error=True)
        conn = from_kb(kb)
        if conn is None:
            return ToolResult(content="连接器未配置", is_error=True)
        base = (kb.connector_config or {}).get("base_url")
        if base:
            assert_safe_url(base)
        docs = await conn.search("测试", top_k=1)
        return ToolResult(content=f"连接正常，返回 {len(docs)} 条", data={"count": len(docs)})


# ==================== 文档：ACL / 文件夹 ====================
class ManageDocAclTool(WriteToolMixin):
    name = "manage_doc_acl"
    description = "文档级授权增删：action=add/remove。principal_type=user/department/role/group。"
    required_permission = "doc:acl_manage"
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "remove"]},
            "doc_id": {"type": "integer"}, "acl_id": {"type": "integer"},
            "principal_type": {"type": "string", "enum": ["user", "department", "role", "group"]},
            "principal_id": {"type": "integer"},
            "effect": {"type": "string", "enum": ["allow", "deny"], "default": "allow"},
        }, "required": ["action", "doc_id"],
    }

    def summarize(self, args: dict) -> str:
        if args.get("action") == "remove":
            return f"移除文档 #{args.get('doc_id')} 的授权 #{args.get('acl_id')}"
        return f"给文档 #{args.get('doc_id')} 授权 {args.get('principal_type')}#{args.get('principal_id')}（{args.get('effect', 'allow')}）"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import Document, DocumentACL
        from app.services import permission as P
        from app.services.doc_acl_service import recompute_doc_acl

        doc = await ctx.db.get(Document, int(args.get("doc_id") or 0))
        if not doc or doc.tenant_id != ctx.tenant_id:
            return ToolResult(content="文档不存在", is_error=True)
        if args.get("action") == "remove":
            acl = await ctx.db.get(DocumentACL, int(args.get("acl_id") or 0))
            if not acl or acl.document_id != doc.id:
                return ToolResult(content="授权不存在", is_error=True)
            await ctx.db.delete(acl)
            await ctx.db.flush()
            await recompute_doc_acl(ctx.db, doc.id)
            await ctx.db.flush()
            return ToolResult(content="已移除授权")
        mapping = {"user": P.user_principal, "department": P.dept_principal,
                   "role": P.role_principal, "group": P.group_principal}
        ptype = args.get("principal_type")
        if ptype not in mapping:
            return ToolResult(content="principal_type 必须为 user/department/role/group", is_error=True)
        pid = mapping[ptype](int(args.get("principal_id") or 0))
        ctx.db.add(DocumentACL(document_id=doc.id, principal_id=pid,
                               effect=str(args.get("effect") or "allow")))
        if doc.visibility != "restricted":
            doc.visibility = "restricted"
        await ctx.db.flush()
        await recompute_doc_acl(ctx.db, doc.id)
        await ctx.db.flush()
        return ToolResult(content="已添加授权")


class DeleteFolderTool(WriteToolMixin):
    name = "delete_folder"
    description = "删除文档文件夹（文档移回未分类）。"
    required_permission = "doc:update"
    dangerous = True
    parameters = {"type": "object", "properties": {"folder_id": {"type": "integer"}}, "required": ["folder_id"]}

    def summarize(self, args: dict) -> str:
        return f"删除文件夹 #{args.get('folder_id')}"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from sqlalchemy import update as _upd

        from app.models import Document, DocumentFolder

        f = await ctx.db.get(DocumentFolder, int(args.get("folder_id") or 0))
        if not f or f.tenant_id != ctx.tenant_id:
            return ToolResult(content="文件夹不存在", is_error=True)
        await ctx.db.execute(_upd(Document).where(Document.folder_id == f.id).values(folder_id=None))
        await ctx.db.delete(f)
        await ctx.db.flush()
        return ToolResult(content="已删除文件夹")


# ==================== 渠道：改 / 测试 ====================
class UpdateChannelTool(WriteToolMixin):
    name = "update_channel"
    description = "修改外部渠道（name/enabled/reply_mode/kb_mode/default_kb_ids）。"
    required_permission = "channel:manage"
    parameters = {
        "type": "object",
        "properties": {"channel_id": {"type": "integer"}, "name": {"type": "string"},
                       "enabled": {"type": "boolean"}, "reply_mode": {"type": "string"},
                       "kb_mode": {"type": "string"},
                       "default_kb_ids": {"type": "array", "items": {"type": "integer"}}},
        "required": ["channel_id"],
    }

    def summarize(self, args: dict) -> str:
        return f"修改渠道 #{args.get('channel_id')}"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import Channel

        c = await ctx.db.get(Channel, int(args.get("channel_id") or 0))
        if not c or c.tenant_id != ctx.tenant_id:
            return ToolResult(content="渠道不存在", is_error=True)
        for f in ("name", "enabled", "reply_mode", "kb_mode", "default_kb_ids"):
            if args.get(f) is not None:
                setattr(c, f, args[f])
        await ctx.db.flush()
        return ToolResult(content=f"已更新渠道 #{c.id}")


class TestChannelTool:
    name = "test_channel"
    description = "测试外部渠道连接（channel_id）。"
    required_permission = "channel:read"
    kind = "read"
    parameters = {"type": "object", "properties": {"channel_id": {"type": "integer"}}, "required": ["channel_id"]}

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.channels.registry import build_adapter
        from app.models import Channel
        from app.services.channel_service import decrypt_config

        c = await ctx.db.get(Channel, int(args.get("channel_id") or 0))
        if not c or c.tenant_id != ctx.tenant_id:
            return ToolResult(content="渠道不存在", is_error=True)
        adapter = build_adapter(c.kind, config=decrypt_config(c.config), on_message=None)
        h = await adapter.health()
        return ToolResult(content=("连接正常：" if h.ok else "连接失败：") + (h.message or "")[:200])


# ==================== RBAC 补全 ====================
class ManageRoleTool(WriteToolMixin):
    name = "manage_role"
    description = "角色的改删：action=update/delete。"
    required_permission = "role:manage"
    dangerous = True
    parameters = {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["update", "delete"]},
                       "role_id": {"type": "integer"}, "name": {"type": "string"},
                       "description": {"type": "string"}},
        "required": ["action", "role_id"],
    }

    def summarize(self, args: dict) -> str:
        return f"{'修改' if args.get('action') == 'update' else '删除'}角色 #{args.get('role_id')}"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import Role

        r = await ctx.db.get(Role, int(args.get("role_id") or 0))
        if not r or (r.tenant_id not in (None, ctx.tenant_id)):
            return ToolResult(content="角色不存在", is_error=True)
        if r.is_system and args.get("action") == "delete":
            return ToolResult(content="内置角色不可删除", is_error=True)
        if args.get("action") == "delete":
            await ctx.db.delete(r)
            await ctx.db.flush()
            return ToolResult(content=f"已删除角色 #{r.id}")
        for f in ("name", "description"):
            if args.get(f) is not None:
                setattr(r, f, args[f])
        await ctx.db.flush()
        return ToolResult(content=f"已更新角色 #{r.id}")


class RevokeUserRoleTool(WriteToolMixin):
    name = "revoke_user_role"
    description = "撤销用户角色（user_role_id，用 list_users 或用户详情查）。"
    required_permission = "user:manage"
    dangerous = True
    parameters = {"type": "object", "properties": {"user_role_id": {"type": "integer"}}, "required": ["user_role_id"]}

    def summarize(self, args: dict) -> str:
        return f"撤销用户角色授予 #{args.get('user_role_id')}"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import UserRole

        ur = await ctx.db.get(UserRole, int(args.get("user_role_id") or 0))
        if not ur or ur.tenant_id != ctx.tenant_id:
            return ToolResult(content="角色授予不存在", is_error=True)
        await ctx.db.delete(ur)
        await ctx.db.flush()
        return ToolResult(content="已撤销角色")


class ManageDeptTool(WriteToolMixin):
    name = "manage_dept"
    description = "部门改删：action=update/delete。"
    required_permission = "dept:manage"
    dangerous = True
    parameters = {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["update", "delete"]},
                       "dept_id": {"type": "integer"}, "name": {"type": "string"},
                       "sort": {"type": "integer"}},
        "required": ["action", "dept_id"],
    }

    def summarize(self, args: dict) -> str:
        return f"{'修改' if args.get('action') == 'update' else '删除'}部门 #{args.get('dept_id')}"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import Department

        d = await ctx.db.get(Department, int(args.get("dept_id") or 0))
        if not d or d.tenant_id != ctx.tenant_id:
            return ToolResult(content="部门不存在", is_error=True)
        if args.get("action") == "delete":
            await ctx.db.delete(d)
            await ctx.db.flush()
            return ToolResult(content=f"已删除部门 #{d.id}")
        for f in ("name", "sort"):
            if args.get(f) is not None:
                setattr(d, f, args[f])
        await ctx.db.flush()
        return ToolResult(content=f"已更新部门 #{d.id}")


class ManageGroupTool(WriteToolMixin):
    name = "manage_group"
    description = "用户组改删/设组员：action=update/delete/set_members。"
    required_permission = "group:manage"
    dangerous = True
    parameters = {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["update", "delete", "set_members"]},
                       "group_id": {"type": "integer"}, "name": {"type": "string"},
                       "description": {"type": "string"}, "user_ids": {"type": "array", "items": {"type": "integer"}}},
        "required": ["action", "group_id"],
    }

    def summarize(self, args: dict) -> str:
        return {"update": "修改", "delete": "删除", "set_members": "设置成员"}.get(args.get("action"), "") + f" 用户组 #{args.get('group_id')}"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import UserGroup, UserGroupMember

        g = await ctx.db.get(UserGroup, int(args.get("group_id") or 0))
        if not g or g.tenant_id != ctx.tenant_id:
            return ToolResult(content="用户组不存在", is_error=True)
        action = args.get("action")
        if action == "delete":
            await ctx.db.execute(_delete(UserGroupMember).where(UserGroupMember.group_id == g.id))
            await ctx.db.delete(g)
            await ctx.db.flush()
            return ToolResult(content=f"已删除用户组 #{g.id}")
        if action == "set_members":
            await ctx.db.execute(_delete(UserGroupMember).where(UserGroupMember.group_id == g.id))
            for uid in (args.get("user_ids") or []):
                ctx.db.add(UserGroupMember(user_id=int(uid), group_id=g.id))
            await ctx.db.flush()
            return ToolResult(content=f"已设置用户组 #{g.id} 成员")
        for f in ("name", "description"):
            if args.get(f) is not None:
                setattr(g, f, args[f])
        await ctx.db.flush()
        return ToolResult(content=f"已更新用户组 #{g.id}")


# ==================== Webhook ====================
class ManageWebhookTokenTool(WriteToolMixin):
    name = "manage_webhook_token"
    description = "定时任务入站 Webhook 令牌增删：action=create/delete。create 需 task_id。"
    required_permission = "schedule:manage"
    dangerous = True
    parameters = {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["create", "delete"]},
                       "task_id": {"type": "integer"}, "token_id": {"type": "integer"},
                       "name": {"type": "string"}},
        "required": ["action"],
    }

    def summarize(self, args: dict) -> str:
        return f"{'创建' if args.get('action') == 'create' else '删除'} Webhook 令牌"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        import hashlib
        import secrets

        from app.models import ScheduledTask, WebhookToken

        if args.get("action") == "create":
            t = await ctx.db.get(ScheduledTask, int(args.get("task_id") or 0))
            if not t or t.tenant_id != ctx.tenant_id:
                return ToolResult(content="定时任务不存在", is_error=True)
            plain = secrets.token_urlsafe(24)
            tok = WebhookToken(tenant_id=ctx.tenant_id, task_id=t.id,
                               token_hash=hashlib.sha256(plain.encode()).hexdigest(),
                               name=str(args.get("name") or "AI 创建"))
            ctx.db.add(tok)
            await ctx.db.flush()
            return ToolResult(content=f"已创建 Webhook 令牌（明文仅一次）：/api/v1/hooks/{plain}",
                              data={"token": plain})
        tk = await ctx.db.get(WebhookToken, int(args.get("token_id") or 0))
        if not tk or tk.tenant_id != ctx.tenant_id:
            return ToolResult(content="令牌不存在", is_error=True)
        await ctx.db.delete(tk)
        await ctx.db.flush()
        return ToolResult(content="已删除 Webhook 令牌")


# ==================== 工作流：发布 / 运行 ====================
class PublishWorkflowTool(WriteToolMixin):
    name = "publish_workflow"
    description = "发布智能体的工作流（agent_id）。"
    required_permission = "workflow:edit"
    parameters = {"type": "object", "properties": {"agent_id": {"type": "integer"}}, "required": ["agent_id"]}

    def summarize(self, args: dict) -> str:
        return f"发布智能体 #{args.get('agent_id')} 的工作流"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.agents.workflow.engine import validate_graph
        from app.models import Workflow
        from app.models.base import utcnow

        wf = (await ctx.db.execute(select(Workflow).where(
            Workflow.agent_id == int(args.get("agent_id") or 0)))).scalars().first()
        if not wf or wf.tenant_id != ctx.tenant_id:
            return ToolResult(content="工作流不存在", is_error=True)
        try:
            validate_graph(wf.graph or {})
        except Exception as e:  # noqa: BLE001
            return ToolResult(content=f"工作流校验失败：{str(e)[:200]}", is_error=True)
        wf.status = "published"
        wf.version = (wf.version or 1) + 1
        wf.published_at = utcnow()
        await ctx.db.flush()
        return ToolResult(content=f"已发布工作流 #{wf.id}（v{wf.version}）")


# ==================== 工作流：AI 生成 ====================
class GenerateWorkflowTool(WriteToolMixin):
    """自然语言 → 工作流图（不落库，返回图供确认/保存）。"""

    name = "generate_workflow"
    description = (
        "根据自然语言需求生成一个工作流 DAG（不落库）。返回可执行的 graph JSON，"
        "用户确认后用 save_workflow 保存。适合『帮我做一个订单自动处理流程』这类需求。"
        "生成的是骨架，外部接口 URL 需用户补充。"
    )
    required_permission = "workflow:edit"
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "需求描述，越具体越好：输入、处理步骤、输出"},
            "agent_id": {"type": "integer", "description": "可选：目标智能体 id，用于继承其默认模型"},
        },
        "required": ["prompt"],
    }

    def summarize(self, args: dict) -> str:
        return f"AI 生成工作流：{str(args.get('prompt') or '')[:40]}"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.services.workflow_ai_service import generate_graph_from_text

        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(content="请描述工作流需求", is_error=True)

        model_id = None
        aid = args.get("agent_id")
        if aid:
            from app.models import Agent

            a = await ctx.db.get(Agent, int(aid))
            if not a or a.tenant_id != ctx.tenant_id:
                return ToolResult(content="智能体不存在", is_error=True)
            model_id = a.model_config_id

        try:
            r = await generate_graph_from_text(
                ctx.db, tenant_id=ctx.tenant_id, prompt=prompt, model_config_id=model_id,
            )
        except Exception as e:  # noqa: BLE001
            return ToolResult(content=f"生成失败：{str(e)[:300]}", is_error=True)

        g = r["graph"]
        lines = [
            f"已生成工作流「{r['name']}」：{r['node_count']} 个节点",
            "节点：" + ", ".join(f"{n['id']}({n['type']})" for n in g["nodes"]),
        ]
        if r["warnings"]:
            lines.append("自动修正：" + "；".join(r["warnings"][:5]))
        lines.append("确认后调用 save_workflow(agent_id, graph) 保存。")
        return ToolResult(
            content="\n".join(lines),
            data={"graph": g, "warnings": r["warnings"], "name": r["name"]},
        )


# ==================== 文档分块（chunk）编辑 ====================
class ManageDocChunkTool(WriteToolMixin):
    name = "manage_doc_chunk"
    description = ("管理文档的检索分块。action=list（列出某文档的分块，含 chunk_id/序号/内容），"
                   "update（改分块内容，会自动重算向量），delete（删除分块），"
                   "split（在指定字符位置把分块一分为二）。改分块会影响检索结果，请谨慎。")
    required_permission = "doc:update"
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "update", "delete", "split"]},
            "doc_id": {"type": "integer", "description": "文档 id"},
            "chunk_id": {"type": "integer", "description": "分块 id（update/delete/split 需要）"},
            "content": {"type": "string", "description": "update：新内容"},
            "offset": {"type": "integer", "description": "split：切分位置（字符，0<offset<长度）"},
            "limit": {"type": "integer", "default": 20, "description": "list 返回条数"},
        },
        "required": ["action", "doc_id"],
    }
    dangerous = True  # delete 有破坏性

    def summarize(self, args: dict) -> str:
        a = args.get("action")
        label = {"list": "列出", "update": "修改", "delete": "删除", "split": "拆分"}.get(a, a)
        return f"{label}文档 #{args.get('doc_id')} 的分块"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from sqlalchemy import update as _upd

        from app.api.v1.document import _reembed_chunk, _refresh_doc_count, _ensure_edit
        from app.models import Chunk, Document, KnowledgeBase

        action = str(args.get("action") or "")
        doc = await ctx.db.get(Document, int(args.get("doc_id") or 0))
        if not doc or doc.tenant_id != ctx.tenant_id:
            return ToolResult(content="文档不存在", is_error=True)
        kb = await ctx.db.get(KnowledgeBase, doc.kb_id)
        try:
            await _ensure_edit(ctx.db, await _load_user(ctx), kb)
        except Exception as e:  # noqa: BLE001
            return ToolResult(content=f"无编辑权限：{str(e)[:120]}", is_error=True)

        if action == "list":
            limit = max(1, min(int(args.get("limit") or 20), 100))
            rows = (await ctx.db.execute(
                select(Chunk).where(Chunk.doc_id == doc.id).order_by(Chunk.ordinal).limit(limit)
            )).scalars().all()
            if not rows:
                return ToolResult(content="该文档暂无分块")
            lines = [f"文档「{doc.title}」的分块（共显示 {len(rows)} 条）："]
            for c in rows:
                preview = (c.content or "").replace("\n", " ")[:80]
                lines.append(f"- [chunk_id={c.id}] #{c.ordinal} {c.chunk_type} {c.token_count}字：{preview}")
            return ToolResult(content="\n".join(lines), data={"count": len(rows)})

        chunk = await ctx.db.get(Chunk, int(args.get("chunk_id") or 0))
        if not chunk or chunk.doc_id != doc.id:
            return ToolResult(content="分块不存在", is_error=True)

        if action == "update":
            content = str(args.get("content") or "").strip()
            if not content:
                return ToolResult(content="分块内容不能为空", is_error=True)
            chunk.content = content
            chunk.token_count = len(content)
            chunk.tsv = content
            await _reembed_chunk(ctx.db, chunk)
            await _refresh_doc_count(ctx.db, doc)
            await ctx.db.flush()
            _invalidate_bm25(doc.tenant_id)
            return ToolResult(content=f"已更新分块 #{chunk.id} 并重算向量")

        if action == "delete":
            await ctx.db.delete(chunk)
            await ctx.db.flush()
            await _refresh_doc_count(ctx.db, doc)
            await ctx.db.flush()
            _invalidate_bm25(doc.tenant_id)
            return ToolResult(content=f"已删除分块 #{chunk.id}")

        if action == "split":
            text = chunk.content or ""
            off = int(args.get("offset") or 0)
            if off <= 0 or off >= len(text):
                return ToolResult(content="切分位置需在分块内部（0 < offset < 长度）", is_error=True)
            head, tail = text[:off], text[off:]
            await ctx.db.execute(
                _upd(Chunk).where(Chunk.doc_id == doc.id, Chunk.ordinal > chunk.ordinal)
                .values(ordinal=Chunk.ordinal + 1)
            )
            new_c = Chunk(
                tenant_id=chunk.tenant_id, kb_id=chunk.kb_id, doc_id=doc.id,
                ordinal=chunk.ordinal + 1,
                chunk_type=chunk.chunk_type if chunk.chunk_type != "parent" else "flat",
                content=tail, token_count=len(tail), page=chunk.page,
                vis_scope=chunk.vis_scope, acl_allow=chunk.acl_allow, acl_deny=chunk.acl_deny,
                enabled=True, tsv=tail,
            )
            chunk.content = head
            chunk.token_count = len(head)
            chunk.tsv = head
            ctx.db.add(new_c)
            await ctx.db.flush()
            await _reembed_chunk(ctx.db, chunk)
            await _reembed_chunk(ctx.db, new_c)
            await _refresh_doc_count(ctx.db, doc)
            await ctx.db.flush()
            _invalidate_bm25(doc.tenant_id)
            return ToolResult(content=f"已把分块 #{chunk.id} 拆成 #{chunk.id} 与 #{new_c.id}")

        return ToolResult(content=f"未知 action：{action}", is_error=True)


# ==================== 文件库管理 ====================
class ManageFilesTool(WriteToolMixin):
    name = "manage_files"
    description = ("管理文件库（对话中生成/上传的文件产物）。"
                   "action=list（列出，可按来源/用户/关键词过滤）、delete（删除一个文件）、"
                   "cleanup（清理已过期文件）。")
    required_permission = "file:manage"
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "delete", "cleanup"]},
            "artifact_id": {"type": "integer", "description": "delete 需要的文件 id"},
            "source": {"type": "string", "enum": ["generated", "upload"], "description": "list 过滤"},
            "keyword": {"type": "string", "description": "list 按文件名搜索"},
            "limit": {"type": "integer", "default": 20},
        },
        "required": ["action"],
    }
    dangerous = True

    def summarize(self, args: dict) -> str:
        a = args.get("action")
        label = {"list": "列出", "delete": "删除", "cleanup": "清理过期"}.get(a, a)
        return f"{label}文件" + (f" #{args.get('artifact_id')}" if args.get("artifact_id") else "")

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from sqlalchemy import func as _func

        from app.models import Artifact

        action = str(args.get("action") or "")

        if action == "list":
            limit = max(1, min(int(args.get("limit") or 20), 100))
            cond = [Artifact.tenant_id == ctx.tenant_id]
            if args.get("source"):
                cond.append(Artifact.source == args["source"])
            if args.get("keyword"):
                cond.append(Artifact.file_name.ilike(f"%{args['keyword']}%"))
            rows = (await ctx.db.execute(
                select(Artifact).where(*cond).order_by(Artifact.id.desc()).limit(limit)
            )).scalars().all()
            if not rows:
                return ToolResult(content="没有匹配的文件")
            lines = [f"文件库（共显示 {len(rows)} 个）："]
            for a in rows:
                size_kb = round((a.size or 0) / 1024, 1)
                lines.append(f"- [id={a.id}] {a.file_name} ({a.file_ext or '-'}, {size_kb}KB, {a.source})")
            return ToolResult(content="\n".join(lines), data={"count": len(rows)})

        if action == "cleanup":
            import time as _t

            from app.ingest.storage import get_storage

            now = int(_t.time() * 1000)
            rows = (await ctx.db.execute(
                select(Artifact).where(
                    Artifact.tenant_id == ctx.tenant_id,
                    Artifact.expires_at.is_not(None),
                    Artifact.expires_at < now,
                )
            )).scalars().all()
            storage = get_storage()
            n = 0
            for a in rows:
                try:
                    storage.delete(a.file_key)
                except Exception:  # noqa: BLE001
                    pass
                await ctx.db.delete(a)
                n += 1
            await ctx.db.flush()
            return ToolResult(content=f"已清理 {n} 个过期文件")

        # delete
        aid = int(args.get("artifact_id") or 0)
        a = await ctx.db.get(Artifact, aid)
        if not a or a.tenant_id != ctx.tenant_id:
            return ToolResult(content="文件不存在", is_error=True)
        from app.ingest.storage import get_storage

        try:
            get_storage().delete(a.file_key)
        except Exception:  # noqa: BLE001
            pass
        await ctx.db.delete(a)
        await ctx.db.flush()
        return ToolResult(content=f"已删除文件「{a.file_name}」")


# ==================== 技能脚本试跑 ====================
class RunSkillScriptTool(WriteToolMixin):
    name = "run_skill_script"
    description = (
        "试跑技能包内的某个入口脚本（需该技能包已启用脚本执行）。支持 Python/Node/Shell（按扩展名自动选择运行时）。"
        "Python 脚本从 stdin 读 JSON 参数、向 stdout 输出结果；Node/Shell 脚本额外可用命令行位置参数 extra_args"
        "（如 build_all.mjs 需要「deck.json -o 输出目录」）。先用 list_skills 查技能 id。"
    )
    required_permission = "skill:execute"
    parameters = {
        "type": "object",
        "properties": {
            "skill_id": {"type": "integer"},
            "script": {"type": "string", "description": "脚本名（技能的 entry_scripts 里，取文件名主干）"},
            "args": {"type": "object", "description": "传给脚本的 JSON 参数（经 stdin 与 env PPT_SKILL_ARGS）"},
            "extra_args": {
                "type": "array", "items": {"type": "string"},
                "description": "追加的命令行位置参数，如 ['examples/x.deck.json', '-o', 'out']",
            },
        },
        "required": ["skill_id", "script"],
    }

    def summarize(self, args: dict) -> str:
        return f"试跑技能 #{args.get('skill_id')} 的脚本 {args.get('script')}"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        import time as _t
        from pathlib import Path as _Path

        from app.agents.tools.registry import run_pack_script
        from app.core.config import settings
        from app.models import SkillPackage

        sid = int(args.get("skill_id") or 0)
        pkg = (await ctx.db.execute(
            select(SkillPackage).where(SkillPackage.skill_id == sid, SkillPackage.tenant_id == ctx.tenant_id)
        )).scalars().first()
        if not pkg or not pkg.pack_dir:
            return ToolResult(content="技能包不存在", is_error=True)
        if not pkg.scripts_enabled:
            return ToolResult(content="该技能包未启用脚本执行（先用 toggle_skill_scripts 开启）", is_error=True)
        scripts = {s.get("name"): s for s in (pkg.entry_scripts or [])}
        name = str(args.get("script") or "")
        if name not in scripts:
            return ToolResult(content=f"脚本不在白名单内：{name}（可选：{', '.join(scripts) or '无'}）", is_error=True)
        entry = scripts[name]
        extra = args.get("extra_args") or []
        if not isinstance(extra, list):
            extra = [str(extra)]

        pack_root = (settings.skill_pack_path / pkg.pack_dir).resolve()
        # 能力桥接：注入平台回调地址与短期令牌（白名单来自清单 permissions.platform_tools）
        bridge = None
        try:
            from app.agents.tools.registry import build_bridge_env
            from app.services.skill_runtime import load_manifest

            mf = load_manifest(pack_root)
            allowed = [str(x) for x in (mf.permissions.get("platform_tools") or [])]
            bridge = build_bridge_env(tenant_id=ctx.tenant_id, user_id=ctx.user_id,
                                      skill_id=sid, allowed_tools=allowed)
        except Exception:  # noqa: BLE001
            bridge = None

        t0 = _t.time()
        res = await run_pack_script(pkg.pack_dir, entry["path"], args.get("args") or {},
                                    extra_args=[str(x) for x in extra],
                                    entry=entry, bridge=bridge)
        # 捕获运行期间新产生的可交付文件 → 登记为 Artifact（供下载）
        try:
            outs = list(entry.get("outputs") or [])
            produced = self._collect_artifacts(pack_root, t0, outputs=outs)
        except Exception:  # noqa: BLE001
            produced = {}
        if produced:
            saved = await self._save_artifacts(ctx, list(produced.values()))
            if saved:
                lines = [res.content, "", "已产出可下载文件："]
                for s in saved:
                    lines.append(f"  • {s['name']}（{s['size']} 字节）")
                return ToolResult(content="\n".join(lines)[:20000], is_error=res.is_error,
                                  data={"artifacts": saved})
        return ToolResult(content=res.content[:20000], is_error=res.is_error)

    # 运行期间产出、可回传下载的交付物：二进制成品优先，其次预览/文本
    _BINARY_DELIVERABLE = {"pptx", "docx", "xlsx", "pdf", "png", "jpg", "jpeg", "svg", "zip"}
    _PREVIEW_DELIVERABLE = {"html"}
    _TEXT_DELIVERABLE = {"json", "csv", "md"}
    _DELIVERABLE_EXT = _BINARY_DELIVERABLE | _PREVIEW_DELIVERABLE | _TEXT_DELIVERABLE
    # 中间报告文件（不作为交付物，避免与成品重复刷屏）
    _INTERMEDIATE_SUFFIXES = (".build.json", ".report.json", ".report.md")
    # 排除目录（依赖/缓存/包内样例，不作为交付物回传）
    _SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "examples", "assets"}

    def _collect_artifacts(self, pack_root, since_ts: float, outputs: list | None = None) -> dict:
        """本次运行新产生的交付物 → {name: path}。

        若清单/入口声明了 outputs（glob，如 "*.pptx"、"out/*.html"），**按其精确匹配**
        （排除 node_modules 等仍生效）；否则回退到按扩展名 + mtime 的启发式。
        """
        if outputs:
            return self._collect_by_globs(pack_root, outputs, since_ts)

        cands: dict[str, tuple] = {}
        for p in pack_root.rglob("*"):
            if not p.is_file():
                continue
            try:
                rel = p.relative_to(pack_root)
            except ValueError:
                continue
            if any(seg in self._SKIP_DIRS or seg.startswith(".") for seg in rel.parts[:-1]):
                continue
            low = p.name.lower()
            ext = p.suffix.lower().lstrip(".")
            if ext not in self._DELIVERABLE_EXT:
                continue
            if any(low.endswith(s) for s in self._INTERMEDIATE_SUFFIXES):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime + 0.5 < since_ts or st.st_size == 0 or st.st_size > 50 * 1024 * 1024:
                continue
            cands[p.name] = (p, ext, st.st_mtime)

        has_binary_or_preview = any(
            ext in self._BINARY_DELIVERABLE or ext in self._PREVIEW_DELIVERABLE
            for _p, ext, _m in cands.values()
        )
        out: dict = {}
        for name, (p, ext, mtime) in cands.items():
            if has_binary_or_preview and ext in self._TEXT_DELIVERABLE:
                continue
            out[name] = p
        return out

    def _collect_by_globs(self, pack_root, patterns: list, since_ts: float) -> dict:
        """按声明的 glob 收集产物（优先），并跳过依赖/隐藏目录。"""
        out: dict = {}
        for pat in patterns:
            pat = str(pat).strip()
            if not pat:
                continue
            for p in pack_root.glob(pat):
                if not p.is_file():
                    continue
                try:
                    rel = p.relative_to(pack_root)
                except ValueError:
                    continue
                if any(seg in self._SKIP_DIRS or seg.startswith(".") for seg in rel.parts[:-1]):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_size == 0 or st.st_size > 50 * 1024 * 1024:
                    continue
                # 声明了 outputs 时允许略过 mtime（脚本可能写死路径、mtime 判定不稳）
                if st.st_mtime + 0.5 < since_ts:
                    continue
                out[p.name] = p
        return out

    async def _save_artifacts(self, ctx, paths) -> list:
        import mimetypes

        from app.agents.tools.file_tools import _MIME, _save_artifact

        rows = []
        for p in sorted(paths, key=lambda x: x.name):
            try:
                data = p.read_bytes()
            except OSError:
                continue
            ext = p.suffix.lower().lstrip(".")
            mime = _MIME.get(ext) or (mimetypes.guess_type(p.name)[0] or "application/octet-stream")
            file_key, art, _ = _save_artifact(ctx, p.name, data, mime)
            rows.append((file_key, art, mime, len(data), p.name))
        if not rows:
            return []
        await ctx.db.flush()  # 先 flush 才有自增 id
        return [{"file_key": fk, "artifact_id": art.id, "name": name,
                 "mime": mime, "size": size, "url": f"/api/v1/chat/attachments/{fk}"}
                for fk, art, mime, size, name in rows]


# ==================== Provider 测试/健康 / KB 统计 ====================
class TestProviderTool(WriteToolMixin):
    name = "test_provider"
    description = ("测试模型 Provider 的连通性。action=test（发一次真实请求验证，可指定 purpose=chat/embedding）、"
                   "health（健康检查并回写状态）、list_models（拉取该 Provider 的可用模型列表）。")
    required_permission = "model:read"
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["test", "health", "list_models"]},
            "provider_id": {"type": "integer"},
            "purpose": {"type": "string", "enum": ["chat", "embedding"], "default": "chat"},
            "model_name": {"type": "string", "description": "可选，指定要测的模型"},
        },
        "required": ["action", "provider_id"],
    }

    def summarize(self, args: dict) -> str:
        return f"{args.get('action')} Provider #{args.get('provider_id')}"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        import time

        from app.api.v1.provider import _build_driver
        from app.models import ModelConfig, ModelProvider
        from app.providers.base import ChatMessage

        action = str(args.get("action") or "")
        pid = int(args.get("provider_id") or 0)
        p = await ctx.db.get(ModelProvider, pid)
        if not p or (p.tenant_id is not None and p.tenant_id != ctx.tenant_id):
            return ToolResult(content="Provider 不存在", is_error=True)
        drv = _build_driver(p.kind, p.base_url, p.api_key, p.timeout, p.extra_headers)
        purpose = str(args.get("purpose") or "chat")

        if action == "list_models":
            try:
                models = await drv.list_models()
                names = [m if isinstance(m, str) else getattr(m, "id", str(m)) for m in (models or [])]
                return ToolResult(content=f"Provider #{pid} 可用模型（{len(names)}）：" + ", ".join(names[:80]))
            except Exception as e:  # noqa: BLE001
                return ToolResult(content=f"拉取失败：{str(e)[:200]}", is_error=True)

        if action == "health":
            t0 = time.time()
            try:
                try:
                    h = await drv.health(purpose=purpose)
                except TypeError:
                    h = await drv.health()
                ok = bool(getattr(h, "ok", False))
                msg = getattr(h, "message", "") or ("正常" if ok else "失败")
            except Exception as e:  # noqa: BLE001
                ok, msg = False, str(e)[:200]
            p.health_status = "ok" if ok else "fail"
            p.last_check_at = int(time.time() * 1000)
            await ctx.db.flush()
            return ToolResult(
                content=f"健康检查：{'正常' if ok else '异常'}（{msg}，{int((time.time()-t0)*1000)}ms）",
                is_error=not ok,
            )

        # test：发一次真实请求
        model = str(args.get("model_name") or "")
        if not model:
            mc = (await ctx.db.execute(
                select(ModelConfig).where(ModelConfig.provider_id == pid, ModelConfig.purpose == purpose)
            )).scalars().first()
            model = mc.model_name if mc else ("text-embedding-3-small" if purpose == "embedding" else "gpt-4o-mini")
        t0 = time.time()
        try:
            if purpose == "embedding":
                vecs = await drv.embed(["测试文本"], model=model)
                return ToolResult(content=f"测试成功：向量维度={len(vecs[0])}，耗时 {int((time.time()-t0)*1000)}ms")
            res = await drv.chat([ChatMessage(role="user", content="你好")], model=model)
            return ToolResult(content=f"测试成功：模型回复「{res.content[:80]}」，耗时 {int((time.time()-t0)*1000)}ms")
        except Exception as e:  # noqa: BLE001
            return ToolResult(content=f"测试失败：{str(e)[:250]}", is_error=True)


class QueryKbStatsTool:
    name = "query_kb_stats"
    description = ("查看知识库统计：文档/分块数量、状态分布、文件类型分布、存储占用、向量覆盖。"
                   "用户问「某知识库有多少文档/多大/入库是否正常」时使用。")
    required_permission = "kb:read"
    kind = "read"
    parameters = {
        "type": "object",
        "properties": {"kb_id": {"type": "integer"}},
        "required": ["kb_id"],
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        from sqlalchemy import func as _func

        from app.models import Chunk, Document, KnowledgeBase

        kid = int(args.get("kb_id") or 0)
        kb = await ctx.db.get(KnowledgeBase, kid)
        if not kb or kb.tenant_id != ctx.tenant_id:
            return ToolResult(content="知识库不存在", is_error=True)
        size = (await ctx.db.execute(
            select(_func.coalesce(_func.sum(Document.file_size), 0)).where(Document.kb_id == kid)
        )).scalar_one()
        docs = (await ctx.db.execute(
            select(_func.count()).select_from(Document).where(Document.kb_id == kid)
        )).scalar_one()
        chunks = (await ctx.db.execute(
            select(_func.count()).select_from(Chunk).where(Chunk.kb_id == kid)
        )).scalar_one()
        embedded = (await ctx.db.execute(
            select(_func.count()).select_from(Chunk).where(Chunk.kb_id == kid, Chunk.embedding.is_not(None))
        )).scalar_one()
        return ToolResult(
            content=(f"知识库「{kb.name}」统计：\n"
                     f"- 文档 {docs} 篇，存储 {round(int(size)/1024/1024, 2)}MB\n"
                     f"- 分块 {chunks} 块，已向量化 {embedded} 块"),
            data={"docs": docs, "chunks": chunks, "embedded": embedded, "size_bytes": int(size)},
        )


def _invalidate_bm25(tenant_id: int) -> None:
    try:
        from app.retrieval import bm25_cache

        bm25_cache.invalidate_tenant(tenant_id)
    except Exception:  # noqa: BLE001
        pass


# ==================== 工作流：运行 / 审批 / 状态 ====================
class RunWorkflowTool(WriteToolMixin):
    name = "run_workflow"
    description = ("运行某个工作流智能体。action=run（跑一次，返回输出与节点状态）、"
                   "status（看某次运行的结果）、list_runs（列历史）。"
                   "agent_id 是 workflow 类型智能体 id（先用 list_agents 查）。")
    required_permission = "workflow:run"
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["run", "status", "list_runs"]},
            "agent_id": {"type": "integer"},
            "inputs": {"type": "object", "description": "run：工作流输入参数"},
            "run_id": {"type": "integer", "description": "status：运行记录 id"},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["action", "agent_id"],
    }

    def summarize(self, args: dict) -> str:
        return f"{args.get('action')} 工作流（智能体 #{args.get('agent_id')}）"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        import json as _json

        from app.agents.workflow.runner import stream_workflow
        from app.middleware.auth_dep import load_principal_set
        from app.models import Agent, Workflow, WorkflowRun

        action = str(args.get("action") or "")
        aid = int(args.get("agent_id") or 0)
        a = await ctx.db.get(Agent, aid)
        if not a or a.tenant_id != ctx.tenant_id:
            return ToolResult(content="智能体不存在", is_error=True)

        if action == "list_runs":
            rows = (await ctx.db.execute(
                select(WorkflowRun).where(WorkflowRun.agent_id == aid, WorkflowRun.tenant_id == ctx.tenant_id)
                .order_by(WorkflowRun.id.desc()).limit(min(int(args.get("limit") or 10), 50))
            )).scalars().all()
            if not rows:
                return ToolResult(content="该工作流暂无运行记录")
            lines = [f"- [run_id={r.id}] {r.status} {r.latency_ms or 0}ms {r.error_msg or ''}".rstrip() for r in rows]
            return ToolResult(content="运行历史：\n" + "\n".join(lines))

        if action == "status":
            rid = int(args.get("run_id") or 0)
            run = await ctx.db.get(WorkflowRun, rid)
            if not run or run.tenant_id != ctx.tenant_id:
                return ToolResult(content="运行记录不存在", is_error=True)
            out = run.output if isinstance(run.output, str) else _json.dumps(run.output, ensure_ascii=False)
            return ToolResult(content=f"运行 #{rid}：{run.status}\n输出：{(out or '')[:2000]}\n"
                                      f"{'错误：' + run.error_msg if run.error_msg else ''}")

        # run：非流式执行，收集节点事件与最终输出
        wf = (await ctx.db.execute(
            select(Workflow).where(Workflow.agent_id == aid).order_by(Workflow.id.desc())
        )).scalars().first()
        if not wf or not wf.graph:
            return ToolResult(content="工作流未配置", is_error=True)
        import time as _t

        run = WorkflowRun(
            tenant_id=ctx.tenant_id, workflow_id=wf.id, agent_id=aid, user_id=ctx.user_id,
            status="running", input=args.get("inputs") or {}, graph_snapshot=wf.graph,
            created_at=int(_t.time() * 1000),
        )
        ctx.db.add(run)
        await ctx.db.flush()
        pset = await load_principal_set(ctx.db, await _load_user(ctx))
        final_text = ""
        node_status = []
        try:
            async for evt in stream_workflow(
                graph=wf.graph, inputs=args.get("inputs") or {}, run_id=run.id,
                tenant_id=ctx.tenant_id, ps=pset,
                default_model_config_id=a.model_config_id, default_kb_ids=a.kb_ids,
                default_system=a.system_prompt, default_temperature=(a.config or {}).get("temperature"),
            ):
                t = evt.get("type")
                if t == "node_finished":
                    node_status.append(f"{evt.get('node_id')}:{evt.get('status')}")
                elif t == "run_finished":
                    final_text = evt.get("text") or _json.dumps(evt.get("output", ""), ensure_ascii=False)
                    if evt.get("status") == "waiting" or evt.get("paused"):
                        return ToolResult(content=f"工作流运行挂起，等待人工审批（run_id={run.id}）。"
                                                  f"节点：{', '.join(node_status)}")
                    if evt.get("err"):
                        return ToolResult(content=f"工作流执行失败：{str(evt.get('err'))[:300]}\n"
                                                  f"节点：{', '.join(node_status)}", is_error=True)
        except Exception as e:  # noqa: BLE001
            return ToolResult(content=f"工作流执行失败：{str(e)[:300]}", is_error=True)
        return ToolResult(content=f"工作流已运行（run_id={run.id}）。输出：{(final_text or '')[:2000]}\n"
                                  f"节点：{', '.join(node_status)}")


class ApproveWorkflowRunTool(WriteToolMixin):
    name = "approve_workflow_run"
    description = "审批挂起的工作流运行（人工审批节点）。run_id 来自 run_workflow 的挂起提示。"
    required_permission = "workflow:run"
    dangerous = True
    parameters = {
        "type": "object",
        "properties": {
            "run_id": {"type": "integer"},
            "decision": {"type": "string", "enum": ["approve", "reject"], "default": "approve"},
        },
        "required": ["run_id"],
    }

    def summarize(self, args: dict) -> str:
        d = "通过" if args.get("decision", "approve") == "approve" else "拒绝"
        return f"{d}工作流运行 #{args.get('run_id')}"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        import json as _json
        import time as _t

        from sqlalchemy import update as _upd

        from app.agents.workflow.runner import stream_workflow
        from app.middleware.auth_dep import load_principal_set
        from app.models import Agent, WorkflowRun

        rid = int(args.get("run_id") or 0)
        run = await ctx.db.get(WorkflowRun, rid)
        if not run or run.tenant_id != ctx.tenant_id:
            return ToolResult(content="运行记录不存在", is_error=True)
        if run.status != "waiting":
            return ToolResult(content="该运行不在等待审批状态", is_error=True)
        decision = args.get("decision") or "approve"
        pending = run.pending_node_id
        res = await ctx.db.execute(
            _upd(WorkflowRun).where(WorkflowRun.id == rid, WorkflowRun.status == "waiting")
            .values(status="running" if decision == "approve" else "canceled")
        )
        if res.rowcount != 1:
            await ctx.db.rollback()
            return ToolResult(content="该运行已被处理", is_error=True)
        await ctx.db.flush()
        if decision != "approve":
            return ToolResult(content=f"已拒绝工作流运行 #{rid}")

        agent = await ctx.db.get(Agent, run.agent_id) if run.agent_id else None
        pset = await load_principal_set(ctx.db, await _load_user(ctx))
        final_text = ""
        try:
            async for evt in stream_workflow(
                graph=run.graph_snapshot or {}, inputs=run.input or {}, run_id=rid,
                tenant_id=ctx.tenant_id, ps=pset,
                default_model_config_id=agent.model_config_id if agent else None,
                default_kb_ids=agent.kb_ids if agent else None,
                default_system=agent.system_prompt if agent else None,
                approval={pending: "approve"} if pending else {}, resume=run.state or {},
            ):
                if evt.get("type") == "run_finished":
                    final_text = evt.get("text") or _json.dumps(evt.get("output", ""), ensure_ascii=False)
                    if evt.get("err"):
                        return ToolResult(content=f"续跑失败：{str(evt.get('err'))[:300]}", is_error=True)
        except Exception as e:  # noqa: BLE001
            return ToolResult(content=f"续跑失败：{str(e)[:300]}", is_error=True)
        return ToolResult(content=f"已通过审批并续跑完成（run_id={rid}）。输出：{(final_text or '')[:2000]}")


# ==================== 邮件入库源 ====================
class ManageEmailSourceTool(WriteToolMixin):
    name = "manage_email_source"
    description = ("管理邮件入库源（把邮箱里的邮件正文与附件自动入库到知识库）。"
                   "action=list/create/update/delete/test/sync。"
                   "create 需 name/imap_host/username/password/kb_id(目标知识库 id)；"
                   "可选 folder(默认 INBOX)、ingest_mode(both/attach/body)、allow_from、subject_keywords。")
    required_permission = "kb:update"
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "create", "update", "delete", "test", "sync"]},
            "source_id": {"type": "integer"},
            "name": {"type": "string"},
            "source_account_id": {"type": "integer", "description": "可选：引用个人邮箱账号（复用其 IMAP 凭证，见 manage_mail.list_accounts）"},
            "imap_host": {"type": "string"},
            "imap_port": {"type": "integer"},
            "use_ssl": {"type": "boolean"},
            "username": {"type": "string"},
            "password": {"type": "string"},
            "folder": {"type": "string"},
            "kb_id": {"type": "integer"},
            "ingest_mode": {"type": "string", "enum": ["both", "attach", "body"]},
            "allow_from": {"type": "string"},
            "subject_keywords": {"type": "string"},
            "enabled": {"type": "boolean"},
        },
        "required": ["action"],
    }
    dangerous = True

    def summarize(self, args: dict) -> str:
        a = args.get("action")
        label = {"list": "列出", "create": "新建", "update": "修改", "delete": "删除",
                 "test": "测试", "sync": "拉取"}.get(a, a)
        return f"{label}邮件入库源" + (f" {args.get('name') or ('#'+str(args.get('source_id')))}" if (args.get('name') or args.get('source_id')) else "")

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.core.crypto import encrypt
        from app.models import EmailSource, KnowledgeBase

        action = str(args.get("action") or "")
        sid = int(args.get("source_id") or 0)

        if action == "list":
            rows = (await ctx.db.execute(
                select(EmailSource).where(EmailSource.tenant_id == ctx.tenant_id).order_by(EmailSource.id.desc())
            )).scalars().all()
            if not rows:
                return ToolResult(content="当前没有邮件入库源")
            lines = [f"#{s.id} {s.name} [{s.username}] → 知识库#{s.kb_id} "
                     f"({'启用' if s.enabled else '停用'}, 已入库 {s.ingested_count or 0})" for s in rows]
            return ToolResult(content="\n".join(lines))

        if action == "create":
            kb = await ctx.db.get(KnowledgeBase, int(args.get("kb_id") or 0))
            if not kb or kb.tenant_id != ctx.tenant_id:
                return ToolResult(content="目标知识库不存在", is_error=True)
            said = int(args.get("source_account_id") or 0)
            # 引用个人邮箱账号时，IMAP 字段可省略（从账号取）；须校验归属
            if said:
                from app.models import MailAccount

                acc = await ctx.db.get(MailAccount, said)
                if not acc or acc.tenant_id != ctx.tenant_id or acc.user_id != ctx.user_id:
                    return ToolResult(content="邮箱账号不存在或不属于你", is_error=True)
            elif not args.get("imap_host") or not args.get("username"):
                return ToolResult(content="create 需要 source_account_id（个人邮箱）或 imap_host+username", is_error=True)
            s = EmailSource(
                tenant_id=ctx.tenant_id, name=str(args.get("name") or "邮件源"),
                source_account_id=said or None,
                imap_host=str(args.get("imap_host") or ""), imap_port=int(args.get("imap_port") or 993),
                use_ssl=bool(args.get("use_ssl", True)), username=str(args.get("username") or ""),
                password=(encrypt(args["password"]) if (args.get("password") and not said) else None),
                folder=str(args.get("folder") or "INBOX"), kb_id=kb.id,
                ingest_mode=str(args.get("ingest_mode") or "both"),
                allow_from=args.get("allow_from"), subject_keywords=args.get("subject_keywords"),
                uploaded_by=ctx.user_id, enabled=bool(args.get("enabled", True)), status="active",
            )
            ctx.db.add(s)
            await ctx.db.flush()
            return ToolResult(content=f"已创建邮件入库源 #{s.id}「{s.name}」")

        s = await ctx.db.get(EmailSource, sid)
        if not s or s.tenant_id != ctx.tenant_id:
            return ToolResult(content="邮件源不存在", is_error=True)

        if action == "update":
            for f in ("name", "source_account_id", "imap_host", "imap_port", "use_ssl", "username",
                      "folder", "kb_id", "ingest_mode", "allow_from", "subject_keywords", "enabled"):
                if args.get(f) is not None:
                    setattr(s, f, args[f])
            if args.get("password"):
                s.password = encrypt(args["password"])
            await ctx.db.flush()
            return ToolResult(content=f"已更新邮件源 #{s.id}")

        if action == "delete":
            await ctx.db.delete(s)
            await ctx.db.flush()
            return ToolResult(content=f"已删除邮件源 #{sid}")

        if action == "test":
            from app.services.email_ingest_service import _resolve_account, test_source

            r = await test_source(s, account=await _resolve_account(ctx.db, s))
            return ToolResult(content=r.get("message", ""), is_error=not r.get("ok"))

        if action == "sync":
            from app.services.email_ingest_service import sync_source

            r = await sync_source(ctx.db, s)
            return ToolResult(content=r.get("message", ""), is_error=not r.get("ok"))

        return ToolResult(content=f"未知 action：{action}", is_error=True)


# ==================== 会话导出 / 分享 ====================
class ExportConversationTool(WriteToolMixin):
    name = "export_conversation"
    description = ("导出对话记录为文件（md/pdf/docx），或生成免登录分享链接。"
                   "conversation_id 可省略（默认导出当前会话）。分享链接短期有效。")
    required_permission = "chat:use"
    parameters = {
        "type": "object",
        "properties": {
            "conversation_id": {"type": "integer"},
            "format": {"type": "string", "enum": ["md", "pdf", "docx"], "default": "md"},
            "share": {"type": "boolean", "description": "true=生成分享链接而非导出文件", "default": False},
        },
    }

    def summarize(self, args: dict) -> str:
        return ("生成会话分享链接" if args.get("share") else f"导出会话为 {args.get('format') or 'md'}")

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        import time as _t

        from app.core.config import settings
        from app.core.security import create_file_token
        from app.ingest.storage import get_storage
        from app.models import Artifact, Conversation
        from app.services.export_service import export_conversation

        cid = int(args.get("conversation_id") or ctx.conversation_id or 0)
        conv = await ctx.db.get(Conversation, cid)
        if not conv or conv.tenant_id != ctx.tenant_id:
            return ToolResult(content="会话不存在", is_error=True)

        if args.get("share"):
            filename, data, mime = await export_conversation(ctx.db, conversation=conv, user_id=ctx.user_id, fmt="md")
            storage = get_storage()
            file_key, chash = storage.save(tenant_id=ctx.tenant_id, filename=filename, data=data)
            art = Artifact(
                tenant_id=ctx.tenant_id, user_id=ctx.user_id, conversation_id=conv.id,
                file_key=file_key, file_name=filename, file_ext="md", mime=mime,
                size=len(data), source="generated", content_hash=chash,
                expires_at=int(_t.time() * 1000) + settings.file_token_expire_minutes * 60 * 1000,
            )
            ctx.db.add(art)
            await ctx.db.flush()
            token = create_file_token(scope="artifact", tenant_id=ctx.tenant_id, artifact_id=art.id)
            return ToolResult(
                content=f"已生成分享链接（{settings.file_token_expire_minutes} 分钟内有效）：\n"
                        f"/api/v1/files/{art.id}/download?inline=1&t={token}",
                data={"artifact_id": art.id, "url": f"/api/v1/files/{art.id}/download?inline=1&t={token}"},
            )

        fmt = args.get("format") or "md"
        filename, data, mime = await export_conversation(ctx.db, conversation=conv, user_id=ctx.user_id, fmt=fmt)
        from app.agents.tools.file_tools import _save_artifact

        file_key, art, _ = _save_artifact(ctx, filename, data, mime)
        await ctx.db.flush()
        return ToolResult(
            content=f"已导出对话记录：{filename}（{len(data)} 字节，可下载）",
            data={"file_key": file_key, "artifact_id": art.id, "name": filename, "mime": mime,
                  "size": len(data), "url": f"/api/v1/chat/attachments/{file_key}"},
        )


class MergeExportDocumentsTool(WriteToolMixin):
    name = "merge_export_documents"
    description = ("把多篇文档的内容合并导出为一个文件（docx/pdf/md/txt）。"
                   "doc_ids 是文档 id 列表（先用 list_documents 查）。")
    required_permission = "doc:read"
    parameters = {
        "type": "object",
        "properties": {
            "doc_ids": {"type": "array", "items": {"type": "integer"}},
            "format": {"type": "string", "enum": ["docx", "pdf", "md", "txt"], "default": "docx"},
            "filename": {"type": "string"},
        },
        "required": ["doc_ids"],
    }

    def summarize(self, args: dict) -> str:
        return f"合并导出 {len(args.get('doc_ids') or [])} 篇文档为 {args.get('format') or 'docx'}"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.agents.tools.file_tools import _render, _save_artifact
        from app.models import Document
        from app.services.export_service import documents_to_markdown

        ids = [int(x) for x in (args.get("doc_ids") or [])]
        if not ids:
            return ToolResult(content="请提供 doc_ids", is_error=True)
        rows = (await ctx.db.execute(
            select(Document).where(Document.id.in_(ids), Document.tenant_id == ctx.tenant_id)
        )).scalars().all()
        if not rows:
            return ToolResult(content="未找到可导出的文档", is_error=True)
        md = await documents_to_markdown(ctx.db, docs=list(rows))
        fmt = args.get("format") or "docx"
        if fmt == "txt":
            blob, mime = md.encode("utf-8"), "text/plain"
        else:
            blob, mime = _render({"format": fmt, "content": md})
        filename = str(args.get("filename") or f"合并导出-{len(rows)}篇.{fmt}")
        if not filename.lower().endswith(f".{fmt}"):
            filename = f"{filename}.{fmt}"
        file_key, art, _ = _save_artifact(ctx, filename, blob, mime)
        await ctx.db.flush()
        return ToolResult(
            content=f"已合并 {len(rows)} 篇文档为「{filename}」（{len(blob)} 字节，可下载）",
            data={"file_key": file_key, "artifact_id": art.id, "name": filename, "mime": mime,
                  "size": len(blob), "url": f"/api/v1/chat/attachments/{file_key}"},
        )


# ==================== 系统设置 ====================
class ManageSystemSettingsTool(WriteToolMixin):
    name = "manage_system_settings"
    description = ("查看/修改平台系统设置（检索阈值、内容安全、连接器、MCP、日志、定时任务上限等）。"
                   "action=get（列出全部可配置项）、update（改指定项，key→value）。"
                   "标 restart 的项需重启服务后生效。")
    required_permission = "system:manage"
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["get", "update"]},
            "updates": {"type": "object", "description": "update：{配置key: 新值}"},
        },
        "required": ["action"],
    }

    def summarize(self, args: dict) -> str:
        if args.get("action") == "update":
            keys = list((args.get("updates") or {}).keys())
            return f"修改系统设置：{', '.join(keys[:5])}"
        return "查看系统设置"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.services import settings_service as S

        action = str(args.get("action") or "")
        if action == "get":
            cfg = S.get_config()
            lines = []
            for c in cfg:
                if c["kind"] == "secret":
                    continue  # 不回显敏感项
                lines.append(f"{c['key']} = {c['value']}" + ("（需重启）" if c["restart"] else ""))
            return ToolResult(content="\n".join(lines[:200]))
        if action == "update":
            updates = args.get("updates") or {}
            if not updates:
                return ToolResult(content="请提供 updates", is_error=True)
            try:
                r = await S.update_config(ctx.db, user_id=ctx.user_id, updates=updates)
            except Exception as e:  # noqa: BLE001
                return ToolResult(content=f"修改失败：{str(e)[:200]}", is_error=True)
            msg = f"已更新：{', '.join(r['changed'])}"
            if r["restart_required"]:
                msg += "（部分项需重启服务后生效）"
            return ToolResult(content=msg)
        return ToolResult(content=f"未知 action：{action}", is_error=True)


# ==================== 客服工单 ====================
class ManageServiceTicketTool(WriteToolMixin):
    name = "manage_service_ticket"
    description = ("管理客服工单（渠道用户请求转人工时自动生成）。"
                   "action=list（列出工单，可按 status 过滤）、get（看详情含消息流）、"
                   "reply（客服回复，会回发到原渠道）、update（改状态/指派/优先级）、close（关闭）。")
    required_permission = "service:manage"
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "get", "reply", "update", "close"]},
            "ticket_id": {"type": "integer"},
            "content": {"type": "string", "description": "reply：回复内容"},
            "status": {"type": "string", "enum": ["open", "pending", "resolved", "closed"]},
            "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
            "assignee_id": {"type": "integer"},
            "resolution": {"type": "string", "description": "close：解决说明"},
            "limit": {"type": "integer", "default": 30},
        },
        "required": ["action"],
    }
    dangerous = True

    def summarize(self, args: dict) -> str:
        a = args.get("action")
        label = {"list": "列出", "get": "查看", "reply": "回复", "update": "更新", "close": "关闭"}.get(a, a)
        return f"{label}客服工单" + (f" #{args.get('ticket_id')}" if args.get("ticket_id") else "")

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import ServiceTicket
        from app.services import service_ticket_service as S

        action = str(args.get("action") or "")
        tid = int(args.get("ticket_id") or 0)

        if action == "list":
            rows = await S.list_tickets(ctx.db, tenant_id=ctx.tenant_id, status=args.get("status"),
                                        limit=int(args.get("limit") or 30))
            if not rows:
                return ToolResult(content="当前没有工单")
            lines = [f"#{t.id} [{t.status}/{t.priority}] {t.subject}"
                     f"（{t.channel_kind or '问答页'}，指派={'#'+str(t.assignee_id) if t.assignee_id else '未指派'}）"
                     for t in rows]
            return ToolResult(content="工单列表：\n" + "\n".join(lines))

        t = await ctx.db.get(ServiceTicket, tid)
        if not t or t.tenant_id != ctx.tenant_id:
            return ToolResult(content="工单不存在", is_error=True)

        if action == "get":
            msgs = t.messages or []
            lines = [f"工单 #{t.id} [{t.status}/{t.priority}] {t.subject}",
                     f"来源：{t.channel_kind or '问答页'} 用户：{t.external_user or '-'}"]
            for m in msgs:
                role = {"user": "用户", "agent": "客服", "assistant": "AI"}.get(m.get("role"), m.get("role"))
                lines.append(f"  [{role}] {m.get('content')}")
            return ToolResult(content="\n".join(lines))

        if action == "reply":
            content = str(args.get("content") or "").strip()
            if not content:
                return ToolResult(content="请提供回复内容", is_error=True)
            r = await S.add_agent_reply(ctx.db, t, content, agent_id=ctx.user_id)
            return ToolResult(content=f"已回复工单 #{tid}"
                                      + ("（已回发到渠道）" if r.get("sent_to_channel") else "（渠道未连接，未回发）"))

        if action == "close":
            await S.close_ticket(ctx.db, t, resolution=args.get("resolution"))
            return ToolResult(content=f"已关闭工单 #{tid}")

        if action == "update":
            for f in ("status", "priority", "assignee_id"):
                if args.get(f) is not None:
                    setattr(t, f, args[f])
            await ctx.db.flush()
            return ToolResult(content=f"已更新工单 #{tid}")

        return ToolResult(content=f"未知 action：{action}", is_error=True)


async def _load_user(ctx: ToolContext):
    from app.models import User

    return await ctx.db.get(User, ctx.user_id)


# ==================== 智能录单 ====================
class ExtractRecordsTool(WriteToolMixin):
    name = "extract_records"
    description = ("智能录单：按「录单模板」从一段文本（通话记录/聊天/邮件）中抽取结构化字段并保存。"
                   "action=list_templates（列出模板，得到 template_id）、extract（从 text 抽取，"
                   "或给 doc_id 从知识库文档取全文抽取）。"
                   "抽取结果会保存为记录，可在「智能录单」页看台账/导出。")
    required_permission = "record:manage"
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list_templates", "extract"]},
            "template_id": {"type": "integer", "description": "extract：录单模板 id"},
            "text": {"type": "string", "description": "extract：要抽取的原始文本"},
            "doc_id": {"type": "integer", "description": "extract：改为从该知识库文档取全文（与 text 二选一）"},
            "save": {"type": "boolean", "default": True, "description": "是否保存为记录"},
            "source_type": {"type": "string", "enum": ["text", "document", "channel"], "default": "text"},
        },
        "required": ["action"],
    }

    def summarize(self, args: dict) -> str:
        a = args.get("action")
        if a == "list_templates":
            return "列出录单模板"
        return f"从文本智能录单（模板 #{args.get('template_id')}）"

    def needs_confirm(self, args: dict) -> bool:
        """按 action 判定：list_templates 是只读查询，免确认；extract（尤其 save）才需确认。"""
        if str(args.get("action") or "") == "list_templates":
            return False
        # extract 且明确不保存 → 只是试抽，也免确认
        if str(args.get("action") or "") == "extract" and args.get("save") is False:
            return False
        return True

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import RecordEntry, RecordTemplate

        action = str(args.get("action") or "")
        if action == "list_templates":
            rows = (await ctx.db.execute(
                select(RecordTemplate).where(RecordTemplate.tenant_id == ctx.tenant_id).order_by(RecordTemplate.id.desc())
            )).scalars().all()
            if not rows:
                return ToolResult(content="当前没有录单模板，请先在「智能录单」页创建")
            lines = [f"#{t.id} {t.name}（字段：{', '.join(f.get('name','') for f in (t.fields or []))}）" for t in rows]
            return ToolResult(content="录单模板：\n" + "\n".join(lines))

        if action == "extract":
            tid = int(args.get("template_id") or 0)
            t = await ctx.db.get(RecordTemplate, tid)
            if not t or t.tenant_id != ctx.tenant_id:
                return ToolResult(content="模板不存在", is_error=True)
            text = str(args.get("text") or "").strip()
            source_type = str(args.get("source_type") or "text")
            source_ref = None
            if not text and args.get("doc_id"):
                # 从知识库文档取全文（与前端 /extract 的 doc_id 一致）
                from app.models import Document

                doc = await ctx.db.get(Document, int(args["doc_id"]))
                if not doc or doc.tenant_id != ctx.tenant_id:
                    return ToolResult(content="文档不存在", is_error=True)
                text = (doc.content or "").strip()
                if not text:
                    from app.models import Chunk

                    chs = (await ctx.db.execute(
                        select(Chunk).where(Chunk.doc_id == doc.id).order_by(Chunk.id).limit(200)
                    )).scalars().all()
                    text = "\n".join(c.content for c in chs if c.content).strip()
                if not text:
                    return ToolResult(content="该文档没有可用的文本内容", is_error=True)
                source_type, source_ref = "document", str(doc.id)
            if not text:
                return ToolResult(content="请提供要抽取的文本或 doc_id", is_error=True)
            from app.services.record_extract_service import extract_records

            try:
                rows = await extract_records(ctx.db, tenant_id=ctx.tenant_id, template=t, text=text)
            except Exception as e:  # noqa: BLE001
                return ToolResult(content=f"抽取失败：{str(e)[:200]}", is_error=True)
            save = args.get("save", True)
            # 免确认/无人值守（auto_write）：AI 在无人核验下执行，抽取结果直接落 confirmed，
            # 省去逐条手动确认；否则落 draft 待人工核对。
            init_status = "confirmed" if getattr(ctx, "auto_write", False) else "draft"
            lines = []
            for row in rows:
                if save:
                    e = RecordEntry(
                        tenant_id=ctx.tenant_id, template_id=t.id, data=row,
                        source_type=source_type, source_ref=source_ref,
                        raw_text=text[:8000], created_by=ctx.user_id, status=init_status,
                    )
                    ctx.db.add(e)
                    await ctx.db.flush()
                lines.append(", ".join(f"{k}={v}" for k, v in row.items()))
            return ToolResult(
                content=f"已抽取 {len(rows)} 条记录（模板「{t.name}」"
                        + ("，已自动确认" if (save and init_status == "confirmed") else "") + "）：\n"
                        + "\n".join(lines),
                data={"count": len(rows), "status": init_status},  # 供前端展示
            )

        return ToolResult(content=f"未知 action：{action}", is_error=True)


OPS_TOOLS2 = [
    CloneAgentTool(), ManageAgentVersionTool(), ManageAgentModeTool(),
    UpdateSkillTool(), DeleteSkillTool(), ImportSkillFromUrlTool(), ToggleSkillScriptsTool(),
    GcSkillPacksTool(),
    FindSkillTool(),
    RegisterToolTool(), DeleteToolTool(), TestToolTool(),
    ManageProviderTool(), ManageModelConfigTool(),
    TestKbConnectorTool(),
    ManageDocAclTool(), DeleteFolderTool(),
    UpdateChannelTool(), TestChannelTool(),
    ManageRoleTool(), RevokeUserRoleTool(), ManageDeptTool(), ManageGroupTool(),
    ManageWebhookTokenTool(), PublishWorkflowTool(), GenerateWorkflowTool(),
    ManageMcpServerTool(),
    ManageNotifyChannelTool(),
    ManageDocChunkTool(), ManageFilesTool(), RunSkillScriptTool(), TestProviderTool(),
    QueryKbStatsTool(),
    RunWorkflowTool(), ApproveWorkflowRunTool(), ManageEmailSourceTool(),
    ExportConversationTool(), MergeExportDocumentsTool(), ManageSystemSettingsTool(),
    ManageServiceTicketTool(),
]
