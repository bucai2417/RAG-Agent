"""工作流最后四项：多轮用例、群聊进度节流、Token 统计、回调重试。"""
from __future__ import annotations

import asyncio
import hashlib

import pytest
from sqlalchemy import select

from app.core.db import AsyncSessionLocal, init_models
from app.core.errors import ValidationError
from app.core.security import hash_password
from app.models import (
    Agent, Tenant, User, Workflow, WorkflowCallbackDelivery, WorkflowRun, WorkflowTest,
)
from app.services import workflow_callback_service as CB
from app.services import workflow_test_service as TS


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


GRAPH = {
    "nodes": [
        {"id": "s", "type": "start", "data": {"inputs": [{"name": "query"}, {"name": "history"}]}},
        {"id": "tt", "type": "text_template", "data": {"sections": [
            {"title": "回复", "content": "Q={{s.query}}\nH={{s.history}}"},
        ]}},
        {"id": "e", "type": "end", "data": {"output": "{{tt.text}}"}},
    ],
    "edges": [{"source": "s", "target": "tt"}, {"source": "tt", "target": "e"}],
}


async def _setup(slug="last"):
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
        if not (await db.execute(select(Workflow).where(Workflow.agent_id == a.id))).scalars().first():
            db.add(Workflow(tenant_id=t.id, agent_id=a.id, name="t", graph=GRAPH))
        await db.commit()
        return {"tenant_id": t.id, "user_id": u.id, "agent_id": a.id}


# ============================================================ 多轮测试用例


def test_history_normalized():
    assert TS._normalize_history(None) is None
    assert TS._normalize_history([]) is None
    out = TS._normalize_history([
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好，请问需要什么"},
        {"role": "system", "content": "应被过滤"},
        {"role": "user", "content": "   "},
        {"bad": 1},
    ])
    assert len(out) == 2
    assert out[0]["role"] == "user"


def test_create_case_with_history():
    async def _t():
        ctx = await _setup("hist")
        async with AsyncSessionLocal() as db:
            r = await TS.create_case(
                db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                name="多轮", input_data={"query": "刚才那个订单"},
                history=[{"role": "user", "content": "我要查订单 A123"}],
                expected="A123", assert_contains=True,
            )
            await db.commit()
            assert r["history"]
            assert r["history"][0]["content"] == "我要查订单 A123"
    _run(_t())


