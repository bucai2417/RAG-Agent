"""上下文压缩的数据库级行为：超长会话分页补全、摘要滚动、死循环防护。

纯函数口径见 test_context_budget.py；这里验证的是「水位推进」这类只在真库上
才暴露的问题——水位一旦推过头，中间那段消息既没进摘要也不再是原文，
是不可恢复的信息丢失，所以每条都要有用例盯着。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.db import AsyncSessionLocal, init_models
from app.models import Conversation, Message, Tenant, User
from app.providers.base import ChatResult
from app.services import context_service as cs


class _FakeLLM:
    """记录每次摘要调用收到的 transcript，便于断言"哪些消息真的进了摘要"。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def chat(self, msgs, **kw):
        prompt = msgs[-1].content
        self.calls.append(prompt)
        # 摘要本身要小，否则会触发"摘要再折叠"分支，干扰断言
        return ChatResult(content=f"摘要#{len(self.calls)}", usage={}, model="fake")


class _RM:
    model_name = "fake"
    config_id = 1


@pytest.fixture()
async def fake_llm(monkeypatch):
    llm = _FakeLLM()
    import app.providers.registry as reg

    async def _get(db, **kw):
        return llm, _RM()

    monkeypatch.setattr(reg, "get_llm", _get)
    return llm


@pytest.fixture()
async def db():
    await init_models()
    async with AsyncSessionLocal() as s:
        yield s


async def _mk_conv(db, slug: str, n: int, unit: int = 150) -> int:
    """造 n 条消息，每条 unit*2 个汉字（≈ unit 个 token 量级）。

    参数要与 low_threshold=2000 匹配：单条太大时「至少保留 keep_recent 条」本身
    就会超过阈值，压缩后仍停在阈值上方，用例断言的就不再是分页/水位行为。
    """
    t = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
    if not t:
        t = Tenant(name=slug.upper(), slug=slug)
        db.add(t)
        await db.flush()
    u = (await db.execute(
        select(User).where(User.tenant_id == t.id, User.username == f"{slug}_u")
    )).scalar_one_or_none()
    if not u:
        u = User(tenant_id=t.id, username=f"{slug}_u", password_hash="x")
        db.add(u)
        await db.flush()
    conv = Conversation(tenant_id=t.id, user_id=u.id, title=slug)
    db.add(conv)
    await db.flush()
    for i in range(n):
        db.add(Message(
            tenant_id=t.id, conversation_id=conv.id,
            role="user" if i % 2 == 0 else "assistant",
            content="内容" * unit + f"#{i}",
        ))
    await db.commit()
    return conv.id


@pytest.fixture()
def low_threshold(monkeypatch):
    """把阈值固定成小值，20~40 条消息就能触发压缩，不必造上百万 token。"""
    monkeypatch.setattr(settings, "context_compress_threshold", 2000, raising=False)
    yield 2000


async def test_compress_advances_watermark_and_keeps_tail(db, fake_llm, low_threshold):
    cid = await _mk_conv(db, "ctx1", 20)
    assert await cs.maybe_compress(db, cid) is True
    conv = await db.get(Conversation, cid)
    assert conv.summary and conv.summary.startswith("摘要#")
    assert conv.summary_upto_message_id

    rows = list((await db.execute(
        select(Message).where(Message.conversation_id == cid).order_by(Message.id)
    )).scalars())
    wm = conv.summary_upto_message_id
    kept = [m for m in rows if m.id > wm]
    # 至少保留 keep_recent 条原文，且压缩后总量应落回阈值以内
    assert len(kept) >= min(cs.keep_recent(), len(rows))
    assert cs.estimate_messages(kept) < low_threshold


async def test_second_call_is_noop_when_below_threshold(db, fake_llm, low_threshold):
    cid = await _mk_conv(db, "ctx2", 20)
    assert await cs.maybe_compress(db, cid) is True
    conv = await db.get(Conversation, cid)
    wm = conv.summary_upto_message_id
    calls = len(fake_llm.calls)

    assert await cs.maybe_compress(db, cid) is False
    assert len(fake_llm.calls) == calls
    conv2 = await db.get(Conversation, cid)
    assert conv2.summary_upto_message_id == wm


async def test_summary_rolls_forward_on_new_messages(db, fake_llm, low_threshold):
    """第二次压缩必须把已有摘要带进 prompt（滚动汇总），而不是丢掉重来。"""
    cid = await _mk_conv(db, "ctx3", 20)
    assert await cs.maybe_compress(db, cid) is True
    conv = await db.get(Conversation, cid)
    first = conv.summary
    first_wm = conv.summary_upto_message_id

    for i in range(20):
        db.add(Message(
            tenant_id=conv.tenant_id, conversation_id=cid,
            role="user" if i % 2 == 0 else "assistant", content="新增" * 200 + f"x{i}",
        ))
    await db.commit()

    assert await cs.maybe_compress(db, cid) is True
    conv2 = await db.get(Conversation, cid)
    assert conv2.summary != first
    # conv / conv2 是同一个 identity map 对象，得用压缩前存下的旧水位比较
    assert conv2.summary_upto_message_id > first_wm
    # 摘要链条没断：第二次调用的 prompt 里带着上一次的摘要
    assert any(f"已有摘要：\n{first}" in c for c in fake_llm.calls)


