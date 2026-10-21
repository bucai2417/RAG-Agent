"""外部接入增强：限流、异步模式、回调投递与签名。"""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from sqlalchemy import select

from app.api.v1 import webhooks as W
from app.core.db import AsyncSessionLocal, init_models
from app.core.errors import AppError, ValidationError
from app.core.security import hash_password
from app.models import Agent, Tenant, User, WebhookToken, Workflow


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


GRAPH = {
    "nodes": [
        {"id": "s", "type": "start", "data": {"inputs": [{"name": "message"}]}},
        {"id": "tt", "type": "text_template", "data": {"sections": [
            {"title": "回复", "content": "收到：{{s.message}}"},
        ]}},
        {"id": "e", "type": "end", "data": {"output": "{{tt.text}}"}},
    ],
    "edges": [{"source": "s", "target": "tt"}, {"source": "tt", "target": "e"}],
}


async def _setup(slug="hookx"):
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


# ============================================================ 限流


def test_rate_limit_blocks_after_quota():
    """按接入点限流：配额用完后返回 429。"""
    async def _t():
        from app.core.rate_limit import rate_limiter

        await _setup("rl")
        tok = WebhookToken(tenant_id=1, kind="workflow", agent_id=1,
                           token_hash=hashlib.sha256(b"rl-tok").hexdigest(),
                           rate_limit_per_min=2)
        allowed = [
            await rate_limiter.allow(f"hooktest:{tok.token_hash}", 2) for _ in range(3)
        ]
        assert allowed == [True, True, False]
    _run(_t())


def test_rate_limit_zero_means_unlimited():
    async def _t():
        from app.core.rate_limit import rate_limiter

        allowed = [await rate_limiter.allow("hooktest:unlimited", 0) for _ in range(20)]
        assert all(allowed)
    _run(_t())


def test_rate_limited_error_is_429():
    err = W._rate_limited(30)
    assert isinstance(err, AppError)
    assert err.status_code == 429
    assert "30" in str(err)


# ============================================================ 异步模式


def test_async_requires_callback_url():
    async def _t():
        ctx = await _setup("asyncnocb")
        async with AsyncSessionLocal() as db:
            tok = WebhookToken(tenant_id=ctx["tenant_id"], kind="workflow",
                               agent_id=ctx["agent_id"],
                               token_hash=hashlib.sha256(b"nocb").hexdigest(),
                               created_by=ctx["user_id"])
            db.add(tok)
            await db.commit()
            with pytest.raises(ValidationError, match="回调地址"):
                await W._invoke_workflow_async(db, tok, {"message": "x", "mode": "async"})
    _run(_t())


def test_async_accepts_and_returns_run_id(monkeypatch):
    """异步模式立即返回 run_id，不阻塞等待结果。"""
    spawned: list = []

    def _fake_spawn(name, coro):
        spawned.append(name)
        coro.close()  # 不真的执行，避免测试里跑后台任务
        return None

    async def _t():
        ctx = await _setup("asyncok")
        async with AsyncSessionLocal() as db:
            tok = WebhookToken(tenant_id=ctx["tenant_id"], kind="workflow",
                               agent_id=ctx["agent_id"],
                               token_hash=hashlib.sha256(b"asynctok").hexdigest(),
                               callback_url="https://example.com/cb",
                               created_by=ctx["user_id"])
            db.add(tok)
            await db.commit()
            import app.tasks.bg as bg

            monkeypatch.setattr(bg, "spawn", _fake_spawn)
            r = await W._invoke_workflow_async(db, tok, {"message": "查订单", "mode": "async"})
            assert r["status"] == "accepted"
            assert r["run_id"]
            assert "回调" in r["message"]
            assert spawned == ["hook_workflow_run"]
    _run(_t())


def test_async_strips_mode_from_inputs(monkeypatch):
    """mode 是控制参数，不该作为工作流输入变量。"""
    captured: dict = {}

    def _fake_spawn(name, coro):
        captured["name"] = name
        coro.close()

    async def _t():
        ctx = await _setup("asyncmode")
        async with AsyncSessionLocal() as db:
            tok = WebhookToken(tenant_id=ctx["tenant_id"], kind="workflow",
                               agent_id=ctx["agent_id"],
                               token_hash=hashlib.sha256(b"modetok").hexdigest(),
                               callback_url="https://example.com/cb",
                               created_by=ctx["user_id"])
            db.add(tok)
            await db.commit()
            import app.tasks.bg as bg

            monkeypatch.setattr(bg, "spawn", _fake_spawn)
            await W._invoke_workflow_async(db, tok, {"message": "hi", "mode": "async"})
            from app.models import WorkflowRun

            run = (await db.execute(
                select(WorkflowRun).order_by(WorkflowRun.id.desc()).limit(1)
            )).scalar_one()
            assert "mode" not in (run.input or {})
            assert run.input.get("message") == "hi"
    _run(_t())


# ============================================================ 回调投递


