"""定时任务调度：轮询到点的任务，用独立 task 执行（不占队列 worker）。

- 执行历史：每次落一条 ScheduledTaskRun
- 失败重试：max_retries + retry_interval_seconds（抄工作流节点级退避思路）
- 并发上限：全局信号量限制同时在跑的执行数
- 通知：终态派发到通知渠道（含站内消息）
"""
from __future__ import annotations

import asyncio
import time

from sqlalchemy import select, update

from app.core.logging import get_logger
from app.services.cron import next_run as cron_next_run

logger = get_logger("scheduler")

_RUNNING: set[int] = set()
_sem: asyncio.Semaphore | None = None


def _get_sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        from app.core.config import settings

        _sem = asyncio.Semaphore(max(1, settings.scheduler_max_concurrency))
    return _sem


async def _compute_next(task, after_ms: int | None = None) -> int | None:
    from app.services.schedule_service import compute_next

    return compute_next(task, after_ms=after_ms)


async def execute_task(task_id: int, *, manual: bool = False) -> None:
    """执行一个定时任务：复用 AgentRunner 或工作流引擎。落执行历史 + 终态通知。"""
    await _get_sem().acquire()
    _RUNNING.add(task_id)
    from app.core.db import AsyncSessionLocal
    from app.models import ScheduledTask, ScheduledTaskRun

    started = int(time.time() * 1000)
    run_row_id: int | None = None
    try:
        async with AsyncSessionLocal() as db:
            t = await db.get(ScheduledTask, task_id)
            if not t:
                return
            run = ScheduledTaskRun(
                tenant_id=t.tenant_id, task_id=task_id, status="running",
                started_at=started, attempt=(t.retry_count or 0) + 1,
            )
            db.add(run)
            await db.commit()
            run_row_id = run.id

            # 超时保护：task.timeout_seconds 生效（0/None 表示不限制）
            timeout_s = t.timeout_seconds or 0
            paused_node: str | None = None
            if timeout_s > 0:
                try:
                    ok, result_text, error_text, wf_run_id, run_atts = await asyncio.wait_for(
                        _do_run(db, t), timeout=timeout_s
                    )
                except asyncio.TimeoutError:
                    ok, result_text, error_text, wf_run_id, run_atts = (
                        False, "", f"执行超时（超过 {timeout_s} 秒）", None, None,
                    )
                    try:
                        await db.rollback()
                    except Exception:  # noqa: BLE001
                        pass
            else:
                try:
                    ok, result_text, error_text, wf_run_id, run_atts = await _do_run(db, t)
                except _SchedulerPaused as sp:
                    # 工作流停在审批节点：既不是成功也不是失败，等人工处理
                    paused_node = sp.node_id
                    ok, result_text, error_text, wf_run_id, run_atts = (
                        False, "", f"工作流停在审批节点 {sp.node_id}，等待人工处理", None, None,
                    )
            finished = int(time.time() * 1000)
            run = await db.get(ScheduledTaskRun, run_row_id)
            # waiting：有审批待处理，别记成 failed（否则会触发失败告警与重试）
            run.status = "waiting" if paused_node else ("success" if ok else "failed")
            run.finished_at = finished
            run.duration_ms = finished - started
            run.output = (result_text or "")[:8000]
            run.error = (error_text or "")[:4000] or None
            run.workflow_run_id = wf_run_id
            run.attachments = run_atts or None

            t.last_run_at = finished
            t.last_result = (result_text or error_text or "")[:2000]
            t.run_count = (t.run_count or 0) + 1
            t.last_run_id = wf_run_id

            if ok:
                t.last_status = "success"
                t.retry_count = 0
                if t.schedule_kind == "once":
                    t.enabled = False
                    t.next_run_at = None
                else:
                    # scheduler_loop 已按「本次计划时刻」算好 next_run_at；此处仅在其缺失或
                    # 已过期（执行耗时跨过了一个周期）时推进，避免覆盖正确的排期。
                    now_ms = int(time.time() * 1000)
                    nxt = t.next_run_at
                    guard = 0
                    while (nxt is None or nxt <= now_ms) and guard < 1000:
                        nxt = await _compute_next(t, after_ms=nxt or now_ms)
                        guard += 1
                    t.next_run_at = nxt
            else:
                t.last_status = "failed"
                # 失败重试
                if (t.retry_count or 0) < (t.max_retries or 0):
                    t.retry_count = (t.retry_count or 0) + 1
                    t.next_run_at = int(time.time() * 1000) + (t.retry_interval_seconds or 60) * 1000
                    logger.info("scheduled_task_retry", task_id=task_id, attempt=t.retry_count)
                else:
                    t.next_run_at = None if t.schedule_kind == "once" else await _compute_next(t)

            await db.commit()

        # 终态通知（独立 session，失败不影响任务状态）
        await _notify(task_id, ok=ok, result=result_text, error=error_text, manual=manual)
        # 群定时任务：把结果作为机器人消息推送到目标群
        if ok and result_text:
            await _push_to_room(task_id, result_text)
        # 依赖链：本任务成功后，触发以本任务为前置的下游任务（A → B）
        if ok:
            await _trigger_dependents(task_id)
    except Exception:  # noqa: BLE001
        logger.exception("scheduled_task_execute_error", task_id=task_id)
    finally:
        _RUNNING.discard(task_id)
        _get_sem().release()


