"""登录安全加固回归：密码强度、防爆破、token 吊销、注册审核、SQL 注入加固。"""
from __future__ import annotations

import asyncio
import inspect


# ==================== 密码强度 ====================
def test_password_strength_rules():
    from app.core.security import check_password_strength

    assert check_password_strength("abc")[0] is False          # 太短
    assert check_password_strength("admin123")[0] is False     # 弱密码
    assert check_password_strength("password")[0] is False     # 弱密码
    assert check_password_strength("aaaaaaaa")[0] is False     # 只有字母（单类）
    assert check_password_strength("12345678")[0] is False     # 只有数字（单类）
    # 不强求大小写：小写字母 + 数字即可
    assert check_password_strength("abc12345")[0] is True
    assert check_password_strength("abcd1234")[0] is True
    assert check_password_strength("Password123!")[0] is True
    assert check_password_strength("Qq123456!")[0] is True
    # 字母 + 符号也可（不含数字）
    assert check_password_strength("abcdefg!")[0] is True


def test_password_validation_raises():
    from app.core.errors import ValidationError
    from app.core.security import validate_password_or_raise

    try:
        validate_password_or_raise("123456")
        assert False, "弱密码应该抛错"
    except ValidationError:
        pass


# ==================== 防爆破 ====================
def test_login_guard_lockout():
    from app.services import login_guard

    login_guard.reset_all()
    login_guard.ensure_not_locked("bob")  # 初始未锁定
    from app.core.config import settings
    for _ in range(settings.login_max_failures):
        login_guard.record_failure("bob")
    try:
        login_guard.ensure_not_locked("bob")
        assert False, "达到阈值后应锁定"
    except Exception:
        pass
    login_guard.record_success("bob")      # 成功登录解除锁定
    login_guard.ensure_not_locked("bob")
    login_guard.reset_all()


def test_login_guard_ip_rate_limit():
    from app.services import login_guard
    from app.core.config import settings

    async def _run():
        # 连续请求超过每分钟上限后应被拒
        allowed = 0
        for _ in range(settings.login_rate_per_min + 3):
            try:
                await login_guard.guard_ip("9.9.9.9")
                allowed += 1
            except Exception:
                break
        return allowed
    allowed = asyncio.new_event_loop().run_until_complete(_run())
    assert allowed <= settings.login_rate_per_min, "IP 限流应在上限内放行"


def test_login_uses_guard():
    import inspect

    from app.api.v1 import auth

    src = inspect.getsource(auth.login)
    assert "guard_ip" in src and "ensure_not_locked" in src and "record_failure" in src


# ==================== token 吊销 ====================
def test_token_carries_version():
    from app.core.security import create_access_token, decode_token

    tok = create_access_token(1, 1, token_version=7)
    assert decode_token(tok).get("tv") == 7


def test_auth_dep_checks_token_version():
    import inspect

    from app.middleware import auth_dep

    src = inspect.getsource(auth_dep.get_current_user)
    assert "token_version" in src and '"tv"' in src


def test_change_password_bumps_version():
    import inspect

    from app.api.v1 import auth

    src = inspect.getsource(auth.change_password)
    assert "token_version" in src
    assert "+ 1" in src


# ==================== 注册审核 ====================
def test_user_has_approval_fields():
    from app.models import User

    cols = set(User.__table__.columns.keys())
    assert {"approval_status", "token_version", "registered_at"} <= cols


def test_register_endpoint_exists():
    from app.api.v1 import auth as A

    methods = {(r.path, m) for r in A.router.routes for m in getattr(r, "methods", set())}
    assert ("/auth/register", "POST") in methods


def test_review_endpoints_exist():
    from app.api.v1 import rbac as R

    methods = {(r.path, m) for r in R.router.routes for m in getattr(r, "methods", set())}
    assert ("/admin/users/pending", "GET") in methods
    assert ("/admin/users/{user_id}/approve", "POST") in methods
    assert ("/admin/users/{user_id}/reject", "POST") in methods


def test_pending_user_cannot_login():
    import inspect

    from app.api.v1 import auth

    src = inspect.getsource(auth.login)
    assert "approval_status" in src and "pending" in src


def test_admin_created_user_is_approved():
    import inspect

    from app.api.v1.rbac import create_user

    src = inspect.getsource(create_user)
    assert 'approval_status="approved"' in src


def test_register_validates_password():
    import inspect

    from app.api.v1 import auth

    src = inspect.getsource(auth.register)
    assert "validate_password_or_raise" in src


