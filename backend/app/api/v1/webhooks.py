"""入站 Webhook：外部系统触发定时任务或工作流。

两种令牌（`WebhookToken.kind`）：
- scheduled_task：触发一次定时任务（异步，立即返回"已触发"）
- workflow：同步执行工作流并**直接返回结果**（客服系统对接的主流需求）

鉴权：随机 token 或自定义可读路径。token 只存哈希，明文仅创建时返回一次。

安全边界：
- 令牌绑定租户，跨租户不可见
- 工作流以「令牌创建者」的身份执行（继承其权限，不放大）
- 禁用的令牌立即失效（不需要删除）
"""
from __future__ import annotations

import hashlib
import re
import secrets
import time

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.logging import get_logger
from app.middleware.auth_dep import require_permission
from app.models import Agent, ScheduledTask, User, WebhookToken, Workflow

router = APIRouter(tags=["webhook"])
logger = get_logger("webhook")

# 自定义路径白名单字符：URL 安全、无歧义（不能带 / 以免与路由冲突）
_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{1,62}$")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _check_callback_url(url: str) -> str | None:
    """校验回调地址（SSRF 防护）。返回错误或 None。

    默认拒绝内网/环回；企业内网部署的客服系统需要通过系统设置
    `webhook_allow_private_callback` 显式放开。
    """
    from app.agents.tools.builtin import _check_url
    from app.core.config import settings

    err = _check_url(url, None)
    if err is None:
        return None
    if settings.webhook_allow_private_callback:
        # 只放行「内网/保留地址」类拦截，协议/主机名类错误仍然拒绝
        if "内网" in err or "保留地址" in err:
            return None
    return err


# ============================================================ 令牌管理


class WorkflowHookIn(BaseModel):
    agent_id: int
    name: str = "默认"
    # 自定义可读路径（可选），如 order-sync → /api/v1/hooks/order-sync
    path: str | None = None
    # 每分钟调用上限（0=不限）。外部系统狂调会打爆 LLM 配额
    rate_limit_per_min: int = 60
    # 异步回调地址（可选）：调用方传 mode=async 时，跑完 POST 到这里
    callback_url: str | None = None


