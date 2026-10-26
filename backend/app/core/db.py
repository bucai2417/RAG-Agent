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

    PostgreSQL 上如果选了 pgvector 但没装 pgvector 包，**不能**静默退化成
    BYTEA —— 那样列类型和 HNSW 索引都会错，而且要到第一次向量检索才暴露。
    这里显式抛错，让部署问题在启动期就暴露。
    """

    impl = LargeBinary
    cache_ok = True

    def __init__(self, dim: int | None = None, *args: Any, **kwargs: Any) -> None:
        self.dim = dim or settings.embedding_dim
        super().__init__(*args, **kwargs)

    def _use_pgvector(self, dialect) -> bool:
        return dialect.name == "postgresql" and settings.resolved_vector_backend == "pgvector"

    def load_dialect_impl(self, dialect):
        if self._use_pgvector(dialect):
            try:
                from pgvector.sqlalchemy import Vector  # type: ignore
            except ImportError as e:  # pragma: no cover - 部署配置错误
                raise RuntimeError(
                    "向量后端为 pgvector，但未安装 pgvector 包。"
                    "请执行 pip install pgvector asyncpg，"
                    "或把 VECTOR_BACKEND 设为 numpy（应用层检索）。"
                ) from e
            return dialect.type_descriptor(Vector(self.dim))
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
        # SQLite 只有单写者：池开大只会让更多连接排队撞写锁，反而放大
        # "database is locked"。这里刻意收窄，把并发压力交给应用层的排队。
        # 上限独立于 db_pool_size：后者是 PG/MySQL 的连接预算（20 量级），
        # 两库共用必然有一边配错。取值夹在 1~3，配大了也只会误伤自己。
        kw["connect_args"] = {
            "check_same_thread": False,
            "timeout": settings.db_busy_timeout,
        }
        kw.update(
            pool_size=max(1, min(3, settings.db_sqlite_pool_size)),
            max_overflow=0,
            # 池被占满时的排队上限，与 PG 共用同一个配置；超时会被转成 503 +
            # Retry-After（见 app.core.errors.db_timeout_error_handler），
            # 而不是静默挂起或变成看不出原因的 500。
            pool_timeout=settings.db_pool_timeout,
        )
    else:
        kw.update(
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_recycle=settings.db_pool_recycle,
            pool_timeout=settings.db_pool_timeout,
        )
        # asyncpg 默认把每个参数都当预编译语句缓存键，PG 侧建议 statement_cache_size=0
        # 配合 PgBouncer 之类的连接池中间件时更稳（这里仅在使用 pgbouncer 时开启）
        if settings.db_disable_asyncpg_stmt_cache and "asyncpg" in url:
            kw["connect_args"] = {"statement_cache_size": 0}
    return kw


def _attach_sqlite_pragmas(engine, is_sqlite: bool) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_conn, _):  # noqa: ANN001
        if not is_sqlite:
            return
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            # 用配置值而不是写死 15000：这里会覆盖 connect_args 的 timeout，
            # 写死的话「系统设置 → SQLite 写锁等待」改了也不生效。
            cur.execute(f"PRAGMA busy_timeout={max(0, int(settings.db_busy_timeout * 1000))}")
            cur.execute("PRAGMA synchronous=NORMAL")
        finally:
            cur.close()


def make_engine(url: str):
    """按 URL 创建 async engine（并挂 SQLite PRAGMA）。"""
    eng = create_async_engine(url, **_engine_kwargs_for(url))
    _attach_sqlite_pragmas(eng, url.startswith("sqlite"))
    return eng


class AppSession(AsyncSession):
    """把乐观锁冲突（StaleDataError）统一转成 409 业务异常。

    为什么不挂在 engine 的 handle_error 上：UPDATE 影响 0 行在 DBAPI 层算成功，
    StaleDataError 是 ORM 在 flush 末尾自己判定的，不经过 handle_error。
    因此在 flush/commit 上转换，覆盖 endpoint 的显式 ``await db.commit()``
    与 ``get_db`` 依赖的隐式提交两条路径。
    """

    async def flush(self, objects=None):  # noqa: ANN001
        from sqlalchemy.orm.exc import StaleDataError

        from app.core.errors import StaleWriteError

        try:
            return await super().flush(objects)
        except StaleDataError as e:
            raise StaleWriteError(
                "内容已被他人修改，请刷新后重试",
                detail={"reason": "stale_write"},
            ) from e

    async def commit(self) -> None:
        # 先显式 flush，才能在 commit 抛 StaleDataError 前完成转换
        await self.flush()
        await super().commit()


async_engine = make_engine(settings.database_url)
current_database_url: str = settings.database_url
AsyncSessionLocal = async_sessionmaker(
    async_engine, class_=AppSession, expire_on_commit=False
)


async def reinit_engine(url: str) -> None:
    """运行时重建全局 engine / sessionmaker（供切库兜底与启动期落地目标库）。

    注意：并发请求持有旧 session 时会有风险，仅在启动早期或切换后调用。
    """
    global async_engine, AsyncSessionLocal, current_database_url
    old = async_engine
    async_engine = make_engine(url)
    AsyncSessionLocal = async_sessionmaker(
        async_engine, class_=AppSession, expire_on_commit=False
    )
    current_database_url = url
    try:
        await old.dispose()
    except Exception:  # noqa: BLE001
        pass


async def sync_engine_with_settings() -> bool:
    """若 settings.database_url 与当前 engine 不一致，则重建 engine。

    为什么需要：engine 在模块导入时创建，而「系统设置」里的 database_url
    是在启动早期（apply_overrides）才叠加进来的。没有这一步，
    在 Web 端切库并重启后，进程会连回旧库，切库只生效一次。
    """
    target = (settings.database_url or "").strip()
    if not target or target == current_database_url:
        return False
    await reinit_engine(target)
    return True


async def get_db() -> AsyncSession:
    """FastAPI 依赖：每请求一个 session。"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def release_connection(session: AsyncSession) -> None:
    """把 session 当前占用的连接还给池（提交但保留 session 可用）。

    为什么需要：AsyncSession 一执行 SELECT 就开事务并占住一条连接，直到 commit / close。
    流式回答要等模型几十秒到几分钟，这段等待期连接是空占的——池缩到 3（SQLite 单写者）
    之后，第 4 个并发对话会在 pool_timeout 后拿到 503。所以在「调用 LLM / 跑外部工具」
    这类长时间等待之前，先把连接还回去，用完再取。

    只在该次请求真的开过事务时提交，避免给只读请求引入多余的空事务。
    """
    if session.in_transaction():
        await session.commit()