# ==================== SQL 注入加固 ====================
def test_sql_connector_rejects_query_placeholder():
    from app.connectors.drivers.sql_db import SqlConnector
    from app.core.errors import ValidationError

    try:
        SqlConnector(kb_id=1, config={
            "dsn": "sqlite:///./x.db",
            "query_sql": "SELECT * FROM t WHERE name LIKE '%{query}%'",
        })
        assert False, "{query} 拼接占位符应被拒绝"
    except ValidationError:
        pass


def test_sql_connector_allows_bound_param():
    from app.connectors.drivers.sql_db import SqlConnector

    # :query 参数绑定应被接受
    SqlConnector(kb_id=1, config={
        "dsn": "sqlite:///./x.db",
        "query_sql": "SELECT * FROM t WHERE name LIKE :query",
    })


def test_sql_readonly_blocks_comment_bypass():
    from app.connectors.drivers.sql_db import _check_readonly
    from app.core.errors import ValidationError

    # 注释绕过的写关键字应被拦
    try:
        _check_readonly("SELECT 1; DROP TABLE x")
        assert False, "多语句应被拒绝"
    except ValidationError:
        pass
    # 纯 select 应通过
    _check_readonly("SELECT a, b FROM t WHERE x = :q")


def test_like_escape():
    from app.connectors.drivers.sql_db import _escape_like

    assert _escape_like("100%") == "100" + chr(92) + "%"
    assert _escape_like("a_b") == "a" + chr(92) + "_b"


# ==================== secret_key 加固 ====================
def test_secret_key_not_placeholder():
    from app.core.config import _SECRET_PLACEHOLDER, settings

    assert settings.secret_key and settings.secret_key != _SECRET_PLACEHOLDER, \
        "运行时密钥不得为占位默认值"


# ==================== 注册可选部门 + 记住密码（前端）====================
def test_register_accepts_department():
    from app.schemas.auth import RegisterRequest

    assert "department_id" in RegisterRequest.model_fields
    # 可选
    r = RegisterRequest(username="u1", password="Passw0rd@1")
    assert r.department_id is None


def test_public_departments_endpoint_exists():
    from app.api.v1 import auth as A

    methods = {(r.path, m) for r in A.router.routes for m in getattr(r, "methods", set())}
    assert ("/auth/register/departments", "GET") in methods


def test_register_validates_department_tenant():
    """注册时部门归属需校验（防指定他租户部门）。"""
    import inspect

    from app.api.v1 import auth

    src = inspect.getsource(auth.register)
    assert "department_id" in src and "tenant_id == tenant.id" in src


def test_pending_list_includes_department():
    import inspect

    from app.api.v1 import rbac

    src = inspect.getsource(rbac.list_pending_users)
    assert "department_name" in src


# ==================== 注册申请通知管理员 ====================
def test_user_notify_service_exists():
    from app.services import user_notify as U

    assert hasattr(U, "notify_admins_registration")
    assert hasattr(U, "notify_user_review_result")


def test_register_notifies_admins():
    import inspect

    from app.api.v1 import auth

    src = inspect.getsource(auth.register)
    assert "notify_admins_registration" in src


def test_approve_reject_notify_applicant():
    import inspect

    from app.api.v1 import rbac

    assert "notify_user_review_result" in inspect.getsource(rbac.approve_user)
    assert "notify_user_review_result" in inspect.getsource(rbac.reject_user)


def test_notify_targets_tenant_admins():
    """通知应发给本租户管理员（is_admin + active）。"""
    import inspect

    from app.services import user_notify

    src = inspect.getsource(user_notify._admin_ids)
    assert "is_admin" in src and "status" in src


def test_review_notification_uses_inapp():
    """审核结果用站内消息通知申请人。"""
    import inspect

    from app.services import user_notify

    src = inspect.getsource(user_notify.notify_user_review_result)
    assert "create_inapp" in src


def test_dispatch_supports_skip_inapp():
    """dispatch 支持跳过站内写入（调用方已自行写入时避免重复/锁等待）。"""
    import inspect

    from app.notifiers.registry import dispatch

    assert "skip_inapp" in inspect.signature(dispatch).parameters


