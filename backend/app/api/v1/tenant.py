"""租户（组织）接口。

两类端点，权限模型不同，不能混：

- ``/tenants``（跨租户）——**平台管理员**专属。判定走 ``require_platform_admin``
  （platform 作用域角色），不是 ``is_admin``：任何组织的管理员都不该能列举、
  停用、改配额别的组织。
- ``/tenants/me``（本组织）——任何登录用户可读自己的组织与用量；改配额的
  ``/tenants/me/quota`` 需要本组织的 ``tenant:manage``。

公开端点（注册页用）在 auth.py 里，只暴露 slug 与名称。

**路由顺序**：``/me`` 系列必须声明在 ``/{tenant_id}`` 之前，否则 "me" 会被
当成路径参数去解析 int 而返回 422。
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import hash_password, validate_password_or_raise
from app.middleware.auth_dep import (
    get_current_user,
    is_platform_admin,
    require_permission,
    require_platform_admin,
)
from app.models import Tenant, User
from app.schemas.tenant import (
    InviteCodeOut,
    QuotaUpdate,
    TenantCreate,
    TenantMeOut,
    TenantOut,
    TenantUpdate,
)
from app.services import tenant_service
from app.services.audit_service import audited

router = APIRouter(prefix="/tenants", tags=["tenant"])


def _to_out(t: Tenant, snap: tenant_service.QuotaSnapshot | None = None) -> TenantOut:
    out = TenantOut(
        id=t.id,
        name=t.name,
        slug=t.slug,
        status=t.status or tenant_service.STATUS_ACTIVE,
        plan=t.plan or "default",
        invite_code=t.invite_code,
        max_users=int(t.max_users or 0),
        max_documents=int(t.max_documents or 0),
        max_storage_bytes=int(t.max_storage_bytes or 0),
        note=t.note,
        created_at=t.created_at.isoformat() if getattr(t, "created_at", None) else None,
    )
    return _with_usage(out, snap)


def _with_usage(out: TenantMeOut | TenantOut, snap: tenant_service.QuotaSnapshot | None):
    if snap is not None:
        out.users = snap.users
        out.documents = snap.documents
        out.storage_bytes = snap.storage_bytes
    return out


async def _me_out(db: AsyncSession, t: Tenant, user: User) -> TenantMeOut:
    snap = await tenant_service.quota_snapshot(db, t.id)
    return TenantMeOut(
        id=t.id,
        name=t.name,
        slug=t.slug,
        status=t.status or tenant_service.STATUS_ACTIVE,
        plan=t.plan or "default",
        is_platform_admin=await is_platform_admin(db, user),
        max_users=snap.max_users,
        max_documents=snap.max_documents,
        max_storage_bytes=snap.max_storage_bytes,
        users=snap.users,
        documents=snap.documents,
        storage_bytes=snap.storage_bytes,
    )


# ==================== 本组织（必须在 /{tenant_id} 之前）====================
@router.get("/me", response_model=TenantMeOut)
async def get_my_tenant(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TenantMeOut:
    """当前用户所属组织与用量（任何登录用户可读）。"""
    t = await db.get(Tenant, user.tenant_id)
    if t is None:
        raise NotFoundError("组织不存在")
    return await _me_out(db, t, user)


@router.patch("/me/quota", response_model=TenantMeOut)
@audited("tenant.quota_update", "tenant")
async def update_my_quota(
    body: QuotaUpdate,
    user: User = Depends(require_permission("tenant:manage")),
    db: AsyncSession = Depends(get_db),
) -> TenantMeOut:
    """调整本组织配额。

    为什么 tenant_admin 默认没有 tenant:manage：配额是资源/计费边界，
    不该由被限额方自己调；内置的 tenant_admin 角色刻意排除了这一项。
    """
    t = await tenant_service.get_or_404(db, user.tenant_id)
    if body.max_users is not None:
        t.max_users = max(0, body.max_users)
    if body.max_documents is not None:
        t.max_documents = max(0, body.max_documents)
    if body.max_storage_bytes is not None:
        t.max_storage_bytes = max(0, body.max_storage_bytes)
    await db.flush()
    return await _me_out(db, t, user)


@router.get("/me/invite-code", response_model=InviteCodeOut)
async def get_invite_code(
    user: User = Depends(require_permission("tenant:invite")),
    db: AsyncSession = Depends(get_db),
) -> InviteCodeOut:
    t = await tenant_service.get_or_404(db, user.tenant_id)
    code = t.invite_code
    return InviteCodeOut(
        invite_code=code,
        enabled=bool(code),
        register_url=f"/register?invite={code}" if code else "",
    )


@router.post("/me/invite-code", response_model=InviteCodeOut)
@audited("tenant.invite_code", "tenant")
async def rotate_invite_code(
    user: User = Depends(require_permission("tenant:invite")),
    db: AsyncSession = Depends(get_db),
) -> InviteCodeOut:
    """生成（或轮换）本组织的邀请码。

    每次调用都重新生成，不提供「查看旧码」——轮换即让旧码失效，
    这是撤销外流邀请链接的唯一手段。
    """
    t = await tenant_service.get_or_404(db, user.tenant_id)
    code = secrets.token_urlsafe(12)
    t.invite_code = code
    await db.flush()
    return InviteCodeOut(
        invite_code=code, enabled=True, register_url=f"/register?invite={code}"
    )


@router.delete("/me/invite-code", response_model=InviteCodeOut)
@audited("tenant.invite_code_clear", "tenant")
async def clear_invite_code(
    user: User = Depends(require_permission("tenant:invite")),
    db: AsyncSession = Depends(get_db),
) -> InviteCodeOut:
    """关闭邀请制：之后该组织只能由管理员代建账号。"""
    t = await tenant_service.get_or_404(db, user.tenant_id)
    t.invite_code = None
    await db.flush()
    return InviteCodeOut(invite_code=None, enabled=False, register_url="")


# ==================== 跨租户（平台管理员）====================
@router.get("", response_model=list[TenantOut])
async def list_tenants(
    _admin: User = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> list[TenantOut]:
    """列出全部组织及用量。

    逐个盘点是 N 次聚合查询：组织数量在实际部署里是个位数到几十，
    为此写一条跨表分组 SQL 反而更难维护；组织数上百再改成批量聚合。
    """
    rows = list((await db.execute(select(Tenant).order_by(Tenant.id))).scalars().all())
    out: list[TenantOut] = []
    for t in rows:
        out.append(_to_out(t, await tenant_service.quota_snapshot(db, t.id)))
    return out


@router.post("", response_model=TenantOut)
@audited("tenant.create", "tenant")
async def create_tenant(
    body: TenantCreate,
    admin: User = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> TenantOut:
    """新建组织，可同时创建该组织的第一位管理员。

    为什么要能一并建管理员：新建组织后如果没人能登录，它就是个死组织——
    平台管理员得先建号、再切到该组织代授角色，两步之间很容易漏。
    """
    slug = tenant_service.validate_slug(body.slug)
    await tenant_service.assert_slug_free(db, slug)

    username: str | None = None
    if body.admin_username:
        username = _validate_username(body.admin_username)
        validate_password_or_raise(body.admin_password or "")

    t = Tenant(
        name=body.name.strip(),
        slug=slug,
        plan=body.plan or "default",
        status=tenant_service.STATUS_ACTIVE,
        max_users=max(0, body.max_users),
        max_documents=max(0, body.max_documents),
        max_storage_bytes=max(0, body.max_storage_bytes),
        note=(body.note or None),
    )
    db.add(t)
    await db.flush()

    if username:
        # 同名跨组织是允许的：唯一约束是 (tenant_id, username)，登录按密码匹配账号。
        # 新组织此刻还没有任何用户，这里不必再查重。
        u = User(
            tenant_id=t.id,
            username=username,
            password_hash=hash_password(body.admin_password or ""),
            display_name=body.admin_display_name or username,
            email=body.admin_email,
            user_type="internal",
            is_admin=False,
            status="active",
            approval_status="approved",
        )
        db.add(u)
        await db.flush()
        # 授予本组织的 tenant_admin（而不是 is_admin：后者是历史直通位，
        # 会让「本组织管理员」在跨租户判断里也通过）
        await tenant_service.bind_tenant_admin(
            db, tenant_id=t.id, user_id=u.id, granted_by=admin.id
        )
        # 建好全员大群：否则该组织首个用户进聊天页没有默认房间
        try:
            from app.models import ChatRoomMember
            from app.services.chat_room_service import ensure_default_room, is_member

            room = await ensure_default_room(db, t.id)
            if not await is_member(db, room_id=room.id, user_id=u.id):
                db.add(
                    ChatRoomMember(tenant_id=t.id, room_id=room.id, user_id=u.id, role="owner")
                )
                await db.flush()
        except Exception:  # noqa: BLE001
            pass

    await db.flush()
    # 带上用量回显：前端建完直接显示「1 位成员」，不用再请求一次详情
    return _to_out(t, await tenant_service.quota_snapshot(db, t.id))


@router.get("/{tenant_id}", response_model=TenantOut)
async def get_tenant(
    tenant_id: int,
    _admin: User = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> TenantOut:
    t = await tenant_service.get_or_404(db, tenant_id)
    return _to_out(t, await tenant_service.quota_snapshot(db, t.id))


@router.patch("/{tenant_id}", response_model=TenantOut)
@audited("tenant.update", "tenant", id_arg="tenant_id")
async def update_tenant(
    tenant_id: int,
    body: TenantUpdate,
    admin: User = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> TenantOut:
    """改组织名称/状态/套餐/配额/备注。

    停用（suspended）会挡住该组织的**新登录与注册**，但不主动踢掉已有会话。
    与「停用用户」不同，这里刻意不做 token 批量失效：一次性踢掉整个组织的人，
    误操作的代价太高，而新登录被挡住已经足够止血。
    """
    t = await tenant_service.get_or_404(db, tenant_id)
    if body.name is not None:
        t.name = body.name.strip() or t.name
    if body.status is not None:
        new_status = tenant_service.normalize_status(body.status)
        if new_status != tenant_service.STATUS_ACTIVE and t.id == admin.tenant_id:
            # 停用自己所在组织会立刻把自己的账号挡在门外，几乎必然是误操作
            raise ConflictError("不能停用/归档你当前所在的组织")
        t.status = new_status
    if body.plan is not None:
        t.plan = body.plan or "default"
    if body.max_users is not None:
        t.max_users = max(0, body.max_users)
    if body.max_documents is not None:
        t.max_documents = max(0, body.max_documents)
    if body.max_storage_bytes is not None:
        t.max_storage_bytes = max(0, body.max_storage_bytes)
    if body.note is not None:
        t.note = body.note or None
    await db.flush()
    return _to_out(t, await tenant_service.quota_snapshot(db, t.id))


@router.delete("/{tenant_id}")
@audited("tenant.archive", "tenant", id_arg="tenant_id")
async def archive_tenant(
    tenant_id: int,
    admin: User = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """归档组织（软操作）。

    不做物理删除：租户下挂着几十张表的数据与磁盘文件，级联删除一旦出错无法
    回滚，而归档已经能挡住登录与注册。物理清理走运维流程。
    """
    t = await tenant_service.get_or_404(db, tenant_id)
    if t.id == admin.tenant_id:
        raise ConflictError("不能归档你当前所在的组织")
    t.status = tenant_service.STATUS_ARCHIVED
    await db.flush()
    return {"message": "组织已归档（数据保留，成员无法再登录）"}


def _validate_username(username: str) -> str:
    """与注册接口同一套用户名规则，避免代建账号绕过校验。"""
    u = (username or "").strip()
    if not (2 <= len(u) <= 64):
        raise ValidationError("用户名长度需在 2~64 之间")
    if not all(c.isalnum() or c in "_.@-" for c in u):
        raise ValidationError("用户名只能包含字母、数字与 _ . @ -")
    return u
