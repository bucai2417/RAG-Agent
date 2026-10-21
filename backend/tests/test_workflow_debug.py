"""工作流调试对话：输入组装、轨迹返回、审批暂停、错误处理。"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.agents.workflow.engine import (
    NodeContext,
    WorkflowPaused,
    execute_graph,
)
from app.agents.workflow.runner import start_input_name
from app.services.permission import PrincipalSet


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _graph(nodes, edges):
    return {"nodes": nodes, "edges": edges}


# ============================================================ 调试输入组装语义
# 调试接口复用 run 的输入约定：消息进 start 第一个输入，history 单独注入。
# 这里直接验证引擎层行为，避免依赖 HTTP 与数据库。


def test_debug_input_fills_declared_start_input():
    g = {"nodes": [{"type": "start", "data": {"inputs": [{"name": "message"}]}}]}
    assert start_input_name(g) == "message"


def test_debug_passes_history_to_variable():
    """history 作为独立输入注入，start 声明后可在下游引用。"""
    g = _graph(
        [
            {"id": "s", "type": "start", "data": {"inputs": [{"name": "query"}, {"name": "history"}]}},
            {"id": "e", "type": "end", "data": {"output": "q={{s.query}}|h={{s.history}}"}},
        ],
        [{"source": "s", "target": "e"}],
    )

    async def _t():
        inputs = {"query": "那它呢", "history": "客户：订单 A123\n客服：已发货"}
        ctx = NodeContext(db=None, ps=PrincipalSet(user_id=1, tenant_id=1), run_id=0, tenant_id=1)
        final, _ = await execute_graph(g, inputs, ctx)
        assert "q=那它呢" in final["output"]
        assert "订单 A123" in final["output"]

    _run(_t())


# ============================================================ 轨迹采集


def test_trace_captures_node_input_output():
    """调试接口靠 node_finished 事件收集轨迹；这里验证事件内容足够重建路径。"""
    g = _graph(
        [
            {"id": "s", "type": "start", "data": {"inputs": [{"name": "query"}]}},
            {"id": "t", "type": "transform", "data": {"value": "{{s.query}}", "op": "to_text"}},
            {"id": "e", "type": "end", "data": {"output": "{{t.output}}"}},
        ],
        [{"source": "s", "target": "t"}, {"source": "t", "target": "e"}],
    )
    events: list[dict] = []

    async def emit(evt):
        events.append(evt)

    async def _t():
        ctx = NodeContext(
            db=None, ps=PrincipalSet(user_id=1, tenant_id=1), run_id=0, tenant_id=1, emit=emit,
        )
        await execute_graph(g, {"query": "hello"}, ctx)

    _run(_t())
    finished = [e for e in events if e["type"] == "node_finished"]
    ids = [e["node_id"] for e in finished]
    assert ids == ["s", "t", "e"], "轨迹应包含全部执行节点且有序"
    for e in finished:
        assert "input" in e and "output" in e
        assert "latency_ms" in e


def test_trace_marks_failed_node():
    g = _graph(
        [
            {"id": "s", "type": "start", "data": {"inputs": [{"name": "q"}]}},
            {"id": "tr", "type": "transform", "data": {"value": "x", "op": "extract", "pattern": "["}},
            {"id": "e", "type": "end", "data": {"output": "{{tr.output}}"}},
        ],
        [{"source": "s", "target": "tr"}, {"source": "tr", "target": "e"}],
    )
    events: list[dict] = []

    async def emit(evt):
        events.append(evt)

    async def _t():
        ctx = NodeContext(
            db=None, ps=PrincipalSet(user_id=1, tenant_id=1), run_id=0, tenant_id=1, emit=emit,
        )
        with pytest.raises(Exception):
            await execute_graph(g, {"q": "x"}, ctx)

    _run(_t())
    failed = [e for e in events if e["type"] == "node_finished" and e.get("status") == "failed"]
    assert failed and failed[0]["node_id"] == "tr"
    assert failed[0].get("error")


def test_trace_stops_at_approval():
    """审批节点暂停时，后面的节点不应出现在轨迹里。"""
    g = _graph(
        [
            {"id": "s", "type": "start", "data": {"inputs": [{"name": "q"}]}},
            {"id": "ap", "type": "approval", "data": {"title": "确认"}},
            {"id": "e", "type": "end", "data": {"output": "ok"}},
        ],
        [{"source": "s", "target": "ap"}, {"source": "ap", "target": "e"}],
    )
    events: list[dict] = []
    paused = None

    async def emit(evt):
        events.append(evt)

    async def _t():
        nonlocal paused
        ctx = NodeContext(
            db=None, ps=PrincipalSet(user_id=1, tenant_id=1), run_id=0, tenant_id=1, emit=emit,
        )
        try:
            await execute_graph(g, {"q": "x"}, ctx)
        except WorkflowPaused as p:
            paused = p

    _run(_t())
    assert paused is not None and paused.node_id == "ap"
    ids = [e["node_id"] for e in events if e["type"] == "node_finished"]
    assert "e" not in ids, "暂停后不应执行 end"


def test_trace_path_for_condition_branch():
    """条件分支：只有命中的分支节点出现在轨迹里，可用于反推走过的边。"""
    g = _graph(
        [
            {"id": "s", "type": "start", "data": {"inputs": [{"name": "flag"}]}},
            {"id": "c", "type": "condition", "data": {"expression": "{{s.flag}} == 'yes'"}},
            {"id": "a", "type": "transform", "data": {"value": "A", "op": "to_text"}},
            {"id": "b", "type": "transform", "data": {"value": "B", "op": "to_text"}},
            {"id": "e", "type": "end", "data": {"output": "{{a.output}}"}},
        ],
        [
            {"source": "s", "target": "c"},
            {"source": "c", "target": "a", "sourceHandle": "true"},
            {"source": "c", "target": "b", "sourceHandle": "false"},
            {"source": "a", "target": "e"},
            {"source": "b", "target": "e"},
        ],
    )
    events: list[dict] = []

    async def emit(evt):
        events.append(evt)

    async def _t():
        ctx = NodeContext(
            db=None, ps=PrincipalSet(user_id=1, tenant_id=1), run_id=0, tenant_id=1, emit=emit,
        )
        await execute_graph(g, {"flag": "yes"}, ctx)

    _run(_t())
    done = {e["node_id"]: e.get("status") for e in events if e["type"] == "node_finished"}
    assert done.get("a") == "success"
    assert "b" not in done or done["b"] == "skipped", "未命中的分支不应成功"


# ============================================================ 接口注册


def test_debug_endpoint_registered():
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/api/v1/agents/{agent_id}/workflow/debug" in paths
