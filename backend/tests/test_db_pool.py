"""SQLite 连接池策略测试（P0-1）。

覆盖四件事：
1. SQLite 池上限独立于 `db_pool_size`，且被夹在 1~3；
2. `db_busy_timeout` 真的落到 PRAGMA 上（之前写死 15000，改了也不生效）；
3. 池排满时抛出的 SQLAlchemy TimeoutError 被转成 503（可重试），
   而不是糊成 500（分不清「机器忙」和「代码炸了」）；
4. 流式回答期间不占连接（`release_connection`），否则池缩到 3 后
   第 4 个并发对话必然超时——池上限和「不持连接」是一对，缺一不可。
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text

import app.core.db as dbm
from app.core.config import settings


def _ev(coro):
    return asyncio.new_event_loop().run_until_complete(coro)



def test_sqlite_pool_size_is_independent_and_clamped(monkeypatch):
    """SQLite 用独立上限，不再跟着 db_pool_size 走；配大了也会被夹到 3。"""
    monkeypatch.setattr(settings, "db_pool_size", 20)
    monkeypatch.setattr(settings, "db_sqlite_pool_size", 3)
    kw = dbm._engine_kwargs_for("sqlite+aiosqlite:///./data/x.db")
    assert kw["pool_size"] == 3
    assert kw["max_overflow"] == 0

    monkeypatch.setattr(settings, "db_sqlite_pool_size", 50)
    assert dbm._engine_kwargs_for("sqlite+aiosqlite:///x.db")["pool_size"] == 3

    monkeypatch.setattr(settings, "db_sqlite_pool_size", 0)
    assert dbm._engine_kwargs_for("sqlite+aiosqlite:///x.db")["pool_size"] == 1


def test_pg_pool_keeps_its_own_budget(monkeypatch):
    """改 SQLite 上限不能顺手把 PG 的池改小——这是分开配置的全部理由。"""
    monkeypatch.setattr(settings, "db_pool_size", 20)
    monkeypatch.setattr(settings, "db_sqlite_pool_size", 1)
    kw = dbm._engine_kwargs_for("postgresql+asyncpg://u:p@localhost/db")
    assert kw["pool_size"] == 20
    assert kw["pool_pre_ping"] is True


def test_sqlite_busy_timeout_reaches_pragma(tmp_path, monkeypatch):
    """配置项必须落到 PRAGMA busy_timeout（毫秒），而不是被写死的 15000 盖掉。"""
    monkeypatch.setattr(settings, "db_busy_timeout", 7)
    url = f"sqlite+aiosqlite:///{(tmp_path / 'busy.db').as_posix()}"
    eng = dbm.make_engine(url)

    async def _read():
        async with eng.connect() as conn:
            return (await conn.execute(text("PRAGMA busy_timeout"))).scalar()

    try:
        assert _ev(_read()) == 7000
    finally:
        _ev(eng.dispose())


def test_pool_timeout_becomes_503(tmp_path, monkeypatch):
    """池占满 → 抛 SQLAlchemy TimeoutError → 处理器给 503 + Retry-After（而不是 500）。"""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'busy.db').as_posix()}"
    monkeypatch.setattr(settings, "db_sqlite_pool_size", 1)
    monkeypatch.setattr(settings, "db_pool_timeout", 1)

    from sqlalchemy.exc import TimeoutError as SATimeoutError

    from app.core.errors import db_timeout_error_handler

    class _Req:
        url = "http://test/api/v1/chat"

    eng = dbm.make_engine(url)

    async def _exhaust():
        # 占住唯一一条连接，第二次 connect 必然排队超时
        held = await eng.connect()
        try:
            try:
                second = await eng.connect()
            except SATimeoutError as e:
                return e
            else:  # pragma: no cover - 池没生效才会走到这里
                await second.close()
                raise AssertionError("池上限没有生效：应超时却拿到了第二条连接")
        finally:
            await held.close()
            await eng.dispose()

    exc = _ev(_exhaust())

    resp = _ev(db_timeout_error_handler(_Req(), exc))
    assert resp.status_code == 503
    body = resp.body.decode()
    assert "service_unavailable" in body and "db_pool_timeout" in body
    assert resp.headers.get("retry-after") == "1"


def test_release_connection_frees_the_pool_slot(tmp_path, monkeypatch):
    """流式前 release_connection 必须把连接还回池，否则池上限形同虚设。"""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'rel.db').as_posix()}"
    monkeypatch.setattr(settings, "db_sqlite_pool_size", 1)
    eng = dbm.make_engine(url)
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    S = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    async def _run():
        async with S() as s:
            await s.execute(text("select 1"))
            held = eng.pool.checkedout()
            await dbm.release_connection(s)
            freed = eng.pool.checkedout()
            # 释放后仍可用：再查一次不应失败（session 没被关掉）
            await s.execute(text("select 1"))
            again = eng.pool.checkedout()
        await eng.dispose()
        return held, freed, again

    held, freed, again = _ev(_run())
    assert held == 1, "查询应占住一条连接（否则这条断言无意义）"
    assert freed == 0, "release_connection 必须归还连接"
    assert again == 1, "归还后 session 仍可继续使用"
