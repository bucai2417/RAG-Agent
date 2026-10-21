"""Webhook 回调投递：带指数退避重试与死信落库。

为什么需要独立服务：
- 对接方临时宕机很常见（重启、发版），一次失败就丢结果不可接受
- 重试必须可观测：失败几次、下次什么时候、最终是否放弃，都要能看到

重试策略：
- 最多 5 次，间隔 5s → 15s → 60s → 300s（指数，封顶 5 分钟）
- 4xx（除 408/429）不重试：对方明确拒绝，重试无意义
- 全部失败后标记 dead，保留最后一次错误与响应体供排障

设计要点：
- 投递记录独立落库（`workflow_callback_delivery`），不塞进 run.output
  —— 重试是跨请求的，需要独立行来更新状态
- 每次尝试都更新 `attempts`/`next_attempt_at`，进程重启后仍能续跑
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx

from app.core.crypto import decrypt
from app.core.db import AsyncSessionLocal
from app.core.logging import get_logger
from app.models import WorkflowCallbackDelivery, WorkflowRun

logger = get_logger("workflow.callback")

# 第 n 次失败后的等待秒数（下标从 0 开始）。长度即最大重试次数。
BACKOFF_SECONDS: tuple[int, ...] = (5, 15, 60, 300)
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1  # 首次 + 4 次重试 = 5


def _now_ms() -> int:
    return int(time.time() * 1000)


def _should_retry(status_code: int | None) -> bool:
    """4xx 一般不重试（对方明确拒绝），408/429 属于暂时性错误，要重试。"""
    if status_code is None:
        return True  # 网络错误：重试
    if status_code in (408, 429):
        return True
    return not (400 <= status_code < 500)


async def enqueue(
    db, *, run_id: int, tenant_id: int, url: str, secret_enc: str | None, payload: dict,
) -> WorkflowCallbackDelivery:
    """登记一条待投递记录（立即尝试由 flush_ready 调度）。"""
    d = WorkflowCallbackDelivery(
        tenant_id=tenant_id, run_id=run_id, url=url,
        secret=secret_enc,
        payload=payload,
        status="pending", attempts=0,
        next_attempt_at=_now_ms(),
    )
    db.add(d)
    await db.flush()
    return d


async def deliver_once(d: WorkflowCallbackDelivery) -> tuple[bool, int | None, str | None]:
    """尝试投递一次。返回 (成功, HTTP 状态码, 错误文本)。"""
    body = json.dumps(d.payload or {}, ensure_ascii=False)
    headers = {"Content-Type": "application/json", "User-Agent": "rag-platform"}
    secret = decrypt(d.secret) if d.secret else None
    if secret:
        sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        headers["X-Signature"] = f"sha256={sig}"

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            resp = await c.post(d.url, content=body.encode(), headers=headers)
        ok = 200 <= resp.status_code < 300
        return ok, resp.status_code, None if ok else f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:  # noqa: BLE001
        return False, None, f"{type(e).__name__}: {str(e)[:200]}"


async def flush_ready(*, limit: int = 50) -> dict:
    """处理所有到期的待投递记录（后台定时任务调用）。"""
    from sqlalchemy import select

    stats = {"sent": 0, "retry": 0, "dead": 0}
    async with AsyncSessionLocal() as db:
        now = _now_ms()
        rows = (
            await db.execute(
                select(WorkflowCallbackDelivery)
                .where(
                    WorkflowCallbackDelivery.status.in_(("pending", "retrying")),
                    WorkflowCallbackDelivery.next_attempt_at <= now,
                )
                .order_by(WorkflowCallbackDelivery.next_attempt_at)
                .limit(limit)
            )
        ).scalars().all()

        for d in rows:
            ok, status, err = await deliver_once(d)
            d.attempts = (d.attempts or 0) + 1
            d.last_status_code = status
            d.last_error = err
            d.last_attempt_at = _now_ms()

            if ok:
                d.status = "sent"
                d.next_attempt_at = None
                stats["sent"] += 1
                logger.info("callback_delivered", delivery_id=d.id, run_id=d.run_id,
                            attempts=d.attempts)
            elif d.attempts >= MAX_ATTEMPTS or not _should_retry(status):
                d.status = "dead"
                d.next_attempt_at = None
                stats["dead"] += 1
                logger.warning("callback_dead", delivery_id=d.id, run_id=d.run_id,
                               attempts=d.attempts, status=status, err=(err or "")[:120])
            else:
                d.status = "retrying"
                wait = BACKOFF_SECONDS[min(d.attempts - 1, len(BACKOFF_SECONDS) - 1)]
                d.next_attempt_at = _now_ms() + wait * 1000
                stats["retry"] += 1
        await db.commit()
    return stats


async def list_deliveries(db, *, tenant_id: int, run_id: int | None = None, limit: int = 50) -> list[dict]:
    from sqlalchemy import select

    stmt = select(WorkflowCallbackDelivery).where(WorkflowCallbackDelivery.tenant_id == tenant_id)
    if run_id:
        stmt = stmt.where(WorkflowCallbackDelivery.run_id == run_id)
    rows = (
        await db.execute(stmt.order_by(WorkflowCallbackDelivery.id.desc()).limit(min(limit, 200)))
    ).scalars().all()
    return [
        {
            "id": r.id, "run_id": r.run_id, "url": r.url, "status": r.status,
            "attempts": r.attempts or 0, "last_status_code": r.last_status_code,
            "last_error": r.last_error, "next_attempt_at": r.next_attempt_at,
            "last_attempt_at": r.last_attempt_at,
        }
        for r in rows
    ]


async def retry_delivery(db, *, tenant_id: int, delivery_id: int) -> bool:
    """手动重投（后台「重试」按钮用）：重置状态并立即尝试一次。"""
    from sqlalchemy import select

    d = (
        await db.execute(
            select(WorkflowCallbackDelivery).where(
                WorkflowCallbackDelivery.id == delivery_id,
                WorkflowCallbackDelivery.tenant_id == tenant_id,
            )
        )
    ).scalars().first()
    if not d:
        return False
    d.status = "pending"
    d.next_attempt_at = _now_ms()
    d.attempts = 0
    d.last_error = None
    await db.flush()
    stats = await flush_ready(limit=1)
    return stats["sent"] > 0