def test_multi_turn_history_reaches_workflow():
    """历史要作为 history 变量进入工作流，才能支持「刚才那个」这类指代。"""
    async def _t():
        ctx = await _setup("histrun")
        async with AsyncSessionLocal() as db:
            await TS.create_case(
                db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                name="指代", input_data={"query": "那个到哪了"},
                history=[{"role": "user", "content": "订单 A123"},
                         {"role": "assistant", "content": "已发货"}],
                expected="订单 A123", assert_contains=True,
            )
            await db.commit()
            r = await TS.run_cases(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"])
            await db.commit()
            assert r["passed"] == 1, r["results"]
            actual = r["results"][0]["actual"]
            assert "客户：订单 A123" in actual
            assert "客服：已发货" in actual
    _run(_t())


def test_single_turn_case_unchanged():
    """没有 history 的用例行为不变（history 变量为空）。"""
    async def _t():
        ctx = await _setup("single")
        async with AsyncSessionLocal() as db:
            await TS.create_case(
                db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                name="单轮", input_data={"query": "你好"},
            )
            await db.commit()
            r = await TS.run_cases(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"])
            await db.commit()
            assert r["passed"] == 1
            assert "H=" in r["results"][0]["actual"]
    _run(_t())


def test_update_case_history():
    async def _t():
        ctx = await _setup("histupd")
        async with AsyncSessionLocal() as db:
            r = await TS.create_case(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                                     name="x", input_data={"query": "a"})
            await db.commit()
            u = await TS.update_case(
                db, tenant_id=ctx["tenant_id"], case_id=r["id"],
                history=[{"role": "user", "content": "上一轮"}],
            )
            await db.commit()
            assert u["history"][0]["content"] == "上一轮"
    _run(_t())


# ============================================================ 群聊进度节流


def test_group_progress_off_by_default():
    from app.channels.dispatcher import _GroupProgressGate

    g = _GroupProgressGate("off", 15)
    assert all(not g.should_send() for _ in range(5))


def test_group_progress_first_only():
    from app.channels.dispatcher import _GroupProgressGate

    g = _GroupProgressGate("first", 15)
    assert g.should_send() is True
    assert g.should_send() is False
    assert g.should_send() is False


def test_group_progress_throttle(monkeypatch):
    import app.channels.dispatcher as D

    clock = {"t": 1000.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])

    g = D._GroupProgressGate("throttle", 10)
    assert g.should_send() is True
    clock["t"] += 3
    assert g.should_send() is False, "未到间隔不应发"
    clock["t"] += 8  # 累计 11 秒
    assert g.should_send() is True


def test_group_progress_unknown_mode_is_silent():
    from app.channels.dispatcher import _GroupProgressGate

    g = _GroupProgressGate("bogus", 5)
    assert g.should_send() is False


# ============================================================ Token 统计


def test_node_context_accumulates_usage():
    from app.agents.workflow.engine import NodeContext
    from app.services.permission import PrincipalSet

    ctx = NodeContext(db=None, ps=PrincipalSet(user_id=1, tenant_id=1), run_id=1, tenant_id=1)
    ctx.add_usage({"prompt_tokens": 100, "completion_tokens": 50})
    ctx.add_usage({"input_tokens": 20, "output_tokens": 10})  # 兼容 anthropic 字段名
    ctx.add_usage(None)  # 容错
    ctx.add_usage({"prompt_tokens": 0})
    assert ctx.usage["prompt_tokens"] == 120
    assert ctx.usage["completion_tokens"] == 60
    assert ctx.usage["calls"] == 3


def test_usage_persisted_on_run():
    async def _t():
        from app.agents.workflow.engine import NodeContext
        from app.services.permission import PrincipalSet

        ctx = await _setup("usage")
        async with AsyncSessionLocal() as db:
            run = WorkflowRun(
                tenant_id=ctx["tenant_id"], workflow_id=1, agent_id=ctx["agent_id"],
                status="success", input={}, created_at=0,
                usage={"prompt_tokens": 10, "completion_tokens": 5, "calls": 1},
            )
            db.add(run)
            await db.commit()
            assert run.usage["prompt_tokens"] == 10
    _run(_t())


def test_usage_endpoint_shape():
    from app.main import app

    assert "/api/v1/agents/{agent_id}/workflow/usage" in app.openapi()["paths"]


# ============================================================ 回调重试


def test_backoff_schedule():
    assert CB.MAX_ATTEMPTS == 5
    assert CB.BACKOFF_SECONDS == (5, 15, 60, 300)


def test_should_retry_rules():
    assert CB._should_retry(None) is True           # 网络错误
    assert CB._should_retry(500) is True            # 服务端错误
    assert CB._should_retry(429) is True            # 限流
    assert CB._should_retry(408) is True            # 超时
    assert CB._should_retry(400) is False           # 参数错，重试无意义
    assert CB._should_retry(404) is False


def test_delivery_retry_then_dead(monkeypatch):
    """失败按退避重试，超过次数标记 dead。"""
    async def _t():
        ctx = await _setup("cbr")
        attempts = {"n": 0}

        async def _fail(d):
            attempts["n"] += 1
            return False, 500, "HTTP 500: boom"

        monkeypatch.setattr(CB, "deliver_once", _fail)

        async with AsyncSessionLocal() as db:
            d = await CB.enqueue(
                db, run_id=1, tenant_id=ctx["tenant_id"],
                url="https://example.com/cb", secret_enc=None, payload={"x": 1},
            )
            await db.commit()
            did = d.id

            # 连续 flush，直到 dead
            for _ in range(10):
                async with AsyncSessionLocal() as db2:
                    row = await db2.get(WorkflowCallbackDelivery, did)
                    row.next_attempt_at = 0  # 强制到期
                    await db2.commit()
                await CB.flush_ready(limit=1)

            async with AsyncSessionLocal() as db3:
                row = await db3.get(WorkflowCallbackDelivery, did)
                assert row.status == "dead"
                assert row.attempts == CB.MAX_ATTEMPTS

    _run(_t())


def test_delivery_success_marks_sent(monkeypatch):
    async def _t():
        ctx = await _setup("cbs")

        async def _ok(d):
            return True, 200, None

        monkeypatch.setattr(CB, "deliver_once", _ok)

        async with AsyncSessionLocal() as db:
            d = await CB.enqueue(
                db, run_id=1, tenant_id=ctx["tenant_id"],
                url="https://example.com/cb", secret_enc=None, payload={"x": 1},
            )
            await db.commit()
            did = d.id

        stats = await CB.flush_ready(limit=1)
        assert stats["sent"] == 1

        async with AsyncSessionLocal() as db2:
            row = await db2.get(WorkflowCallbackDelivery, did)
            assert row.status == "sent"
            assert row.attempts == 1
    _run(_t())


def test_client_error_goes_dead_immediately(monkeypatch):
    """4xx 不该重试（对方明确拒绝）。"""
    async def _t():
        ctx = await _setup("cb4")
        attempts = {"n": 0}

        async def _fail(d):
            attempts["n"] += 1
            return False, 400, "HTTP 400: bad request"

        monkeypatch.setattr(CB, "deliver_once", _fail)

        async with AsyncSessionLocal() as db:
            d = await CB.enqueue(
                db, run_id=1, tenant_id=ctx["tenant_id"],
                url="https://example.com/cb", secret_enc=None, payload={},
            )
            await db.commit()
            did = d.id

        await CB.flush_ready(limit=1)

        async with AsyncSessionLocal() as db2:
            row = await db2.get(WorkflowCallbackDelivery, did)
            assert row.status == "dead"
            assert row.attempts == 1, "4xx 应只尝试一次"
    _run(_t())


def test_failed_delivery_exposes_diagnostics():
    async def _t():
        ctx = await _setup("cbd")
        async with AsyncSessionLocal() as db:
            await CB.enqueue(
                db, run_id=1, tenant_id=ctx["tenant_id"],
                url="https://example.com/cb", secret_enc=None, payload={},
            )
            await db.commit()
            rows = await CB.list_deliveries(db, tenant_id=ctx["tenant_id"])
            assert rows
            assert "attempts" in rows[0]
            assert "last_error" in rows[0]
    _run(_t())


def test_callback_routes_registered():
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/api/v1/workflow-callback-deliveries" in paths
    assert "/api/v1/workflow-callback-deliveries/{delivery_id}/retry" in paths
