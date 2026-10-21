"""工作流接口：graph 存取、发布、SSE 运行、运行轨迹。"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal, get_db
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.middleware.auth_dep import load_principal_set, require_permission
from app.services.audit_service import audited, record_audit
from app.models import Agent, NodeRun, User, Workflow, WorkflowRun
from app.services.permission import PrincipalSet

logger = get_logger("workflow_api")

router = APIRouter(tags=["workflow"])


class GraphIn(BaseModel):
    graph: dict
    name: str | None = None


class RunIn(BaseModel):
    inputs: dict = {}


def _run_timeout(agent: Agent | None) -> int:
    """本次运行的执行预算（秒）。agent.config.run_timeout_seconds 优先，否则用全局默认。

    没有整体上限时，loop + LLM 可以跑几十分钟并长期占用连接与配额。
    """
    from app.core.config import settings

    default = int(getattr(settings, "workflow_run_timeout_seconds", 600) or 600)
    if agent is not None:
        cfg = agent.config or {}
        try:
            v = int(cfg.get("run_timeout_seconds") or 0)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return default


async def _get_agent_workflow(db: AsyncSession, agent_id: int, tenant_id: int) -> tuple[Agent, Workflow]:
    a = await db.get(Agent, agent_id)
    if not a or a.tenant_id != tenant_id:
        raise NotFoundError("智能体不存在")
    wf = (
        await db.execute(select(Workflow).where(Workflow.agent_id == agent_id).order_by(Workflow.id.desc()))
    ).scalars().first()
    if not wf:
        wf = Workflow(tenant_id=tenant_id, agent_id=agent_id, name=a.name, graph={"nodes": [], "edges": []})
        db.add(wf)
        await db.flush()
    return a, wf


@router.get("/approvals/pending")
async def pending_approvals(
    user: User = Depends(require_permission("workflow:read")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """待我审批的工作流运行（status=waiting）。"""
    rows = (
        await db.execute(
            select(WorkflowRun).where(
                WorkflowRun.tenant_id == user.tenant_id, WorkflowRun.status == "waiting"
            ).order_by(WorkflowRun.id.desc()).limit(100)
        )
    ).scalars().all()
    # 补智能体名
    agent_ids = [r.agent_id for r in rows if r.agent_id]
    names: dict[int, str] = {}
    if agent_ids:
        for a in (await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))).scalars().all():
            names[a.id] = a.name
    return [
        {"run_id": r.id, "agent_id": r.agent_id, "agent_name": names.get(r.agent_id or 0, f"#{r.agent_id}"),
         "pending_node_id": r.pending_node_id, "input": r.input, "created_at": r.created_at}
        for r in rows
    ]


@router.get("/agents/{agent_id}/workflow")
async def get_workflow(
    agent_id: int,
    user: User = Depends(require_permission("workflow:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _, wf = await _get_agent_workflow(db, agent_id, user.tenant_id)
    return {"id": wf.id, "graph": wf.graph or {"nodes": [], "edges": []}, "version": wf.version, "status": wf.status}


@router.put("/agents/{agent_id}/workflow")
@audited("workflow.save", "workflow", id_arg="agent_id")
async def save_workflow(
    agent_id: int,
    body: GraphIn,
    user: User = Depends(require_permission("workflow:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.agents.workflow.engine import validate_graph

    _, wf = await _get_agent_workflow(db, agent_id, user.tenant_id)
    # 允许保存空图（编辑中）；非空则校验
    if body.graph.get("nodes"):
        validate_graph(body.graph)
    wf.graph = body.graph
    if body.name:
        wf.name = body.name
    await db.flush()
    return {"message": "已保存", "version": wf.version}


@router.post("/agents/{agent_id}/workflow/publish")
@audited("workflow.publish", "workflow", id_arg="agent_id")
async def publish_workflow(
    agent_id: int,
    body: dict | None = None,
    user: User = Depends(require_permission("workflow:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.agents.workflow.engine import validate_graph
    from app.models.base import utcnow
    from app.services.workflow_version_service import create_snapshot

    _, wf = await _get_agent_workflow(db, agent_id, user.tenant_id)
    validate_graph(wf.graph or {})
    wf.status = "published"
    wf.version = (wf.version or 1) + 1
    wf.published_at = utcnow()
    # 发布即存档：失败无法「保存」，改坏可以回滚
    snap = await create_snapshot(
        db, wf=wf, user_id=user.id, note=(body or {}).get("note") if body else None,
    )
    await db.flush()
    return {"message": "已发布", "version": wf.version, "snapshot_id": snap.id}


@router.get("/agents/{agent_id}/workflow/versions")
async def list_workflow_versions(
    agent_id: int,
    user: User = Depends(require_permission("workflow:read")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """已发布的历史版本（含节点数，便于辨认）。"""
    from app.services import workflow_version_service as S

    return await S.list_versions(db, tenant_id=user.tenant_id, agent_id=agent_id)


@router.post("/agents/{agent_id}/workflow/versions/{version_id}/rollback")
@audited("workflow.rollback", "workflow", id_arg="agent_id")
async def rollback_workflow(
    agent_id: int,
    version_id: int,
    user: User = Depends(require_permission("workflow:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """回滚到某个已发布版本（回滚也会产生新快照，可再回退）。"""
    from app.services import workflow_version_service as S

    r = await S.rollback(
        db, tenant_id=user.tenant_id, agent_id=agent_id,
        version_id=version_id, user_id=user.id,
    )
    await db.commit()
    return r


@router.get("/agents/{agent_id}/workflow/export")
async def export_workflow(
    agent_id: int,
    user: User = Depends(require_permission("workflow:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """导出为可移植 JSON（不含密钥与租户信息）。"""
    from app.services.workflow_version_service import export_workflow as _export

    a, wf = await _get_agent_workflow(db, agent_id, user.tenant_id)
    return _export(agent=a, wf=wf)


@router.post("/workflow/import")
async def import_workflow(
    body: dict,
    user: User = Depends(require_permission("workflow:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """导入工作流 JSON，创建为新的工作流型智能体（不覆盖现有）。"""
    from app.services.workflow_version_service import import_workflow as _import

    r = await _import(
        db, tenant_id=user.tenant_id, user_id=user.id, data=body,
    )
    await db.commit()
    return r


@router.post("/agents/{agent_id}/workflow/run")
async def run_workflow(
    agent_id: int,
    body: RunIn,
    user: User = Depends(require_permission("workflow:run")),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    from app.agents.workflow.runner import stream_workflow

    a = await db.get(Agent, agent_id)
    if not a or a.tenant_id != user.tenant_id:
        raise NotFoundError("智能体不存在")
    wf = (
        await db.execute(select(Workflow).where(Workflow.agent_id == agent_id).order_by(Workflow.id.desc()))
    ).scalars().first()
    if not wf or not wf.graph:
        raise NotFoundError("工作流未配置")

    run = WorkflowRun(
        tenant_id=user.tenant_id, workflow_id=wf.id, agent_id=agent_id, user_id=user.id,
        status="running", input=body.inputs, graph_snapshot=wf.graph, created_at=int(time.time() * 1000),
    )
    db.add(run)
    await db.commit()
    run_id = run.id
    graph = wf.graph
    inputs = body.inputs

    async def gen():
        pset = await load_principal_set(db, user)
        # 先推 run_id，前端「停止」按钮需要它
        yield f"event: meta\ndata: {json.dumps({'run_id': run_id}, ensure_ascii=False)}\n\n"
        async for evt in stream_workflow(
            graph=graph, inputs=inputs, run_id=run_id, tenant_id=user.tenant_id, ps=pset,
            default_model_config_id=a.model_config_id, default_kb_ids=a.kb_ids,
            default_system=a.system_prompt, default_temperature=(a.config or {}).get("temperature"),
            timeout_seconds=_run_timeout(a),
        ):
            etype = evt.get("type", "node")
            yield f"event: {etype}\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/agents/{agent_id}/workflow/runs")
async def list_runs(
    agent_id: int,
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,
    user: User = Depends(require_permission("workflow:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """列出该工作流的历次运行。status 可按状态筛选（failed / waiting / success ...）。"""
    a = await db.get(Agent, agent_id)
    if not a or a.tenant_id != user.tenant_id:
        raise NotFoundError("智能体不存在")

    cond = [WorkflowRun.agent_id == agent_id, WorkflowRun.tenant_id == user.tenant_id]
    if status:
        # 支持逗号分隔多状态，如 ?status=failed,waiting
        wanted = [s.strip() for s in status.split(",") if s.strip()]
        if wanted:
            cond.append(WorkflowRun.status.in_(wanted))

    rows = (
        await db.execute(
            select(WorkflowRun)
            .where(*cond)
            .order_by(WorkflowRun.id.desc())
            .limit(min(limit, 100)).offset(offset)
        )
    ).scalars().all()

    # 统计各状态数量（供筛选标签显示，不随筛选变化）
    from sqlalchemy import func as _func

    counts_rows = (
        await db.execute(
            select(WorkflowRun.status, _func.count())
            .where(WorkflowRun.agent_id == agent_id, WorkflowRun.tenant_id == user.tenant_id)
            .group_by(WorkflowRun.status)
        )
    ).all()
    status_counts = {str(s): int(n) for s, n in counts_rows}

    return {
        "runs": [
            {"id": r.id, "status": r.status, "latency_ms": r.latency_ms,
             "created_at": r.created_at, "finished_at": r.finished_at,
             "error_msg": r.error_msg, "input": r.input,
             # 渠道回推状态：审批/异步完成把结果推回客户时写入，供「重发」按钮判断
             "channel_reply": (r.output or {}).get("channel_reply")}
            for r in rows
        ],
        "status_counts": status_counts,
    }


@router.get("/workflow-runs/{run_id}")
async def get_run(
    run_id: int,
    user: User = Depends(require_permission("workflow:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    run = await db.get(WorkflowRun, run_id)
    if not run or run.tenant_id != user.tenant_id:
        raise NotFoundError("运行记录不存在")
    nodes = (
        await db.execute(select(NodeRun).where(NodeRun.run_id == run_id).order_by(NodeRun.seq))
    ).scalars().all()
    return {
        "run": {
            "id": run.id, "status": run.status, "input": run.input, "output": run.output,
            "error_msg": run.error_msg, "latency_ms": run.latency_ms,
            "pending_node_id": run.pending_node_id,
        },
        "nodes": [
            {"node_id": n.node_id, "node_type": n.node_type, "seq": n.seq, "status": n.status,
             "input": n.input, "output": n.output, "error_msg": n.error_msg}
            for n in nodes
        ],
    }


class ApproveIn(BaseModel):
    decision: str  # approve | reject
    comment: str | None = None


class GenerateIn(BaseModel):
    prompt: str
    model_config_id: int | None = None


@router.post("/agents/{agent_id}/workflow/generate")
async def generate_workflow(
    agent_id: int,
    body: GenerateIn,
    user: User = Depends(require_permission("workflow:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """自然语言 → 工作流图。只生成不落库，由前端确认后再保存。"""
    from app.services.workflow_ai_service import generate_graph_from_text

    a = await db.get(Agent, agent_id)
    if not a or a.tenant_id != user.tenant_id:
        raise NotFoundError("智能体不存在")

    model_id = body.model_config_id or a.model_config_id
    r = await generate_graph_from_text(
        db, tenant_id=user.tenant_id, prompt=body.prompt, model_config_id=model_id,
    )
    record_audit(
        db, action="workflow.ai_generate", resource_type="agent", resource_id=agent_id,
        after={"nodes": r["node_count"], "prompt": body.prompt[:200]},
    )
    return r


class DebugIn(BaseModel):
    message: str
    # 前端维护的多轮历史（[{role, content}]），避免为调试创建真实会话记录
    history: list[dict] = []
    model_config_id: int | None = None


class CaseIn(BaseModel):
    name: str
    input: dict
    expected: str | None = None
    assert_contains: bool = True
    # 多轮历史：[{role: "user"|"assistant", content: "..."}]，空=单轮
    history: list[dict] | None = None


class CaseUpdateIn(BaseModel):
    name: str | None = None
    input: dict | None = None
    expected: str | None = None
    assert_contains: bool | None = None
    history: list[dict] | None = None


class RunCasesIn(BaseModel):
    case_ids: list[int] | None = None


# ---- 测试用例 ----


@router.get("/agents/{agent_id}/workflow/cases")
async def list_cases(
    agent_id: int,
    user: User = Depends(require_permission("workflow:read")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """列出该智能体的测试用例（含上次执行结果）。"""
    from app.services import workflow_test_service as S

    return await S.list_cases(db, tenant_id=user.tenant_id, agent_id=agent_id)


@router.post("/agents/{agent_id}/workflow/cases")
async def create_case(
    agent_id: int,
    body: CaseIn,
    user: User = Depends(require_permission("workflow:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import workflow_test_service as S

    r = await S.create_case(
        db, tenant_id=user.tenant_id, agent_id=agent_id,
        name=body.name, input_data=body.input,
        expected=body.expected, assert_contains=body.assert_contains,
        history=body.history,
    )
    await db.commit()
    return r


@router.patch("/workflow-cases/{case_id}")
async def update_case(
    case_id: int,
    body: CaseUpdateIn,
    user: User = Depends(require_permission("workflow:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import workflow_test_service as S

    r = await S.update_case(
        db, tenant_id=user.tenant_id, case_id=case_id,
        **body.model_dump(exclude_unset=True),
    )
    await db.commit()
    return r


@router.delete("/workflow-cases/{case_id}")
async def delete_case(
    case_id: int,
    user: User = Depends(require_permission("workflow:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import workflow_test_service as S

    await S.delete_case(db, tenant_id=user.tenant_id, case_id=case_id)
    await db.commit()
    return {"message": "已删除"}


@router.post("/agents/{agent_id}/workflow/cases/run")
async def run_cases(
    agent_id: int,
    body: RunCasesIn,
    user: User = Depends(require_permission("workflow:run")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """批量回归：默认跑全部用例。每跑一条都跑完整工作流，注意耗时。"""
    from app.services import workflow_test_service as S

    r = await S.run_cases(
        db, tenant_id=user.tenant_id, agent_id=agent_id, case_ids=body.case_ids,
    )
    await db.commit()
    return r


@router.post("/agents/{agent_id}/workflow/debug")
async def debug_workflow(
    agent_id: int,
    body: DebugIn,
    user: User = Depends(require_permission("workflow:run")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """调试对话：把一条消息跑过整条工作流，返回回复与执行轨迹。

    与正式运行的区别：
    - 不创建 WorkflowRun 记录（调试不污染运行历史）
    - 历史由前端传回，服务端不落库
    - 返回每个节点的输入/输出，供画布做「实时路径演示」
    """
    import time as _t

    from sqlalchemy import select as _select

    from app.agents.workflow.engine import NodeContext, WorkflowPaused, execute_graph
    from app.agents.workflow.runner import start_input_name

    a = await db.get(Agent, agent_id)
    if not a or a.tenant_id != user.tenant_id:
        raise NotFoundError("智能体不存在")

    wf = (
        await db.execute(
            _select(Workflow).where(Workflow.agent_id == agent_id).order_by(Workflow.id.desc())
        )
    ).scalars().first()
    if not wf or not wf.graph or not (wf.graph.get("nodes")):
        raise NotFoundError("该智能体还没有配置工作流")

    # 组装输入：消息 + 历史（与渠道场景保持一致的字段语义）
    hist_lines: list[str] = []
    for h in (body.history or [])[-10:]:
        if not isinstance(h, dict):
            continue
        role = "客户" if h.get("role") == "user" else "客服"
        content = str(h.get("content") or "").strip()
        if content:
            hist_lines.append(f"{role}：{content[:300]}")

    inputs = {
        start_input_name(wf.graph): body.message,
        "history": "\n".join(hist_lines),
        "attachments": [],
    }

    pset = await load_principal_set(db, user)
    trace: list[dict] = []

    async def emit(evt: dict) -> None:
        if evt.get("type") == "node_finished":
            trace.append({
                "node_id": evt.get("node_id"),
                "status": evt.get("status"),
                "latency_ms": evt.get("latency_ms"),
                "input": evt.get("input"),
                "output": evt.get("output"),
                "error": evt.get("error"),
            })

    ctx = NodeContext(
        db=db, ps=pset, run_id=0, tenant_id=user.tenant_id, inputs=inputs, emit=emit,
        default_model_config_id=body.model_config_id or a.model_config_id,
        default_kb_ids=a.kb_ids, default_system=a.system_prompt,
        default_temperature=(a.config or {}).get("temperature"),
    )

    t0 = _t.time()
    err: str | None = None
    paused: dict | None = None
    final: dict = {}
    try:
        final, _records = await execute_graph(wf.graph, inputs, ctx)
    except WorkflowPaused as p:
        paused = {"node_id": p.node_id, **(p.info or {})}
    except Exception as e:  # noqa: BLE001
        err = str(e)[:300]

    if err:
        text = f"执行失败：{err}"
    elif paused:
        text = f"流程停在人工审批（{paused.get('title') or paused.get('node_id')}）。"
    elif isinstance(final, dict):
        text = str(final.get("output", final))
    else:
        text = str(final)

    return {
        "text": text,
        "error": err,
        "paused": paused,
        "trace": trace,
        "latency_ms": int((_t.time() - t0) * 1000),
        "final": final,
    }


@router.get("/workflow/node-types")
async def list_node_types(
    user: User = Depends(require_permission("workflow:read")),
) -> dict:
    """返回可用节点类型及规格（供前端面板分组 / AI 提示词共用）。"""
    from app.agents.workflow.nodes_extra import NODE_SPECS_EXTRA
    from app.services.workflow_ai_service import NODE_SPECS

    return {
        "types": sorted(NODE_SPECS.keys()),
        "specs": NODE_SPECS,
        "extra": NODE_SPECS_EXTRA,
    }


class TemplateSaveIn(BaseModel):
    name: str | None = None
    category: str = "自定义"
    description: str | None = None
    # 是否把该工作流的测试用例一并存进模板（默认是）
    include_cases: bool = True


class TemplateApplyIn(BaseModel):
    # 二选一：内置模板的 key，或用户模板的 id
    key: str | None = None
    template_id: int | None = None
    name: str | None = None


@router.get("/workflow/templates")
async def list_templates(
    user: User = Depends(require_permission("workflow:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """工作流模板：内置场景模板 + 本租户保存的模板。"""
    from app.services import workflow_template_service as S

    return {
        "builtin": S.list_builtin(),
        "user": await S.list_user_templates(db, tenant_id=user.tenant_id),
        "public": await S.list_public_templates(db, tenant_id=user.tenant_id),
    }


@router.post("/agents/{agent_id}/workflow/save-as-template")
async def save_as_template(
    agent_id: int,
    body: TemplateSaveIn,
    user: User = Depends(require_permission("workflow:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """把当前工作流快照保存为模板。"""
    from app.services import workflow_template_service as S

    r = await S.save_template(
        db, tenant_id=user.tenant_id, user_id=user.id, agent_id=agent_id,
        name=body.name, category=body.category, description=body.description,
        include_cases=body.include_cases,
    )
    await db.commit()
    return r


@router.delete("/workflow/templates/{template_id}")
async def delete_template(
    template_id: int,
    user: User = Depends(require_permission("workflow:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import workflow_template_service as S

    await S.delete_template(db, tenant_id=user.tenant_id, template_id=template_id)
    await db.commit()
    return {"message": "已删除"}


@router.post("/workflow/templates/{template_id}/publish")
async def publish_template(
    template_id: int,
    public: bool = True,
    allow_high_risk: bool = False,
    user: User = Depends(require_permission("workflow:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """申请发布 / 取消发布到公共模板库。

    发布走审核流：先做风险扫描，再进入 pending 等管理员审核。
    含 code 节点的模板默认被拦下（allow_high_risk 仅管理员可用）。
    """
    from app.services import workflow_template_service as S

    # 只有管理员能放开高风险模板
    if allow_high_risk and not user.is_admin:
        allow_high_risk = False

    r = await S.set_visibility(
        db, tenant_id=user.tenant_id, template_id=template_id, public=public,
        allow_high_risk=allow_high_risk,
    )
    record_audit(
        db, action="workflow.template_publish", resource_type="workflow_template",
        resource_id=template_id,
        after={"public": public, "risk": r.get("risk_level"), "blocked": r.get("blocked", False)},
    )
    await db.commit()
    return r


class TemplateReviewIn(BaseModel):
    approve: bool
    note: str | None = None


class TemplateRateIn(BaseModel):
    rating: int
    comment: str | None = None


@router.get("/workflow/templates/pending")
async def list_pending_template_reviews(
    user: User = Depends(require_permission("workflow:manage")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """待审核的公共模板（跨租户，需 workflow:manage）。"""
    from app.services import workflow_template_service as S

    return await S.list_pending_reviews(db)


@router.post("/workflow/templates/{template_id}/review")
async def review_template(
    template_id: int,
    body: TemplateReviewIn,
    user: User = Depends(require_permission("workflow:manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """管理员审核公共模板：通过则公开，拒绝则退回私有。"""
    from app.services import workflow_template_service as S

    r = await S.review_template(
        db, template_id=template_id, approve=body.approve,
        note=body.note, reviewer_id=user.id,
    )
    record_audit(db, action="workflow.template_review", resource_type="workflow_template",
                 resource_id=template_id, after={"approve": body.approve, "note": body.note})
    await db.commit()
    return r


@router.post("/workflow/templates/{template_id}/rate")
async def rate_template(
    template_id: int,
    body: TemplateRateIn,
    user: User = Depends(require_permission("workflow:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """给公共模板评分（1-5，每租户一次，可修改）。"""
    from app.services import workflow_template_service as S

    r = await S.rate_template(
        db, tenant_id=user.tenant_id, user_id=user.id, template_id=template_id,
        rating=body.rating, comment=body.comment,
    )
    await db.commit()
    return r


@router.post("/workflow/templates/apply")
async def apply_template(
    body: TemplateApplyIn,
    user: User = Depends(require_permission("workflow:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """套用模板：创建一个新的工作流型智能体（不覆盖现有）。"""
    from app.services import workflow_template_service as S

    cases: list[dict] = []
    bump_template_id: int | None = None

    if body.key:
        t = S.get_builtin(body.key)
        if not t:
            raise NotFoundError("内置模板不存在")
        graph, name, desc = t["graph"], t["name"], t["description"]
        cases = list(t.get("cases") or [])
    elif body.template_id:
        from app.models import WorkflowTemplate

        r = await db.get(WorkflowTemplate, body.template_id)
        # 自己的模板 OR 公共模板都可套用
        is_own = r is not None and r.tenant_id == user.tenant_id
        is_public = r is not None and r.visibility == "public"
        if not r or not (is_own or is_public):
            raise NotFoundError("模板不存在或未公开")
        graph, name, desc = r.graph or {}, r.name, r.description or ""
        cases = list(r.cases or [])
        bump_template_id = r.id
    else:
        raise ValidationError("需提供 key 或 template_id")

    result = await S.apply_template(
        db, tenant_id=user.tenant_id, user_id=user.id,
        graph=graph, name=body.name or name, description=desc, cases=cases,
    )
    if bump_template_id is not None:
        from app.models import WorkflowTemplate as _WT

        row = await db.get(_WT, bump_template_id)
        if row is not None:
            row.apply_count = (row.apply_count or 0) + 1
    await db.commit()
    return result


@router.get("/agents/{agent_id}/workflow/usage")
async def workflow_usage(
    agent_id: int,
    days: int = 30,
    user: User = Depends(require_permission("workflow:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """按天统计该工作流的模型调用用量（Token 与调用次数）。

    数据来自 WorkflowRun.usage —— 只有真正调过 LLM 的运行才有值，
    纯数据变换的工作流会是空。
    """
    from sqlalchemy import select as _select

    a = await db.get(Agent, agent_id)
    if not a or a.tenant_id != user.tenant_id:
        raise NotFoundError("智能体不存在")

    days = max(1, min(int(days or 30), 365))
    since_ms = int((time.time() - days * 86400) * 1000)

    rows = (
        await db.execute(
            _select(WorkflowRun.created_at, WorkflowRun.usage)
            .where(
                WorkflowRun.agent_id == agent_id,
                WorkflowRun.tenant_id == user.tenant_id,
                WorkflowRun.created_at >= since_ms,
                WorkflowRun.usage.is_not(None),
            )
        )
    ).all()

    by_day: dict[str, dict] = {}
    total = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0, "runs": 0}
    for created_at, usage in rows:
        if not isinstance(usage, dict):
            continue
        # 按本地日期归组（UTC 偏移在统计场景可接受；如需精确可加时区参数）
        day = time.strftime("%Y-%m-%d", time.localtime((created_at or 0) / 1000))
        bucket = by_day.setdefault(
            day, {"date": day, "prompt_tokens": 0, "completion_tokens": 0, "calls": 0, "runs": 0}
        )
        for k in ("prompt_tokens", "completion_tokens", "calls"):
            v = int(usage.get(k) or 0)
            bucket[k] += v
            total[k] += v
        bucket["runs"] += 1
        total["runs"] += 1

    series = sorted(by_day.values(), key=lambda x: x["date"])
    return {"days": days, "total": total, "series": series}


@router.post("/workflow-runs/{run_id}/cancel")
async def cancel_run(
    run_id: int,
    user: User = Depends(require_permission("workflow:run")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """取消正在执行的工作流运行。

    只能取消本进程内正在跑的任务（多副本部署时请求需落在同一副本上）；
    任务不在本进程时返回 ok=false，但已把状态标记为 canceled 作为兜底，
    避免前端一直显示「运行中」。
    """
    from sqlalchemy import update as _upd

    from app.agents.workflow import cancel as wf_cancel

    run = await db.get(WorkflowRun, run_id)
    if not run or run.tenant_id != user.tenant_id:
        raise NotFoundError("运行记录不存在")
    if run.status not in ("running", "waiting"):
        raise ConflictError(f"该运行当前状态为 {run.status}，无法取消")

    signalled = wf_cancel.cancel(run_id)

    if not signalled:
        # 任务不在本进程（或已结束）：直接把状态置为 canceled
        res = await db.execute(
            _upd(WorkflowRun)
            .where(WorkflowRun.id == run_id, WorkflowRun.status.in_(("running", "waiting")))
            .values(status="canceled", finished_at=int(time.time() * 1000),
                    error_msg="已手动取消")
        )
        if res.rowcount != 1:
            await db.rollback()
            raise ConflictError("该运行已被处理")
        await db.commit()

    record_audit(db, action="workflow.cancel", resource_type="workflow_run",
                 resource_id=run_id, after={"signalled": signalled})
    await db.commit()
    return {
        "ok": True,
        "signalled": signalled,
        "message": "已请求取消" if signalled else "已标记为取消（任务不在本进程）",
    }


@router.post("/workflow-runs/{run_id}/resend")
async def resend_run_result(
    run_id: int,
    user: User = Depends(require_permission("workflow:run")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """补发工作流结果到原渠道客户（上次发送失败时用）。"""
    from app.services.workflow_notify import resend_workflow_result

    run = await db.get(WorkflowRun, run_id)
    if not run or run.tenant_id != user.tenant_id:
        raise NotFoundError("运行记录不存在")

    cached = (run.output or {}).get("channel_reply") or {}
    if not cached.get("text"):
        raise ConflictError("该运行没有可补发的渠道回复")

    ok = await resend_workflow_result(db, run=run)
    record_audit(db, action="workflow.resend", resource_type="workflow_run",
                 resource_id=run_id, after={"sent": ok})
    await db.commit()
    return {"ok": ok, "message": "已重新发送" if ok else "发送失败（渠道可能未连接）"}


@router.post("/workflow-runs/{run_id}/approve")
async def approve_run(
    run_id: int,
    body: ApproveIn,
    user: User = Depends(require_permission("workflow:run")),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """人工审批：通过则从快照恢复执行（复用缓存，不重跑上游），拒绝则终止。"""
    from sqlalchemy import update as _upd

    from app.agents.workflow.runner import stream_workflow

    run = await db.get(WorkflowRun, run_id)
    if not run or run.tenant_id != user.tenant_id:
        raise NotFoundError("运行记录不存在")
    if run.status != "waiting":
        raise ConflictError("该运行不在等待审批状态")
    decision = body.decision if body.decision in ("approve", "reject") else "reject"
    pending = run.pending_node_id

    # CAS 防重复审批
    res = await db.execute(
        _upd(WorkflowRun).where(WorkflowRun.id == run_id, WorkflowRun.status == "waiting")
        .values(status="running" if decision == "approve" else "canceled")
    )
    if res.rowcount != 1:
        await db.rollback()
        raise ConflictError("该运行已被处理")
    await db.commit()
    record_audit(db, action="workflow.approve", resource_type="workflow_run",
                 resource_id=run_id, after={"decision": decision})
    await db.commit()

    if decision == "reject":
        # 拒绝：除了给前端 SSE，还要通知渠道客户（若是渠道发起的运行）
        async def rej():
            try:
                from app.services.workflow_notify import notify_approval_decision

                await notify_approval_decision(db, run=run, decision="reject")
            except Exception:  # noqa: BLE001
                logger.exception("workflow_reject_notify_failed", run_id=run_id)
            yield f"event: run_finished\ndata: {json.dumps({'status': 'canceled', 'run_id': run_id}, ensure_ascii=False)}\n\n"
        return StreamingResponse(rej(), media_type="text/event-stream")

    graph = run.graph_snapshot or {}
    inputs = run.input or {}
    resume = run.state or {}
    approval = {pending: "approve"} if pending else {}
    agent = await db.get(Agent, run.agent_id) if run.agent_id else None

    async def gen():
        pset = await load_principal_set(db, user)
        final_text: str | None = None
        try:
            async for evt in stream_workflow(
                graph=graph, inputs=inputs, run_id=run_id, tenant_id=user.tenant_id, ps=pset,
                default_model_config_id=agent.model_config_id if agent else None,
                default_kb_ids=agent.kb_ids if agent else None,
                default_system=agent.system_prompt if agent else None,
                default_temperature=(agent.config or {}).get("temperature") if agent else None,
                approval=approval, resume=resume,
                timeout_seconds=_run_timeout(agent),
            ):
                etype = evt.get("type", "node")
                if etype == "run_finished":
                    final_text = evt.get("text")
                yield f"event: {etype}\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n"
        finally:
            # 结果回推渠道客户。放在 finally：即便前端 SSE 断开，
            # stream_workflow 的后台 task 仍会跑完，这里照样能把结果送出去。
            # 判断依据是「有来源渠道快照」而不是 conversation_id ——
            # 后台运行也可能挂会话，但没有渠道身份，回推必然失败。
            has_origin = bool(
                getattr(run, "origin_external_user", None) or run.conversation_id
            )
            if has_origin:
                try:
                    from app.core.db import AsyncSessionLocal
                    from app.models import WorkflowRun as _WR
                    from app.services.workflow_notify import notify_approval_decision

                    async with AsyncSessionLocal() as ndb:
                        fresh = await ndb.get(_WR, run_id)
                        if fresh is not None:
                            await notify_approval_decision(
                                ndb, run=fresh, decision="approve", result_text=final_text,
                            )
                except Exception:  # noqa: BLE001
                    logger.exception("workflow_approve_notify_failed", run_id=run_id)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
