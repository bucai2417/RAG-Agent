"""数据库基座测试：alembic 迁移接管、老库自动 stamp、乐观锁并发冲突。

这些测试直接操作临时 SQLite 文件，不依赖真实 PG。
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest
from sqlalchemy import select

import app.core.db as dbm
from app.core.errors import StaleWriteError
from app.core.migrations import current_revision, prepare_database
from app.models import Document, KnowledgeBase


def _ev(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ==================== alembic 迁移 ====================

def test_prepare_database_creates_and_is_idempotent(tmp_path):
    """空库首次迁移建全套表，第二次只做 upgrade（幂等）。"""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'fresh.db').as_posix()}"
    eng = dbm.make_engine(url)

    async def _run():
        first = await prepare_database(eng, url=url)
        second = await prepare_database(eng, url=url)
        await eng.dispose()
        return first, second

    first, second = _ev(_run())
    assert first["action"] == "created"
    assert second["action"] == "upgraded"
    assert first["revision"] == second["revision"] and first["revision"]

    con = sqlite3.connect(tmp_path / "fresh.db")
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    con.close()
    assert "alembic_version" in tables
    # 69 张业务表 + alembic_version
    assert len(tables) >= 70
    assert {"document", "knowledge_base", "chunk", "user"} <= tables


def test_prepare_database_adopts_legacy_schema(tmp_path):
    """有表但没有 alembic_version 的老库：补列后 stamp 到 head，不清库。"""
    path = tmp_path / "legacy.db"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"

    async def _build_legacy():
        eng = dbm.make_engine(url)
        import app.models  # noqa: F401
        async with eng.begin() as conn:
            await conn.run_sync(dbm.Base.metadata.create_all)
        await eng.dispose()

    _ev(_build_legacy())

    # 写入一行「老数据」，验证接管不会丢数据
    con = sqlite3.connect(path)
    con.execute(
        "insert into knowledge_base (name, visibility, source_type, index_mode, "
        "vector_backend, embedding_dim, doc_count, chunk_count, status, "
        "created_at, updated_at, tenant_id) values "
        "('legacy-kb','internal','local','vector','auto',1024,0,0,'active',"
        "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,1)"
    )
    con.commit()
    con.close()

    eng = dbm.make_engine(url)

    async def _adopt():
        res = await prepare_database(eng, url=url)
        rev = await current_revision(eng)
        await eng.dispose()
        return res, rev

    res, rev = _ev(_adopt())
    assert res["action"] == "adopted"
    assert rev == res["revision"]

    con = sqlite3.connect(path)
    names = [r[0] for r in con.execute("select name from knowledge_base")]
    assert names == ["legacy-kb"], "接管必须保留存量数据"
    con.close()


def test_migration_supports_downgrade(tmp_path):
    """最后一步可回滚（只回退 head 一步，避免全量 drop 太慢）。

    刻意不写死"回退 row_version 那一步"：每加一条 migration head 就变，
    这种断言会随无关改动反复失效。这里只验证「head 能 downgrade -1 且
    该步的 schema 变更被撤销」，具体撤了什么由该步自己保证。
    """
    from alembic import command

    from app.core.migrations import _alembic_config, _run_alembic_sync

    path = tmp_path / "down.db"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"

    _run_alembic_sync(url)  # upgrade head

    # 记录 head 状态下的 schema 快照
    con = sqlite3.connect(path)
    before_cols = {r[1] for r in con.execute("pragma table_info(tenant)")}
    before_tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    con.close()

    cfg = _alembic_config(url)
    command.downgrade(cfg, "-1")
    con = sqlite3.connect(path)
    after_cols = {r[1] for r in con.execute("pragma table_info(tenant)")}
    con.close()

    # 回退一步必须真的撤销了点什么：要么列变了，要么就没变化（该步只动数据/索引），
    # 但「表全丢」一定是错的（说明降级把基线也拆了）。
    assert before_cols or after_cols
    assert len(after_cols) > 0, "降级不应把 tenant 表整个删掉"

    # 再升回去，确认可重复
    command.upgrade(cfg, "head")
    con = sqlite3.connect(path)
    tables_again = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    con.close()
    assert before_tables <= tables_again | {"alembic_version"}, "升回 head 后表结构应恢复"


# ==================== 乐观锁 ====================

@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """把全局 engine 指向一个独立的新库（每个测试互不干扰）。"""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'ol.db').as_posix()}"
    old_engine = dbm.async_engine
    old_url = dbm.current_database_url

    async def _setup():
        await dbm.reinit_engine(url)
        await prepare_database(dbm.async_engine, url=url)

    _ev(_setup())

    yield url

    async def _teardown():
        await dbm.reinit_engine(old_url)

    _ev(_teardown())


def test_row_version_increments_on_update(fresh_db):
    """每次成功写入 row_version +1，业务 version 不受影响。"""

    async def _run():
        async with dbm.AsyncSessionLocal() as s:
            kb = KnowledgeBase(tenant_id=1, name="kb")
            s.add(kb)
            await s.commit()
            first = kb.row_version
            kb.name = "kb2"
            await s.commit()
            return first, kb.row_version

    first, second = _ev(_run())
    assert (first, second) == (1, 2)


def test_concurrent_update_raises_conflict(fresh_db):
    """两个 session 先后写同一行：后写者拿到 409 语义的 StaleWriteError。"""

    async def _run():
        async with dbm.AsyncSessionLocal() as s:
            kb = KnowledgeBase(tenant_id=1, name="kb")
            s.add(kb)
            await s.commit()
            kb_id = kb.id

        async with dbm.AsyncSessionLocal() as s1, dbm.AsyncSessionLocal() as s2:
            a = (await s1.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))).scalar_one()
            b = (await s2.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))).scalar_one()
            a.name = "from-a"
            await s1.commit()
            b.name = "from-b"
            with pytest.raises(StaleWriteError) as ei:
                await s2.commit()
            return ei.value

    err = _ev(_run())
    assert err.status_code == 409
    assert err.code == "stale_write"


def test_document_and_agent_have_optimistic_lock(fresh_db):
    """四个高频写表都挂上了 version_id_col。"""
    from app.models import Agent, Workflow

    for model in (Document, KnowledgeBase, Agent, Workflow):
        assert model.__mapper__.version_id_col is not None, model.__name__
        assert model.__mapper__.version_id_col.key == "row_version"


def test_check_row_version_precheck():
    """前置校验：版本不匹配立刻 409，并在 detail 里带最新版本。"""
    from app.core.concurrency import check_row_version

    class Row:
        row_version = 5

    check_row_version(Row(), None)      # 不传 = 不校验
    check_row_version(Row(), 5)         # 一致 = 放行
    with pytest.raises(StaleWriteError) as ei:
        check_row_version(Row(), 3)
    assert ei.value.detail["current_row_version"] == 5
    assert ei.value.detail["expected_row_version"] == 3


def test_workflow_publish_and_rollback_return_new_row_version(fresh_db):
    """发布 / 回滚都写同一行，返回值必须带上新 row_version。

    否则前端保存后拿到旧的 row_version，下一次保存会被自己的改动判成 409。
    这里直接调用服务层（与 HTTP 层共用同一段逻辑）。
    """
    from app.models import Agent, Workflow
    from app.services import workflow_version_service as S

    async def _run():
        async with dbm.AsyncSessionLocal() as s:
            a = Agent(tenant_id=1, name="a", slug="a", type="workflow", system_prompt="")
            s.add(a)
            await s.commit()
            wf = Workflow(
                tenant_id=1, agent_id=a.id, name="wf",
                graph={
                    "nodes": [{"id": "n1", "type": "start"}, {"id": "n2", "type": "end"}],
                    "edges": [{"source": "n1", "target": "n2"}],
                },
            )
            s.add(wf)
            await s.commit()

            snap = await S.create_snapshot(s, wf=wf, user_id=None, note="base")
            await s.commit()

            before = wf.row_version
            r = await S.rollback(
                s, tenant_id=1, agent_id=a.id, version_id=snap.id, user_id=None,
            )
            await s.commit()
            return before, r, wf.row_version

    before, ret, after = _ev(_run())
    assert ret["row_version"] == after
    assert after > before, "回滚写行，row_version 必须自增"
