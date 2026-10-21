"""渠道 ↔ 工作流集成：输入组装、渠道执行、回复模式与指令。

不依赖真实渠道与网络：工作流只跑纯数据节点；渠道对象用轻量替身。
"""
from __future__ import annotations

import asyncio

import pytest

from app.agents.workflow.runner import (
    _NODE_LABELS,
    build_channel_inputs,
    run_workflow_for_channel,
    start_input_name,
)
from app.services.permission import PrincipalSet


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ============================================================ 输入名解析


def test_start_input_name_reads_declaration():
    g = {"nodes": [{"type": "start", "data": {"inputs": [{"name": "message"}]}}]}
    assert start_input_name(g) == "message"


def test_start_input_name_defaults_to_query():
    assert start_input_name({"nodes": [{"type": "start", "data": {}}]}) == "query"
    assert start_input_name({"nodes": []}) == "query"
    assert start_input_name({}) == "query"


def test_start_input_name_ignores_malformed_inputs():
    g = {"nodes": [{"type": "start", "data": {"inputs": ["not-a-dict"]}}]}
    assert start_input_name(g) == "query"


# ============================================================ 输入组装


def test_build_channel_inputs_basic():
    g = {"nodes": [{"type": "start", "data": {"inputs": [{"name": "query"}]}}]}

    async def _t():
        inputs = await build_channel_inputs(None, graph=g, query="你好", conversation=None)
        assert inputs["query"] == "你好"
        assert inputs["history"] == ""
        assert inputs["attachments"] == []

    _run(_t())


def test_build_channel_inputs_with_attachments():
    g = {"nodes": [{"type": "start", "data": {"inputs": [{"name": "msg"}]}}]}
    atts = [{"name": "a.pdf", "mime": "application/pdf", "size": 10, "url": "/x"}]

    async def _t():
        inputs = await build_channel_inputs(None, graph=g, query="看下", conversation=None, attachments=atts)
        assert inputs["msg"] == "看下", "应填入声明的输入名而非硬编码 query"
        assert inputs["attachments"] == atts

    _run(_t())


def test_build_channel_inputs_history_from_conversation():
    """有会话时把历史拼成纯文本，供指代消解使用。"""
    from types import SimpleNamespace

    g = {"nodes": [{"type": "start", "data": {"inputs": [{"name": "query"}]}}]}

    class _DB:
        async def execute(self, *a, **k):
            class _R:
                def scalars(self):
                    class _S:
                        def all(self_inner):
                            return [
                                SimpleNamespace(role="assistant", content="订单 A123 已发货"),
                                SimpleNamespace(role="user", content="那个订单到哪了"),
                            ]
                    return _S()
            return _R()

    async def _t():
        conv = SimpleNamespace(id=1, summary=None, summary_upto_message_id=0)
        inputs = await build_channel_inputs(_DB(), graph=g, query="什么时候到", conversation=conv)
        assert "订单 A123 已发货" in inputs["history"]
        assert "客户：那个订单到哪了" in inputs["history"]

    _run(_t())


# ============================================================ 进度文案


def test_progress_labels_cover_common_nodes():
    for t in ("llm", "knowledge_retrieval", "http", "table", "approval"):
        assert t in _NODE_LABELS


def test_progress_labels_are_customer_friendly():
    """进度文案不能暴露内部节点名，客户看不懂 'llm' / 'json_path'。"""
    for label in _NODE_LABELS.values():
        assert label and not label.isascii(), f"进度文案应为中文：{label}"


# ============================================================ 渠道执行工作流


def test_run_workflow_for_channel_success():
    """完整跑一次渠道工作流：消息 → 表格 → 文本 → 输出。"""
    graph = {
        "nodes": [
            {"id": "s", "type": "start",
             "data": {"inputs": [{"name": "query"}, {"name": "history"}]}},
            {"id": "t", "type": "transform",
             "data": {"value": "{{s.query}}", "op": "to_text"}},
            {"id": "e", "type": "end", "data": {"output": "收到：{{t.output}}"}},
        ],
        "edges": [
            {"source": "s", "target": "t"}, {"source": "t", "target": "e"},
        ],
    }
    ps = PrincipalSet(user_id=1, tenant_id=1)

    async def _t():
        r = await run_workflow_for_channel(
            graph=graph, query="查订单", run_id=1, tenant_id=1, ps=ps,
        )
        assert r["err"] is None
        assert "收到：查订单" in r["text"]
        assert r["paused"] is False

    _run(_t())


def test_run_workflow_for_channel_reports_failure():
    """节点抛错时要返回可读失败信息，而不是把异常抛给渠道。"""
    from app.core.errors import ValidationError

    graph = {
        "nodes": [
            {"id": "s", "type": "start", "data": {"inputs": [{"name": "query"}]}},
            {"id": "tr", "type": "transform",
             "data": {"value": "x", "op": "extract", "pattern": "["}},
            {"id": "e", "type": "end", "data": {"output": "{{tr.output}}"}},
        ],
        "edges": [{"source": "s", "target": "tr"}, {"source": "tr", "target": "e"}],
    }
    ps = PrincipalSet(user_id=1, tenant_id=1)

    async def _t():
        r = await run_workflow_for_channel(graph=graph, query="x", run_id=2, tenant_id=1, ps=ps)
        assert r["err"], "应捕获节点异常"
        assert "失败" in r["text"]

    _run(_t())


def test_channel_reply_mode_accepts_workflow():
    from app.api.v1.channels import _norm_choice

    assert _norm_choice("workflow", ("rag", "smart", "agent", "workflow"), "rag") == "workflow"
    assert _norm_choice("bogus", ("rag", "smart", "agent", "workflow"), "rag") == "rag"


def test_run_command_registered():
    from app.channels.commands import COMMANDS

    assert "run" in COMMANDS


def test_help_mentions_run_command():
    from app.channels.commands import HELP_TEXT

    assert "/run" in HELP_TEXT