@router.post("/workflow-hooks")
async def create_workflow_hook(
    body: WorkflowHookIn,
    user: User = Depends(require_permission("workflow:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """为工作流型智能体创建入站接入点。返回的 token/URL 只显示一次。"""
    a = await db.get(Agent, body.agent_id)
    if not a or a.tenant_id != user.tenant_id:
        raise NotFoundError("智能体不存在")
    if a.type != "workflow":
        raise ValidationError("只有工作流型智能体可以创建接入点")

    path = (body.path or "").strip().lower() or None
    if path:
        if not _PATH_RE.match(path):
            raise ValidationError("路径只能用小写字母、数字、下划线或短横线，长度 2-63")
        # 保留字：避免与既有路由冲突
        if path in ("hooks", "health", "docs", "openapi"):
            raise ValidationError(f"路径 {path} 为保留字")
        exists = (
            await db.execute(select(WebhookToken).where(WebhookToken.path == path))
        ).scalar_one_or_none()
        if exists:
            raise ConflictError(f"路径 {path} 已被占用")

    plain = secrets.token_urlsafe(24)
    # 回调地址要过 SSRF 校验：这是服务端主动发起的出站请求
    cb_url = (body.callback_url or "").strip() or None
    if cb_url:
        err = _check_callback_url(cb_url)
        if err:
            raise ValidationError(f"回调地址不可用：{err}")

    tok = WebhookToken(
        tenant_id=user.tenant_id, kind="workflow", agent_id=a.id,
        token_hash=_hash(plain), name=body.name or "默认", path=path,
        call_count=0, created_by=user.id,
        rate_limit_per_min=max(0, int(body.rate_limit_per_min or 0)),
        callback_url=cb_url,
    )
    db.add(tok)
    await db.flush()

    handle = path or plain
    return {
        "id": tok.id,
        "kind": "workflow",
        "agent_id": a.id,
        "agent_name": a.name,
        "token": plain,
        "path": path,
        # 两个 URL 都可用：可读路径便于对接方配置，token 便于轮换
        "hook_url": f"/api/v1/hooks/{handle}",
        "rate_limit_per_min": tok.rate_limit_per_min,
        "callback_url": tok.callback_url,
        "message": "请妥善保存 token，仅显示一次",
    }


@router.get("/workflow-hooks")
async def list_workflow_hooks(
    agent_id: int | None = None,
    user: User = Depends(require_permission("workflow:read")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    stmt = select(WebhookToken).where(
        WebhookToken.tenant_id == user.tenant_id, WebhookToken.kind == "workflow"
    )
    if agent_id:
        stmt = stmt.where(WebhookToken.agent_id == agent_id)
    rows = (await db.execute(stmt.order_by(WebhookToken.id.desc()))).scalars().all()

    agent_names: dict[int, str] = {}
    if rows:
        ids = [r.agent_id for r in rows if r.agent_id]
        if ids:
            for a in (await db.execute(select(Agent).where(Agent.id.in_(ids)))).scalars().all():
                agent_names[a.id] = a.name

    return [
        {
            "id": r.id, "agent_id": r.agent_id,
            "agent_name": agent_names.get(r.agent_id or 0, f"#{r.agent_id}"),
            "name": r.name, "enabled": r.enabled, "path": r.path,
            "hook_url": f"/api/v1/hooks/{r.path}" if r.path else None,
            "call_count": r.call_count or 0, "last_called_at": r.last_called_at,
            "rate_limit_per_min": r.rate_limit_per_min or 0,
            "callback_url": r.callback_url,
        }
        for r in rows
    ]


@router.delete("/workflow-hooks/{hook_id}")
async def delete_workflow_hook(
    hook_id: int,
    user: User = Depends(require_permission("workflow:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    tok = await db.get(WebhookToken, hook_id)
    if not tok or tok.tenant_id != user.tenant_id or tok.kind != "workflow":
        raise NotFoundError("接入点不存在")
    tok.enabled = False  # 软删除：保留调用统计，历史排障可用
    await db.flush()
    return {"message": "已停用"}


@router.post("/workflow-hooks/{hook_id}/toggle")
async def toggle_workflow_hook(
    hook_id: int,
    enabled: bool = True,
    user: User = Depends(require_permission("workflow:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    tok = await db.get(WebhookToken, hook_id)
    if not tok or tok.tenant_id != user.tenant_id or tok.kind != "workflow":
        raise NotFoundError("接入点不存在")
    tok.enabled = enabled
    await db.flush()
    return {"enabled": tok.enabled}


# ============================================================ 定时任务令牌（保持原接口）


@router.post("/scheduled-tasks/{task_id}/webhook-token")
async def create_webhook_token(
    task_id: int,
    body: dict,
    user: User = Depends(require_permission("schedule:manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """为定时任务生成入站 Webhook 令牌（明文仅本次返回）。"""
    t = await db.get(ScheduledTask, task_id)
    if not t or t.tenant_id != user.tenant_id:
        raise NotFoundError("定时任务不存在")
    plain = secrets.token_urlsafe(24)
    tok = WebhookToken(
        tenant_id=user.tenant_id, kind="scheduled_task", task_id=task_id,
        token_hash=_hash(plain), name=(body or {}).get("name") or "默认",
    )
    db.add(tok)
    await db.flush()
    return {"id": tok.id, "token": plain, "webhook_url": f"/api/v1/hooks/{plain}",
            "message": "请妥善保存 token，仅显示一次"}


@router.get("/scheduled-tasks/{task_id}/webhook-tokens")
async def list_webhook_tokens(
    task_id: int,
    user: User = Depends(require_permission("schedule:read")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    rows = (await db.execute(
        select(WebhookToken).where(
            WebhookToken.tenant_id == user.tenant_id,
            WebhookToken.kind == "scheduled_task",
            WebhookToken.task_id == task_id,
        )
    )).scalars().all()
    return [{"id": r.id, "name": r.name, "enabled": r.enabled,
             "call_count": r.call_count or 0} for r in rows]


@router.delete("/webhook-tokens/{token_id}")
async def delete_webhook_token(
    token_id: int,
    user: User = Depends(require_permission("schedule:manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    tk = await db.get(WebhookToken, token_id)
    if tk and tk.tenant_id == user.tenant_id:
        await db.delete(tk)
        await db.flush()
    return {"message": "已删除"}


# ============================================================ 公开入站端点


async def _resolve_token(db: AsyncSession, handle: str) -> WebhookToken:
    """按「自定义路径」或「token 明文」查找令牌。两者都可作为 URL。"""
    h = (handle or "").strip()
    if not h:
        raise PermissionDeniedError("缺少接入标识")
    tok = (
        await db.execute(select(WebhookToken).where(WebhookToken.path == h.lower()))
    ).scalar_one_or_none()
    if tok is None:
        tok = (
            await db.execute(select(WebhookToken).where(WebhookToken.token_hash == _hash(h)))
        ).scalar_one_or_none()
    if not tok or not tok.enabled:
        raise PermissionDeniedError("无效或已停用的接入令牌")
    return tok


@router.get("/workflow-callback-deliveries")
async def list_callback_deliveries(
    run_id: int | None = None,
    limit: int = 50,
    user: User = Depends(require_permission("workflow:read")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """回调投递记录（含重试次数与最后一次错误），供对接排障。"""
    from app.services import workflow_callback_service as CB

    return await CB.list_deliveries(
        db, tenant_id=user.tenant_id, run_id=run_id, limit=limit,
    )


@router.post("/workflow-callback-deliveries/{delivery_id}/retry")
async def retry_callback_delivery(
    delivery_id: int,
    user: User = Depends(require_permission("workflow:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """手动重投一条回调（重置次数后立即尝试一次）。"""
    from app.services import workflow_callback_service as CB

    ok = await CB.retry_delivery(db, tenant_id=user.tenant_id, delivery_id=delivery_id)
    await db.commit()
    return {"ok": ok, "message": "重投成功" if ok else "重投失败（仍会被后台继续重试）"}


@router.post("/hooks/{handle}")
async def invoke_webhook(
    handle: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """外部系统入站。

    - kind=workflow：默认**同步执行并返回结果**；请求体带 `mode=async` 时改为
      立即返回 run_id，完成后再 POST 到该接入点配置的 callback_url
    - kind=scheduled_task：异步触发，立即返回"已触发"

    限流按接入点计（`rate_limit_per_min`，0=不限），超限返回 429。
    """
    tok = await _resolve_token(db, handle)

    # 限流：按令牌 id，与服务与工作流无关（同一工作流可给不同对接方不同额度）
    if (tok.rate_limit_per_min or 0) > 0:
        from app.core.rate_limit import rate_limiter

        if not await rate_limiter.allow(f"hook:{tok.id}", tok.rate_limit_per_min):
            raise _rate_limited(tok.rate_limit_per_min)

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    if not isinstance(payload, dict):
        payload = {"data": payload}

    # 统计（失败不影响主流程）
    try:
        tok.call_count = (tok.call_count or 0) + 1
        tok.last_called_at = int(time.time() * 1000)
        await db.flush()
    except Exception:  # noqa: BLE001
        pass

    if tok.kind == "workflow":
        if str(payload.get("mode") or "").lower() == "async":
            return await _invoke_workflow_async(db, tok, payload)
        return await _invoke_workflow(db, tok, payload)
    return await _invoke_scheduled_task(db, tok, payload)


def _rate_limited(limit: int):
    from app.core.errors import AppError

    err = AppError(f"调用过于频繁（上限 {limit} 次/分钟）", code="rate_limited")
    err.status_code = 429
    return err


async def _invoke_workflow(db: AsyncSession, tok: WebhookToken, payload: dict) -> dict:
    """同步执行工作流并返回结果。

    载荷约定（对接方友好）：
    - 若载荷带 `message` 字段，用它作为主输入（客服系统的自然语言消息）
    - 其余键同时注入为输入变量，供 start 声明的同名输入直接取值
    """
    from app.agents.workflow.engine import NodeContext, WorkflowPaused, execute_graph
    from app.agents.workflow.runner import start_input_name
    from app.middleware.auth_dep import get_user_permission_codes, load_principal_set
    from app.models import WorkflowRun

    if not tok.agent_id:
        raise ValidationError("该接入点未绑定工作流")
    agent = await db.get(Agent, tok.agent_id)
    if not agent or agent.tenant_id != tok.tenant_id:
        raise NotFoundError("目标智能体不存在")
    if agent.status == "disabled":
        raise ValidationError("目标智能体已禁用")

    wf = (
        await db.execute(
            select(Workflow).where(Workflow.agent_id == agent.id).order_by(Workflow.id.desc())
        )
    ).scalars().first()
    if not wf or not wf.graph or not (wf.graph.get("nodes")):
        raise ValidationError("目标工作流未配置")

    # 输入：start 声明的第一个名字取 message（或同名键），其余载荷键都可直接引用
    primary = str(payload.get("message") or payload.get("query") or "")
    inputs: dict = dict(payload)
    inputs[start_input_name(wf.graph)] = primary or inputs.get(start_input_name(wf.graph), "")

    # 以令牌创建者身份执行（不放大权限）
    owner = await db.get(User, tok.created_by) if tok.created_by else None
    if owner is not None:
        ps = await load_principal_set(db, owner)
        perms = await get_user_permission_codes(db, owner)
    else:
        from app.services.permission import PrincipalSet

        ps = PrincipalSet(user_id=0, tenant_id=tok.tenant_id)
        perms = set()

    run = WorkflowRun(
        tenant_id=tok.tenant_id, workflow_id=wf.id, agent_id=agent.id,
        user_id=owner.id if owner else None, status="running",
        input=inputs, graph_snapshot=wf.graph, created_at=int(time.time() * 1000),
    )
    db.add(run)
    await db.commit()
    run_id = run.id

    ctx = NodeContext(
        db=db, ps=ps, run_id=run_id, tenant_id=tok.tenant_id, inputs=inputs,
        default_model_config_id=agent.model_config_id, default_kb_ids=agent.kb_ids,
        default_system=agent.system_prompt,
        default_temperature=(agent.config or {}).get("temperature"), perms=perms,
    )

    t0 = time.time()
    err: str | None = None
    paused = None
    final: dict = {}
    try:
        final, _ = await execute_graph(wf.graph, inputs, ctx)
    except WorkflowPaused as p:
        paused = {"node_id": p.node_id, **(p.info or {})}
    except Exception as e:  # noqa: BLE001
        err = str(e)[:300]

    # 落运行终态（保持与其它入口一致的轨迹可查）
    try:
        r2 = await db.get(WorkflowRun, run_id)
        if r2 is not None:
            r2.status = "waiting" if paused else ("failed" if err else "success")
            if paused:
                r2.pending_node_id = paused.get("node_id")
            elif err:
                r2.error_msg = err
            else:
                r2.output = final
            if ctx.usage and (ctx.usage.get("calls") or 0) > 0:
                r2.usage = dict(ctx.usage)
            r2.latency_ms = int((time.time() - t0) * 1000)
            r2.finished_at = int(time.time() * 1000)
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()
        logger.exception("webhook_workflow_finalize_failed", run_id=run_id)

    if paused:
        return {"ok": False, "status": "waiting", "run_id": run_id,
                "message": "流程需要人工审批，请稍后在后台处理", "pending": paused}
    if err:
        return {"ok": False, "status": "failed", "run_id": run_id, "error": err}

    text = str(final.get("output", final)) if isinstance(final, dict) else str(final)
    return {
        "ok": True, "status": "success", "run_id": run_id,
        "text": text, "output": final,
        "latency_ms": int((time.time() - t0) * 1000),
    }


async def _invoke_workflow_async(db: AsyncSession, tok: WebhookToken, payload: dict) -> dict:
    """异步模式：立即返回 run_id，工作流在后台跑，完成后回调。

    为什么要这个模式：含 LLM / 外部 API 的长流程可能超过对接方的 HTTP 超时
    （很多平台 5-10s 就断）。异步 + 回调是这类系统的标准做法。
    """
    from app.models import WorkflowRun
    from app.tasks.bg import spawn

    # 先做与同步路径相同的校验，避免"返回了 run_id 但其实跑不起来"
    if not tok.agent_id:
        raise ValidationError("该接入点未绑定工作流")
    agent = await db.get(Agent, tok.agent_id)
    if not agent or agent.tenant_id != tok.tenant_id:
        raise NotFoundError("目标智能体不存在")
    if not tok.callback_url:
        raise ValidationError(
            "该接入点未配置回调地址，无法使用异步模式（请在接入点设置 callback_url）"
        )

    wf = (
        await db.execute(
            select(Workflow).where(Workflow.agent_id == agent.id).order_by(Workflow.id.desc())
        )
    ).scalars().first()
    if not wf or not wf.graph or not (wf.graph.get("nodes")):
        raise ValidationError("目标工作流未配置")

    primary = str(payload.get("message") or payload.get("query") or "")
    inputs: dict = {k: v for k, v in payload.items() if k != "mode"}
    from app.agents.workflow.runner import start_input_name

    inputs[start_input_name(wf.graph)] = primary or inputs.get(start_input_name(wf.graph), "")

    run = WorkflowRun(
        tenant_id=tok.tenant_id, workflow_id=wf.id, agent_id=agent.id,
        user_id=tok.created_by, status="running",
        input=inputs, graph_snapshot=wf.graph, created_at=int(time.time() * 1000),
    )
    db.add(run)
    await db.commit()
    run_id = run.id

    spawn("hook_workflow_run", _run_and_callback(
        run_id=run_id, token_id=tok.id, agent_id=agent.id, wf_id=wf.id,
        tenant_id=tok.tenant_id, inputs=inputs,
    ))
    return {
        "ok": True, "status": "accepted", "run_id": run_id,
        "message": "已受理，完成后会回调你配置的地址",
        "callback_url": tok.callback_url,
    }


async def _run_and_callback(*, run_id: int, token_id: int, agent_id: int, wf_id: int,
                            tenant_id: int, inputs: dict) -> None:
    """后台执行工作流，完成后 POST 回调（带 HMAC 签名）。"""
    import hashlib
    import hmac
    import json as _json

    import httpx

    from app.agents.tools.builtin import _check_url
    from app.agents.workflow.engine import NodeContext, WorkflowPaused, execute_graph
    from app.core.crypto import decrypt
    from app.core.db import AsyncSessionLocal
    from app.middleware.auth_dep import get_user_permission_codes, load_principal_set
    from app.models import Agent, User, WebhookToken, Workflow, WorkflowRun

    result: dict = {"run_id": run_id, "ok": False, "status": "failed", "error": "未执行"}
    t0 = time.time()

    async with AsyncSessionLocal() as db:
        try:
            tok = await db.get(WebhookToken, token_id)
            agent = await db.get(Agent, agent_id)
            wf = await db.get(Workflow, wf_id)
            if not tok or not agent or not wf:
                result = {"run_id": run_id, "ok": False, "status": "failed", "error": "接入点或工作流已删除"}
                raise RuntimeError("资源不存在")

            owner = await db.get(User, tok.created_by) if tok.created_by else None
            if owner is not None:
                ps = await load_principal_set(db, owner)
                perms = await get_user_permission_codes(db, owner)
            else:
                from app.services.permission import PrincipalSet

                ps = PrincipalSet(user_id=0, tenant_id=tenant_id)
                perms = set()

            ctx = NodeContext(
                db=db, ps=ps, run_id=run_id, tenant_id=tenant_id, inputs=inputs,
                default_model_config_id=agent.model_config_id, default_kb_ids=agent.kb_ids,
                default_system=agent.system_prompt,
                default_temperature=(agent.config or {}).get("temperature"), perms=perms,
            )

            err = None
            paused = None
            final: dict = {}
            try:
                final, _ = await execute_graph(wf.graph, inputs, ctx)
            except WorkflowPaused as p:
                paused = {"node_id": p.node_id, **(p.info or {})}
            except Exception as e:  # noqa: BLE001
                err = str(e)[:300]

            if paused:
                result = {"run_id": run_id, "ok": False, "status": "waiting", "pending": paused}
            elif err:
                result = {"run_id": run_id, "ok": False, "status": "failed", "error": err}
            else:
                text = str(final.get("output", final)) if isinstance(final, dict) else str(final)
                result = {"run_id": run_id, "ok": True, "status": "success",
                          "text": text, "output": final}

            # 落终态
            r2 = await db.get(WorkflowRun, run_id)
            if r2 is not None:
                r2.status = result["status"]
                if paused:
                    r2.pending_node_id = paused.get("node_id")
                    r2.state = {"variables": {}}
                elif err:
                    r2.error_msg = err
                else:
                    r2.output = final
                if ctx.usage and (ctx.usage.get("calls") or 0) > 0:
                    r2.usage = dict(ctx.usage)
                r2.latency_ms = int((time.time() - t0) * 1000)
                r2.finished_at = int(time.time() * 1000)
            await db.commit()

            # 回调：登记投递记录后立即尝试一次，失败由后台任务按退避重试
            cb = tok.callback_url
            if cb:
                if _check_callback_url(cb):
                    logger.warning("hook_callback_blocked", run_id=run_id, url=cb[:80])
                else:
                    from app.services import workflow_callback_service as CB

                    await CB.enqueue(
                        db, run_id=run_id, tenant_id=tenant_id, url=cb,
                        secret_enc=tok.callback_secret, payload=result,
                    )
                    await db.commit()
                    # 立即尝试一次，避免常规场景也要等轮询
                    await CB.flush_ready(limit=1)
        except Exception:  # noqa: BLE001
            logger.exception("hook_async_run_failed", run_id=run_id)
            await db.rollback()
            # 执行阶段就崩了：尽力把失败也回调出去，避免对接方一直等
            try:
                tok2 = await db.get(WebhookToken, token_id)
                if tok2 and tok2.callback_url and not _check_callback_url(tok2.callback_url):
                    from app.services import workflow_callback_service as CB2

                    await CB2.enqueue(
                        db, run_id=run_id, tenant_id=tenant_id, url=tok2.callback_url,
                        secret_enc=tok2.callback_secret,
                        payload={"run_id": run_id, "ok": False, "status": "failed",
                                 "error": "服务端执行异常"},
                    )
                    await db.commit()
            except Exception:  # noqa: BLE001
                pass


async def _invoke_scheduled_task(db: AsyncSession, tok: WebhookToken, payload: dict) -> dict:
    import asyncio

    from app.tasks.scheduler_tasks import execute_task

    if not tok.task_id:
        raise ValidationError("该接入点未绑定定时任务")
    t = await db.get(ScheduledTask, tok.task_id)
    if not t or not t.enabled:
        raise NotFoundError("目标任务不存在或已禁用")

    if payload:
        t.inputs = {**(t.inputs or {}), "_webhook": payload}
        await db.flush()

    from app.tasks.bg import spawn
    spawn("webhook_run", execute_task(t.id, manual=True))
    return {"message": "已触发", "task_id": t.id}
