"""鉴权接口：登录 / 注册 / 刷新 / 当前用户。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import jwt
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.errors import AuthError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.middleware.auth_dep import get_current_user
from app.models import Department, Role, User, UserRole
from app.schemas.auth import (
    LoginRequest,
    PasswordChangeRequest,
    ProfileUpdateRequest,
    RefreshRequest,
    RegisterRequest,
    RoleBrief,
    TokenResponse,
    UserOut,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    from app.services.audit_service import record_audit, record_audit_async
    from app.services import login_guard

    # 防爆破第一层：按客户端 IP 限流
    client_ip = _client_ip(request)
    await login_guard.guard_ip(client_ip)
    # 防爆破第二层：账号锁定检查（不提示账号是否存在，统一话术）
    login_guard.ensure_not_locked(body.username)

    stmt = select(User).where(User.username == body.username)
    slug = (getattr(body, "tenant_slug", None) or "").strip()
    if slug:
        # 显式指定组织时就只在该组织内找，避免"另一个组织里同名同密码的账号"
        # 把人登进错误的组织（跨组织同名是允许的）
        from app.services import tenant_service

        t = await tenant_service.get_by_slug(db, slug)
        if t is None:
            login_guard.record_failure(body.username)
            raise AuthError("用户名或密码错误")
        stmt = stmt.where(User.tenant_id == t.id)

    result = await db.execute(stmt)
    users = result.scalars().all()
    # 同名（跨租户）时按密码匹配唯一用户，避免取到错误租户的账号
    user = next((u for u in users if verify_password(body.password, u.password_hash)), None)

    if not user:
        login_guard.record_failure(body.username)
        await record_audit_async(
            action="user.login", resource_type="user", resource_id=body.username,
            result="failure", error="用户名或密码错误", actor_name=body.username,
        )
        # 统一模糊提示，避免用户名枚举
        raise AuthError("用户名或密码错误")

    # 组织状态：停用/归档的组织不接受新登录。放在密码校验之后，
    # 否则「该组织已停用」会变成一个可枚举组织存在性的接口。
    from app.models import Tenant as _Tenant

    _t = await db.get(_Tenant, user.tenant_id)
    if _t is not None and (_t.status or "active") != "active":
        await record_audit_async(
            action="user.login", resource_type="user", resource_id=user.id,
            result="failure", error="组织已停用", actor_name=user.username,
        )
        raise AuthError("所在组织已停用，请联系平台管理员")

    if getattr(user, "approval_status", "approved") == "pending":
        await record_audit_async(
            action="user.login", resource_type="user", resource_id=user.id,
            result="failure", error="账号待管理员审核", actor_name=user.username,
        )
        raise AuthError("账号正在等待管理员审核，通过后即可登录")
    if getattr(user, "approval_status", "approved") == "rejected":
        await record_audit_async(
            action="user.login", resource_type="user", resource_id=user.id,
            result="failure", error="账号审核未通过", actor_name=user.username,
        )
        raise AuthError("账号审核未通过，请联系管理员")
    if user.status != "active":
        await record_audit_async(
            action="user.login", resource_type="user", resource_id=user.id,
            result="failure", error="账号已停用", actor_name=user.username,
        )
        raise AuthError("账号已停用")

    login_guard.record_success(body.username)
    user.last_login_at = datetime.now(timezone.utc)
    await db.flush()
    record_audit(
        db, action="user.login", resource_type="user", resource_id=user.id,
        tenant_id=user.tenant_id, actor_id=user.id, actor_name=user.username,
    )
    tv = int(getattr(user, "token_version", 0) or 0)
    return TokenResponse(
        access_token=create_access_token(user.id, user.tenant_id, token_version=tv),
        refresh_token=create_refresh_token(user.id, user.tenant_id, tv),
    )


def _client_ip(request: Request) -> str:
    """取真实客户端 IP（优先反代头，其次 socket）。"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return request.client.host if request.client else ""


@router.get("/register/status")
async def register_status() -> dict:
    """公开：自助注册是否开放。

    供登录页决定是否显示「注册」入口。
    刻意不依赖 /register/departments 是否为空来判断 —— 注册开放但尚未建部门时
    该端点也会返回空数组，据此判断会误认为注册已关闭。
    """
    return {
        "enabled": bool(settings.registration_enabled),
        "require_approval": bool(settings.require_admin_approval),
    }