def test_run_and_callback_posts_with_signature(monkeypatch):
    """回调带 HMAC 签名，对接方可验证来源。

    回调现在走投递队列（支持重试），所以这里验证：
    1. 投递记录被登记，payload 正确
    2. deliver_once 生成的签名与密钥重算一致
    """
    posted: dict = {}

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None, headers=None, json=None):
            posted.update({"url": url, "content": content, "headers": headers})
            return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    async def _t():
        from sqlalchemy import select as _sel

        from app.models import WorkflowCallbackDelivery

        ctx = await _setup("cbsig")
        from app.core.crypto import encrypt

        async with AsyncSessionLocal() as db:
            tok = WebhookToken(tenant_id=ctx["tenant_id"], kind="workflow",
                               agent_id=ctx["agent_id"],
                               token_hash=hashlib.sha256(b"sigtok").hexdigest(),
                               callback_url="https://example.com/cb",
                               callback_secret=encrypt("s3cret"),
                               created_by=ctx["user_id"])
            db.add(tok)
            await db.commit()
            tok_id = tok.id

        await W._run_and_callback(
            run_id=1, token_id=tok_id, agent_id=ctx["agent_id"], wf_id=1,
            tenant_id=ctx["tenant_id"], inputs={"message": "hi"},
        )

        # 1) 投递记录已登记且 payload 正确
        async with AsyncSessionLocal() as db:
            d = (await db.execute(
                _sel(WorkflowCallbackDelivery).where(
                    WorkflowCallbackDelivery.tenant_id == ctx["tenant_id"]
                ).order_by(WorkflowCallbackDelivery.id.desc())
            )).scalars().first()
            assert d is not None, "应登记回调投递记录"
            assert d.url == "https://example.com/cb"
            assert d.payload["run_id"] == 1
            assert d.payload["ok"] is True
            delivery_id = d.id

        # 2) 签名可被同样密钥验证
        from app.services import workflow_callback_service as CB

        async with AsyncSessionLocal() as db:
            row = await db.get(WorkflowCallbackDelivery, delivery_id)
            ok, status, err = await CB.deliver_once(row)
        assert ok is True and status == 200
        assert posted["url"] == "https://example.com/cb"
        body = posted["content"].decode()
        import hmac as _hmac

        real = "sha256=" + _hmac.new(b"s3cret", body.encode(), hashlib.sha256).hexdigest()
        assert posted["headers"]["X-Signature"] == real
    _run(_t())



def test_run_and_callback_success_payload(monkeypatch):
    """成功结果写进投递记录的 payload（回调失败可重试，内容不丢）。"""
    from sqlalchemy import select as _sel

    from app.models import WorkflowCallbackDelivery

    async def _t():
        ctx = await _setup("cbsuccess")
        async with AsyncSessionLocal() as db:
            tok = WebhookToken(tenant_id=ctx["tenant_id"], kind="workflow",
                               agent_id=ctx["agent_id"],
                               token_hash=hashlib.sha256(b"oktok").hexdigest(),
                               callback_url="https://example.com/cb",
                               created_by=ctx["user_id"])
            db.add(tok)
            await db.commit()
            tok_id = tok.id
            wf = (await db.execute(
                select(Workflow).where(Workflow.agent_id == ctx["agent_id"])
            )).scalars().first()

        await W._run_and_callback(
            run_id=1, token_id=tok_id, agent_id=ctx["agent_id"], wf_id=wf.id,
            tenant_id=ctx["tenant_id"], inputs={"message": "你好"},
        )
        async with AsyncSessionLocal() as db:
            d = (await db.execute(
                _sel(WorkflowCallbackDelivery).where(
                    WorkflowCallbackDelivery.tenant_id == ctx["tenant_id"]
                ).order_by(WorkflowCallbackDelivery.id.desc())
            )).scalars().first()
            assert d is not None
            assert d.payload["status"] == "success"
            assert "收到：你好" in d.payload["text"]
    _run(_t())


def test_run_and_callback_failure_payload(monkeypatch):
    posted: dict = {}

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None, headers=None, json=None):
            posted.update({"content": content, "json": json})
            return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    async def _t():
        from sqlalchemy import select as _sel
        from sqlalchemy import update as _upd

        from app.models import WorkflowCallbackDelivery

        ctx = await _setup("cbfail")
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
            tok = WebhookToken(tenant_id=ctx["tenant_id"], kind="workflow",
                               agent_id=ctx["agent_id"],
                               token_hash=hashlib.sha256(b"failtok").hexdigest(),
                               callback_url="https://example.com/cb",
                               created_by=ctx["user_id"])
            db.add(tok)
            await db.commit()
            tok_id = tok.id
            wf = (await db.execute(
                select(Workflow).where(Workflow.agent_id == ctx["agent_id"])
            )).scalars().first()

        await W._run_and_callback(
            run_id=1, token_id=tok_id, agent_id=ctx["agent_id"], wf_id=wf.id,
            tenant_id=ctx["tenant_id"], inputs={"message": "x"},
        )
        async with AsyncSessionLocal() as db:
            d = (await db.execute(
                _sel(WorkflowCallbackDelivery).where(
                    WorkflowCallbackDelivery.tenant_id == ctx["tenant_id"]
                ).order_by(WorkflowCallbackDelivery.id.desc())
            )).scalars().first()
            assert d is not None
            assert d.payload["status"] == "failed"
            assert d.payload["error"]
    _run(_t())


# ============================================================ 后台任务宽限


def test_bg_critical_tasks_get_grace():
    """退出时关键任务（hook_ 前缀）不应被立刻取消。"""
    from app.tasks import bg

    assert bg._is_critical(type("T", (), {"get_name": lambda self: "hook_workflow_run"})())
    assert not bg._is_critical(type("T", (), {"get_name": lambda self: "workflow_run"})())


def test_callback_url_validated_on_create():
    """创建接入点时校验回调地址（SSRF 防护）。"""
    from app.agents.tools.builtin import _check_url

    assert _check_url("http://127.0.0.1:8080/cb", None) is not None
    assert _check_url("http://10.0.0.5/cb", None) is not None
    assert _check_url("https://example.com/cb", None) is None
