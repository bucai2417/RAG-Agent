"""检索编排服务：权限解析 → 向量+关键词召回 → RRF 融合 → 父子补全。"""
from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KBMember, KnowledgeBase
from app.retrieval.bm25 import keyword_search
from app.retrieval.fusion import rrf_fuse
from app.retrieval.vector_store.store import VectorHit, get_vector_store
from app.providers.registry import get_embedding
from app.schemas.chat import Citation, RetrievalResponse, RetrievedChunk
from app.services.permission import (
    PermissionFilter,
    PrincipalSet,
    build_permission_filter,
    filter_accessible_kbs,
)


async def resolve_accessible_kbs(db: AsyncSession, ps: PrincipalSet) -> list[int]:
    """查询该主体可访问的知识库 id 列表（权限感知检索的第一道过滤）。"""
    rows = (
        await db.execute(
            select(KnowledgeBase.id, KnowledgeBase.visibility).where(
                KnowledgeBase.tenant_id == ps.tenant_id,
                KnowledgeBase.status == "active",
            )
        )
    ).all()
    # 取各 KB 的成员 principal
    kb_ids = [r[0] for r in rows]
    member_map: dict[int, list[int]] = {}
    if kb_ids:
        members = (
            await db.execute(
                select(KBMember.kb_id, KBMember.principal_id).where(KBMember.kb_id.in_(kb_ids))
            )
        ).all()
        for kb_id, principal_id in members:
            member_map.setdefault(kb_id, []).append(principal_id)

    kb_rows = [
        {"kb_id": r[0], "visibility": r[1], "principals": member_map.get(r[0], [])}
        for r in rows
    ]
    return filter_accessible_kbs(ps, kb_rows)


async def build_filter(
    db: AsyncSession,
    ps: PrincipalSet,
    kb_ids: list[int] | None = None,
    force_kb_ids: list[int] | None = None,
) -> PermissionFilter:
    accessible = await resolve_accessible_kbs(db, ps)
    # API Key 强制限制：先与可访问集合取交集
    if force_kb_ids is not None:
        allowed = set(force_kb_ids)
        accessible = [k for k in accessible if k in allowed]
    if kb_ids:
        # 请求显式指定了知识库范围：无论谁，都必须限定在该范围内
        requested = set(kb_ids)
        if ps.is_admin and force_kb_ids is None:
            scoped = list(requested)
        else:
            # 非管理员（或受限 key）：取「可访问」与「请求范围」的交集
            scoped = [k for k in accessible if k in requested]
        return PermissionFilter(
            tenant_id=ps.tenant_id,
            accessible_kb_ids=scoped,
            principals=ps.principals,
            bypass_kb=False,
        )
    return build_permission_filter(ps, accessible, force_kb_ids=force_kb_ids)


async def embed_query(
    db: AsyncSession, *, tenant_id: int, query: str, config_id: int | None = None
) -> list[float]:
    emb_driver, rm = await get_embedding(db, tenant_id=tenant_id, config_id=config_id)
    vecs = await emb_driver.embed([query], model=rm.model_name)
    return vecs[0]


async def group_kbs_by_embedding_model(
    db: AsyncSession, kb_ids: list[int]
) -> tuple[dict[int | None, list[int]], set[int]]:
    """按各 KB 的 embedding_model_id 分组，返回 (model_id → kb_ids, 非向量库集合)。

    非向量库（不参与向量召回，只走 BM25/外部）包括：
    - 图文库（source_type=entry）：条目短，靠关键词即可命中
    - 纯关键词库（index_mode=keyword）：用户明确选择不用 embedding
    未配置模型的普通库归入 None 组，走租户默认 embedding。
    """
    if not kb_ids:
        return {}, set()
    rows = (
        await db.execute(
            select(KnowledgeBase.id, KnowledgeBase.source_type, KnowledgeBase.embedding_model_id,
                   KnowledgeBase.index_mode)
            .where(KnowledgeBase.id.in_(kb_ids))
        )
    ).all()
    groups: dict[int | None, list[int]] = {}
    skip: set[int] = set()
    for kid, source_type, model_id, index_mode in rows:
        if (source_type or "local") == "entry" or (index_mode or "vector") == "keyword":
            skip.add(kid)
            continue
        groups.setdefault(model_id, []).append(kid)
    return groups, skip


