"""token 估算 + 上下文压缩 + cron 解析测试。"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from app.services.cron import next_run, parse_cron
from app.services.token_est import estimate_messages, estimate_tokens


def test_estimate_tokens():
    assert estimate_tokens('') == 0
    # 中文约 len/1.5
    assert 2 <= estimate_tokens('你好世界') <= 5
    # 英文约 len/4
    assert 3 <= estimate_tokens('hello world foo') <= 6
    # 混合
    assert estimate_tokens('你好 hello') >= 3


def test_estimate_messages():
    msgs = [{'content': '你好'}, {'content': 'hello world'}]
    assert estimate_messages(msgs) >= 8  # 每条 +4 开销


def test_cron_parse_and_next():
    minute, hour, dom, month, dow = parse_cron('30 9 * * *')
    assert minute == {30} and hour == {9}
    d = next_run('30 9 * * *', datetime(2026, 1, 1, 0, 0))
    assert d == datetime(2026, 1, 1, 9, 30)

    d2 = next_run('*/15 * * * *', datetime(2026, 1, 1, 0, 7))
    assert d2 == datetime(2026, 1, 1, 0, 15)

    # 每周一三五 09:00 → 2026-01-01 是周四，下一个 09:00 匹配
    m, h, _, _, w = parse_cron('0 9 * * 1,3,5')
    assert w == {1, 3, 5}


def test_cron_bad_expr():
    with pytest.raises(ValueError):
        parse_cron('bad expr')
    with pytest.raises(ValueError):
        parse_cron('1 2 3')  # 段数不足


async def _run_compress():
    from sqlalchemy import select

    from app.core.db import AsyncSessionLocal, init_models
    from app.models import Conversation, Message, Tenant, User
    from app.services.context_service import KEEP_RECENT, load_context, maybe_compress

    await init_models()
    async with AsyncSessionLocal() as db:
        t = (await db.execute(select(Tenant).where(Tenant.slug == "ctx"))).scalar_one_or_none()
        if not t:
            t = Tenant(name="CTX", slug="ctx"); db.add(t); await db.flush()
        u = (await db.execute(select(User).where(User.username == "ctx_u"))).scalar_one_or_none()
        if not u:
            u = User(tenant_id=t.id, username="ctx_u", password_hash="x"); db.add(u); await db.flush()
        conv = Conversation(tenant_id=t.id, user_id=u.id, title="t")
        db.add(conv); await db.flush()
        # 造 20 条长消息（超阈值）
        for i in range(20):
            db.add(Message(tenant_id=t.id, conversation_id=conv.id, role="user" if i % 2 == 0 else "assistant",
                           content=("内容" * 400) + str(i)))
        await db.commit()
        cid = conv.id

    # mock get_llm 让摘要不真调 LLM
    import app.providers.registry as reg

    class _FakeLLM:
        async def chat(self, msgs, **kw):
            from app.providers.base import ChatResult
            return ChatResult(content="这是摘要", usage={}, model="fake")

    class _RM:
        model_name = "fake"; config_id = 1

    async def _fake_get_llm(db, **kw):
        return _FakeLLM(), _RM()

    orig = reg.get_llm
    reg.get_llm = _fake_get_llm
    try:
        async with AsyncSessionLocal() as db:
            ok = await maybe_compress(db, cid)
            assert ok is True
            conv2 = await db.get(Conversation, cid)
            assert conv2.summary == "这是摘要"
            assert conv2.summary_upto_message_id is not None
            watermark = conv2.summary_upto_message_id
        # 二次调用不重复压缩（水位已到，to_summarize 为空）
        async with AsyncSessionLocal() as db:
            # 现有消息都已 <= 水位（除最近 KEEP_RECENT 条），不产生新的摘要
            conv3 = await db.get(Conversation, cid)
            assert conv3.summary_upto_message_id == watermark
        # load_context 返回摘要 + 最近消息
        async with AsyncSessionLocal() as db:
            conv4 = await db.get(Conversation, cid)
            summary, history = await load_context(db, conv4)
            assert summary and "摘要" in summary
            assert len(history) <= KEEP_RECENT
    finally:
        reg.get_llm = orig
    print("OK compress")


def test_context_compress():
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_run_compress())
    finally:
        loop.close()
        asyncio.set_event_loop(None)


async def _run_stats_and_threshold():
    """context_stats 口径必须与真正触发压缩的判断一致。"""
    from sqlalchemy import select

    from app.core.config import settings
    from app.core.db import AsyncSessionLocal, init_models
    from app.models import Conversation, Message, Tenant, User
    from app.services import context_service as cs

    await init_models()
    async with AsyncSessionLocal() as db:
        t = (await db.execute(select(Tenant).where(Tenant.slug == "ctx"))).scalar_one_or_none()
        if not t:
            t = Tenant(name="CTX", slug="ctx"); db.add(t); await db.flush()
        u = (await db.execute(select(User).where(User.username == "ctx_u"))).scalar_one_or_none()
        if not u:
            u = User(tenant_id=t.id, username="ctx_u", password_hash="x"); db.add(u); await db.flush()
        conv = Conversation(tenant_id=t.id, user_id=u.id, title="stats")
        db.add(conv); await db.flush()
        # 少量消息：远低于阈值
        for i in range(4):
            db.add(Message(tenant_id=t.id, conversation_id=conv.id,
                           role="user" if i % 2 == 0 else "assistant", content=f"短消息{i}"))
        await db.commit()
        cid = conv.id

    async with AsyncSessionLocal() as db:
        conv = await db.get(Conversation, cid)
        s = await cs.context_stats(db, conv)
        assert s["available"] is True
        assert s["compressed"] is False
        assert s["message_count"] == 4
        assert s["limit_tokens"] == cs.threshold_tokens()
        assert 0 < s["percent"] < 100
        # 未超阈值 → 不压缩
        assert await cs.maybe_compress(db, cid) is False

    # 阈值可热改：调低到极低值后同样 4 条消息就该判超限
    orig = settings.context_compress_threshold
    try:
        settings.context_compress_threshold = 1
        async with AsyncSessionLocal() as db:
            conv = await db.get(Conversation, cid)
            s2 = await cs.context_stats(db, conv)
            assert s2["limit_tokens"] == 1
            assert s2["percent"] == 100.0
            assert s2["used_tokens"] > s2["limit_tokens"]
    finally:
        settings.context_compress_threshold = orig

    # 无会话时给出安全空值，而不是抛错
    async with AsyncSessionLocal() as db:
        blank = await cs.context_stats(db, None)
        assert blank["available"] is False and blank["used_tokens"] == 0


def test_context_stats_matches_threshold():
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_run_stats_and_threshold())
    finally:
        loop.close()
        asyncio.set_event_loop(None)
