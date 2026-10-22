"""管理类 AI 工具：让管理员通过对话操作平台内部对象。

分类：
  - read 类：直接执行（查列表 / 读文件）。
  - write 类：不直接执行，由 AgentRunner 挂起为 AgentAction（HITL），
    管理员在前端确认卡片点"确认执行"后才真正落库。

管理类工具不默认进入 agent.tool_config，需"管理助手"智能体显式启用，
避免普通智能体的 schema 里出现管理工具。
"""
from __future__ import annotations

import re
import time

from sqlalchemy import select

from app.agents.tools.base import ToolContext, ToolResult, WriteToolMixin


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return s or f"agent-{int(time.time())}"


# ==================== read 类 ====================
class ListSkillsTool:
    name = "list_skills"
    description = "列出本租户的所有技能（含 id/名称/类型/来源），用于了解现有技能后再决定创建或修改。"
    required_permission = "skill:read"
    kind = "read"
    parameters = {"type": "object", "properties": {}}

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import Skill

        rows = (
            await ctx.db.execute(
                select(Skill).where(Skill.tenant_id == ctx.tenant_id).order_by(Skill.id.desc()).limit(100)
            )
        ).scalars().all()
        if not rows:
            return ToolResult(content="（暂无技能）", data={"count": 0})
        lines = [f"[{s.id}] {s.name} | 类型={s.kind} | 来源={s.source}" for s in rows]
        return ToolResult(content="\n".join(lines), data={"count": len(rows)})


class ReadSkillFileTool:
    name = "read_skill_file"
    description = "读取某个技能包内的文本文件内容（如 SKILL.md、scripts/*.py）。需先通过 list_skills 获取 skill_id。"
    required_permission = "skill:read"
    kind = "read"
    parameters = {
        "type": "object",
        "properties": {
            "skill_id": {"type": "integer", "description": "技能 id"},
            "path": {"type": "string", "description": "包内相对路径，如 SKILL.md"},
        },
        "required": ["skill_id", "path"],
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.api.v1.skills import TEXT_EXT, _safe_in_pack
        from app.core.config import settings
        from app.models import SkillPackage

        skill_id = int(args.get("skill_id") or 0)
        path = str(args.get("path") or "")
        pkg = (
            await ctx.db.execute(
                select(SkillPackage).where(
                    SkillPackage.skill_id == skill_id, SkillPackage.tenant_id == ctx.tenant_id
                )
            )
        ).scalars().first()
        if not pkg or not pkg.pack_dir:
            return ToolResult(content="技能包不存在", is_error=True)
        target = _safe_in_pack(settings.skill_pack_path / pkg.pack_dir, path)
        if not target:
            return ToolResult(content="文件不存在", is_error=True)
        if target.suffix.lower().lstrip(".") not in TEXT_EXT:
            return ToolResult(content="非文本文件，无法读取", is_error=True)
        return ToolResult(content=target.read_text(encoding="utf-8", errors="ignore")[:200000])


class ListKbsTool:
    name = "list_kbs"
    description = "列出本租户的知识库（含 id/名称/可见性）。"
    required_permission = "kb:read"
    kind = "read"
    parameters = {"type": "object", "properties": {}}

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import KnowledgeBase

        rows = (
            await ctx.db.execute(
                select(KnowledgeBase).where(
                    KnowledgeBase.tenant_id == ctx.tenant_id, KnowledgeBase.status == "active"
                ).limit(100)
            )
        ).scalars().all()
        if not rows:
            return ToolResult(content="（暂无知识库）", data={"count": 0})
        lines = [f"[{k.id}] {k.name} | 可见性={k.visibility}" for k in rows]
        return ToolResult(content="\n".join(lines), data={"count": len(rows)})


class ListDocumentsTool:
    name = "list_documents"
    description = "列出某知识库下的文档（含 id/标题/状态）。"
    required_permission = "doc:read"
    kind = "read"
    parameters = {
        "type": "object",
        "properties": {"kb_id": {"type": "integer", "description": "知识库 id"}},
        "required": ["kb_id"],
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import Document

        kb_id = int(args.get("kb_id") or 0)
        rows = (
            await ctx.db.execute(
                select(Document).where(
                    Document.tenant_id == ctx.tenant_id, Document.kb_id == kb_id
                ).order_by(Document.id.desc()).limit(100)
            )
        ).scalars().all()
        if not rows:
            return ToolResult(content="（该知识库暂无文档）", data={"count": 0})
        lines = [f"[{d.id}] {d.title} | 状态={d.status}" for d in rows]
        return ToolResult(content="\n".join(lines), data={"count": len(rows)})


class ListAgentsTool:
    name = "list_agents"
    description = "列出本租户的智能体（含 id/名称/类型/状态）。"
    required_permission = "agent:read"
    kind = "read"
    parameters = {"type": "object", "properties": {}}

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import Agent

        rows = (
            await ctx.db.execute(
                select(Agent).where(Agent.tenant_id == ctx.tenant_id).order_by(Agent.id.desc()).limit(100)
            )
        ).scalars().all()
        if not rows:
            return ToolResult(content="（暂无智能体）", data={"count": 0})
        lines = [f"[{a.id}] {a.name} | 类型={a.type} | 状态={a.status}" for a in rows]
        return ToolResult(content="\n".join(lines), data={"count": len(rows)})


# ==================== write 类（HITL）====================
# 统一复用 base.WriteToolMixin（run 转发 execute、summarize 供确认卡片），
# 本文件不再自带副本，避免多处实现漂移。
_WriteToolMixin = WriteToolMixin


class CreateSkillTool(_WriteToolMixin):
    name = "create_skill"
    description = "创建一个新的技能（prompt_pack 能力包）。写操作，需管理员在对话中确认后才生效。"
    required_permission = "skill:edit"
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "技能名称"},
            "description": {"type": "string", "description": "技能描述"},
            "body_md": {"type": "string", "description": "技能正文（Markdown），作为提示词模板"},
            "kind": {"type": "string", "enum": ["prompt_pack"], "default": "prompt_pack"},
        },
        "required": ["name", "body_md"],
    }

    def summarize(self, args: dict) -> str:
        return f"创建技能「{args.get('name')}」（能力包，正文 {len(args.get('body_md') or '')} 字符）"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import Skill

        name = str(args.get("name") or "").strip()
        body_md = str(args.get("body_md") or "")
        if not name:
            return ToolResult(content="技能名称不能为空", is_error=True)
        slug_base = _slugify(name)
        slug = slug_base
        n = 1
        while (
            await ctx.db.execute(
                select(Skill).where(Skill.tenant_id == ctx.tenant_id, Skill.slug == slug)
            )
        ).scalar_one_or_none():
            n += 1
            slug = f"{slug_base}-{n}"
        sk = Skill(
            tenant_id=ctx.tenant_id,
            owner_id=ctx.user_id,
            name=name,
            slug=slug,
            description=args.get("description"),
            kind="prompt_pack",
            prompt_template=body_md,
            body_md=body_md,
            source="manual",
        )
        ctx.db.add(sk)
        await ctx.db.flush()
        return ToolResult(content=f"已创建技能：{sk.name}（id={sk.id}）", data={"skill_id": sk.id})