async def _run_registration_notify():
    from sqlalchemy import select

    from app.core.config import settings
    from app.core.db import AsyncSessionLocal, init_models
    from app.models import Notification, Role, Tenant, User, UserRole
    from app.core.security import hash_password

    # 自助注册默认已关闭；本用例测的是「注册后通知管理员」，故显式开启
    _prev_reg = settings.registration_enabled
    settings.registration_enabled = True

    await init_models()
    async with AsyncSessionLocal() as db:
        t = (await db.execute(select(Tenant).order_by(Tenant.id))).scalars().first()
        if not t:
            t = Tenant(name="NT", slug="nt"); db.add(t); await db.flush()
        # 确保有管理员
        admin = (await db.execute(select(User).where(
            User.tenant_id == t.id, User.is_admin.is_(True)))).scalars().first()
        if not admin:
            admin = User(tenant_id=t.id, username="nt_admin", password_hash=hash_password("x"),
                         is_admin=True, status="active")
            db.add(admin); await db.flush()
        await db.commit()
        tid, aid = t.id, admin.id

    # 调注册（走 API 函数，触发通知）
    from app.api.v1.auth import register
    from app.schemas.auth import RegisterRequest
    import time as _t

    uname = f"nt_user_{int(_t.time() * 1000) % 10**8}"
    async with AsyncSessionLocal() as db:
        class _Req:  # 模拟 Request 只需 headers/client
            headers = {}
            client = None
        # 必须显式指定组织：测试库中通常已有多个组织，无参注册会被明确拒绝
        # （这正是本次改造的目的——不再静默落进「编号最小的那个」）
        await register(RegisterRequest(username=uname, password="Notify@2026x",
                                       display_name="通知测试", reason="测试通知",
                                       tenant_slug=t.slug), _Req(), db)
        await db.commit()

    async with AsyncSessionLocal() as db:
        notes = (await db.execute(select(Notification).where(
            Notification.tenant_id == tid, Notification.user_id == aid,
            Notification.ref_type == "user",
        ).order_by(Notification.id.desc()))).scalars().all()
        assert notes, "管理员应收到注册申请通知"
        n = notes[0]
        assert "注册申请" in n.title
        assert n.link == "/admin/users"
        assert uname in (n.body or "")
        # 清理
        u = (await db.execute(select(User).where(User.username == uname))).scalar_one_or_none()
        if u:
            await db.delete(u)
        await db.commit()

    # 还原开关，避免影响其它用例
    settings.registration_enabled = _prev_reg


def test_registration_sends_admin_notification():
    import asyncio

    asyncio.new_event_loop().run_until_complete(_run_registration_notify())


# ==================== 审核通过授予基础角色 ====================
def test_approve_grants_default_role():
    """审核通过注册申请时须授予基础角色，否则新用户权限为空、全站 403。"""
    import inspect

    from app.api.v1 import rbac

    src = inspect.getsource(rbac.approve_user)
    assert "_grant_default_viewer_role" in src
    assert 'if not u.is_admin' in src


def test_grant_default_role_is_idempotent():
    """管理员建号与审核通过共用同一幂等辅助函数。"""
    import inspect

    from app.api.v1 import rbac

    helper = inspect.getsource(rbac._grant_default_viewer_role)
    assert "exists" in helper, "应检查是否已授予，避免重复"
    assert "_grant_default_viewer_role" in inspect.getsource(rbac.create_user)


async def _run_approve_grants_role():
    from sqlalchemy import select

    from app.core.db import AsyncSessionLocal, init_models
    from app.core.security import hash_password
    from app.models import Role, Tenant, User, UserRole

    await init_models()
    async with AsyncSessionLocal() as db:
        t = (await db.execute(select(Tenant).order_by(Tenant.id))).scalars().first()
        if not t:
            t = Tenant(name="RG", slug="rg"); db.add(t); await db.flush()
        # 确保内置 viewer 角色存在（测试库未必跑过 seed）
        viewer = (await db.execute(select(Role).where(
            Role.tenant_id.is_(None), Role.code == "viewer"))).scalar_one_or_none()
        if not viewer:
            db.add(Role(tenant_id=None, code="viewer", name="普通用户", is_system=True, scope="tenant"))
            await db.flush()
        admin = (await db.execute(select(User).where(
            User.tenant_id == t.id, User.is_admin.is_(True)))).scalars().first()
        if not admin:
            admin = User(tenant_id=t.id, username="rg_admin", password_hash=hash_password("x"),
                         is_admin=True, status="active")
            db.add(admin); await db.flush()
        # 建一个待审核用户
        u = User(tenant_id=t.id, username=f"rg_{admin.id}_{t.id}", password_hash=hash_password("x"),
                 is_admin=False, status="active", approval_status="pending")
        db.add(u); await db.flush()
        uid, tid = u.id, t.id
        await db.commit()

    from app.api.v1.rbac import approve_user

    async with AsyncSessionLocal() as db:
        adm = await db.get(User, admin.id)
        await approve_user(uid, user=adm, db=db)
        await db.commit()

    async with AsyncSessionLocal() as db:
        viewer = (await db.execute(select(Role).where(
            Role.tenant_id.is_(None), Role.code == "viewer"))).scalar_one_or_none()
        link = (await db.execute(select(UserRole).where(
            UserRole.user_id == uid, UserRole.role_id == viewer.id))).scalar_one_or_none()
        assert link is not None, "审核通过后应授予普通用户角色"
        # 清理
        u = await db.get(User, uid)
        await db.delete(u)
        await db.commit()


