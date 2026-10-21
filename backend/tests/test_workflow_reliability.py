"""工作流可靠性：审批暂停、超时、取消、版本快照、导入导出、分支持久化。"""
from __future__ import annotations

import asyncio
import hashlib

import pytest
from sqlalchemy import select

from app.agents.workflow import cancel as wf_cancel
from app.agents.workflow.engine import NodeContext, WorkflowPaused, execute_graph
from app.core.db import AsyncSessionLocal, init_models
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import hash_password
from app.models import (
    Agent, NodeRun, Tenant, User, WebhookToken, Workflow, WorkflowRun, WorkflowVersion,
)
from app.services import workflow_version_service as V
from app.services.permission import PrincipalSet


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


GRAPH = {
    "nodes": [
        {"id": "s", "type": "start", "data": {"inputs": [{"name": "query"}]}},
        {"id": "t", "type": "transform", "data": {"value": "{{s.query}}", "op": "to_text"}},
        {"id": "e", "type": "end", "data": {"output": "{{t.output}}"}},
    ],
    "edges": [{"source": "s", "target": "t"}, {"source": "t", "target": "e"}],
}

APPROVAL_GRAPH = {
    "nodes": [
        {"id": "s", "type": "start", "data": {"inputs": [{"name": "query"}]}},
        {"id": "ap", "type": "approval", "data": {"title": "确认"}},
        {"id": "e", "type": "end", "data": {"output": "ok"}},
    ],
    "edges": [{"source": "s", "target": "ap"}, {"source": "ap", "target": "e"}],
}


async def _setup(slug="rel"):
    await init_models()
    async with AsyncSessionLocal() as db:
        t = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
        if not t:
            t = Tenant(name=slug, slug=slug)
            db.add(t)
            await db.flush()
        u = (await db.execute(select(User).where(User.username == f"{slug}_u"))).scalar_one_or_none()
        if not u:
            u = User(tenant_id=t.id, username=f"{slug}_u", password_hash=hash_password("x"), is_admin=True)
            db.add(u)
            await db.flush()
        a = Agent(tenant_id=t.id, name=f"{slug}助手", slug=f"{slug}-bot",
                  type="workflow", owner_id=u.id, status="published")
        db.add(a)
        await db.flush()
        wf = (await db.execute(select(Workflow).where(Workflow.agent_id == a.id))).scalars().first()
        if not wf:
            wf = Workflow(tenant_id=t.id, agent_id=a.id, name="t", graph=GRAPH)
            db.add(wf)
        await db.commit()
        return {"tenant_id": t.id, "user_id": u.id, "agent_id": a.id, "wf_id": wf.id}


# ============================================================ 取消注册表


def test_cancel_registry_tracks_task():
    async def _t():
        async def _work():
            async with wf_cancel.track(9999):
                assert wf_cancel.is_running(9999)
                await asyncio.sleep(0.01)
            assert not wf_cancel.is_running(9999)

        await asyncio.create_task(_work())
        assert not wf_cancel.is_running(9999), "退出后应自动注销"
    _run(_t())


def test_cancel_requests_task_cancellation():
    async def _t():
        started = asyncio.Event()
        cancelled = {"v": False}

        async def _work():
            async with wf_cancel.track(8888):
                started.set()
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    cancelled["v"] = True
                    raise

        task = asyncio.create_task(_work())
        await started.wait()
        ok = wf_cancel.cancel(8888)
        assert ok is True
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert cancelled["v"] is True
    _run(_t())


def test_cancel_unknown_run_returns_false():
    assert wf_cancel.cancel(123456) is False


# ============================================================ 审批暂停（定时任务路径）


def test_approval_pauses_in_scheduler_path():
    """定时任务场景：审批节点必须抛 WorkflowPaused，而不是正常返回。"""
    async def _t():
        ctx = NodeContext(db=None, ps=PrincipalSet(user_id=1, tenant_id=1), run_id=1, tenant_id=1)
        with pytest.raises(WorkflowPaused) as ei:
            await execute_graph(APPROVAL_GRAPH, {"query": "x"}, ctx)
        assert ei.value.node_id == "ap"
    _run(_t())


def test_scheduler_catches_paused_separately():
    """_do_run 必须在通用 except 之前单独处理 WorkflowPaused。"""
    import inspect

    from app.tasks import scheduler_tasks as S

    src = inspect.getsource(S._do_run)
    idx_paused = src.find("except WorkflowPaused")
    idx_generic = src.find("except Exception")
    assert idx_paused > 0, "缺少 WorkflowPaused 分支"
    assert idx_generic > 0
    assert idx_paused < idx_generic, "WorkflowPaused 必须写在通用 except 之前"


