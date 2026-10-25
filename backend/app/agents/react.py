"""共享 ReAct 工具循环：AgentRunner 与问答页（chat_service）复用同一套工具调用逻辑。

产出统一事件流（yield dict），由 API 层转 SSE：
  delta | reasoning | tool_call | tool_result | citations | usage | pending_action | _suspended | turn
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator, Awaitable, Callable

from app.agents.tools.base import ToolContext, ToolResult
from app.agents.tools.registry import to_openai_schema, tool_allowed
from app.core.logging import get_logger
from app.providers.base import ChatMessage, ToolCall, ToolCallDelta

logger = get_logger("agent.react")

TOOL_TIMEOUT = 60


def _finalize_tool_calls(acc: dict[int, dict]) -> list[ToolCall]:
    out = []
    for _, s in sorted(acc.items()):
        out.append(ToolCall(
            id=s.get("id") or f"call_{_}",
            name=s.get("name") or "",
            arguments=s.get("arguments") or "{}",
        ))
    return out


def _accumulate(acc: dict[int, dict], deltas: list[ToolCallDelta]) -> None:
    for d in deltas:
        slot = acc.setdefault(d.index, {"id": "", "name": "", "arguments": ""})
        if d.id:
            slot["id"] = d.id
        if d.name:
            slot["name"] = d.name
        slot["arguments"] += d.arguments_delta


async def run_react_loop(
    *,
    db,
    ps,
    perms: set[str],
    llm,
    rm,
    messages: list[ChatMessage],
    tools: list,
    conversation_id: int | None = None,
    agent_id: int | None = None,
    room_id: int | None = None,
    tool_config: dict | None = None,
    dynamic_tools: dict[str, object] | None = None,
    temperature: float = 0.3,
    max_turns: int = 0,
    budget_seconds: int = 0,
    allow_auto_write: bool = False,
    on_audit: Callable[..., Awaitable[None]] | None = None,
    on_pending_action: Callable[[object, ToolCall, dict], Awaitable[object]] | None = None,
) -> AsyncIterator[dict]:
    """多轮工具循环。write 工具：有 on_pending_action 则挂起（HITL），否则 allow_auto_write 时直执行。

    dynamic_tools：待命工具池（name→Tool）。它们**不**随初始 schema 下发；
    当某次工具结果 data.enable_tools 命中时，按需启用（下一轮 LLM 可见），用于省 token。
    默认 None → 行为与不带该参数完全一致（向后兼容）。

    max_turns：**<= 0 表示不限制轮次**（默认）。浏览器自动化这类多步任务
    （snapshot → click → snapshot → fill → …）不该被固定轮数腰斩。
    不限制不等于放任：另有两道兜底——连续两轮相同调用即终止；
    整体时间预算 budget_seconds（<=0 时取 settings.agent_loop_budget_seconds）到点强制收尾。

    返回事件流；调用方负责落库助手消息。
    """
    tool_by_name = {t.name: t for t in tools}
    openai_tools = to_openai_schema(tools)
    last_sig: str | None = None
    repeat = 0
    exhausted = True  # 轮次耗尽（未自然结束）

    if budget_seconds <= 0:
        from app.core.config import settings as _settings

        budget_seconds = int(getattr(_settings, "agent_loop_budget_seconds", 900) or 0)
    deadline = (time.time() + budget_seconds) if budget_seconds > 0 else None

    turn = 0
    while True:
        # 轮次上限：仅当显式配置了正数时才生效（默认不限制）
        if max_turns > 0 and turn >= max_turns:
            break
        # 时间预算：防止无上限轮次把连接和模型配额吃光
        if deadline is not None and time.time() >= deadline:
            yield {"type": "delta", "text": "\n（已达到本次执行时间上限，正在收尾）"}
            break
        turn += 1

        # 进下一轮模型调用前先把连接还给池：上一轮工具只 flush 不 commit，
        # 连接会一直被这条事务占着，而模型生成动辄几十秒。SQLite 池只有 1~3 条，
        # 占满后其它对话直接 503。这里提交的正是「已完成的那一轮工具效果」，
        # 语义上也更诚实：工具确实执行过，就不该因为最终回答没生成而被回滚。
        if db is not None:
            from app.core.db import release_connection

            await release_connection(db)

        acc_tool: dict[int, dict] = {}
        turn_text = ""
        pending_enable: list[str] = []
        stream = await llm.chat(messages, model=rm.model_name, stream=True,
                                temperature=temperature, tools=openai_tools)
        async for chunk in stream:
            if chunk.reasoning:
                yield {"type": "reasoning", "text": chunk.reasoning}
            if chunk.delta:
                turn_text += chunk.delta
                yield {"type": "delta", "text": chunk.delta}
            if chunk.tool_calls:
                _accumulate(acc_tool, chunk.tool_calls)
            if chunk.usage:
                yield {"type": "usage", "usage": chunk.usage}
            if chunk.finish:
                break

        tool_calls = _finalize_tool_calls(acc_tool)
        if not tool_calls:
            exhausted = False  # LLM 不再调工具 → 自然结束
            break

        messages.append(ChatMessage(role="assistant", content=turn_text, tool_calls=tool_calls))

        sig = json.dumps([(tc.name, tc.arguments) for tc in tool_calls], sort_keys=True)
        if sig == last_sig:
            repeat += 1
            if repeat >= 2:
                yield {"type": "delta", "text": "\n（检测到重复调用，已终止）"}
                break
        else:
            repeat = 0
            last_sig = sig

        for tc in tool_calls:
            yield {"type": "tool_call", "id": tc.id, "name": tc.name,
                   "arguments": tc.arguments, "turn": turn}
            tool = tool_by_name.get(tc.name)
            # 解析参数（HITL 判定与执行都要用）
            try:
                call_args = json.loads(tc.arguments or "{}")
            except json.JSONDecodeError:
                call_args = {}
            # 是否需确认：工具若实现 needs_confirm(args) 则以其为准（action 混合型工具可按参数判定），
            # 否则回退到工具级 auto_approve（True=免确认）。
            if tool and hasattr(tool, "needs_confirm"):
                try:
                    _need_confirm = bool(tool.needs_confirm(call_args))
                except Exception:  # noqa: BLE001
                    _need_confirm = not getattr(tool, "auto_approve", False)
            else:
                _need_confirm = not getattr(tool, "auto_approve", False) if tool else False
            if not tool:
                result = ToolResult(content=f"未知工具: {tc.name}", is_error=True)
            elif not tool_allowed(tool, perms):
                result = ToolResult(content=f"无权限调用工具: {tc.name}", is_error=True)
                if on_audit:
                    await on_audit(tc, result, True, autonomy="denied")
            elif (getattr(tool, "kind", "read") == "write"
                  and on_pending_action
                  and _need_confirm):
                # 写操作 → HITL 挂起（auto_approve / needs_confirm=False 的免确认，直接执行）
                args = call_args
                action = await on_pending_action(tool, tc, args)
                yield {
                    "type": "pending_action", "action_id": action.id, "tool_name": tool.name,
                    "arguments": args,
                    "summary": getattr(action, "summary", "") or "",
                    "expires_at": getattr(action, "expires_at", None),
                }
                yield {"type": "_suspended", "action_id": action.id}
                return
            else:
                args = call_args
                # auto_write：本次是「写工具且未经 HITL 确认直接执行」（如用户开免确认 / auto_approve）。
                # 工具据此调整产出（如录单在无人核验时直接落 confirmed）。
                _auto_write = getattr(tool, "kind", "read") == "write"
                ctx = ToolContext(
                    db=db, ps=ps, tenant_id=ps.tenant_id, user_id=ps.user_id,
                    conversation_id=conversation_id, agent_id=agent_id, room_id=room_id,
                    config=((tool_config or {}).get("builtin", {}).get(tc.name) or {}),
                    auto_write=_auto_write,
                )
                _t0 = time.time()
                try:
                    result = await asyncio.wait_for(tool.run(args, ctx), timeout=TOOL_TIMEOUT)
                except asyncio.TimeoutError:
                    result = ToolResult(content="工具执行超时", is_error=True)
                except Exception as e:  # noqa: BLE001
                    result = ToolResult(content=f"工具执行错误: {str(e)[:150]}", is_error=True)
                result.latency_ms = int((time.time() - _t0) * 1000)
                if on_audit:
                    # autonomy：写工具走到这里 = 未经 HITL 直接执行（"自动"）；
                    # 只读工具无副作用，标 "read"。
                    _autonomy = "auto" if getattr(tool, "kind", "read") == "write" else "read"
                    await on_audit(tc, result, False, autonomy=_autonomy)

            yield {
                "type": "tool_result", "id": tc.id, "name": tc.name,
                "content": result.content[:4000], "is_error": result.is_error,
                "data": result.data, "turn": turn,
            }
            # 动态启用：工具结果若声明 enable_tools，收集起来在下一轮生效
            if dynamic_tools and isinstance(result.data, dict):
                for nm in (result.data.get("enable_tools") or []):
                    if nm not in tool_by_name and nm not in pending_enable:
                        pending_enable.append(str(nm))
            if result.citations:
                cites = [c.model_dump() if hasattr(c, "model_dump") else c for c in result.citations]
                yield {"type": "citations", "citations": cites}
            messages.append(ChatMessage(role="tool", content=result.content, tool_call_id=tc.id, name=tc.name))

        # 结算动态启用：本轮请求的工具在"下一轮"LLM 调用时可见（权限二次复核）
        if pending_enable:
            added = False
            for nm in pending_enable:
                cand = dynamic_tools.get(nm) if dynamic_tools else None
                if cand is None:
                    continue
                if not tool_allowed(cand, perms):  # 纵深防御：启用前再复核权限
                    if on_audit:
                        await on_audit(
                            ToolCall(id=f"enable_{nm}", name=nm, arguments="{}"),
                            ToolResult(content="动态启用被拒绝：权限不足", is_error=True), True,
                        )
                    continue
                if nm not in tool_by_name:
                    tool_by_name[nm] = cand
                    added = True
            if added:
                openai_tools = to_openai_schema(list(tool_by_name.values()))
            pending_enable.clear()

        yield {"type": "turn", "turn": turn}

    # 轮次耗尽仍想调工具 → 强制收尾一次（不带工具），避免回答戛然而止
    if exhausted:
        yield {"type": "delta", "text": "\n\n（已达到工具调用上限，以下为基于当前已获取信息的总结）\n"}
        try:
            final = await llm.chat(
                messages + [ChatMessage(
                    role="user",
                    content="请直接基于以上已获得的信息给出最终回答，不要再调用工具。",
                )],
                model=rm.model_name, stream=True, temperature=temperature,
            )
            async for chunk in final:
                if chunk.reasoning:
                    yield {"type": "reasoning", "text": chunk.reasoning}
                if chunk.delta:
                    yield {"type": "delta", "text": chunk.delta}
                if chunk.usage:
                    yield {"type": "usage", "usage": chunk.usage}
                if chunk.finish:
                    break
        except Exception:  # noqa: BLE001
            yield {"type": "delta", "text": "（无法生成总结，请重试或缩小问题范围）"}
