"""启动期数据库准备：alembic 版本化迁移 + 老库自动 stamp。

为什么不用 ``Base.metadata.create_all()``：
- create_all 不会改已存在的表，只能靠 ``_sync_missing_columns`` 逐列 ALTER，
  没有版本记录、没有回滚路径，多进程同时启动还会互相抢 DDL 锁。
- 现在所有 schema 变更都走 alembic revision。``create_all`` 只在
  「老库首次接管」这一条路径上保留一次，之后不再参与。
  需要临时绕过迁移（只读副本、紧急排障）时设 ``RAG_SKIP_MIGRATIONS=1``。

线程模型：alembic 的 env.py 用 ``asyncio.run()`` 起自己的事件循环，不能直接在
已经运行的事件循环里调用。因此这里把迁移执行放进独立线程（``asyncio.to_thread``）。

并发：多个 worker/副本同时启动时，``upgrade`` 由数据库的版本表串行化；
SQLite 下额外尝试进程级文件锁（装了 portalocker 时生效）。
"""
from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

logger = get_logger("migrations")

_MIGRATION_LOCK = threading.Lock()


def alembic_root() -> Path | None:
    """定位 alembic 目录：源码运行 / 冻结运行都兼容。"""
    import sys

    candidates = [
        Path(__file__).resolve().parent.parent.parent,  # backend/
        Path(getattr(sys, "_MEIPASS", "") or "."),        # PyInstaller 解包目录
        Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path("."),
        Path.cwd(),
    ]
    for base in candidates:
        try:
            ini = base / "alembic.ini"
            d = base / "alembic"
            if ini.is_file() and (d / "env.py").is_file():
                return base
        except OSError:
            continue
    return None


def migrations_enabled() -> bool:
    """是否允许启动期自动迁移。

    两个开关任一关闭即跳过：环境变量 RAG_SKIP_MIGRATIONS（排障用）
    与 settings.db_auto_migrate（Web 端系统设置）。
    """
    if os.environ.get("RAG_SKIP_MIGRATIONS", "").strip() in ("1", "true", "True"):
        return False
    try:
        from app.core.config import settings

        return bool(getattr(settings, "db_auto_migrate", True))
    except Exception:  # noqa: BLE001
        return True


def _alembic_config(url: str):
    from alembic.config import Config

    root = alembic_root()
    if root is None:
        raise FileNotFoundError("找不到 alembic.ini / alembic/env.py")
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.attributes["url"] = url
    return cfg


def _acquire_file_lock(url: str):
    """SQLite 下跨进程串行化 DDL。

    用标准库实现（Windows: msvcrt，POSIX: fcntl），不引入额外依赖。
    拿不到锁只降级为「无锁」而不是失败——DDL 本身仍会被 SQLite 的写锁串行化。
    """
    if not url.startswith("sqlite"):
        return None
    try:
        from app.core.config import DATA_DIR

        fh = open(DATA_DIR / ".migrate.lock", "a+b")  # noqa: SIM115
    except Exception:  # noqa: BLE001
        return None
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        return fh
    except Exception:  # noqa: BLE001
        try:
            fh.close()
        except Exception:  # noqa: BLE001
            pass
        return None


def _release_file_lock(fh) -> None:
    if fh is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:  # noqa: BLE001
        pass
    try:
        fh.close()
    except Exception:  # noqa: BLE001
        pass


def _run_alembic_sync(url: str, *, stamp_revision: str | None = None) -> None:
    """在独立线程里执行 alembic（upgrade head 或 stamp）。"""
    from alembic import command

    cfg = _alembic_config(url)
    if stamp_revision:
        command.stamp(cfg, stamp_revision, purge=True)
    else:
        command.upgrade(cfg, "head")


async def _detect_state(engine) -> dict:
    from sqlalchemy import inspect

    async with engine.connect() as conn:
        return await conn.run_sync(
            lambda sc: {
                "has_version": "alembic_version" in set(inspect(sc).get_table_names()),
                "tables": set(inspect(sc).get_table_names()),
            }
        )


async def current_revision(engine) -> str | None:
    from sqlalchemy import text

    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text("SELECT version_num FROM alembic_version"))).first()
            return row[0] if row else None
    except Exception:  # noqa: BLE001
        return None


async def _adopt_legacy_schema(engine, url: str) -> None:
    """老库（有表、无 alembic_version）接管：建缺表 → 补缺列 → stamp 到 head。

    create_all 在这里只负责「补齐整张缺失的表」，之后的 schema 变更全部由
    revision 承担，因此不会再出现「升级靠补列」的状态。
    """
    from app.core.db import Base, _sync_missing_columns

    import app.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_sync_missing_columns)

    head = await asyncio.to_thread(_head_revision, url)
    await asyncio.to_thread(_run_alembic_sync, url, stamp_revision=head)
    logger.info("legacy_schema_adopted", revision=head)


def _head_revision(url: str) -> str:
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(_alembic_config(url)).get_current_head()
    if not head:
        raise RuntimeError("没有可用的 alembic revision")
    return head


async def prepare_database(engine=None, *, url: str | None = None) -> dict:
    """把数据库准备到最新 schema。幂等，供启动期与切库流程调用。

    返回 ``{"action": created|upgraded|adopted|skipped, "revision": str|None}``。
    """
    from app.core.config import settings
    from app.core.db import Base, async_engine

    engine = engine or async_engine
    url = url or settings.database_url

    if not migrations_enabled():
        logger.warning("migrations_skipped_by_env")
        return {"action": "skipped", "revision": await current_revision(engine)}

    if alembic_root() is None:
        # 打包遗漏等极端情况：保证服务仍能启动，但要显式告警
        logger.error("alembic_missing_fallback_create_all")
        import app.models  # noqa: F401

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return {"action": "create_all", "revision": None}

    lock_fh = _acquire_file_lock(url)
    try:
        if not _MIGRATION_LOCK.acquire(timeout=180):
            raise RuntimeError("等待迁移锁超时（其他进程仍在迁移）")
        try:
            state = await _detect_state(engine)
            if state["has_version"]:
                await asyncio.to_thread(_run_alembic_sync, url)
                action = "upgraded"
            elif not state["tables"] - {"sqlite_sequence"}:
                await asyncio.to_thread(_run_alembic_sync, url)
                action = "created"
            else:
                logger.warning("legacy_schema_detected", tables=len(state["tables"]))
                await _adopt_legacy_schema(engine, url)
                # 接管后继续跑一次，应用基线之后可能追加的 revision
                await asyncio.to_thread(_run_alembic_sync, url)
                action = "adopted"

            revision = await current_revision(engine)
            logger.info("schema_ready", action=action, revision=revision)
            return {"action": action, "revision": revision}
        finally:
            _MIGRATION_LOCK.release()
    finally:
        _release_file_lock(lock_fh)
