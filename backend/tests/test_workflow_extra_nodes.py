"""工作流扩展节点（电商/办公）+ AI 生成：执行器行为与图清洗测试。

不依赖网络与数据库：执行器只做数据变换；AI 生成部分直接测 sanitize/parse。
"""
from __future__ import annotations

import asyncio

import pytest

from app.agents.workflow import nodes_extra as N
from app.agents.workflow.engine import KNOWN_NODE_TYPES, NODE_EXECUTORS, validate_graph
from app.core.errors import ValidationError


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ============================================================ 节点注册


def test_extra_nodes_registered():
    for t in (
        "table", "mapping", "json_path", "transform", "collection", "batch",
        "text_template", "email", "summarize", "router",
        "http_form", "webhook", "delay",
    ):
        assert t in NODE_EXECUTORS, t


def test_node_count():
    assert len(KNOWN_NODE_TYPES) >= 28


def test_extra_meta_covers_all_extra_nodes():
    from app.agents.workflow.nodes_extra import NODE_SPECS_EXTRA

    for t in (
        "table", "mapping", "json_path", "transform", "collection", "batch",
        "text_template", "email", "summarize", "router",
        "http_form", "webhook", "delay",
    ):
        assert t in NODE_SPECS_EXTRA, t


# ============================================================ table


def test_table_filter_sort_aggregate():
    rows = [
        {"name": "A", "amount": "100", "status": "未付款"},
        {"name": "B", "amount": "50", "status": "已付款"},
        {"name": "C", "amount": "200", "status": "未付款"},
    ]
    r = _run(N._exec_table({}, {
        "rows": rows,
        "filters": [{"field": "status", "op": "eq", "value": "未付款"}],
        "sort": {"field": "amount", "desc": True},
        "aggregates": [{"name": "total", "field": "amount", "op": "sum"}],
    }, None))
    assert r["count"] == 2
    assert [x["name"] for x in r["rows"]] == ["C", "A"]
    assert r["summary"]["total"] == 300.0
    assert "C" in r["csv"]


def test_table_field_projection_dot_path():
    rows = [{"order": {"no": "X1", "amt": 9}}, {"order": {"no": "X2", "amt": 8}}]
    r = _run(N._exec_table({}, {
        "rows": rows,
        "fields": [{"name": "单号", "from": "order.no"}, {"name": "金额", "from": "order.amt"}],
    }, None))
    assert r["rows"][0] == {"单号": "X1", "金额": 9}


def test_table_distinct_and_limit():
    rows = [{"k": 1}, {"k": 1}, {"k": 2}, {"k": 3}]
    r = _run(N._exec_table({}, {"rows": rows, "distinct": True, "distinct_field": "k", "limit": 2}, None))
    assert r["count"] == 2


def test_table_rejects_oversized():
    rows = [{"i": i} for i in range(N.MAX_ROWS + 1)]
    with pytest.raises(ValidationError):
        _run(N._exec_table({}, {"rows": rows}, None))


def test_table_accepts_json_string():
    r = _run(N._exec_table({}, {"rows": '[{"a":1},{"a":2}]'}, None))
    assert r["count"] == 2


def test_table_accepts_newline_text():
    r = _run(N._exec_table({}, {"rows": "a\nb\nc"}, None))
    assert r["count"] == 3


def test_table_numeric_sort_not_lexical():
    """字符串数字要按数值排序，否则 '10' 会排在 '9' 前面。"""
    rows = [{"v": "9"}, {"v": "10"}, {"v": "2"}]
    r = _run(N._exec_table({}, {"rows": rows, "sort": {"field": "v", "desc": True}}, None))
    assert [x["v"] for x in r["rows"]] == ["10", "9", "2"]


# ============================================================ transform


def test_transform_extract_all():
    r = _run(N._exec_transform({}, {
        "value": "订单 A123 与 B456", "op": "extract", "pattern": r"[A-Z]\d+", "all": True,
    }, None))
    assert r["output"] == ["A123", "B456"]