@router.get("/register/departments")
async def public_departments(
    tenant_slug: str | None = None, invite_code: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """注册用：返回可选部门（扁平列表，含层级缩进）。

    公开端点（未登录可访问），仅暴露部门 id/名称/层级，不含任何敏感信息，
    供注册表单选择归属部门。

    组织按 slug / invite_code 决定（与提交注册同一套路由规则），
    否则多组织部署下会把 A 组织的部门列给 B 组织的申请人。
    """
    from app.models import Department

    if not settings.registration_enabled:
        return []
    from app.services import tenant_service

    try:
        tenant = await tenant_service.resolve_registration_tenant(
            db, slug=tenant_slug, invite_code=invite_code
        )
    except Exception:  # noqa: BLE001
        # 组织标识无效时不泄露"这个组织存不存在"，直接当没有可选部门
        return []
    rows = (
        await db.execute(
            select(Department).where(
                Department.tenant_id == tenant.id,
                Department.is_deleted.is_(False),
                Department.status == "active",
            ).order_by(Department.sort, Department.id)
        )
    ).scalars().all()
    return [
        {"id": d.id, "name": d.name, "parent_id": d.parent_id, "depth": d.depth}
        for d in rows
    ]


@router.get("/register/tenants")
async def public_tenants(db: AsyncSession = Depends(get_db)) -> dict:
    """注册页需要的组织路由信息：可选组织列表 + 是否需要显式指定。

    只返回 slug 与名称。为什么不带 id：id 是内部主键，公开端点没有理由暴露；
    注册本来就用 slug / 邀请码路由。
    """
    from app.services import tenant_service

    if not settings.registration_enabled:
        return {"multi_tenant": False, "require_selector": False, "tenants": [], "default_slug": None}
    rows = await tenant_service.tenant_options(db)
    return {
        "multi_tenant": len(rows) > 1,
        "require_selector": len(rows) > 1,
        "tenants": rows,
        "default_slug": rows[0]["slug"] if len(rows) == 1 else None,
    }


@router.post("/register")
async def register(body: RegisterRequest, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    """自助注册：创建待审核账号，需管理员通过后才能登录。

    安全：IP 限流 + 密码强度校验 + 用户名唯一性；返回统一话术避免账号枚举。
    """
    from app.core.errors import ConflictError
    from app.core.security import validate_password_or_raise
    from app.services import login_guard
    from app.services.audit_service import record_audit_async

    await login_guard.guard_ip(_client_ip(request))
    if not settings.registration_enabled:
        raise AuthError("系统未开放自助注册，请联系管理员开通账号")
    validate_password_or_raise(body.password)

    # 落哪个组织：邀请码 > slug > 唯一组织。原先固定取 id 最小的租户，
    # 多组织部署下会让所有注册都挤进 1 号组织，且调用方看不出来。
    from app.services import tenant_service

    try:
        tenant = await tenant_service.resolve_registration_tenant(
            db, slug=body.tenant_slug, invite_code=body.invite_code
        )
    except Exception as e:  # noqa: BLE001
        # 组织解析失败与"用户名已被占用"一样，都属于可以明确告诉用户的情况
        raise AuthError(str(e)) from e

    exists = (
        await db.execute(
            select(User).where(User.tenant_id == tenant.id, User.username == body.username)
        )
    ).scalar_one_or_none()
    if exists:
        raise ConflictError("用户名已被占用")

    # 人数配额（0 = 不限）。放在唯一性校验之后：已存在的用户名应当报"占用"，
    # 而不是让用户以为是配额问题。
    await tenant_service.ensure_user_quota(db, tenant.id)

    # 校验所选部门归属该租户且有效（防越权指定他租户部门）
    dept_id = None
    if body.department_id:
        from app.models import Department

        dept = await db.get(Department, body.department_id)
        if dept and dept.tenant_id == tenant.id and not dept.is_deleted and dept.status == "active":
            dept_id = dept.id

    pending = "pending" if settings.require_admin_approval else "approved"
    u = User(
        tenant_id=tenant.id,
        username=body.username,
        password_hash=hash_password(body.password),
        display_name=body.display_name or body.username,
        email=body.email,
        department_id=dept_id,
        user_type="internal",
        is_admin=False,
        status="active",
        approval_status=pending,
        registered_at=datetime.now(timezone.utc),
        settings=json.dumps({"register_reason": body.reason} if body.reason else {}, ensure_ascii=False),
    )
    db.add(u)
    await db.flush()
    # 全员默认入群（默认大群所有内部用户天然可见；此处显式入表记录元数据）
    try:
        from app.models import ChatRoomMember
        from app.services.chat_room_service import ensure_default_room, is_member

        _room = await ensure_default_room(db, tenant.id)
        if not await is_member(db, room_id=_room.id, user_id=u.id):
            db.add(ChatRoomMember(tenant_id=tenant.id, room_id=_room.id, user_id=u.id, role="member"))
            await db.flush()
    except Exception:  # noqa: BLE001
        pass
    await record_audit_async(
        action="user.register", resource_type="user", resource_id=u.id,
        actor_name=body.username, result="success",
        error=None if pending == "approved" else "待审核",
    )
    # 通知管理员有新的注册申请（待审核时）；失败不影响注册
    if pending == "pending":
        from app.services.user_notify import notify_admins_registration

        await notify_admins_registration(db, tenant_id=tenant.id, user=u, reason=body.reason)
        return {"message": "注册成功，请等待管理员审核通过后登录", "pending": True}
    return {"message": "注册成功，请登录", "pending": False}


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    try:
        payload = decode_token(body.refresh_token)
    except jwt.PyJWTError:
        raise AuthError("刷新凭证无效或已过期")
    if payload.get("type") != "refresh":
        raise AuthError("凭证类型错误")
    user = await db.get(User, int(payload["sub"]))
    if not user or user.status != "active":
        raise AuthError("用户不存在或已停用")
    if getattr(user, "approval_status", "approved") != "approved":
        raise AuthError("账号尚未通过审核")
    # 版本校验：改密码/停用后旧 refresh 也必须失效
    if int(payload.get("tv", 0)) != int(getattr(user, "token_version", 0) or 0):
        raise AuthError("凭证已失效，请重新登录")
    tv = int(getattr(user, "token_version", 0) or 0)
    return TokenResponse(
        access_token=create_access_token(user.id, user.tenant_id, token_version=tv),
        refresh_token=create_refresh_token(user.id, user.tenant_id, tv),
    )


@router.patch("/profile", response_model=UserOut)
async def update_profile(
    body: ProfileUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    """自助修改个人资料（姓名/邮箱/头像）。仅本人可改，不可改用户名与权限。"""
    from app.core.errors import ConflictError
    from app.services.audit_service import record_audit

    patch = body.model_dump(exclude_unset=True)
    if "email" in patch and patch["email"]:
        # 邮箱在本租户内唯一
        exists = (
            await db.execute(
                select(User).where(
                    User.tenant_id == user.tenant_id, User.email == patch["email"], User.id != user.id
                )
            )
        ).scalar_one_or_none()
        if exists:
            raise ConflictError("该邮箱已被其他账号使用")
    for k, v in patch.items():
        setattr(user, k, v)
    await db.flush()
    record_audit(
        db, action="user.profile_update", resource_type="user", resource_id=user.id,
        tenant_id=user.tenant_id, actor_id=user.id, actor_name=user.username,
    )
    # 返回完整的用户视图（含权限/角色），避免前端 setUser 后权限被清空
    from app.middleware.auth_dep import get_user_permission_codes

    out = UserOut.model_validate(user)
    out.permissions = sorted(await get_user_permission_codes(db, user))
    out.roles = await _user_roles(db, user)
    if user.department_id:
        dept = await db.get(Department, user.department_id)
        out.department_name = dept.name if dept else None
    return out


@router.get("/me", response_model=UserOut)
async def me(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> UserOut:
    from app.middleware.auth_dep import get_user_permission_codes

    perms = await get_user_permission_codes(db, user)
    out = UserOut.model_validate(user)
    out.permissions = sorted(perms)
    out.roles = await _user_roles(db, user)
    if user.department_id:
        dept = await db.get(Department, user.department_id)
        out.department_name = dept.name if dept else None
    return out


async def _user_roles(db: AsyncSession, user: User) -> list[RoleBrief]:
    """当前用户的有效角色（跨授予范围，去重）。"""
    rows = (
        await db.execute(
            select(Role.id, Role.code, Role.name)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(
                UserRole.user_id == user.id,
                UserRole.tenant_id == user.tenant_id,
            )
        )
    ).all()
    seen: dict[int, RoleBrief] = {}
    for rid, code, name in rows:
        seen.setdefault(rid, RoleBrief(id=rid, code=code, name=name))
    return list(seen.values())


@router.post("/password")
async def change_password(
    body: PasswordChangeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """修改当前用户密码：校验旧密码 + 强度后写入，并使旧 token 全部失效。"""
    from app.core.errors import ValidationError
    from app.core.security import validate_password_or_raise
    from app.services.audit_service import record_audit

    if not verify_password(body.old_password, user.password_hash):
        raise ValidationError("原密码不正确")
    if verify_password(body.new_password, user.password_hash):
        raise ValidationError("新密码不能与原密码相同")
    validate_password_or_raise(body.new_password)
    user.password_hash = hash_password(body.new_password)
    # 强制下线：递增 token 版本，之前签发的 access/refresh 立即失效
    user.token_version = int(getattr(user, "token_version", 0) or 0) + 1
    await db.flush()
    record_audit(
        db, action="user.password_change", resource_type="user", resource_id=user.id,
        tenant_id=user.tenant_id, actor_id=user.id, actor_name=user.username,
    )
    return {"message": "密码已修改，请重新登录"}