def test_scheduler_paused_signal_class():
    from app.tasks.scheduler_tasks import _SchedulerPaused

    e = _SchedulerPaused("approve_1", {"title": "确认"})
    assert e.node_id == "approve_1"
    assert e.info["title"] == "确认"


def test_do_run_annotation_matches_returns():
    """返回 5 元（含 attachments），注解必须一致。"""
    import inspect
    import typing

    from app.tasks import scheduler_tasks as S

    sig = inspect.signature(S._do_run)
    ann = str(sig.return_annotation)
    assert "list" in ann, f"注解应包含附件列表，实际为 {ann}"


# ============================================================ 整体超时


def test_stream_workflow_timeout_marks_failed(monkeypatch):
    """整条链超时要中止并落 failed，而不是一直挂着。"""
    async def _t():
        from app.agents.workflow.runner import stream_workflow

        ctx_data = await _setup("timeout")

        async def _slow_exec(graph, inputs, ctx, resume=None):
            await asyncio.sleep(5)
            return {}, []

        import app.agents.workflow.runner as R

        monkeypatch.setattr(R, "execute_graph", _slow_exec)

        events = []
        async for evt in stream_workflow(
            graph=GRAPH, inputs={"query": "x"}, run_id=1, tenant_id=ctx_data["tenant_id"],
            ps=PrincipalSet(user_id=1, tenant_id=ctx_data["tenant_id"]),
            timeout_seconds=0.05,
        ):
            events.append(evt)

        types = [e.get("type") for e in events]
        assert "run_timeout" in types
        final = [e for e in events if e.get("type") == "run_finished"][0]
        assert final["status"] == "failed"
        assert "超时" in (final.get("err") or "")
    _run(_t())


# ============================================================ 版本快照


