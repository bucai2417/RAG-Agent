"""对话接口：会话管理 + SSE 流式问答。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal, get_db
from app.core.errors import NotFoundError, PermissionDeniedError
from app.middleware.auth_dep import (
    FileAccess,
    get_file_access,
    get_principal_set,
    require_permission,
    user_has_permission,
)
from app.models import Conversation, Message, UsageLog, User
from app.schemas.chat import ChatRequest, ConversationCreate, ConversationOut, MessageOut
from app.services.chat_service import get_or_create_conversation, regenerate_meta, stream_answer
from app.services.permission import PrincipalSet

router = APIRouter(prefix="/chat", tags=["chat"])


async def _assert_conv_access(db: AsyncSession, conv_id: int, user: User) -> Conversation:
    """会话归属校验：本人，或拥有 chat:read_all（可跨用户只读）。"""
    conv = await db.get(Conversation, conv_id)
    if not conv or conv.tenant_id != user.tenant_id:
        raise NotFoundError("会话不存在")
    if conv.user_id == user.id:
        return conv
    if await user_has_permission(db, user, "chat:read_all"):
        return conv
    raise NotFoundError("会话不存在")


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    before_id: int | None = None,
    limit: int = 100,
    user: User = Depends(require_permission("chat:use")), db: AsyncSession = Depends(get_db)
) -> list[ConversationOut]:
    """列出我的会话（按 id 倒序）。before_id 游标用于「加载更多」。"""
    stmt = select(Conversation).where(Conversation.user_id == user.id)
    if before_id:
        stmt = stmt.where(Conversation.id < before_id)
    rows = (
        await db.execute(stmt.order_by(Conversation.id.desc()).limit(max(1, min(limit, 500))))
    ).scalars().all()
    return [ConversationOut.model_validate(r) for r in rows]


@router.post("/conversations", response_model=ConversationOut)
async def create_conversation(
    body: ConversationCreate,
    user: User = Depends(require_permission("chat:use")),
    db: AsyncSession = Depends(get_db),
) -> ConversationOut:
    conv = Conversation(
        tenant_id=user.tenant_id,
        user_id=user.id,
        title=body.title or "新对话",
        kb_ids=body.kb_ids or [],
    )
    db.add(conv)
    await db.flush()
    return ConversationOut.model_validate(conv)


@router.patch("/conversations/{conv_id}", response_model=ConversationOut)
async def rename_conversation(
    conv_id: int,
    body: dict,
    user: User = Depends(require_permission("chat:use")),
    db: AsyncSession = Depends(get_db),
) -> ConversationOut:
    conv = await db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise NotFoundError("会话不存在")
    if "title" in body:
        conv.title = str(body["title"])[:200]
    if "model_config_id" in body:
        v = body["model_config_id"]
        conv.model_config_id = int(v) if v else None
    await db.flush()
    return ConversationOut.model_validate(conv)


@router.delete("/conversations/{conv_id}")
async def delete_conversation(
    conv_id: int,
    user: User = Depends(require_permission("chat:use")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from sqlalchemy import delete as sql_delete

    conv = await db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise NotFoundError("会话不存在")
    await db.execute(sql_delete(Message).where(Message.conversation_id == conv_id))
    await db.delete(conv)
    await db.flush()
    return {"message": "已删除"}


@router.get("/conversations/{conv_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conv_id: int,
    before_id: int | None = None,
    limit: int | None = None,
    user: User = Depends(require_permission("chat:use")),
    db: AsyncSession = Depends(get_db),
) -> list[MessageOut]:
    """列出会话消息。

    分页：传 limit（+可选 before_id 游标）时只返回**最近 limit 条**（按 id 升序），
    用于长会话增量加载历史；不传则返回全部（向后兼容）。
    """
    await _assert_conv_access(db, conv_id, user)
    stmt = select(Message).where(Message.conversation_id == conv_id)
    if limit is not None:
        # 游标：取 before_id 之前的 limit 条（按 id 倒序取，再反转为升序）
        stmt = stmt.order_by(Message.id.desc()).limit(max(1, min(limit, 500)))
        if before_id:
            stmt = stmt.where(Message.id < before_id)
        rows = list((await db.execute(stmt)).scalars().all())
        rows.reverse()
    else:
        rows = (
            await db.execute(stmt.order_by(Message.id.asc()))
        ).scalars().all()
    out = [MessageOut.model_validate(r) for r in rows]
    # 兜底：历史消息若未记录 artifacts（旧版本生成的文件），按「产物时间落在相邻
    # assistant 消息之间」精确归属；已有 artifacts 的消息不重复处理。
    # 分页模式下不做此兜底（时间边界仅覆盖本页，会失准）——新消息已带 artifacts 字段。
    from app.models import Artifact

    need = limit is None and any(mo.role == "assistant" and not mo.artifacts for mo in out)
    if need:
        arts = (
            await db.execute(
                select(Artifact).where(
                    Artifact.conversation_id == conv_id, Artifact.source == "generated"
                ).order_by(Artifact.id.asc())
            )
        ).scalars().all()
        # 已挂在任何消息上的 artifact_id，避免重复
        used: set[int] = set()
        for mo in out:
            for a in (mo.artifacts or []):
                if a.get("artifact_id"):
                    used.add(a["artifact_id"])
        # 待回填的 assistant 消息时间边界（仅无 artifacts 的）
        bounds: list[tuple[int, int, MessageOut]] = []
        for mo in out:
            if mo.role == "assistant" and not mo.artifacts:
                raw = next((r for r in rows if r.id == mo.id), None)
                bounds.append((mo.id, int(getattr(raw, "created_at", 0) or 0), mo))

        def _ats(a) -> int:
            """产物创建时间 -> epoch 毫秒。

            SQLite 存的是 UTC 的 naive datetime（TimestampMixin 用 utcnow()），
            读出后无 tzinfo；必须按 UTC 解释，否则 .timestamp() 会再减掉本地时区偏移，
            时间整体前移导致归属错乱。
            """
            try:
                dt = a.created_at
                if dt is None:
                    return 0
                if dt.tzinfo is None:
                    from datetime import timezone as _tz
                    dt = dt.replace(tzinfo=_tz.utc)
                return int(dt.timestamp() * 1000)
            except Exception:  # noqa: BLE001
                return 0

        for a in arts:
            if a.id in used:
                continue
            t = _ats(a)
            target = next((mo for _mid, mts, mo in bounds if mts >= t), None)
            if target is None and bounds:
                target = bounds[-1][2]
            if target is not None:
                target.artifacts = list(target.artifacts or []) + [{
                    "file_key": a.file_key, "artifact_id": a.id, "name": a.file_name,
                    "mime": a.mime, "size": a.size,
                }]
    # 过滤掉物理文件已丢失的产物，避免前端渲染出点开即 404 的下载/预览链接
    from app.ingest.storage import get_storage as _gs

    storage = _gs()
    for mo in out:
        if not mo.artifacts:
            continue
        kept = []
        for a in mo.artifacts:
            try:
                if a.get("file_key") and storage.path(a["file_key"]).is_file():
                    kept.append(a)
            except Exception:  # noqa: BLE001
                continue
        mo.artifacts = kept or None
    return out


@router.get("/conversations/{conv_id}/context")
async def conversation_context(
    conv_id: int,
    user: User = Depends(require_permission("chat:use")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """当前会话的上下文占用快照（输入框圆形进度环的数据源）。

    与后端真正触发压缩的判断同源：used_tokens 就是"摘要 + 水位后原文历史"的估计值，
    limit_tokens 就是 system_setting 里的压缩阈值。
    """
    from app.services.context_service import context_stats

    conv = await _assert_conv_access(db, conv_id, user)
    return await context_stats(db, conv)


@router.get("/conversations/{conv_id}/usage")
async def conversation_usage(
    conv_id: int,
    user: User = Depends(require_permission("chat:use")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """当前会话的累计用量。"""
    await _assert_conv_access(db, conv_id, user)
    row = (
        await db.execute(
            select(
                func.count(),
                func.coalesce(func.sum(UsageLog.prompt_tokens), 0),
                func.coalesce(func.sum(UsageLog.completion_tokens), 0),
                func.coalesce(func.sum(UsageLog.total_tokens), 0),
                func.coalesce(func.sum(UsageLog.cached_tokens), 0),
            ).where(
                UsageLog.conversation_id == conv_id,
                UsageLog.tenant_id == user.tenant_id,
                UsageLog.purpose == "chat",
            )
        )
    ).one()
    return {
        "conversation_id": conv_id,
        "calls": row[0],
        "prompt_tokens": int(row[1] or 0),
        "completion_tokens": int(row[2] or 0),
        "total_tokens": int(row[3] or 0),
        "cached_tokens": int(row[4] or 0),
    }


# ==================== 跨用户查看（需 chat:read_all）====================
@router.get("/admin/conversations")
async def admin_list_conversations(
    user_id: int | None = None,
    limit: int = 100,
    user: User = Depends(require_permission("chat:read_all")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """查看本租户内任意用户的会话（只读）。"""
    cond = [Conversation.tenant_id == user.tenant_id]
    if user_id:
        cond.append(Conversation.user_id == user_id)
    rows = (
        await db.execute(select(Conversation).where(*cond).order_by(Conversation.id.desc()).limit(limit))
    ).scalars().all()
    owner_ids = {r.user_id for r in rows}
    names: dict[int, str] = {}
    if owner_ids:
        for u in (await db.execute(select(User).where(User.id.in_(owner_ids)))).scalars().all():
            names[u.id] = u.display_name or u.username
    return [
        {**ConversationOut.model_validate(r).model_dump(),
         "owner_id": r.user_id, "owner_name": names.get(r.user_id, f"#{r.user_id}")}
        for r in rows
    ]


@router.get("/admin/users")
async def admin_chat_users(
    user: User = Depends(require_permission("chat:read_all")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """有会话的用户 + 会话数，供跨用户切换下拉。渠道虚拟用户标注渠道来源与渠道内 ID。"""
    from app.models import ChannelUser

    rows = (
        await db.execute(
            select(Conversation.user_id, func.count())
            .where(Conversation.tenant_id == user.tenant_id)
            .group_by(Conversation.user_id)
        )
    ).all()
    ids = [r[0] for r in rows if r[0]]
    names: dict[int, str] = {}
    if ids:
        for u in (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all():
            names[u.id] = u.display_name or u.username

    # 关联渠道绑定：{user_id: (channel_kind, external_id)}
    chan_map: dict[int, tuple[str, str]] = {}
    if ids:
        cus = (await db.execute(
            select(ChannelUser).where(ChannelUser.user_id.in_(ids))
        )).scalars().all()
        for cu in cus:
            chan_map[cu.user_id] = (cu.channel, cu.external_id)

    kind_label = {"qqbot": "QQ机器人", "wxclaw": "微信", "wework": "企业微信", "feishu": "飞书"}
    out: list[dict] = []
    for uid, cnt in rows:
        if not uid:
            continue
        ext = chan_map.get(uid)
        if ext:
            kind, ext_id = ext
            out.append({
                "user_id": uid, "name": names.get(uid, f"#{uid}"),
                "conversation_count": cnt,
                "channel": kind, "channel_label": kind_label.get(kind, kind),
                "external_id": ext_id,
            })
        else:
            out.append({
                "user_id": uid, "name": names.get(uid, f"#{uid}"),
                "conversation_count": cnt, "channel": None, "channel_label": None, "external_id": None,
            })
    return out


@router.post("/completions")
async def chat_completions(
    body: ChatRequest,
    request: Request,
    user: User = Depends(require_permission("chat:use")),
    ps: PrincipalSet = Depends(get_principal_set),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    conv = await get_or_create_conversation(
        db,
        user_id=ps.user_id,
        tenant_id=ps.tenant_id,
        conversation_id=body.conversation_id,
        kb_ids=body.kb_ids,
        title=body.message[:30],
    )
    if body.model_config_id:
        conv.model_config_id = body.model_config_id
    conv_id = conv.id
    # 立即提交，确保流式生成阶段新 session 能读到该会话
    await db.commit()
    query = body.message
    top_k = body.top_k
    model_config_id = body.model_config_id or conv.model_config_id
    temperature = body.temperature
    # 图片附件 → 转 data URL（视觉模型用）
    images = _to_image_payloads(db, body.attachments)
    attachments = body.attachments or None
    # kb 绑定已由 get_or_create_conversation 按三态处理（None=沿用/[]=全库/[..]=限定），
    # 此处不再清空，避免「切模型/追问」时静默扩大到全库、造成检索范围漂移。

    async def event_gen():
        # 用独立 session 保证流式期间连接可用
        async with AsyncSessionLocal() as sdb:
            conv2 = await sdb.get(Conversation, conv_id)
            # 重新加载完整 principal 集合（含 role/group/dept 祖先），与同步检索一致
            from app.middleware.auth_dep import load_principal_set

            pset = await load_principal_set(sdb, user)
            try:
                async for evt in stream_answer(
                    sdb, ps=pset, conversation=conv2, query=query, top_k=top_k,
                    model_config_id=model_config_id, temperature=temperature,
                    images=images, attachments=attachments, use_retrieval=body.use_retrieval,
                    use_tools=body.use_tools, allow_auto_write=body.allow_auto_write,
                    request=request,
                ):
                    etype = evt.pop("type")
                    yield f"event: {etype}\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n"
            except Exception as e:  # noqa: BLE001
                yield f"event: error\ndata: {json.dumps({'message': str(e)[:300]}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _to_image_payloads(db: AsyncSession, attachments: list[dict]) -> list[dict]:
    """把图片附件转成 data URL（供视觉模型）。"""
    import base64
    import mimetypes

    from app.ingest.storage import get_storage

    out: list[dict] = []
    storage = get_storage()
    for att in attachments or []:
        if att.get("type") != "image":
            continue
        key = att.get("file_key")
        if not key:
            continue
        try:
            data = storage.read(key)
        except Exception:  # noqa: BLE001
            continue
        mime = att.get("mime") or mimetypes.guess_type(att.get("name", ""))[0] or "image/png"
        b64 = base64.b64encode(data).decode()
        out.append({"url": f"data:{mime};base64,{b64}"})
    return out


@router.post("/attachments")
async def upload_attachment(
    file: UploadFile = File(...),
    kind: str = "auto",  # auto/image/document/video
    kb_id: int | None = None,  # 文档入库时指定知识库
    user: User = Depends(require_permission("chat:use")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """对话附件上传：图片（视觉）、文档（入库检索）、视频（仅展示）。"""
    import mimetypes
    from pathlib import Path as _P

    from app.ingest.storage import get_storage

    filename = file.filename or "file"
    ext = _P(filename).suffix.lower().lstrip(".")
    mime = file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    data = await file.read()
    storage = get_storage()
    file_key, content_hash = storage.save(tenant_id=user.tenant_id, filename=filename, data=data)

    # 判定类型
    if kind == "auto":
        if ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp"):
            kind = "image"
        elif ext in ("mp4", "webm", "mov", "avi", "mkv", "mp3", "wav", "m4a"):
            kind = "video"
        else:
            kind = "document"

    from app.core.security import create_file_token

    signed = create_file_token(scope="attachment", tenant_id=user.tenant_id, file_key=file_key)
    att = {
        "type": kind,
        "file_key": file_key,
        "name": filename,
        "mime": mime,
        "size": len(data),
        "url": f"/api/v1/chat/attachments/{file_key}?t={signed}",
    }
    # 登记为产物（上传来源），供文件管理页统一查看
    from app.models import Artifact

    db.add(Artifact(
        tenant_id=user.tenant_id, user_id=user.id, file_key=file_key, file_name=filename,
        file_ext=ext, mime=mime, size=len(data), source="upload", content_hash=content_hash,
    ))
    # 文档：入知识库、可被检索
    if kind == "document" and kb_id:
        from app.models import Document
        from app.tasks.ingest_tasks import enqueue_document

        doc = Document(
            tenant_id=user.tenant_id, kb_id=kb_id, title=_P(filename).stem,
            source_type="upload", file_key=file_key, file_name=filename, file_ext=ext,
            file_size=len(data), content_hash=content_hash, status="pending", uploaded_by=user.id,
        )
        db.add(doc)
        await db.flush()
        att["doc_id"] = doc.id
        await db.commit()
        await enqueue_document(doc.id)
    else:
        await db.commit()
    return att


@router.get("/attachments/{file_key:path}")
async def get_attachment(
    file_key: str,
    download: int = 0,
    access: FileAccess = Depends(get_file_access),
) -> FileResponse:
    from app.ingest.storage import get_storage

    if access.via == "token":
        # 签名 token：必须 scope==attachment 且 file_key 精确一致
        if access.scope != "attachment" or access.file_key != file_key:
            raise PermissionDeniedError("链接无权访问该文件")
        tenant_id = access.tenant_id
    else:
        tenant_id = access.user.tenant_id
    try:
        p = get_storage().path(file_key, tenant_id=tenant_id)
    except ValueError:
        raise NotFoundError("附件不存在")
    if not p.is_file():
        raise NotFoundError("附件不存在")
    from app.core.http_utils import content_disposition

    return FileResponse(str(p), headers={
        "Content-Disposition": content_disposition(p.name, inline=not download),
    })


@router.post("/attachments/{file_key:path}/sign")
async def sign_attachment(
    file_key: str,
    user: User = Depends(require_permission("chat:use")),
) -> dict:
    """为附件签发短期访问 URL（供 <img>/<video>/下载 使用）。"""
    from app.core.config import settings
    from app.core.security import create_file_token
    from app.ingest.storage import get_storage

    try:
        p = get_storage().path(file_key, tenant_id=user.tenant_id)
    except ValueError:
        raise NotFoundError("附件不存在")
    if not p.is_file():
        raise NotFoundError("附件不存在")
    token = create_file_token(scope="attachment", tenant_id=user.tenant_id, file_key=file_key)
    return {
        "url": f"/api/v1/chat/attachments/{file_key}?t={token}",
        "expires_in": settings.file_token_expire_minutes * 60,
    }


@router.post("/attachments/sign")
async def sign_attachments_batch(
    body: dict,
    user: User = Depends(require_permission("chat:use")),
) -> dict:
    """批量签发附件 URL（一次请求覆盖一屏附件）。"""
    from app.core.config import settings
    from app.core.security import create_file_token
    from app.ingest.storage import get_storage

    keys = body.get("file_keys") or []
    storage = get_storage()
    urls: dict[str, str] = {}
    for fk in keys:
        fk = str(fk)
        try:
            p = storage.path(fk, tenant_id=user.tenant_id)
        except ValueError:
            continue
        if not p.is_file():
            continue
        token = create_file_token(scope="attachment", tenant_id=user.tenant_id, file_key=fk)
        urls[fk] = f"/api/v1/chat/attachments/{fk}?t={token}"
    return {"urls": urls, "expires_in": settings.file_token_expire_minutes * 60}


@router.patch("/messages/{message_id}/feedback")
async def set_feedback(
    message_id: int,
    body: dict,
    user: User = Depends(require_permission("chat:use")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    msg = await db.get(Message, message_id)
    if not msg or msg.tenant_id != user.tenant_id:
        raise NotFoundError("消息不存在")
    # 归属校验：只能对自己会话里的消息反馈
    conv = await db.get(Conversation, msg.conversation_id)
    if not conv or conv.user_id != user.id:
        raise NotFoundError("消息不存在")
    msg.feedback = int(body.get("feedback", 0))
    await db.flush()
    return {"message": "已记录", "feedback": msg.feedback}


@router.post("/conversations/{conv_id}/regenerate")
async def regenerate(
    conv_id: int,
    request: Request,
    use_retrieval: int = 1,
    user: User = Depends(require_permission("chat:use")),
    ps: PrincipalSet = Depends(get_principal_set),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """重新生成最后一条回答。"""
    conv = await db.get(Conversation, conv_id)
    if not conv or conv.user_id != user.id:
        raise NotFoundError("会话不存在")
    query, attachments = await regenerate_meta(db, conv_id, user.id)
    if not query:
        raise NotFoundError("无可重新生成的消息")
    images = _to_image_payloads(db, attachments)
    from app.core.config import settings as _settings
    top_k = _settings.retrieval_top_k
    model_config_id = conv.model_config_id or None

    async def event_gen():
        async with AsyncSessionLocal() as sdb:
            conv2 = await sdb.get(Conversation, conv_id)
            from app.middleware.auth_dep import load_principal_set

            pset = await load_principal_set(sdb, user)
            try:
                async for evt in stream_answer(
                    sdb, ps=pset, conversation=conv2, query=query, top_k=top_k,
                    model_config_id=model_config_id, images=images,
                    attachments=attachments or None, use_retrieval=bool(use_retrieval),
                    request=request,
                ):
                    etype = evt.pop("type")
                    yield f"event: {etype}\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n"
            except Exception as e:  # noqa: BLE001
                yield f"event: error\ndata: {json.dumps({'message': str(e)[:300]}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class _ToolConfirmIn(__import__("pydantic").BaseModel):
    action_id: int
    decision: str = "approve"  # approve | reject


@router.post("/tool-confirm")
async def chat_tool_confirm(
    body: _ToolConfirmIn,
    user: User = Depends(require_permission("chat:use")),
    ps: PrincipalSet = Depends(get_principal_set),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """问答页 HITL：确认 AI 的写操作（建库/删文档等）。approve 则执行并以确认者权限续跑。"""
    import time as _time

    from sqlalchemy import update as _upd

    from app.agents.tools.registry import registry, tool_allowed
    from app.core.errors import ConflictError
    from app.middleware.auth_dep import get_user_permission_codes
    from app.models import AgentAction

    action = await db.get(AgentAction, body.action_id)
    if not action or action.tenant_id != user.tenant_id:
        raise NotFoundError("待确认操作不存在")
    if action.status != "pending":
        raise ConflictError("该操作已处理")
    if action.expires_at and action.expires_at < int(_time.time() * 1000):
        action.status = "expired"
        await db.commit()
        raise ConflictError("该操作已过期")

    decision = body.decision if body.decision in ("approve", "reject") else "reject"
    tool = registry.get_admin(action.tool_name) or registry.get_builtin(action.tool_name)
    if tool is None:
        raise NotFoundError("工具不存在")
    perms = await get_user_permission_codes(db, user)
    if not tool_allowed(tool, perms):
        raise ConflictError("你没有执行该操作的权限")

    new_status = "approved" if decision == "approve" else "rejected"
    res = await db.execute(
        _upd(AgentAction).where(AgentAction.id == action.id, AgentAction.status == "pending")
        .values(status=new_status, approved_by=user.id)
    )
    if res.rowcount != 1:
        await db.rollback()
        raise ConflictError("该操作已被处理")
    await db.commit()
    await db.refresh(action)

    async def gen():
        if decision == "reject":
            yield f"event: action_resolved\ndata: {json.dumps({'action_id': action.id, 'decision': 'reject', 'ok': True}, ensure_ascii=False)}\n\n"
            return
        # 执行工具
        from app.agents.tools.base import ToolContext

        ctx = ToolContext(
            db=db, ps=ps, tenant_id=user.tenant_id, user_id=user.id,
            conversation_id=action.conversation_id, agent_id=None, config={},
        )
        try:
            args = action.arguments or {}
            result = await tool.run(args, ctx)
            action.status = "executed"
            action.result = {"content": result.content[:2000], "is_error": result.is_error}
            await db.commit()
            # data 一并下发：前端靠它渲染结构化提示（如「本机没装浏览器 → 去哪装」的引导卡片）
            yield f"event: tool_result\ndata: {json.dumps({'id': f'action-{action.id}', 'name': tool.name, 'content': result.content[:4000], 'is_error': result.is_error, 'data': result.data}, ensure_ascii=False)}\n\n"
            yield f"event: action_resolved\ndata: {json.dumps({'action_id': action.id, 'decision': 'approve', 'ok': True, 'result': result.content[:500]}, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            action.status = "failed"
            action.error = str(e)[:500]
            await db.commit()
            yield f"event: error\ndata: {json.dumps({'message': f'执行失败: {str(e)[:200]}'}, ensure_ascii=False)}\n\n"
            return

        # 续跑：把工具结果回灌 LLM，继续作答/调用后续工具
        from app.services.chat_service import resume_answer

        tool_text = result.content if not result.is_error else f"工具执行失败：{result.content[:300]}"
        try:
            async for evt in resume_answer(
                db, ps=ps, action=action, tool=tool, tool_result_text=tool_text,
                allow_auto_write=True,
            ):
                etype = evt.get("type")
                yield f"event: {etype}\ndata: {json.dumps({k: v for k, v in evt.items() if k != 'type'}, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"event: error\ndata: {json.dumps({'message': f'续跑失败: {str(e)[:200]}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/conversations/{conv_id}/export")
async def export_conversation_endpoint(
    conv_id: int,
    fmt: str = "md",
    user: User = Depends(require_permission("chat:use")),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """导出会话为 md/pdf/docx 文件。"""
    from app.services.export_service import export_conversation

    conv = await _assert_conv_access(db, conv_id, user)
    if fmt not in ("md", "pdf", "docx"):
        fmt = "md"
    filename, data, mime = await export_conversation(db, conversation=conv, user_id=user.id, fmt=fmt)
    import io

    from app.core.http_utils import content_disposition

    return StreamingResponse(io.BytesIO(data), media_type=mime,
                             headers={"Content-Disposition": content_disposition(filename)})


@router.post("/conversations/{conv_id}/share")
async def share_conversation(
    conv_id: int,
    user: User = Depends(require_permission("chat:use")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """生成会话的免登录分享链接（先导出为 md 产物，再签发短期 token URL）。"""
    import time as _time

    from app.core.config import settings
    from app.core.security import create_file_token
    from app.ingest.storage import get_storage
    from app.models import Artifact
    from app.services.export_service import export_conversation

    conv = await _assert_conv_access(db, conv_id, user)
    filename, data, mime = await export_conversation(db, conversation=conv, user_id=user.id, fmt="md")
    storage = get_storage()
    file_key, chash = storage.save(tenant_id=user.tenant_id, filename=filename, data=data)
    art = Artifact(
        tenant_id=user.tenant_id, user_id=user.id, conversation_id=conv.id,
        file_key=file_key, file_name=filename, file_ext=filename.rsplit(".", 1)[-1],
        mime=mime, size=len(data), source="generated", content_hash=chash,
        expires_at=int(_time.time() * 1000) + settings.file_token_expire_minutes * 60 * 1000,
    )
    db.add(art)
    await db.flush()
    token = create_file_token(scope="artifact", tenant_id=user.tenant_id, artifact_id=art.id)
    return {
        # 分享产物为 md 对话记录：用 attachment 下载（inline 会让浏览器把 markdown
        # 当纯文本显示/下载，用户容易误以为「生成的文件打不开」）
        "url": f"/api/v1/files/{art.id}/download?t={token}",
        "download_url": f"/api/v1/files/{art.id}/download?t={token}",
        "expires_in": settings.file_token_expire_minutes * 60,
    }