async def _search_vectors_by_model(
    db: AsyncSession,
    *,
    tenant_id: int,
    query: str,
    pf: PermissionFilter,
    groups: dict[int | None, list[int]],
    candidate_k: int,
    max_scan: int | None,
) -> tuple[list[VectorHit], list[str]]:
    """按 KB 的向量模型分组做向量召回，各组结果合并。

    KB 级 embedding_model_id 若与租户默认不同，用它生成 query 向量，
    保证「同库同模型」的向量空间一致（否则跨模型算 cosine 无意义）。
    """
    import asyncio
    from dataclasses import replace

    from app.core.logging import get_logger

    log = get_logger("retrieval")
    store = get_vector_store()
    warnings: list[str] = []

    async def _one(model_id: int | None, kbs: list[int]) -> list[VectorHit]:
        sub_pf = replace(pf, accessible_kb_ids=kbs) if not pf.bypass_kb else pf
        try:
            qvec = await embed_query(db, tenant_id=tenant_id, query=query, config_id=model_id)
            hits = await store.search(db, query_vec=qvec, pf=sub_pf, top_k=candidate_k, max_scan=max_scan)
            # 标的库 id（向量库真实所属 kb），避免跨组串库
            return [h for h in hits if h.kb_id in set(kbs)]
        except Exception as e:  # noqa: BLE001
            log.warning("vector_search_group_failed", model_id=model_id, err=str(e)[:200])
            warnings.append("vector_unavailable")
            return []

    if len(groups) <= 1:
        # 单组（绝大多数场景）：直接一次调用，保持原路径
        only = next(iter(groups.items()), (None, []))
        return await _one(*only), warnings

    results = await asyncio.gather(*[_one(mid, kbs) for mid, kbs in groups.items()])
    out: list[VectorHit] = []
    for r in results:
        out.extend(r)
    out.sort(key=lambda h: h.score, reverse=True)
    return out[:candidate_k], warnings


# 追问/指代信号：短句 + 指代词 → 需要结合上下文改写再检索
_REFER_WORDS = ("它", "他", "她", "这个", "那个", "这", "那", "这些", "那些", "该", "此", "上述", "前面", "刚才", "呢", "还有")


def _needs_condense(query: str) -> bool:
    """启发式判断 query 是否依赖上下文（短句或含指代词）。"""
    q = (query or "").strip()
    if not q or len(q) > 40:
        return False
    if len(q) <= 12:
        return True
    return any(w in q for w in _REFER_WORDS)


async def condense_query(db: AsyncSession, *, tenant_id: int, query: str, history: list) -> str:
    """多轮追问改写：把「最近上下文 + 当前 query」压成独立完整问题。失败回退原文。"""
    from app.core.config import settings

    if not getattr(settings, "query_rewrite_enabled", True):
        return query
    if not history or not _needs_condense(query):
        return query
    # 取最近 3 轮 user 消息作上下文
    recent = [m.content for m in history[-6:] if getattr(m, "role", "") == "user" and m.content][-3:]
    if not recent:
        return query
    try:
        from app.providers.base import ChatMessage
        from app.providers.registry import get_llm

        llm, rm = await get_llm(db, tenant_id=tenant_id)
        sys = ("你是检索查询改写器。用户的问题可能是省略主语的追问（用「它/这个/上述」等指代）。"
               "请结合历史对话，把它改写成**一个独立、完整、可检索**的问题。"
               "只输出改写后的问题本身，不要解释、不要引号。若问题已完整，原样输出。")
        usr = "【历史对话】\n" + "\n".join(f"- {c}" for c in recent) + f"\n\n【当前问题】\n{query}\n\n【改写】"
        res = await llm.chat(
            [ChatMessage(role="system", content=sys), ChatMessage(role="user", content=usr)],
            model=rm.model_name, stream=False, temperature=0.0,
        )
        out = (getattr(res, "content", "") or "").strip().strip('"').strip("'")
        # 合理性校验：非空、不太长、不是空话
        if 0 < len(out) <= 200:
            return out
    except Exception:  # noqa: BLE001
        pass
    return query