class WriteSkillFileTool(_WriteToolMixin):
    name = "write_skill_file"
    description = ("编辑技能包内的文本文件（如 SKILL.md、scripts/*.py、tools/*.mjs）。"
                   "**目标文件不存在时会新建**（可用于在技能包内创建 deck.json、临时数据文件等）。"
                   "写操作，需管理员确认。")
    required_permission = "skill:edit"
    parameters = {
        "type": "object",
        "properties": {
            "skill_id": {"type": "integer", "description": "技能 id"},
            "path": {"type": "string", "description": "包内相对路径，如 SKILL.md 或 deck.json"},
            "content": {"type": "string", "description": "新的文件内容"},
        },
        "required": ["skill_id", "path", "content"],
    }

    def summarize(self, args: dict) -> str:
        return f"写入技能 {args.get('skill_id')} 的文件 {args.get('path')}（{len(args.get('content') or '')} 字符）"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.api.v1.skills import TEXT_EXT, _safe_pack_target
        from app.core.config import settings
        from app.models import Skill, SkillPackage
        from app.services.skill_pack_service import parse_skill_md

        skill_id = int(args.get("skill_id") or 0)
        path = str(args.get("path") or "")
        content = str(args.get("content") or "")
        pkg = (
            await ctx.db.execute(
                select(SkillPackage).where(
                    SkillPackage.skill_id == skill_id, SkillPackage.tenant_id == ctx.tenant_id
                )
            )
        ).scalars().first()
        if not pkg or not pkg.pack_dir:
            return ToolResult(content="技能包不存在", is_error=True)
        if Path(path).suffix.lower().lstrip(".") not in TEXT_EXT:
            return ToolResult(content=f"仅可写入文本文件（允许：{', '.join(sorted(TEXT_EXT))}）", is_error=True)
        target = _safe_pack_target(settings.skill_pack_path / pkg.pack_dir, path, allow_new=True)
        if not target:
            return ToolResult(content="非法路径", is_error=True)
        is_new = not target.is_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if target.name.upper() == "SKILL.MD":
            sk = await ctx.db.get(Skill, skill_id)
            if sk:
                _, md_body = parse_skill_md(content)
                sk.body_md = md_body
                sk.prompt_template = md_body
                await ctx.db.flush()
        return ToolResult(content=f"已{'新建' if is_new else '写入'} {path}",
                          data={"path": path, "created": is_new})


