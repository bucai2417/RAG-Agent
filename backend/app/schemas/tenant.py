"""租户（组织）DTO。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class TenantOut(BaseModel):
    id: int
    name: str
    slug: str
    status: str
    plan: str
    invite_code: str | None = None
    max_users: int = 0
    max_documents: int = 0
    max_storage_bytes: int = 0
    note: str | None = None
    created_at: str | None = None
    # 用量（列表页展示，避免前端逐个请求盘点接口）
    users: int = 0
    documents: int = 0
    storage_bytes: int = 0


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    slug: str = Field(min_length=1, max_length=64)
    plan: str = Field(default="default", max_length=32)
    max_users: int = Field(default=0, ge=0)
    max_documents: int = Field(default=0, ge=0)
    max_storage_bytes: int = Field(default=0, ge=0)
    note: str | None = Field(default=None, max_length=512)
    # 可选：同时创建该组织的第一位管理员（不开则先建组织、之后由管理员代建账号）
    admin_username: str | None = Field(default=None, min_length=2, max_length=64)
    admin_password: str | None = Field(default=None, min_length=8, max_length=128)
    admin_display_name: str | None = Field(default=None, max_length=64)
    admin_email: str | None = Field(default=None, max_length=128)


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    status: str | None = None
    plan: str | None = Field(default=None, max_length=32)
    max_users: int | None = Field(default=None, ge=0)
    max_documents: int | None = Field(default=None, ge=0)
    max_storage_bytes: int | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=512)


class TenantMeOut(BaseModel):
    """当前用户所属组织（任何登录用户可读，不含配额以外的运营字段）。"""

    id: int
    name: str
    slug: str
    status: str
    plan: str
    # 是否平台管理员：前端据此决定是否显示「组织管理」入口
    is_platform_admin: bool = False
    # 配额上限为 0 表示不限；前端据此决定是否显示用量条
    max_users: int = 0
    max_documents: int = 0
    max_storage_bytes: int = 0
    users: int = 0
    documents: int = 0
    storage_bytes: int = 0


class QuotaUpdate(BaseModel):
    max_users: int | None = Field(default=None, ge=0)
    max_documents: int | None = Field(default=None, ge=0)
    max_storage_bytes: int | None = Field(default=None, ge=0)


class InviteCodeOut(BaseModel):
    invite_code: str | None = None
    # 邀请码为空时前端据此提示「当前为管理员代建账号模式」
    enabled: bool = False
    # 拼好的注册链接（前端可直接展示/复制）
    register_url: str = ""


class TenantPublicOut(BaseModel):
    """公开端点：仅供注册页展示可选组织，不含任何运营数据。"""

    slug: str
    name: str


class RegisterTenantField(BaseModel):
    """注册页需要知道的组织路由规则（是否需要填组织标识/邀请码）。"""

    multi_tenant: bool = False
    require_selector: bool = False
    tenants: list[TenantPublicOut] = []
    default_slug: str | None = None

    @classmethod
    def from_count(cls, count: int, tenants: list[dict]) -> "RegisterTenantField":
        return cls(
            multi_tenant=count > 1,
            # 只有组织数 > 1 时才必须显式指定：单组织部署保持原有「填用户名密码即可」的体验
            require_selector=count > 1,
            tenants=[TenantPublicOut(**t) for t in tenants],
            default_slug=(tenants[0]["slug"] if count == 1 and tenants else None),
        )