def test_approve_actually_grants_role():
    import asyncio

    asyncio.new_event_loop().run_until_complete(_run_approve_grants_role())


# ==================== 角色/部门列表优化 ====================
def test_roles_have_descriptions():
    """内置角色都应有用途描述（列表页展示用）。"""
    from app.services.permission_seed import ROLES

    for item in ROLES:
        assert len(item) == 5, f"ROLES 应为 5 元组（含描述），{item[0]} 不符"
        code, _name, _scope, _perms, desc = item
        assert desc and len(desc) >= 4, f"角色 {code} 缺少用途描述"


def test_role_out_has_counts():
    from app.schemas.rbac import RoleOut

    fields = RoleOut.model_fields
    assert "permission_count" in fields and "user_count" in fields


def test_dept_tree_node_has_member_count():
    from app.schemas.rbac import DeptTreeNode

    assert "member_count" in DeptTreeNode.model_fields


def test_list_roles_returns_counts():
    import inspect

    from app.api.v1 import rbac

    src = inspect.getsource(rbac.list_roles)
    assert "permission_count" in src and "user_count" in src


def test_dept_tree_counts_members():
    import inspect

    from app.api.v1 import rbac

    src = inspect.getsource(rbac.dept_tree)
    assert "member_count" in src


# ==================== 角色精简 + 删除用户 + 授予校验 ====================
def test_roles_simplified_to_core():
    """内置角色精简为核心档（≤7 个），覆盖大企业常用场景。"""
    from app.services.permission_seed import ROLES

    codes = [r[0] for r in ROLES]
    assert len(codes) <= 7, f"内置角色应精简，实际 {len(codes)} 个: {codes}"
    # 核心角色必须在
    for must in ("super_admin", "tenant_admin", "kb_admin", "viewer"):
        assert must in codes, f"缺少核心角色 {must}"
    # 已下线的冗余角色不应在 ROLES
    for gone in ("kb_editor", "kb_viewer", "dept_viewer", "dept_editor", "guest"):
        assert gone not in codes, f"{gone} 应已下线"


def test_retired_roles_listed():
    from app.services.permission_seed import RETIRED_ROLE_CODES

    assert "dept_viewer" in RETIRED_ROLE_CODES
    assert "kb_editor" in RETIRED_ROLE_CODES


def test_delete_user_endpoint_exists():
    from app.api.v1 import rbac as R

    methods = {(r.path, m) for r in R.router.routes for m in getattr(r, "methods", set())}
    assert ("/admin/users/{user_id}", "DELETE") in methods


def test_delete_user_protections():
    """删除用户应有自我/最后管理员/资产保护。"""
    import inspect

    from app.api.v1.rbac import delete_user

    src = inspect.getsource(delete_user)
    assert "不能删除当前登录的账号" in src
    assert "最后一个管理员" in src
    assert "KnowledgeBase" in src  # 资产检查


def test_grant_department_role_validates_scope():
    """部门级角色授予须校验 scope_id 命中用户所属部门，否则不生效应直接拒绝。"""
    import inspect

    from app.api.v1.rbac import grant_user_role

    src = inspect.getsource(grant_user_role)
    assert "resolve_user_dept_ids" in src
    assert "不会生效" in src or "必须是该用户所属部门" in src


