"""向量存储抽象与实现。

- PgVectorStore: PostgreSQL + pgvector 原生向量检索
- NumpyStore: SQLite/MySQL，向量存 BLOB，应用层用 numpy cosine 计算
两者都接受「权限过滤条件」并保证只检索有权内容。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chunk
from app.services.permission import PermissionFilter, build_doc_acl_clause, chunk_visible_py


@dataclass
class VectorHit:
    chunk_id: int
    doc_id: int
    kb_id: int
    content: str
    score: float
    page: int | None = None
    parent_content: str | None = None
    section: str | None = None
    raw_score: float | None = None      # 原始分（RRF 原始/向量相似度，用于阈值判断）
    rerank_score: float | None = None   # 重排分
    source: str = "vector"              # vector | bm25 | fused | rerank | external
    # 融合后保留的原始来源集合（vector/bm25/external），供按来源分别处理（如阈值豁免）
    sources: set = field(default_factory=set)
    # 外部知识库命中：有值表示来自外部源（chunk_id/doc_id 为占位，不指向本地表）
    ext_ref: str | None = None
    ext_title: str | None = None
    source_uri: str | None = None


def _apply_permission(stmt, pf: PermissionFilter, dialect: str):
    """统一的权限过滤（租户 + KB 白名单 + 文档级 ACL）——所有向量/关键词检索必须经此。"""
    from sqlalchemy import text

    stmt = stmt.where(Chunk.tenant_id == pf.tenant_id)
    if not pf.bypass_kb:
        if pf.accessible_kb_ids:
            stmt = stmt.where(Chunk.kb_id.in_(pf.accessible_kb_ids))
        else:
            # 无可访问 KB，返回空（用永假条件）
            stmt = stmt.where(Chunk.kb_id == -1)
    # 文档级 ACL：继承(0)/公开(1) 放行；受限(2) 需 acl_allow 命中且不在 acl_deny
    stmt = stmt.where(text(build_doc_acl_clause(pf.principals, dialect, col="")))
    stmt = stmt.where(Chunk.enabled.is_(True))
    # 只检索子块/扁平块，不检索父块
    stmt = stmt.where(Chunk.chunk_type != "parent")
    # 排除解析失败/未就绪文档残留的半截分块（见 _exclude_broken_docs）
    stmt = _exclude_broken_docs(stmt)
    return stmt


def _exclude_broken_docs(stmt):
    """排除未完成/解析失败的文档留下的分块。

    解析失败的文档常残留半截分块（入库时先切块再嵌入，随时可能中断），
    它们内容不完整、页码对不上原文，被当成引用来源展示给用户就是误导。
    只排除明确不可用的状态：pending/parsing/chunking/embedding/failed/disabled。
    文档行不存在时（历史脏数据）不排除——宁可显示也不静默丢内容。
    用 NOT EXISTS 而不是 JOIN：JOIN 会改变 select 的列形状，影响既有调用方。
    """
    from sqlalchemy import exists, select as _select

    from app.models import Document

    bad = _select(Document.id).where(
        (Document.id == Chunk.doc_id)
        & (Document.status.in_(("pending", "parsing", "chunking", "embedding", "failed", "disabled")))
    )
    return stmt.where(~exists(bad))



def _dialect_of(db) -> str:
    try:
        return db.get_bind().dialect.name
    except Exception:  # noqa: BLE001
        return "sqlite"


class VectorStore:
    """基类。"""

    async def search(
        self, db: AsyncSession, *, query_vec: list[float], pf: PermissionFilter, top_k: int,
        max_scan: int | None = None,
    ) -> list[VectorHit]:
        raise NotImplementedError

    async def add_embeddings(self, db: AsyncSession, chunk_vecs: list[tuple[int, list[float]]]) -> None:
        raise NotImplementedError


class NumpyStore(VectorStore):
    """SQLite/MySQL 实现：全量取回（带权限过滤）后在应用层算 cosine。"""

    async def search(self, db, *, query_vec, pf, top_k, max_scan: int | None = None) -> list[VectorHit]:
        import numpy as np

        dialect = _dialect_of(db)
        stmt = select(
            Chunk.id, Chunk.doc_id, Chunk.kb_id, Chunk.content, Chunk.page,
            Chunk.parent_content, Chunk.section, Chunk.embedding,
            Chunk.vis_scope, Chunk.acl_allow, Chunk.acl_deny,
        )
        stmt = _apply_permission(stmt, pf, dialect)
        if max_scan:
            stmt = stmt.limit(max_scan)
        rows = (await db.execute(stmt)).all()
        if not rows:
            return []

        # 纵深防御：Python 侧先按可见性筛出子集（同 chunk_visible_py 语义），再堆矩阵，
        # 避免把不可见行算进向量矩阵。
        pset = set(pf.principals)
        kept = [r for r in rows if r[7] is not None and chunk_visible_py(pset, r[8], r[9], r[10])]
        if not kept:
            return []

        # 一次性堆叠 + 批量归一化 + 一次矩阵乘（替代逐行 Python 点积）
        mat = np.vstack([np.asarray(r[7], dtype=np.float32) for r in kept])
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        mat /= norms
        q = np.asarray(query_vec, dtype=np.float32)
        qn = float(np.linalg.norm(q)) or 1.0
        sims = (mat @ (q / qn)).astype(np.float32)

        k = min(top_k, sims.shape[0])
        if k <= 0:
            return []
        idx = np.argpartition(-sims, k - 1)[:k]
        idx = idx[np.argsort(-sims[idx])]

        hits: list[VectorHit] = []
        for i in idx.tolist():
            r = kept[i]
            hits.append(
                VectorHit(
                    chunk_id=r[0], doc_id=r[1], kb_id=r[2], content=r[3],
                    score=float(sims[i]), raw_score=float(sims[i]),
                    page=r[4], parent_content=r[5], section=r[6], source="vector",
                )
            )
        return hits

    async def add_embeddings(self, db, chunk_vecs) -> None:
        for chunk_id, vec in chunk_vecs:
            chunk = await db.get(Chunk, chunk_id)
            if chunk:
                chunk.embedding = vec


class PgVectorStore(VectorStore):
    """PostgreSQL + pgvector 实现。"""

    async def search(self, db, *, query_vec, pf, top_k) -> list[VectorHit]:
        from sqlalchemy import text

        # 迭代扫描：避免过滤后召回不足
        try:
            await db.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
            await db.execute(text("SET LOCAL hnsw.max_scan_tuples = 20000"))
        except Exception:  # noqa: BLE001
            pass  # 老版本 pgvector 无此参数

        vec_literal = "[" + ",".join(f"{x:.6f}" for x in query_vec) + "]"
        kb_cond = ""
        params: dict = {"q": vec_literal, "k": top_k, "tenant": pf.tenant_id}
        if not pf.bypass_kb:
            if pf.accessible_kb_ids:
                kb_cond = "AND kb_id = ANY(:kbs)"
                params["kbs"] = pf.accessible_kb_ids
            else:
                kb_cond = "AND kb_id = -1"

        acl_clause = build_doc_acl_clause(pf.principals, "postgresql", col="")
        sql = f"""
            SELECT id, doc_id, kb_id, content, page, parent_content, section,
                   1 - (embedding <=> :q) AS score
            FROM chunk
            WHERE tenant_id = :tenant
              {kb_cond}
              AND {acl_clause}
              AND enabled = true
              AND chunk_type != 'parent'
              AND NOT EXISTS (
                SELECT 1 FROM document d
                WHERE d.id = chunk.doc_id
                  AND d.status IN ('pending','parsing','chunking','embedding','failed','disabled')
              )
            ORDER BY embedding <=> :q
            LIMIT :k
        """
        rows = (await db.execute(text(sql), params)).all()
        return [
            VectorHit(
                chunk_id=r[0], doc_id=r[1], kb_id=r[2], content=r[3],
                score=float(r[7]), page=r[4], parent_content=r[5], section=r[6],
            )
            for r in rows
        ]

    async def add_embeddings(self, db, chunk_vecs) -> None:
        from sqlalchemy import text

        for chunk_id, vec in chunk_vecs:
            lit = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
            await db.execute(
                text("UPDATE chunk SET embedding = :v WHERE id = :id"),
                {"v": lit, "id": chunk_id},
            )


_store: VectorStore | None = None


def get_vector_store() -> VectorStore:
    global _store
    if _store is None:
        from app.core.config import settings

        _store = PgVectorStore() if settings.resolved_vector_backend == "pgvector" else NumpyStore()
    return _store
