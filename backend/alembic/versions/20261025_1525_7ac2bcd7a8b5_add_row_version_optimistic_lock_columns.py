"""add row_version optimistic lock columns

Revision ID: 7ac2bcd7a8b5
Revises: 6c546463f5d7
Create Date: 2026-10-25 15:25:27.192958

注意：本文件由 alembic autogenerate 生成后人工核对。迁移必须同时适用于
SQLite（开发/桌面）与 PostgreSQL（生产），方言特定语句请用
``op.get_bind().dialect.name`` 分支。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7ac2bcd7a8b5'
down_revision: Union[str, None] = '6c546463f5d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 乐观锁列：存量行全部从 1 开始。
    # server_default 用 sa.text 而不是字符串，避免 PG 把 '1' 当字符默认值写到整型列上。
    for table in ("agent", "document", "knowledge_base", "workflow"):
        op.add_column(
            table,
            sa.Column("row_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        )


def downgrade() -> None:
    # SQLite 不支持 DROP COLUMN（3.35 之前），用 batch 模式统一处理
    for table in ("agent", "document", "knowledge_base", "workflow"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("row_version")