class CreateAgentTool(_WriteToolMixin):
    name = "create_agent"
    description = "创建一个工具循环智能体。写操作，需管理员确认。"
    required_permission = "agent:edit"
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "智能体名称"},
            "system_prompt": {"type": "string", "description": "系统提示词"},
            "kb_ids": {"type": "array", "items": {"type": "integer"}, "description": "知识库范围"},
            "description": {"type": "string"},
        },
        "required": ["name"],
    }

    def summarize(self, args: dict) -> str:
        return f"创建智能体「{args.get('name')}」"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.models import Agent

        name = str(args.get("name") or "").strip()
        if not name:
            return ToolResult(content="智能体名称不能为空", is_error=True)
        slug_base = _slugify(name)
        slug = slug_base
        n = 1
        while (
            await ctx.db.execute(
                select(Agent).where(Agent.tenant_id == ctx.tenant_id, Agent.slug == slug)
            )
        ).scalar_one_or_none():
            n += 1
            slug = f"{slug_base}-{n}"
        a = Agent(
            tenant_id=ctx.tenant_id,
            owner_id=ctx.user_id,
            name=name,
            slug=slug,
            description=args.get("description"),
            type="agent",
            system_prompt=args.get("system_prompt") or "你是一个智能助手。",
            kb_ids=args.get("kb_ids") or [],
            tool_config={
                "builtin": {
                    "knowledge_retrieval": {"enabled": True},
                    "read_file": {"enabled": True},
                    "list_conversation_files": {"enabled": True},
                    "convert_file": {"enabled": True},
                    "generate_file": {"enabled": True},
                    "edit_file": {"enabled": True},
                    "convert_file_to": {"enabled": True},
                },
                "max_turns": 4,
            },
            status="draft",
        )
        ctx.db.add(a)
        await ctx.db.flush()
        return ToolResult(content=f"已创建智能体：{a.name}（id={a.id}）", data={"agent_id": a.id})


class WriteDocumentTool(_WriteToolMixin):
    name = "write_document"
    description = "向知识库写入一篇文本文档（创建 Document 并入队解析）。写操作，需管理员确认。"
    required_permission = "doc:upload"
    parameters = {
        "type": "object",
        "properties": {
            "kb_id": {"type": "integer", "description": "知识库 id"},
            "title": {"type": "string", "description": "文档标题"},
            "content": {"type": "string", "description": "文档正文（纯文本/Markdown）"},
        },
        "required": ["kb_id", "title", "content"],
    }

    def summarize(self, args: dict) -> str:
        return f"向知识库 {args.get('kb_id')} 写入文档「{args.get('title')}」（{len(args.get('content') or '')} 字符）"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        from app.ingest.storage import get_storage
        from app.models import Document, KnowledgeBase

        kb_id = int(args.get("kb_id") or 0)
        title = str(args.get("title") or "untitled")
        content = str(args.get("content") or "")
        kb = await ctx.db.get(KnowledgeBase, kb_id)
        if not kb or kb.tenant_id != ctx.tenant_id:
            return ToolResult(content="知识库不存在", is_error=True)
        filename = f"{title}.md"
        data = content.encode("utf-8")
        storage = get_storage()
        file_key, content_hash = storage.save(tenant_id=ctx.tenant_id, filename=filename, data=data)
        doc = Document(
            tenant_id=ctx.tenant_id, kb_id=kb_id, title=title, source_type="upload",
            file_key=file_key, file_name=filename, file_ext="md", file_size=len(data),
            content_hash=content_hash, status="pending", uploaded_by=ctx.user_id,
        )
        ctx.db.add(doc)
        await ctx.db.flush()
        doc_id = doc.id
        await ctx.db.commit()
        from app.tasks.ingest_tasks import enqueue_document

        await enqueue_document(doc_id)
        return ToolResult(content=f"已写入文档：{title}（id={doc_id}）", data={"doc_id": doc_id})


ADMIN_TOOLS = [
    ListSkillsTool(),
    ReadSkillFileTool(),
    ListKbsTool(),
    ListDocumentsTool(),
    ListAgentsTool(),
    CreateSkillTool(),
    WriteSkillFileTool(),
    CreateAgentTool(),
    WriteDocumentTool(),
]

# 平台运营类工具（用量/组织/审计查询 + 写操作）
from app.agents.tools.platform_tools import PLATFORM_TOOLS  # noqa: E402

ADMIN_TOOLS += PLATFORM_TOOLS

# 全量操作工具（定时任务/RBAC/KB/智能体/APIKey/文档/渠道/工作流/模型）
from app.agents.tools.ops_tools import OPS_TOOLS  # noqa: E402

ADMIN_TOOLS += OPS_TOOLS

# 第二轮操作工具（版本/模式/技能/工具台/模型/文档ACL/渠道/RBAC补全/webhook/工作流）
from app.agents.tools.ops_tools2 import OPS_TOOLS2  # noqa: E402

ADMIN_TOOLS += OPS_TOOLS2

# 第三轮补全工具（待办提醒/录单模板/话术库/渠道身份/站内通知/问答评测/会话管理）
from app.agents.tools.complement_tools import COMPLEMENT_TOOLS  # noqa: E402

ADMIN_TOOLS += COMPLEMENT_TOOLS
