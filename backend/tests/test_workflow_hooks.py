"""工作流外部接入：令牌创建、路径解析、同步执行、载荷映射、权限。"""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from app.api.v1 import webhooks as W
from app.core.db import AsyncSessionLocal, init_models
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.security import hash_password
from app.models import Agent, Tenant, User, WebhookToken, Workflow


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


GRAPH = {
    "nodes": [
        {"id": "s", "type": "start", "data": {"inputs": [{"name": "message"}, {"name": "order_id"}]}},
        {"id": "tt", "type": "text_template", "data": {"sections": [
            {"title": "结果", "content": "订单 {{s.order_id}} 已发货"},
        ]}},
        {"id": "e", "type": "end", "data": {"output": "{{tt.text}}"}},
    ],
    "edges": [{"source": "s", "target": "tt"}, {"source": "tt", "target": "e"}],
}


async def _setup(slug="hook"):
    await init_models()
    async with AsyncSessionLocal() as db:
        t = (await db.execute(__import__("sqlalchemy").select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
        if not t:
            t = Tenant(name=slug, slug=slug)
            db.add(t)
            await db.flush()
        u = (await db.execute(
            __import__("sqlalchemy").select(User).where(User.username == f"{slug}_u")
        )).scalar_one_or_none()
        if not u:
            u = User(tenant_id=t.id, username=f"{slug}_u", password_hash=hash_password("x"), is_admin=True)
            db.add(u)
            await db.flush()
        a = Agent(tenant_id=t.id, name=f"{slug}助手", slug=f"{slug}-bot",
                  type="workflow", owner_id=u.id, status="published")
        db.add(a)
        await db.flush()
        wf = Workflow(tenant_id=t.id, agent_id=a.id, name="t", graph=GRAPH)
        db.add(wf)
        await db.commit()
        return {"tenant_id": t.id, "user_id": u.id, "agent_id": a.id}


# ============================================================ 令牌解析


def test_resolve_by_custom_path():
    async def _t():
        ctx = await _setup("hookpath")
        async with AsyncSessionLocal() as db:
            tok = WebhookToken(tenant_id=ctx["tenant_id"], kind="workflow", agent_id=ctx["agent_id"],
                               token_hash=hashlib.sha256(b"plain-1").hexdigest(),
                               path="order-sync", created_by=ctx["user_id"])
            db.add(tok)
            await db.commit()
            r = await W._resolve_token(db, "order-sync")
            assert r.path == "order-sync"
            # 大小写不敏感
            r2 = await W._resolve_token(db, "ORDER-SYNC")
            assert r2.id == r.id
    _run(_t())


def test_resolve_by_plain_token():
    async def _t():
        ctx = await _setup("hookplain")
        plain = "plain-token-xyz"
        async with AsyncSessionLocal() as db:
            db.add(WebhookToken(tenant_id=ctx["tenant_id"], kind="workflow", agent_id=ctx["agent_id"],
                                token_hash=hashlib.sha256(plain.encode()).hexdigest(),
                                created_by=ctx["user_id"]))
            await db.commit()
            r = await W._resolve_token(db, plain)
            assert r.kind == "workflow"
    _run(_t())


def test_resolve_rejects_disabled():
    async def _t():
        ctx = await _setup("hookoff")
        async with AsyncSessionLocal() as db:
            db.add(WebhookToken(tenant_id=ctx["tenant_id"], kind="workflow", agent_id=ctx["agent_id"],
                                token_hash=hashlib.sha256(b"off").hexdigest(),
                                path="off-path", enabled=False, created_by=ctx["user_id"]))
            await db.commit()
            with pytest.raises(PermissionDeniedError):
                await W._resolve_token(db, "off-path")
    _run(_t())


def test_resolve_rejects_unknown():
    async def _t():
        await _setup("hookunknown")
        async with AsyncSessionLocal() as db:
            with pytest.raises(PermissionDeniedError):
                await W._resolve_token(db, "no-such-handle")
    _run(_t())


def test_resolve_rejects_empty():
    async def _t():
        await _setup("hookempty")
        async with AsyncSessionLocal() as db:
            with pytest.raises(PermissionDeniedError):
                await W._resolve_token(db, "   ")
    _run(_t())


# ============================================================ 同步执行


def test_invoke_workflow_returns_result():
    """核心能力：外部 POST 直接拿到工作流输出，而不是"已触发"。"""
    async def _t():
        ctx = await _setup("hookrun")
        async with AsyncSessionLocal() as db:
            tok = WebhookToken(tenant_id=ctx["tenant_id"], kind="workflow", agent_id=ctx["agent_id"],
                               token_hash=hashlib.sha256(b"run").hexdigest(),
                               created_by=ctx["user_id"])
            db.add(tok)
            await db.commit()
            r = await W._invoke_workflow(db, tok, {"message": "查订单", "order_id": "A123"})
            assert r["ok"] is True
            assert r["status"] == "success"
            assert "A123" in r["text"]
            assert r["run_id"]
    _run(_t())


def test_invoke_maps_message_to_first_start_input():
    """载荷里的 message 要落到 start 声明的第一个输入，而不是硬编码 query。"""
    async def _t():
        ctx = await _setup("hookmap")
        async with AsyncSessionLocal() as db:
            tok = WebhookToken(tenant_id=ctx["tenant_id"], kind="workflow", agent_id=ctx["agent_id"],
                               token_hash=hashlib.sha256(b"map").hexdigest(),
                               created_by=ctx["user_id"])
            db.add(tok)
            await db.commit()
            r = await W._invoke_workflow(db, tok, {"message": "你好"})
            assert r["ok"] is True
            # 未提供 order_id → 空串，模板仍应产出文本
            assert "已发货" in r["text"]
    _run(_t())


def test_invoke_workflow_records_run():
    """外部调用也要留下运行记录，便于后台排查。"""
    async def _t():
        from sqlalchemy import select

        from app.models import WorkflowRun

        ctx = await _setup("hookrecord")
        async with AsyncSessionLocal() as db:
            tok = WebhookToken(tenant_id=ctx["tenant_id"], kind="workflow", agent_id=ctx["agent_id"],
                               token_hash=hashlib.sha256(b"rec").hexdigest(),
                               created_by=ctx["user_id"])
            db.add(tok)
            await db.commit()
            r = await W._invoke_workflow(db, tok, {"message": "x", "order_id": "B1"})
            run = (await db.execute(
                select(WorkflowRun).where(WorkflowRun.id == r["run_id"])
            )).scalar_one()
            assert run.status == "success"
            assert run.output
    _run(_t())


def test_invoke_failure_returns_error():
    """节点报错时返回结构化失败，而不是 500。"""
    async def _t():
        from sqlalchemy import select
        from sqlalchemy import update as _upd

        ctx = await _setup("hookfail")
        bad = {
            "nodes": [
                {"id": "s", "type": "start", "data": {"inputs": [{"name": "message"}]}},
                {"id": "tr", "type": "transform", "data": {"value": "x", "op": "extract", "pattern": "["}},
                {"id": "e", "type": "end", "data": {"output": "{{tr.output}}"}},
            ],
            "edges": [{"source": "s", "target": "tr"}, {"source": "tr", "target": "e"}],
        }
        async with AsyncSessionLocal() as db:
            await db.execute(_upd(Workflow).where(Workflow.agent_id == ctx["agent_id"]).values(graph=bad))
            await db.commit()
            tok = WebhookToken(tenant_id=ctx["tenant_id"], kind="workflow", agent_id=ctx["agent_id"],
                               token_hash=hashlib.sha256(b"fail").hexdigest(),
                               created_by=ctx["user_id"])
            db.add(tok)
            await db.commit()
            r = await W._invoke_workflow(db, tok, {"message": "x"})
            assert r["ok"] is False
            assert r["status"] == "failed"
            assert r["error"]
    _run(_t())


def test_invoke_without_agent():
    async def _t():
        ctx = await _setup("hooknoagent")
        async with AsyncSessionLocal() as db:
            tok = WebhookToken(tenant_id=ctx["tenant_id"], kind="workflow", agent_id=None,
                               token_hash=hashlib.sha256(b"na").hexdigest())
            db.add(tok)
            await db.commit()
            with pytest.raises(ValidationError):
                await W._invoke_workflow(db, tok, {"message": "x"})
    _run(_t())


def test_invoke_without_workflow_graph():
    async def _t():
        from sqlalchemy import update as _upd

        ctx = await _setup("hooknog")
        async with AsyncSessionLocal() as db:
            await db.execute(_upd(Workflow).where(Workflow.agent_id == ctx["agent_id"]).values(graph={"nodes": []}))
            await db.commit()
            tok = WebhookToken(tenant_id=ctx["tenant_id"], kind="workflow", agent_id=ctx["agent_id"],
                               token_hash=hashlib.sha256(b"ng").hexdigest())
            db.add(tok)
            await db.commit()
            with pytest.raises(ValidationError):
                await W._invoke_workflow(db, tok, {"message": "x"})
    _run(_t())


# ============================================================ 路径校验


def test_path_validation_rules():
    assert W._PATH_RE.match("order-sync")
    assert W._PATH_RE.match("a1")
    assert not W._PATH_RE.match("Order-Sync")   # 大写不允许（创建时统一转小写）
    assert not W._PATH_RE.match("with space")
    assert not W._PATH_RE.match("a/b")
    assert not W._PATH_RE.match("-leading")


def test_routes_registered():
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/api/v1/workflow-hooks" in paths
    assert "/api/v1/hooks/{handle}" in paths
    assert "/api/v1/workflow-hooks/{hook_id}/toggle" in paths