async def _push_to_room(task_id: int, result_text: str) -> None:
    """群定时任务：把执行结果以机器人身份发到目标群并广播。"""
    from app.core.db import AsyncSessionLocal
    from app.models import Agent, ChatRoom, ScheduledTask
    from app.services import chat_room_service as S
    from app.services.chat_broadcaster import broadcaster

    try:
        async with AsyncSessionLocal() as db:
            t = await db.get(ScheduledTask, task_id)
            if not t or not t.room_id:
                return
            room = await db.get(ChatRoom, t.room_id)
            if not room or room.status != "active":
                return
            bot_id = t.room_bot_agent_id or t.agent_id
            ag = await db.get(Agent, bot_id)
            if not ag:
                return
            body = (result_text or "").strip()
            if not body:
                return
            msg = await S.post_message(
                db, room=room, sender_id=bot_id, sender_type="agent",
                content=f"【{t.name}】\n{body[:6000]}",
            )
            await db.flush()
            out = await S.serialize_message(db, msg)
            await db.commit()
        await broadcaster.broadcast_room(
            tenant_id=room.tenant_id, room_id=room.id,
            payload={"type": "message", "room_id": room.id, "message": out},
        )
    except Exception:  # noqa: BLE001
        logger.exception("push_to_room_failed", task_id=task_id)


async def _trigger_dependents(task_id: int) -> None:
    """本任务成功后，触发所有「以本任务为前置且启用」的下游任务。"""
    from sqlalchemy import select

    from app.core.db import AsyncSessionLocal
    from app.models import ScheduledTask

    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(ScheduledTask).where(
                    ScheduledTask.depends_on_task_id == task_id,
                    ScheduledTask.enabled.is_(True),
                )
            )).scalars().all()
            ids = [t.id for t in rows]
        for did in ids:
            if did in _RUNNING:
                continue
            logger.info("scheduled_task_dependent_triggered", upstream=task_id, downstream=did)
            from app.tasks.bg import spawn
            spawn("sched_dep", execute_task(did))
    except Exception:  # noqa: BLE001
        logger.exception("trigger_dependents_failed", task_id=task_id)