def test_transform_extract_single_is_scalar():
    r = _run(N._exec_transform({}, {"value": "订单 A123", "op": "extract", "pattern": r"[A-Z]\d+"}, None))
    assert r["output"] == "A123"


def test_transform_to_number_strips_currency():
    r = _run(N._exec_transform({}, {"value": "￥1,299.50", "op": "to_number"}, None))
    assert r["output"] == 1299.5


def test_transform_to_number_no_digits():
    r = _run(N._exec_transform({}, {"value": "无金额", "op": "to_number"}, None))
    assert r["output"] is None


def test_transform_invalid_regex():
    with pytest.raises(ValidationError):
        _run(N._exec_transform({}, {"value": "x", "op": "extract", "pattern": "["}, None))


def test_transform_split_join():
    r = _run(N._exec_transform({}, {"value": "a,b,c", "op": "split", "separator": ","}, None))
    assert r["output"] == ["a", "b", "c"]
    r = _run(N._exec_transform({}, {"value": ["a", "b"], "op": "join", "separator": "-"}, None))
    assert r["output"] == "a-b"


# ============================================================ collection


def test_collection_union():
    r = _run(N._exec_collection({}, {"lists": [[1, 2, 3], [2, 3, 4]], "op": "union"}, None))
    # output 是 JSON 字符串（便于代入模板），结构化结果在 items
    assert r["items"] == [1, 2, 3, 4]
    assert r["count"] == 4


def test_collection_diff():
    r = _run(N._exec_collection({}, {"lists": [[1, 2, 3], [2, 3, 4]], "op": "diff"}, None))
    assert r["items"] == [1]


def test_collection_intersect():
    r = _run(N._exec_collection({}, {"lists": [[1, 2, 3], [2, 3, 4]], "op": "intersect"}, None))
    assert r["items"] == [2, 3]


def test_collection_merge_keeps_duplicates():
    r = _run(N._exec_collection({}, {"lists": [[1, 2], [2, 3]], "op": "merge"}, None))
    assert r["items"] == [1, 2, 2, 3]


def test_collection_unknown_op():
    with pytest.raises(ValidationError):
        _run(N._exec_collection({}, {"lists": [[]], "op": "nope"}, None))


# ============================================================ mapping / json_path


def test_mapping_builds_object():
    r = _run(N._exec_mapping({}, {"mappings": [
        {"key": "orderNo", "value": "X1"}, {"key": "amount", "value": 10},
    ]}, None))
    assert r["data"] == {"orderNo": "X1", "amount": 10}
    assert "orderNo" in r["output"]


def test_mapping_base_merge():
    r = _run(N._exec_mapping({}, {"base": {"a": 1, "b": 2}, "mappings": [{"key": "b", "value": 9}]}, None))
    assert r["data"] == {"a": 1, "b": 9}


def test_mapping_exposes_flat_fields():
    """用户会写 {{m.orderNo}} 而不是 {{m.data.orderNo}}，必须同时提供扁平字段。"""
    r = _run(N._exec_mapping({}, {"mappings": [{"key": "orderNo", "value": "X1"}]}, None))
    assert r["orderNo"] == "X1"
    assert r["data"]["orderNo"] == "X1"


def test_mapping_flat_fields_do_not_shadow_reserved():
    """映射键为 data/output 时不能覆盖节点自身的保留字段。"""
    r = _run(N._exec_mapping({}, {"mappings": [{"key": "output", "value": "X"}]}, None))
    assert r["output"] != "X", "保留字段不应被覆盖"


def test_json_path_nested():
    r = _run(N._exec_json_path({}, {"source": '{"data":{"items":[{"id":7}]}}', "path": "data.items.0.id"}, None))
    assert r["value"] == 7


def test_json_path_missing_returns_none():
    r = _run(N._exec_json_path({}, {"source": "{}", "path": "a.b.c"}, None))
    assert r["value"] is None


