"""智能体接口：CRUD + 行为模式 + SSE 运行。"""
from __future__ import annotations

import json
import re
import time

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal, get_db
from app.core.errors import ConflictError, NotFoundError
from app.middleware.auth_dep import get_current_user, get_principal_set, load_principal_set, require_permission
from app.services.audit_service import audited, record_audit
from app.models import Agent, AgentMode, AgentVersion, Conversation, Message, User
from app.schemas.agent import (
    AgentCreate,
    AgentOut,
    AgentRunRequest,
    AgentUpdate,
    ModeCreate,
    ModeOut,
    ModeUpdate,
    ToolConfirmRequest,
)
from app.services.permission import PrincipalSet

router = APIRouter(prefix="/agents", tags=["agent"])

# 持有工作流后台执行任务引用，避免被 GC（任务结束自动移除）
_BG_TASKS: set = set()


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return s or f"agent-{int(time.time())}"


async def _get_agent(db: AsyncSession, agent_id: int, tenant_id: int) -> Agent:
    a = await db.get(Agent, agent_id)
    if not a or a.tenant_id != tenant_id:
        raise NotFoundError("智能体不存在")
    return a


# ==================== Agent CRUD ====================
@router.get("", response_model=list[AgentOut])
async def list_agents(
    user: User = Depends(require_permission("agent:read")),
    db: AsyncSession = Depends(get_db),
) -> list[Agent]:
    from sqlalchemy import or_

    # visibility=private 仅 owner/admin 可见；tenant 全租户可见
    con = [
        Agent.tenant_id == user.tenant_id,
        or_(Agent.visibility != "private", Agent.owner_id == user.id, user.is_admin),
    ]
    rows = (
        await db.execute(select(Agent).where(*con).order_by(Agent.id.desc()))
    ).scalars().all()
    return list(rows)


@router.post("", response_model=AgentOut)
@audited("agent.create", "agent")
async def create_agent(
    body: AgentCreate,
    user: User = Depends(require_permission("agent:edit")),
    db: AsyncSession = Depends(get_db),
) -> Agent:
    slug = body.slug or _slugify(body.name)
    exists = (
        await db.execute(
            select(Agent).where(Agent.tenant_id == user.tenant_id, Agent.slug == slug)
        )
    ).scalar_one_or_none()
    if exists:
        raise ConflictError("标识已存在")
    a = Agent(
        tenant_id=user.tenant_id,
        owner_id=user.id,
        name=body.name,
        slug=slug,
        description=body.description,
        icon=body.icon,
        type=body.type,
        system_prompt=body.system_prompt,
        model_config_id=body.model_config_id,
        kb_ids=body.kb_ids,
        skill_ids=body.skill_ids,
        tool_config=body.tool_config,
        config=body.config,
    )
    db.add(a)
    await db.flush()
    return a


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(
    agent_id: int,
    user: User = Depends(require_permission("agent:read")),
    db: AsyncSession = Depends(get_db),
) -> Agent:
    return await _get_agent(db, agent_id, user.tenant_id)


