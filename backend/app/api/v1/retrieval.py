"""检索调试接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.middleware.auth_dep import get_principal_set, require_permission
from app.models import User
from app.schemas.chat import RetrievalRequest, RetrievalResponse
from app.services.permission import PrincipalSet
from app.services.retrieval_service import retrieve

router = APIRouter(prefix="/retrieval", tags=["retrieval"])


@router.get("/missed-queries")
async def missed_queries(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(require_permission("retrieval:query")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """最近 N 天未命中的高频问题（供补内容/优化检索）。"""
    from app.services.kb_audit_service import top_missed_queries

    return await top_missed_queries(db, tenant_id=user.tenant_id, days=days, limit=limit)


@router.post("/query", response_model=RetrievalResponse)
async def query(
    body: RetrievalRequest,
    _guard = Depends(require_permission("retrieval:query")),
    ps: PrincipalSet = Depends(get_principal_set),
    db: AsyncSession = Depends(get_db),
) -> RetrievalResponse:
    return await retrieve(
        db,
        ps=ps,
        query=body.query,
        kb_ids=body.kb_ids,
        top_k=body.top_k,
        use_hybrid=body.use_hybrid,
        score_threshold=body.score_threshold,
        candidate_k=body.candidate_k,
        use_rerank=body.use_rerank,
        retrieval_lexical_guard=body.lexical_guard,
    )
