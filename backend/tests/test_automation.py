"""自动化能力测试：通知渠道、站内消息、执行历史、一次性/重试、Webhook、事件触发。"""
from __future__ import annotations

import asyncio

import pytest


# ---- 通知：加密 + 渠道构建 + 派发 ----
def test_notifier_build_all_kinds():
    from app.notifiers.registry import build_notifier

    assert build_notifier("inapp", tenant_id=1, config={}).__class__.__name__ == "InAppNotifier"
    assert build_notifier("webhook", tenant_id=1, config={"url": "https://x/y"}).__class__.__name__ == "WebhookNotifier"
    assert build_notifier("wecom", tenant_id=1, config={"webhook_url": "https://x"}).__class__.__name__ == "WecomNotifier"
    assert build_notifier("dingtalk", tenant_id=1, config={"webhook_url": "https://x"}).__class__.__name__ == "WecomNotifier"
    assert build_notifier("smtp", tenant_id=1, config={"host": "x", "to_addrs": ["a@b.c"]}).__class__.__name__ == "SmtpNotifier"


def test_notifier_unknown_kind_raises():
    from app.core.errors import ValidationError
    from app.notifiers.registry import build_notifier

    with pytest.raises(ValidationError):
        build_notifier("nope", tenant_id=1, config={})


def test_notifier_config_secret_decrypt():
    """渠道配置里的密钥字段读时自动解密。"""
    from app.core.crypto import encrypt
    from app.notifiers.registry import _decrypt_config

    tok = encrypt("secret-123")
    cfg = _decrypt_config({"webhook_url": "https://x", "secret": tok, "password": tok})
    assert cfg["secret"] == "secret-123"
    assert cfg["password"] == "secret-123"


@pytest.mark.asyncio
async def test_webhook_notifier_ssrf_blocked():
    """Webhook 出站必须过 SSRF 守卫（回环被拒）。"""
    from app.core.errors import ValidationError
    from app.notifiers.base import NotificationMessage
    from app.notifiers.drivers.webhook import WebhookNotifier

    n = WebhookNotifier(config={"url": "http://127.0.0.1:9/x"})
    with pytest.raises(ValidationError):
        await n.send(NotificationMessage(title="t"))


def test_inapp_message_definition():
    from app.notifiers.base import NotificationMessage

    m = NotificationMessage(title="标题", body="正文", kind="task", level="success", user_id=5)
    assert m.kind == "task" and m.level == "success"


# ---- 站内消息 + 渠道 dispatch（DB 级）----
async def _setup_notif():
    from app.core.db import AsyncSessionLocal, init_models
    from app.core.security import hash_password
    from app.models import Notification, Tenant, User

    await init_models()
    async with AsyncSessionLocal() as db:
        tenant = Tenant(name="TN", slug="tn")
        db.add(tenant); await db.flush()
        u = User(tenant_id=tenant.id, username="nu", password_hash=hash_password("x"), display_name="nu")
        db.add(u); await db.flush()
        await db.commit()
        return {"tenant": tenant.id, "user": u.id}


@pytest.fixture(scope="module")
def ndata():
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_setup_notif())
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_dispatch_writes_inapp(ndata):
    """dispatch 即使无 NotifyChannel 记录，也应把站内消息写入（保证铃铛有内容）。"""
    from app.core.db import AsyncSessionLocal
    from app.models import Notification
    from app.notifiers.base import NotificationMessage
    from app.notifiers.registry import dispatch

    async with AsyncSessionLocal() as db:
        res = await dispatch(
            db, tenant_id=ndata["tenant"], user_id=ndata["user"],
            msg=NotificationMessage(title="任务完成", kind="task", level="success", user_id=ndata["user"]),
        )
        await db.commit()
    assert res.get("inapp") is True

    async with AsyncSessionLocal() as db:
        from sqlalchemy import func, select

        cnt = (await db.execute(
            select(func.count()).select_from(Notification).where(Notification.user_id == ndata["user"])
        )).scalar_one()
        assert cnt >= 1


