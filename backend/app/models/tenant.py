"""租户与部门（组织树，预留完整树形结构）。"""
from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import IdMixin, TenantMixin, TimestampMixin


class Tenant(Base, IdMixin, TimestampMixin):
    """组织（租户）。

    配额字段全部 0 = 不限。判定在 app/services/tenant_service.py 统一做，
    避免各入口各写一套阈值比较（上传、建库、建号三处都要用同一口径）。
    """

    __tablename__ = "tenant"
    # 约束显式命名：不命名时 PG 会生成随机名，downgrade 的 drop_constraint 找不到目标
    __table_args__ = (UniqueConstraint("invite_code", name="uq_tenant_invite_code"),)

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 对外标识：注册/邀请码按它路由，也是 URL 里唯一能出现的租户标识（id 不外露）
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # active/suspended/archived：suspended 禁止登录与注册，archived 视为只读归档
    status: Mapped[str] = mapped_column(String(16), default="active")
    plan: Mapped[str] = mapped_column(String(32), default="default")
    settings: Mapped[str | None] = mapped_column(Text, nullable=True)  # 预留 JSON
    # 邀请码：注册时凭它入组。空 = 只允许管理员代建账号（不开放自助入组）
    invite_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 配额（0 = 不限）
    max_users: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_documents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_storage_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0"
    )
    # 备注：给平台管理员记录来源/合同等，租户自己不可见
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)


class Department(Base, IdMixin, TimestampMixin, TenantMixin):
    """部门树：邻接表 + path（点分隔）+ depth，为下轮部门级授权预留。"""

    __tablename__ = "department"

    parent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    path: Mapped[str] = mapped_column(String(512), default="", index=True)  # 如 "1.3.7."
    depth: Mapped[int] = mapped_column(Integer, default=0)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active")
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
