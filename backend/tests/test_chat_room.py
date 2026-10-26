"""企业聊天室：房间/消息/成员/撤回/置顶/@机器人。"""
from __future__ import annotations

import asyncio


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _setup():
    from sqlalchemy import select

    from app.core.db import AsyncSessionLocal, init_models
    from app.core.security import hash_password
    from app.models import Tenant, User

    await init_models()
    async with AsyncSessionLocal() as db:
        t = (await db.execute(select(Tenant).order_by(Tenant.id))).scalars().first()
        if not t:
            t = Tenant(name="CR", slug="cr"); db.add(t); await db.flush()
        u = (await db.execute(select(User).where(User.username == "cr_u"))).scalar_one_or_none()
        if not u:
            u = User(tenant_id=t.id, username="cr_u", password_hash=hash_password("Cr@2026abc"),
                     is_admin=True, user_type="internal", status="active", approval_status="approved")
            db.add(u); await db.flush()
        await db.commit()
        return {"tenant_id": t.id, "uid": u.id}


def test_chat_room_models_exist():
    from app.models import ChatMessage, ChatRoom, ChatRoomMember

    assert ChatRoom.__tablename__ == "chat_room"
    assert ChatRoomMember.__tablename__ == "chat_room_member"
    assert ChatMessage.__tablename__ == "chat_message"
    cols = set(ChatMessage.__table__.columns.keys())
    assert {"revoked", "pinned", "sender_id", "mentions", "room_id"} <= cols


def test_broadcaster_basic():
    from app.services.chat_broadcaster import ChatBroadcaster

    async def _run2():
        b = ChatBroadcaster()
        sent = []

        class FakeWS:
            async def send_text(self, t): sent.append(t)
        ws = FakeWS()
        await b.join(tenant_id=1, room_id=5, user_id=9, ws=ws)
        assert b.online_in_room(1, 5) == 1
        await b.broadcast_room(tenant_id=1, room_id=5, payload={"type": "message"})
        assert len(sent) == 1 and '"message"' in sent[0]
        await b.leave(tenant_id=1, room_id=5, user_id=9, ws=ws)
        assert b.online_in_room(1, 5) == 0
    _run(_run2())


def test_default_room_created():
    async def _run2():
        from app.core.db import AsyncSessionLocal
        from app.services.chat_room_service import ensure_default_room, list_rooms

        d = await _setup()
        async with AsyncSessionLocal() as db:
            r1 = await ensure_default_room(db, d["tenant_id"])
            r2 = await ensure_default_room(db, d["tenant_id"])
            assert r1.id == r2.id, "默认大群应幂等（同一租户只有一个）"
            rooms = await list_rooms(db, tenant_id=d["tenant_id"], user_id=d["uid"], is_admin=True)
            assert any(r["is_default"] for r in rooms)
            await db.commit()
    _run(_run2())


def test_post_and_revoke_message():
    async def _run2():
        from app.core.db import AsyncSessionLocal
        from app.models import ChatRoom
        from app.services.chat_room_service import (
            can_revoke, ensure_default_room, post_message,
        )

        d = await _setup()
        async with AsyncSessionLocal() as db:
            room = await ensure_default_room(db, d["tenant_id"])
            msg = await post_message(db, room=room, sender_id=d["uid"], content="hello")
            assert msg.id and msg.revoked is False
            # 本人可撤回
            assert await can_revoke(db, room=room, msg=msg, actor_id=d["uid"], is_admin=False)
            # 他人不可
            assert not await can_revoke(db, room=room, msg=msg, actor_id=999999, is_admin=False)
            # 管理员可
            assert await can_revoke(db, room=room, msg=msg, actor_id=999999, is_admin=True)
            await db.commit()
    _run(_run2())


def test_chat_room_routes_registered():
    from app.api.v1 import chat_room as C

    methods = {(r.path, m) for r in C.router.routes for m in getattr(r, "methods", set())}
    assert ("/chat/rooms", "GET") in methods
    assert ("/chat/rooms/{room_id}/messages", "POST") in methods
    assert ("/chat/rooms/{room_id}/messages/{msg_id}/revoke", "POST") in methods
    assert ("/chat/rooms/{room_id}/messages/{msg_id}/pin", "POST") in methods
    assert ("/chat/rooms/{room_id}/bots", "POST") in methods
    assert ("/chat/rooms/{room_id}/members/{member_user_id}/role", "POST") in methods
    # WebSocket 路由
    from starlette.routing import WebSocketRoute
    assert any(isinstance(r, WebSocketRoute) and r.path == "/chat/rooms/ws" for r in C.router.routes)


def test_ws_route_order_before_dynamic():
    """WebSocket 路由不应被 /{room_id} 干扰（WS 与 HTTP 分开匹配，但顺序仍应正确）。"""
    from app.api.v1 import chat_room as C

    paths = [getattr(r, "path", "") for r in C.router.routes]
    assert "/chat/rooms/direct" in paths
    # direct 必须在 {room_id} 之前
    assert paths.index("/chat/rooms/direct") < paths.index("/chat/rooms/{room_id}")


