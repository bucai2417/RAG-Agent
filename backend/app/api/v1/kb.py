"""知识库接口：CRUD + 成员可见性。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.concurrency import check_row_version
from app.core.db import get_db
from app.core.errors import NotFoundError, PermissionDeniedError
from app.middleware.auth_dep import get_principal_set, load_principal_set, require_permission
from app.models import KBMember, KnowledgeBase, User
from app.schemas.kb import ConnectorTestIn, KBCreate, KBOut, KBUpdate, MemberIn
from app.services.audit_service import audited
from app.services.permission import PrincipalSet, user_principal

router = APIRouter(prefix="/kbs", tags=["knowledge_base"])


@router.get("", response_model=list[KBOut])
async def list_kbs(
    user: User = Depends(require_permission("kb:read")),
    ps: PrincipalSet = Depends(get_principal_set),
    db: AsyncSession = Depends(get_db),
) -> list[KnowledgeBase]:
    from app.services.retrieval_service import resolve_accessible_kbs

    accessible = await resolve_accessible_kbs(db, ps)
    if not accessible:
        return []
    rows = (
        await db.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.tenant_id == user.tenant_id,
                KnowledgeBase.id.in_(accessible),
                KnowledgeBase.status == "active",
            )
        )
    ).scalars().all()
    return list(rows)


@router.post("", response_model=KBOut)
@audited("kb.create", "kb")
async def create_kb(
    body: KBCreate,
    user: User = Depends(require_permission("kb:create")),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeBase:
    from app.services import tenant_service

    await tenant_service.ensure_kb_quota(db, user.tenant_id)
    kb = KnowledgeBase(
        tenant_id=user.tenant_id,
        name=body.name,
        description=body.description,
        icon=body.icon,
        visibility=body.visibility,
        embedding_model_id=body.embedding_model_id,
        chunk_strategy=body.chunk_strategy,
        source_type=body.source_type,
        index_mode=(body.index_mode if body.source_type == "local" else "vector"),
        connector_kind=body.connector_kind if body.source_type == "external" else None,
        connector_config=_encrypt_connector(body.connector_config) if body.source_type == "external" else None,
        owner_id=user.id,
    )
    db.add(kb)
    await db.flush()
    # 创建者自动成为 manager。SQLite 会复用已删库的 rowid，可能残留旧 kb_member 行
    # （软删的库其成员未清理）→ 先清理该 kb_id 的残留，再插入，避免唯一约束冲突。
    from sqlalchemy import delete as _del

    await db.execute(_del(KBMember).where(KBMember.kb_id == kb.id))
    db.add(
        KBMember(
            tenant_id=user.tenant_id,
            kb_id=kb.id,
            principal_id=user_principal(user.id),
            perm_level="manager",
            granted_by=user.id,
        )
    )
    await db.flush()
    return kb


@router.get("/{kb_id}", response_model=KBOut)
async def get_kb(
    kb_id: int,
    _guard: User = Depends(require_permission("kb:read")),
    ps: PrincipalSet = Depends(get_principal_set),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeBase:
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.tenant_id != ps.tenant_id:
        raise NotFoundError("知识库不存在")
    await _ensure_access(db, ps, kb)
    kb.my_perm = await _effective_perm(db, ps, kb)
    return kb


@router.patch("/{kb_id}", response_model=KBOut)
@audited("kb.update", "kb", id_arg="kb_id")
async def update_kb(
    kb_id: int,
    body: KBUpdate,
    user: User = Depends(require_permission("kb:update")),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeBase:
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.tenant_id != user.tenant_id:
        raise NotFoundError("知识库不存在")
    if not await _can_manage(db, user, kb):
        raise PermissionDeniedError("无管理权限")
    check_row_version(kb, body.row_version)
    patch = body.model_dump(exclude_unset=True)
    patch.pop("row_version", None)
    if "connector_config" in patch:
        # api_key 已是密文则保留，否则加密；clear 则整体清空
        patch["connector_config"] = _encrypt_connector(patch["connector_config"], existing=kb.connector_config)
    if "source_type" in patch and patch["source_type"] != "external":
        # 转回本地库时清空连接器配置
        patch["connector_kind"] = None
        patch["connector_config"] = None
    for field, val in patch.items():
        setattr(kb, field, val)
    await db.flush()
    from app.connectors.registry import invalidate_cache

    invalidate_cache()
    return kb


@router.get("/{kb_id}/stats")
async def kb_stats(
    kb_id: int,
    _guard: User = Depends(require_permission("kb:read")),
    ps: PrincipalSet = Depends(get_principal_set),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """知识库统计：文档/分块计数、状态分布、类型分布、存储占用、向量覆盖。"""
    from sqlalchemy import func as _func

    from app.models import Chunk, Document

    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.tenant_id != ps.tenant_id:
        raise NotFoundError("知识库不存在")
    await _ensure_access(db, ps, kb)

    # 文档维度
    doc_rows = (
        await db.execute(
            select(Document.status, Document.file_ext, Document.file_size)
            .where(Document.kb_id == kb_id)
        )
    ).all()
    by_status: dict[str, int] = {}
    by_ext: dict[str, int] = {}
    total_size = 0
    for status, ext, size in doc_rows:
        by_status[status] = by_status.get(status, 0) + 1
        key = (ext or "其它").lower()
        by_ext[key] = by_ext.get(key, 0) + 1
        total_size += int(size or 0)

    # 分块维度
    chunk_total = (
        await db.execute(select(_func.count()).select_from(Chunk).where(Chunk.kb_id == kb_id))
    ).scalar_one()
    # 有向量的分块数（embedding 非空）。SQLite 上 VectorType 存文本，非空即已嵌入
    embedded = (
        await db.execute(
            select(_func.count()).select_from(Chunk).where(
                Chunk.kb_id == kb_id, Chunk.embedding.is_not(None)
            )
        )
    ).scalar_one()

    return {
        "doc_count": len(doc_rows),
        "chunk_count": int(chunk_total or 0),
        "embedded_chunks": int(embedded or 0),
        "embedded_ratio": round((embedded or 0) / chunk_total, 4) if chunk_total else 0.0,
        "total_size": total_size,
        "by_status": by_status,
        "by_ext": by_ext,
    }


@router.get("/{kb_id}/audit")
async def kb_audit(
    kb_id: int,
    stale_days: int = 180,
    _guard: User = Depends(require_permission("kb:read")),
    ps: PrincipalSet = Depends(get_principal_set),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """内容巡检：空/失败/无分块/未标签/陈旧文档清单。"""
    from app.services.kb_audit_service import audit_kb

    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.tenant_id != ps.tenant_id:
        raise NotFoundError("知识库不存在")
    await _ensure_access(db, ps, kb)
    return await audit_kb(db, kb_id=kb_id, tenant_id=ps.tenant_id, stale_days=stale_days)


# ===== 连接器配置（外部知识库）=====
@router.post("/connector/test")
async def test_connector(
    body: ConnectorTestIn,
    _guard: User = Depends(require_permission("kb:create")),
) -> dict:
    """创建前验证连接器配置：SSRF 守卫 + 实际检索一次，返回预览。"""
    from app.connectors.http_guard import assert_safe_url
    from app.connectors.registry import build_connector

    cfg = dict(body.connector_config or {})
    base = cfg.get("base_url")
    if base:
        assert_safe_url(base)
    conn = build_connector(body.connector_kind, cfg, kb_id=0)
    docs = await conn.search(body.query, top_k=body.top_k)
    return {
        "ok": True,
        "count": len(docs),
        "items": [
            {"title": d.title, "content": (d.content or "")[:300], "score": d.score, "source_uri": d.source_uri}
            for d in docs
        ],
    }


@router.get("/{kb_id}/connector")
async def get_connector(
    kb_id: int,
    _guard: User = Depends(require_permission("kb:read")),
    ps: PrincipalSet = Depends(get_principal_set),
    db: AsyncSession = Depends(get_db),
) -> dict:
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.tenant_id != ps.tenant_id:
        raise NotFoundError("知识库不存在")
    await _ensure_access(db, ps, kb)
    return {
        "source_type": kb.source_type or "local",
        "connector_kind": kb.connector_kind,
        "config": _masked_connector(kb.connector_config),
    }


@router.post("/{kb_id}/connector/test")
async def test_kb_connector(
    kb_id: int,
    body: dict,
    user: User = Depends(require_permission("kb:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.connectors.http_guard import assert_safe_url
    from app.connectors.registry import from_kb

    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.tenant_id != user.tenant_id:
        raise NotFoundError("知识库不存在")
    if kb.source_type != "external":
        raise PermissionDeniedError("该知识库不是外部来源")
    conn = from_kb(kb)
    if conn is None:
        raise PermissionDeniedError("连接器未配置")
    base = (kb.connector_config or {}).get("base_url")
    if base:
        assert_safe_url(base)
    query = (body or {}).get("query") or "测试"
    docs = await conn.search(query, top_k=int((body or {}).get("top_k") or 3))
    return {
        "ok": True,
        "count": len(docs),
        "items": [
            {"title": d.title, "content": (d.content or "")[:300], "score": d.score, "source_uri": d.source_uri}
            for d in docs
        ],
    }


# ===== 导入同步（模式 B：拉取远端内容落本地索引）=====
@router.get("/{kb_id}/sync/status")
async def kb_sync_status(
    kb_id: int,
    _guard: User = Depends(require_permission("kb:read")),
    ps: PrincipalSet = Depends(get_principal_set),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from sqlalchemy import func as _func

    from app.models import Document
    from app.services.kb_sync_service import sync_status

    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.tenant_id != ps.tenant_id:
        raise NotFoundError("知识库不存在")
    await _ensure_access(db, ps, kb)
    st = sync_status(kb)
    st["synced_doc_count"] = (
        await db.execute(
            select(_func.count()).select_from(Document).where(
                Document.kb_id == kb_id, Document.source_type == "external_sync"
            )
        )
    ).scalar_one()
    return st


@router.post("/{kb_id}/sync")
@audited("kb.sync", "kb", id_arg="kb_id")
async def kb_sync(
    kb_id: int,
    body: dict | None = None,
    user: User = Depends(require_permission("kb:update")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """从外部系统拉取内容并导入本地索引（幂等，重复同步不产生重复文档）。"""
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.tenant_id != user.tenant_id:
        raise NotFoundError("知识库不存在")
    if not await _can_manage(db, user, kb):
        raise PermissionDeniedError("无管理权限")
    from app.services.kb_sync_service import sync_external_kb

    limit = int((body or {}).get("limit") or 500)
    return await sync_external_kb(db, kb_id, limit=limit)


@router.put("/{kb_id}/sync/config")
@audited("kb.sync_config", "kb", id_arg="kb_id")
async def kb_sync_config(
    kb_id: int,
    body: dict,
    user: User = Depends(require_permission("kb:update")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """配置导入同步：启用开关、种子查询（连接器无批量列举能力时的检索词）、单次上限。"""
    from app.services.kb_sync_service import SYNC_ENABLED, SYNC_LIMIT, SYNC_SEED_QUERIES

    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.tenant_id != user.tenant_id:
        raise NotFoundError("知识库不存在")
    if not await _can_manage(db, user, kb):
        raise PermissionDeniedError("无管理权限")
    settings = dict(kb.settings or {})
    if "enabled" in body:
        settings[SYNC_ENABLED] = bool(body["enabled"])
    if "seed_queries" in body:
        settings[SYNC_SEED_QUERIES] = body["seed_queries"] or []
    if "limit" in body:
        settings[SYNC_LIMIT] = int(body["limit"] or 500)
    kb.settings = settings
    await db.flush()
    from app.services.kb_sync_service import sync_status

    return sync_status(kb)


@router.delete("/{kb_id}")
@audited("kb.delete", "kb", id_arg="kb_id")
async def delete_kb(
    kb_id: int,
    user: User = Depends(require_permission("kb:delete")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.tenant_id != user.tenant_id:
        raise NotFoundError("知识库不存在")
    if not await _can_manage(db, user, kb):
        raise PermissionDeniedError("无管理权限")
    kb.status = "deleted"
    # 清理成员授权，避免残留（SQLite rowid 复用会导致新库撞唯一约束）
    from sqlalchemy import delete as _del

    await db.execute(_del(KBMember).where(KBMember.kb_id == kb_id))
    # 清理名下文档与分块，避免软删库的文档/分块仍被检索或统计带出（产生脏数据）
    from app.models import Chunk, Document

    doc_ids = [
        d for (d,) in (await db.execute(select(Document.id).where(Document.kb_id == kb_id))).all()
    ]
    if doc_ids:
        await db.execute(_del(Chunk).where(Chunk.doc_id.in_(doc_ids)))
        await db.execute(_del(Document).where(Document.id.in_(doc_ids)))
    # 清理文档文件夹
    from app.models import DocumentFolder

    await db.execute(_del(DocumentFolder).where(DocumentFolder.kb_id == kb_id))
    await db.flush()
    return {"message": "已删除", "removed_documents": len(doc_ids)}


@router.get("/{kb_id}/members")
async def list_members(
    kb_id: int,
    user: User = Depends(require_permission("kb:read")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.tenant_id != user.tenant_id:
        raise NotFoundError("知识库不存在")
    rows = (
        await db.execute(select(KBMember).where(KBMember.kb_id == kb_id))
    ).scalars().all()

    from app.models import Department, Role, UserGroup
    from app.services.permission import DEPT_BASE, GROUP_BASE, ROLE_BASE, USER_BASE

    def _split(pid: int) -> tuple[str, int]:
        if pid >= GROUP_BASE:
            return "group", pid - GROUP_BASE
        if pid >= ROLE_BASE:
            return "role", pid - ROLE_BASE
        if pid >= DEPT_BASE:
            return "department", pid - DEPT_BASE
        if pid >= USER_BASE:
            return "user", pid - USER_BASE
        return "public", pid

    typed = [(r, *_split(r.principal_id)) for r in rows]
    uids = [i for _, t, i in typed if t == "user"]
    dids = [i for _, t, i in typed if t == "department"]
    rids = [i for _, t, i in typed if t == "role"]
    gids = [i for _, t, i in typed if t == "group"]

    umap: dict[int, User] = {}
    if uids:
        umap = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(uids)))).scalars().all()}
    dmap: dict[int, Department] = {}
    if dids:
        dmap = {d.id: d for d in (await db.execute(select(Department).where(Department.id.in_(dids)))).scalars().all()}
    rmap: dict[int, Role] = {}
    if rids:
        rmap = {r.id: r for r in (await db.execute(select(Role).where(Role.id.in_(rids)))).scalars().all()}
    gmap: dict[int, UserGroup] = {}
    if gids:
        gmap = {g.id: g for g in (await db.execute(select(UserGroup).where(UserGroup.id.in_(gids)))).scalars().all()}

    def _name(t: str, i: int) -> str:
        if t == "user" and i in umap:
            return umap[i].display_name or umap[i].username
        if t == "department" and i in dmap:
            return dmap[i].name
        if t == "role" and i in rmap:
            return rmap[i].name
        if t == "group" and i in gmap:
            return gmap[i].name
        return f"{t}#{i}"

    return [
        {
            "id": r.id,
            "principal_id": r.principal_id,
            "principal_type": ptype,
            "principal_ref_id": pid_ref,
            "user_id": pid_ref if ptype == "user" else None,
            "display_name": _name(ptype, pid_ref),
            "perm_level": r.perm_level,
        }
        for r, ptype, pid_ref in typed
    ]


@router.post("/{kb_id}/members")
@audited("kb.member_add", "kb", id_arg="kb_id")
async def add_member(
    kb_id: int,
    body: MemberIn,
    user: User = Depends(require_permission("kb:member_manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.tenant_id != user.tenant_id:
        raise NotFoundError("知识库不存在")
    if not await _can_manage(db, user, kb):
        raise PermissionDeniedError("无管理权限")
    from app.services import permission as P

    mapping = {
        "user": P.user_principal,
        "department": P.dept_principal,
        "role": P.role_principal,
        "group": P.group_principal,
    }
    if body.principal_type not in mapping:
        raise PermissionDeniedError(f"不支持的授权对象类型: {body.principal_type}")
    if body.perm_level not in ("viewer", "editor", "manager"):
        raise PermissionDeniedError("perm_level 只能为 viewer/editor/manager")
    pid = mapping[body.principal_type](body.principal_id)
    # 已存在则更新权限，否则新增
    exist = (
        await db.execute(
            select(KBMember).where(KBMember.kb_id == kb_id, KBMember.principal_id == pid)
        )
    ).scalar_one_or_none()
    if exist:
        exist.perm_level = body.perm_level
    else:
        db.add(
            KBMember(
                tenant_id=user.tenant_id, kb_id=kb_id, principal_id=pid,
                perm_level=body.perm_level, granted_by=user.id,
            )
        )
    await db.flush()
    return {"message": "已保存成员"}


@router.delete("/{kb_id}/members/{member_id}")
@audited("kb.member_remove", "kb", id_arg="kb_id")
async def remove_member(
    kb_id: int,
    member_id: int,
    user: User = Depends(require_permission("kb:member_manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.tenant_id != user.tenant_id:
        raise NotFoundError("知识库不存在")
    if not await _can_manage(db, user, kb):
        raise PermissionDeniedError("无管理权限")
    m = await db.get(KBMember, member_id)
    if not m or m.kb_id != kb_id:
        raise NotFoundError("成员不存在")
    await db.delete(m)
    await db.flush()
    return {"message": "已移除"}


# ---- 内部权限判定 ----
def _encrypt_connector(cfg: dict | None, existing: dict | None = None) -> dict | None:
    """加密连接器配置中的 api_key。已是密文（enc: 前缀）则原样保留；
    前端传空但库里已有密文时，沿用旧密文（用户未改动的场景）。"""
    from app.core.crypto import encrypt

    if not cfg:
        return cfg
    cfg = dict(cfg)
    key = cfg.get("api_key")
    if key and key.startswith("enc:"):
        pass  # 已是密文
    elif not key and existing and existing.get("api_key"):
        cfg["api_key"] = existing["api_key"]  # 未改动，沿用旧值
    elif key:
        cfg["api_key"] = encrypt(key)
    return cfg


def _masked_connector(cfg: dict | None) -> dict | None:
    """脱敏：api_key 只回显提示，供前端展示。"""
    from app.core.crypto import mask

    if not cfg:
        return cfg
    out = dict(cfg)
    if out.get("api_key"):
        out["api_key"] = mask(out["api_key"])
        out["api_key_set"] = True
    else:
        out["api_key_set"] = False
    return out


def _can_manage_kb(user: User, kb: KnowledgeBase) -> bool:
    return bool(user.is_admin or kb.owner_id == user.id)


async def _ensure_access(db: AsyncSession, ps: PrincipalSet, kb: KnowledgeBase) -> None:
    # public：本租户全员可见；internal：仅内部用户可见（外部客户不得）；private：owner 与显式成员
    if kb.visibility == "public":
        return
    if kb.visibility == "internal" and not ps.is_external:
        return
    if ps.is_admin or kb.owner_id == ps.user_id:
        return
    member = (
        await db.execute(
            select(KBMember).where(
                KBMember.kb_id == kb.id,
                KBMember.principal_id.in_(ps.principals),
            )
        )
    ).scalars().first()
    if not member:
        raise PermissionDeniedError("无权访问该知识库")


async def _can_manage(db: AsyncSession, user: User, kb: KnowledgeBase) -> bool:
    if user.is_admin or kb.owner_id == user.id:
        return True
    ps = await load_principal_set(db, user)
    member = (
        await db.execute(
            select(KBMember).where(
                KBMember.kb_id == kb.id,
                KBMember.principal_id.in_(ps.principals),
                KBMember.perm_level == "manager",
            )
        )
    ).scalars().first()
    return member is not None


async def _effective_perm(db: AsyncSession, ps: PrincipalSet, kb: KnowledgeBase) -> str:
    """当前用户对该库的有效级别：owner/manager/editor/viewer。"""
    if ps.is_admin or kb.owner_id == ps.user_id:
        return "owner"
    rows = (
        await db.execute(
            select(KBMember.perm_level).where(
                KBMember.kb_id == kb.id,
                KBMember.principal_id.in_(ps.principals),
            )
        )
    ).scalars().all()
    for lvl in ("manager", "editor", "viewer"):
        if lvl in rows:
            return lvl
    return "viewer"  # 公开/内部库的本租户用户：只读
