"""上下文压缩：超阈值时把早期对话摘要写入 Conversation.summary，保留最近若干条原文。

四个数字决定行为（都可在「系统设置 → 检索与重排」热改）：
- 窗口 context_window_tokens：模型上下文窗口。0 = 按模型名推断（见 _WINDOW_HINTS）。
- 比例 context_window_ratio：压缩阈值占窗口的比例（默认 0.7，余量留给输出/检索资料/工具 schema）。
- 阈值 context_compress_threshold：超过它就压缩。0 = 自动 = 窗口 × 比例 − 输出预留。
- 保留 context_keep_recent：压缩后**至少**原样保留的最近消息条数（下限，不是上限）。

预算口径（三者同源，环上的数字就是真发出去的量）：
  used       = 摘要 + 水位之后的全部原文      → 决定要不要压缩（压力值）
  loaded     = 摘要 + 本轮真正发给模型的原文  → 按 token 预算动态截取，不再固定 6 条
  threshold  = 触发线

为什么不能固定条数：条数与 token 没有换算关系。改成按预算从最新往前取，
长会话才能把阈值用满；同时压制"最近 N 条本身已超阈值 → 永远压不下去"的死循环。
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

# 兜底默认值；运行期以系统设置为准
THRESHOLD_TOKENS = 4000           # 仅在取不到配置时兜底
KEEP_RECENT = 6
DEFAULT_WINDOW_TOKENS = 32768     # 认不出模型名时的保守窗口
DEFAULT_WINDOW_RATIO = 0.7
DEFAULT_OUTPUT_RESERVE = 4096

_RATIO_MIN, _RATIO_MAX = 0.3, 0.95
# 压缩后目标占用（占阈值比例）。压到 35% 留足后续多轮空间，避免每轮都触发摘要。
_KEEP_RATIO = 0.35
# 单次摘要调用的输入上限；超出则分块滚动汇总，避免一次性塞爆模型
_SUMMARY_CHUNK_TOKENS = 6000
# 摘要自身占阈值上限；超了就把摘要本身再折一次
_SUMMARY_MAX_RATIO = 0.35
# 单次最多取多少条历史（防超长会话把整表读进内存）
_HARD_ROW_CAP = 500

# 模型名 → 上下文窗口。按"更具体的在前"排列：gpt-4o 必须排在 gpt-4 之前。
_WINDOW_HINTS: tuple[tuple[str, int], ...] = (
    ("claude-3-5", 200_000), ("claude-3.5", 200_000), ("claude-3-7", 200_000),
    ("claude-sonnet-4", 200_000), ("claude-opus-4", 200_000), ("claude", 200_000),
    ("gemini-1.5-pro", 1_000_000), ("gemini-1.5", 1_000_000), ("gemini-2", 1_000_000),
    ("gemini", 1_000_000),
    ("gpt-4.1", 1_000_000), ("gpt-4o", 128_000), ("gpt-4-turbo", 128_000),
    ("gpt-4", 8_192), ("gpt-3.5", 16_384), ("gpt-3", 4_096),
    ("o1-", 200_000), ("o3-", 200_000), ("o4-", 200_000), ("o1", 128_000), ("o3", 128_000),
    ("qwen-long", 1_000_000), ("qwen2.5", 131_072), ("qwen3", 131_072),
    ("qwen-max", 32_768), ("qwen", 32_768),
    ("deepseek", 65_536), ("kimi", 131_072), ("moonshot", 131_072),
    ("glm-4", 131_072), ("chatglm", 8_192), ("glm", 8_192),
    ("yi-", 200_000), ("llama-4", 1_000_000), ("llama-3.1", 131_072),
    ("llama-3", 8_192), ("llama", 8_192),
    ("mistral-large", 131_072), ("mixtral", 32_768), ("mistral", 32_768),
    ("doubao", 131_072), ("hunyuan", 32_768), ("ernie", 32_768),
)

# 同一会话正在压缩时不再重复提交，避免一次回答触发多次摘要 LLM 调用
_inflight: set[int] = set()


def _cfg_int(name: str, default: int) -> int:
    try:
        from app.core.config import settings

        v = getattr(settings, name, None)
        if v is None or v == "":
            return default
        return int(v)
    except Exception:  # noqa: BLE001
        return default


def _cfg_float(name: str, default: float) -> float:
    try:
        from app.core.config import settings

        v = getattr(settings, name, None)
        if v is None or v == "":
            return default
        return float(v)
    except Exception:  # noqa: BLE001
        return default


def window_tokens(model_name: str | None = None) -> int:
    """模型上下文窗口。显式配置优先，其次按模型名推断，最后保守兜底。"""
    explicit = _cfg_int("context_window_tokens", 0)
    if explicit > 0:
        return explicit
    name = (model_name or "").lower()
    if name:
        for pat, win in _WINDOW_HINTS:
            if pat in name:
                return win
    return DEFAULT_WINDOW_TOKENS


def output_reserve_tokens(window: int) -> int:
    """给输出留的预算：显式配置，且不超过窗口的 25%。"""
    reserve = _cfg_int("context_output_reserve_tokens", DEFAULT_OUTPUT_RESERVE)
    if reserve <= 0:
        reserve = DEFAULT_OUTPUT_RESERVE
    return max(512, min(reserve, int(window * 0.25)))


def threshold_tokens(model_name: str | None = None) -> int:
    """当前生效的压缩阈值。显式配置 > 0 时用它，否则按窗口自动推导。"""
    explicit = _cfg_int("context_compress_threshold", 0)
    if explicit > 0:
        return explicit
    win = window_tokens(model_name)
    ratio = _cfg_float("context_window_ratio", DEFAULT_WINDOW_RATIO)
    ratio = min(_RATIO_MAX, max(_RATIO_MIN, ratio))
    return max(1024, int(win * ratio) - output_reserve_tokens(win))


def keep_recent() -> int:
    """压缩后**至少**以原文保留的最近消息条数（系统设置可热改）。"""
    v = _cfg_int("context_keep_recent", 0)
    return v if v > 0 else KEEP_RECENT


async def _model_name_for(db: AsyncSession, conversation) -> str | None:
    """会话使用的对话模型名（用于推断窗口）。取不到返回 None（走兜底窗口）。"""
    if conversation is None:
        return None
    try:
        from app.models import ModelConfig

        cid = getattr(conversation, "model_config_id", None)
        if cid:
            mc = await db.get(ModelConfig, cid)
            if mc is not None and mc.model_name:
                return mc.model_name
        tenant_id = getattr(conversation, "tenant_id", None)
        stmt = select(ModelConfig).where(
            ModelConfig.purpose == "chat", ModelConfig.status == "active"
        )
        if tenant_id is not None:
            stmt = stmt.where(
                (ModelConfig.tenant_id == tenant_id) | (ModelConfig.tenant_id.is_(None))
            )
        mc = (await db.execute(stmt.order_by(ModelConfig.priority.asc()).limit(1))).scalars().first()
        if mc is not None:
            return mc.model_name
    except Exception:  # noqa: BLE001
        logger.debug("context_model_name_failed")
    return None


async def _history_rows(
    db: AsyncSession, conversation_id: int, watermark: int = 0, limit: int | None = None
) -> list:
    """取会话的 user/assistant 消息（水位之后），按 id 升序，最多 limit 条（取最新的一批）。"""
    from app.models import Message

    cond = [Message.conversation_id == conversation_id, Message.role.in_(("user", "assistant"))]
    if watermark:
        cond.append(Message.id > watermark)
    stmt = (
        select(Message)
        .where(*cond)
        .order_by(Message.id.desc())
        .limit(limit or _HARD_ROW_CAP)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    rows.reverse()
    return rows


async def _history_range(
    db: AsyncSession, conversation_id: int, after_id: int, before_id: int, limit: int
) -> list:
    """取 (after_id, before_id) 之间的消息（升序，最多 limit 条）。

    用于水位推进前的补全：_HARD_ROW_CAP 只保证读最新一批，超长会话里更早那段
    若直接跳过就会被水位永久吞掉。
    """
    from app.models import Message

    cond = [
        Message.conversation_id == conversation_id,
        Message.role.in_(("user", "assistant")),
        Message.id < before_id,
    ]
    if after_id:
        cond.append(Message.id > after_id)
    rows = list(
        (
            await db.execute(
                select(Message).where(*cond).order_by(Message.id.asc()).limit(limit)
            )
        ).scalars().all()
    )
    return rows


def _msg_tokens(m) -> int:
    return estimate_tokens(getattr(m, "content", "") or "") + 4


def _tail_within_budget(rows: list, budget: int, keep_min: int = 0) -> list:
    """从最新往前取，累计不超过 budget；keep_min 是"至少保留条数"。

    预算实在不够时也至少留最后一条，否则模型完全看不到"刚刚说了什么"。
    """
    tail: list = []
    acc = 0
    for m in reversed(rows):
        t = _msg_tokens(m)
        if tail and acc + t > budget and len(tail) >= keep_min:
            break
        tail.append(m)
        acc += t
    tail.reverse()
    # 截断后开头的 assistant 消息失去了对应的提问，读起来像"凭空接话"，丢掉。
    while len(tail) > 1 and getattr(tail[0], "role", "") != "user":
        tail.pop(0)
    return tail


async def context_stats(db: AsyncSession, conversation) -> dict:
    """上下文占用快照：供前端圆形进度环与悬浮详情使用。

    used_tokens（压力值，决定压缩）与 loaded_tokens（本轮真正发出去的量）分开报：
    两者不同即说明历史已被 token 预算裁剪。
    """
    from app.models import Conversation

    blank = {
        "available": False,
        "conversation_id": None,
        "used_tokens": 0,
        "loaded_tokens": 0,
        "loaded_messages": 0,
        "limit_tokens": threshold_tokens(),
        "window_tokens": window_tokens(),
        "output_reserve_tokens": output_reserve_tokens(window_tokens()),
        "keep_recent": keep_recent(),
        "keep_target_tokens": int(threshold_tokens() * _KEEP_RATIO),
        "percent": 0.0,
        "compressed": False,
        "summary_tokens": 0,
        "watermark": 0,
        "message_count": 0,
        "archived_messages": 0,
        "compressing": False,
        "model_name": None,
    }
    if conversation is None:
        return blank

    conv = conversation
    if not isinstance(conv, Conversation):
        cid = getattr(conversation, "id", None)
        conv = await db.get(Conversation, cid) if cid else None
    if conv is None:
        return blank

    model_name = await _model_name_for(db, conv)
    win = window_tokens(model_name)
    limit = threshold_tokens(model_name)
    keep = keep_recent()

    watermark = int(conv.summary_upto_message_id or 0)
    summary = conv.summary or ""
    summary_tokens = estimate_tokens(summary)
    rows = await _history_rows(db, conv.id, watermark)

    used = summary_tokens + estimate_messages(rows)
    # 本轮真正能发的原文：阈值减去摘要后的预算
    tail = _tail_within_budget(rows, max(0, limit - summary_tokens))
    loaded = summary_tokens + estimate_messages(tail)

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
        "loaded_tokens": int(loaded),
        "loaded_messages": len(tail),
        "limit_tokens": limit,
        "window_tokens": win,
        "output_reserve_tokens": output_reserve_tokens(win),
        "keep_recent": keep,
        "keep_target_tokens": int(limit * _KEEP_RATIO),
        "percent": round(min(100.0, used * 100.0 / limit), 1) if limit else 0.0,
        "compressed": bool(summary),
        "summary_tokens": summary_tokens,
        "watermark": watermark,
        "message_count": len(rows) + archived,
        "archived_messages": archived,
        "compressing": conv.id in _inflight,
        "model_name": model_name,
    }


async def _summarize(llm, rm, prev: str, rows: list) -> str:
    """把一批消息并入已有摘要。"""
    transcript = "\n".join(f"{m.role}: {(m.content or '')}" for m in rows)
    prompt = (f"已有摘要：\n{prev}\n\n" if prev else "") + f"新增对话：\n{transcript}"
    res = await llm.chat(
        [ChatMessage(role="system", content=SUMMARY_SYSTEM),
         ChatMessage(role="user", content=prompt)],
        model=rm.model_name, stream=False, temperature=0.2,
    )
    return (res.content or "").strip()


async def maybe_compress(db: AsyncSession, conversation_id: int) -> bool:
    """若对话上下文超阈值则压缩早期消息。返回是否执行了压缩。

    入口处先占位再干活：占位与判断之间不能有 await，否则两个同一会话的任务
    会在 await 处交错，双双通过检查、各调一次摘要模型。后写的那个还会把先写的
    水位覆盖掉——多花一次钱，且可能丢掉一段谁都没读到的消息。
    """
    if conversation_id in _inflight:
        return False
    _inflight.add(conversation_id)
    try:
        return await _maybe_compress_locked(db, conversation_id)
    finally:
        _inflight.discard(conversation_id)


async def _maybe_compress_locked(db: AsyncSession, conversation_id: int) -> bool:
    """maybe_compress 的实际实现；调用方负责 _inflight 占位与释放。"""
    from app.models import Conversation

    conv = await db.get(Conversation, conversation_id)
    if not conv:
        return False

    model_name = await _model_name_for(db, conv)
    limit = threshold_tokens(model_name)
    keep_min = keep_recent()
    watermark = int(conv.summary_upto_message_id or 0)
    rows = await _history_rows(db, conversation_id, watermark)
    # 判定必须用「摘要 + 全量待用历史」，不能用裁剪过的窗口：
    # 窗口长度恒定，永远够不到阈值，压缩就会形同未实现。
    summary = conv.summary or ""
    used = estimate_tokens(summary) + estimate_messages(rows)
    if used < limit:
        return False

    # 保留多少原文：留到"阈值的 35%"，但至少 keep_min 条。这样即使最近 N 条本身
    # 就超阈值，也会继续往前压，不会出现 to_summarize 为空、环卡在 100% 的死循环。
    target = int(limit * _KEEP_RATIO)
    tail = _tail_within_budget(rows, target, keep_min=keep_min)
    to_summarize = rows[: len(rows) - len(tail)]
    if not to_summarize and len(rows) > 1:
        # 极端情况：连最低保留条数本身都超阈值（超长单条消息）。
        # 再按条数下限保底就永远压不下去，这里退让一步，仍保留最近一半原文。
        cut = max(1, len(rows) // 2)
        tail = rows[cut:]
        to_summarize = rows[:cut]
        logger.info(
            "context_compress_force_floor",
            conversation_id=conversation_id,
            rows=len(rows),
            summarized=len(to_summarize),
        )
    if not to_summarize:
        return False

    # 行数被 _HARD_ROW_CAP 截断时 rows 只是最新一批：水位之后、to_summarize 之前可能还有
    # 更早的消息。直接把水位推到 to_summarize[-1] 会把那一段永久吞掉，所以按 id 区间补齐。
    if len(rows) >= _HARD_ROW_CAP:
        gap = await _history_range(
            db, conversation_id, watermark, rows[0].id, _HARD_ROW_CAP + 1
        )
        if len(gap) > _HARD_ROW_CAP:
            # 缺口比一页还长：这一轮只吃最早那一页。若照样把水位推到最新一条，
            # 中间没读到的段就既没进摘要、也不再是原文（被水位永久跳过）。
            # 留到下一轮继续往前推，直到追平。
            to_summarize = gap[:_HARD_ROW_CAP]
            tail = []
            logger.info(
                "context_compress_backfill_page",
                conversation_id=conversation_id,
                page=len(to_summarize),
            )
        elif gap:
            to_summarize = gap + to_summarize
        used = estimate_tokens(summary) + estimate_messages(to_summarize + tail)

    try:
        from app.providers.registry import get_llm

        llm, rm = await get_llm(db, tenant_id=conv.tenant_id)
    except Exception:  # noqa: BLE001
        logger.exception("context_compress_failed", conversation_id=conversation_id)
        return False

    try:
        running = summary
        # 已有摘要过大时先自我折叠一次，避免"摘要 + 新增"直接越过模型输入上限
        if estimate_tokens(running) > limit * _SUMMARY_MAX_RATIO:
            running = await _summarize(llm, rm, "", [
                ChatMessage(role="assistant", content=running)
            ])

        # 分块滚动汇总：单批超过 _SUMMARY_CHUNK_TOKENS 先落一批，避免一次塞爆
        batch: list = []
        batch_tokens = 0
        for m in to_summarize:
            t = _msg_tokens(m)
            if batch and batch_tokens + t > _SUMMARY_CHUNK_TOKENS:
                running = await _summarize(llm, rm, running, batch)
                batch, batch_tokens = [], 0
            batch.append(m)
            batch_tokens += t
        if batch:
            running = await _summarize(llm, rm, running, batch)

        if not running:
            return False
        # 摘要本身仍然过大：再折一次（只输入摘要，让模型压缩自己）
        if estimate_tokens(running) > limit * _SUMMARY_MAX_RATIO:
            folded = await _summarize(llm, rm, "", [
                ChatMessage(role="assistant", content=running)
            ])
            if folded:
                running = folded
    except Exception:  # noqa: BLE001
        logger.exception("context_compress_failed", conversation_id=conversation_id)
        return False

    conv.summary = running
    conv.summary_upto_message_id = to_summarize[-1].id
    await db.commit()
    logger.info(
        "context_compressed",
        conversation_id=conversation_id,
        upto=to_summarize[-1].id,
        summarized_messages=len(to_summarize),
        kept_messages=len(tail),
        before_tokens=used,
        after_tokens=estimate_tokens(running) + estimate_messages(tail),
        summary_tokens=estimate_tokens(running),
        threshold=limit,
    )
    return True


async def load_context(
    db: AsyncSession,
    conversation,
    limit: int | None = None,
    budget_tokens: int | None = None,
) -> tuple[str | None, list[ChatMessage]]:
    """返回 (摘要块, 历史消息)。

    历史按 **token 预算** 从最新往前取（不再是固定条数），预算缺省 = 阈值 − 摘要占用，
    这样长对话能把阈值用满，而环上的 used/loaded 也是同一口径。
    limit 仅作条数上限（渠道/工作流等要短上下文时传）。
    """
    if conversation is None:
        return None, []

    summary = getattr(conversation, "summary", None)
    watermark = int(getattr(conversation, "summary_upto_message_id", 0) or 0)
    summary_block = f"[历史对话摘要]\n{summary}" if summary else None

    rows = await _history_rows(db, conversation.id, watermark)
    if not rows:
        return summary_block, []

    if budget_tokens is None:
        model_name = await _model_name_for(db, conversation)
        budget_tokens = max(0, threshold_tokens(model_name) - estimate_tokens(summary))
    rows = _tail_within_budget(rows, budget_tokens)
    if limit and limit > 0:
        rows = rows[-limit:]
        while len(rows) > 1 and getattr(rows[0], "role", "") != "user":
            rows.pop(0)
    history = [ChatMessage(role=m.role, content=m.content) for m in rows if m.content]
    return summary_block, history


async def _compress_job(conversation_id: int) -> None:
    """队列任务：独立 session 执行压缩。"""
    from app.core.db import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            await maybe_compress(db, conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception("context_compress_job_failed", conversation_id=conversation_id)
