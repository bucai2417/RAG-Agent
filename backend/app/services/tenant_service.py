"""租户（组织）生命周期与配额。

原先租户只在 bootstrap 里建一个 ``default``，注册把所有新用户都塞进
``select(Tenant).order_by(id).first()`` —— 一个部署只能有一个组织。
本模块补上「建组织、开户、配额、停用」这些运营动作，并把配额判定收敛到一处，
避免上传、建库、建号三个入口各写一套阈值比较。

配额一律 0 = 不限：默认部署的行为与改造前完全一致。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, QuotaExceededError, ValidationError
from app.core.logging import get_logger

logger = get_logger("tenant")

STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_ARCHIVED = "archived"
_STATUSES = (STATUS_ACTIVE, STATUS_SUSPENDED, STATUS_ARCHIVED)

# slug 会出现在 URL 与邀请链接里：只允许小写字母/数字/连字符，且不以连字符开头结尾
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")

# 保留 slug：避免与前端路由或系统标识冲突
_RESERVED_SLUGS = {
    "default", "admin", "api", "app", "auth", "login", "register", "system",
    "settings", "static", "assets", "www", "root", "null", "undefined",
}


@dataclass
class QuotaSnapshot:
    """一次配额盘点结果。``None`` 表示该项不限。"""

    max_users: int
    max_documents: int
    max_storage_bytes: int
    users: int
    documents: int
    storage_bytes: int

    @property
    def users_over(self) -> bool:
        return self.max_users > 0 and self.users >= self.max_users

    @property
    def documents_over(self) -> bool:
        return self.max_documents > 0 and self.documents >= self.max_documents

    def storage_over(self, incoming: int = 0) -> bool:
        return (
            self.max_storage_bytes > 0
            and self.storage_bytes + max(0, incoming) > self.max_storage_bytes
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_users": self.max_users,
            "max_documents": self.max_documents,
            "max_storage_bytes": self.max_storage_bytes,
            "users": self.users,
            "documents": self.documents,
            "storage_bytes": self.storage_bytes,
        }


def validate_slug(slug: str) -> str:
    """规范化并校验 slug；不合法直接抛 422 而不是让唯一约束去兜。"""
    s = (slug or "").strip().lower()
    if not s:
        raise ValidationError("请填写组织标识（slug）")
    if not _SLUG_RE.match(s):
        raise ValidationError(
            "组织标识只能包含小写字母、数字与连字符，且不能以连字符开头/结尾（1~64 位）"
        )
    if s in _RESERVED_SLUGS:
        raise ValidationError(f"组织标识「{s}」是系统保留字，请换一个")
    return s


async def get_by_slug(db: AsyncSession, slug: str) -> Any | None:
    from app.models import Tenant

    s = (slug or "").strip().lower()
    if not s:
        return None
    return (
        await db.execute(select(Tenant).where(func.lower(Tenant.slug) == s))
    ).scalar_one_or_none()


async def get_or_404(db: AsyncSession, tenant_id: int) -> Any:
    from app.models import Tenant

    t = await db.get(Tenant, tenant_id)
    if t is None:
        raise NotFoundError("组织不存在")
    return t


async def resolve_registration_tenant(
    db: AsyncSession, *, slug: str | None = None, invite_code: str | None = None
) -> Any:
    """注册时决定账号落到哪个组织。

    优先级：邀请码 > slug > 唯一的默认组织。

    为什么不保留「取 id 最小的那个」这个兜底：多组织部署下它会让所有注册都进
    1 号组织，而调用方看不出这件事；单组织部署下又与「唯一组织」等价，所以这里
    只在**确实只有一个组织**时才允许无参数注册，多组织时必须显式指定。
    """
    from app.models import Tenant

    code = (invite_code or "").strip()
    if code:
        t = (
            await db.execute(select(Tenant).where(Tenant.invite_code == code))
        ).scalar_one_or_none()
        if t is None:
            raise ValidationError("邀请码无效或已失效")
        _ensure_can_join(t)
        return t

    s = (slug or "").strip()
    if s:
        t = await get_by_slug(db, s)
        if t is None:
            raise ValidationError("组织不存在，请核对组织标识")
        _ensure_can_join(t)
        return t

    rows = list((await db.execute(select(Tenant).order_by(Tenant.id).limit(2))).scalars().all())
    if not rows:
        raise ValidationError("系统尚未初始化，请联系管理员")
    if len(rows) > 1:
        raise ValidationError("本系统有多个组织，请填写组织标识或邀请码")
    _ensure_can_join(rows[0])
    return rows[0]


def _ensure_can_join(t: Any) -> None:
    status = getattr(t, "status", STATUS_ACTIVE) or STATUS_ACTIVE
    if status != STATUS_ACTIVE:
        raise ValidationError("该组织当前不接受新成员（已停用或已归档）")


async def quota_snapshot(db: AsyncSession, tenant_id: int) -> QuotaSnapshot:
    """盘点当前用量。文档数与存储按租户统计（文件大小取 document.file_size）。"""
    from app.models import Document, Tenant, User

    t = await get_or_404(db, tenant_id)
    users = int(
        (await db.execute(select(func.count(User.id)).where(User.tenant_id == tenant_id)))
        .scalar_one()
        or 0
    )
    doc_row = (
        await db.execute(
            select(func.count(Document.id), func.coalesce(func.sum(Document.file_size), 0)).where(
                Document.tenant_id == tenant_id
            )
        )
    ).one()
    return QuotaSnapshot(
        max_users=int(getattr(t, "max_users", 0) or 0),
        max_documents=int(getattr(t, "max_documents", 0) or 0),
        max_storage_bytes=int(getattr(t, "max_storage_bytes", 0) or 0),
        users=users,
        documents=int(doc_row[0] or 0),
        storage_bytes=int(doc_row[1] or 0),
    )


async def ensure_user_quota(db: AsyncSession, tenant_id: int) -> None:
    """新账号落库前检查人数配额。0 = 不限，此时不做任何查询。"""
    from app.models import Tenant

    t = await db.get(Tenant, tenant_id)
    if t is None:
        raise NotFoundError("组织不存在")
    if int(getattr(t, "max_users", 0) or 0) <= 0:
        return
    snap = await quota_snapshot(db, tenant_id)
    if snap.users_over:
        raise QuotaExceededError(
            f"该组织已达到人数上限（{snap.max_users}），请联系管理员扩容",
            detail={"reason": "user_quota", **snap.as_dict()},
        )


async def ensure_document_quota(db: AsyncSession, tenant_id: int, incoming_bytes: int = 0) -> None:
    """上传前检查文档数/存储配额。任一上限为 0 即跳过对应判断。"""
    from app.models import Tenant

    t = await db.get(Tenant, tenant_id)
    if t is None:
        raise NotFoundError("组织不存在")
    max_docs = int(getattr(t, "max_documents", 0) or 0)
    max_bytes = int(getattr(t, "max_storage_bytes", 0) or 0)
    if max_docs <= 0 and max_bytes <= 0:
        return
    snap = await quota_snapshot(db, tenant_id)
    if max_docs > 0 and snap.documents_over:
        raise QuotaExceededError(
            f"该组织已达到文档数上限（{snap.max_documents}），请删除部分文档或联系管理员扩容",
            detail={"reason": "document_quota", **snap.as_dict()},
        )
    if snap.storage_over(incoming_bytes):
        raise QuotaExceededError(
            f"该组织存储空间不足（上限 {snap.max_storage_bytes} 字节），请联系管理员扩容",
            detail={"reason": "storage_quota", **snap.as_dict()},
        )


async def ensure_kb_quota(db: AsyncSession, tenant_id: int) -> None:
    """建库前检查知识库数量——沿用文档数配额（0 = 不限）。

    单独开一个字段会让设置页多一个几乎没人调的旋钮；「文档数」已经是最贴近
    存储压力的口径，知识库本身不占空间。
    """
    await ensure_document_quota(db, tenant_id, 0)


def normalize_status(status: str) -> str:
    s = (status or "").strip().lower()
    if s not in _STATUSES:
        raise ValidationError(f"状态只能是 {' / '.join(_STATUSES)}")
    return s


async def assert_slug_free(db: AsyncSession, slug: str, *, exclude_id: int | None = None) -> None:
    existing = await get_by_slug(db, slug)
    if existing is not None and existing.id != exclude_id:
        raise ConflictError(f"组织标识「{slug}」已被占用")


async def bind_tenant_admin(
    db: AsyncSession, *, tenant_id: int, user_id: int, granted_by: int | None = None
) -> None:
    """把某用户设为该组织管理员（tenant_admin，tenant 作用域）。

    为什么不直接置 ``is_admin=True``：``is_admin`` 是历史直通位，任何租户级
    判断都会放行；新组织的管理员应当是「本组织的 tenant_admin」，与权限模型
    保持一致，也避免以后审计时说不清权限从哪来。
    """
    from app.models import Role, UserRole

    role = (
        await db.execute(
            select(Role).where(Role.tenant_id.is_(None), Role.code == "tenant_admin")
        )
    ).scalar_one_or_none()
    if role is None:
        logger.warning("tenant_admin_role_missing", tenant_id=tenant_id)
        return
    exists = (
        await db.execute(
            select(UserRole.id).where(
                UserRole.user_id == user_id,
                UserRole.role_id == role.id,
                UserRole.scope_type == "tenant",
            )
        )
    ).scalar_one_or_none()
    if exists:
        return
    db.add(
        UserRole(
            tenant_id=tenant_id,
            user_id=user_id,
            role_id=role.id,
            scope_type="tenant",
            scope_id=0,
            granted_by=granted_by,
        )
    )
    await db.flush()


async def purge_user_grants(db: AsyncSession, user_id: int) -> int:
    """删除用户的全部角色授予，返回删除行数。

    为什么必须显式清理：``user_role`` 没有外键级联，用户被物理删除后授予行会
    留在表里。SQLite 会复用被删行的 rowid，于是「新建一个用户」可能直接继承
    上一个用户的授予——包括 platform 作用域的 super_admin。
    这类残留只在删除用户时产生，所以在删用户的同一事务里清掉。
    """
    from sqlalchemy import delete as _delete

    from app.models import UserRole

    res = await db.execute(_delete(UserRole).where(UserRole.user_id == user_id))
    return int(res.rowcount or 0)


async def tenant_options(db: AsyncSession, *, active_only: bool = True) -> list[dict]:
    """公开可见的组织选项（注册页用）：只有 slug 与名称，不含配额/备注。"""
    from app.models import Tenant

    stmt = select(Tenant).order_by(Tenant.id)
    if active_only:
        stmt = stmt.where(or_(Tenant.status == STATUS_ACTIVE, Tenant.status.is_(None)))
    rows = list((await db.execute(stmt)).scalars().all())
    return [{"slug": t.slug, "name": t.name} for t in rows]