async def _run_retire_migrates_grants():
    """历史角色被下线时，其授予应迁移到 viewer（用户不会变成无权限）。"""
    from sqlalchemy import select

    from app.core.db import AsyncSessionLocal, init_models
    from app.core.security import hash_password
    from app.models import Role, Tenant, User, UserRole
    from app.services.permission_seed import seed_permissions_and_roles

    await init_models()
    async with AsyncSessionLocal() as db:
        t = (await db.execute(select(Tenant).order_by(Tenant.id))).scalars().first()
        if not t:
            t = Tenant(name="RT", slug="rt"); db.add(t); await db.flush()
        # 建一个挂着「已下线角色」的用户（模拟历史数据）
        viewer = (await db.execute(select(Role).where(Role.tenant_id.is_(None), Role.code == "viewer"))).scalar_one_or_none()
        retired = (await db.execute(select(Role).where(Role.tenant_id.is_(None), Role.code == "dept_viewer"))).scalar_one_or_none()
        if not viewer or not retired:
            # 先 seed 一次确保角色齐（此环境下 dept_viewer 可能已被清）
            await seed_permissions_and_roles(db)
            await db.commit()
            viewer = (await db.execute(select(Role).where(Role.tenant_id.is_(None), Role.code == "viewer"))).scalar_one()
            retired = (await db.execute(select(Role).where(Role.tenant_id.is_(None), Role.code == "dept_viewer"))).scalar_one_or_none()
            if not retired:
                return  # 已下线，跳过（幂等环境）
        u = User(tenant_id=t.id, username=f"rt_{t.id}", password_hash=hash_password("x"))
        db.add(u); await db.flush()
        db.add(UserRole(tenant_id=t.id, user_id=u.id, role_id=retired.id, scope_type="department", scope_id=1))
        await db.commit()
        uid = u.id
    # 再 seed → 应迁移
    async with AsyncSessionLocal() as db:
        await seed_permissions_and_roles(db)
        await db.commit()
    async with AsyncSessionLocal() as db:
        v = (await db.execute(select(Role).where(Role.tenant_id.is_(None), Role.code == "viewer"))).scalar_one()
        links = (await db.execute(select(UserRole).where(
            UserRole.user_id == uid, UserRole.role_id == v.id))).scalars().all()
        assert links, "历史角色授予应迁移为 viewer"
        # 清理
        for l in (await db.execute(select(UserRole).where(UserRole.user_id == uid))).scalars().all():
            await db.delete(l)
        usr = await db.get(User, uid)
        if usr:
            await db.delete(usr)
        await db.commit()


def test_retired_role_grants_migrate_to_viewer():
    import asyncio

    asyncio.new_event_loop().run_until_complete(_run_retire_migrates_grants())


# ==================== 数据完整性 + 资料编辑 ====================
def test_delete_provider_cascades_model_config():
    """删 Provider 应级联清理其 ModelConfig，避免孤儿配置。"""
    import inspect

    from app.api.v1.provider import delete_provider

    src = inspect.getsource(delete_provider)
    assert "ModelConfig" in src and "delete(" in src


def test_delete_kb_cleans_documents():
    """删知识库应清理名下文档/分块，避免软删库内容仍被检索。"""
    import inspect

    from app.api.v1.kb import delete_kb

    src = inspect.getsource(delete_kb)
    assert "Document" in src and "Chunk" in src and "DocumentFolder" in src


def test_delete_dept_unassigns_users():
    """删部门应把该部门用户置为未分配，避免挂到已删部门。"""
    import inspect

    from app.api.v1.rbac import delete_dept

    src = inspect.getsource(delete_dept)
    assert "department_id=None" in src


def test_profile_update_endpoint():
    from app.api.v1 import auth as A

    methods = {(r.path, m) for r in A.router.routes for m in getattr(r, "methods", set())}
    assert ("/auth/profile", "PATCH") in methods


def test_profile_update_cannot_change_username():
    """个人资料编辑只允许 姓名/邮箱/电话/头像，不含用户名/权限。"""
    from app.schemas.auth import ProfileUpdateRequest

    fields = set(ProfileUpdateRequest.model_fields.keys())
    assert fields == {"display_name", "email", "phone", "avatar"}


def test_ticket_export_route_exists():
    from app.api.v1 import service_tickets as S

    methods = {(r.path, m) for r in S.router.routes for m in getattr(r, "methods", set())}
    assert ("/service-tickets/export", "GET") in methods


def test_audit_filters_resource():
    """审计接口支持按资源类型/对象 id 过滤（供对象追溯）。"""
    import inspect

    from app.api.v1.audit import list_audit_logs

    src = inspect.getsource(list_audit_logs)
    assert "resource_type" in src and "resource_id" in src
