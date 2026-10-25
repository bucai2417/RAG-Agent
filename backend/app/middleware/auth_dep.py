"""鉴权依赖：解析 JWT → 输出当前用户、principal 集合与权限校验。"""
from __future__ import annotations

import jwt
from fastapi import Depends, Header, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.errors import AuthError, PermissionDeniedError
from app.core.security import decode_token
from app.middleware.request_id import tenant_id_ctx, user_id_ctx
from app.models import Department, Permission, Role, RolePermission, User, UserGroupMember, UserRole
from app.models.base import utcnow
from app.services.permission import PrincipalSet


async def get_current_user(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError("未提供有效凭证")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except jwt.PyJWTError:
        raise AuthError("凭证无效或已过期")
    if payload.get("type") != "access":
        raise AuthError("凭证类型错误")
    user_id = int(payload.get("sub", 0))
    user = await db.get(User, user_id)
    if not user or user.status != "active":
        raise AuthError("用户不存在或已停用")
    # token 版本校验：改密码/停用/强制下线后旧 token 立即失效
    if int(payload.get("tv", 0)) != int(getattr(user, "token_version", 0) or 0):
        raise AuthError("凭证已失效，请重新登录")
    # 审核状态：待审核/被拒用户不得使用系统
    if getattr(user, "approval_status", "approved") != "approved":
        raise AuthError("账号尚未通过审核")
    user_id_ctx.set(user.id)
    tenant_id_ctx.set(user.tenant_id)
    return user


async def load_principal_set(db: AsyncSession, user: User) -> PrincipalSet:
    """构造用户的主体集合。

    - dept_ids：本部门 + 全部祖先部门（用 Department.path 前缀解析，实现向下继承）
    - role_ids：只取 scope in (platform, tenant) 的有效授予
      （kb/department 级角色不进全局 principal，避免跨库越权）
    - group_ids：用户所在组
    """
    dept_ids: list[int] = []
    if user.department_id:
        dept = await db.get(Department, user.department_id)
        if dept and dept.path:
            dept_ids = [int(x) for x in dept.path.strip(".").split(".") if x]
        else:
            dept_ids = [user.department_id]

    now = utcnow()
    role_ids = [
        r
        for (r,) in (
            await db.execute(
                select(UserRole.role_id).where(
                    UserRole.user_id == user.id,
                    UserRole.tenant_id == user.tenant_id,
                    UserRole.scope_type.in_(("platform", "tenant")),
                    or_(UserRole.expires_at.is_(None), UserRole.expires_at > now),
                )
            )
        ).all()
    ]
    group_ids = [
        g
        for (g,) in (
            await db.execute(
                select(UserGroupMember.group_id).where(UserGroupMember.user_id == user.id)
            )
        ).all()
    ]
    return PrincipalSet(
        user_id=user.id,
        tenant_id=user.tenant_id,
        is_admin=bool(user.is_admin),
        is_external=(getattr(user, "user_type", "internal") == "external"),
        dept_ids=dept_ids,
        role_ids=role_ids,
        group_ids=group_ids,
    )


async def get_principal_set(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> PrincipalSet:
    return await load_principal_set(db, user)


async def resolve_user_dept_ids(db: AsyncSession, user: User) -> list[int]:
    """本部门 + 全部祖先部门（用 Department.path 前缀解析，实现向下继承）。"""
    if not user.department_id:
        return []
    dept = await db.get(Department, user.department_id)
    if dept and dept.path:
        return [int(x) for x in dept.path.strip(".").split(".") if x]
    return [user.department_id]


async def get_user_permission_codes(db: AsyncSession, user: User) -> set[str]:
    """用户拥有的全部权限 code（跨所有角色授予）。

    作用域约束：tenant/platform 级角色全局生效；department 级角色仅当授予的
    scope_id 命中用户所属部门（含祖先）时才生效，防止跨部门越权授予。
    """
    if user.is_admin:
        return {"*"}
    dept_ids = await resolve_user_dept_ids(db, user)
    rows = await db.execute(
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, Role.id == RolePermission.role_id)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(
            UserRole.user_id == user.id,
            UserRole.tenant_id == user.tenant_id,
            or_(UserRole.expires_at.is_(None), UserRole.expires_at > utcnow()),
            or_(Role.tenant_id.is_(None), Role.tenant_id == user.tenant_id),
            or_(
                UserRole.scope_type.in_(("platform", "tenant")),
                and_(
                    UserRole.scope_type == "department",
                    UserRole.scope_id.in_(dept_ids or [-1]),
                ),
            ),
        )
    )
    return {c for (c,) in rows.all()}


async def user_has_permission(db: AsyncSession, user: User, code: str) -> bool:
    """统一权限判定：is_admin 直通；否则查聚合权限码（含 '*'）。"""
    if user.is_admin:
        return True
    perms = await get_user_permission_codes(db, user)
    return "*" in perms or code in perms


async def is_platform_admin(db: AsyncSession, user: User) -> bool:
    """是否平台管理员（可跨租户操作）。

    判定依据只能是 **platform 作用域**的角色授予：
    - ``user.is_admin`` 与 ``super_admin`` 的 tenant 级授予都意味着「本租户的管理员」，
      用它去跨租户操作等于让任一组织的管理员管理所有组织；
    - 所以跨租户端点（租户列表/配额/停用）一律走这个判定，而不是 is_admin。

    这样默认部署不受影响：单租户时唯一的组织就是自己的组织，而
    「平台运维」需要显式把一个角色授予到 platform 作用域（见 rbac 的授予规则）。
    """
    rows = await db.execute(
        select(UserRole.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(
            UserRole.user_id == user.id,
            UserRole.scope_type == "platform",
            or_(UserRole.expires_at.is_(None), UserRole.expires_at > utcnow()),
            or_(Role.tenant_id.is_(None), Role.tenant_id == user.tenant_id),
        )
        .limit(1)
    )
    return rows.scalar_one_or_none() is not None


async def require_platform_admin(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> User:
    """平台管理员门：跨租户端点专用（租户创建/停用/配额）。"""
    if await is_platform_admin(db, user):
        return user
    raise PermissionDeniedError("需要平台管理员权限（platform 作用域的超级管理员）")


def require_permission(code: str):
    """权限门依赖工厂：缺权限则 403。is_admin 直通（兼容遗留）。

    注意 is_admin 只是**本租户**的管理员：它不该让跨租户端点通过，
    所以那些端点用 require_platform_admin，而不是这里。
    """

    async def _dep(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> User:
        if user.is_admin:
            return user
        perms = await get_user_permission_codes(db, user)
        if "*" not in perms and code not in perms:
            raise PermissionDeniedError(f"缺少权限: {code}")
        return user

    return _dep


async def require_admin(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> User:
    """管理员门：is_admin 直通，或拥有 tenant:manage / 通配权限的角色。"""
    if user.is_admin:
        return user
    perms = await get_user_permission_codes(db, user)
    if "tenant:manage" in perms or "*" in perms:
        return user
    raise PermissionDeniedError("需要管理员权限")


# ============ 文件访问鉴权（登录态 或 签名 token）============
class FileAccess:
    """文件访问结果：来源=登录用户 或 签名 token。"""

    def __init__(
        self,
        *,
        via: str,
        tenant_id: int,
        user: User | None = None,
        scope: str | None = None,
        file_key: str | None = None,
        artifact_id: int | None = None,
    ) -> None:
        self.via = via  # "user" | "token"
        self.tenant_id = tenant_id
        self.user = user
        self.scope = scope
        self.file_key = file_key
        self.artifact_id = artifact_id


async def get_file_access(
    t: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> FileAccess:
    """文件访问依赖：优先登录态（等价 chat:use 校验），否则校验 ?t= 签名 token。

    专供 <img>/<video>/<a> 等无法携带 Authorization 头的原生标签。
    """
    # 1) 登录态
    if authorization and authorization.lower().startswith("bearer "):
        user = await get_current_user(authorization, db)
        if not await user_has_permission(db, user, "chat:use"):
            raise PermissionDeniedError("缺少权限: chat:use")
        return FileAccess(via="user", tenant_id=user.tenant_id, user=user)
    # 2) 签名 token
    if t:
        try:
            payload = decode_token(t)
        except jwt.PyJWTError:
            raise PermissionDeniedError("附件链接无效或已过期")
        if payload.get("type") != "file":
            raise PermissionDeniedError("凭证类型错误")
        return FileAccess(
            via="token",
            tenant_id=int(payload.get("tenant_id", 0)),
            scope=payload.get("scope"),
            file_key=payload.get("file_key"),
            artifact_id=payload.get("artifact_id"),
        )
    # 3) 都没有
    raise AuthError("未提供有效凭证")


# ============ API Key 鉴权（供内部系统调用）============
from fastapi import Header as _Header


class ApiKeyAuth:
    """API Key 鉴权结果：携带虚拟 principal 集合。"""

    def __init__(self, *, key, ps: PrincipalSet, scopes: set[str], kb_ids: list[int] | None):
        self.key = key
        self.ps = ps
        self.scopes = scopes
        self.kb_ids = kb_ids

    def has_scope(self, code: str) -> bool:
        return "*" in self.scopes or code in self.scopes


async def get_apikey_auth(
    x_api_key: str | None = _Header(default=None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
) -> ApiKeyAuth:
    """校验 X-API-Key，返回虚拟主体（绑定用户则继承其权限）。"""
    from app.core.errors import AuthError
    from app.core.rate_limit import rate_limiter
    from app.models import ApiKey
    from app.services.api_key_service import hash_key

    if not x_api_key:
        raise AuthError("缺少 X-API-Key")
    key = (
        await db.execute(
            select(ApiKey).where(ApiKey.key_hash == hash_key(x_api_key), ApiKey.status == "active")
        )
    ).scalar_one_or_none()
    if not key or not key.enabled:
        raise AuthError("无效的 API Key")
    if key.expires_at and key.expires_at < utcnow():
        raise AuthError("API Key 已过期")

    if not await rate_limiter.allow(f"apikey:{key.id}", key.rate_limit):
        from app.core.errors import AppError

        err = AppError("请求过于频繁", code="rate_limited")
        err.status_code = 429
        raise err

    key.last_used_at = utcnow()
    # 虚拟主体
    if key.user_id:
        u = await db.get(User, key.user_id)
        ps = await load_principal_set(db, u) if u else PrincipalSet(user_id=0, tenant_id=key.tenant_id)
    else:
        ps = PrincipalSet(user_id=0, tenant_id=key.tenant_id)
    # 记录上下文（审计）
    from app.middleware.request_id import actor_name_ctx, actor_type_ctx, tenant_id_ctx, user_id_ctx

    tenant_id_ctx.set(key.tenant_id)
    if key.user_id:
        user_id_ctx.set(key.user_id)
    actor_type_ctx.set("apikey")
    actor_name_ctx.set(f"apikey:{key.key_prefix}")

    scopes = set(key.scopes or [])
    return ApiKeyAuth(key=key, ps=ps, scopes=scopes, kb_ids=key.kb_ids)
