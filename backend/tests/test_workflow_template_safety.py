"""模板安全：风险扫描、发布审核、评分。"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.core.db import AsyncSessionLocal, init_models
from app.core.errors import NotFoundError, ValidationError
from app.core.security import hash_password
from app.models import (
    Agent, Tenant, User, Workflow, WorkflowTemplate, WorkflowTemplateRating,
)
from app.services import workflow_template_scan as SCAN
from app.services import workflow_template_service as T


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


SAFE_GRAPH = {
    "nodes": [
        {"id": "s", "type": "start", "data": {"inputs": [{"name": "query"}]}},
        {"id": "t", "type": "transform", "data": {"value": "{{s.query}}", "op": "to_text"}},
        {"id": "e", "type": "end", "data": {"output": "{{t.output}}"}},
    ],
    "edges": [{"source": "s", "target": "t"}, {"source": "t", "target": "e"}],
}

CODE_GRAPH = {
    "nodes": [
        {"id": "s", "type": "start", "data": {"inputs": []}},
        {"id": "c", "type": "code", "data": {"code": "import os\nos.system('id')"}},
        {"id": "e", "type": "end", "data": {"output": "{{c.output}}"}},
    ],
    "edges": [{"source": "s", "target": "c"}, {"source": "c", "target": "e"}],
}

HTTP_GRAPH = {
    "nodes": [
        {"id": "s", "type": "start", "data": {"inputs": []}},
        {"id": "h", "type": "http", "data": {"url": "https://api.example.com/x", "method": "GET"}},
        {"id": "e", "type": "end", "data": {"output": "{{h.output}}"}},
    ],
    "edges": [{"source": "s", "target": "h"}, {"source": "h", "target": "e"}],
}


async def _setup(slug="scan", *, graph=None):
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
            db.add(Workflow(tenant_id=t.id, agent_id=a.id, name="t", graph=graph or SAFE_GRAPH))
        await db.commit()
        return {"tenant_id": t.id, "user_id": u.id, "agent_id": a.id}


# ============================================================ 风险扫描


def test_scan_safe_graph_is_none():
    r = SCAN.scan_graph(SAFE_GRAPH)
    assert r["level"] == "none"
    assert r["findings"] == []


def test_scan_detects_code_as_high():
    r = SCAN.scan_graph(CODE_GRAPH)
    assert r["level"] == "high"
    assert any(f["node_type"] == "code" for f in r["findings"])
    assert "os.system" in r["findings"][0]["hint"]


def test_scan_detects_http_as_medium():
    r = SCAN.scan_graph(HTTP_GRAPH)
    assert r["level"] == "medium"
    assert "api.example.com" in r["findings"][0]["hint"]


def test_scan_takes_highest_level():
    mixed = {
        "nodes": [
            {"id": "s", "type": "start", "data": {}},
            {"id": "h", "type": "http", "data": {"url": "https://x"}},
            {"id": "c", "type": "code", "data": {"code": "print(1)"}},
            {"id": "e", "type": "end", "data": {}},
        ],
        "edges": [],
    }
    r = SCAN.scan_graph(mixed)
    assert r["level"] == "high"
    assert len(r["findings"]) == 2


def test_scan_all_builtin_templates():
    """内置模板的风险等级应已知且不含 high。"""
    for t in T.BUILTIN_TEMPLATES:
        r = SCAN.scan_graph(t["graph"])
        assert r["level"] != "high", f"{t['key']} 不应含高风险节点"


def test_check_publishable_blocks_code():
    ok, reason = SCAN.check_publishable(CODE_GRAPH)
    assert ok is False
    assert "可执行代码" in reason


def test_check_publishable_allows_code_when_overridden():
    ok, _ = SCAN.check_publishable(CODE_GRAPH, allow_high_risk=True)
    assert ok is True


def test_check_publishable_allows_medium():
    ok, reason = SCAN.check_publishable(HTTP_GRAPH)
    assert ok is True
    assert "medium" in reason


# ============================================================ 发布审核


def test_publish_safe_template_goes_pending():
    async def _t():
        ctx = await _setup("rev1")
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"],
                agent_id=ctx["agent_id"], name="安全模板",
            )
            await db.commit()
            res = await T.set_visibility(
                db, tenant_id=ctx["tenant_id"], template_id=r["id"], public=True,
            )
            await db.commit()
            assert res["visibility"] == "pending"
            assert res["review_status"] == "pending"
            assert res["risk_level"] == "none"

            # 未审核前公共库看不到
            pub = await T.list_public_templates(db, tenant_id=999)
            assert all(x["id"] != r["id"] for x in pub)
    _run(_t())


def test_publish_code_template_blocked():
    async def _t():
        ctx = await _setup("rev2", graph=CODE_GRAPH)
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"], agent_id=ctx["agent_id"],
            )
            await db.commit()
            res = await T.set_visibility(
                db, tenant_id=ctx["tenant_id"], template_id=r["id"], public=True,
            )
            await db.commit()
            assert res.get("blocked") is True
            assert res["risk_level"] == "high"
    _run(_t())


def test_publish_code_with_override_allowed():
    async def _t():
        ctx = await _setup("rev3", graph=CODE_GRAPH)
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=ctx["tenant_id"], user_id=ctx["user_id"], agent_id=ctx["agent_id"],
            )
            await db.commit()
            res = await T.set_visibility(
                db, tenant_id=ctx["tenant_id"], template_id=r["id"], public=True,
                allow_high_risk=True,
            )
            await db.commit()
            assert not res.get("blocked")
            assert res["review_status"] == "pending"
    _run(_t())


def test_review_approve_publishes():
    async def _t():
        a = await _setup("rev4")
        b = await _setup("rev4b")
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=a["tenant_id"], user_id=a["user_id"], agent_id=a["agent_id"],
                name="待审",
            )
            await db.commit()
            await T.set_visibility(db, tenant_id=a["tenant_id"], template_id=r["id"], public=True)
            await db.commit()

            res = await T.review_template(
                db, template_id=r["id"], approve=True, note="看起来没问题", reviewer_id=999,
            )
            await db.commit()
            assert res["review_status"] == "approved"
            assert res["visibility"] == "public"

            pub = await T.list_public_templates(db, tenant_id=b["tenant_id"])
            assert any(x["id"] == r["id"] for x in pub)
    _run(_t())


def test_review_reject_returns_private():
    async def _t():
        a = await _setup("rev5")
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=a["tenant_id"], user_id=a["user_id"], agent_id=a["agent_id"],
            )
            await db.commit()
            await T.set_visibility(db, tenant_id=a["tenant_id"], template_id=r["id"], public=True)
            await db.commit()
            res = await T.review_template(
                db, template_id=r["id"], approve=False, note="不通过",
            )
            await db.commit()
            assert res["review_status"] == "rejected"
            assert res["visibility"] == "private"
    _run(_t())


def test_review_requires_pending():
    async def _t():
        a = await _setup("rev6")
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=a["tenant_id"], user_id=a["user_id"], agent_id=a["agent_id"],
            )
            await db.commit()
            with pytest.raises(ValidationError):
                await T.review_template(db, template_id=r["id"], approve=True)
    _run(_t())


def test_pending_list_shows_scan_findings():
    async def _t():
        a = await _setup("rev7", graph=HTTP_GRAPH)
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=a["tenant_id"], user_id=a["user_id"], agent_id=a["agent_id"],
                name="中风险",
            )
            await db.commit()
            await T.set_visibility(db, tenant_id=a["tenant_id"], template_id=r["id"], public=True)
            await db.commit()
            pend = await T.list_pending_reviews(db)
            item = next(x for x in pend if x["id"] == r["id"])
            assert item["risk_level"] == "medium"
            assert item["risk_findings"], "审核员要能看到具体风险节点"
    _run(_t())


# ============================================================ 评分


def test_rate_template_updates_average():
    async def _t():
        a = await _setup("rate1")
        b = await _setup("rate2")
        c = await _setup("rate3")
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=a["tenant_id"], user_id=a["user_id"], agent_id=a["agent_id"],
                name="被评分",
            )
            await db.commit()
            await T.set_visibility(db, tenant_id=a["tenant_id"], template_id=r["id"], public=True)
            await T.review_template(db, template_id=r["id"], approve=True)
            await db.commit()

            r1 = await T.rate_template(
                db, tenant_id=b["tenant_id"], user_id=b["user_id"], template_id=r["id"], rating=5,
            )
            await db.commit()
            assert r1["rating"] == 5.0 and r1["rating_count"] == 1

            r2 = await T.rate_template(
                db, tenant_id=c["tenant_id"], user_id=c["user_id"], template_id=r["id"], rating=3,
            )
            await db.commit()
            assert r2["rating"] == 4.0 and r2["rating_count"] == 2
    _run(_t())


def test_rate_is_idempotent_per_tenant():
    """同租户重复评分应改分而非累加。"""
    async def _t():
        a = await _setup("rate4")
        b = await _setup("rate5")
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=a["tenant_id"], user_id=a["user_id"], agent_id=a["agent_id"],
            )
            await db.commit()
            await T.rate_template(
                db, tenant_id=b["tenant_id"], user_id=b["user_id"], template_id=r["id"], rating=5,
            )
            await db.commit()
            r2 = await T.rate_template(
                db, tenant_id=b["tenant_id"], user_id=b["user_id"], template_id=r["id"], rating=2,
            )
            await db.commit()
            assert r2["rating_count"] == 1
            assert r2["rating"] == 2.0

            rows = (await db.execute(
                select(WorkflowTemplateRating).where(
                    WorkflowTemplateRating.template_id == r["id"]
                )
            )).scalars().all()
            assert len(rows) == 1
    _run(_t())


def test_cannot_rate_own_template():
    async def _t():
        a = await _setup("rate6")
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=a["tenant_id"], user_id=a["user_id"], agent_id=a["agent_id"],
            )
            await db.commit()
            with pytest.raises(ValidationError, match="自己的模板"):
                await T.rate_template(
                    db, tenant_id=a["tenant_id"], user_id=a["user_id"],
                    template_id=r["id"], rating=5,
                )
    _run(_t())


def test_rating_bounds():
    async def _t():
        a = await _setup("rate7")
        b = await _setup("rate8")
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=a["tenant_id"], user_id=a["user_id"], agent_id=a["agent_id"],
            )
            await db.commit()
            with pytest.raises(ValidationError):
                await T.rate_template(
                    db, tenant_id=b["tenant_id"], user_id=b["user_id"],
                    template_id=r["id"], rating=6,
                )
            with pytest.raises(ValidationError):
                await T.rate_template(
                    db, tenant_id=b["tenant_id"], user_id=b["user_id"],
                    template_id=r["id"], rating=0,
                )
    _run(_t())


def test_public_list_sorted_by_rating():
    async def _t():
        a = await _setup("sortr1")
        b = await _setup("sortr2")
        c = await _setup("sortr3")
        async with AsyncSessionLocal() as db:
            ids = []
            for name in ("差", "好"):
                r = await T.save_template(
                    db, tenant_id=a["tenant_id"], user_id=a["user_id"], agent_id=a["agent_id"],
                    name=name,
                )
                await db.commit()
                await T.set_visibility(db, tenant_id=a["tenant_id"], template_id=r["id"], public=True)
                await T.review_template(db, template_id=r["id"], approve=True)
                await db.commit()
                ids.append(r["id"])

            await T.rate_template(db, tenant_id=b["tenant_id"], user_id=b["user_id"],
                                  template_id=ids[0], rating=1)
            await T.rate_template(db, tenant_id=b["tenant_id"], user_id=b["user_id"],
                                  template_id=ids[1], rating=5)
            await T.rate_template(db, tenant_id=c["tenant_id"], user_id=c["user_id"],
                                  template_id=ids[1], rating=5)
            await db.commit()

            pub = await T.list_public_templates(db, tenant_id=999)
            names = [x["name"] for x in pub if x["name"] in ("差", "好")]
            assert names.index("好") < names.index("差"), "高评分应排前面"
    _run(_t())


def test_template_out_includes_new_fields():
    async def _t():
        a = await _setup("shape2")
        async with AsyncSessionLocal() as db:
            r = await T.save_template(
                db, tenant_id=a["tenant_id"], user_id=a["user_id"], agent_id=a["agent_id"],
            )
            await db.commit()
            rows = await T.list_user_templates(db, tenant_id=a["tenant_id"])
            item = next(x for x in rows if x["id"] == r["id"])
            for k in ("risk_level", "review_status", "rating", "rating_count"):
                assert k in item, k
    _run(_t())


def test_routes_registered():
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/api/v1/workflow/templates/pending" in paths
    assert "/api/v1/workflow/templates/{template_id}/review" in paths
    assert "/api/v1/workflow/templates/{template_id}/rate" in paths
