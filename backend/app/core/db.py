"""数据库：async engine / session / Base，以及跨库向量类型 VectorType。

VectorType 设计：
- PostgreSQL: 使用原生 vector(n) 列（需 pgvector 扩展）
- SQLite / MySQL: 以 float32 二进制 BLOB 存储，检索在应用层用 numpy 计算
这样同一套 ORM 模型可在三种库间迁移，向量检索后端通过 settings 切换。
"""
from __future__ import annotations

import struct
from typing import Any

from sqlalchemy import Float, LargeBinary, TypeDecorator, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    pass


class VectorType(TypeDecorator):
    """跨数据库向量列。

    读取时统一返回 list[float]；写入接受 list[float]。
    """

    impl = LargeBinary
    cache_ok = True

    def __init__(self, dim: int | None = None, *args: Any, **kwargs: Any) -> None:
        self.dim = dim or settings.embedding_dim
        super().__init__(*args, **kwargs)

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql" and settings.resolved_vector_backend == "pgvector":
            try:
                from pgvector.sqlalchemy import Vector  # type: ignore

                return dialect.type_descriptor(Vector(self.dim))
            except ImportError:
                pass
        return dialect.type_descriptor(LargeBinary())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql" and settings.resolved_vector_backend == "pgvector":
            return value  # pgvector 自行处理
        # 其余库：float32 紧凑二进制
        return struct.pack(f"<{len(value)}f", *value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql" and settings.resolved_vector_backend == "pgvector":
            return list(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            b = bytes(value)
            n = len(b) // 4
            return list(struct.unpack(f"<{n}f", b))
        return value


# ---- engine / session ----
def _engine_kwargs_for(url: str) -> dict[str, Any]:
    kw: dict[str, Any] = {"echo": False, "future": True}
    if url.startswith("sqlite"):
        # timeout: 遇到锁时等待（秒），而非立即失败；SQLite 单写者，需容忍短暂争锁
        kw["connect_args"] = {"check_same_thread": False, "timeout": 15}
    else:
        kw.update(pool_pre_ping=True, pool_size=10, max_overflow=20)
    return kw


def _attach_sqlite_pragmas(engine, is_sqlite: bool) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_conn, _):  # noqa: ANN001
        if not is_sqlite:
            return
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=15000")
            cur.execute("PRAGMA synchronous=NORMAL")
        finally:
            cur.close()


def make_engine(url: str):
    """按 URL 创建 async engine（并挂 SQLite PRAGMA）。"""
    eng = create_async_engine(url, **_engine_kwargs_for(url))
    _attach_sqlite_pragmas(eng, url.startswith("sqlite"))
    return eng


async_engine = make_engine(settings.database_url)
AsyncSessionLocal = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


async def reinit_engine(url: str) -> None:
    """运行时重建全局 engine / sessionmaker（供切库兜底与启动期落地目标库）。

    注意：并发请求持有旧 session 时会有风险，仅在启动早期或切换后调用。
    """
    global async_engine, AsyncSessionLocal
    old = async_engine
    async_engine = make_engine(url)
    AsyncSessionLocal = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        await old.dispose()
    except Exception:  # noqa: BLE001
        pass


async def get_db() -> AsyncSession:
    """FastAPI 依赖：每请求一个 session。"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_models() -> None:
    """开发用：对主库建表 + 补列（生产用 alembic）。"""
    await init_models_for(async_engine)


async def init_models_for(engine) -> list[str]:
    """对指定 engine 建表 + 补列（供切库向导对目标库初始化复用）。返回表名列表。"""
    # 确保所有模型被导入注册
    import app.models  # noqa: F401

    async with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            from sqlalchemy import text

            try:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            except Exception:  # noqa: BLE001
                pass
        await conn.run_sync(Base.metadata.create_all)
        # create_all 不会给已存在的表加列：检测缺失列并 ALTER TABLE 补上
        await conn.run_sync(_sync_missing_columns)
    return [t.name for t in Base.metadata.sorted_tables]


# 历史遗留 NULL 回填只用扫描一次的标记（见 _sync_missing_columns）
_BACKFILL_MARKER_KEY = "legacy_null_backfill_done"


def _sync_missing_columns(sync_conn) -> None:
    """对比 ORM 定义与现有表结构，补齐缺失列（仅 SQLite/简单场景）。

    关键：ADD COLUMN 必须带上 DEFAULT，否则存量行新列为 NULL，会导致
    「列非空但值为 None」的响应校验 500（如 Document.kind）。

    性能约束：这里在**每次启动**都会跑。早期实现对每个非空且带标量默认值的列
    都发一次 `_`WHERE col IS NULL`_` 全表 UPDATE（本项目 69 张表共 242 条），
    无索引时是全表扫描，数据量一大启动就会越来越慢。现在改为：
    - 只在本次真的新建了列时回填该列（新列由 DDL 的 DEFAULT 覆盖，几乎不会命中）；
    - 老版本加列未带 DEFAULT 造成的遗留 NULL，用一次性标记控制，只扫一遍。
    """
    from sqlalchemy import inspect, text

    inspector = inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())
    added_columns: list = []

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name in cols:
                continue
            # 生成 ALTER TABLE ADD COLUMN（类型用 SQLite 兼容写法）
            coltype = col.type.compile(dialect=sync_conn.dialect)
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'
            # 标量默认值：让存量行立即有值，避免 NULL
            default_literal = _scalar_default(col)
            if default_literal is not None:
                ddl += f" DEFAULT {default_literal}"
            try:
                sync_conn.execute(text(ddl))
                added_columns.append(col)
            except Exception:  # noqa: BLE001
                pass  # 已有列 / 不支持的类型，忽略

    # 1) 本次新增的列：SQLite 的 ADD COLUMN ... DEFAULT 会给存量行填上默认值，
    #    这里只兜底处理「默认值不是标量」（当时 DDL 没带 DEFAULT）的情况。
    for col in added_columns:
        _backfill_nulls(sync_conn, col, text)

    # 2) 历史遗留：老版本加列未带 DEFAULT，存量行为 NULL。只扫一次。
    _backfill_legacy_nulls_once(sync_conn, text)


def _backfill_nulls(sync_conn, col, text_mod) -> None:
    """把某列的非空 NULL 行回填为标量默认值（可空列不动，避免误改）。"""
    if col.nullable or col.default is None:
        return
    default_literal = _scalar_default(col)
    if default_literal is None:
        return
    try:
        sync_conn.execute(
            text_mod(
                f'UPDATE "{col.table.name}" SET "{col.name}" = {default_literal} '
                f'WHERE "{col.name}" IS NULL'
            )
        )
    except Exception:  # noqa: BLE001
        pass


def _backfill_legacy_nulls_once(sync_conn, text_mod) -> None:
    """对老库做一次全量 NULL 回填，并用内部标记表记录，避免每次启动重复扫描。

    标记表刻意独立于 ORM 模型（`Base.metadata` 里没有它），因此不受迁移逻辑影响。
    """
    try:
        sync_conn.execute(
            text_mod(
                "CREATE TABLE IF NOT EXISTS schema_meta ("
                "  key TEXT PRIMARY KEY,"
                "  value TEXT"
                ")"
            )
        )
        done = sync_conn.execute(
            text_mod(f"SELECT value FROM schema_meta WHERE key = '{_BACKFILL_MARKER_KEY}'")
        ).scalar()
        if done:
            return
    except Exception:  # noqa: BLE001
        return

    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            _backfill_nulls(sync_conn, col, text_mod)

    try:
        sync_conn.execute(
            text_mod(
                "INSERT OR REPLACE INTO schema_meta (key, value) "
                f"VALUES ('{_BACKFILL_MARKER_KEY}', '1')"
            )
        )
    except Exception:  # noqa: BLE001
        pass


def _scalar_default(col) -> str | None:
    """把 ORM 列定义的标量默认值转成 SQL 字面量；非标量（函数/序列）返回 None。"""
    if col.default is None or not getattr(col.default, "is_scalar", False):
        return None
    raw = col.default.arg
    if isinstance(raw, bool):
        return "1" if raw else "0"
    if isinstance(raw, (int, float)):
        return str(raw)
    if isinstance(raw, str):
        return "'" + raw.replace("'", "''") + "'"
    return None