@router.patch("/{agent_id}", response_model=AgentOut)
@audited("agent.update", "agent", id_arg="agent_id")
async def update_agent(
    agent_id: int,
    body: AgentUpdate,
    user: User = Depends(require_permission("agent:edit")),
    db: AsyncSession = Depends(get_db),
) -> Agent:
    a = await _get_agent(db, agent_id, user.tenant_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(a, k, v)
    await db.flush()
    return a


@router.delete("/{agent_id}")
@audited("agent.delete", "agent", id_arg="agent_id")
async def delete_agent(
    agent_id: int,
    user: User = Depends(require_permission("agent:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    a = await _get_agent(db, agent_id, user.tenant_id)
    if getattr(a, "is_builtin", False):
        from app.core.errors import ConflictError

        raise ConflictError("内置智能体不可删除")
    await db.delete(a)
    await db.flush()
    return {"message": "已删除"}


@router.post("/{agent_id}/publish", response_model=AgentOut)
@audited("agent.publish", "agent", id_arg="agent_id")
async def publish_agent(
    agent_id: int,
    user: User = Depends(require_permission("agent:edit")),
    db: AsyncSession = Depends(get_db),
) -> Agent:
    from app.models.base import utcnow

    a = await _get_agent(db, agent_id, user.tenant_id)
    a.status = "published"
    a.published_at = utcnow()
    await db.flush()
    return a


# ==================== Modes ====================
@router.post("/{agent_id}/clone", response_model=AgentOut)
@audited("agent.clone", "agent", id_arg="agent_id")
async def clone_agent(
    agent_id: int,
    user: User = Depends(require_permission("agent:edit")),
    db: AsyncSession = Depends(get_db),
) -> Agent:
    """复制智能体（含行为模式）。"""
    src = await _get_agent(db, agent_id, user.tenant_id)
    base_slug = f"{src.slug}-copy"
    slug = base_slug
    n = 1
    while (
        await db.execute(select(Agent).where(Agent.tenant_id == user.tenant_id, Agent.slug == slug))
    ).scalar_one_or_none():
        n += 1
        slug = f"{base_slug}-{n}"
    new = Agent(
        tenant_id=user.tenant_id, owner_id=user.id, name=f"{src.name} 副本", slug=slug,
        description=src.description, icon=src.icon, type=src.type,
        system_prompt=src.system_prompt, model_config_id=src.model_config_id,
        kb_ids=src.kb_ids, skill_ids=src.skill_ids, tool_config=src.tool_config,
        config=src.config, status="draft",
    )
    db.add(new)
    await db.flush()
    # 复制模式
    for m in (
        await db.execute(select(AgentMode).where(AgentMode.agent_id == agent_id))
    ).scalars().all():
        db.add(AgentMode(
            tenant_id=user.tenant_id, agent_id=new.id, name=m.name, description=m.description,
            system_prompt=m.system_prompt, skill_ids=m.skill_ids, tool_config=m.tool_config,
            kb_ids=m.kb_ids, params=m.params, is_default=m.is_default, sort=m.sort,
        ))
    await db.flush()
    return new


# ==================== 版本管理 ====================
async def _snapshot(db: AsyncSession, a: Agent) -> dict:
    modes = (
        await db.execute(select(AgentMode).where(AgentMode.agent_id == a.id).order_by(AgentMode.sort))
    ).scalars().all()
    return {
        "name": a.name, "description": a.description, "system_prompt": a.system_prompt,
        "model_config_id": a.model_config_id, "kb_ids": a.kb_ids, "skill_ids": a.skill_ids,
        "tool_config": a.tool_config, "config": a.config, "visibility": a.visibility,
        "modes": [
            {"name": m.name, "description": m.description, "system_prompt": m.system_prompt,
             "skill_ids": m.skill_ids, "tool_config": m.tool_config, "kb_ids": m.kb_ids,
             "params": m.params, "is_default": m.is_default, "sort": m.sort}
            for m in modes
        ],
    }


@router.post("/{agent_id}/versions")
@audited("agent.snapshot", "agent", id_arg="agent_id")
async def create_version(
    agent_id: int,
    body: dict,
    user: User = Depends(require_permission("agent:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from sqlalchemy import func

    a = await _get_agent(db, agent_id, user.tenant_id)
    maxv = (await db.execute(
        select(func.max(AgentVersion.version)).where(AgentVersion.agent_id == agent_id)
    )).scalar_one_or_none() or 0
    v = AgentVersion(
        tenant_id=user.tenant_id, agent_id=agent_id, version=maxv + 1,
        snapshot=await _snapshot(db, a), note=(body or {}).get("note"), created_by=user.id,
    )
    db.add(v)
    await db.flush()
    return {"id": v.id, "version": v.version, "created_at": str(v.created_at)}


@router.get("/{agent_id}/versions")
async def list_versions(
    agent_id: int,
    user: User = Depends(require_permission("agent:read")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await _get_agent(db, agent_id, user.tenant_id)
    rows = (
        await db.execute(
            select(AgentVersion).where(AgentVersion.agent_id == agent_id).order_by(AgentVersion.version.desc())
        )
    ).scalars().all()
    return [{"id": r.id, "version": r.version, "note": r.note, "created_at": str(r.created_at)} for r in rows]


@router.post("/{agent_id}/versions/{version_id}/rollback", response_model=AgentOut)
@audited("agent.rollback", "agent", id_arg="agent_id")
async def rollback_version(
    agent_id: int,
    version_id: int,
    user: User = Depends(require_permission("agent:edit")),
    db: AsyncSession = Depends(get_db),
) -> Agent:
    a = await _get_agent(db, agent_id, user.tenant_id)
    v = await db.get(AgentVersion, version_id)
    if not v or v.agent_id != agent_id or v.tenant_id != user.tenant_id:
        raise NotFoundError("版本不存在")
    snap = v.snapshot or {}
    for k in ("name", "description", "system_prompt", "model_config_id", "kb_ids",
              "skill_ids", "tool_config", "config", "visibility"):
        if k in snap:
            setattr(a, k, snap[k])
    # 重建模式
    from sqlalchemy import delete as _del

    await db.execute(_del(AgentMode).where(AgentMode.agent_id == agent_id))
    for m in snap.get("modes") or []:
        db.add(AgentMode(tenant_id=user.tenant_id, agent_id=agent_id, **m))
    await db.flush()
    return a


@router.get("/{agent_id}/modes", response_model=list[ModeOut])
async def list_modes(
    agent_id: int,
    user: User = Depends(require_permission("agent:read")),
    db: AsyncSession = Depends(get_db),
) -> list[AgentMode]:
    await _get_agent(db, agent_id, user.tenant_id)
    rows = (
        await db.execute(
            select(AgentMode).where(AgentMode.agent_id == agent_id).order_by(AgentMode.sort)
        )
    ).scalars().all()
    return list(rows)


@router.post("/{agent_id}/modes", response_model=ModeOut)
@audited("agent_mode.create", "agent_mode")
async def create_mode(
    agent_id: int,
    body: ModeCreate,
    user: User = Depends(require_permission("agent:edit")),
    db: AsyncSession = Depends(get_db),
) -> AgentMode:
    await _get_agent(db, agent_id, user.tenant_id)
    m = AgentMode(
        tenant_id=user.tenant_id,
        agent_id=agent_id,
        name=body.name,
        description=body.description,
        system_prompt=body.system_prompt,
        skill_ids=body.skill_ids,
        tool_config=body.tool_config,
        kb_ids=body.kb_ids,
        params=body.params,
        is_default=body.is_default,
        sort=body.sort,
    )
    db.add(m)
    await db.flush()
    return m


@router.patch("/{agent_id}/modes/{mode_id}", response_model=ModeOut)
@audited("agent_mode.update", "agent_mode", id_arg="mode_id")
async def update_mode(
    agent_id: int,
    mode_id: int,
    body: ModeUpdate,
    user: User = Depends(require_permission("agent:edit")),
    db: AsyncSession = Depends(get_db),
) -> AgentMode:
    m = await db.get(AgentMode, mode_id)
    if not m or m.agent_id != agent_id or m.tenant_id != user.tenant_id:
        raise NotFoundError("模式不存在")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(m, k, v)
    await db.flush()
    return m


@router.delete("/{agent_id}/modes/{mode_id}")
@audited("agent_mode.delete", "agent_mode", id_arg="mode_id")
async def delete_mode(
    agent_id: int,
    mode_id: int,
    user: User = Depends(require_permission("agent:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    m = await db.get(AgentMode, mode_id)
    if not m or m.agent_id != agent_id or m.tenant_id != user.tenant_id:
        raise NotFoundError("模式不存在")
    await db.delete(m)
    await db.flush()
    return {"message": "已删除"}


# ==================== 运行（SSE）====================
@router.post("/{agent_id}/run")
async def run_agent(
    agent_id: int,
    body: AgentRunRequest,
    user: User = Depends(require_permission("agent:run")),
    ps: PrincipalSet = Depends(get_principal_set),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    agent = await _get_agent(db, agent_id, user.tenant_id)

    # 会话：复用 Conversation，agent_id/mode_id 存 settings
    conv: Conversation | None = None
    if body.conversation_id:
        conv = await db.get(Conversation, body.conversation_id)
        if conv and conv.user_id != user.id:
            conv = None
    if not conv:
        conv = Conversation(
            tenant_id=user.tenant_id,
            user_id=user.id,
            title=body.message[:30],
            kb_ids=body.kb_ids or agent.kb_ids or [],
            settings={"agent_id": agent_id, "mode_id": body.mode_id},
        )
        db.add(conv)
    await db.commit()
    conv_id = conv.id
    query = body.message
    mode_id = body.mode_id

    async def gen():
        async with AsyncSessionLocal() as sdb:
            from app.middleware.auth_dep import load_principal_set as _lps

            pset = await load_principal_set(sdb, user)

            # workflow 型智能体：走工作流引擎（消息作为 start 输入）
            if agent.type == "workflow":
                import asyncio

                from app.agents.workflow.engine import NodeContext, execute_graph
                from app.models import NodeRun, Workflow, WorkflowRun

                wf = (
                    await sdb.execute(
                        select(Workflow).where(Workflow.agent_id == agent_id).order_by(Workflow.id.desc())
                    )
                ).scalars().first()
                if not wf or not wf.graph:
                    yield f"event: error\ndata: {json.dumps({'message': '工作流未配置'}, ensure_ascii=False)}\n\n"
                    return
                # 输入：把 query 填进工作流声明的第一个输入名（默认 query）
                from app.agents.workflow.runner import start_input_name

                inputs = {start_input_name(wf.graph): query}

                run = WorkflowRun(
                    tenant_id=user.tenant_id, workflow_id=wf.id, agent_id=agent_id,
                    user_id=user.id, conversation_id=conv_id, status="running",
                    input=inputs, graph_snapshot=wf.graph, created_at=int(time.time() * 1000),
                )
                sdb.add(run)
                await sdb.commit()
                run_id = run.id

                # 执行与落库放在独立后台 task（独立 session），与 SSE 连接解耦：
                # 客户端断开也不会中断运行，终态一定能落库。
                queue: asyncio.Queue = asyncio.Queue()
                _DONE = "__wf_done__"

                async def _run_workflow():
                    async with AsyncSessionLocal() as wdb:
                        from app.middleware.auth_dep import get_user_permission_codes as _gupc

                        async def emit(evt: dict):
                            await queue.put(evt)

                        ctx = NodeContext(
                            db=wdb, ps=pset, run_id=run_id, tenant_id=user.tenant_id, inputs=inputs,
                            emit=emit, default_model_config_id=agent.model_config_id,
                            default_kb_ids=agent.kb_ids, default_system=agent.system_prompt,
                            default_temperature=(agent.config or {}).get("temperature"),
                            perms=await _gupc(wdb, user),
                        )
                        t0 = time.time()
                        err_msg = None
                        final_out: dict = {}
                        records: list[dict] = []
                        try:
                            final_out, records = await execute_graph(wf.graph, inputs, ctx)
                        except Exception as e:  # noqa: BLE001
                            err_msg = str(e)[:300]
                        out_text = (
                            str(final_out.get("output", final_out)) if isinstance(final_out, dict) else str(final_out)
                        )
                        if err_msg:
                            out_text = f"[工作流执行失败] {err_msg}"
                        assistant_id = None
                        try:
                            r2 = await wdb.get(WorkflowRun, run_id)
                            if err_msg:
                                r2.status = "failed"; r2.error_msg = err_msg
                            else:
                                r2.status = "success"; r2.output = final_out
                            r2.latency_ms = int((time.time() - t0) * 1000)
                            r2.finished_at = int(time.time() * 1000)
                            for rr in records:
                                wdb.add(NodeRun(
                                    tenant_id=user.tenant_id, run_id=run_id, node_id=rr["node_id"],
                                    node_type=rr["node_type"], seq=rr["seq"], status=rr["status"],
                                    input=rr.get("input"), output=rr.get("output"), error_msg=rr.get("error_msg"),
                                    created_at=int(time.time() * 1000),
                                ))
                            asst = Message(
                                tenant_id=user.tenant_id, conversation_id=conv_id, role="assistant",
                                content=out_text, model="workflow",
                                latency_ms=int((time.time() - t0) * 1000), created_at=int(time.time() * 1000),
                            )
                            wdb.add(asst)
                            await wdb.commit()
                            assistant_id = asst.id
                        except Exception as e:  # noqa: BLE001
                            await wdb.rollback()
                        await queue.put({
                            "type": _DONE, "err": err_msg, "text": out_text,
                            "message_id": assistant_id, "run_id": run_id,
                        })

                from app.tasks.bg import spawn
                bg = spawn("workflow_run", _run_workflow())
                _BG_TASKS.add(bg)
                bg.add_done_callback(_BG_TASKS.discard)

                yield f"event: meta\ndata: {json.dumps({'conversation_id': conv_id, 'run_id': run_id}, ensure_ascii=False)}\n\n"

                # 边执行边转发节点事件
                while True:
                    evt = await queue.get()
                    etype = evt.get("type") if isinstance(evt, dict) else "node"
                    if etype == _DONE:
                        if evt.get("err"):
                            yield f"event: error\ndata: {json.dumps({'message': evt['err']}, ensure_ascii=False)}\n\n"
                        text = evt.get("text") or ""
                        if text:
                            yield f"event: delta\ndata: {json.dumps({'text': text}, ensure_ascii=False)}\n\n"
                        yield f"event: done\ndata: {json.dumps({'message_id': evt.get('message_id'), 'run_id': evt.get('run_id')}, ensure_ascii=False)}\n\n"
                        break
                    yield f"event: {etype}\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n"
                return

            # 普通 Agent：ReAct 工具循环
            from app.agents.runner import AgentRunner
            from app.middleware.auth_dep import get_user_permission_codes
            from app.services.context_service import load_context

            # 以操作者身份计算权限码集合，用于工具级 RBAC 过滤
            perms = await get_user_permission_codes(sdb, user)
            conv2 = await sdb.get(Conversation, conv_id)
            summary, history = await load_context(sdb, conv2)
            runner = AgentRunner(
                sdb, agent=agent, ps=pset, conversation=conv2, history=history, perms=perms, summary=summary,
                model_override=body.model_config_id,
            )
            try:
                async for evt in runner.run(query, mode_id=mode_id):
                    etype = evt.pop("type")
                    yield f"event: {etype}\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n"
            except Exception as e:  # noqa: BLE001
                yield f"event: error\ndata: {json.dumps({'message': str(e)[:300]}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{agent_id}/tool-confirm")
async def confirm_tool_action(
    agent_id: int,
    body: ToolConfirmRequest,
    user: User = Depends(require_permission("agent:run")),
    ps: PrincipalSet = Depends(get_principal_set),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """管理员确认 AI 的写操作（HITL）：approve 则执行并以确认者身份续跑，reject 则丢弃。"""
    from app.models import AgentAction

    agent = await _get_agent(db, agent_id, user.tenant_id)
    action = await db.get(AgentAction, body.action_id)
    if not action or action.tenant_id != user.tenant_id:
        raise NotFoundError("待确认操作不存在")
    if action.status != "pending":
        raise ConflictError("该操作已处理")
    if action.expires_at and action.expires_at < int(time.time() * 1000):
        action.status = "expired"
        await db.commit()
        raise ConflictError("该操作已过期")

    decision = body.decision if body.decision in ("approve", "reject") else "reject"

    # 解析工具（以确认者权限为准）
    from app.agents.tools.registry import registry, tool_allowed
    from app.middleware.auth_dep import get_user_permission_codes

    tool = registry.get_admin(action.tool_name) or registry.get_builtin(action.tool_name)
    if tool is None:
        raise NotFoundError("工具不存在")
    perms = await get_user_permission_codes(db, user)
    if not tool_allowed(tool, perms):
        raise ConflictError("你没有执行该操作的权限")

    # 原子防重放：CAS 更新状态
    from sqlalchemy import update as _upd

    new_status = "approved" if decision == "approve" else "rejected"
    res = await db.execute(
        _upd(AgentAction)
        .where(AgentAction.id == action.id, AgentAction.status == "pending")
        .values(status=new_status, approved_by=user.id)
    )
    if res.rowcount != 1:
        await db.rollback()
        raise ConflictError("该操作已被处理")
    await db.commit()
    await db.refresh(action)

    if decision == "reject":
        record_audit(
            db, action="agent.tool_write", resource_type="tool", resource_id=tool.name,
            result="failure", error="rejected", actor_id=user.id, tenant_id=user.tenant_id,
            after={"arguments": action.arguments},
        )
        await db.commit()

        async def rej_gen():
            yield f"event: action_resolved\ndata: {json.dumps({'action_id': action.id, 'decision': 'reject', 'ok': True}, ensure_ascii=False)}\n\n"
            yield f"event: delta\ndata: {json.dumps({'text': '（操作已被拒绝）'}, ensure_ascii=False)}\n\n"
            yield "event: done\ndata: {}\n\n"

        return StreamingResponse(rej_gen(), media_type="text/event-stream")

    conv_id = action.conversation_id
    action_id = action.id
    args = action.arguments or {}

    async def gen():
        async with AsyncSessionLocal() as sdb:
            from app.agents.runner import AgentRunner, load_history
            from app.middleware.auth_dep import load_principal_set

            pset = await load_principal_set(sdb, user)
            a2 = await sdb.get(AgentAction, action_id)
            tool2 = registry.get_admin(a2.tool_name) or registry.get_builtin(a2.tool_name)
            # 以确认者身份执行（谁确认谁负责）
            from app.agents.tools.base import ToolContext

            ctx = ToolContext(
                db=sdb, ps=pset, tenant_id=user.tenant_id, user_id=user.id,
                conversation_id=conv_id, agent_id=agent_id, config={},
            )
            t0 = time.time()
            a2.error = None  # 清空可能的历史残留
            try:
                result = await tool2.run(args, ctx)
                a2.status = "executed"
                a2.result = {"content": result.content[:4000], "is_error": result.is_error, "data": result.data}
                a2.executed_at = int(time.time() * 1000)
                if result.is_error:
                    a2.status = "failed"
                    a2.error = result.content[:300]
            except Exception as e:  # noqa: BLE001
                result = None
                a2.status = "failed"
                a2.error = str(e)[:300]
            await sdb.commit()
            from app.agents.tools.base import is_sensitive, tool_risk_level

            record_audit(
                sdb, action="agent.tool_write", resource_type="tool", resource_id=tool2.name,
                result="success" if result and not result.is_error else "failure",
                error=(result.content[:200] if result and result.is_error else (a2.error if a2.status == "failed" else None)),
                actor_id=user.id, tenant_id=user.tenant_id,
                before={"arguments": args},
                after={
                    "result": (result.content[:500] if result else None),
                    "sensitive": is_sensitive(tool2),
                    "risk_level": tool_risk_level(tool2),
                    "autonomy": "confirmed",  # 人工确认后执行
                },
            )
            await sdb.commit()

            yield f"event: action_resolved\ndata: {json.dumps({'action_id': action_id, 'decision': 'approve', 'ok': bool(result and not result.is_error), 'error': a2.error}, ensure_ascii=False)}\n\n"

            # 续跑：把工具结果回灌 LLM
            from app.services.context_service import load_context

            conv = await sdb.get(Conversation, conv_id)
            summary, history = await load_context(sdb, conv)
            runner = AgentRunner(sdb, agent=agent, ps=pset, conversation=conv, history=history, perms=perms, summary=summary)
            tool_text = result.content if result else f"工具执行失败：{a2.error}"
            async for evt in runner.resume_action(a2, tool2, args, tool_text):
                etype = evt.pop("type")
                yield f"event: {etype}\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