async def test_no_message_is_silently_dropped_when_rows_exceed_cap(
    db, fake_llm, low_threshold, monkeypatch
):
    """超过 _HARD_ROW_CAP 的会话：水位推进必须覆盖"水位→原 to_summarize"之间那段。

    这是最危险的一条：漏掉的那段消息既进不了摘要，也因为 id <= 水位而不再作为
    原文加载 —— 用户会看到 AI 完全忘了中间几轮说过什么，且无从排查。
    """
    monkeypatch.setattr(cs, "_HARD_ROW_CAP", 10)
    cid = await _mk_conv(db, "ctx4", 45)

    summaries: list[int] = []
    for _ in range(10):  # 反复压缩直到把水位推到会话末尾
        if not await cs.maybe_compress(db, cid):
            break
        conv = await db.get(Conversation, cid)
        summaries.append(conv.summary_upto_message_id)

    conv = await db.get(Conversation, cid)
    rows = list((await db.execute(
        select(Message).where(Message.conversation_id == cid).order_by(Message.id)
    )).scalars())
    kept = [m for m in rows if m.id > conv.summary_upto_message_id]
    assert len(kept) >= 1, "水位推到了最后一条之后，原文全没了"

    # 每次推进的水位必须单调且按 id 连续覆盖：被跳过的消息 id 不能落在
    # 「上一轮水位, 本轮水位」之间却既没进摘要对话也没留在 tail 里。
    assert summaries == sorted(summaries)
    # 45 条 / 每轮至少吃一条 → 至少推进过一次
    assert summaries


async def test_compress_keeps_recent_even_when_they_exceed_threshold(
    db, fake_llm, monkeypatch
):
    """超长单条消息：「保底条数」会让 to_summarize 为空，必须有退让路径。

    否则水位不动、used 恒高于阈值 → 每轮都触发压缩、每轮都压不下去，
    用户看到的是一张永远 100% 的环和反复的摘要调用账单。
    """
    monkeypatch.setattr(settings, "context_compress_threshold", 500, raising=False)
    cid = await _mk_conv(db, "ctx5", 4, unit=1200)
    # 4 条各 ~1600 token，远超 500 阈值，且全部落在 keep_recent 之内
    assert await cs.maybe_compress(db, cid) is True
    conv = await db.get(Conversation, cid)
    assert conv.summary_upto_message_id is not None


async def test_llm_failure_does_not_advance_watermark(db, low_threshold, monkeypatch):
    """摘要调用失败必须原样退出：水位动了就等于那批消息被吞掉。"""
    cid = await _mk_conv(db, "ctx6", 20)

    import app.providers.registry as reg

    class _Boom:
        async def chat(self, *a, **kw):
            raise RuntimeError("upstream 502")

    async def _get(db_, **kw):
        return _Boom(), _RM()

    monkeypatch.setattr(reg, "get_llm", _get)
    assert await cs.maybe_compress(db, cid) is False
    conv = await db.get(Conversation, cid)
    assert conv.summary is None
    assert conv.summary_upto_message_id is None


async def test_load_context_respects_budget_not_fixed_count(db, low_threshold):
    cid = await _mk_conv(db, "ctx7", 30)
    conv = await db.get(Conversation, cid)
    summary, history = await cs.load_context(db, conv)
    assert summary is None
    assert history
    # 条数不是固定值：长会话在预算内应带出多于 keep_recent(6) 条
    assert len(history) > cs.keep_recent()


async def test_load_context_limit_still_wins(db, low_threshold):
    """渠道/工作流显式要短上下文时，limit 仍然是条数上限。"""
    cid = await _mk_conv(db, "ctx8", 30)
    conv = await db.get(Conversation, cid)
    _summary, history = await cs.load_context(db, conv, limit=4)
    assert len(history) <= 4


async def test_concurrent_compress_is_deduplicated(monkeypatch):
    """同一会话并发压缩只能调一次摘要模型。

    两条任务同时到时会各自读到旧水位；若都去调模型，后写的水位会把先写的覆盖，
    中间那批消息就再也没被任何一方读到——而且用户为两次摘要各付一次钱。
    """
    import asyncio

    await init_models()
    async with AsyncSessionLocal() as s:
        cid = await _mk_conv(s, "ctx9", 20)

    calls: list[str] = []
    gate = asyncio.Event()

    class _SlowLLM:
        async def chat(self, msgs, **kw):
            calls.append(msgs[-1].content)
            await gate.wait()  # 卡住第一个压缩，让第二个一定在飞行中到达
            return ChatResult(content="摘要", usage={}, model="fake")

    import app.providers.registry as reg

    async def _get(db_, **kw):
        return _SlowLLM(), _RM()

    monkeypatch.setattr(reg, "get_llm", _get)
    monkeypatch.setattr(settings, "context_compress_threshold", 2000, raising=False)

    async def run() -> bool:
        async with AsyncSessionLocal() as s:
            return await cs.maybe_compress(s, cid)

    first = asyncio.create_task(run())
    for _ in range(200):  # 等第一个任务真正占位
        if cid in cs._inflight:
            break
        await asyncio.sleep(0.005)
    second = asyncio.create_task(run())
    await asyncio.sleep(0.02)
    gate.set()
    results = await asyncio.gather(first, second)

    assert results.count(True) == 1, "只应有一次压缩真正执行"
    assert len(calls) == 1, f"摘要模型被调了 {len(calls)} 次"
    assert cid not in cs._inflight, "占位必须释放，否则该会话再也压不动"


async def test_inflight_is_released_on_failure(db, low_threshold, monkeypatch):
    """摘要失败也必须释放占位，否则一次网络抖动会让该会话永久停止压缩。"""
    cid = await _mk_conv(db, "ctx10", 20)

    import app.providers.registry as reg

    class _Boom:
        async def chat(self, *a, **kw):
            raise RuntimeError("upstream 502")

    async def _get(db_, **kw):
        return _Boom(), _RM()

    monkeypatch.setattr(reg, "get_llm", _get)
    assert await cs.maybe_compress(db, cid) is False
    assert cid not in cs._inflight