async def _do_run(db, t) -> tuple[bool, str, str, int | None, list[dict] | None]:
    """真正执行任务体。返回 (成功, 结果文本, 错误文本, workflow_run_id, 附件)。

    不创建会话、不落 Message：结果只写进 ScheduledTaskRun（在 execute_task 落库）。

    审批暂停以 _SchedulerPaused 抛出（而非返回失败），让调用方区分
    「需要人处理」与「执行出错」。
    """
    from app.agents.runner import AgentRunner
    from app.agents.workflow.engine import NodeContext, WorkflowPaused, execute_graph
    from app.middleware.auth_dep import get_user_permission_codes, load_principal_set
    from app.models import Agent, User, Workflow, WorkflowRun

    owner = await db.get(User, t.owner_id)
    agent = await db.get(Agent, t.agent_id)
    if not owner or not agent:
        return False, "", "目标不存在", None
    pset = await load_principal_set(db, owner)
    perms = await get_user_permission_codes(db, owner)

    try:
        if t.target_type == "workflow":
            wf = (await db.execute(
                select(Workflow).where(Workflow.agent_id == t.agent_id).order_by(Workflow.id.desc())
            )).scalars().first()
            if not wf or not wf.graph:
                raise ValueError("工作流未配置")
            inputs = t.inputs or {"query": t.prompt or t.name}
            run = WorkflowRun(
                tenant_id=t.tenant_id, workflow_id=wf.id, agent_id=t.agent_id, user_id=t.owner_id,
                conversation_id=None, status="running", input=inputs, graph_snapshot=wf.graph,
                created_at=int(time.time() * 1000),
            )
            db.add(run); await db.commit(); run_id = run.id
            ctx = NodeContext(
                db=db, ps=pset, run_id=run_id, tenant_id=t.tenant_id, inputs=inputs,
                default_model_config_id=agent.model_config_id, default_kb_ids=agent.kb_ids,
                default_system=agent.system_prompt, default_temperature=(agent.config or {}).get("temperature"),
                perms=perms,
            )
            final, _records = await execute_graph(wf.graph, inputs, ctx)
            r2 = await db.get(WorkflowRun, run_id)
            r2.status = "success"; r2.output = final; r2.finished_at = int(time.time() * 1000)
            if ctx.usage and (ctx.usage.get("calls") or 0) > 0:
                r2.usage = dict(ctx.usage)
            text = str(final.get("output", final)) if isinstance(final, dict) else str(final)
            attachments = await _collect_artifacts(db, final, t.tenant_id)
            return True, text, "", run_id, attachments
        else:
            # 不建会话、不落 Message：无人值守地跑一次提示词
            # 群定时任务（room_id 非空）：注入房间作用域群管工具，让机器人能真正执行群管理
            room_tools = None
            if t.room_id:
                from app.agents.tools.chat_room_tools import build_room_tools

                room_tools = build_room_tools(t.room_id)
            runner = AgentRunner(
                db, agent=agent, ps=pset, conversation=None, history=[], perms=perms,
                summary=None, persist=False,
                room_id=t.room_id, extra_tools=room_tools,
            )
            text = ""
            async for evt in runner.run(t.prompt or t.name):
                if evt.get("type") == "delta":
                    text += evt.get("text", "")
            return True, text, "", None, []
    except WorkflowPaused as p:
        # 定时任务里撞到审批节点：不是失败，是「等人处理」。
        # 必须单独捕获 —— WorkflowPaused 继承 Exception，落进下面的通用 except
        # 会被记成执行失败，且 WorkflowRun 永远停在 running。
        await _mark_paused(db, t, p)
        raise _SchedulerPaused(p.node_id, p.info or {}) from p
    except Exception as e:  # noqa: BLE001
        logger.exception("scheduled_task_run_failed", task_id=t.id)
        return False, "", str(e)[:1000], None, []


class _SchedulerPaused(Exception):
    """内部信号：定时任务的工作流停在审批节点。

    与「执行失败」区分开，让 execute_task 把 ScheduledTaskRun 记成 waiting
    而不是 failed，并在结果里提示需要人工审批。
    """

    def __init__(self, node_id: str, info: dict) -> None:
        super().__init__(f"等待审批: {node_id}")
        self.node_id = node_id
        self.info = info


async def _mark_paused(db, t, p) -> None:
    """把该任务最新的 WorkflowRun 置为 waiting 并保存变量快照。"""
    from sqlalchemy import select as _select

    from app.models import WorkflowRun

    try:
        run = (
            await db.execute(
                _select(WorkflowRun)
                .where(WorkflowRun.agent_id == t.agent_id)
                .order_by(WorkflowRun.id.desc())
                .limit(1)
            )
        ).scalars().first()
        if run is not None and run.status == "running":
            run.status = "waiting"
            run.pending_node_id = p.node_id
            run.state = {"variables": p.variables or {}}
            run.finished_at = int(time.time() * 1000)
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("scheduler_paused_mark_failed", task_id=t.id)


async def _collect_artifacts(db, final, tenant_id: int) -> list[dict]:
    """从工作流最终输出里递归找出 artifact_id/file_key，查 Artifact 组附件。"""
    from sqlalchemy import select

    from app.models import Artifact

    ids: set[int] = set()
    keys: set[str] = set()

    def _walk(o):
        if isinstance(o, dict):
            if o.get("artifact_id"):
                try:
                    ids.add(int(o["artifact_id"]))
                except (TypeError, ValueError):
                    pass
            if o.get("file_key") and isinstance(o.get("file_key"), str):
                keys.add(o["file_key"])
            for v in o.values():
                _walk(v)
        elif isinstance(o, list):
            for v in o:
                _walk(v)

    _walk(final)
    if not ids and not keys:
        return []
    conds = []
    if ids:
        conds.append(Artifact.id.in_(ids))
    if keys:
        conds.append(Artifact.file_key.in_(keys))
    from sqlalchemy import or_

    rows = (await db.execute(select(Artifact).where(Artifact.tenant_id == tenant_id, or_(*conds)))).scalars().all()
    return [{"file_key": a.file_key, "name": a.file_name, "mime": a.mime, "size": a.size} for a in rows]


