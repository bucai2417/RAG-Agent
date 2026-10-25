"""权限原子项与内置角色的定义与播种（幂等）。

被 scripts/seed.py 调用，也可在生产环境单独执行。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Permission, Role, RolePermission, User, UserRole

# ===== 权限原子项（resource:action）=====
PERMISSIONS: list[tuple[str, str, str, str]] = [
    # (code, name, resource, action)
    ("kb:create", "创建知识库", "kb", "create"),
    ("kb:read", "查看知识库", "kb", "read"),
    ("kb:update", "编辑知识库", "kb", "update"),
    ("kb:delete", "删除知识库", "kb", "delete"),
    ("kb:member_manage", "管理知识库成员", "kb", "member_manage"),
    ("doc:upload", "上传文档", "doc", "upload"),
    ("doc:read", "查看文档", "doc", "read"),
    ("doc:update", "编辑文档", "doc", "update"),
    ("doc:delete", "删除文档", "doc", "delete"),
    ("doc:download", "下载文档", "doc", "download"),
    ("doc:acl_manage", "管理文档权限", "doc", "acl_manage"),
    ("file:read", "读取对话文件", "file", "read"),
    ("file:write", "生成/改写文件", "file", "write"),
    ("file:manage", "管理全部文件", "file", "manage"),
    ("retrieval:query", "检索", "retrieval", "query"),
    ("chat:use", "使用问答", "chat", "use"),
    ("chat:read_all", "查看全部用户问答", "chat", "read_all"),
    ("model:read", "查看模型", "model", "read"),
    ("model:manage", "管理模型", "model", "manage"),
    ("user:read", "查看用户", "user", "read"),
    ("user:manage", "管理用户", "user", "manage"),
    ("role:read", "查看角色", "role", "read"),
    ("role:manage", "管理角色", "role", "manage"),
    ("dept:read", "查看部门", "dept", "read"),
    ("dept:manage", "管理部门", "dept", "manage"),
    ("group:read", "查看用户组", "group", "read"),
    ("group:manage", "管理用户组", "group", "manage"),
    ("audit:read", "查看审计日志", "audit", "read"),
    ("apikey:read", "查看密钥", "apikey", "read"),
    ("apikey:manage", "管理密钥", "apikey", "manage"),
    ("workflow:read", "查看工作流", "workflow", "read"),
    ("workflow:edit", "编辑工作流", "workflow", "edit"),
    ("workflow:run", "运行工作流", "workflow", "run"),
    # 审核公共模板等跨租户管理动作（仅管理员/平台运营）
    ("workflow:manage", "管理工作流模板", "workflow", "manage"),
    ("app:read", "查看应用", "app", "read"),
    ("app:edit", "编辑应用", "app", "edit"),
    ("app:run", "运行应用", "app", "run"),
    ("agent:read", "查看智能体", "agent", "read"),
    ("agent:edit", "编辑智能体", "agent", "edit"),
    ("agent:run", "运行智能体", "agent", "run"),
    ("skill:read", "查看技能", "skill", "read"),
    ("skill:edit", "编辑技能", "skill", "edit"),
    ("tool:read", "查看工具", "tool", "read"),
    ("tool:manage", "管理工具", "tool", "manage"),
    ("tool:invoke", "调用外部工具", "tool", "invoke"),
    ("mcp:read", "查看 MCP 服务器", "mcp", "read"),
    ("mcp:manage", "管理 MCP 服务器", "mcp", "manage"),
    ("mcp:invoke", "调用 MCP 工具", "mcp", "invoke"),
    ("skill:execute", "执行技能脚本", "skill", "execute"),
    ("agent:admin_tool", "使用管理类 AI 工具", "agent", "admin_tool"),
    ("schedule:read", "查看定时任务", "schedule", "read"),
    ("schedule:manage", "管理定时任务", "schedule", "manage"),
    ("channel:read", "查看外部渠道", "channel", "read"),
    ("channel:manage", "管理外部渠道", "channel", "manage"),
    ("notify:read", "查看通知渠道", "notify", "read"),
    ("notify:manage", "管理通知渠道", "notify", "manage"),
    ("service:read", "查看客服工单", "service", "read"),
    ("service:manage", "处理客服工单", "service", "manage"),
    ("service:submit", "外部系统提交工单（对外 API）", "service", "submit"),
    ("record:read", "查看录单记录", "record", "read"),
    ("record:manage", "智能录单", "record", "manage"),
    ("eval:read", "查看问答评测", "eval", "read"),
    ("eval:manage", "管理问答评测（建集/运行）", "eval", "manage"),
    ("system:read", "查看系统日志", "system", "read"),
    ("system:write", "管理系统日志（清理）", "system", "write"),
    ("system:manage", "管理系统设置", "system", "manage"),
    ("sso:read", "查看 SSO", "sso", "read"),
    ("sso:manage", "管理 SSO", "sso", "manage"),
    # 拆成两项而不是一项：
    #   tenant:manage —— 配额/计费边界，属于平台侧，不该由被限额方自调；
    #   tenant:invite —— 本组织的邀请入组与邀请码轮换，这是日常运营动作。
    # 早先只有 tenant:manage 一项，导致组织管理员连自己组织的邀请码都改不了。
    ("tenant:manage", "调整组织配额", "tenant", "manage"),
    ("tenant:invite", "管理组织邀请入组", "tenant", "invite"),
]

# 权限 code -> 简写分组
_ALL = [p[0] for p in PERMISSIONS]

# ===== 内置角色（tenant_id=None, is_system=True）=====
# 精简为 6 个核心角色，覆盖绝大多数企业场景；更细粒度授权走「知识库成员」/「部门」
# 或由管理员按需自建自定义角色。
# (code, name, scope, 权限 code 列表, 用途描述)
ROLES: list[tuple[str, str, str, list[str], str]] = [
    ("super_admin", "超级管理员", "platform", _ALL, "平台最高权限，可管理所有租户；一般仅系统所有者使用"),
    ("tenant_admin", "租户管理员", "tenant",
     # 含 tenant:invite（管自己组织的邀请入组），但不含 tenant:manage（配额由平台侧控制）
     [c for c in _ALL if c != "tenant:manage"], "管理本租户的用户、角色、知识库与全部业务功能"),
    ("kb_admin", "知识库管理员", "tenant", [
        "kb:create", "kb:read", "kb:update", "kb:delete", "kb:member_manage",
        "doc:upload", "doc:read", "doc:update", "doc:delete", "doc:download", "doc:acl_manage",
        "retrieval:query", "chat:use", "audit:read", "eval:read", "eval:manage",
    ], "管理全部知识库（建库/上传/成员/删除），不含用户与系统设置"),
    ("app_admin", "应用管理员", "tenant", [
        "app:read", "app:edit", "app:run",
        "workflow:read", "workflow:edit", "workflow:run",
        "model:read", "retrieval:query", "chat:use",
        "agent:read", "agent:edit", "agent:run", "skill:read", "tool:read",
    ], "管理应用与工作流编排，可配置模型，不含用户/知识库管理"),
    ("agent_admin", "智能体管理员", "tenant", [
        "agent:read", "agent:edit", "agent:run",
        "skill:read", "skill:edit", "tool:read", "tool:manage",
        "mcp:read", "mcp:invoke",
        "workflow:read", "workflow:edit", "workflow:run",
        "model:read", "retrieval:query", "chat:use",
    ], "管理智能体、技能、工具与 MCP 服务"),
    ("viewer", "普通用户", "tenant", ["kb:read", "doc:read", "file:read", "retrieval:query", "chat:use"],
     "默认角色：可问答、检索与查看知识库，不能修改任何内容。新用户一般给这个"),
    # 外部客服客户：能问答、检索、调只读外部工具（查订单/物流），但无法用写/管理工具
    ("service_agent", "客服客户", "tenant", [
        "chat:use", "retrieval:query", "service:submit", "tool:invoke", "mcp:invoke",
    ], "外部客服场景：可问答、检索、提交工单、调用只读工具（查订单/物流），不能管理"),
]

# 历史内置角色：已下线。若库中仍存在则由 seed 迁移其授予到 viewer 后删除。
# （部门级/知识库级的精细授权请改用「知识库成员」页，或自建角色）
RETIRED_ROLE_CODES = {
    "kb_editor", "kb_viewer", "guest",
    "dept_viewer", "dept_editor", "dept_agent_admin",
}


async def sync_role_meta(db: AsyncSession) -> int:
    """仅同步内置角色的名称/范围/描述（不动权限、不新增角色）。启动时调用。

    用于版本升级后文案变更（如补描述、改中文名）自动生效，无需手动 seed。
    返回更新的角色数。
    """
    from sqlalchemy import select as _select

    updated = 0
    for code, name, scope, _perm_codes, desc in ROLES:
        role = (
            await db.execute(_select(Role).where(Role.tenant_id.is_(None), Role.code == code))
        ).scalar_one_or_none()
        if not role:
            continue
        changed = False
        if role.name != name:
            role.name = name; changed = True
        if role.scope != scope:
            role.scope = scope; changed = True
        if desc and role.description != desc:
            role.description = desc; changed = True
        if changed:
            updated += 1
    # 一并下线历史角色（幂等）
    viewer = (
        await db.execute(select(Role).where(Role.tenant_id.is_(None), Role.code == "viewer"))
    ).scalar_one_or_none()
    await _retire_legacy_roles(db, retire_target_code="viewer", viewer_id=viewer.id if viewer else None)
    await db.commit()
    return updated


async def seed_permissions_and_roles(db: AsyncSession) -> dict[str, int]:
    """幂等播种权限与内置角色，返回 {role_code: role_id}。"""
    # 1. 权限
    existing = {
        p.code for (p,) in (await db.execute(select(Permission).where(Permission.code.in_(_ALL)))).all()
    }
    code_to_perm: dict[str, Permission] = {}
    for code, name, resource, action in PERMISSIONS:
        if code in existing:
            perm = (await db.execute(select(Permission).where(Permission.code == code))).scalar_one()
        else:
            perm = Permission(code=code, name=name, resource=resource, action=action)
            db.add(perm)
            await db.flush()
        code_to_perm[code] = perm

    # 2. 角色
    role_ids: dict[str, int] = {}
    for code, name, scope, perm_codes, desc in ROLES:
        role = (
            await db.execute(select(Role).where(Role.tenant_id.is_(None), Role.code == code))
        ).scalar_one_or_none()
        if not role:
            role = Role(tenant_id=None, code=code, name=name, scope=scope, is_system=True, description=desc)
            db.add(role)
            await db.flush()
        else:
            # 同步内置角色的名称/范围/描述（便于升级后文案更新）
            role.name = name
            role.scope = scope
            if desc:
                role.description = desc
        role_ids[code] = role.id

        # 3. 角色权限（幂等：先删后建，保证与定义一致）
        from sqlalchemy import delete

        await db.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
        for pc in perm_codes:
            if pc in code_to_perm:
                db.add(RolePermission(role_id=role.id, permission_id=code_to_perm[pc].id))
    await db.flush()

    # 4. 下线历史内置角色：把授予迁移到 viewer，再删除角色本身（幂等）
    await _retire_legacy_roles(db, retire_target_code="viewer", viewer_id=role_ids.get("viewer"))
    return role_ids


async def _retire_legacy_roles(db: AsyncSession, *, retire_target_code: str, viewer_id: int | None) -> None:
    """清理 RETIRED_ROLE_CODES 中的历史内置角色。

    - 若有用户仍持有 → 迁移为 viewer（避免这些用户权限为空、全站 403）
    - 删除其权限关联与角色记录
    幂等：角色不存在则跳过。
    """
    from sqlalchemy import delete

    if not RETIRED_ROLE_CODES:
        return
    retired = (
        await db.execute(
            select(Role).where(Role.tenant_id.is_(None), Role.is_system.is_(True),
                               Role.code.in_(list(RETIRED_ROLE_CODES)))
        )
    ).scalars().all()
    for r in retired:
        if viewer_id and r.id != viewer_id:
            # 把授予该角色的用户改授为 viewer（同 scope 语义：tenant 级全局生效）
            grants = (
                await db.execute(select(UserRole).where(UserRole.role_id == r.id))
            ).scalars().all()
            for g in grants:
                already = (
                    await db.execute(
                        select(UserRole).where(
                            UserRole.user_id == g.user_id, UserRole.role_id == viewer_id
                        )
                    )
                ).scalar_one_or_none()
                if already:
                    await db.delete(g)
                else:
                    g.role_id = viewer_id
                    g.scope_type = "tenant"
                    g.scope_id = 0
        await db.execute(delete(RolePermission).where(RolePermission.role_id == r.id))
        await db.delete(r)
    if retired:
        await db.flush()


async def backfill_admin_super(db: AsyncSession, tenant_id: int, role_ids: dict[str, int]) -> None:
    """给现有 is_admin 用户回填 super_admin 授予（幂等）。"""
    admins = (
        await db.execute(select(User).where(User.is_admin.is_(True), User.tenant_id == tenant_id))
    ).scalars().all()
    super_id = role_ids.get("super_admin")
    if not super_id:
        return
    for u in admins:
        exists = (
            await db.execute(
                select(UserRole).where(
                    UserRole.user_id == u.id,
                    UserRole.role_id == super_id,
                    UserRole.scope_type == "tenant",
                )
            )
        ).scalar_one_or_none()
        if not exists:
            db.add(
                UserRole(
                    tenant_id=tenant_id,
                    user_id=u.id,
                    role_id=super_id,
                    scope_type="tenant",
                    scope_id=0,
                )
            )
    await db.flush()


async def backfill_viewer_role(db: AsyncSession, tenant_id: int, role_ids: dict[str, int]) -> None:
    """给非管理员、且没有任何角色授予的用户回填 viewer（幂等）。

    背景：补齐权限码强制后，无角色用户权限集为空会全站 403，需兜底为普通用户。
    """
    viewer_id = role_ids.get("viewer")
    if not viewer_id:
        return
    users = (
        await db.execute(select(User).where(User.is_admin.is_(False), User.tenant_id == tenant_id))
    ).scalars().all()
    for u in users:
        has_role = (
            await db.execute(select(UserRole.id).where(UserRole.user_id == u.id).limit(1))
        ).scalar_one_or_none()
        if has_role is None:
            db.add(
                UserRole(
                    tenant_id=tenant_id,
                    user_id=u.id,
                    role_id=viewer_id,
                    scope_type="tenant",
                    scope_id=0,
                )
            )
    await db.flush()
