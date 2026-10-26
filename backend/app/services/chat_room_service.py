"""聊天室业务逻辑：房间/成员/消息、默认大群、成员角色、撤回/置顶。"""
from __future__ import annotations

import time

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.core.logging import get_logger
from app.models import Agent, ChatMessage, ChatRoom, ChatRoomMember, User

logger = get_logger("chat_room")

DEFAULT_ROOM_NAME = "全员大群"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_detach(db: AsyncSession, obj) -> None:
    """把未能落库的 ORM 实例从 session 摘掉。

    唯一冲突后该实例既不在 identity_map 也不在 ``new`` 里（flush 失败已回滚），
    但 session 仍持有它；不摘掉的话，调用方后续一次 ``commit()`` 会把它再插一次，
    又撞一次约束。摘除是幂等的，实例未挂载时静默跳过。
    """
    try:
        if obj in db:
            db.expunge(obj)
    except Exception:  # noqa: BLE001
        pass


async def _insert_or_reuse(db: AsyncSession, obj, lookup):
    """插入一行；撞唯一约束时回读已存在的那一行并返回它。

    「先查后建」在并发下必然有窗口：两个请求都查不到 → 都去插。唯一索引是最终判据，
    这里负责让「插失败」的那一方能继续走完请求——直接用 savepoint 回滚这一小段，
    再按同样的条件回读。

    为什么不能用普通 try/except 包住 flush：IntegrityError 会把 SQLAlchemy 的
    外层事务标记为「必须回滚」，此后该 session 的任何写操作都会抛
    PendingRollbackError，调用方（登录、建租户、发消息）会在毫不相干的地方炸掉。

    ``lookup`` 是一个无参协程，返回已存在的行或 None；只有它能给出结果时才算成功，
    否则原样抛出——那说明约束冲突来自别的原因，不该被静默吞掉。

    返回 ``(row, created)``：``created=False`` 表示这行是别人先建的、本次复用了它。
    调用方据此决定「首次创建才做的事」（如建群主成员行、打日志）要不要执行。
    """
    try:
        async with db.begin_nested():
            db.add(obj)
            await db.flush()
        return obj, True
    except IntegrityError:
        _safe_detach(db, obj)
        existing = await lookup()
        if existing is None:
            raise
        return existing, False


async def ensure_default_room(db: AsyncSession, tenant_id: int) -> ChatRoom:
    """确保租户有「全员大群」，返回它（幂等、并发安全）。

    并发安全靠两道保险，缺一不可：

    - **唯一索引**（``uq_chat_room_tenant_default``）：最终判据。没有它，两个请求
      都查不到行、都插入，就真的存在两个默认大群了。
    - **savepoint 兜底**：撞上唯一冲突时只回滚这一小段，重新读一次已有房间；
      直接让 IntegrityError 冒出去会把整个请求事务打成「必须回滚」，
      调用方（登录、建租户）随后任何一次写库都会失败。
    """
    stmt = select(ChatRoom).where(
        ChatRoom.tenant_id == tenant_id, ChatRoom.is_default.is_(True),
        ChatRoom.status == "active",
    )

    async def _lookup() -> ChatRoom | None:
        return (await db.execute(stmt)).scalars().first()

    existed = await _lookup()
    if existed:
        return existed

    # 用第一个管理员作为群主
    owner = (
        await db.execute(
            select(User).where(User.tenant_id == tenant_id, User.is_admin.is_(True))
            .order_by(User.id)
        )
    ).scalars().first()
    room = ChatRoom(
        tenant_id=tenant_id, name=DEFAULT_ROOM_NAME, kind="group",
        owner_id=owner.id if owner else 0, is_default=True,
        announcement="欢迎使用企业聊天室。请文明发言，涉及敏感信息请注意合规。",
    )
    room, created = await _insert_or_reuse(db, room, _lookup)

    if created and owner:
        # 群主成员行：并发下可能已被别人补上（uq_chat_member_room_user 兜底），
        # 撞上就当作已存在——目标是「有且只有一条」，不是「一定由我创建」。
        try:
            async with db.begin_nested():
                db.add(ChatRoomMember(
                    tenant_id=tenant_id, room_id=room.id, user_id=owner.id, role="owner",
                ))
                await db.flush()
        except IntegrityError:
            pass
    if created:
        logger.info("default_room_created", tenant_id=tenant_id, room_id=room.id)
    else:
        logger.info("default_room_race_reused", tenant_id=tenant_id, room_id=room.id)
    return room


