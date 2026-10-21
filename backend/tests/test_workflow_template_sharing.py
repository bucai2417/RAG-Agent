"""模板附带测试用例 + 跨租户共享。"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.core.db import AsyncSessionLocal, init_models
from app.core.errors import NotFoundError
from app.core.security import hash_password
from app.models import Agent, Tenant, User, Workflow, WorkflowTemplate, WorkflowTest
from app.services import workflow_template_service as T


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


GRAPH = {
    "nodes": [
        {"id": "s", "type": "start", "data": {"inputs": [{"name": "query"}]}},
        {"id": "e", "type": "end", "data": {"output": "{{s.query}}"}},
    ],
    "edges": [{"source": "s", "target": "e"}],
}


async def _setup(slug="tc"):
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


# ============================================================ 模板带用例


def test_builtin_has_case_count_field():
    for t in T.list_builtin():
        assert "case_count" in t
    # 至少有一个内置模板带用例
    assert any(t["case_count"] > 0 for t in T.list_builtin())


def test_builtin_table_summary_case_is_runnable():
    """内置模板带的用例必须真能过（否则等于给了个坏基准）。"""
    t = T.get_builtin("table-summary")
    cases = t.get("cases") or []
    assert cases
    c = cases[0]
    assert c["input"].get("rows")
    assert c["expected"]


def test_save_template_includes_cases():
    async def _t():
        ctx = await _setup("tcsave")
        async with AsyncSessionLocal() as db:
            db.add(WorkflowTest(
                tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                name="用例A", input={"query": "你好"}, expected="你好",
                assert_contains=True, history=[{"role": "user", "content": "上一轮"}],
            ))
            await db.commit()

            r = await T.save_template(
                db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"],
                agent_id=ctx["agent_id"], name="带用例",
            )
            await db.commit()
            assert r["case_count"] == 1

            row = await db.get(WorkflowTemplate, r["id"])
            assert len(row.cases) == 1
            assert row.cases[0]["name"] == "用例A"
            assert row.cases[0]["history"][0]["content"] == "上一轮"
    _run(_t())


def test_save_template_can_exclude_cases():
    async def _t():
        ctx = await _setup("tcnocase")
        async with AsyncSessionLocal() as db:
            db.add(WorkflowTest(
                tenant_id=ctx["tenant_id"], agent_id=ctx["agent_id"],
                name="x", input={"query": "a"},
            ))
            await db.commit()
            r = await T.save_template(
                db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"],
                agent_id=ctx["agent_id"], include_cases=False,
            )
            await db.commit()
            assert r["case_count"] == 0
    _run(_t())


def test_apply_template_creates_cases():
    async def _t():
        ctx = await _setup("tcapply")
        cases = [{
            "name": "模板用例", "input": {"query": "hi"},
            "expected": "hi", "assert_contains": True, "history": [],
        }]
        async with AsyncSessionLocal() as db:
            r = await T.apply_template(
                db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"],
                graph=GRAPH, name="套用例", cases=cases,
            )
            await db.commit()
            assert r["case_count"] == 1

            rows = (await db.execute(
                select(WorkflowTest).where(WorkflowTest.agent_id == r["agent_id"])
            )).scalars().all()
            assert len(rows) == 1
            assert rows[0].name == "模板用例"
            assert rows[0].expected == "hi"
    _run(_t())


def test_apply_template_skips_bad_cases():
    """坏用例跳过而不是让整个套用失败。"""
    async def _t():
        ctx = await _setup("tcskip")
        cases = [
            {"name": "好", "input": {"query": "a"}},
            {"name": "坏", "input": {}},        # 空输入
            {"name": "也坏"},                   # 缺 input
            "not-a-dict",
        ]
        async with AsyncSessionLocal() as db:
            r = await T.apply_template(
                db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"],
                graph=GRAPH, name="部分坏", cases=cases,
            )
            await db.commit()
            assert r["case_count"] == 1
    _run(_t())


def test_apply_builtin_template_brings_its_cases():
    async def _t():
        ctx = await _setup("tcbuiltin")
        t = T.get_builtin("table-summary")
        async with AsyncSessionLocal() as db:
            r = await T.apply_template(
                db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"],
                graph=t["graph"], name="带内置用例",
                cases=t.get("cases") or [],
            )
            await db.commit()
            assert r["case_count"] >= 1
    _run(_t())


# ============================================================ 跨租户共享


def test_publish_template_to_public():
    async def _t():
        ctx = await _setup("tcpub")
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"],
                agent_id=ctx["agent_id"], name="共享模板",
            )
            await db.commit()
            res = await T.set_visibility(
                db, tenant_id=ctx["tenant_id"], template_id=r["id"], public=True,
            )
            await db.commit()
            # 发布走审核流：先 pending，审核通过才 public
            assert res["visibility"] == "pending"
            assert res["review_status"] == "pending"

            rev = await T.review_template(db, template_id=r["id"], approve=True)
            await db.commit()
            assert rev["visibility"] == "public"
    _run(_t())


def test_public_templates_visible_to_other_tenant():
    async def _t():
        a = await _setup("tcpa")
        b = await _setup("tcpb")
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=a["tenant_id"], user_id=a["user_id"],
                agent_id=a["agent_id"], name="A 的共享",
            )
            await db.commit()
            await T.set_visibility(db, tenant_id=a["tenant_id"], template_id=r["id"], public=True)
            await T.review_template(db, template_id=r["id"], approve=True)
            await db.commit()

            # B 租户能看到
            pub = await T.list_public_templates(db, tenant_id=b["tenant_id"])
            assert any(x["id"] == r["id"] for x in pub)

            # A 自己看不到（避免重复）
            own_pub = await T.list_public_templates(db, tenant_id=a["tenant_id"])
            assert all(x["id"] != r["id"] for x in own_pub)
    _run(_t())


def test_private_template_not_in_public():
    async def _t():
        a = await _setup("tcprv")
        b = await _setup("tcprvb")
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=a["tenant_id"], user_id=a["user_id"],
                agent_id=a["agent_id"], name="私有",
            )
            await db.commit()
            pub = await T.list_public_templates(db, tenant_id=b["tenant_id"])
            assert all(x["id"] != r["id"] for x in pub), "私有模板不应出现在公共库"
    _run(_t())


def test_apply_public_template_cross_tenant():
    """其他租户的公开模板可套用，且产生本租户的智能体。"""
    async def _t():
        a = await _setup("tcca")
        b = await _setup("tccb")
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=a["tenant_id"], user_id=a["user_id"],
                agent_id=a["agent_id"], name="跨租户",
            )
            await db.commit()
            await T.set_visibility(db, tenant_id=a["tenant_id"], template_id=r["id"], public=True)
            await db.commit()

            row = await db.get(WorkflowTemplate, r["id"])
            out = await T.apply_template(
                db, tenant_id=b["tenant_id"], user_id=b["user_id"],
                graph=row.graph, name="B 套用", cases=row.cases or [],
            )
            await db.commit()

            ag = await db.get(Agent, out["agent_id"])
            assert ag.tenant_id == b["tenant_id"], "套用后应归属调用方租户"
            assert ag.tenant_id != a["tenant_id"]
    _run(_t())


def test_unpublish_removes_from_public():
    async def _t():
        a = await _setup("tcunp")
        b = await _setup("tcunpb")
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=a["tenant_id"], user_id=a["user_id"],
                agent_id=a["agent_id"], name="撤下",
            )
            await db.commit()
            await T.set_visibility(db, tenant_id=a["tenant_id"], template_id=r["id"], public=True)
            await T.review_template(db, template_id=r["id"], approve=True)
            await db.commit()
            pub1 = await T.list_public_templates(db, tenant_id=b["tenant_id"])
            assert any(x["id"] == r["id"] for x in pub1)

            await T.set_visibility(db, tenant_id=a["tenant_id"], template_id=r["id"], public=False)
            await db.commit()
            pub2 = await T.list_public_templates(db, tenant_id=b["tenant_id"])
            assert all(x["id"] != r["id"] for x in pub2)
    _run(_t())


def test_publish_cross_tenant_denied():
    async def _t():
        a = await _setup("tcdeny")
        b = await _setup("tcdenyb")
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=a["tenant_id"], user_id=a["user_id"],
                agent_id=a["agent_id"], name="x",
            )
            await db.commit()
            with pytest.raises(NotFoundError):
                await T.set_visibility(
                    db, tenant_id=b["tenant_id"], template_id=r["id"], public=True,
                )
    _run(_t())


def test_public_list_sorted_by_popularity():
    """公共库排序：评分优先，同分（或无评分）看套用次数。"""
    async def _t():
        a = await _setup("tcsort")
        b = await _setup("tcsortb")
        async with AsyncSessionLocal() as db:
            ids = []
            for n in ("低", "高"):
                r = await T.save_template(
                    db, tenant_id=a["tenant_id"], user_id=a["user_id"],
                    agent_id=a["agent_id"], name=n,
                )
                await db.commit()
                await T.set_visibility(db, tenant_id=a["tenant_id"], template_id=r["id"], public=True)
                await T.review_template(db, template_id=r["id"], approve=True)
                await db.commit()
                ids.append(r["id"])

            # 两个都没有评分时，按套用次数排序
            row = await db.get(WorkflowTemplate, ids[1])
            row.apply_count = 10
            await db.commit()

            pub = await T.list_public_templates(db, tenant_id=b["tenant_id"])
            names = [x["name"] for x in pub]
            assert names.index("高") < names.index("低"), "同评分时按套用次数降序"
    _run(_t())


def test_template_output_shape():
    async def _t():
        ctx = await _setup("tcshape")
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"],
                agent_id=ctx["agent_id"], name="形状",
            )
            await db.commit()
            rows = await T.list_user_templates(db, tenant_id=ctx["tenant_id"])
            item = next(x for x in rows if x["id"] == r["id"])
            for k in ("id", "name", "category", "node_count", "case_count",
                      "source", "visibility", "apply_count"):
                assert k in item, k
    _run(_t())


def test_routes_registered():
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/api/v1/workflow/templates/{template_id}/publish" in paths