async def _notify(task_id: int, *, ok: bool, result: str, error: str, manual: bool = False) -> None:
    """终态通知：按任务通知开关（notify_on）决定是否发，附结果摘要。"""
    from app.core.db import AsyncSessionLocal
    from app.models import ScheduledTask, ScheduledTaskRun
    from app.notifiers.base import NotificationMessage
    from app.notifiers.registry import dispatch

    try:
        async with AsyncSessionLocal() as db:
            t = await db.get(ScheduledTask, task_id)
            if not t:
                return
            mode = t.notify_on or "fail"
            if mode == "never":
                return
            if mode == "success" and not ok:
                return
            if mode == "fail" and ok:
                return
            prefix = "手动运行" if manual else "定时任务"
            # 取最近一条执行记录补充耗时/结果摘要
            last_run = (await db.execute(
                select(ScheduledTaskRun).where(ScheduledTaskRun.task_id == task_id)
                .order_by(ScheduledTaskRun.id.desc()).limit(1)
            )).scalars().first()
            summary = (result[:400] if ok else error[:400]) or ""
            # 附件：把本轮产物读成 bytes 供邮件/外部渠道发送
            atts: list[dict] = []
            if last_run and last_run.attachments:
                from app.ingest.storage import get_storage

                storage = get_storage()
                for a in last_run.attachments[:5]:
                    try:
                        atts.append({"name": a.get("name") or "file",
                                     "mime": a.get("mime") or "application/octet-stream",
                                     "data": storage.read(a["file_key"])})
                    except Exception:  # noqa: BLE001
                        continue
            msg = NotificationMessage(
                title=f"{prefix}「{t.name}」{'执行成功' if ok else '执行失败'}",
                body=summary,
                level="success" if ok else "error",
                kind="task", link="/scheduled", ref_type="scheduled_task", ref_id=task_id,
                user_id=t.owner_id,
                attachments=atts or None,
                meta={
                    "task_id": task_id, "task_name": t.name,
                    "status": "success" if ok else "failed",
                    "duration_ms": last_run.duration_ms if last_run else None,
                    "run_id": last_run.id if last_run else None,
                    "attachment_count": len(atts),
                },
            )
            await dispatch(db, tenant_id=t.tenant_id, msg=msg, user_id=t.owner_id)
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("scheduled_task_notify_error", task_id=task_id)


async def scheduler_loop(interval_seconds: int = 30) -> None:
    """后台轮询：到点任务 CAS 抢占后独立执行。"""
    from app.core.db import AsyncSessionLocal
    from app.models import ScheduledTask

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            async with AsyncSessionLocal() as db:
                now = int(time.time() * 1000)
                rows = (await db.execute(
                    select(ScheduledTask).where(
                        ScheduledTask.enabled.is_(True),
                        ScheduledTask.trigger_kind == "schedule",
                        ScheduledTask.next_run_at.is_not(None),
                        ScheduledTask.next_run_at <= now,
                    )
                )).scalars().all()
                for t in rows:
                    if t.id in _RUNNING:
                        continue
                    # once 任务派发后清空 next_run_at，避免每轮重复触发。
                    # 其余任务以「本次计划时刻」为基准递推：这样服务宕机错过的触发点会自然
                    # 递增（catch-up），而不是全部被跳过。
                    planned = t.next_run_at
                    nxt = None if t.schedule_kind == "once" else await _compute_next(t, after_ms=planned)
                    # 若算出的下次仍早于现在（宕机期间积压多个周期），直接推进到当前之后，
                    # 只补跑一次（避免一次性风暴式补跑）
                    now_ms = int(time.time() * 1000)
                    guard = 0
                    while nxt is not None and nxt <= now_ms and guard < 1000:
                        nxt = await _compute_next(t, after_ms=nxt)
                        guard += 1
                    # CAS 抢占：只有把 next_run_at 从旧值改成功才派发
                    res = await db.execute(
                        update(ScheduledTask)
                        .where(ScheduledTask.id == t.id, ScheduledTask.next_run_at == t.next_run_at)
                        .values(next_run_at=nxt, last_status="running")
                    )
                    if res.rowcount == 1:
                        await db.commit()
                        from app.tasks.bg import spawn
                        spawn("sched_due", execute_task(t.id))
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("scheduler_loop_error")