async def _split_local_external(
    db: AsyncSession, kb_ids: list[int]
) -> tuple[list[int], list]:
    """把 KB 列表拆成 (本地 kb_ids, 外部 KB 行列表)。"""
    if not kb_ids:
        return [], []
    rows = (
        await db.execute(
            select(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids))
        )
    ).scalars().all()
    local_ids: list[int] = []
    external: list = []
    for kb in rows:
        if getattr(kb, "source_type", "local") == "external":
            external.append(kb)
        else:
            local_ids.append(kb.id)
    return local_ids, external


async def _search_external(
    external_kbs: list, query: str, *, top_k: int, warnings: list[str]
) -> list:
    """并发检索所有外部知识库，归一化为 VectorHit。单源失败不阻断。"""
    import asyncio

    from app.connectors.registry import from_kb
    from app.core.config import settings
    from app.core.logging import get_logger

    log = get_logger("retrieval")
    cap = settings.connector_max_top_k

    async def _one(kb):
        try:
            conn = from_kb(kb)
            if conn is None:
                return []
            docs = await conn.search(query, top_k=min(top_k, cap))
            hits: list[VectorHit] = []
            for rank, d in enumerate(docs, start=1):
                hits.append(
                    VectorHit(
                        chunk_id=0, doc_id=0, kb_id=kb.id, content=d.content,
                        score=1.0 / (60 + rank),  # 占位；真实分由 rrf 决定
                        page=d.page, ext_ref=d.ref or f"kb{kb.id}:{rank}",
                        ext_title=d.title, source_uri=d.source_uri,
                        source="external",
                    )
                )
            return hits
        except Exception as e:  # noqa: BLE001
            log.warning("external_search_failed", kb_id=kb.id, err=str(e)[:200])
            warnings.append(f"external_unavailable:{kb.id}")
            return []

    if not external_kbs:
        return []
    results = await asyncio.gather(*[_one(kb) for kb in external_kbs])
    out: list[VectorHit] = []
    for r in results:
        out.extend(r)
    return out


