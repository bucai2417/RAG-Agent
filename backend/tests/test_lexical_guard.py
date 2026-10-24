"""词面依据复核：向量通路的低分噪声不能进 citations。

线上现场：问「打开必应并搜索吕不韦」，回答下面挂着「后续会开发企业内部聊天模块」
和文档「t」。两条都不是 BM25 命中的（词面判据会挡掉），而是向量通路进来的——
余弦不是 0 只说明共享了几个字，分块少的小库里这类无关命中很容易占满 top_k。

这里用假的向量库复现同一条编排路径（retrieve 全流程），
钉住两件事：无依据的低分命中被丢弃；有依据的（含换过说法的）保留。
不连网、不起进程、不依赖真实 embedding 模型。
"""
from __future__ import annotations

import pytest

from app.retrieval.vector_store.store import VectorHit


class _FakePS:
    tenant_id = 1
    user_id = 1
    is_admin = True
    principals = [0]


def _mk_hit(cid: int, content: str, score: float) -> VectorHit:
    return VectorHit(
        chunk_id=cid, doc_id=cid, kb_id=1, content=content,
        score=score, raw_score=score, source="vector",
    )


async def _run(monkeypatch, *, hits: list[VectorHit], query: str, guard: bool,
               floor: float = 0.0):
    """以假向量库跑一遍 retrieve，返回命中内容列表。"""
    from app.services import retrieval_service as rs
    from app.services.permission import PermissionFilter

    async def fake_build_filter(db, ps, kb_ids=None, force_kb_ids=None):
        return PermissionFilter(tenant_id=1, accessible_kb_ids=[1], principals=[0])

    class FakeStore:
        async def search(self, db, **kw):
            return list(hits)

    async def fake_embed(db, *, tenant_id, query, config_id=None):
        return [0.0]

    async def fake_groups(db, ids):
        return ({None: [1]}, set())

    async def fake_chunks(db, hs):
        from app.schemas.chat import RetrievedChunk

        return [
            RetrievedChunk(chunk_id=h.chunk_id, doc_id=h.doc_id, kb_id=h.kb_id,
                           content=h.content, score=h.score, doc_title=f"文档{h.chunk_id}")
            for h in hs
        ]

    monkeypatch.setattr(rs, "build_filter", fake_build_filter)
    monkeypatch.setattr(rs, "get_vector_store", lambda: FakeStore())
    monkeypatch.setattr(rs, "embed_query", fake_embed)
    monkeypatch.setattr(rs, "group_kbs_by_embedding_model", fake_groups)
    monkeypatch.setattr(rs, "_to_chunks", fake_chunks)

    async def fake_split(db, ids):
        return (list(ids or []), [])  # 全部当本地库，无外部源

    monkeypatch.setattr(rs, "_split_local_external", fake_split)

    from app.core.config import settings

    monkeypatch.setattr(settings, "retrieval_vec_guard_floor", floor, raising=False)

    resp = await rs.retrieve(
        None, ps=_FakePS(), query=query, kb_ids=[1], top_k=5,
        use_hybrid=False, retrieval_lexical_guard=guard,
    )
    return [c.content for c in resp.chunks]


NOISE = [
    _mk_hit(1, "后续会开发企业内部聊天模块", 0.011),
    _mk_hit(2, "测试分块 查看提示 文本 权限感知", 0.010),
    _mk_hit(3, "本公司的报销流程：填写报销单，附发票，直属主管审批。", 0.009),
]


@pytest.mark.asyncio
async def test_irrelevant_low_score_hits_are_dropped(monkeypatch):
    """两个开发文档（低分且词面无依据）应被丢弃，只留报销流程那条。"""
    kept = await _run(monkeypatch, hits=NOISE, query="报销流程是什么", guard=True)
    assert kept == ["本公司的报销流程：填写报销单，附发票，直属主管审批。"]


@pytest.mark.asyncio
async def test_guard_off_returns_raw_candidates(monkeypatch):
    """关掉复核要能看到原始候选——否则检索调试页没法排查召回噪声。"""
    kept = await _run(monkeypatch, hits=NOISE, query="报销流程是什么", guard=False)
    assert len(kept) == 3


@pytest.mark.asyncio
async def test_guard_drops_everything_when_nothing_is_evidenced(monkeypatch):
    """问的是知识库里没有的东西 → 宁可返回空，也不塞无关资料当"参考资料"。"""
    kept = await _run(monkeypatch, hits=NOISE, query="打开必应并搜索吕不韦", guard=True)
    assert kept == [], "全部无依据时应返回空，而不是把开发文档当成检索结果"


@pytest.mark.asyncio
async def test_high_score_semantic_hit_survives_guard(monkeypatch):
    """高分命中即便是"换了说法"也保留：语义检索的价值就在这里。

    问「怎么退货」而正文写「退换货规定」，词面判据会判无依据；
    但分数很高，说明向量认为语义相关，不能因为字面不同就丢掉。
    """
    hits = [_mk_hit(9, "退换货规定：收到货 7 天内可无理由寄回。", 0.62)]
    kept = await _run(monkeypatch, hits=hits, query="怎么退货", guard=True, floor=0.3)
    assert len(kept) == 1, "高于豁免分数线的命中不应被词面判据卡掉"

    # 同一条命中去掉豁免线 → 词面无依据，应被丢弃（证明豁免线确实是它活下来的原因）
    dropped = await _run(monkeypatch, hits=hits, query="怎么退货", guard=True, floor=0.0)
    assert dropped == []