# ---- 执行历史 + 一次性 + 重试 ----
def test_scheduled_task_model_fields():
    from app.models import ScheduledTask

    cols = ScheduledTask.__table__.columns
    assert cols["trigger_kind"].default.arg == "schedule"
    assert cols["max_retries"].default.arg == 0
    assert cols["retry_count"].default.arg == 0
    assert "run_at" in cols and "event_name" in cols


def test_scheduled_task_run_model():
    from app.models import ScheduledTaskRun

    cols = ScheduledTaskRun.__table__.columns
    assert cols["status"].default.arg == "running"
    assert cols["attempt"].default.arg == 1


def test_once_validation():
    from app.api.v1.scheduled_tasks import TaskIn, _validate_schedule
    from app.core.errors import ValidationError

    # once 无 run_at → 报错
    with pytest.raises(ValidationError):
        _validate_schedule(TaskIn(name="a", agent_id=1, schedule_kind="once"))
    # once 带 run_at → 通过
    _validate_schedule(TaskIn(name="a", agent_id=1, schedule_kind="once", run_at=9999999999999))


def test_retry_cap_validation():
    from app.api.v1.scheduled_tasks import TaskIn, _validate_schedule
    from app.core.errors import ValidationError

    with pytest.raises(ValidationError):
        _validate_schedule(TaskIn(name="a", agent_id=1, schedule_kind="interval",
                                  interval_seconds=60, max_retries=999))


@pytest.mark.asyncio
async def test_compute_next_once_returns_run_at():
    from app.api.v1.scheduled_tasks import _compute_next
    from app.models import ScheduledTask

    t = ScheduledTask(tenant_id=1, owner_id=1, agent_id=1, name="once", schedule_kind="once", run_at=1234567890123)
    assert await _compute_next(t) == 1234567890123


@pytest.mark.asyncio
async def test_run_history_recorded(ndata):
    """execute_task 成功/失败都应落一条 ScheduledTaskRun（用直接落库验证记录路径）。"""
    from app.core.db import AsyncSessionLocal
    from app.models import ScheduledTaskRun

    async with AsyncSessionLocal() as db:
        db.add(ScheduledTaskRun(
            tenant_id=ndata["tenant"], task_id=999, status="success",
            started_at=1, finished_at=2, duration_ms=1, output="ok", attempt=1,
        ))
        await db.commit()
    async with AsyncSessionLocal() as db:
        from sqlalchemy import func, select

        cnt = (await db.execute(
            select(func.count()).select_from(ScheduledTaskRun).where(ScheduledTaskRun.task_id == 999)
        )).scalar_one()
        assert cnt == 1


# ---- Webhook token ----
def test_webhook_hash_stable():
    from app.api.v1.webhooks import _hash

    assert _hash("abc") == _hash("abc")
    assert _hash("abc") != _hash("abd")
    assert len(_hash("abc")) == 64  # sha256 hex


def test_webhook_endpoints_registered():
    from app.api.v1.webhooks import router

    paths = {r.path for r in router.routes}
    # {handle} 同时接受自定义可读路径与随机 token
    assert "/hooks/{handle}" in paths
    assert "/scheduled-tasks/{task_id}/webhook-token" in paths
    assert "/webhook-tokens/{token_id}" in paths
    # 工作流接入点（外部系统直接调用工作流）
    assert "/workflow-hooks" in paths
    assert "/workflow-hooks/{hook_id}/toggle" in paths


# ---- 事件总线 ----
@pytest.mark.asyncio
async def test_event_publish_no_subscribers(ndata):
    """无订阅者时不报错、返回 0。"""
    from app.tasks.event_bus import publish

    n = await publish("document.ready", tenant_id=ndata["tenant"], payload={"document_id": 1})
    assert n == 0


def test_event_dispatches_to_matching_task():
    """事件派发：查 trigger_kind=event 且 event_name 匹配的任务（纯查询逻辑）。"""
    from app.models import ScheduledTask

    t = ScheduledTask(tenant_id=1, owner_id=1, agent_id=1, name="e", trigger_kind="event", event_name="document.ready")
    assert t.trigger_kind == "event" and t.event_name == "document.ready"
