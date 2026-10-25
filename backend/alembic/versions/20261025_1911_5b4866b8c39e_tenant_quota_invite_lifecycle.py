"""tenant quota invite lifecycle

Revision ID: 5b4866b8c39e
Revises: 7ac2bcd7a8b5
Create Date: 2026-10-25 19:11:52.258170

给 tenant 加「邀请码 + 配额 + 备注」，用于多组织部署下的自助入组与资源边界。

两处必须手工修正（autogenerate 生成的原版不能直接用）：

1. **SQLite 不支持 ALTER 加唯一约束** —— 原版 ``op.create_unique_constraint``
   在 SQLite 上直接报 "No support for ALTER of constraints"。改成 batch 模式重建表。
2. **约束必须显式命名** —— 原版传的是 ``None``，PG 上会建出随机名约束，
   downgrade 时 ``drop_constraint(None, ...)`` 必然失败。

``invite_code`` 可空且唯一：SQLite 与 PostgreSQL 都允许多个 NULL，
即「未开放邀请」的组织可以有很多个，符合预期。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '5b4866b8c39e'
down_revision: Union[str, None] = '7ac2bcd7a8b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UQ_NAME = "uq_tenant_invite_code"


def upgrade() -> None:
    # SQLite 加唯一约束必须重建表 → 统一走 batch，两种方言都成立。
    # 配额默认 0 = 不限，与改造前行为一致（存量组织不会被突然限额）。
    with op.batch_alter_table("tenant") as batch:
        batch.add_column(sa.Column("invite_code", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column("max_users", sa.Integer(), server_default=sa.text("0"), nullable=False)
        )
        batch.add_column(
            sa.Column("max_documents", sa.Integer(), server_default=sa.text("0"), nullable=False)
        )
        batch.add_column(
            sa.Column(
                "max_storage_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False
            )
        )
        batch.add_column(sa.Column("note", sa.String(length=512), nullable=True))
        batch.create_unique_constraint(_UQ_NAME, ["invite_code"])


def downgrade() -> None:
    with op.batch_alter_table("tenant") as batch:
        batch.drop_constraint(_UQ_NAME, type_="unique")
        batch.drop_column("note")
        batch.drop_column("max_storage_bytes")
        batch.drop_column("max_documents")
        batch.drop_column("max_users")
        batch.drop_column("invite_code")
