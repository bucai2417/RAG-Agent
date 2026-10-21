"""工作流结果回推渠道：把后台事件（审批决定、异步完成）推回原客户。

为什么需要独立服务：工作流可能由后台客服审批触发，此时**没有入站的 InboundMessage**
可供 in dispatcher 复用（`_reply` 依赖它拿 external_user/group/account）。本服务
从 WorkflowRun.conversation_id 反查身份，自己构造 OutboundMessage。

设计要点（勿凭直觉改动）：

1. **群聊/私聊必须分清**。ChannelUser 只存 external_id，不存"这条消息来自群还是私聊"。
   可靠的做法是查该会话最近一条入站消息的 meta——但 Message 表没有存群 id。
   因此约定：私聊优先（多数客服场景）；需要群回发时由调用方显式传 group_id。
   群场景的审批目前由工单承担，客户在群里能看到客服回复。

2. **渠道 adapter 可能没连上**（服务重启中、token 过期）。此时不抛错、只记日志，
   因为审批动作本身已经成功落库 —— 回推失败不该让审批接口报错。

3. **发送失败不丢消息**：把待发内容写回 WorkflowRun.output 的 `channel_reply` 字段，
   便于后台排查与手动补发（见 `resend_workflow_result`）。
"""
from __future__ import annotations

from app.core.logging import get_logger

logger = get_logger("workflow_notify")


async def _resolve_target(db, run) -> dict | None:
    """按 WorkflowRun 反查渠道身份。返回 {channel_id, channel_kind, external_user, tenant_id}。

    优先用运行上记录的来源快照（可靠）；没有则回退到 ChannelUser 反查
    （老数据 / 非渠道入口创建的运行，conversation_id 可能为空，反查会失败）。
    """
    from sqlalchemy import select

    from app.models import Channel, ChannelUser

    # 1) 快照优先：创建运行时已记下「发给谁、发到哪」
    snap_user = getattr(run, "origin_external_user", None)
    snap_kind = getattr(run, "origin_channel_kind", None)
    if snap_user and snap_kind:
        return {
            "channel_id": getattr(run, "origin_channel_id", None),
            "channel_kind": snap_kind,
            "external_user": snap_user,
            "external_group": getattr(run, "origin_external_group", None),
            "tenant_id": run.tenant_id,
        }

    # 2) 回退：按会话反查（仅老数据会走到这里）
    if not run.conversation_id:
        return None

    cu = (
        await db.execute(
            select(ChannelUser).where(
                ChannelUser.conversation_id == run.conversation_id,
                ChannelUser.tenant_id == run.tenant_id,
            )
        )
    ).scalars().first()
    if not cu:
        return None

    # ChannelUser.channel 是 kind（字符串），不是 channel_id；取该租户下匹配 kind 的启用渠道
    ch = (
        await db.execute(
            select(Channel).where(
                Channel.tenant_id == run.tenant_id,
                Channel.kind == cu.channel,
                Channel.status == "active",
            ).order_by(Channel.id.desc())
        )
    ).scalars().first()

    return {
        "channel_id": ch.id if ch else None,
        "channel_kind": cu.channel,
        "external_user": cu.external_id,
        "external_group": None,
        "tenant_id": run.tenant_id,
    }


async def push_workflow_result(db, *, run, text: str, prefix: str = "") -> bool:
    """把工作流输出推回发起该运行的客户。返回是否发送成功。

    prefix：给消息加前缀（如「您的申请已通过：」），调用方按业务语境决定。
    """
    from app.channels.base import OutboundMessage
    from app.channels.manager import channel_manager

    if not (text or "").strip():
        return False

    target = await _resolve_target(db, run)
    if not target:
        logger.info("workflow_notify_no_target", run_id=run.id)
        return False

    content = f"{prefix}{text}".strip()
    grp = target.get("external_group")
    out = OutboundMessage(
        content=content,
        # 群聊回群：此时 user_id 必须为空，否则部分渠道会当成私聊发给群 id
        user_id=None if grp else target["external_user"],
        group_id=grp,
        account=None,
    )

    ok = False
    try:
        if target["channel_id"]:
            ok = await channel_manager.send(target["channel_id"], out)
        if not ok and target["channel_kind"]:
            # 渠道被重建/删除后 id 变化 → 按 kind 找当前运行中的渠道兜底
            ok = await channel_manager.send_with_fallback(
                target["channel_id"] or 0, target["channel_kind"], out,
            )
    except Exception:  # noqa: BLE001
        logger.exception("workflow_notify_send_failed", run_id=run.id)

    # 成败都记到 run.output，便于后台看到"结果有没有送达"
    try:
        out_map = dict(run.output or {})
        out_map["channel_reply"] = {"sent": bool(ok), "text": content[:2000]}
        run.output = out_map
        await db.flush()
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()
        logger.exception("workflow_notify_record_failed", run_id=run.id)

    if ok:
        logger.info("workflow_notify_sent", run_id=run.id, kind=target["channel_kind"])
    else:
        logger.warning("workflow_notify_unsent", run_id=run.id, kind=target["channel_kind"])
    return ok


async def notify_approval_decision(db, *, run, decision: str, result_text: str | None = None) -> bool:
    """审批结束后通知客户（通过/拒绝）。仅对渠道发起的运行生效。

    - 通过：发工作流最终输出（result_text），前缀「已处理完成：」
    - 拒绝：发固定文案「您的请求未通过审批，请联系客服」
    """
    if decision == "reject":
        return await push_workflow_result(
            db, run=run, text="您的请求未通过审批，如需帮助请联系客服。",
        )
    text = (result_text or "").strip()
    if not text:
        return await push_workflow_result(db, run=run, text="已处理完成。")
    return await push_workflow_result(db, run=run, text=text, prefix="已处理完成：\n")


async def resend_workflow_result(db, *, run) -> bool:
    """手动补发：把上次未送达的结果再推一次（后台「重发」按钮用）。"""
    cached = (run.output or {}).get("channel_reply") or {}
    text = str(cached.get("text") or "").strip()
    if not text:
        return False
    return await push_workflow_result(db, run=run, text=text)
