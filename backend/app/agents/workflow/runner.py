"""工作流流式执行器：边执行边产出事件（与 SSE 连接解耦，客户端断开仍跑完）。

被 chat 侧（agents.py）与编排页运行（workflow.py）共用，消除两份重复逻辑。
"""
from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.workflow.engine import NodeContext, WorkflowPaused, execute_graph
from app.core.logging import get_logger
from app.services.permission import PrincipalSet

logger = get_logger("workflow")

# 持有后台任务引用，避免被 GC
_BG_TASKS: set = set()
_DONE = "__wf_done__"


# ---------------------------------------------------------------- 渠道场景


def start_input_name(graph: dict) -> str:
    """工作流声明的第一个输入名（渠道消息要填进去的那个）。

    多数工作流用 `query`；这里读 start 节点声明，避免硬编码导致消息丢进空变量。
    """
    start = next((n for n in (graph.get("nodes") or []) if n.get("type") == "start"), None)
    if not start:
        return "query"
    ins = (start.get("data") or {}).get("inputs") or []
    if ins and isinstance(ins[0], dict):
        return str(ins[0].get("name") or "query")
    return "query"


async def build_channel_inputs(
    db, *, graph: dict, query: str, conversation=None, attachments: list[dict] | None = None,
) -> dict:
    """把渠道消息组装成工作流 start 输入。

    除了消息正文，额外注入渠道专属入参（start 声明了同名输入即可直接引用）：
    - `history`：最近几轮对话的纯文本摘要，供「刚才那个订单」之类的指代使用
    - `attachments`：[{name, mime, size, url}]，供附件处理节点使用

    未声明的输入会被 _exec_start 忽略（它只读 start 声明的名字），所以多给不会报错。
    """
    from app.services.context_service import load_context

    name = start_input_name(graph)
    inputs: dict = {name: query}

    history_text = ""
    if conversation is not None:
        try:
            summary, history = await load_context(db, conversation, limit=8)
            lines: list[str] = []
            if summary:
                lines.append(summary)
            for m in history:
                role = "客户" if m.role == "user" else "客服"
                content = (m.content or "").strip()
                if content:
                    lines.append(f"{role}：{content[:300]}")
            history_text = "\n".join(lines)
        except Exception:  # noqa: BLE001
            logger.warning("workflow_channel_history_failed")

    inputs["history"] = history_text
    inputs["attachments"] = attachments or []
    return inputs


# 节点类型 → 给客户看的进度文案（不要暴露内部节点名）
_NODE_LABELS: dict[str, str] = {
    "llm": "正在思考",
    "summarize": "正在整理信息",
    "knowledge_retrieval": "正在查询资料",
    "http": "正在查询",
    "http_form": "正在提交",
    "webhook": "正在同步",
    "table": "正在汇总数据",
    "mapping": "正在整理结果",
    "json_path": "正在解析结果",
    "transform": "正在处理数据",
    "collection": "正在合并数据",
    "batch": "正在分批处理",
    "text_template": "正在生成回复",
    "email": "正在发送邮件",
    "notify": "正在推送消息",
    "agent": "正在处理",
    "approval": "等待人工确认",
    "loop": "正在批量处理",
    "delay": "请稍候",
}


