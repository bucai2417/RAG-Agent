"""chat room unique keys

Revision ID: 9d31c0a4f7b2
Revises: 5b4866b8c39e
Create Date: 2026-10-26 01:00:00.000000

给 chat_room 补两条唯一约束，挡住「先查后建」在并发下的重复创建：

1. ``uq_chat_room_tenant_default`` —— 每个租户只能有一个默认大群。
   ``ensure_default_room`` 是「查不到就建」，多人首次登录同时触发会建出多个
   ``is_default`` 房间，此后每个人看到的「全员大群」都不一样。
2. ``uq_chat_room_direct_pair`` —— 同一租户、同一对用户的私聊只能有一个。
   ``get_or_create_direct`` 同样是先查后建，两个人同时点开与对方的私聊会各建一个，
   消息从此分裂。

两处在 SQLite / PostgreSQL 上都用**部分唯一索引**（谓词写在索引上）：

- 私聊键带上 ``kind`` / ``status``：群聊行 ``peer_user_id`` 为 NULL（多行 NULL 不冲突，
  但显式排除更清楚），解散后的旧房间也应在用户重新发起私聊时让位。部分索引的谓词
  不参与唯一性比较，所以只能把这两个列放进键里。
- 默认大群键带 ``is_default AND status = 'active'`` 谓词：非默认房间不该占该键，
  已归档的旧默认大群也不该——归档动作正是把 ``status`` 改成 ``archived``，
  若谓词只看 ``is_default``，清理完仍然冲突（升级时会直接报 UNIQUE failed）。
  同理 ``ensure_default_room`` 只认 active，谓词的口径必须与查询一致。

MySQL 没有部分索引，谓词会被忽略（SQLAlchemy 编译时直接丢弃），于是变成
``(tenant_id)`` 全表唯一 / 私聊键带 kind+status 的唯一 —— 前者会让 MySQL 部署
无法建第二个房间。因此 MySQL 走「只建私聊键、默认大群键跳过」的分支，行为不弱于
改造前（改造前什么约束都没有）。

存量数据：重复行必须先清理，否则建唯一索引直接失败。清理策略是**保留最小 id**
（最早创建、消息最多），把其余同名房间标记为 ``archived`` 而不是删除 ——
删除会连带丢消息，而归档只是让它们从列表里消失，数据仍在库里可事后人工合并。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '9d31c0a4f7b2'
down_revision: Union[str, None] = '5b4866b8c39e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UQ_DEFAULT = "uq_chat_room_tenant_default"
_UQ_DIRECT = "uq_chat_room_direct_pair"
_PREDICATE_DEFAULT_SQLITE = "is_default = 1 AND status = 'active'"
_PREDICATE_DEFAULT_PG = "is_default AND status = 'active'"
_PREDICATE_DIRECT = "kind = 'direct' AND status = 'active'"


def _archive_duplicate_defaults() -> None:
    """同一租户有多个「活跃的」is_default 房间时，只保留最早的一个，其余归档。

    只统计 ``status = 'active'``：索引谓词与 ``ensure_default_room`` 的查询都用
    这个口径，已归档的旧默认大群不占键，也就不需要再动它（动了反而会改历史数据）。

    用 ``op.get_bind()`` 而不是 ``op.execute()`` 的占位符风格：这里要先查出
    要保留的 id 再更新，是两步依赖的语句，必须共用同一个连接。

    SQL 里直接写方言字面量而不走绑定参数：SQLite 的布尔列存 0/1，
    而驱动把 Python ``True`` 绑成 bool，实测这条比较在 SQLite 上匹配不到任何行，
    结果是归档悄悄失效、建唯一索引时才炸。谓词固定、无外部输入，无注入面。
    """
    bind = op.get_bind()
    true_lit = "TRUE" if bind.dialect.name == "postgresql" else "1"
    scope = f"is_default = {true_lit} AND status = 'active'"
    tenants = bind.execute(
        sa.text(
            f"SELECT tenant_id FROM chat_room WHERE {scope} "
            "GROUP BY tenant_id HAVING COUNT(*) > 1"
        )
    ).scalars().all()
    for tenant_id in tenants:
        # 保留最小 id（最早创建、消息最多）；其余归档而非删除——
        # 删除会连带丢消息，归档只是让它们从列表里消失，数据仍可事后人工合并。
        bind.execute(
            sa.text(
                f"UPDATE chat_room SET status = 'archived' "
                f"WHERE tenant_id = :t AND {scope} "
                f"AND id <> (SELECT MIN(id) FROM chat_room "
                f"            WHERE tenant_id = :t AND {scope})"
            ),
            {"t": tenant_id},
        )


def _archive_duplicate_directs() -> None:
    """同一租户同一对用户的多个活跃私聊，只保留最早的一个，其余归档。"""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT tenant_id, owner_id, peer_user_id "
            "FROM chat_room WHERE kind = 'direct' AND status = 'active' "
            "GROUP BY tenant_id, owner_id, peer_user_id HAVING COUNT(*) > 1"
        )
    ).all()
    for tenant_id, owner_id, peer_id in rows:
        bind.execute(
            sa.text(
                "UPDATE chat_room SET status = 'archived' "
                "WHERE tenant_id = :t AND owner_id = :o AND peer_user_id = :p "
                "AND kind = 'direct' AND status = 'active' "
                "AND id <> (SELECT MIN(id) FROM chat_room "
                "           WHERE tenant_id = :t AND owner_id = :o "
                "           AND peer_user_id = :p AND kind = 'direct' "
                "           AND status = 'active')"
            ),
            {"t": tenant_id, "o": owner_id, "p": peer_id},
        )


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    _archive_duplicate_defaults()
    _archive_duplicate_directs()

    # 私聊键：三种方言都支持（MySQL 上谓词被忽略，键本身已足够区分）
    op.create_index(
        _UQ_DIRECT,
        "chat_room",
        ["tenant_id", "owner_id", "peer_user_id", "kind", "status"],
        unique=True,
        **(
            {"sqlite_where": sa.text(_PREDICATE_DIRECT),
             "postgresql_where": sa.text(_PREDICATE_DIRECT)}
            if dialect != "mysql" else {}
        ),
    )

    # 默认大群键：MySQL 无部分索引，``(tenant_id)`` 全唯一会让一个租户只能有一个房间，
    # 因此只在支持部分索引的方言上建（MySQL 维持改造前行为）。
    if dialect != "mysql":
        op.create_index(
            _UQ_DEFAULT,
            "chat_room",
            ["tenant_id"],
            unique=True,
            **(
                {"sqlite_where": sa.text(_PREDICATE_DEFAULT_SQLITE)}
                if dialect == "sqlite"
                else {"postgresql_where": sa.text(_PREDICATE_DEFAULT_PG)}
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        op.drop_index(_UQ_DEFAULT, table_name="chat_room")
    op.drop_index(_UQ_DIRECT, table_name="chat_room")