def test_bot_mention_detection():
    """@机器人名 或 mentions 含 agent_id 都应触发。"""
    import inspect

    from app.api.v1.chat_room import _mentioned_bots

    src = inspect.getsource(_mentioned_bots)
    assert "@" in src and "mentions" in src


def test_bot_service_exists():
    from app.services import chat_bot_service as B

    assert hasattr(B, "maybe_trigger_bots")


# ==================== P0-4：并发重复创建 ====================
def test_default_room_unique_index_defined():
    """模型层必须声明「每租户一个默认大群」的唯一索引。

    没有它，ensure_default_room 的「查不到就建」在并发下会建出多个默认大群，
    此后每个人看到的「全员大群」都不一样。索引必须带部分谓词，
    否则所有房间都会被 (tenant_id) 唯一约束挡住。
    """
    from app.models import ChatRoom

    idx = {i.name: i for i in ChatRoom.__table__.indexes}
    assert "uq_chat_room_tenant_default" in idx, "缺少每租户一个默认大群的唯一索引"
    default_idx = idx["uq_chat_room_tenant_default"]
    assert default_idx.unique is True
    assert [c.name for c in default_idx.columns] == ["tenant_id"]
    # 部分索引：谓词必须只覆盖默认大群
    assert "is_default" in str(default_idx.dialect_options["sqlite"]["where"])


def test_direct_room_unique_index_defined():
    """私聊唯一键必须把 kind/status 放进键里。

    部分索引的谓词不参与唯一性比较：若只写 (tenant_id, owner_id, peer_user_id)，
    而群聊行的 peer_user_id 是 NULL，在 MySQL（无部分索引）上所有群聊会互撞。
    """
    from app.models import ChatRoom

    idx = {i.name: i for i in ChatRoom.__table__.indexes}
    assert "uq_chat_room_direct_pair" in idx
    d = idx["uq_chat_room_direct_pair"]
    assert d.unique is True
    assert [c.name for c in d.columns] == [
        "tenant_id", "owner_id", "peer_user_id", "kind", "status",
    ]


def _make_user(tenant_id: int, username: str, *, is_admin: bool = False):
    """建一个真实用户（私聊要求对端存在且同租户；默认大群要求有管理员当群主）。"""
    from app.core.security import hash_password
    from app.models import User

    return User(
        tenant_id=tenant_id, username=username, password_hash=hash_password("Cr@2026abc"),
        is_admin=is_admin, user_type="internal", status="active",
        approval_status="approved",
    )