async def init_models() -> None:
    """把数据库准备到最新 schema（走 alembic，见 app/core/migrations.py）。

    保留这个函数名是为了兼容既有调用点（bootstrap / scripts / 测试）。
    """
    from app.core.migrations import prepare_database

    await prepare_database(async_engine)


async def init_models_for(engine) -> list[str]:
    """对指定 engine 迁移到最新 schema（供切库向导对目标库初始化复用）。返回表名列表。"""
    from app.core.migrations import prepare_database

    if engine.url.render_as_string(hide_password=False) != settings.database_url:
        await prepare_database(engine, url=engine.url.render_as_string(hide_password=False))
    else:
        await prepare_database(engine)
    return [t.name for t in Base.metadata.sorted_tables]


# 历史遗留 NULL 回填只用扫描一次的标记（见 _sync_missing_columns）
_BACKFILL_MARKER_KEY = "legacy_null_backfill_done"


def _sync_missing_columns(sync_conn) -> None:
    """对比 ORM 定义与现有表结构，补齐缺失列。

    这条路径只服务「老库首次被 alembic 接管」（见 migrations._adopt_legacy_schema），
    不再出现在正常启动流程里 —— 正常升级由 revision 完成。

    之所以仍然保留：老用户升级到带 alembic 的版本时，库里没有 alembic_version，
    必须先让结构与 ORM 对齐才能 stamp，否则后续 revision 会因缺列而炸。

    方言注意事项：
    - 布尔默认值在 SQLite 用 1/0，在 PostgreSQL 必须用 TRUE/FALSE；
    - ``INSERT OR REPLACE`` 是 SQLite 语法，PG/MySQL 用 ON CONFLICT / ON DUPLICATE KEY；
    - 向量列在 PG 上是 vector(n)，ALTER 前需要扩展已就绪（由迁移负责建）。
    """
    from sqlalchemy import inspect, text

    dialect = sync_conn.dialect.name
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
            coltype = col.type.compile(dialect=sync_conn.dialect)
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'
            default_literal = _scalar_default(col, dialect)
            if default_literal is not None:
                # PG/MySQL 的 ADD COLUMN ... NOT NULL DEFAULT 会重写全表，
                # 但这些都是小表补列，可以接受；SQLite 只更新元数据。
                ddl += f" DEFAULT {default_literal}"
            try:
                sync_conn.execute(text(ddl))
                added_columns.append(col)
            except Exception:  # noqa: BLE001
                pass  # 已有列 / 不支持的类型（如无扩展的 vector），交给迁移处理

    # 1) 本次新增的列：SQLite 的 ADD COLUMN ... DEFAULT 会给存量行填上默认值，
    #    这里只兜底处理「默认值不是标量」的情况。
    for col in added_columns:
        _backfill_nulls(sync_conn, col, text, dialect)

    # 2) 历史遗留：老版本加列未带 DEFAULT，存量行为 NULL。只扫一次。
    _backfill_legacy_nulls_once(sync_conn, text, dialect)


# MySQL 没有部分索引，谓词会被丢弃。凡是「唯一性依赖谓词」的索引，
# 在 MySQL 上都会退化成无谓词的全表唯一约束，语义完全不同，必须跳过。
# 目前只有「每租户一个默认大群」属于这一类：去掉谓词就变成「每租户只能有一个房间」。
_MYSQL_UNSAFE_INDEXES = {"uq_chat_room_tenant_default"}