def test_publish_creates_snapshot():
    async def _t():
        ctx = await _setup("ver")
        async with AsyncSessionLocal() as db:
            wf = await db.get(Workflow, ctx["wf_id"])
            wf.version = 2
            snap = await V.create_snapshot(db, wf=wf, user_id=ctx["user_id"], note="首发")
            await db.commit()
            assert snap.version == 2
            assert snap.graph["nodes"]
            rows = await V.list_versions(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"])
            assert any(r["id"] == snap.id for r in rows)
    _run(_t())


def test_snapshot_rejects_empty_graph():
    async def _t():
        ctx = await _setup("verempty")
        async with AsyncSessionLocal() as db:
            wf = await db.get(Workflow, ctx["wf_id"])
            wf.graph = {"nodes": [], "edges": []}
            with pytest.raises(ValidationError):
                await V.create_snapshot(db, wf=wf, user_id=ctx["user_id"])
    _run(_t())


def test_rollback_restores_graph():
    async def _t():
        ctx = await _setup("roll")
        async with AsyncSessionLocal() as db:
            wf = await db.get(Workflow, ctx["wf_id"])
            wf.version = 1
            snap = await V.create_snapshot(db, wf=wf, user_id=ctx["user_id"], note="v1")
            await db.commit()
            snap_id = snap.id

            # 改坏
            wf.graph = {
                "nodes": [
                    {"id": "s", "type": "start", "data": {"inputs": []}},
                    {"id": "e", "type": "end", "data": {"output": "changed"}},
                ],
                "edges": [{"source": "s", "target": "e"}],
            }
            await db.commit()

            r = await V.rollback(
                db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                version_id=snap_id, user_id=ctx["user_id"],
            )
            await db.commit()
            assert "已回滚" in r["message"]

            wf2 = await db.get(Workflow, ctx["wf_id"])
            assert any(n["id"] == "t" for n in wf2.graph["nodes"]), "应恢复原始节点"
            # 回滚本身也留快照，可再回退
            vs = await V.list_versions(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"])
            assert len(vs) >= 2
    _run(_t())


def test_rollback_rejects_foreign_version():
    async def _t():
        a = await _setup("rb1")
        b = await _setup("rb2")
        async with AsyncSessionLocal() as db:
            wf_a = await db.get(Workflow, a["wf_id"])
            snap = await V.create_snapshot(db, wf=wf_a, user_id=a["user_id"])
            await db.commit()
            with pytest.raises(NotFoundError):
                await V.rollback(
                    db, tenant_id=b["tenant_id"], agent_id=b["agent_id"],
                    version_id=snap.id, user_id=b["user_id"],
                )
    _run(_t())


# ============================================================ 导入 / 导出


def test_export_has_no_secrets():
    async def _t():
        ctx = await _setup("exp")
        async with AsyncSessionLocal() as db:
            a = await db.get(Agent, ctx["agent_id"])
            wf = await db.get(Workflow, ctx["wf_id"])
            data = V.export_workflow(agent=a, wf=wf)
            assert data["format"] == "rag-workflow"
            assert data["graph"]["nodes"]
            # 不应携带任何身份/密钥字段
            for k in ("tenant_id", "owner_id", "config", "id"):
                assert k not in data or k == "config"
            assert "api_key" not in str(data).lower()
            assert "secret" not in str(data).lower()
    _run(_t())


def test_import_creates_new_agent():
    async def _t():
        ctx = await _setup("imp")
        payload = {
            "format": "rag-workflow",
            "name": "导入的流程",
            "graph": GRAPH,
            "system_prompt": "你是助手",
        }
        async with AsyncSessionLocal() as db:
            r = await V.import_workflow(
                db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"], data=payload,
            )
            await db.commit()
            assert r["agent_id"] != ctx["agent_id"], "导入应创建新智能体，不覆盖"
            ag = await db.get(Agent, r["agent_id"])
            assert ag.type == "workflow"
            assert ag.name == "导入的流程"
            wf = await db.get(Workflow, r["workflow_id"])
            assert wf.graph["nodes"]
    _run(_t())


def test_import_dedupes_slug():
    async def _t():
        ctx = await _setup("impdup")
        payload = {"format": "rag-workflow", "name": "同名", "graph": GRAPH}
        async with AsyncSessionLocal() as db:
            r1 = await V.import_workflow(db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"], data=payload)
            r2 = await V.import_workflow(db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"], data=payload)
            await db.commit()
            a1 = await db.get(Agent, r1["agent_id"])
            a2 = await db.get(Agent, r2["agent_id"])
            assert a1.slug != a2.slug
    _run(_t())


def test_import_rejects_empty_graph():
    async def _t():
        ctx = await _setup("impbad")
        async with AsyncSessionLocal() as db:
            with pytest.raises(ValidationError):
                await V.import_workflow(
                    db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"],
                    data={"format": "rag-workflow", "name": "空", "graph": {"nodes": []}},
                )
    _run(_t())


def test_import_rejects_unknown_format():
    async def _t():
        ctx = await _setup("impfmt")
        async with AsyncSessionLocal() as db:
            with pytest.raises(ValidationError, match="格式"):
                await V.import_workflow(
                    db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"],
                    data={"format": "n8n", "name": "x", "graph": GRAPH},
                )
    _run(_t())


# ============================================================ 并行分支


def test_parallel_branch_emits_events_and_records():
    """分支内节点要发进度事件并落记录，否则画布上一片灰。"""
    g = {
        "nodes": [
            {"id": "s", "type": "start", "data": {"inputs": [{"name": "q"}]}},
            {"id": "p", "type": "parallel", "data": {}},
            {"id": "a1", "type": "transform", "data": {"value": "A", "op": "to_text"}},
            {"id": "b1", "type": "transform", "data": {"value": "B", "op": "to_text"}},
            {"id": "j", "type": "join", "data": {}},
            {"id": "e", "type": "end", "data": {"output": "done"}},
        ],
        "edges": [
            {"source": "s", "target": "p"},
            {"source": "p", "target": "a1"}, {"source": "p", "target": "b1"},
            {"source": "a1", "target": "j"}, {"source": "b1", "target": "j"},
            {"source": "j", "target": "e"},
        ],
    }
    events: list[dict] = []

    async def emit(evt):
        events.append(evt)

    async def _t():
        ctx = NodeContext(db=None, ps=PrincipalSet(user_id=1, tenant_id=1),
                          run_id=1, tenant_id=1, emit=emit)
        _, records = await execute_graph(g, {"q": "x"}, ctx)
        return records

    records = _run(_t())
    started = [e for e in events if e["type"] == "node_started"]
    started_ids = {e["node_id"] for e in started}
    assert {"a1", "b1"} <= started_ids, "分支节点应发出 node_started"
    finished_ids = {e["node_id"] for e in events if e["type"] == "node_finished"}
    assert {"a1", "b1"} <= finished_ids
    rec_ids = {r["node_id"] for r in records}
    assert {"a1", "b1"} <= rec_ids, "分支节点应写入 records"


# ============================================================ 接口注册


def test_new_endpoints_registered():
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/api/v1/workflow-runs/{run_id}/cancel" in paths
    assert "/api/v1/agents/{agent_id}/workflow/versions" in paths
    assert "/api/v1/agents/{agent_id}/workflow/versions/{version_id}/rollback" in paths
    assert "/api/v1/agents/{agent_id}/workflow/export" in paths
    assert "/api/v1/workflow/import" in paths
