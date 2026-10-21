"""工作流模板：内置模板合法性、用户模板 CRUD、套用语义。"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.agents.workflow.engine import NODE_EXECUTORS, NodeContext, execute_graph, validate_graph
from app.core.db import AsyncSessionLocal, init_models
from app.core.errors import NotFoundError, ValidationError
from app.core.security import hash_password
from app.models import Agent, Tenant, User, Workflow, WorkflowTemplate
from app.services import workflow_template_service as T
from app.services.permission import PrincipalSet


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


GRAPH = {
    "nodes": [
        {"id": "s", "type": "start", "data": {"inputs": [{"name": "query"}]}},
        {"id": "e", "type": "end", "data": {"output": "{{s.query}}"}},
    ],
    "edges": [{"source": "s", "target": "e"}],
}


async def _setup(slug="tpl"):
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


# ============================================================ 内置模板


def test_all_builtin_templates_are_valid():
    """内置模板必须能通过引擎校验，且只用已注册的节点类型。"""
    assert len(T.BUILTIN_TEMPLATES) >= 5
    for t in T.BUILTIN_TEMPLATES:
        validate_graph(t["graph"])
        types = {n["type"] for n in t["graph"]["nodes"]}
        unknown = types - set(NODE_EXECUTORS)
        assert not unknown, f"模板 {t['key']} 使用了未注册节点: {unknown}"


def test_builtin_templates_have_metadata():
    for t in T.BUILTIN_TEMPLATES:
        assert t["key"] and t["name"] and t["description"]
        assert t["category"]
        assert t["tags"]


def test_builtin_keys_unique():
    keys = [t["key"] for t in T.BUILTIN_TEMPLATES]
    assert len(keys) == len(set(keys))


def test_builtin_has_positions():
    """模板要带坐标，否则套用后画布上全叠在一起。"""
    for t in T.BUILTIN_TEMPLATES:
        for n in t["graph"]["nodes"]:
            assert isinstance(n.get("position"), dict)
            assert isinstance(n["position"].get("x"), int)
            assert isinstance(n["position"].get("y"), int)


def test_table_summary_template_runs():
    """纯数据变换模板要能直接跑通（不依赖任何外部服务）。"""
    t = T.get_builtin("table-summary")
    assert t is not None

    async def _t():
        ctx = NodeContext(db=None, ps=PrincipalSet(user_id=1, tenant_id=1), run_id=1, tenant_id=1)
        rows = '[{"name":"A","amount":100},{"name":"B","amount":200}]'
        final, records = await execute_graph(t["graph"], {"rows": rows}, ctx)
        assert all(r["status"] == "success" for r in records)
        assert "A,100" in final["output"]

    _run(_t())


def test_list_builtin_shape():
    items = T.list_builtin()
    assert items
    assert {"key", "name", "category", "description", "tags", "node_count", "source"} <= set(items[0])
    assert items[0]["source"] == "builtin"


def test_get_builtin_unknown():
    assert T.get_builtin("no-such-key") is None


# ============================================================ 用户模板


def test_save_template_from_workflow():
    async def _t():
        ctx = await _setup("tplsave")
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"],
                agent_id=ctx["agent_id"], name="我的流程", category="客服",
            )
            await db.commit()
            assert r["node_count"] == 2
            rows = await T.list_user_templates(db, tenant_id=ctx["tenant_id"])
            assert any(x["id"] == r["id"] for x in rows)
            assert rows[0]["source"] == "user"
    _run(_t())


def test_save_template_rejects_empty_workflow():
    async def _t():
        from sqlalchemy import update as _upd

        ctx = await _setup("tplempty")
        async with AsyncSessionLocal() as db:
            await db.execute(_upd(Workflow).where(Workflow.agent_id == ctx["agent_id"]).values(graph={"nodes": []}))
            await db.commit()
            with pytest.raises(ValidationError):
                await T.save_template(
                    db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"], agent_id=ctx["agent_id"],
                )
    _run(_t())


def test_save_template_foreign_agent():
    async def _t():
        ctx = await _setup("tplforeign")
        async with AsyncSessionLocal() as db:
            with pytest.raises(NotFoundError):
                await T.save_template(
                    db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"], agent_id=999999,
                )
    _run(_t())


def test_delete_template():
    async def _t():
        ctx = await _setup("tpldel")
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"], agent_id=ctx["agent_id"],
            )
            await db.commit()
            await T.delete_template(db, tenant_id=ctx["tenant_id"], template_id=r["id"])
            await db.commit()
            rows = await T.list_user_templates(db, tenant_id=ctx["tenant_id"])
            assert all(x["id"] != r["id"] for x in rows)
    _run(_t())


def test_delete_template_cross_tenant_denied():
    async def _t():
        a = await _setup("tpdx1")
        b = await _setup("tpdx2")
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=a["tenant_id"], user_id=a["user_id"], agent_id=a["agent_id"],
            )
            await db.commit()
            with pytest.raises(NotFoundError):
                await T.delete_template(db, tenant_id=b["tenant_id"], template_id=r["id"])
    _run(_t())


# ============================================================ 套用


def test_apply_builtin_creates_new_agent():
    async def _t():
        ctx = await _setup("tplapply")
        t = T.get_builtin("faq-bot")
        async with AsyncSessionLocal() as db:
            r = await T.apply_template(
                db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"],
                graph=t["graph"], name="套用的客服", description=t["description"],
            )
            await db.commit()
            assert r["agent_id"] != ctx["agent_id"], "套用应创建新智能体"
            ag = await db.get(Agent, r["agent_id"])
            assert ag.type == "workflow"
            assert ag.name == "套用的客服"
            wf = await db.get(Workflow, r["workflow_id"])
            assert len(wf.graph["nodes"]) == len(t["graph"]["nodes"])
    _run(_t())


def test_apply_dedupes_name():
    async def _t():
        ctx = await _setup("tpldup")
        t = T.get_builtin("table-summary")
        async with AsyncSessionLocal() as db:
            r1 = await T.apply_template(
                db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"],
                graph=t["graph"], name="同名",
            )
            r2 = await T.apply_template(
                db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"],
                graph=t["graph"], name="同名",
            )
            await db.commit()
            a1 = await db.get(Agent, r1["agent_id"])
            a2 = await db.get(Agent, r2["agent_id"])
            assert a1.slug != a2.slug
    _run(_t())


def test_apply_rejects_invalid_graph():
    async def _t():
        ctx = await _setup("tplbad")
        async with AsyncSessionLocal() as db:
            with pytest.raises(ValidationError):
                await T.apply_template(
                    db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"],
                    graph={"nodes": []}, name="空",
                )
            # 缺 end 节点也应被拒
            with pytest.raises(ValidationError):
                await T.apply_template(
                    db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"],
                    graph={"nodes": [{"id": "s", "type": "start", "data": {}}], "edges": []},
                    name="缺 end",
                )
    _run(_t())


def test_apply_user_template_roundtrip():
    """保存 → 套用，图内容应一致。"""
    async def _t():
        ctx = await _setup("tplrt")
        async with AsyncSessionLocal() as db:
            saved = await T.save_template(
                db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"],
                agent_id=ctx["agent_id"], name="往返",
            )
            await db.commit()
            row = await db.get(WorkflowTemplate, saved["id"])
            r = await T.apply_template(
                db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"],
                graph=row.graph, name="套用往返",
            )
            await db.commit()
            wf = await db.get(Workflow, r["workflow_id"])
            ids = [n["id"] for n in wf.graph["nodes"]]
            assert ids == ["s", "e"]
    _run(_t())


# ============================================================ 接口注册


def test_template_routes_registered():
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/api/v1/workflow/templates" in paths
    assert "/api/v1/workflow/templates/apply" in paths
    assert "/api/v1/workflow/templates/{template_id}" in paths
    assert "/api/v1/agents/{agent_id}/workflow/save-as-template" in paths
    assert "/api/v1/agents/{agent_id}/workflow/usage" in paths
