"""技能市场接口：搜索、预览、安装、整包安装。

设计：
- 只读端点用 skill:read（能看到市场即可浏览/预览）
- 安装/更新用 skill:edit（与现有导入接口一致）
- 全部委托 app.services.skill_market_service，本层只做参数校验、权限与审计
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.errors import NotFoundError
from app.middleware.auth_dep import require_permission
from app.models import Skill, User
from app.services import skill_market_service

router = APIRouter(prefix="/skills/market", tags=["skill-market"])


class InstallIn(BaseModel):
    repo: str
    subpath: str | None = None
    upgrade_skill_id: int | None = None


class InstallCollectionIn(BaseModel):
    repo: str


@router.get("/search")
async def search_market(
    q: str = Query(default="", description="关键词；留空返回精选清单"),
    limit: int = Query(default=20, ge=1, le=50),
    user: User = Depends(require_permission("skill:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """搜索技能市场（精选 + GitHub 实时）。"""
    return await skill_market_service.search_market(
        db, tenant_id=user.tenant_id, keywords=q, limit=limit,
    )


@router.get("/detail")
async def market_detail(
    repo: str = Query(..., description="owner/repo"),
    subpath: str = Query(default="", description="多技能仓库的子技能目录"),
    user: User = Depends(require_permission("skill:read")),
) -> dict:
    """技能详情预览（直接读 GitHub raw，不落盘）。"""
    try:
        return await skill_market_service.get_market_detail(repo, subpath=subpath)
    except ValueError as e:
        raise NotFoundError(str(e))


@router.post("/install")
async def install(
    body: InstallIn,
    user: User = Depends(require_permission("skill:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """安装技能；带 upgrade_skill_id 时改为覆盖更新已有技能包。"""
    from app.services.audit_service import record_audit

    upgrade_id = body.upgrade_skill_id
    if upgrade_id is not None:
        sk = await db.get(Skill, upgrade_id)
        if not sk or sk.tenant_id != user.tenant_id:
            raise NotFoundError("技能不存在")

    r = await skill_market_service.install_from_market(
        db, tenant_id=user.tenant_id, owner_id=user.id,
        repo=body.repo, subpath=body.subpath or "",
        upgrade_skill_id=upgrade_id,
    )
    if not r.get("multi_skill") and not r.get("duplicate"):
        record_audit(
            db,
            action="skill.upgrade" if upgrade_id is not None else "skill.market_install",
            resource_type="skill", resource_id=r.get("skill_id"),
        )
    return r


@router.post("/install-collection")
async def install_collection(
    body: InstallCollectionIn,
    user: User = Depends(require_permission("skill:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """把多技能仓库整体装为「技能集合」（父 + N 个子技能）。"""
    from app.services.audit_service import record_audit

    r = await skill_market_service.install_collection_from_market(
        db, tenant_id=user.tenant_id, owner_id=user.id, repo=body.repo,
    )
    if not r.get("duplicate"):
        record_audit(
            db, action="skill.market_install_collection",
            resource_type="skill", resource_id=r.get("skill_id"),
        )
    return r
