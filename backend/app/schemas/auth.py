"""鉴权相关 DTO。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    # 限制长度，防超长输入 DoS / 撞哈希
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    # 可选：多组织部署下用来指定登录到哪个组织（同名账号可存在于不同组织）
    tenant_slug: str | None = Field(default=None, max_length=64)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=128)
    department_id: int | None = None  # 申请归属部门（注册表单可选）
    reason: str | None = Field(default=None, max_length=256)  # 申请理由（供管理员审核参考）
    # 落哪个组织：邀请码优先，其次 slug；多组织部署时必须提供其一，
    # 单组织部署可留空（行为与改造前一致）
    invite_code: str | None = Field(default=None, max_length=128)
    tenant_slug: str | None = Field(default=None, max_length=64)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class PasswordChangeRequest(BaseModel):
    old_password: str
    new_password: str


class ProfileUpdateRequest(BaseModel):
    """个人资料自助编辑（仅本人可改的字段）。"""
    display_name: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=128)
    phone: str | None = Field(default=None, max_length=32)
    avatar: str | None = Field(default=None, max_length=256)


class RoleBrief(BaseModel):
    id: int
    code: str
    name: str


class UserOut(BaseModel):
    id: int
    tenant_id: int
    username: str
    display_name: str
    email: str | None = None
    phone: str | None = None
    avatar: str | None = None
    is_admin: bool = False
    department_id: int | None = None
    department_name: str | None = None
    roles: list[RoleBrief] = []
    permissions: list[str] = []

    model_config = {"from_attributes": True}