async def is_member(db: AsyncSession, *, room_id: int, user_id: int) -> ChatRoomMember | None:
    """是否为成员。默认大群：所有内部用户天然是成员（动态，不依赖成员表）。

    非默认群仍以成员表为准。
    """
    m = (
        await db.execute(
            select(ChatRoomMember).where(
                ChatRoomMember.room_id == room_id, ChatRoomMember.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if m:
        return m
    # 默认大群：任何内部用户视为成员（构造一个内存对象，不落库）
    room = await db.get(ChatRoom, room_id)
    if room and room.is_default and room.tenant_id:
        u = await db.get(User, user_id)
        if u and u.tenant_id == room.tenant_id and getattr(u, "user_type", "internal") != "external":
            return ChatRoomMember(room_id=room_id, user_id=user_id, role="member")
    return None


async def get_member_row(db: AsyncSession, *, room_id: int, user_id: int) -> ChatRoomMember | None:
    """只取库中真实成员行（默认大群的「虚拟成员」返回 None）。"""
    return (
        await db.execute(
            select(ChatRoomMember).where(
                ChatRoomMember.room_id == room_id, ChatRoomMember.user_id == user_id
            )
        )
    ).scalar_one_or_none()


async def ensure_member_row(
    db: AsyncSession, *, tenant_id: int, room_id: int, user_id: int, role: str = "member",
) -> ChatRoomMember:
    """取库中成员行，不存在则落库（用于对默认大群虚拟成员执行禁言/设管理员等持久化操作）。

    同样可能并发（两人同时「清未读」会各插一条），因此撞上 ``uq_chat_member_room_user``
    时回读已存在的那一行，而不是把 IntegrityError 抛给调用方。
    """
    m = await get_member_row(db, room_id=room_id, user_id=user_id)
    if m:
        return m
    row = ChatRoomMember(tenant_id=tenant_id, room_id=room_id, user_id=user_id, role=role)
    row, _created = await _insert_or_reuse(
        db, row, lambda: get_member_row(db, room_id=room_id, user_id=user_id),
    )
    return row


async def mark_read(db: AsyncSession, *, room_id: int, user_id: int, tenant_id: int) -> None:
    """记录已读水位（毫秒）。

    默认大群成员可能是「虚拟成员」（未显式入表），此处确保持久化一条成员行，
    否则 last_read_at 写入内存对象、会话结束即丢失，导致未读数永远不清零。
    """
    m = await ensure_member_row(db, tenant_id=tenant_id, room_id=room_id, user_id=user_id)
    m.last_read_at = _now_ms()
    await db.flush()


async def member_role(db: AsyncSession, *, room: ChatRoom, user_id: int, is_admin: bool) -> str:
    """有效角色：租户管理员在群里等同 owner（拥有撤回任何消息等权力）。"""
    if is_admin or room.owner_id == user_id:
        return "owner"
    m = await is_member(db, room_id=room.id, user_id=user_id)
    return m.role if m else ""


async def can_access(db: AsyncSession, *, room: ChatRoom, user_id: int, is_admin: bool) -> bool:
    """能否查看房间：默认大群全员可见；其它群需是成员；管理员均可。"""
    if is_admin or room.is_default or room.owner_id == user_id:
        return True
    return (await is_member(db, room_id=room.id, user_id=user_id)) is not None


async def list_rooms(db: AsyncSession, *, tenant_id: int, user_id: int, is_admin: bool) -> list[dict]:
    """我可见的房间：默认大群 + 我加入的群 + 我的私聊。附未读数与最后一条消息摘要。"""
    await ensure_default_room(db, tenant_id)
    # 我加入的 room_id
    my_rooms = [
        r for (r,) in (await db.execute(
            select(ChatRoomMember.room_id).where(ChatRoomMember.user_id == user_id)
        )).all()
    ]
    stmt = select(ChatRoom).where(
        ChatRoom.tenant_id == tenant_id,
        ChatRoom.status == "active",
        or_(
            ChatRoom.is_default.is_(True),
            ChatRoom.id.in_(my_rooms or [-1]),
            ChatRoom.owner_id == user_id,
        ),
    ).order_by(ChatRoom.last_message_at.desc().nullslast(), ChatRoom.id.desc())
    rooms = (await db.execute(stmt)).scalars().all()

    out: list[dict] = []
    remarks = await get_remarks(db, owner_id=user_id)
    for room in rooms:
        last = (
            await db.execute(
                select(ChatMessage).where(ChatMessage.room_id == room.id)
                .order_by(ChatMessage.id.desc()).limit(1)
            )
        ).scalar_one_or_none()
        m = await is_member(db, room_id=room.id, user_id=user_id)
        last_read = (m.last_read_at if m else None) or 0
        unread = (
            await db.execute(
                select(func.count()).select_from(ChatMessage).where(
                    ChatMessage.room_id == room.id, ChatMessage.created_at > last_read,
                    ChatMessage.revoked.is_(False),
                )
            )
        ).scalar_one()
        # 私聊：附带对方资料 + 我给他设的备注（备注优先展示）
        peer = None
        if room.kind == "direct":
            peer_id = room.peer_user_id if room.owner_id == user_id else room.owner_id
            peer = await _user_brief(db, peer_id)
            if peer:
                peer["remark"] = remarks.get(peer["user_id"]) or None
                peer["display"] = peer["remark"] or peer["name"]
        out.append(_room_brief(
            room, last=last, unread=int(unread),
            my_role=(m.role if m else ("owner" if room.owner_id == user_id else "")),
            peer=peer,
        ))
    return out


def _room_brief(room: ChatRoom, *, last: ChatMessage | None, unread: int, my_role: str,
                peer: dict | None = None) -> dict:
    preview = ""
    if last:
        if last.revoked:
            preview = "（消息已撤回）"
        elif last.content:
            preview = last.content[:60]
        elif last.attachments:
            preview = "[附件]"
    return {
        "id": room.id, "name": room.name, "kind": room.kind, "is_default": room.is_default,
        "announcement": room.announcement, "owner_id": room.owner_id, "my_role": my_role,
        "peer_user_id": room.peer_user_id, "peer": peer,
        "last_message_at": room.last_message_at, "last_preview": preview,
        "message_count": room.message_count, "unread": unread,
    }


async def get_or_create_direct(db: AsyncSession, *, tenant_id: int, user_id: int, peer_id: int) -> ChatRoom:
    """私聊房间：按无序 user 对唯一（并发安全）。

    两个人同时点开与对方的私聊时，两边都会查不到行再各插一条，
    于是消息分成两个房间、双方各自只看到自己的一半。
    唯一索引 ``uq_chat_room_direct_pair`` 是最终判据，savepoint 只负责
    让「撞车」的那一方能继续用同一事务（否则整个请求事务作废）。
    """
    if user_id == peer_id:
        raise ConflictError("不能与自己私聊")
    a, b = sorted((user_id, peer_id))

    def _stmt():
        return select(ChatRoom).where(
            ChatRoom.tenant_id == tenant_id, ChatRoom.kind == "direct",
            ChatRoom.owner_id == a, ChatRoom.peer_user_id == b,
            ChatRoom.status == "active",
        )

    async def _lookup() -> ChatRoom | None:
        return (await db.execute(_stmt())).scalars().first()

    existed = await _lookup()
    if existed:
        return existed
    peer = await db.get(User, peer_id)
    if not peer or peer.tenant_id != tenant_id:
        raise NotFoundError("对方用户不存在")
    room = ChatRoom(
        tenant_id=tenant_id, name="私聊", kind="direct",
        owner_id=a, peer_user_id=b,
    )
    room, created = await _insert_or_reuse(db, room, _lookup)
    if not created:
        logger.info("direct_room_race_reused", tenant_id=tenant_id, room_id=room.id)
        return room

    for uid in (a, b):
        # 成员行并发补写会撞 uq_chat_member_room_user：目标是有且只有一条，撞上即已满足
        try:
            async with db.begin_nested():
                db.add(ChatRoomMember(
                    tenant_id=tenant_id, room_id=room.id, user_id=uid, role="member",
                ))
                await db.flush()
        except IntegrityError:
            pass
    return room


async def post_message(
    db: AsyncSession, *, room: ChatRoom, sender_id: int | None, sender_type: str = "user",
    content: str = "", attachments: list | None = None, mentions: list | None = None,
    reply_to_id: int | None = None, content_type: str = "text",
) -> ChatMessage:
    """落一条消息并更新房间摘要计数。"""
    msg = ChatMessage(
        tenant_id=room.tenant_id, room_id=room.id, sender_id=sender_id,
        sender_type=sender_type, content=content, content_type=content_type,
        attachments=attachments or None, mentions=mentions or None,
        reply_to_id=reply_to_id, created_at=_now_ms(),
    )
    db.add(msg)
    room.last_message_at = msg.created_at
    room.message_count = (room.message_count or 0) + 1
    await db.flush()
    return msg


async def can_revoke(db: AsyncSession, *, room: ChatRoom, msg: ChatMessage, actor_id: int, is_admin: bool) -> bool:
    """撤回权限：本人 或 群主/群管理员/租户管理员。"""
    if is_admin or room.owner_id == actor_id:
        return True
    role = await member_role(db, room=room, user_id=actor_id, is_admin=is_admin)
    if role in ("owner", "admin"):
        return True
    return msg.sender_id == actor_id and msg.sender_type == "user"


async def read_receipt(db: AsyncSession, *, room: ChatRoom, msgs: list[ChatMessage]) -> dict[int, dict]:
    """已读回执：返回每条消息「被多少人读过 / 谁没读」。

    实现：用成员表的 last_read_at 水位判断——某成员的水位 >= 消息时间即视为已读。
    返回 {msg_id: {"read_by": [uid...], "unread_by": [uid...], "read_count": n}}
    """
    rows = (
        await db.execute(select(ChatRoomMember).where(ChatRoomMember.room_id == room.id))
    ).scalars().all()
    # 人类成员的水位（默认大群里没显式入表的不算，避免把全员当未读）
    levels: dict[int, int] = {}
    for m in rows:
        if m.user_id and m.last_read_at:
            levels[m.user_id] = int(m.last_read_at)
    out: dict[int, dict] = {}
    for msg in msgs:
        if msg.revoked:
            continue
        read_by = [uid for uid, ts in levels.items() if ts >= msg.created_at]
        unread_by = [uid for uid in levels if uid not in read_by]
        out[msg.id] = {"read_by": read_by, "unread_by": unread_by, "read_count": len(read_by)}
    return out


async def serialize_message(db: AsyncSession, msg: ChatMessage, *, name_map: dict[int, dict] | None = None) -> dict:
    """把消息序列化成前端结构（含发送者名/用户名/部门/头像，撤回占位）。"""
    nm = name_map or {}
    sender = nm.get(msg.sender_id or 0, {}) if msg.sender_type == "user" else {}
    if msg.sender_type == "agent":
        ag = await db.get(Agent, msg.sender_id) if msg.sender_id else None
        sender = {"name": (ag.name if ag else "机器人"), "is_agent": True, "agent_id": msg.sender_id}
    return {
        "id": msg.id, "room_id": msg.room_id, "sender_id": msg.sender_id,
        "sender_type": msg.sender_type, "sender_name": sender.get("name") or "未知",
        "sender_username": sender.get("username"),
        "sender_department": sender.get("department"),
        "sender_is_agent": bool(sender.get("is_agent")),
        "content": "" if msg.revoked else msg.content,
        "content_type": msg.content_type,
        "attachments": None if msg.revoked else msg.attachments,
        "mentions": msg.mentions, "reply_to_id": msg.reply_to_id,
        "pinned": msg.pinned, "revoked": msg.revoked, "revoked_by": msg.revoked_by,
        "created_at": msg.created_at,
    }


async def name_map_for(db: AsyncSession, msgs: list[ChatMessage]) -> dict[int, dict]:
    """构造 {user_id: {name, username, department}} 映射（用于消息序列化）。

    注意：私聊备注是「每个观看者各自设置」的，服务端不烘焙进消息（WS 广播会串味），
    由前端按当前用户的备注映射覆盖展示。
    """
    from app.models import Department

    ids = [m.sender_id for m in msgs if m.sender_type == "user" and m.sender_id]
    if not ids:
        return {}
    rows = (await db.execute(select(User).where(User.id.in_(set(ids))))).scalars().all()
    dept_ids = {u.department_id for u in rows if u.department_id}
    dept_map: dict[int, str] = {}
    if dept_ids:
        depts = (await db.execute(select(Department).where(Department.id.in_(dept_ids)))).scalars().all()
        dept_map = {d.id: d.name for d in depts}
    return {
        u.id: {
            "name": u.display_name or u.username,
            "username": u.username,
            "department": dept_map.get(u.department_id) if u.department_id else None,
        }
        for u in rows
    }


async def get_remarks(db: AsyncSession, *, owner_id: int) -> dict[int, str]:
    """某用户设置的全部私聊备注 {peer_id: remark}（仅非空）。"""
    from app.models import ChatUserRemark

    rows = (
        await db.execute(select(ChatUserRemark).where(ChatUserRemark.owner_id == owner_id))
    ).scalars().all()
    return {r.peer_id: r.remark for r in rows if r.remark}


async def set_remark(
    db: AsyncSession, *, tenant_id: int, owner_id: int, peer_id: int, remark: str,
) -> str | None:
    """设置/清除对某人的私聊备注。remark 为空则删除该备注，返回 None。"""
    from app.models import ChatUserRemark

    text = (remark or "").strip()[:64]
    row = (
        await db.execute(
            select(ChatUserRemark).where(
                ChatUserRemark.owner_id == owner_id, ChatUserRemark.peer_id == peer_id
            )
        )
    ).scalar_one_or_none()
    if not text:
        if row:
            await db.delete(row)
            await db.flush()
        return None
    if row:
        row.remark = text
    else:
        db.add(ChatUserRemark(tenant_id=tenant_id, owner_id=owner_id, peer_id=peer_id, remark=text))
    await db.flush()
    return text


async def _user_brief(db: AsyncSession, user_id: int | None, *, online_ids: set[int] | None = None) -> dict | None:
    """某用户的展示信息（姓名/用户名/部门/是否管理员/是否在线）。"""
    from app.models import Department

    if not user_id:
        return None
    u = await db.get(User, user_id)
    if not u:
        return None
    dept_name = None
    if u.department_id:
        d = await db.get(Department, u.department_id)
        dept_name = d.name if d else None
    return {
        "user_id": u.id, "name": u.display_name or u.username, "username": u.username,
        "department": dept_name, "is_admin": bool(u.is_admin),
        "online": (u.id in online_ids) if online_ids is not None else None,
    }


async def search_messages(
    db: AsyncSession, *, room_id: int, keyword: str, limit: int = 30,
    before_id: int | None = None,
) -> list[ChatMessage]:
    """按关键词搜索群内历史消息（撤回的除外，内容/附件名匹配）。"""
    kw = (keyword or "").strip()
    stmt = select(ChatMessage).where(
        ChatMessage.room_id == room_id,
        ChatMessage.revoked.is_(False),
        ChatMessage.content.ilike(f"%{kw}%") if kw else True,
    )
    if before_id:
        stmt = stmt.where(ChatMessage.id < before_id)
    stmt = stmt.order_by(ChatMessage.id.desc()).limit(limit)
    rows = list((await db.execute(stmt)).scalars().all())
    rows.reverse()
    return rows


async def room_stats(db: AsyncSession, *, room: ChatRoom, days: int = 7) -> dict:
    """群活跃统计：成员数/消息总数/最近 N 天消息数/活跃人数/Top 发言者/近期要点。"""
    from app.services.chat_room_service import _now_ms as _now

    now_ms = _now()
    since = now_ms - days * 86_400_000

    member_count = int(
        (await db.execute(
            select(func.count()).select_from(ChatRoomMember).where(ChatRoomMember.room_id == room.id)
        )).scalar_one()
    )
    message_count = int(room.message_count or 0)
    recent_rows = (
        await db.execute(
            select(ChatMessage).where(
                ChatMessage.room_id == room.id,
                ChatMessage.revoked.is_(False),
                ChatMessage.created_at >= since,
            ).order_by(ChatMessage.id.desc())
        )
    ).scalars().all()
    recent_count = len(recent_rows)
    speakers: dict[int, int] = {}
    for m in recent_rows:
        if m.sender_type == "user" and m.sender_id:
            speakers[m.sender_id] = speakers.get(m.sender_id, 0) + 1
    nm = await name_map_for(db, list(recent_rows))
    top = sorted(speakers.items(), key=lambda x: -x[1])[:5]
    top_speakers = [(nm.get(uid, {}).get("name") or f"用户{uid}", c) for uid, c in top]
    # 近期代表消息（最近 8 条有内容的）
    snippets = [m.content[:80] for m in recent_rows if m.content][:8]
    return {
        "member_count": member_count,
        "message_count": message_count,
        "recent_count": recent_count,
        "active_users": len(speakers),
        "speakers": speakers,
        "top_speakers": top_speakers,
        "recent_snippets": snippets,
    }


async def set_mute_all(
    db: AsyncSession, *, room: ChatRoom, enabled: bool, minutes: int = 0,
) -> int | None:
    """开启/关闭全员禁言。存入 room.settings["mute_all_until"]（毫秒，None=未开启）。

    开启时可带 minutes（到点自动解除由调度器/读取时判断）。返回该到期时刻。
    """
    settings_ = dict(room.settings or {})
    if enabled:
        until = _now_ms() + minutes * 60_000 if minutes > 0 else 0
        settings_["mute_all_until"] = until
        room.settings = settings_
    else:
        settings_.pop("mute_all_until", None)
        room.settings = settings_
    await db.flush()
    return settings_.get("mute_all_until")


def mute_all_active(room: ChatRoom) -> bool:
    """全员禁言是否生效（0 表示手动解除前一直生效）。"""
    until = (room.settings or {}).get("mute_all_until")
    if until is None:
        return False
    if until == 0:
        return True
    return until > _now_ms()
