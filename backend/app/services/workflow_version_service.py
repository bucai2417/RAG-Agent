"""工作流版本快照与导入导出。

设计要点（勿凭直觉改动）：

1. **快照在「发布」时创建，不在「保存」时**。保存是高频编辑动作（每拖一次节点
   就可能存一次），全存快照会快速膨胀；发布才代表「一个可交付的版本」。

2. **回滚 = 把快照写回当前 graph，并再存一份新快照**。不做「删除后重建」，
   因为 workfow.id 被运行记录引用，换了 id 会断掉历史关联。

3. **导入总是创建新智能体**，不覆盖现有工作流 —— 导入是「复制一份」的语义，
   覆盖是破坏性操作，应该走显式回滚。
"""
from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.models import Agent, User, Workflow, WorkflowVersion


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name or "").strip("-").lower()
    return s or "workflow"


async def create_snapshot(
    db: AsyncSession, *, wf: Workflow, user_id: int | None, note: str | None = None,
) -> WorkflowVersion:
    """为当前图存一份快照（发布时调用）。"""
    if not wf.graph or not (wf.graph.get("nodes")):
        raise ValidationError("工作流为空，无法发布")

    v = WorkflowVersion(
        tenant_id=wf.tenant_id, workflow_id=wf.id,
        version=int(wf.version or 1), graph=wf.graph,
        note=(note or "")[:500] or None, created_by=user_id,
    )
    db.add(v)
    await db.flush()
    return v


async def list_versions(
    db: AsyncSession, *, tenant_id: int, agent_id: int,
) -> list[dict]:
    wf = await _workflow_of(db, tenant_id=tenant_id, agent_id=agent_id)
    rows = (
        await db.execute(
            select(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == wf.id, WorkflowVersion.tenant_id == tenant_id)
            .order_by(WorkflowVersion.version.desc(), WorkflowVersion.id.desc())
        )
    ).scalars().all()
    return [
        {
            "id": r.id, "version": r.version, "note": r.note,
            "node_count": len((r.graph or {}).get("nodes") or []),
            "edge_count": len((r.graph or {}).get("edges") or []),
            "created_at": str(r.created_at) if r.created_at else None,
        }
        for r in rows
    ]


async def rollback(
    db: AsyncSession, *, tenant_id: int, agent_id: int, version_id: int, user_id: int | None,
) -> dict:
    """回滚到指定快照。回滚本身也会留下一条新快照，保证可再回退。"""
    wf = await _workflow_of(db, tenant_id=tenant_id, agent_id=agent_id)
    v = await db.get(WorkflowVersion, version_id)
    if not v or v.tenant_id != tenant_id or v.workflow_id != wf.id:
        raise NotFoundError("版本不存在")
    if not v.graph:
        raise ValidationError("该版本快照为空")

    from app.agents.workflow.engine import validate_graph

    validate_graph(v.graph)  # 防御：快照本身若已损坏，不要写回去

    wf.graph = v.graph
    wf.version = (wf.version or 1) + 1
    # 回滚也是一次状态变更，存快照便于「回滚的回滚」
    await create_snapshot(
        db, wf=wf, user_id=user_id, note=f"回滚到 v{v.version}",
    )
    await db.flush()
    return {"message": f"已回滚到 v{v.version}", "version": wf.version}


async def _workflow_of(db: AsyncSession, *, tenant_id: int, agent_id: int) -> Workflow:
    a = await db.get(Agent, agent_id)
    if not a or a.tenant_id != tenant_id:
        raise NotFoundError("智能体不存在")
    wf = (
        await db.execute(
            select(Workflow).where(Workflow.agent_id == agent_id).order_by(Workflow.id.desc())
        )
    ).scalars().first()
    if not wf:
        raise NotFoundError("工作流不存在")
    return wf


# ---------------------------------------------------------------- 导入 / 导出


def export_workflow(*, agent: Agent, wf: Workflow) -> dict:
    """导出为可移植的 JSON（不含任何密钥、不含租户/用户 id）。"""
    return {
        "format": "rag-workflow",
        "format_version": 1,
        "name": wf.name or agent.name,
        "description": agent.description or "",
        "system_prompt": agent.system_prompt or "",
        "graph": wf.graph or {"nodes": [], "edges": []},
        "config": {
            k: v for k, v in (agent.config or {}).items()
            if k in ("temperature", "max_turns", "run_timeout_seconds")
        },
    }


async def import_workflow(
    db: AsyncSession, *, tenant_id: int, user_id: int, data: dict,
) -> dict:
    """把导出的 JSON 导入为**新的工作流型智能体**（不覆盖现有）。

    不覆盖是有意的：导入是「复制一份」的语义，覆盖属于破坏性操作。
    """
    from app.agents.workflow.engine import validate_graph

    if not isinstance(data, dict):
        raise ValidationError("导入内容必须是 JSON 对象")
    if data.get("format") and data.get("format") != "rag-workflow":
        raise ValidationError(f"不支持的文件格式：{data.get('format')}")

    graph = data.get("graph")
    if not isinstance(graph, dict) or not (graph.get("nodes")):
        raise ValidationError("导入内容缺少 graph 或 graph 为空")
    validate_graph(graph)

    name = str(data.get("name") or "导入的工作流").strip()[:128]

    # slug 去重
    base = _slugify(name)
    slug, n = base, 2
    while (
        await db.execute(
            select(Agent).where(Agent.tenant_id == tenant_id, Agent.slug == slug)
        )
    ).scalar_one_or_none():
        slug = f"{base}-{n}"
        n += 1

    cfg = data.get("config")
    agent = Agent(
        tenant_id=tenant_id, owner_id=user_id, name=name, slug=slug,
        description=str(data.get("description") or ""),
        system_prompt=str(data.get("system_prompt") or ""),
        type="workflow", status="draft", visibility="private",
        config=cfg if isinstance(cfg, dict) else None,
    )
    db.add(agent)
    await db.flush()

    wf = Workflow(
        tenant_id=tenant_id, agent_id=agent.id, name=name,
        graph=graph, version=1, status="draft",
    )
    db.add(wf)
    await db.flush()

    return {
        "message": f"已导入为「{name}」",
        "agent_id": agent.id,
        "workflow_id": wf.id,
        "node_count": len(graph.get("nodes") or []),
    }