def sync_missing_indexes(sync_conn) -> dict:
    """为**已存在**的表补建 ORM 里声明的索引，返回 ``{"created": [...], "skipped": [...]}``。

    ``create_all`` 只在建表时顺带建索引，对已存在的表一概不管；而 alembic 的
    老库接管路径是「补表 + 补列 + stamp 到 head」——stamp 跳过了所有历史 revision，
    于是**模型后来新增的索引永远不会落到老库上**。

    后果已经在真实库里出现过：``chat_room`` 的两条唯一索引在 ``alembic_version``
    已是 head 的库上根本不存在，P0-4 的并发修复因此完全失效（约束是最终判据，
    缺了它就是「先查后建」裸奔）；``webhook_token`` 的路径唯一索引、
    ``tenant`` 的邀请码唯一索引同理——它们挡的是重复数据，不是性能。

    这里只处理 ``Index`` 对象（含部分索引）。表级 ``UniqueConstraint`` 在 SQLite
    上只能靠重建表来加，属于 revision 的职责，不在本函数范围内。

    ``skipped`` 收录建不出来的名字（存量数据重复、方言不支持等）。
    唯一约束撞上存量重复数据时**不能**静默成功：调用方要把它记进日志，
    让运维看到「这条约束仍然没建上，需要先清理数据」。
    """
    from sqlalchemy import inspect
    from sqlalchemy.exc import IntegrityError

    inspector = inspect(sync_conn)
    dialect = sync_conn.dialect.name
    existing_tables = set(inspector.get_table_names())
    created: list[str] = []
    skipped: list[str] = []

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        have = {ix["name"] for ix in inspector.get_indexes(table.name)}
        for idx in table.indexes:
            name = idx.name or f"ix_{table.name}_?"
            if name in have:
                continue
            if dialect == "mysql" and name in _MYSQL_UNSAFE_INDEXES:
                skipped.append(name)
                continue
            try:
                idx.create(sync_conn, checkfirst=True)
                created.append(name)
            except IntegrityError:
                # 存量重复数据：约束建不上。数据清理属于人工决策（归档 vs 合并），
                # 这里只报告，不替用户改业务数据。
                skipped.append(name)
            except Exception:  # noqa: BLE001
                skipped.append(name)
    return {"created": created, "skipped": skipped}


def _backfill_nulls(sync_conn, col, text_mod, dialect: str = "sqlite") -> None:
    """把某列的非空 NULL 行回填为标量默认值（可空列不动，避免误改）。"""
    if col.nullable or col.default is None:
        return
    default_literal = _scalar_default(col, dialect)
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


def _backfill_legacy_nulls_once(sync_conn, text_mod, dialect: str = "sqlite") -> None:
    """对老库做一次全量 NULL 回填，并用内部标记表记录，避免每次启动重复扫描。

    标记表刻意独立于 ORM 模型（`Base.metadata` 里没有它），因此不受迁移逻辑影响。
    """
    try:
        sync_conn.execute(
            text_mod(
                "CREATE TABLE IF NOT EXISTS schema_meta ("
                "  key VARCHAR(64) PRIMARY KEY,"
                "  value VARCHAR(255)"
                ")"
            )
        )
        done = sync_conn.execute(
            text_mod(
                "SELECT value FROM schema_meta WHERE key = :k"
            ),
            {"k": _BACKFILL_MARKER_KEY},
        ).scalar()
        if done:
            return
    except Exception:  # noqa: BLE001
        return

    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            _backfill_nulls(sync_conn, col, text_mod, dialect)

    try:
        if dialect == "sqlite":
            sync_conn.execute(
                text_mod(
                    "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (:k, :v)"
                ),
                {"k": _BACKFILL_MARKER_KEY, "v": "1"},
            )
        elif dialect == "mysql":
            sync_conn.execute(
                text_mod(
                    "INSERT INTO schema_meta (key, value) VALUES (:k, :v) "
                    "ON DUPLICATE KEY UPDATE value = :v"
                ),
                {"k": _BACKFILL_MARKER_KEY, "v": "1"},
            )
        else:
            sync_conn.execute(
                text_mod(
                    "INSERT INTO schema_meta (key, value) VALUES (:k, :v) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
                ),
                {"k": _BACKFILL_MARKER_KEY, "v": "1"},
            )
    except Exception:  # noqa: BLE001
        pass


def _scalar_default(col, dialect: str = "sqlite") -> str | None:
    """把 ORM 列定义的标量默认值转成当前方言的 SQL 字面量。

    非标量（函数/序列）返回 None —— 这类列由迁移显式处理。
    布尔值必须按方言渲染：SQLite 没有布尔类型，1/0 才通用；
    PG 虽然接受 1/0 隐式转换，但写成 TRUE/FALSE 才不会被当成整数默认值。
    """
    if col.default is None or not getattr(col.default, "is_scalar", False):
        return None
    raw = col.default.arg
    if isinstance(raw, bool):
        if dialect == "sqlite":
            return "1" if raw else "0"
        return "TRUE" if raw else "FALSE"
    if isinstance(raw, (int, float)):
        return str(raw)
    if isinstance(raw, str):
        return "'" + raw.replace("'", "''") + "'"
    return None