async def retrieve(
    db: AsyncSession,
    *,
    ps: PrincipalSet,
    query: str,
    kb_ids: list[int] | None = None,
    top_k: int | None = None,
    use_hybrid: bool = True,
    candidate_k: int | None = None,
    score_threshold: float = 0.0,
    use_rerank: bool | None = None,
    retrieval_lexical_guard: bool = True,
) -> RetrievalResponse:
    """检索编排。

    retrieval_lexical_guard：是否对**低分且词面无依据**的命中做复核丢弃
    （见融合后的「词面依据复核」段）。默认开；检索调试页若要观察原始召回，
    可传 False 关掉，看到的就是未经复核的完整候选。
    """
    from app.core.config import settings

    # 默认条数取自系统设置（可热改），调用方显式传入则覆盖
    if top_k is None:
        top_k = settings.retrieval_top_k
    if candidate_k is None:
        candidate_k = settings.retrieval_candidate_k

    t0 = time.time()
    pf = await build_filter(db, ps, kb_ids)
    warnings: list[str] = []
    degraded = False

    # 拆分本地 / 外部知识库来源
    local_ids, external_kbs = await _split_local_external(db, pf.accessible_kb_ids)
    if external_kbs:
        pf.accessible_kb_ids = local_ids

    rankings: list[tuple[str, list[VectorHit]]] = []  # (来源标签, 结果)

    # 外部知识库联邦检索（并发，单源失败不阻断）
    if external_kbs:
        ext_hits = await _search_external(external_kbs, query, top_k=candidate_k, warnings=warnings)
        if ext_hits:
            rankings.append(("external", ext_hits))
        if warnings:
            degraded = True

    # 向量召回（仅本地）。按各 KB 的 embedding_model_id 分组：同组同模型，
    # 保证 query 向量与库内向量处于同一向量空间（跨模型算 cosine 无意义）。
    # 图文库（source_type=entry）不做向量嵌入，跳过。
    emb_groups, emb_skip = await group_kbs_by_embedding_model(db, local_ids)
    if emb_skip:
        # 纯图文库场景：无需向量召回，直接走 BM25
        vec_kb_ids = set(local_ids) - emb_skip
    else:
        vec_kb_ids = set(local_ids)
    if emb_groups and vec_kb_ids:
        try:
            vec_hits, vec_warn = await _search_vectors_by_model(
                db, tenant_id=ps.tenant_id, query=query, pf=pf, groups=emb_groups,
                candidate_k=candidate_k, max_scan=settings.vector_max_scan or None,
            )
            if settings.retrieval_vec_min > 0:
                vec_hits = [h for h in vec_hits if h.score >= settings.retrieval_vec_min]
            rankings.append(("vector", vec_hits))
            if vec_warn:
                degraded = True
                warnings.extend(vec_warn)
        except Exception:  # noqa: BLE001
            from app.core.logging import get_logger

            get_logger("retrieval").exception("vector_search_failed")
            degraded = True
            warnings.append("vector_unavailable")

    # 关键词召回（BM25，带索引缓存：避免每次全库重分词）
    if use_hybrid:
        from app.retrieval import bm25_cache

        principals = list(pf.principals)
        cached = bm25_cache.get(ps.tenant_id, pf.accessible_kb_ids, pf.bypass_kb, principals)
        if cached:
            index, rows = cached
            kw_hits = await keyword_search(db, query=query, pf=pf, top_k=candidate_k, index=index, rows=rows)
        else:
            # 本次新建索引时写入缓存（键含 principals，防跨权限串缓存）
            def _cache_put(idx, rws, _t=ps.tenant_id, _k=pf.accessible_kb_ids,
                           _b=pf.bypass_kb, _p=principals):
                bm25_cache.put(_t, _k, _b, rws, _p)

            kw_hits = await keyword_search(
                db, query=query, pf=pf, top_k=candidate_k, on_build=_cache_put
            )
        if settings.retrieval_bm25_min > 0:
            kw_hits = [h for h in kw_hits if h.score >= settings.retrieval_bm25_min]
        rankings.append(("bm25", kw_hits))

    if len(rankings) > 1:
        wmap = {"vector": settings.rrf_weight_vector, "bm25": settings.rrf_weight_bm25,
                "external": settings.rrf_weight_external}
        fused = rrf_fuse([r for _s, r in rankings], k=settings.rrf_k,
                         top_n=max(candidate_k, top_k),
                         weights=[wmap.get(s, 1.0) for s, _r in rankings])
    elif rankings:
        fused = rankings[0][1][:max(candidate_k, top_k)]
    else:
        fused = []

    # MMR 去冗余（可选）：在重排前对候选做多样性重排，避免近重复内容挤占名额。
    # 关键：MMR 的候选池必须足够大才有意义——用候选召回池（candidate_k）而非最终 top_k，
    # 否则只在 top_k 内去重，几乎无效（与 rerank 是否开启无关）。
    if settings.mmr_enabled and len(fused) > top_k:
        from app.retrieval.mmr import mmr_select

        pool_n = max(top_k, min(len(fused), max(candidate_k, settings.rerank_pool)))
        fused = await mmr_select(db, fused, top_k=pool_n, lambda_=settings.mmr_lambda)

    # 重排（可选）
    rerank_on = settings.rerank_enabled if use_rerank is None else use_rerank
    if rerank_on and fused:
        fused, rr_degraded = await _rerank(
            db, tenant_id=ps.tenant_id, query=query, fused=fused,
            pool=settings.rerank_pool, top_k=top_k,
        )
        if rr_degraded:
            warnings.append("rerank_unavailable")

    fused = fused[:top_k]

    # 相关度阈值过滤：用**原始相似度**（向量余弦/BM25 分）判断，而非 RRF 融合分
    # （RRF 是排名制、跨查询不可比，拿它比阈值会导致阈值形同虚设）。
    # 注意：score_threshold 是「向量余弦」语义，对纯 BM25 命中不适用——
    # 纯关键词库的命中只受 retrieval_bm25_min 约束（在 BM25 召回处已过滤），此处豁免。
    if score_threshold > 0:
        def _pass(h) -> bool:
            srcs = getattr(h, "sources", None) or set()
            # 仅 BM25 路命中、且无向量/external 参与 → 不由向量阈值裁决
            if srcs and srcs == {"bm25"}:
                return True
            return (h.raw_score if h.raw_score is not None else h.score) >= score_threshold

        fused = [h for h in fused if _pass(h)]

    # 词面依据复核（向量通路的兜底）。
    #
    # 为什么必须有这一步：BM25 召回在 keyword_search 里已按「覆盖率 + 双字组」
    # 挡掉无关片段，向量召回却没有任何依据判据——余弦只要不是 0 就会进候选，
    # 而小库（或方向不完全一致的分块）里余弦低到 0.05 的无关文档照样能占满 top_k。
    # 线上表现就是：问「打开必应并搜索吕不韦」，回答下面挂着两份开发文档。
    #
    # 这里刻意**只在低分时**复核，且**只丢弃词面完全无依据的**：
    # - 高分命中不动：语义检索的价值就在于用不同措辞命中（问「怎么退货」命中
    #   「退换货规定」），拿字面去卡会毁掉它的主要优点；
    # - 低分 + 词面无依据 = 纯噪声，留着只会误导用户；
    # - 低分但词面有依据（比如问句里的实词确实出现在正文）说明有真实关联，
    #   只是措辞不同或分块偏长把分数摊薄了，保留。
    if retrieval_lexical_guard and fused:
        from app.retrieval.bm25 import has_lexical_evidence

        floor = settings.retrieval_vec_guard_floor

        def _keep(h) -> bool:
            # 外部知识库的排序由对方决定，本地词面判据不适用。
            # 用 ext_ref 判断而非 sources：sources 只在多路 RRF 融合时填充，
            # 只命中外部源（单路）时它是空集，看 sources 会把外部结果全丢掉。
            if getattr(h, "ext_ref", None):
                return True
            srcs = getattr(h, "sources", None) or set()
            # 纯 BM25 命中已在召回处判过，无需重复
            if srcs == {"bm25"}:
                return True
            raw = h.raw_score if h.raw_score is not None else h.score
            # 分数已明显高于地板 → 不当成噪声（语义命中，措辞可能完全不同）
            if raw is not None and floor > 0 and raw >= floor:
                return True
            return has_lexical_evidence(query, h.parent_content or h.content)

        kept = [h for h in fused if _keep(h)]
        dropped = len(fused) - len(kept)
        if dropped:
            # 少量丢弃属正常（无关条目），全部丢光说明检索确实没找到依据 → 记 miss
            from app.core.logging import get_logger as _get_logger

            _get_logger("retrieval").info(
                "lexical_guard_dropped", dropped=dropped, kept=len(kept), query=query[:80],
            )
        fused = kept

    chunks = await _to_chunks(db, fused)
    # 未命中日志（异步、不阻塞）——供知识库运营发现缺口
    if not chunks and (query or "").strip():
        try:
            import asyncio

            from app.tasks.bg import spawn
            spawn("log_miss", _log_miss(ps.tenant_id, query, pf.accessible_kb_ids, ps.user_id))
        except Exception:  # noqa: BLE001
            pass
    return RetrievalResponse(
        query=query, chunks=chunks, timing_ms=int((time.time() - t0) * 1000),
        trusted=len(chunks) > 0, degraded=degraded, warnings=warnings,
    )