# ============================================================ 办公


def test_text_template_sections():
    r = _run(N._exec_text_template({}, {"sections": [
        {"title": "概要", "content": "正常"}, {"content": "第二段"},
    ]}, None))
    assert "概要" in r["output"] and "正常" in r["output"] and "第二段" in r["output"]


def test_email_requires_recipient():
    with pytest.raises(ValidationError):
        _run(N._exec_email({}, {"subject": "x", "account_id": 1}, None))


def test_email_requires_account():
    with pytest.raises(ValidationError):
        _run(N._exec_email({}, {"to": "a@b.com", "subject": "x"}, None))


def test_router_contains_match():
    r = _run(N._exec_router({}, {
        "value": "我要申请退款", "cases": [{"handle": "售后", "values": ["退货", "退款"]}],
    }, None))
    assert r["branch"] == "售后"


def test_router_default():
    r = _run(N._exec_router({}, {"value": "咨询", "cases": [{"handle": "售后", "values": ["退货"]}]}, None))
    assert r["branch"] == "default"


def test_router_comma_string_values():
    r = _run(N._exec_router({}, {"value": "退货", "cases": [{"handle": "售后", "values": "退货,退款"}]}, None))
    assert r["branch"] == "售后"


# ============================================================ batch / delay / 参数校验


def test_batch_splits():
    r = _run(N._exec_batch({}, {"items": list(range(7)), "size": 3}, None))
    assert r["count"] == 3 and r["total"] == 7
    assert r["batches"][0] == [0, 1, 2]


def test_batch_size_clamped():
    r = _run(N._exec_batch({}, {"items": [1, 2], "size": 9999}, None))
    assert len(r["batches"][0]) == 2


def test_webhook_requires_url():
    with pytest.raises(ValidationError):
        _run(N._exec_webhook({}, {}, None))


def test_http_form_requires_url():
    with pytest.raises(ValidationError):
        _run(N._exec_http_form({}, {}, None))


# ============================================================ AI 生成：清洗 / 布局 / 解析


def test_sanitize_remaps_illegal_ids_in_edges():
    from app.services.workflow_ai_service import sanitize_graph

    g, _ = sanitize_graph({
        "nodes": [
            {"id": "start", "type": "start", "data": {}},
            {"id": "http 1", "type": "http", "data": {"url": "https://x"}},
        ],
        "edges": [{"source": "start", "target": "http 1"}],
    })
    assert "http_1" in [n["id"] for n in g["nodes"]]
    assert any(e["target"] == "http_1" for e in g["edges"]), "边应随 id 清洗同步改写"


def test_sanitize_adds_end_and_connects_leaf():
    from app.services.workflow_ai_service import sanitize_graph

    g, warns = sanitize_graph({
        "nodes": [
            {"id": "start", "type": "start", "data": {}},
            {"id": "llm_1", "type": "llm", "data": {}},
        ],
        "edges": [{"source": "start", "target": "llm_1"}],
    })
    assert any(n["type"] == "end" for n in g["nodes"])
    assert any(e["source"] == "llm_1" and e["target"] == "end" for e in g["edges"])
    assert any("end" in w for w in warns)


def test_sanitize_drops_unknown_type():
    from app.services.workflow_ai_service import sanitize_graph

    g, warns = sanitize_graph({
        "nodes": [
            {"id": "start", "type": "start", "data": {}},
            {"id": "ghost", "type": "not_a_node", "data": {}},
            {"id": "end", "type": "end", "data": {}},
        ],
        "edges": [{"source": "start", "target": "ghost"}, {"source": "start", "target": "end"}],
    })
    assert all(n["type"] != "not_a_node" for n in g["nodes"])
    assert any("未知节点类型" in w for w in warns)


