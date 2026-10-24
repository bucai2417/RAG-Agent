"""引用合并与「已引用/仅检索到」区分。

对应线上问题：明明没有触发知识库内容，回答下方却一直显示「引用来源」。
这里钉住三件事：
1. 两组引用（预检索 + 工具检索）合并后不重复、工具命中优先；
2. 外部知识库的占位 chunk_id 不会把不同来源误合并成一条；
3. used_ids 只包含答案里真的出现、且落在有效范围内的编号。
"""
from __future__ import annotations

from app.services.chat_service import _citation_usage, _merge_citations


def _c(cid: int, title: str = "文档", page: int | None = 1, snippet: str = "") -> dict:
    return {"chunk_id": cid, "doc_id": cid, "doc_title": title, "page": page, "snippet": snippet}


class TestMergeCitations:
    def test_tool_hits_come_first(self):
        """工具检索是 AI 真正看过的资料，应排在预检索结果前面。"""
        tool = [_c(5, "工具命中")]
        pre = [_c(9, "预检索")]
        out = _merge_citations(tool, pre)
        assert [c["chunk_id"] for c in out] == [5, 9]

    def test_same_chunk_is_deduped(self):
        out = _merge_citations([_c(5)], [_c(5)])
        assert len(out) == 1

    def test_external_sources_not_merged_by_placeholder_id(self):
        """外部知识库的 chunk_id 恒为 0，只能按标题/页码/片段区分。"""
        a = {"chunk_id": 0, "doc_id": 0, "doc_title": "外部A", "page": 1, "snippet": "aaa"}
        b = {"chunk_id": 0, "doc_id": 0, "doc_title": "外部B", "page": 1, "snippet": "bbb"}
        out = _merge_citations([a], [b])
        assert len(out) == 2, "不同外部来源不能因为 chunk_id 都是 0 而被合并"

    def test_external_same_source_is_deduped(self):
        a = {"chunk_id": 0, "doc_id": 0, "doc_title": "外部A", "page": 1, "snippet": "同样的片段"}
        out = _merge_citations([a], [dict(a)])
        assert len(out) == 1

    def test_empty_inputs(self):
        assert _merge_citations([], []) == []
        assert _merge_citations([_c(1)], []) == [_c(1)]


class TestCitationUsage:
    def test_marks_only_used_numbers(self):
        cits = [_c(1), _c(2), _c(3)]
        assert _citation_usage("按资料 [1] 和 [3] 的说法", cits)["used_ids"] == [1, 3]

    def test_no_citation_markers_means_nothing_used(self):
        """检索到了但答案完全没标注 → 不该显示成「引用来源」。"""
        assert _citation_usage("这是模型自己的知识。", [_c(1)])["used_ids"] == []

    def test_out_of_range_numbers_are_dropped(self):
        """答案编了不存在的 [9]，不能拿来高亮——那是幻觉引用。"""
        assert _citation_usage("见 [1] 与 [9]", [_c(1)])["used_ids"] == [1]

    def test_empty_citations(self):
        assert _citation_usage("见 [1]", [])["used_ids"] == []

    def test_duplicate_markers_collapse(self):
        assert _citation_usage("[2] 和 [2]", [_c(1), _c(2)])["used_ids"] == [2]