async def _log_miss(tenant_id: int, query: str, kb_ids: list[int], user_id: int | None) -> None:
    """独立 session 记录未命中（失败静默）。"""
    try:
        from app.core.db import AsyncSessionLocal
        from app.models import RetrievalMiss

        async with AsyncSessionLocal() as db:
            db.add(RetrievalMiss(
                tenant_id=tenant_id, query=query[:500],
                kb_ids=",".join(str(k) for k in (kb_ids or [])[:20]), user_id=user_id,
            ))
            await db.commit()
    except Exception:  # noqa: BLE001
        pass


async def _rerank(
    db: AsyncSession, *, tenant_id: int, query: str, fused: list[VectorHit],
    pool: int, top_k: int,
) -> tuple[list[VectorHit], bool]:
    """融合后重排。无真模型/失败时返回原序；失败返回 (原序, True)。"""
    from app.core.config import settings
    from app.core.logging import get_logger

    log = get_logger("retrieval")
    if len(fused) <= 1:
        return fused, False
    try:
        from app.providers.registry import get_rerank

        resolved = await get_rerank(db, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001
        resolved = None
    if not resolved:
        return fused, False
    driver, rm = resolved
    kind = getattr(rm, "provider_kind", None)
    if kind == "local_hash" and not settings.rerank_allow_fake:
        return fused, False

    cand = fused[:pool]
    docs = [h.parent_content or h.content for h in cand]
    try:
        if _accepts_model(driver.rerank):
            pairs = await driver.rerank(query, docs, top_n=top_k, model=rm.model_name)
        else:
            pairs = await driver.rerank(query, docs, top_n=top_k)
    except Exception:  # noqa: BLE001
        log.exception("rerank_failed")
        return fused, True

    out: list[VectorHit] = []
    seen: set[int] = set()
    rmax = max((s for _, s in pairs), default=1.0) or 1.0
    for idx, sc in pairs:
        if 0 <= idx < len(cand):
            h = cand[idx]
            h.rerank_score = float(sc)
            h.score = float(sc) / rmax
            h.source = "rerank"
            out.append(h)
            seen.add(idx)
    for i, h in enumerate(cand):
        if i not in seen:
            out.append(h)
    out.extend(fused[pool:])
    return out, False


def _accepts_model(fn) -> bool:
    import inspect

    try:
        return "model" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


async def _to_chunks(db: AsyncSession, hits: list[VectorHit]) -> list[RetrievedChunk]:
    if not hits:
        return []
    from app.models import Document

    # 本地命中才需要查文档标题；外部命中自带 ext_title
    doc_ids = list({h.doc_id for h in hits if not h.ext_ref and h.doc_id})
    title_map: dict[int, str] = {}
    att_map: dict[int, list] = {}
    if doc_ids:
        docs = (
            await db.execute(
                select(Document.id, Document.title, Document.attachments).where(Document.id.in_(doc_ids))
            )
        ).all()
        title_map = {d[0]: d[1] for d in docs}
        # 图文条目：把配套附件带出来（命中后随回答发给提问者）
        att_map = {d[0]: d[2] for d in docs if d[2]}

    out: list[RetrievedChunk] = []
    for h in hits:
        # 父子补全：命中子块时用父块内容增强上下文
        content = h.parent_content or h.content
        if h.ext_ref:
            out.append(
                RetrievedChunk(
                    chunk_id=0, doc_id=0, kb_id=h.kb_id,
                    content=content, score=round(h.score, 4), page=h.page,
                    doc_title=h.ext_title or f"外部知识库#{h.kb_id}",
                    source="external", external=True, source_uri=h.source_uri,
                )
            )
        else:
            out.append(
                RetrievedChunk(
                    chunk_id=h.chunk_id, doc_id=h.doc_id, kb_id=h.kb_id,
                    content=content, score=round(h.score, 4), page=h.page,
                    doc_title=title_map.get(h.doc_id), source="fused",
                    attachments=att_map.get(h.doc_id),
                )
            )
    return out


def to_citations(chunks: list[RetrievedChunk]) -> list[Citation]:
    return [
        Citation(
            chunk_id=c.chunk_id,
            doc_id=c.doc_id,
            doc_title=c.doc_title,
            page=c.page,
            score=c.score,
            snippet=(c.content or "")[:120],
            source_uri=c.source_uri,
            attachments=c.attachments,
        )
        for c in chunks
    ]
