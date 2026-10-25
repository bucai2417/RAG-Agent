"""RBAC 接口：角色/权限矩阵、用户、用户-角色授予、部门树、用户组。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.core.security import hash_password
from app.middleware.auth_dep import get_current_user, require_permission
from app.services.audit_service import audited
from app.models import (
    Department,
    Permission,
    Role,
    RolePermission,
    User,
    UserGroup,
    UserGroupMember,
    UserRole,
)
from app.schemas.rbac import (
    DeptCreate,
    DeptOut,
    DeptTreeNode,
    DeptUpdate,
    GroupCreate,
    GroupUpdate,
    GroupMembersIn,
    GroupOut,
    PermissionOut,
    RoleCreate,
    RoleOut,
    RolePermissionsIn,
    RoleUpdate,
    UserCreate,
    UserListItem,
    UserRoleGrant,
    UserRoleOut,
    UserUpdate,
)

router = APIRouter(prefix="/admin", tags=["rbac"])


# ==================== 角色 ====================
@router.get("/roles", response_model=list[RoleOut])
async def list_roles(
    user: User = Depends(require_permission("role:read")),
    db: AsyncSession = Depends(get_db),
) -> list[RoleOut]:
    rows = (
        await db.execute(
            select(Role).where(
                (Role.tenant_id == user.tenant_id) | (Role.tenant_id.is_(None))
            ).order_by(Role.is_system.desc(), Role.scope, Role.id)
        )
    ).scalars().all()
    # 权限数 / 已授予用户数（供列表页展示，避免前端逐个请求）
    pc_rows = (await db.execute(
        select(RolePermission.role_id, func.count()).group_by(RolePermission.role_id)
    )).all()
    uc_rows = (await db.execute(
        select(UserRole.role_id, func.count()).where(UserRole.tenant_id == user.tenant_id)
        .group_by(UserRole.role_id)
    )).all()
    pc_map = {r[0]: r[1] for r in pc_rows}
    uc_map = {r[0]: r[1] for r in uc_rows}
    out: list[RoleOut] = []
    for r in rows:
        item = RoleOut.model_validate(r)
        item.permission_count = int(pc_map.get(r.id, 0))
        item.user_count = int(uc_map.get(r.id, 0))
        out.append(item)
    return out


@router.post("/roles", response_model=RoleOut)
@audited("role.create", "role")
async def create_role(
    body: RoleCreate,
    user: User = Depends(require_permission("role:manage")),
    db: AsyncSession = Depends(get_db),
) -> Role:
    role = Role(
        tenant_id=user.tenant_id,
        code=body.code,
        name=body.name,
        scope=body.scope,
        is_system=False,
        description=body.description,
    )
    db.add(role)
    await db.flush()
    return role


@router.patch("/roles/{role_id}", response_model=RoleOut)
@audited("role.update", "role", id_arg="role_id")
async def update_role(
    role_id: int,
    body: RoleUpdate,
    user: User = Depends(require_permission("role:manage")),
    db: AsyncSession = Depends(get_db),
) -> Role:
    role = await db.get(Role, role_id)
    if not role or (role.tenant_id not in (None, user.tenant_id)):
        raise NotFoundError("角色不存在")
    if role.is_system:
        raise PermissionDeniedError("内置角色不可修改")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(role, k, v)
    await db.flush()
    return role


@router.delete("/roles/{role_id}")
@audited("role.delete", "role", id_arg="role_id")
async def delete_role(
    role_id: int,
    user: User = Depends(require_permission("role:manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    role = await db.get(Role, role_id)
    if not role or role.tenant_id != user.tenant_id:
        raise NotFoundError("角色不存在")
    if role.is_system:
        raise PermissionDeniedError("内置角色不可删除")
    in_use = (
        await db.execute(select(UserRole).where(UserRole.role_id == role_id))
    ).scalars().first()
    if in_use:
        raise ConflictError("角色已被授予用户，无法删除")
    await db.delete(role)
    await db.flush()
    return {"message": "已删除"}


@router.get("/permissions", response_model=list[PermissionOut])
async def list_permissions(
    user: User = Depends(require_permission("role:read")),
    db: AsyncSession = Depends(get_db),
) -> list[Permission]:
    rows = (await db.execute(select(Permission).order_by(Permission.resource, Permission.action))).scalars().all()
    return list(rows)


@router.get("/roles/{role_id}/permissions", response_model=list[int])
async def get_role_permissions(
    role_id: int,
    user: User = Depends(require_permission("role:read")),
    db: AsyncSession = Depends(get_db),
) -> list[int]:
    rows = (
        await db.execute(select(RolePermission.permission_id).where(RolePermission.role_id == role_id))
    ).all()
    return [r[0] for r in rows]


@router.put("/roles/{role_id}/permissions")
@audited("role.permissions", "role", id_arg="role_id")
async def set_role_permissions(
    role_id: int,
    body: RolePermissionsIn,
    user: User = Depends(require_permission("role:manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    role = await db.get(Role, role_id)
    if not role or (role.tenant_id not in (None, user.tenant_id)):
        raise NotFoundError("角色不存在")
    if role.is_system:
        raise PermissionDeniedError("内置角色权限不可修改")
    from sqlalchemy import delete

    await db.execute(delete(RolePermission).where(RolePermission.role_id == role_id))
    for pid in set(body.permission_ids):
        db.add(RolePermission(role_id=role_id, permission_id=pid))
    await db.flush()
    return {"message": "已保存"}


# ==================== 用户 ====================
@router.get("/users")
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    search: str | None = None,
    status: str | None = Query(None, description="按审核状态过滤：pending/approved/rejected"),
    user: User = Depends(require_permission("user:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from sqlalchemy import or_

    stmt = select(User).where(
        User.tenant_id == user.tenant_id,
        or_(User.user_type.is_(None), User.user_type != "external"),
    )
    if search:
        like = f"%{search}%"
        stmt = stmt.where((User.username.like(like)) | (User.display_name.like(like)))
    if status:
        stmt = stmt.where(User.approval_status == status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(User.id.desc()).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    return {
        "items": [UserListItem.model_validate(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/users/pending")
async def list_pending_users(
    user: User = Depends(require_permission("user:read")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """待审核的注册用户列表（供管理员审批）。"""
    rows = (
        await db.execute(
            select(User).where(
                User.tenant_id == user.tenant_id,
                User.approval_status == "pending",
                or_(User.user_type.is_(None), User.user_type != "external"),
            ).order_by(User.id.desc())
        )
    ).scalars().all()
    # 解析部门名（供管理员审核时确认）
    from app.models import Department

    dept_ids = [u.department_id for u in rows if u.department_id]
    dept_map: dict[int, str] = {}
    if dept_ids:
        dept_map = {
            d.id: d.name
            for d in (await db.execute(select(Department).where(Department.id.in_(dept_ids)))).scalars().all()
        }
    out = []
    for u in rows:
        reason = None
        try:
            reason = (json.loads(u.settings or "{}") or {}).get("register_reason")
        except Exception:  # noqa: BLE001
            pass
        out.append({
            "id": u.id, "username": u.username, "display_name": u.display_name,
            "email": u.email, "reason": reason,
            "department_id": u.department_id,
            "department_name": dept_map.get(u.department_id) if u.department_id else None,
            "registered_at": u.registered_at.isoformat() if u.registered_at else None,
        })
    return out


async def _grant_default_viewer_role(db: AsyncSession, *, tenant_id: int, user_id: int) -> bool:
    """给用户授予内置「普通用户」角色（幂等）。

    避免新用户权限集为空导致全站 403——管理员建号、审核通过注册申请都需调用。
    返回是否实际新增。
    """
    viewer = (
        await db.execute(select(Role).where(Role.tenant_id.is_(None), Role.code == "viewer"))
    ).scalar_one_or_none()
    if not viewer:
        return False
    exists = (
        await db.execute(
            select(UserRole).where(UserRole.user_id == user_id, UserRole.role_id == viewer.id)
        )
    ).scalar_one_or_none()
    if exists:
        return False
    db.add(
        UserRole(
            tenant_id=tenant_id, user_id=user_id, role_id=viewer.id,
            scope_type="tenant", scope_id=0,
        )
    )
    await db.flush()
    return True


async def _join_default_chat_room(db: AsyncSession, *, tenant_id: int, user_id: int) -> None:
    """确保租户「全员大群」存在并把该用户加入（幂等）。

    默认大群所有内部用户天然可见，此处显式入表是为了记录角色/已读水位等元数据。
    """
    try:
        from app.models import ChatRoomMember
        from app.services.chat_room_service import ensure_default_room, is_member

        room = await ensure_default_room(db, tenant_id)
        if await is_member(db, room_id=room.id, user_id=user_id):
            return
        db.add(ChatRoomMember(tenant_id=tenant_id, room_id=room.id, user_id=user_id, role="member"))
        await db.flush()
    except Exception:  # noqa: BLE001
        # 聊天室不可用不应阻断建号
        pass


@router.post("/users/{user_id}/approve")
@audited("user.approve", "user", id_arg="user_id")
async def approve_user(
    user_id: int,
    user: User = Depends(require_permission("user:manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """审核通过：允许该用户登录，并授予基础权限（普通用户角色）。"""
    from app.models.base import utcnow

    u = await db.get(User, user_id)
    if not u or u.tenant_id != user.tenant_id:
        raise NotFoundError("用户不存在")
    u.approval_status = "approved"
    u.reviewed_by = user.id
    u.reviewed_at = utcnow()
    await db.flush()
    # 授予基础角色，否则新用户权限集为空会全站 403
    if not u.is_admin:
        await _grant_default_viewer_role(db, tenant_id=u.tenant_id, user_id=u.id)
    # 全员默认入群
    await _join_default_chat_room(db, tenant_id=u.tenant_id, user_id=u.id)
    # 通知申请人审核结果（站内，随本事务提交）
    from app.services.user_notify import notify_user_review_result

    await notify_user_review_result(db, tenant_id=u.tenant_id, user=u, approved=True)
    return {"message": f"已通过「{u.display_name or u.username}」的注册申请",
            "approval_status": u.approval_status}


@router.post("/users/{user_id}/reject")
@audited("user.reject", "user", id_arg="user_id")
async def reject_user(
    user_id: int,
    user: User = Depends(require_permission("user:manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """审核拒绝：该用户无法登录（保留记录，可后续再改）。"""
    from app.models.base import utcnow

    u = await db.get(User, user_id)
    if not u or u.tenant_id != user.tenant_id:
        raise NotFoundError("用户不存在")
    u.approval_status = "rejected"
    u.reviewed_by = user.id
    u.reviewed_at = utcnow()
    await db.flush()
    from app.services.user_notify import notify_user_review_result

    await notify_user_review_result(db, tenant_id=u.tenant_id, user=u, approved=False)
    return {"message": f"已拒绝「{u.display_name or u.username}」的注册申请",
            "approval_status": u.approval_status}



@router.post("/users", response_model=UserListItem)
@audited("user.create", "user")
async def create_user(
    body: UserCreate,
    user: User = Depends(require_permission("user:manage")),
    db: AsyncSession = Depends(get_db),
) -> User:
    exists = (
        await db.execute(
            select(User).where(User.tenant_id == user.tenant_id, User.username == body.username)
        )
    ).scalar_one_or_none()
    if exists:
        raise ConflictError("用户名已存在")
    # 管理员建号：直接生效（approved），无需再审核；密码需满足强度要求
    from app.core.security import validate_password_or_raise

    validate_password_or_raise(body.password)
    u = User(
        tenant_id=user.tenant_id,
        username=body.username,
        password_hash=hash_password(body.password),
        display_name=body.display_name or body.username,
        email=body.email,
        department_id=body.department_id,
        is_admin=body.is_admin,
        approval_status="approved",
    )
    db.add(u)
    await db.flush()
    # 自动授予内置「普通用户」角色，避免新用户权限集为空而全站 403
    if not u.is_admin:
        await _grant_default_viewer_role(db, tenant_id=user.tenant_id, user_id=u.id)
    # 全员默认入群
    await _join_default_chat_room(db, tenant_id=user.tenant_id, user_id=u.id)
    return u


@router.patch("/users/{user_id}", response_model=UserListItem)
@audited("user.update", "user", id_arg="user_id")
async def update_user(
    user_id: int,
    body: UserUpdate,
    user: User = Depends(require_permission("user:manage")),
    db: AsyncSession = Depends(get_db),
) -> User:
    u = await db.get(User, user_id)
    if not u or u.tenant_id != user.tenant_id:
        raise NotFoundError("用户不存在")
    patch = body.model_dump(exclude_unset=True)
    # 停用账号 / 重置密码时递增强制下线：旧 token 立即失效
    force_logout = ("status" in patch and patch["status"] != "active") or ("password" in patch)
    if "password" in patch and patch["password"]:
        from app.core.security import hash_password as _hp, validate_password_or_raise

        validate_password_or_raise(patch["password"])
        patch["password_hash"] = _hp(patch.pop("password"))
    else:
        patch.pop("password", None)
    for k, v in patch.items():
        setattr(u, k, v)
    if force_logout:
        u.token_version = int(getattr(u, "token_version", 0) or 0) + 1
    await db.flush()
    return u


@router.delete("/users/{user_id}")
@audited("user.delete", "user", id_arg="user_id")
async def delete_user(
    user_id: int,
    user: User = Depends(require_permission("user:manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """物理删除用户，并清理其角色/通知等关联。

    安全约束：
    - 不能删自己
    - 不能删本租户最后一个管理员（避免锁死系统）
    - 若该用户创建了知识库等资产，拒绝删除并提示（避免产生孤儿数据）
    """
    from sqlalchemy import delete as _del, select as _sel

    from app.models import KnowledgeBase, Notification

    u = await db.get(User, user_id)
    if not u or u.tenant_id != user.tenant_id:
        raise NotFoundError("用户不存在")
    if u.id == user.id:
        raise PermissionDeniedError("不能删除当前登录的账号")
    # 最后一个管理员保护
    if u.is_admin:
        admins = (
            await db.execute(
                _sel(func.count()).select_from(User).where(
                    User.tenant_id == user.tenant_id, User.is_admin.is_(True),
                    or_(User.status.is_(None), User.status != "disabled"),
                )
            )
        ).scalar_one()
        if admins <= 1:
            raise ConflictError("不能删除最后一个管理员，请先指定其他管理员")
    # 资产检查：拥有知识库则拒绝（保留数据归属）
    owned = (
        await db.execute(
            _sel(func.count()).select_from(KnowledgeBase).where(
                KnowledgeBase.owner_id == u.id, KnowledgeBase.tenant_id == user.tenant_id
            )
        )
    ).scalar_one()
    if owned:
        raise ConflictError(f"该用户是 {owned} 个知识库的拥有者，请先转移或删除这些知识库")

    # 清理关联。角色授予必须清干净：user_role 无外键级联，而 SQLite 会复用
    # 已删行的 rowid，残留授予会被下一个新建用户「继承」。
    from app.services.tenant_service import purge_user_grants

    await purge_user_grants(db, u.id)
    await db.execute(_del(Notification).where(Notification.user_id == u.id))
    await db.delete(u)
    await db.flush()
    return {"message": f"已删除用户「{u.display_name or u.username}」"}


@router.get("/users/{user_id}/roles", response_model=list[UserRoleOut])
async def list_user_roles(
    user_id: int,
    user: User = Depends(require_permission("user:read")),
    db: AsyncSession = Depends(get_db),
) -> list[UserRoleOut]:
    rows = (
        await db.execute(select(UserRole).where(UserRole.user_id == user_id))
    ).scalars().all()
    role_ids = [r.role_id for r in rows]
    roles = {}
    if role_ids:
        for role in (await db.execute(select(Role).where(Role.id.in_(role_ids)))).scalars().all():
            roles[role.id] = role
    out = []
    for r in rows:
        item = UserRoleOut.model_validate(r)
        if r.role_id in roles:
            item.role_code = roles[r.role_id].code
            item.role_name = roles[r.role_id].name
        out.append(item)
    return out


@router.post("/users/{user_id}/roles", response_model=UserRoleOut)
@audited("user_role.grant", "user_role")
async def grant_user_role(
    user_id: int,
    body: UserRoleGrant,
    user: User = Depends(require_permission("user:manage")),
    db: AsyncSession = Depends(get_db),
) -> UserRoleOut:
    target = await db.get(User, user_id)
    if not target or target.tenant_id != user.tenant_id:
        raise NotFoundError("用户不存在")
    role = await db.get(Role, body.role_id)
    if not role:
        raise NotFoundError("角色不存在")
    # 角色 scope 与授予 scope 的合法性校验
    if role.scope == "platform" and body.scope_type != "platform":
        raise PermissionDeniedError("平台级角色只能授予 platform 范围")
    if role.scope == "kb" and body.scope_type != "kb":
        raise PermissionDeniedError("知识库级角色必须授予到具体知识库")
    if role.scope == "department":
        if body.scope_type != "department" or body.scope_id is None:
            raise PermissionDeniedError("部门级角色必须授予到具体部门")
        # 关键校验：授予部门必须命中用户所属部门（含祖先），否则该角色不会生效
        # （权限判定里 department 级角色要求 scope_id ∈ 用户的部门祖先链）
        from app.middleware.auth_dep import resolve_user_dept_ids

        target_depts = set(await resolve_user_dept_ids(db, target))
        if not target_depts:
            raise ConflictError("该用户未分配部门，无法授予部门级角色；请先为用户设置部门")
        if body.scope_id not in target_depts:
            raise ConflictError(
                "该部门级角色不会生效：授予的部门必须是该用户所属部门之一。"
                "请先将用户分配到该部门，或改授租户级角色（如「普通用户」）"
            )
    if role.scope == "kb" and body.scope_id is not None:
        # 知识库级：校验知识库存在且属于本租户
        from app.models import KnowledgeBase

        kb = await db.get(KnowledgeBase, body.scope_id)
        if not kb or kb.tenant_id != user.tenant_id:
            raise NotFoundError("知识库不存在")

    ur = UserRole(
        tenant_id=user.tenant_id,
        user_id=user_id,
        role_id=body.role_id,
        scope_type=body.scope_type,
        scope_id=body.scope_id,
        granted_by=user.id,
        expires_at=body.expires_at,
    )
    db.add(ur)
    await db.flush()
    item = UserRoleOut.model_validate(ur)
    item.role_code = role.code
    item.role_name = role.name
    return item


@router.delete("/users/{user_id}/roles/{ur_id}")
@audited("user_role.revoke", "user_role", id_arg="ur_id")
async def revoke_user_role(
    user_id: int,
    ur_id: int,
    user: User = Depends(require_permission("user:manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ur = await db.get(UserRole, ur_id)
    if not ur or ur.user_id != user_id:
        raise NotFoundError("授予不存在")
    await db.delete(ur)
    await db.flush()
    return {"message": "已撤销"}


# ==================== 部门 ====================
@router.get("/departments/tree", response_model=list[DeptTreeNode])
async def dept_tree(
    user: User = Depends(require_permission("dept:read")),
    db: AsyncSession = Depends(get_db),
) -> list[DeptTreeNode]:
    rows = (
        await db.execute(
            select(Department).where(
                Department.tenant_id == user.tenant_id, Department.is_deleted.is_(False)
            )
        )
    ).scalars().all()
    nodes = {d.id: DeptTreeNode.model_validate(d) for d in rows}
    # 直属成员数（供列表展示）
    cnt_rows = (await db.execute(
        select(User.department_id, func.count()).where(
            User.tenant_id == user.tenant_id,
            User.department_id.is_not(None),
            or_(User.user_type.is_(None), User.user_type != "external"),
        ).group_by(User.department_id)
    )).all()
    cnt_map = {r[0]: r[1] for r in cnt_rows}
    for did, node in nodes.items():
        node.member_count = int(cnt_map.get(did, 0))
    roots: list[DeptTreeNode] = []
    for d in rows:
        node = nodes[d.id]
        if d.parent_id and d.parent_id in nodes:
            nodes[d.parent_id].children.append(node)
        else:
            roots.append(node)
    return roots


@router.post("/departments", response_model=DeptOut)
@audited("dept.create", "department")
async def create_dept(
    body: DeptCreate,
    user: User = Depends(require_permission("dept:manage")),
    db: AsyncSession = Depends(get_db),
) -> Department:
    parent = await db.get(Department, body.parent_id) if body.parent_id else None
    d = Department(
        tenant_id=user.tenant_id,
        parent_id=body.parent_id,
        name=body.name,
        code=body.code,
        sort=body.sort,
        depth=(parent.depth + 1) if parent else 0,
    )
    db.add(d)
    await db.flush()
    d.path = (parent.path if parent else "") + f"{d.id}."
    await db.flush()
    return d


@router.patch("/departments/{dept_id}", response_model=DeptOut)
@audited("dept.update", "department", id_arg="dept_id")
async def update_dept(
    dept_id: int,
    body: DeptUpdate,
    user: User = Depends(require_permission("dept:manage")),
    db: AsyncSession = Depends(get_db),
) -> Department:
    from sqlalchemy import func as sqlfunc
    from sqlalchemy import update

    d = await db.get(Department, dept_id)
    if not d or d.tenant_id != user.tenant_id:
        raise NotFoundError("部门不存在")

    if body.name is not None:
        d.name = body.name
    if body.code is not None:
        d.code = body.code
    if body.sort is not None:
        d.sort = body.sort

    # 移动部门
    if body.parent_id is not None and body.parent_id != d.parent_id:
        new_parent = await db.get(Department, body.parent_id) if body.parent_id else None
        if new_parent and (new_parent.path or "").startswith(d.path):
            raise PermissionDeniedError("不能把部门移动到自己的子部门下")
        old_path = d.path
        new_path = (new_parent.path if new_parent else "") + f"{d.id}."
        delta = (new_parent.depth + 1 - d.depth) if new_parent else -d.depth
        # 更新自身与所有子孙
        await db.execute(
            update(Department)
            .where(Department.path.like(f"{old_path}%"))
            .values(
                path=sqlfunc.replace(Department.path, old_path, new_path),
                depth=Department.depth + delta,
            )
        )
        d.parent_id = body.parent_id
    await db.flush()
    await db.refresh(d)
    return d


@router.delete("/departments/{dept_id}")
@audited("dept.delete", "department", id_arg="dept_id")
async def delete_dept(
    dept_id: int,
    user: User = Depends(require_permission("dept:manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    d = await db.get(Department, dept_id)
    if not d or d.tenant_id != user.tenant_id:
        raise NotFoundError("部门不存在")
    children = (
        await db.execute(
            select(func.count()).select_from(Department).where(
                Department.parent_id == dept_id, Department.is_deleted.is_(False)
            )
        )
    ).scalar_one()
    if children:
        raise ConflictError("请先删除子部门")
    d.is_deleted = True
    # 该部门下的用户置为「未分配部门」，避免用户挂到已删除部门导致部门级授权/统计异常
    from sqlalchemy import update as _upd

    moved = await db.execute(
        _upd(User).where(
            User.tenant_id == user.tenant_id, User.department_id == dept_id
        ).values(department_id=None)
    )
    await db.flush()
    return {"message": "已删除", "unassigned_users": moved.rowcount or 0}


# ==================== 用户组 ====================
@router.get("/groups", response_model=list[GroupOut])
async def list_groups(
    user: User = Depends(require_permission("group:read")),
    db: AsyncSession = Depends(get_db),
) -> list[UserGroup]:
    rows = (
        await db.execute(select(UserGroup).where(UserGroup.tenant_id == user.tenant_id))
    ).scalars().all()
    return list(rows)


@router.post("/groups", response_model=GroupOut)
@audited("group.create", "user_group")
async def create_group(
    body: GroupCreate,
    user: User = Depends(require_permission("group:manage")),
    db: AsyncSession = Depends(get_db),
) -> UserGroup:
    g = UserGroup(tenant_id=user.tenant_id, name=body.name, description=body.description)
    db.add(g)
    await db.flush()
    return g


@router.patch("/groups/{group_id}")
@audited("group.update", "user_group", id_arg="group_id")
async def update_group(
    group_id: int,
    body: GroupUpdate,
    user: User = Depends(require_permission("group:manage")),
    db: AsyncSession = Depends(get_db),
) -> GroupOut:
    g = await db.get(UserGroup, group_id)
    if not g or g.tenant_id != user.tenant_id:
        raise NotFoundError("用户组不存在")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(g, k, v)
    await db.flush()
    return GroupOut.model_validate(g)


@router.delete("/groups/{group_id}")
@audited("group.delete", "user_group", id_arg="group_id")
async def delete_group(
    group_id: int,
    user: User = Depends(require_permission("group:manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from sqlalchemy import delete as sql_delete

    g = await db.get(UserGroup, group_id)
    if not g or g.tenant_id != user.tenant_id:
        raise NotFoundError("用户组不存在")
    await db.execute(sql_delete(UserGroupMember).where(UserGroupMember.group_id == group_id))
    await db.delete(g)
    await db.flush()
    return {"message": "已删除"}


@router.get("/groups/{group_id}/members", response_model=list[int])
async def list_group_members(
    group_id: int,
    user: User = Depends(require_permission("group:read")),
    db: AsyncSession = Depends(get_db),
) -> list[int]:
    rows = (
        await db.execute(select(UserGroupMember.user_id).where(UserGroupMember.group_id == group_id))
    ).all()
    return [r[0] for r in rows]


@router.put("/groups/{group_id}/members")
@audited("group.members", "user_group", id_arg="group_id")
async def set_group_members(
    group_id: int,
    body: GroupMembersIn,
    user: User = Depends(require_permission("group:manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from sqlalchemy import delete as sql_delete

    g = await db.get(UserGroup, group_id)
    if not g or g.tenant_id != user.tenant_id:
        raise NotFoundError("用户组不存在")
    await db.execute(sql_delete(UserGroupMember).where(UserGroupMember.group_id == group_id))
    for uid in set(body.user_ids):
        db.add(UserGroupMember(user_id=uid, group_id=group_id))
    await db.flush()
    return {"message": "已保存"}
