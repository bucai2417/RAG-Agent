"""检索相关性门槛：没有依据的「弱命中」不能被当成引用来源。

为什么单独测这一层：这是用户可见的「明明没触发知识库却显示引用了」的根因——
BM25 对中文按单字切分，单字命中即 score>0 进入候选，而分数阈值默认 0（不过滤），
于是「继续完善智能问答界面」会因一个「面」字命中无关文档并被展示成引用。
真机上很难复现（取决于库里恰好有哪些字），只能在这里钉死判据。

不连浏览器、不联网、不起进程。
"""
from __future__ import annotations

import pytest

from app.retrieval.bm25 import (
    MIN_COVERAGE,
    BM25,
    _coverage,
    _hit_units,
    informative_units,
    min_hits_for,
    tokenize,
)


# ---------------------------------------------------------------- 证据单元


class TestInformativeUnits:
    def test_drops_single_chars(self):
        """单字不能作为依据：中文里几乎每段文本都含「的/面/行」。"""
        units = informative_units("继续完善智能问答界面")
        assert "面" not in units
        assert all(len(u) >= 2 for u in units)

    def test_chinese_bigrams(self):
        assert informative_units("报销流程") == ["报销", "销流", "流程"]

    def test_english_words_kept_whole(self):
        assert informative_units("beta 2") == ["beta"]

    def test_single_char_query_falls_back_to_itself(self):
        """只查一个字时不能判成「无依据」，否则这类查询永远检索不到东西。"""
        assert informative_units("面") == ["面"]

    def test_single_letter_query_falls_back(self):
        assert informative_units("t") == ["t"]


class TestMinHits:
    def test_scales_with_query_length(self):
        """长查询要求覆盖更多字符，单字碰运气进不来。"""
        short = min_hits_for(informative_units("报销"))
        long = min_hits_for(informative_units("继续完善智能问答界面的引用来源展示"))
        assert long > short

    def test_never_zero_for_nonempty_query(self):
        # need=0 等于不过滤，任何有共同字的片段都会进来
        assert min_hits_for(informative_units("怎么退款")) >= 1

    def test_empty_query(self):
        assert min_hits_for([]) == 0


# ---------------------------------------------------------------- 覆盖率判据


class TestCoverage:
    """这些用例直接对应线上观察到的现象与不能破坏的正常命中。"""

    CASES = [
        # (query, chunk, 是否应判为相关)
        ("继续完善智能问答界面", "知识库运营面验证行。知识库运营面验证行。", False),
        ("继续完善智能问答界面", "后续会开发企业内部聊天模块", False),
        ("继续完善智能问答界面", "智能问答界面支持引用来源点击查看。", True),
        ("怎么退货", "退货流程：在订单页申请退货，填写原因，客服审核后上门取件。", True),
        ("怎么退款", "退款流程说明：在订单页点申请退款，3 个工作日到账。", True),
        ("报销流程是什么", "本公司的报销流程：填写报销单，附发票，直属主管审批，财务复核。", True),
    ]

    @pytest.mark.parametrize("query,chunk,expect", CASES)
    def test_coverage_verdict(self, query, chunk, expect):
        units = informative_units(query)
        need = min_hits_for(units)
        assert (_hit_units(units, chunk) >= need) is expect

    def test_coverage_is_ratio(self):
        units = informative_units("报销流程是什么")
        assert 0.0 < _coverage(units, "报销流程：填写报销单") <= 1.0
        assert _coverage(units, "") == 0.0

    def test_threshold_is_not_too_strict(self):
        """0.3 是实测出来的下限：0.35 会误杀「怎么退货」这类正常命中。"""
        assert MIN_COVERAGE <= 0.3


# ---------------------------------------------------------------- 端到端：BM25 检索


class _Row(tuple):
    """模拟 keyword_search 里 rows 的形状（content 在 r[3]）。"""

    def __new__(cls, cid: int, content: str):
        return super().__new__(cls, (cid, 1, 1, content, 1, None, None, 0, None, None))


def _index(contents: list[str]) -> tuple[BM25, list]:
    rows = [_Row(i + 1, c) for i, c in enumerate(contents)]
    corpus = [tokenize(r[3]) for r in rows]
    return BM25(corpus), rows


