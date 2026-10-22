"""上下文压缩：超阈值时把早期对话摘要写入 Conversation.summary，保留最近 N 条原文。

两个数字决定行为：
- 阈值（context_compress_threshold）：上下文 token 超过它就触发压缩，可在「系统设置 → 检索与重排」热改。
- KEEP_RECENT：压缩后仍以原文保留的最近消息条数。

context_stats() 把同一套口径暴露给前端，用于输入框里的圆形占用环——
"进度"必须和真正触发压缩的判断同源，否则环满却不压缩（或反之）会误导用户。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.providers.base import ChatMessage
from app.services.token_est import estimate_messages, estimate_tokens

logger = get_logger("context")

SUMMARY_SYSTEM = (
    "你是一个对话摘要器。把下面的多轮对话压缩成简洁的中文摘要，"
    "保留关键事实、结论、用户偏好与待办事项，不要遗漏人名/实体名与数字。只输出摘要本身。"
)

# 兜底默认值；运行期以系统设置为准（见 threshold_tokens / keep_recent）
THRESHOLD_TOKENS = 4000
KEEP_RECENT = 6

# 同一会话正在压缩时不再重复提交，避免一次回答触发多次摘要 LLM 调用
_inflight: set[int] = set()


def threshold_tokens() -> int:
    """当前生效的压缩阈值（系统设置可热改；取不到或非法则用默认）。"""
    try:
        from app.core.config import settings

        v = int(getattr(settings, "context_compress_threshold", 0) or 0)
        return v if v > 0 else THRESHOLD_TOKENS
    except Exception:  # noqa: BLE001
        return THRESHOLD_TOKENS


def keep_recent() -> int:
    """压缩后以原文保留的最近消息条数（系统设置可热改）。"""
    try:
        from app.core.config import settings

        v = int(getattr(settings, "context_keep_recent", 0) or 0)
        return v if v > 0 else KEEP_RECENT
    except Exception:  # noqa: BLE001
        return KEEP_RECENT


async def _history_rows(db: AsyncSession, conversation_id: int, watermark: int = 0) -> list:
    """取会话的 user/assistant 消息（水位之后），按 id 升序。"""
    from app.models import Message

    cond = [Message.conversation_id == conversation_id, Message.role.in_(("user", "assistant"))]
    if watermark:
        cond.append(Message.id > watermark)
    return list(
        (await db.execute(select(Message).where(*cond).order_by(Message.id.asc()))).scalars().all()
    )


async def context_stats(db: AsyncSession, conversation) -> dict:
    """上下文占用快照：供前端圆形进度环与悬浮详情使用。

    口径与 maybe_compress 一致——"已用"就是真正会喂给模型的 token：
    摘要块 + 水位之后的原文历史（未压缩时即全部历史）。
    """
    from app.models import Conversation

    limit = threshold_tokens()
    keep = keep_recent()
    blank = {
        "available": False,
        "conversation_id": None,
        "used_tokens": 0,
        "limit_tokens": limit,
        "keep_recent": keep,
        "percent": 0.0,
        "compressed": False,
        "summary_tokens": 0,
        "watermark": 0,
        "message_count": 0,
        "compressing": False,
    }
    if conversation is None:
        return blank

    conv = conversation
    if not isinstance(conv, Conversation):
        cid = getattr(conversation, "id", None)
        conv = await db.get(Conversation, cid) if cid else None
    if conv is None:
        return blank

    watermark = int(conv.summary_upto_message_id or 0)
    summary = conv.summary or ""
    rows = await _history_rows(db, conv.id, watermark)
    used = estimate_tokens(summary) + estimate_messages(rows)

    # 已进摘要的消息条数：悬浮层要显示「N 条已折叠」才有说服力
    archived = 0
    if watermark:
        from app.models import Message

        archived = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(Message)
                    .where(
                        Message.conversation_id == conv.id,
                        Message.id <= watermark,
                        Message.role.in_(("user", "assistant")),
                    )
                )
            ).scalar()
            or 0
        )

    return {
        "available": True,
        "conversation_id": conv.id,
        "used_tokens": int(used),
        "limit_tokens": limit,
        "keep_recent": keep,
        "percent": round(min(100.0, used * 100.0 / limit), 1) if limit else 0.0,
        "compressed": bool(summary),
        "summary_tokens": estimate_tokens(summary),
        "watermark": watermark,
        "message_count": len(rows) + archived,
        "archived_messages": archived,
        "compressing": conv.id in _inflight,
    }


async def maybe_compress(db: AsyncSession, conversation_id: int) -> bool:
    """若对话上下文超阈值则压缩早期消息。返回是否执行了压缩。"""
    from app.models import Conversation

    conv = await db.get(Conversation, conversation_id)
    if not conv:
        return False

    keep = keep_recent()
    watermark = int(conv.summary_upto_message_id or 0)
    rows = await _history_rows(db, conversation_id, watermark)
    # 判定必须用「摘要 + 全量待用历史」，不能用被裁到最近 N 条的窗口：
    # 窗口长度恒定，永远够不到阈值，压缩就会形同未实现。
    used = estimate_tokens(conv.summary or "") + estimate_messages(rows)
    if used < threshold_tokens():
        return False

    # 待摘要的早期消息：水位之后、最近 N 条之前
    to_summarize = rows[:-keep] if keep > 0 else list(rows)
    if not to_summarize:
        return False

    transcript = "\n".join(f"{m.role}: {m.content}" for m in to_summarize)
    prev = conv.summary or ""
    prompt = (f"已有摘要：\n{prev}\n\n" if prev else "") + f"新增对话：\n{transcript}"

    _inflight.add(conversation_id)
    try:
        from app.providers.registry import get_llm

        llm, rm = await get_llm(db, tenant_id=conv.tenant_id)
        res = await llm.chat(
            [ChatMessage(role="system", content=SUMMARY_SYSTEM), ChatMessage(role="user", content=prompt)],
            model=rm.model_name, stream=False, temperature=0.2,
        )
        summary = (res.content or "").strip()
    except Exception:  # noqa: BLE001
        logger.exception("context_compress_failed", conversation_id=conversation_id)
        return False
    finally:
        _inflight.discard(conversation_id)

    if not summary:
        return False
    conv.summary = summary
    conv.summary_upto_message_id = to_summarize[-1].id
    await db.commit()
    logger.info(
        "context_compressed",
        conversation_id=conversation_id,
        upto=to_summarize[-1].id,
        before_tokens=used,
        summary_tokens=estimate_tokens(summary),
    )
    return True


async def load_context(
    db: AsyncSession, conversation, limit: int | None = None
) -> tuple[str | None, list[ChatMessage]]:
    """返回 (摘要块, 最近 N 条历史消息)。limit 缺省用系统设置里的 keep_recent。"""
    from app.models import Message

    summary = None
    watermark = 0
    if conversation is not None:
        summary = getattr(conversation, "summary", None)
        watermark = getattr(conversation, "summary_upto_message_id", 0) or 0
    if conversation is None:
        return None, []
    cond = [Message.conversation_id == conversation.id, Message.role.in_(("user", "assistant"))]
    if watermark:
        cond.append(Message.id > watermark)
    rows = (
        await db.execute(
            select(Message).where(*cond).order_by(Message.id.desc()).limit(limit or keep_recent())
        )
    ).scalars().all()
    history = [ChatMessage(role=m.role, content=m.content) for m in reversed(rows) if m.content]
    summary_block = f"[历史对话摘要]\n{summary}" if summary else None
    return summary_block, history


async def _compress_job(conversation_id: int) -> None:
    """队列任务：独立 session 执行压缩。"""
    from app.core.db import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            await maybe_compress(db, conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception("context_compress_job_failed", conversation_id=conversation_id)
