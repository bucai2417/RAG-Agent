"""Alembic 环境：连接串与元数据都来自应用自身，保证「应用连哪、迁移打哪」。

要点：
- URL 优先取调用方通过 config.attributes["url"] 传入的值（启动期迁移用当前生效库），
  否则回退到 settings.database_url。
- 支持 async 驱动（aiosqlite / asyncpg / asyncmy）：通过 run_sync 执行迁移。
- VectorType 用 ``app.core.db.VectorType(dim)`` 渲染，迁移文件因此保持方言中立——
  同一个 revision 在 SQLite 上是 BLOB，在 PostgreSQL 上是 pgvector 的 vector(n)。
"""
from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

# 允许 `python -m alembic` 在 backend/ 下直接运行
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings  # noqa: E402
from app.core.db import Base, VectorType  # noqa: E402
import app.models  # noqa: E402,F401  确保 69 张表全部注册进 metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# 内部标记表，不属于 ORM 模型，迁移无关
_IGNORED_TABLES = {"schema_meta"}


def _database_url() -> str:
    url = config.attributes.get("url") or config.get_main_option("sqlalchemy.url")
    return (url or settings.database_url).strip()


def _include_object(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table" and name in _IGNORED_TABLES:
        return False
    return True


def render_item(type_, obj, autogen_context):
    """让 autogenerate 为自定义类型生成可导入的写法。"""
    if type_ == "type" and isinstance(obj, VectorType):
        autogen_context.imports.add("import app.core.db")
        return f"app.core.db.VectorType({obj.dim})"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        render_item=render_item,
        include_object=_include_object,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_item=render_item,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    url = _database_url()
    engine = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            is_pg = connection.dialect.name == "postgresql"
            if is_pg:
                # 多副本同时启动时串行化 DDL；会话级锁随连接关闭自动释放
                from sqlalchemy import text

                await connection.execute(text("SELECT pg_advisory_lock(1363169613)"))
            try:
                await connection.run_sync(_do_run_migrations)
            finally:
                if is_pg:
                    from sqlalchemy import text

                    await connection.execute(text("SELECT pg_advisory_unlock(1363169613)"))
            await connection.commit()
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    url = _database_url()
    if "+aiosqlite" in url or "+asyncpg" in url or "+asyncmy" in url:
        asyncio.run(_run_async_migrations())
        return

    from sqlalchemy import create_engine

    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            _do_run_migrations(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