class TestKeywordSearchFiltering:
    """keyword_search 是召回入口，弱命中必须在这里被挡掉。

    只测真实入口而不只测判据函数：判据写对了但没接进召回路径，
    用户看到的还是「无关问题也显示引用」。
    """

    @pytest.mark.asyncio
    async def test_weak_hit_is_dropped_end_to_end(self):
        from app.retrieval.bm25 import keyword_search
        from app.services.permission import PermissionFilter

        index, rows = _index([
            "知识库运营面验证行。知识库运营面验证行。",
            "本公司的报销流程：填写报销单，附发票，直属主管审批。",
        ])
        pf = PermissionFilter(tenant_id=1, accessible_kb_ids=[1], principals=[0])
        hits = await keyword_search(
            None, query="继续完善智能问答界面", pf=pf, top_k=5, index=index, rows=rows,
        )
        assert hits == [], "仅靠单字命中的无关片段不应进入召回结果"

    @pytest.mark.asyncio
    async def test_relevant_hit_is_kept_end_to_end(self):
        from app.retrieval.bm25 import keyword_search
        from app.services.permission import PermissionFilter

        index, rows = _index([
            "知识库运营面验证行。知识库运营面验证行。",
            "本公司的报销流程：填写报销单，附发票，直属主管审批。",
        ])
        pf = PermissionFilter(tenant_id=1, accessible_kb_ids=[1], principals=[0])
        hits = await keyword_search(
            None, query="报销流程是什么", pf=pf, top_k=5, index=index, rows=rows,
        )
        assert len(hits) == 1
        assert "报销流程" in hits[0].content

    @pytest.mark.asyncio
    async def test_min_coverage_can_be_relaxed(self):
        """留出调松的口子：某些库（代码片段/编号表）确实需要更宽的召回。"""
        from app.retrieval.bm25 import keyword_search
        from app.services.permission import PermissionFilter

        index, rows = _index(["只有面字出现"])
        pf = PermissionFilter(tenant_id=1, accessible_kb_ids=[1], principals=[0])
        hits = await keyword_search(
            None, query="继续完善智能问答界面", pf=pf, top_k=5,
            index=index, rows=rows, min_coverage=0.0,
        )
        assert hits, "min_coverage=0 时应关闭依据判据（只保留 BM25 分数过滤）"


# ---------------------------------------------------------------- 词面依据复核（向量通路）


class TestLexicalEvidence:
    """向量通路原本没有依据判据，无关文档因此能占满 top_k。

    线上现场：问「打开必应并搜索吕不韦」，回答下面挂着「后续会开发企业内部聊天模块」
    和文档「t」。前者 BM25 判据会挡掉，后者是向量余弦低分命中——余弦不是 0
    只说明共享了几个字。这里钉住「两条通路判据一致」。
    """

    CASES = [
        ("打开必应并搜索吕不韦", "后续会开发企业内部聊天模块", False),
        ("打开必应并搜索吕不韦", "本公司的报销流程：填写报销单，附发票，直属主管审批。", False),
        ("报销流程是什么", "本公司的报销流程：填写报销单，附发票，直属主管审批。", True),
        ("怎么退货", "退货流程：在订单页申请退货，填写原因，客服审核后上门取件。", True),
    ]

    @pytest.mark.parametrize("query,chunk,expect", CASES)
    def test_evidence_verdict(self, query, chunk, expect):
        from app.retrieval.bm25 import has_lexical_evidence

        assert has_lexical_evidence(query, chunk) is expect

    def test_matches_bm25_recall_verdict(self):
        """同一片段必须先被 BM25 接受、才可能被判为有依据，否则两条通路会打架。"""
        from app.retrieval.bm25 import _hit_units, has_lexical_evidence, informative_units, min_hits_for

        query, chunk = "报销流程是什么", "本公司的报销流程：填写报销单，附发票，直属主管审批。"
        units = informative_units(query)
        bm25_ok = _hit_units(units, chunk) >= min_hits_for(units)
        assert bm25_ok is has_lexical_evidence(query, chunk) is True

    def test_single_char_query_is_not_rejected(self):
        """只查一个字的查询无从施加判据，不能一律判成无依据（否则永远检索不到）。"""
        from app.retrieval.bm25 import has_lexical_evidence

        assert has_lexical_evidence("面", "只有面字出现") is True

    def test_empty_query_is_not_rejected(self):
        from app.retrieval.bm25 import has_lexical_evidence

        assert has_lexical_evidence("", "任意内容") is True