async def run_workflow_for_channel(
    *,
    graph: dict,
    query: str,
    run_id: int,
    tenant_id: int,
    ps,
    db=None,
    conversation=None,
    attachments: list[dict] | None = None,
    default_model_config_id: int | None = None,
    default_kb_ids: list[int] | None = None,
    default_system: str | None = None,
    default_temperature: float | None = None,
    perms: set[str] | None = None,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> dict:
    """在渠道里执行一次工作流，返回 {text, err, paused, node_id, run_id, final}。

    与 stream_workflow 的区别：
    - 输入按渠道场景组装（消息 + 历史 + 附件）
    - 支持 on_progress 回调，把节点进度翻译成给客户看的文案
    - WorkflowRun/NodeRun 照常落库，便于后台排查
    """
    from app.core.db import AsyncSessionLocal
    from app.models import NodeRun, WorkflowRun

    # 会话历史需要在调用方的 session 上读（调用方可能已在事务中）
    if db is not None:
        inputs = await build_channel_inputs(
            db, graph=graph, query=query, conversation=conversation, attachments=attachments,
        )
    else:
        async with AsyncSessionLocal() as hdb:
            inputs = await build_channel_inputs(
                hdb, graph=graph, query=query, conversation=conversation, attachments=attachments,
            )

    t0 = time.time()
    err_msg: str | None = None
    final_out: dict = {}
    paused = None
    records: list[dict] = []

    async def emit(evt: dict) -> None:
        if not on_progress or evt.get("type") != "node_started":
            return
        label = _NODE_LABELS.get(evt.get("node_type") or "", "")
        if not label:
            return
        try:
            await on_progress(f"{label}…")
        except Exception:  # noqa: BLE001
            pass

    async with AsyncSessionLocal() as wdb:
        ctx = NodeContext(
            db=wdb, ps=ps, run_id=run_id, tenant_id=tenant_id, inputs=inputs, emit=emit,
            default_model_config_id=default_model_config_id, default_kb_ids=default_kb_ids,
            default_system=default_system, default_temperature=default_temperature,
            perms=perms,
        )
        try:
            final_out, records = await execute_graph(graph, inputs, ctx)
        except WorkflowPaused as p:
            paused = p
        except Exception as e:  # noqa: BLE001
            err_msg = str(e)[:300]

        latency_ms = int((time.time() - t0) * 1000)

        try:
            run = await wdb.get(WorkflowRun, run_id)
            if run is not None:
                if paused is not None:
                    run.status = "waiting"
                    run.pending_node_id = paused.node_id
                    run.state = {"variables": paused.variables or _vars_from_records(records)}
                elif err_msg:
                    run.status = "failed"
                    run.error_msg = err_msg
                else:
                    run.status = "success"
                    run.output = final_out
                run.latency_ms = latency_ms
                run.finished_at = int(time.time() * 1000)
                if ctx.usage and (ctx.usage.get("calls") or 0) > 0:
                    run.usage = dict(ctx.usage)
            for rr in (paused.records if paused is not None else records):
                wdb.add(NodeRun(
                    tenant_id=tenant_id, run_id=run_id, node_id=rr["node_id"],
                    node_type=rr["node_type"], seq=rr["seq"], status=rr["status"],
                    input=rr.get("input"), output=rr.get("output"), error_msg=rr.get("error_msg"),
                    retry_count=rr.get("retry_count", 0), latency_ms=rr.get("latency_ms", 0),
                    created_at=int(time.time() * 1000),
                ))
            await wdb.commit()
        except Exception:  # noqa: BLE001
            await wdb.rollback()
            logger.exception("workflow_channel_finalize_failed", run_id=run_id)

    if paused is not None:
        info = paused.info or {}
        title = info.get("title") or paused.node_id
        return {
            "text": f"已提交人工处理（{title}），客服确认后我们会继续为你处理。",
            "err": None, "paused": True, "node_id": paused.node_id,
            "run_id": run_id, "final": {},
        }

    text = str(final_out.get("output", final_out)) if isinstance(final_out, dict) else str(final_out)
    if err_msg:
        text = f"处理失败：{err_msg}"
    return {"text": text, "err": err_msg, "paused": False, "run_id": run_id, "final": final_out}


def _vars_from_records(records: list[dict]) -> dict:
    """从节点记录重建 resume 变量（node_id → output），仅取执行成功且有输出的节点。"""
    import json

    out: dict = {}
    for r in records:
        if r.get("status") != "success":
            continue
        val = r.get("output")
        try:
            json.dumps(val)
            out[r["node_id"]] = val
        except (TypeError, ValueError):
            out[r["node_id"]] = str(val)[:5000]
    return out


async def stream_workflow(
    *,
    graph: dict,
    inputs: dict,
    run_id: int,
    tenant_id: int,
    ps: PrincipalSet,
    default_model_config_id: int | None = None,
    default_kb_ids: list[int] | None = None,
    default_system: str | None = None,
    default_temperature: float | None = None,
    approval: dict | None = None,
    resume: dict | None = None,
    perms: set[str] | None = None,
    on_terminal: Callable[[AsyncSession, dict], Awaitable[None]] | None = None,
    timeout_seconds: int | None = None,
) -> AsyncIterator[dict]:
    """执行工作流并逐事件 yield（node_started/node_finished/run_finished）。

    - 独立 session + 后台 task：客户端断开不中断、终态必落库。
    - on_terminal(db, info)：落库后回调（chat 侧用它写 assistant Message）。
    - approval：审批决策 {node_id: "approve"|"reject"}；resume：{variables}。
    - timeout_seconds：整条链的执行预算（含所有节点），超时即中止。
      单节点各有超时，但没有整体上限时 loop + LLM 可以跑几十分钟。
    """
    from app.agents.workflow import cancel as wf_cancel
    from app.core.db import AsyncSessionLocal
    from app.models import NodeRun, WorkflowRun

    queue: asyncio.Queue = asyncio.Queue()

    async def _run():
        async with AsyncSessionLocal() as wdb:
            async def emit(evt: dict):
                await queue.put(evt)

            ctx = NodeContext(
                db=wdb, ps=ps, run_id=run_id, tenant_id=tenant_id, inputs=inputs, emit=emit,
                default_model_config_id=default_model_config_id, default_kb_ids=default_kb_ids,
                default_system=default_system, default_temperature=default_temperature,
                approval=approval, perms=perms,
            )
            t0 = time.time()
            err_msg = None
            final_out: dict = {}
            records: list[dict] = []
            paused: WorkflowPaused | None = None
            cancelled = False
            try:
                with_timeout = execute_graph(graph, inputs, ctx, resume=resume)
                if timeout_seconds and timeout_seconds > 0:
                    final_out, records = await asyncio.wait_for(
                        with_timeout, timeout=timeout_seconds
                    )
                else:
                    final_out, records = await with_timeout
            except asyncio.TimeoutError:
                err_msg = f"工作流执行超时（超过 {timeout_seconds} 秒）"
                await queue.put({"type": "run_timeout", "run_id": run_id,
                                 "timeout_seconds": timeout_seconds})
            except asyncio.CancelledError:
                cancelled = True
                err_msg = "运行已被取消"
            except WorkflowPaused as p:
                paused = p
            except Exception as e:  # noqa: BLE001
                err_msg = str(e)[:300]
            latency_ms = int((time.time() - t0) * 1000)

            # 审批暂停：置 waiting，存变量快照，SSE 发 approval_required
            if paused is not None:
                try:
                    r2 = await wdb.get(WorkflowRun, run_id)
                    r2.status = "waiting"
                    r2.pending_node_id = paused.node_id
                    r2.state = {"variables": paused.variables or _vars_from_records(records)}
                    r2.finished_at = int(time.time() * 1000)
                    for rr in paused.records:
                        wdb.add(NodeRun(
                            tenant_id=tenant_id, run_id=run_id, node_id=rr["node_id"],
                            node_type=rr["node_type"], seq=rr["seq"], status=rr["status"],
                            input=rr.get("input"), output=rr.get("output"), error_msg=rr.get("error_msg"),
                            retry_count=rr.get("retry_count", 0), latency_ms=rr.get("latency_ms", 0),
                            created_at=int(time.time() * 1000),
                        ))
                    await wdb.commit()
                except Exception:  # noqa: BLE001
                    await wdb.rollback()
                await queue.put({"type": "approval_required", "node_id": paused.node_id, "run_id": run_id, **paused.info})
                await queue.put({"type": _DONE, "paused": True, "run_id": run_id,
                                 "status": "waiting", "node_id": paused.node_id})
                return

            out_text = str(final_out.get("output", final_out)) if isinstance(final_out, dict) else str(final_out)
            if err_msg:
                out_text = f"[工作流执行失败] {err_msg}"
            final_status = "canceled" if cancelled else ("failed" if err_msg else "success")
            try:
                r2 = await wdb.get(WorkflowRun, run_id)
                if cancelled:
                    r2.status = "canceled"
                    r2.error_msg = out_text[:300]
                elif err_msg:
                    r2.status = "failed"; r2.error_msg = err_msg
                else:
                    r2.status = "success"; r2.output = final_out
                r2.latency_ms = latency_ms
                r2.finished_at = int(time.time() * 1000)
                if ctx.usage and (ctx.usage.get("calls") or 0) > 0:
                    r2.usage = dict(ctx.usage)
                for rr in records:
                    wdb.add(NodeRun(
                        tenant_id=tenant_id, run_id=run_id, node_id=rr["node_id"],
                        node_type=rr["node_type"], seq=rr["seq"], status=rr["status"],
                        input=rr.get("input"), output=rr.get("output"), error_msg=rr.get("error_msg"),
                        retry_count=rr.get("retry_count", 0), latency_ms=rr.get("latency_ms", 0),
                        created_at=int(time.time() * 1000),
                    ))
                if on_terminal:
                    await on_terminal(wdb, {"text": out_text, "err": err_msg, "run_id": run_id, "final": final_out})
                await wdb.commit()
            except Exception:  # noqa: BLE001
                await wdb.rollback()
                logger.exception("workflow_finalize_failed", run_id=run_id)
            # 事件触发：工作流运行完成（供定时任务/其他流程消费）
            try:
                from app.tasks.event_bus import publish

                wf_id = None
                r3 = await wdb.get(WorkflowRun, run_id)
                if r3:
                    wf_id = r3.workflow_id
                await publish(
                    "workflow.completed" if not err_msg else "workflow.failed",
                    tenant_id=tenant_id,
                    payload={"run_id": run_id, "workflow_id": wf_id, "status": final_status},
                )
            except Exception:  # noqa: BLE001
                logger.exception("workflow_event_publish_failed", run_id=run_id)
            await queue.put({
                "type": _DONE, "err": err_msg, "text": out_text, "run_id": run_id,
                "status": final_status, "output": final_out,
            })
            # 已把终态落库并通知调用方；此时要真正结束协程。
            # 吞掉 CancelledError 会让 asyncio 认为任务正常结束，取消信号失效。
            if cancelled:
                raise asyncio.CancelledError()

    async def _tracked_run():
        """登记到取消注册表，让「停止」按钮能真正中断执行。"""
        async with wf_cancel.track(run_id):
            await _run()

    bg = asyncio.create_task(_tracked_run())
    _BG_TASKS.add(bg)
    bg.add_done_callback(_BG_TASKS.discard)

    while True:
        evt = await queue.get()
        etype = evt.get("type") if isinstance(evt, dict) else "node"
        if etype == _DONE:
            # 对外统一暴露为 run_finished（内部哨兵不外泄）
            evt = {**evt, "type": "run_finished"}
            yield evt
            break
        yield evt