def test_direct_room_same_pair_is_reused():
    """同一对用户反复发起私聊，永远拿到同一个房间（含顺序颠倒）。"""
    async def _run2():
        from app.core.db import AsyncSessionLocal
        from app.services.chat_room_service import get_or_create_direct

        d = await _setup()
        async with AsyncSessionLocal() as db:
            peer = _make_user(d["tenant_id"], "cr_peer_a")
            db.add(peer)
            await db.commit()
            peer_id = peer.id

        async with AsyncSessionLocal() as db:
            r1 = await get_or_create_direct(
                db, tenant_id=d["tenant_id"], user_id=d["uid"], peer_id=peer_id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            # 反向发起：必须命中同一间，而不是新建
            r2 = await get_or_create_direct(
                db, tenant_id=d["tenant_id"], user_id=peer_id, peer_id=d["uid"])
            await db.commit()
            assert r1.id == r2.id, "私聊必须按无序用户对唯一"
    _run(_run2())


def test_concurrent_direct_room_creation():
    """两个请求同时建同一对用户的私聊：只能有一个房间，双方都返回同一 id。

    这是 P0-4 的真实故障：两边都查不到行 → 各插一条 → 消息分裂在两个房间。
    唯一索引是最终判据，savepoint 让「撞车」的一方能在同一事务里回读。
    """
    async def _run2():
        from app.core.db import AsyncSessionLocal
        from app.services.chat_room_service import get_or_create_direct

        d = await _setup()
        async with AsyncSessionLocal() as db:
            u1 = _make_user(d["tenant_id"], "cr_pair_1")
            u2 = _make_user(d["tenant_id"], "cr_pair_2")
            db.add_all([u1, u2])
            await db.commit()
            uid1, uid2 = u1.id, u2.id

        async def _one():
            async with AsyncSessionLocal() as db:
                room = await get_or_create_direct(
                    db, tenant_id=d["tenant_id"], user_id=uid1, peer_id=uid2)
                await db.commit()
                return room.id

        ids = await asyncio.gather(_one(), _one(), _one())
        assert len(set(ids)) == 1, f"并发建私聊产生了多个房间: {ids}"

        from sqlalchemy import func, select
        from app.models import ChatRoom

        async with AsyncSessionLocal() as db:
            n = (await db.execute(
                select(func.count()).select_from(ChatRoom).where(
                    ChatRoom.tenant_id == d["tenant_id"],
                    ChatRoom.kind == "direct",
                    ChatRoom.owner_id == min(uid1, uid2),
                    ChatRoom.peer_user_id == max(uid1, uid2),
                    ChatRoom.status == "active",
                )
            )).scalar_one()
            assert n == 1
    _run(_run2())


def test_concurrent_default_room_creation():
    """多人首次登录同时触发 ensure_default_room：只能建出一个默认大群。"""
    async def _run2():
        from sqlalchemy import func, select

        from app.core.db import AsyncSessionLocal
        from app.models import ChatRoom, Tenant
        from app.services.chat_room_service import ensure_default_room

        await _setup()
        # 独立租户（并给一个管理员：默认大群用「第一个管理员」当群主，
        # 没有管理员时 owner_id=0、不建成员行——那样测不出成员行的并发）
        async with AsyncSessionLocal() as db:
            t = Tenant(name="RaceT", slug="race-t")
            db.add(t)
            await db.flush()
            db.add(_make_user(t.id, "race_admin", is_admin=True))
            await db.commit()
            tid = t.id

        async def _one():
            async with AsyncSessionLocal() as db:
                room = await ensure_default_room(db, tid)
                await db.commit()
                return room.id

        ids = await asyncio.gather(_one(), _one(), _one(), _one())
        assert len(set(ids)) == 1, f"并发建默认大群产生了多个房间: {ids}"
        async with AsyncSessionLocal() as db:
            n = (await db.execute(
                select(func.count()).select_from(ChatRoom).where(
                    ChatRoom.tenant_id == tid, ChatRoom.is_default.is_(True),
                    ChatRoom.status == "active",
                )
            )).scalar_one()
            assert n == 1
            # 群主成员行也只应有一条
            from app.models import ChatRoomMember
            m = (await db.execute(
                select(func.count()).select_from(ChatRoomMember).where(
                    ChatRoomMember.room_id == ids[0])
            )).scalar_one()
            assert m == 1
    _run(_run2())


def test_insert_or_reuse_recovers_from_unique_conflict():
    """确定性覆盖「撞唯一约束后回读」分支。

    `asyncio.gather` 在 SQLite 上常常退化成一个一个跑，覆盖不到冲突分支——
    而冲突分支正是这段代码存在的理由。这里直接让插入必然撞车：
    B 先建好并提交，A 再插同一个业务键。

    两件事必须同时成立：
    1. 返回的是已存在的那一行（不是 None、也不抛出去）；
    2. A 的事务仍然可用 —— 这正是用 savepoint 而不是裸 try/except 的原因：
       IntegrityError 若不回滚到 savepoint，会把外层事务打成「必须回滚」，
       调用方（登录、建租户）随后任何一次写库都会炸。
    """
    async def _run2():
        from sqlalchemy import func, select, text

        from app.core.db import AsyncSessionLocal
        from app.models import ChatRoom, Tenant
        from app.services.chat_room_service import _insert_or_reuse, ensure_default_room

        await _setup()
        async with AsyncSessionLocal() as db:
            t = Tenant(name="ConflictT", slug="conflict-t")
            db.add(t)
            await db.flush()
            tid = t.id
            await db.commit()

        stmt = select(ChatRoom).where(
            ChatRoom.tenant_id == tid, ChatRoom.is_default.is_(True),
            ChatRoom.status == "active",
        )

        async def _lookup(db):
            return (await db.execute(stmt)).scalars().first()

        async with AsyncSessionLocal() as b:
            existing = await ensure_default_room(b, tid)
            await b.commit()
            winner_id = existing.id

        async with AsyncSessionLocal() as a:
            dup = ChatRoom(
                tenant_id=tid, name="全员大群", kind="group", owner_id=0,
                is_default=True, status="active",
            )
            row, created = await _insert_or_reuse(a, dup, lambda: _lookup(a))
            assert created is False, "撞车的一方不应报告「已创建」"
            assert row.id == winner_id, "必须回读已存在的那一行"
            # 事务仍可用（savepoint 只回滚了这一小段）
            await a.execute(text("select 1"))
            await a.commit()

        async with AsyncSessionLocal() as db:
            n = (await db.execute(
                select(func.count()).select_from(ChatRoom).where(
                    ChatRoom.tenant_id == tid, ChatRoom.is_default.is_(True),
                    ChatRoom.status == "active",
                )
            )).scalar_one()
            assert n == 1
            assert (await _lookup(db)).id == winner_id
    _run(_run2())


def test_transfer_owner_rejects_direct_room():
    """私聊的 owner_id 是业务键（用户对里较小的 id），转让会打断唯一键与查询。

    若允许转让，这一对用户再点开私聊会新建一个房间，历史消息一分为二。
    """
    import inspect

    from app.api.v1.chat_room import transfer_owner

    src = inspect.getsource(transfer_owner)
    assert 'room.kind == "direct"' in src