def test_sanitize_layout_no_overlap():
    from app.services.workflow_ai_service import sanitize_graph

    g, _ = sanitize_graph({
        "nodes": [
            {"id": "start", "type": "start", "data": {}},
            {"id": "a", "type": "llm", "data": {}},
            {"id": "b", "type": "llm", "data": {}},
            {"id": "end", "type": "end", "data": {}},
        ],
        "edges": [
            {"source": "start", "target": "a"},
            {"source": "start", "target": "b"},
            {"source": "a", "target": "end"},
            {"source": "b", "target": "end"},
        ],
    })
    positions = [tuple(n["position"].values()) for n in g["nodes"]]
    assert len(set(positions)) == len(positions), "节点不应重叠"


def test_sanitize_rejects_empty():
    from app.services.workflow_ai_service import sanitize_graph

    with pytest.raises(ValidationError):
        sanitize_graph({"nodes": [], "edges": []})


def test_sanitize_output_passes_engine_validation():
    from app.services.workflow_ai_service import sanitize_graph

    g, _ = sanitize_graph({
        "nodes": [
            {"id": "start", "type": "start", "data": {}},
            {"id": "t", "type": "table", "data": {"rows": "{{start.query}}"}},
        ],
        "edges": [{"source": "start", "target": "t"}],
    })
    validate_graph(g)


def test_parse_graph_json_fence_and_prose():
    from app.services.workflow_ai_service import parse_graph_json

    assert parse_graph_json('```json\n{"nodes":[]}\n```') == {"nodes": []}
    assert parse_graph_json('结果如下 {"a":1} 完毕') == {"a": 1}


def test_parse_graph_json_invalid():
    from app.services.workflow_ai_service import parse_graph_json

    with pytest.raises(ValidationError):
        parse_graph_json("这不是 JSON")


def test_node_specs_cover_all_executors():
    """NODE_SPECS 是喂给 AI 的节点清单，漏了节点 AI 就不会用。"""
    from app.services.workflow_ai_service import NODE_SPECS

    missing = set(NODE_EXECUTORS) - set(NODE_SPECS)
    assert not missing, f"以下节点缺少 AI 规格说明：{missing}"


# ============================================================ 端到端：真实引擎跑新节点


def test_e2e_table_mapping_template():
    """start → table → mapping → text_template → end，验证新节点接进引擎后能跑通。"""
    from app.agents.workflow.engine import NodeContext, execute_graph
    from app.services.permission import PrincipalSet

    rows = '[{"name":"A","amt":"100","st":"未付"},{"name":"B","amt":"50","st":"已付"},{"name":"C","amt":"200","st":"未付"}]'
    g = {
        "nodes": [
            {"id": "s", "type": "start", "data": {"inputs": [{"name": "data"}]}},
            {"id": "t", "type": "table", "data": {
                "rows": "{{s.data}}",
                "filters": [{"field": "st", "op": "eq", "value": "未付"}],
                "aggregates": [{"name": "total", "field": "amt", "op": "sum"}],
            }},
            {"id": "m", "type": "mapping", "data": {"mappings": [
                {"key": "cnt", "value": "{{t.count}}"},
                {"key": "amt", "value": "{{t.summary.total}}"},
            ]}},
            {"id": "tt", "type": "text_template", "data": {"sections": [
                {"title": "汇总", "content": "未付 {{m.cnt}} 单，合计 {{m.amt}} 元"},
            ]}},
            {"id": "e", "type": "end", "data": {"output": "{{tt.text}}"}},
        ],
        "edges": [
            {"source": "s", "target": "t"}, {"source": "t", "target": "m"},
            {"source": "m", "target": "tt"}, {"source": "tt", "target": "e"},
        ],
    }

    async def _t():
        ctx = NodeContext(db=None, ps=PrincipalSet(user_id=1, tenant_id=1), run_id=1, tenant_id=1)
        final, records = await execute_graph(g, {"data": rows}, ctx)
        assert "未付 2 单" in final["output"]
        assert "300" in final["output"]
        assert all(r["status"] == "success" for r in records)

    _run(_t())
