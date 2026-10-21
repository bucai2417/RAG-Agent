"""外部 IM 渠道管理接口 + 入站回调端点。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.errors import NotFoundError, PermissionDeniedError, ValidationError
from app.middleware.auth_dep import require_permission
from app.models import Channel, User
from app.services.audit_service import audited
from app.services.channel_service import (
    decrypt_config, encrypt_config, get_channel, mask_config, secret_meta,
)

router = APIRouter(prefix="/channels", tags=["channel"])

SUPPORTED_KINDS = ("qqbot", "wxclaw", "wework", "feishu")


class ChannelIn(BaseModel):
    kind: str
    name: str
    config: dict | None = None
    enabled: bool = True
    default_tenant_id: int | None = None
    default_kb_ids: list[int] | None = None
    # 以下枚举允许传 null/空串表示「不选」→ 归一为默认值（见 _norm_choice）
    kb_mode: str | None = None  # auto | custom | off
    service_mode: str | None = None  # qa=只答 | support=客服（可转人工）
    default_lang: str | None = None  # 默认回答语言（zh/en/ja/...，空=自动）
    default_agent_id: int | None = None
    reply_mode: str | None = None  # rag | smart | agent | workflow
    progress_hint: bool | None = None  # 工作流型智能体的工作中提示
    group_progress_mode: str | None = None  # off | first | throttle
    group_progress_interval: int | None = None
    command_prefix: str | None = None
    greeting: str | None = None
    # 渠道附件入库
    attach_ingest: str | None = None  # off | bound | all
    attach_kb_ids: list[int] | None = None
    attach_max_mb: int | None = None
    attach_types: str | None = None  # 逗号分隔扩展名白名单


def _norm_choice(v: str | None, allowed: tuple[str, ...], default: str) -> str:
    """归一枚举：空/空串/非法值 → 默认值（允许前端“不选”）。"""
    s = (v or "").strip()
    return s if s in allowed else default


def _to_out(c: Channel) -> dict:
    return {
        "id": c.id, "kind": c.kind, "name": c.name,
        "config": mask_config(c.config),
        # 敏感字段元信息（是否已设置 + 明文真实长度，不含明文）
        "secret_meta": secret_meta(c.config),
        "enabled": c.enabled, "connected": c.connected, "last_error": c.last_error,
        "default_tenant_id": c.default_tenant_id, "default_kb_ids": c.default_kb_ids or [],
        "kb_mode": c.kb_mode or "auto",
        "service_mode": getattr(c, "service_mode", "qa") or "qa",
        "default_lang": getattr(c, "default_lang", None),
        "default_agent_id": c.default_agent_id, "reply_mode": c.reply_mode,
        "progress_hint": bool(getattr(c, "progress_hint", False)),
        "group_progress_mode": getattr(c, "group_progress_mode", "off") or "off",
        "group_progress_interval": int(getattr(c, "group_progress_interval", 15) or 15),
        "command_prefix": c.command_prefix, "greeting": c.greeting,
        "attach_ingest": getattr(c, "attach_ingest", "off") or "off",
        "attach_kb_ids": getattr(c, "attach_kb_ids", None) or [],
        "attach_max_mb": getattr(c, "attach_max_mb", 20) or 20,
        "attach_types": getattr(c, "attach_types", None),
        "callback_url": f"/api/v1/channels/callback/{c.id}",
    }


@router.get("")
async def list_channels(
    user: User = Depends(require_permission("channel:read")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    rows = (await db.execute(
        select(Channel).where(Channel.tenant_id == user.tenant_id, Channel.status == "active")
        .order_by(Channel.id.desc())
    )).scalars().all()
    return [_to_out(c) for c in rows]


@router.get("/{channel_id}/secrets")
async def reveal_secrets(
    channel_id: int,
    user: User = Depends(require_permission("channel:manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """按需读取某个渠道的明文敏感配置（token/secret）。仅 channel:manage 可用，且写审计日志。

    列表/详情接口只回掩码值 + 长度，避免明文常驻前端。点“查看”时才调这里，拿到明文后仅存内存（不写 localStorage）。
    """
    from app.services.audit_service import record_audit

    ch = await get_channel(db, channel_id, user.tenant_id)
    plain = decrypt_config(ch.config)
    secret_keys = ("secret", "token", "password", "encoding_aes_key", "client_secret")
    keys = []
    for k in (ch.config or {}).keys():
        if any(s in k.lower() for s in secret_keys):
            keys.append(k)
    out = {k: str(plain.get(k) or "") for k in keys}
    record_audit(
        db, action="channel.reveal_secret", resource_type="channel", resource_id=ch.id,
        tenant_id=user.tenant_id, actor_id=user.id, actor_name=user.username,
        result="success", after={"fields": keys},
    )
    await db.flush()
    return {"secrets": out}


@router.post("")
@audited("channel.create", "channel")
async def create_channel(
    body: ChannelIn,
    user: User = Depends(require_permission("channel:manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if body.kind not in SUPPORTED_KINDS:
        raise ValidationError(f"不支持的渠道类型: {body.kind}")
    c = Channel(
        tenant_id=user.tenant_id, kind=body.kind, name=body.name,
        config=encrypt_config(body.config), enabled=body.enabled,
        default_tenant_id=body.default_tenant_id or user.tenant_id,
        default_kb_ids=body.default_kb_ids,
        kb_mode=_norm_choice(body.kb_mode, ("auto", "custom", "off"), "auto"),
        service_mode=_norm_choice(body.service_mode, ("qa", "support"), "qa"),
        default_lang=(body.default_lang or "").strip() or None,
        default_agent_id=body.default_agent_id,
        reply_mode=_norm_choice(body.reply_mode, ("rag", "smart", "agent", "workflow"), "rag"),
        progress_hint=bool(body.progress_hint),
        group_progress_mode=_norm_choice(body.group_progress_mode, ("off", "first", "throttle"), "off"),
        group_progress_interval=max(1, int(body.group_progress_interval or 15)),
        command_prefix=(body.command_prefix or "").strip() or "/",
        greeting=(body.greeting or "").strip() or None,
        attach_ingest=_norm_choice(body.attach_ingest, ("off", "bound", "all"), "off"),
        attach_kb_ids=body.attach_kb_ids,
        attach_max_mb=int(body.attach_max_mb) if body.attach_max_mb else 20,
        attach_types=(body.attach_types or "").strip() or None,
    )
    db.add(c)
    await db.flush()
    await db.commit()
    if c.enabled:
        from app.channels.manager import channel_manager

        await channel_manager.restart(c.id)
    return _to_out(c)


@router.patch("/{channel_id}")
@audited("channel.update", "channel", id_arg="channel_id")
async def update_channel(
    channel_id: int,
    body: ChannelIn,
    user: User = Depends(require_permission("channel:manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    c = await get_channel(db, channel_id, user.tenant_id)
    c.kind = body.kind
    c.name = body.name
    c.config = encrypt_config(body.config, existing=c.config)
    c.enabled = body.enabled
    c.default_tenant_id = body.default_tenant_id or user.tenant_id
    c.default_kb_ids = body.default_kb_ids
    c.kb_mode = _norm_choice(body.kb_mode, ("auto", "custom", "off"), "auto")
    c.service_mode = _norm_choice(body.service_mode, ("qa", "support"), "qa")
    c.default_lang = (body.default_lang or "").strip() or None
    c.default_agent_id = body.default_agent_id
    c.reply_mode = _norm_choice(body.reply_mode, ("rag", "smart", "agent", "workflow"), "rag")
    if body.progress_hint is not None:
        c.progress_hint = bool(body.progress_hint)
    if body.group_progress_mode is not None:
        c.group_progress_mode = _norm_choice(body.group_progress_mode, ("off", "first", "throttle"), "off")
    if body.group_progress_interval is not None:
        c.group_progress_interval = max(1, int(body.group_progress_interval))
    c.command_prefix = (body.command_prefix or "").strip() or "/"
    c.greeting = (body.greeting or "").strip() or None
    c.attach_ingest = _norm_choice(body.attach_ingest, ("off", "bound", "all"), "off")
    c.attach_kb_ids = body.attach_kb_ids
    c.attach_max_mb = int(body.attach_max_mb) if body.attach_max_mb else 20
    c.attach_types = (body.attach_types or "").strip() or None
    await db.flush()
    await db.commit()
    from app.channels.manager import channel_manager

    if c.enabled:
        await channel_manager.restart(c.id)
    else:
        await channel_manager._stop_one(c.id)
        c.connected = False
        await db.commit()
    return _to_out(c)


@router.delete("/{channel_id}")
@audited("channel.delete", "channel", id_arg="channel_id")
async def delete_channel(
    channel_id: int,
    user: User = Depends(require_permission("channel:manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    c = await get_channel(db, channel_id, user.tenant_id)
    c.status = "deleted"
    c.enabled = False
    await db.commit()
    from app.channels.manager import channel_manager

    await channel_manager._stop_one(c.id)
    return {"message": "已删除"}


@router.post("/{channel_id}/test")
async def test_channel(
    channel_id: int,
    user: User = Depends(require_permission("channel:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """连接测试：用已保存配置构建适配器并调用 health()。"""
    from app.channels.registry import build_adapter

    c = await get_channel(db, channel_id, user.tenant_id)
    try:
        adapter = build_adapter(c.kind, config=decrypt_config(c.config), on_message=None)
        h = await adapter.health()
        return {"ok": h.ok, "message": h.message, "latency_ms": h.latency_ms}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)[:300], "latency_ms": 0}


# ---- 微信（wxclaw）扫码登录 ----
# 说明：get_qrcode_status 是长轮询接口（有 ticket 时服务端挂住，直到状态变化或自身超时）。
# 若由前端每次轮询直接调，会请求堆积且不稳定。改为：后端起一个后台轮询任务持续查询，
# 结果写入内存态；前端只读缓存（秒回）。命中 token 后停止轮询。
_WX_SCAN_TASKS: dict[str, asyncio.Task] = {}   # session/channel key → 后台轮询任务

# 无渠道依赖的扫码会话（新建渠道时用）：session → state
_WX_SCAN_SESSIONS: dict[str, dict] = {}
# 已存渠道的扫码态：channel_id → state
_WX_LOGIN_STATE: dict[int, dict] = {}


async def _wxclaw_poll_loop(key: str, state: dict) -> None:
    """后台长轮询 get_qrcode_status，直到拿到 token 或会话失效。结果写回 state。"""
    from app.channels import wxclaw_login

    while True:
        ticket = state.get("ticket", "")
        if not ticket or state.get("status") == "logged_in":
            return
        try:
            parsed = await wxclaw_login.check_login_status(ticket)
        except Exception:  # noqa: BLE001
            parsed = {}
        if parsed.get("token"):
            state["token"] = parsed["token"]
            state["bot_id"] = parsed.get("bot_id", "")
            state["status"] = "logged_in"
            return
        st = parsed.get("status") or ""
        if st == "expired":
            try:
                p2 = await wxclaw_login.fetch_login_qr()
                qd = await wxclaw_login.build_display_qr(p2.get("qr") or p2.get("ticket"))
                state.update({"ticket": p2.get("ticket", ""), "qr": p2.get("qr", ""),
                              "qr_display": qd, "status": "wait"})
            except Exception:  # noqa: BLE001
                state["status"] = "wait"
        elif st:
            state["status"] = st
        # 空 st（长轮询超时/无变化）→ 保持当前状态，继续轮询
        await asyncio.sleep(0.5)


def _start_wx_poll(key: str, state: dict) -> None:
    old = _WX_SCAN_TASKS.get(key)
    if old and not old.done():
        old.cancel()
    _WX_SCAN_TASKS[key] = asyncio.create_task(_wxclaw_poll_loop(key, state))


@router.post("/wxclaw/scan")
async def wxclaw_scan(
    user: User = Depends(require_permission("channel:manage")),
) -> dict:
    """生成微信扫码二维码（不依赖已存在的渠道，供"新建渠道"表单用）。

    返回 session（供轮询）。后端起后台轮询任务，前端只读状态。
    """
    import secrets

    from app.channels import wxclaw_login

    try:
        parsed = await wxclaw_login.fetch_login_qr()
    except Exception as e:  # noqa: BLE001
        raise ValidationError(f"获取二维码失败：{str(e)[:300]}") from e
    qr_display = await wxclaw_login.build_display_qr(parsed.get("qr") or parsed.get("ticket"))
    session = secrets.token_urlsafe(16)
    state = {
        "ticket": parsed.get("ticket", ""), "qr": parsed.get("qr", ""),
        "qr_display": qr_display, "status": parsed.get("status") or "wait", "token": "",
    }
    _WX_SCAN_SESSIONS[session] = state
    _start_wx_poll(session, state)
    return {
        "session": session, "logged_in": bool(parsed.get("token")),
        "qr": parsed.get("qr", ""), "qr_display": qr_display,
        "ticket": parsed.get("ticket", ""), "status": state["status"],
    }


@router.get("/wxclaw/scan-status")
async def wxclaw_scan_status(
    session: str,
    user: User = Depends(require_permission("channel:manage")),
) -> dict:
    """读扫码会话状态（秒回，后台任务在轮询上游）。"""
    st = _WX_SCAN_SESSIONS.get(session)
    if not st:
        return {"logged_in": False, "status": "idle", "token": "", "qr_display": ""}
    return {
        "logged_in": st.get("status") == "logged_in" and bool(st.get("token")),
        "status": st.get("status", "wait"), "token": st.get("token", ""),
        "bot_id": st.get("bot_id", ""), "qr_display": st.get("qr_display", ""),
    }



@router.post("/{channel_id}/wxclaw/scan-login")
@audited("channel.wxclaw_scan", "channel", id_arg="channel_id")
async def wxclaw_scan_login(
    channel_id: int,
    user: User = Depends(require_permission("channel:manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """生成微信扫码二维码（固定地址 ilinkai.weixin.qq.com）。"""
    from app.channels import wxclaw_login

    c = await get_channel(db, channel_id, user.tenant_id)
    if c.kind != "wxclaw":
        raise ValidationError("该渠道不是微信渠道")
    try:
        parsed = await wxclaw_login.fetch_login_qr()
    except Exception as e:  # noqa: BLE001
        raise ValidationError(f"获取二维码失败：{str(e)[:300]}") from e
    qr_display = await wxclaw_login.build_display_qr(parsed.get("qr") or parsed.get("ticket"))
    state = {
        "ticket": parsed.get("ticket", ""), "qr": parsed.get("qr", ""),
        "qr_display": qr_display, "status": parsed.get("status") or "wait", "token": "",
    }
    _WX_LOGIN_STATE[channel_id] = state
    _start_wx_poll(f"ch_{channel_id}", state)
    return {
        "ok": True,
        "logged_in": bool(parsed.get("token")),
        "qr": parsed.get("qr", ""), "qr_display": qr_display,
        "ticket": parsed.get("ticket", ""), "status": state["status"],
    }


@router.get("/{channel_id}/wxclaw/login-status")
async def wxclaw_login_status(
    channel_id: int,
    user: User = Depends(require_permission("channel:manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """读扫码状态（秒回）；后台任务命中 token 后写回渠道配置并触发重连。"""
    c = await get_channel(db, channel_id, user.tenant_id)
    state = _WX_LOGIN_STATE.get(channel_id) or {}

    # 已登录：config 里已有 token
    cfg = decrypt_config(c.config)
    if cfg.get("token"):
        return {"logged_in": True, "status": "logged_in",
                "qr_display": state.get("qr_display", ""), "ticket": state.get("ticket", "")}

    if state.get("status") == "logged_in" and state.get("token"):
        # 后台已拿到 token：落库 + 重连
        from app.channels import wxclaw_login

        new_cfg = wxclaw_login.upsert_account(
            dict(cfg), token=state["token"], api_url=wxclaw_login.FIXED_API_URL,
            bot_id=state.get("bot_id", ""), account_name=state.get("bot_id", "") or "",
        )
        c.config = encrypt_config(new_cfg, existing=c.config)
        await db.flush()
        await db.commit()
        from app.channels.manager import channel_manager

        await channel_manager.restart(channel_id)
        return {"logged_in": True, "status": "logged_in",
                "qr_display": state.get("qr_display", ""), "ticket": state.get("ticket", "")}

    return {"logged_in": False, "status": state.get("status", "wait") if state else "idle",
            "qr_display": state.get("qr_display", ""), "ticket": state.get("ticket", "")}


@router.get("/{channel_id}/users")
async def list_channel_users(
    channel_id: int,
    user: User = Depends(require_permission("channel:read")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    from app.models import ChannelUser

    c = await get_channel(db, channel_id, user.tenant_id)
    rows = (await db.execute(
        select(ChannelUser).where(ChannelUser.channel == c.kind, ChannelUser.tenant_id == c.tenant_id)
        .order_by(ChannelUser.id.desc()).limit(200)
    )).scalars().all()
    bound_ids = [r.bound_user_id for r in rows if r.bound_user_id]
    names: dict[int, str] = {}
    if bound_ids:
        for u in (await db.execute(select(User).where(User.id.in_(bound_ids)))).scalars().all():
            names[u.id] = u.display_name or u.username
    return [
        {"id": r.id, "external_id": r.external_id, "display_name": r.display_name,
         "user_id": r.user_id, "agent_id": r.agent_id, "created_at": r.created_at,
         "bound_user_id": r.bound_user_id, "bound_user_name": names.get(r.bound_user_id or 0),
         "bind_code": r.bind_code}
        for r in rows
    ]


class BindIn(BaseModel):
    user_id: int | None = None
    bind_code: str | None = None


class BindByCodeIn(BaseModel):
    bind_code: str
    user_id: int


@router.post("/users/bind-by-code")
@audited("channel.user_bind", "channel")
async def bind_by_code(
    body: BindByCodeIn,
    user: User = Depends(require_permission("channel:manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """按「一次性绑定码」把渠道身份绑定到内部账号（无需知道渠道/身份 id）。

    用户先在渠道里发送 /bind 取得 6 位码，管理员在此输入码 + 选择内部账号即可。
    """
    from app.core.errors import ValidationError
    from app.models import ChannelUser

    code = (body.bind_code or "").strip().upper()
    if not code:
        raise ValidationError("请输入绑定码")
    cu = (await db.execute(
        select(ChannelUser).where(
            ChannelUser.bind_code == code, ChannelUser.tenant_id == user.tenant_id
        )
    )).scalars().first()
    if not cu:
        raise NotFoundError("绑定码无效或已作废（请让用户在渠道重新发送 /bind 获取）")
    target = await db.get(User, body.user_id)
    if not target or target.tenant_id != user.tenant_id:
        raise NotFoundError("目标账号不存在")
    if getattr(target, "user_type", "internal") == "external":
        raise ValidationError("只能绑定到内部账号，不能绑定到外部客户")
    cu.bound_user_id = target.id
    cu.bind_code = None
    await db.flush()
    await db.commit()
    return {
        "message": f"已把 {cu.display_name or cu.external_id} 绑定到「{target.display_name or target.username}」",
        "bound_user_id": target.id, "channel_user_id": cu.id,
        "external_id": cu.external_id, "channel": cu.channel,
    }


@router.post("/{channel_id}/users/{cu_id}/bind")
@audited("channel.user_bind", "channel", id_arg="channel_id")
async def bind_channel_user(
    channel_id: int,
    cu_id: int,
    body: BindIn,
    user: User = Depends(require_permission("channel:manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """把渠道身份绑定到内部账号（继承其权限）。支持按 user_id 或 bind_code。"""
    from app.core.errors import ValidationError
    from app.models import ChannelUser

    c = await get_channel(db, channel_id, user.tenant_id)
    cu = await db.get(ChannelUser, cu_id)
    if not cu or cu.tenant_id != c.tenant_id:
        raise NotFoundError("渠道用户不存在")

    # 按绑定码定位身份（若提供）
    if body.bind_code:
        cu2 = (await db.execute(
            select(ChannelUser).where(ChannelUser.bind_code == body.bind_code.strip().upper(),
                                      ChannelUser.tenant_id == c.tenant_id)
        )).scalars().first()
        if not cu2:
            raise NotFoundError("绑定码无效或已过期")
        cu = cu2

    if not body.user_id:
        raise NotFoundError("请选择要绑定的内部账号")
    target = await db.get(User, body.user_id)
    if not target or target.tenant_id != user.tenant_id:
        raise NotFoundError("目标账号不存在")
    if getattr(target, "user_type", "internal") == "external":
        raise ValidationError("只能绑定到内部账号，不能绑定到外部客户")

    cu.bound_user_id = target.id
    cu.bind_code = None
    await db.flush()
    await db.commit()
    return {"message": f"已绑定到「{target.display_name or target.username}」", "bound_user_id": target.id}


@router.delete("/{channel_id}/users/{cu_id}/bind")
@audited("channel.user_unbind", "channel", id_arg="channel_id")
async def unbind_channel_user(
    channel_id: int,
    cu_id: int,
    user: User = Depends(require_permission("channel:manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """解绑渠道身份（恢复为外部客户只读）。"""
    from app.models import ChannelUser

    c = await get_channel(db, channel_id, user.tenant_id)
    cu = await db.get(ChannelUser, cu_id)
    if not cu or cu.tenant_id != c.tenant_id:
        raise NotFoundError("渠道用户不存在")
    cu.bound_user_id = None
    cu.bind_code = None
    await db.flush()
    await db.commit()
    return {"message": "已解绑"}


# ---- 公开入站回调（无登录；按渠道自验签）----
@router.post("/callback/{channel_id}")
async def channel_callback(
    channel_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """企微/QQBot 的 URL 回调入口。当前用于"URL 验证 + 事件接收"。

    WS 长连接渠道（qqbot/wework/feishu/wxclaw）不需要此端点；
    本端点供用户选择"回调模式"时使用，需要自行配置平台的签名校验。
    """
    c = await db.get(Channel, channel_id)
    if not c or c.status != "active":
        raise NotFoundError("渠道不存在")
    body = await request.json()

    # QQBot 回调：op=13 是 URL 校验（需用 Ed25519 签名回包）
    if c.kind == "qqbot" and body.get("op") == 13:
        # 简化：返回 {"plain_token":..., "signature":...} 由前端/运维侧配合；此处直接透传 d
        return {"plain_token": body.get("d", {}).get("plain_token", ""), "signature": ""}

    # 其余渠道的入站事件：交 dispatcher 处理
    from app.channels.base import InboundMessage
    from app.channels.dispatcher import handle_inbound

    msg = _parse_callback(c.kind, body)
    if not msg:
        return {"message": "ignored"}
    from app.channels.manager import channel_manager

    adapter = channel_manager._adapters.get(channel_id)
    await handle_inbound(adapter, c, msg, send=adapter is not None)
    return {"message": "ok"}


def _parse_callback(kind: str, body: dict):
    """从回调 JSON 解析出入站消息（各渠道格式不同）。"""
    from app.channels.base import InboundMessage

    if kind == "wework":
        # 企微回调：加密体，此处仅处理明文调试体
        data = body.get("body") or body
        from_info = data.get("from") or {}
        uid = str(from_info.get("userid") or "")
        if not uid:
            return None
        return InboundMessage(channel="wework", external_user=uid,
                              content=str((data.get("text") or {}).get("content") or ""),
                              message_id=str(data.get("msgid") or ""))
    return None
