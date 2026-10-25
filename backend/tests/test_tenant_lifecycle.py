"""多组织（租户）生命周期与配额回归。

覆盖三件事，每件都对应一个此前会出错的具体场景：

1. **注册路由**：原先固定 ``order_by(id).first()``，所有注册都进 1 号组织，
   多组织部署下调用方完全看不出来。
2. **配额**：0 = 不限（存量部署不受影响）；超限返回 507 且带原因，不是笼统 500。
3. **权限边界**：跨租户端点只认 platform 作用域，普通组织管理员（哪怕 is_admin）
   不得列举/停用别家组织。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.db import AsyncSessionLocal, init_models
from app.core.security import hash_password
from app.main import app
from app.models import Tenant, User, UserRole, Role

c = TestClient(app)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(scope="module", autouse=True)
def _schema():
    """建表 + 播种角色。本文件不依赖其它测试先建库（单跑也必须能过）。"""
    _run(init_models())
    from app.services.permission_seed import seed_permissions_and_roles

    async def _seed():
        async with AsyncSessionLocal() as db:
            await seed_permissions_and_roles(db)
            await db.commit()

    _run(_seed())
    yield


async def _make_tenant(slug: str, name: str | None = None, **kwargs) -> int:
    async with AsyncSessionLocal() as db:
        t = Tenant(name=name or slug, slug=slug, plan="default", **kwargs)
        db.add(t)
        await db.flush()
        tid = t.id
        await db.commit()
        return tid


async def _make_user(tenant_id: int, username: str, *, is_admin: bool = False) -> int:
    async with AsyncSessionLocal() as db:
        u = User(
            tenant_id=tenant_id,
            username=username,
            password_hash=hash_password("Passw0rd!"),
            display_name=username,
            is_admin=is_admin,
            status="active",
            approval_status="approved",
        )
        db.add(u)
        await db.flush()
        uid = u.id
        await db.commit()
        return uid


async def _grant_platform(user_id: int, tenant_id: int) -> None:
    async with AsyncSessionLocal() as db:
        role = (
            await db.execute(
                __import__("sqlalchemy").select(Role).where(
                    Role.tenant_id.is_(None), Role.code == "super_admin"
                )
            )
        ).scalar_one()
        db.add(
            UserRole(
                tenant_id=tenant_id,
                user_id=user_id,
                role_id=role.id,
                scope_type="platform",
                scope_id=0,
            )
        )
        await db.commit()


@pytest.fixture(autouse=True)
def _relax_login_limit(monkeypatch):
    """本文件会反复登录不同账号，而 IP 限流对测试客户端是同一个来源。

    只清账号锁定（reset_all）不够：IP 维度走的是独立令牌桶。这里把上限调高，
    以免 429 掩盖真正的权限断言失败。
    """
    from app.services import login_guard

    login_guard.reset_all()
    monkeypatch.setattr(settings, "login_rate_per_min", 10_000)
    yield
    login_guard.reset_all()


def _login(username: str, password: str = "Passw0rd!") -> str:
    r = c.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _clear_tenants(*slugs: str) -> None:
    """删除测试组织及其用户。

    必须连同 user_role 一起清：授予行没有外键级联，而 SQLite 会复用被删行的
    rowid，残留的 platform 授予会被下一个新建用户「继承」，让权限断言随机失败。
    """

    async def _do():
        from sqlalchemy import delete, select

        from app.models import UserRole
        from app.services.tenant_service import purge_user_grants

        async with AsyncSessionLocal() as db:
            for s in slugs:
                t = (await db.execute(select(Tenant).where(Tenant.slug == s))).scalar_one_or_none()
                if t is None:
                    continue
                uids = [
                    r for (r,) in (
                        await db.execute(select(User.id).where(User.tenant_id == t.id))
                    ).all()
                ]
                for uid in uids:
                    await purge_user_grants(db, uid)
                await db.execute(delete(User).where(User.tenant_id == t.id))
                await db.execute(
                    delete(UserRole).where(UserRole.tenant_id == t.id)
                )
                await db.execute(delete(Tenant).where(Tenant.id == t.id))
            await db.commit()

    _run(_do())


# ==================== 注册路由 ====================
def test_register_routes_by_invite_code(monkeypatch):
    """凭邀请码注册必须落到该组织，而不是编号最小的那个。"""
    monkeypatch.setattr(settings, "registration_enabled", True)
    monkeypatch.setattr(settings, "require_admin_approval", False)
    _clear_tenants("t-inv", "t-other")
    try:
        _run(_make_tenant("t-other", "别的组织"))
        tid = _run(_make_tenant("t-inv", "邀请组织", invite_code="INVCODE-1"))

        r = c.post(
            "/api/v1/auth/register",
            json={
                "username": "inv_user",
                "password": "Passw0rd!",
                "invite_code": "INVCODE-1",
            },
        )
        assert r.status_code == 200, r.text

        async def _check():
            from sqlalchemy import select

            async with AsyncSessionLocal() as db:
                u = (
                    await db.execute(select(User).where(User.username == "inv_user"))
                ).scalar_one()
                return u.tenant_id

        assert _run(_check()) == tid
    finally:
        _clear_tenants("t-inv", "t-other")


def test_register_rejects_unknown_invite_code(monkeypatch):
    monkeypatch.setattr(settings, "registration_enabled", True)
    r = c.post(
        "/api/v1/auth/register",
        json={"username": "nobody_x", "password": "Passw0rd!", "invite_code": "NOPE-NOT-REAL"},
    )
    assert r.status_code >= 400
    assert "邀请码" in str(r.json())


def test_register_requires_selector_when_multiple_tenants(monkeypatch):
    """多组织且不给 slug/邀请码时必须明确报错，不能默默塞进某个组织。"""
    monkeypatch.setattr(settings, "registration_enabled", True)
    _clear_tenants("t-a", "t-b")
    try:
        _run(_make_tenant("t-a", "组织A"))
        _run(_make_tenant("t-b", "组织B"))
        r = c.post(
            "/api/v1/auth/register",
            json={"username": "ambiguous_user", "password": "Passw0rd!"},
        )
        assert r.status_code >= 400
        assert "组织" in str(r.json())
    finally:
        _clear_tenants("t-a", "t-b")


def test_register_into_suspended_tenant_rejected(monkeypatch):
    monkeypatch.setattr(settings, "registration_enabled", True)
    _clear_tenants("t-susp")
    try:
        _run(_make_tenant("t-susp", "停用组织", status="suspended"))
        r = c.post(
            "/api/v1/auth/register",
            json={"username": "susp_user", "password": "Passw0rd!", "tenant_slug": "t-susp"},
        )
        assert r.status_code >= 400
        assert "不接受新成员" in str(r.json())
    finally:
        _clear_tenants("t-susp")


def test_public_register_tenants_shape(monkeypatch):
    monkeypatch.setattr(settings, "registration_enabled", True)
    d = c.get("/api/v1/auth/register/tenants").json()
    assert set(d.keys()) == {"multi_tenant", "require_selector", "tenants", "default_slug"}
    # 公开端点不得暴露配额、备注或内部 id
    for t in d["tenants"]:
        assert set(t.keys()) == {"slug", "name"}


# ==================== 配额 ====================
def test_user_quota_blocks_registration(monkeypatch):
    monkeypatch.setattr(settings, "registration_enabled", True)
    monkeypatch.setattr(settings, "require_admin_approval", False)
    _clear_tenants("t-quota")
    try:
        tid = _run(_make_tenant("t-quota", "限额组织", max_users=1))
        # 组织创建者本人占掉唯一的名额
        _run(_make_user(tid, "quota_owner"))

        r = c.post(
            "/api/v1/auth/register",
            json={"username": "quota_extra", "password": "Passw0rd!", "tenant_slug": "t-quota"},
        )
        assert r.status_code == 507, r.text
        assert "上限" in str(r.json())
    finally:
        _clear_tenants("t-quota")


def test_zero_quota_means_unlimited():
    """0 = 不限：这是存量部署的默认值，必须完全不受影响。"""
    from app.services.tenant_service import QuotaSnapshot

    snap = QuotaSnapshot(
        max_users=0, max_documents=0, max_storage_bytes=0,
        users=99999, documents=99999, storage_bytes=10**12,
    )
    assert snap.users_over is False
    assert snap.documents_over is False
    assert snap.storage_over(10**9) is False


def test_document_quota_counts_bytes():
    from app.services.tenant_service import QuotaSnapshot

    snap = QuotaSnapshot(
        max_users=0, max_documents=0, max_storage_bytes=1000,
        users=0, documents=0, storage_bytes=900,
    )
    assert snap.storage_over(0) is False      # 900 < 1000
    assert snap.storage_over(100) is False    # 恰好 1000，不超
    assert snap.storage_over(101) is True


# ==================== 权限边界 ====================
def test_tenant_list_requires_platform_admin():
    """普通登录用户（无 platform 授予）不得列举全部组织。"""
    _clear_tenants("t-plain")
    try:
        tid = _run(_make_tenant("t-plain", "普通组织"))
        _run(_make_user(tid, "plain_user", is_admin=True))
        tok = _login("plain_user")
        r = c.get("/api/v1/tenants", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403
    finally:
        _clear_tenants("t-plain")


def test_platform_admin_can_list_and_create():
    _clear_tenants("t-plat", "t-created")
    try:
        tid = _run(_make_tenant("t-plat", "平台组织"))
        uid = _run(_make_user(tid, "plat_admin", is_admin=True))
        _run(_grant_platform(uid, tid))
        tok = _login("plat_admin")
        h = {"Authorization": f"Bearer {tok}"}

        r = c.get("/api/v1/tenants", headers=h)
        assert r.status_code == 200, r.text
        assert any(t["slug"] == "t-plat" for t in r.json())

        r = c.post(
            "/api/v1/tenants",
            headers=h,
            json={
                "name": "新组织",
                "slug": "t-created",
                "admin_username": "new_org_admin",
                "admin_password": "Passw0rd!",
            },
        )
        assert r.status_code == 200, r.text
        created = r.json()
        assert created["slug"] == "t-created"
        assert created["users"] == 1  # 一并创建的组织管理员

        # 新组织管理员应当拿到 tenant_admin（本组织管理员），能登录但不能跨租户
        r = c.get("/api/v1/tenants", headers={"Authorization": f"Bearer {_login('new_org_admin')}"})
        assert r.status_code == 403
    finally:
        _clear_tenants("t-plat", "t-created")


def test_cannot_suspend_own_tenant():
    _clear_tenants("t-self")
    try:
        tid = _run(_make_tenant("t-self", "自家组织"))
        uid = _run(_make_user(tid, "self_admin", is_admin=True))
        _run(_grant_platform(uid, tid))
        tok = _login("self_admin")
        r = c.patch(
            f"/api/v1/tenants/{tid}",
            headers={"Authorization": f"Bearer {tok}"},
            json={"status": "suspended"},
        )
        assert r.status_code == 409
        assert "当前所在" in str(r.json())
    finally:
        _clear_tenants("t-self")


def test_tenant_me_shape_and_usage():
    _clear_tenants("t-me")
    try:
        tid = _run(_make_tenant("t-me", "我的组织", max_users=5))
        _run(_make_user(tid, "me_user"))
        tok = _login("me_user")
        r = c.get("/api/v1/tenants/me", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["slug"] == "t-me"
        assert d["max_users"] == 5
        assert d["users"] == 1
        assert d["is_platform_admin"] is False
    finally:
        _clear_tenants("t-me")


def test_slug_validation_rejects_reserved_and_bad_format():
    from app.core.errors import ValidationError
    from app.services.tenant_service import validate_slug

    for bad in ("Admin", "-x", "x-", "a b", "", "system"):
        with pytest.raises(ValidationError):
            validate_slug(bad)
    assert validate_slug("Acme-01") == "acme-01"


def test_invite_code_rotation_invalidates_old(monkeypatch):
    """轮换后旧码必须立刻失效——这是撤销外流邀请链接的唯一手段。"""
    monkeypatch.setattr(settings, "registration_enabled", True)
    _clear_tenants("t-rot")
    try:
        tid = _run(_make_tenant("t-rot", "轮换组织", invite_code="OLD-CODE"))
        uid = _run(_make_user(tid, "rot_admin", is_admin=True))
        _run(_grant_platform(uid, tid))  # super_admin 含 tenant:manage
        tok = _login("rot_admin")
        h = {"Authorization": f"Bearer {tok}"}

        r = c.post("/api/v1/tenants/me/invite-code", headers=h)
        assert r.status_code == 200, r.text
        new_code = r.json()["invite_code"]
        assert new_code and new_code != "OLD-CODE"

        # 旧码注册失败
        r = c.post(
            "/api/v1/auth/register",
            json={"username": "old_code_user", "password": "Passw0rd!", "invite_code": "OLD-CODE"},
        )
        assert r.status_code >= 400
    finally:
        _clear_tenants("t-rot")


def test_tenant_admin_can_manage_invite_but_not_quota():
    """组织管理员必须能管自己组织的邀请入组，但不能自行调整配额。

    这两件事早先共用一个 tenant:manage，导致建好组织后管理员连邀请链接
    都生成不了（新组织等于没有入组入口）。拆成 tenant:invite 后：
    - 邀请：本组织管理员可做（日常运营）
    - 配额：只有平台侧可做（资源/计费边界，不该由被限额方自调）
    """
    _clear_tenants("t-invite")
    try:
        tid = _run(_make_tenant("t-invite", "邀请组织"))
        uid = _run(_make_user(tid, "invite_admin"))
        # 授予本组织的 tenant_admin（与建组织时的行为一致）
        async def _bind():
            from app.services.tenant_service import bind_tenant_admin

            async with AsyncSessionLocal() as db:
                await bind_tenant_admin(db, tenant_id=tid, user_id=uid)
                await db.commit()

        _run(_bind())
        tok = _login("invite_admin")
        h = {"Authorization": f"Bearer {tok}"}

        r = c.post("/api/v1/tenants/me/invite-code", headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["invite_code"]

        r = c.patch("/api/v1/tenants/me/quota", headers=h, json={"max_users": 999})
        assert r.status_code == 403
        assert "tenant:manage" in str(r.json())
    finally:
        _clear_tenants("t-invite")
