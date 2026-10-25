"""模型公共 Mixin。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

# SQLite 只能对 INTEGER PRIMARY KEY 自增，BigInteger 需降级为 Integer
BigIntPK = BigInteger().with_variant(Integer, "sqlite")
BigIntFK = BigInteger


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IdMixin:
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=func.now()
    )


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TenantMixin:
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)


class RowVersionMixin:
    """乐观锁列。

    与业务语义的 ``version``（文档版本号、工作流版本号，用户可见）区分开：
    这一列只由 SQLAlchemy 维护，用来检测「读到之后被别人改过」。
    模型侧需同时声明 ``__mapper_args__ = {"version_id_col": ROW_VERSION_COL}``，
    SQLAlchemy 会把 UPDATE 变成 ``... WHERE id = ? AND row_version = ?``，
    命中 0 行时抛 ``StaleDataError``（由 app/core/errors 转 409）。
    """

    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )


# 声明 version_id_col 时直接引用列对象：Python 类体内取不到 mixin 的属性名
ROW_VERSION_COL = RowVersionMixin.row_version
