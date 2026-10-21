"""工作流测试用例：CRUD、断言语义、批量回归。"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.core.db import AsyncSessionLocal, init_models
from app.core.errors import NotFoundError, ValidationError
from app.core.security import hash_password
from app.models import Agent, Tenant, User, Workflow
from app.services import workflow_test_service as S


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


GRAPH = {
    "nodes": [
        {"id": "s", "type": "start", "data": {"inputs": [{"name": "query"}]}},
        {"id": "tt", "type": "text_template", "data": {"sections": [
            {"title": "回复", "content": "收到：{{s.query}}"},
        ]}},
        {"id": "e", "type": "end", "data": {"output": "{{tt.text}}"}},
    ],
    "edges": [{"source": "s", "target": "tt"}, {"source": "tt", "target": "e"}],
}


async def _setup(slug="wt"):
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
        wf = await db.execute(select(Workflow).where(Workflow.agent_id == a.id))
        if not wf.scalars().first():
            db.add(Workflow(tenant_id=t.id, agent_id=a.id, name="t", graph=GRAPH))
        await db.commit()
        return {"tenant_id": t.id, "user_id": u.id, "agent_id": a.id}


# ============================================================ CRUD


def test_create_and_list_case():
    async def _t():
        ctx = await _setup("wtcrud")
        async with AsyncSessionLocal() as db:
            r = await S.create_case(
                db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                name="基础问答", input_data={"query": "你好"}, expected="收到",
            )
            await db.commit()
            assert r["name"] == "基础问答"
            assert r["assert_contains"] is True
            rows = await S.list_cases(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"])
            assert any(x["id"] == r["id"] for x in rows)
    _run(_t())


def test_create_requires_name_and_input():
    async def _t():
        ctx = await _setup("wtvalid")
        async with AsyncSessionLocal() as db:
            with pytest.raises(ValidationError):
                await S.create_case(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                                    name="  ", input_data={"q": "x"})
            with pytest.raises(ValidationError):
                await S.create_case(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                                    name="x", input_data={})
    _run(_t())


def test_create_rejects_foreign_agent():
    async def _t():
        ctx = await _setup("wtforeign")
        async with AsyncSessionLocal() as db:
            with pytest.raises(NotFoundError):
                await S.create_case(db, tenant_id=ctx["tenant_id"], agent_id=999999,
                                    name="x", input_data={"q": "1"})
    _run(_t())


def test_update_and_delete_case():
    async def _t():
        ctx = await _setup("wtupd")
        async with AsyncSessionLocal() as db:
            r = await S.create_case(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                                    name="旧名", input_data={"query": "a"})
            await db.commit()
            u = await S.update_case(db, tenant_id=ctx["tenant_id"], case_id=r["id"],
                                    name="新名", expected="收到", assert_contains=False)
            await db.commit()
            assert u["name"] == "新名"
            assert u["expected"] == "收到"
            assert u["assert_contains"] is False
            await S.delete_case(db, tenant_id=ctx["tenant_id"], case_id=r["id"])
            await db.commit()
            rows = await S.list_cases(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"])
            assert all(x["id"] != r["id"] for x in rows)
    _run(_t())


def test_cross_tenant_update_denied():
    async def _t():
        a = await _setup("wtisolate")
        b = await _setup("wtisolate2")
        async with AsyncSessionLocal() as db:
            r = await S.create_case(db, tenant_id=a["tenant_id"], agent_id=a["agent_id"],
                                    name="x", input_data={"query": "1"})
            await db.commit()
            with pytest.raises(NotFoundError):
                await S.update_case(db, tenant_id=b["tenant_id"], case_id=r["id"], name="hack")
    _run(_t())


# ============================================================ 断言语义


def test_run_passes_when_expected_contained():
    async def _t():
        ctx = await _setup("wtrunok")
        async with AsyncSessionLocal() as db:
            await S.create_case(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                                name="命中", input_data={"query": "你好"}, expected="收到：你好")
            await db.commit()
            r = await S.run_cases(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"])
            await db.commit()
            assert r["total"] == 1
            assert r["passed"] == 1
            assert r["failed"] == 0
    _run(_t())


def test_run_fails_when_expected_missing():
    async def _t():
        ctx = await _setup("wtrunfail")
        async with AsyncSessionLocal() as db:
            await S.create_case(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                                name="不命中", input_data={"query": "你好"}, expected="完全不存在的字符串")
            await db.commit()
            r = await S.run_cases(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"])
            await db.commit()
            assert r["failed"] == 1
            assert "期望包含" in (r["results"][0]["error"] or "")
    _run(_t())


def test_run_smoke_without_expected():
    """没设期望值时只做冒烟：能跑完即通过。"""
    async def _t():
        ctx = await _setup("wtsmoke")
        async with AsyncSessionLocal() as db:
            await S.create_case(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                                name="冒烟", input_data={"query": "任意"})
            await db.commit()
            r = await S.run_cases(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"])
            await db.commit()
            assert r["passed"] == 1
    _run(_t())


def test_assert_contains_false_ignores_expected():
    async def _t():
        ctx = await _setup("wtoff")
        async with AsyncSessionLocal() as db:
            await S.create_case(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                                name="关闭断言", input_data={"query": "x"},
                                expected="不可能出现", assert_contains=False)
            await db.commit()
            r = await S.run_cases(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"])
            await db.commit()
            assert r["passed"] == 1, "关闭断言后不该因 expected 不匹配而失败"
    _run(_t())


def test_run_records_last_status():
    async def _t():
        ctx = await _setup("wtrecord")
        async with AsyncSessionLocal() as db:
            c = await S.create_case(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                                    name="记录", input_data={"query": "hi"}, expected="收到")
            await db.commit()
            await S.run_cases(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"])
            await db.commit()
            rows = await S.list_cases(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"])
            row = next(x for x in rows if x["id"] == c["id"])
            assert row["last_status"] == "passed"
            assert row["last_actual"]
            assert row["last_latency_ms"] is not None
            assert row["last_run_at"]
    _run(_t())


def test_run_selected_cases_only():
    async def _t():
        ctx = await _setup("wtsel")
        async with AsyncSessionLocal() as db:
            c1 = await S.create_case(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                                     name="A", input_data={"query": "a"})
            await S.create_case(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                                name="B", input_data={"query": "b"})
            await db.commit()
            r = await S.run_cases(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                                  case_ids=[c1["id"]])
            await db.commit()
            assert r["total"] == 1
            assert r["results"][0]["name"] == "A"
    _run(_t())


def test_run_without_cases_returns_empty():
    async def _t():
        ctx = await _setup("wtnone")
        async with AsyncSessionLocal() as db:
            r = await S.run_cases(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"])
            assert r["total"] == 0
            assert r["results"] == []
    _run(_t())


def test_run_node_error_reported():
    async def _t():
        from sqlalchemy import update as _upd

        ctx = await _setup("wterror")
        bad = {
            "nodes": [
                {"id": "s", "type": "start", "data": {"inputs": [{"name": "query"}]}},
                {"id": "tr", "type": "transform", "data": {"value": "x", "op": "extract", "pattern": "["}},
                {"id": "e", "type": "end", "data": {"output": "{{tr.output}}"}},
            ],
            "edges": [{"source": "s", "target": "tr"}, {"source": "tr", "target": "e"}],
        }
        async with AsyncSessionLocal() as db:
            await db.execute(_upd(Workflow).where(Workflow.agent_id == ctx["agent_id"]).values(graph=bad))
            await db.commit()
            await S.create_case(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                                name="出错", input_data={"query": "x"})
            await db.commit()
            r = await S.run_cases(db, tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"])
            await db.commit()
            assert r["failed"] == 1
            assert r["results"][0]["status"] == "error"
            assert r["results"][0]["error"]
    _run(_t())


def test_routes_registered():
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/api/v1/agents/{agent_id}/workflow/cases" in paths
    assert "/api/v1/agents/{agent_id}/workflow/cases/run" in paths
    assert "/api/v1/workflow-cases/{case_id}" in paths
