"""会话级串行锁（P0-5）：并发互斥、异常安全、条目自动回收。

背景：`_CONV_LOCKS` 早先只在模块级字典里惰性创建，配套的 `release_conv_lock`
却没有任何调用点——字典随会话数单调增长，长期运行会慢慢吃内存（每个会话一个
Lock 对象）。这里锁住三件事：锁仍然真的互斥、异常路径会释放、用完不留条目。
"""
from __future__ import annotations

import asyncio

import pytest


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_conv_lock_serializes_same_conversation():
    """同一会话的轮次必须串行（否则历史/计数竞态、上下文互相看不到）。"""
    async def _run2():
        from app.services.chat_service import conv_lock

        order: list[str] = []

        async def _turn(tag: str, delay: float):
            async with conv_lock(42):
                order.append(f"{tag}:enter")
                await asyncio.sleep(delay)
                order.append(f"{tag}:exit")

        await asyncio.gather(_turn("a", 0.05), _turn("b", 0.0))
        # 不允许出现交叉：enter 之后必须紧跟同一个任务的 exit
        assert order in (
            ["a:enter", "a:exit", "b:enter", "b:exit"],
            ["b:enter", "b:exit", "a:enter", "a:exit"],
        ), f"同会话未被串行: {order}"
    _run(_run2())


def test_conv_lock_allows_different_conversations_in_parallel():
    """不同会话不应互相阻塞——否则一个慢回答会挡住所有人。"""
    async def _run2():
        from app.services.chat_service import conv_lock

        started: list[str] = []
        release = asyncio.Event()

        async def _turn(tag: str, cid: int):
            async with conv_lock(cid):
                started.append(tag)
                await release.wait()

        t1 = asyncio.create_task(_turn("a", 1))
        t2 = asyncio.create_task(_turn("b", 2))
        # 两个不同会话都进入临界区后才会同时「已启动」
        for _ in range(50):
            if len(started) == 2:
                break
            await asyncio.sleep(0.01)
        assert sorted(started) == ["a", "b"], f"不同会话被互相阻塞: {started}"
        release.set()
        await asyncio.gather(t1, t2)
    _run(_run2())


def test_conv_lock_entries_are_reclaimed():
    """用完不留条目：否则字典随会话数无限增长（P0-5 的原始问题）。"""
    async def _run2():
        from app.services.chat_service import _CONV_LOCKS, conv_lock

        for cid in range(1000, 1200):
            async with conv_lock(cid):
                pass
        leaked = [cid for cid in range(1000, 1200) if cid in _CONV_LOCKS]
        assert leaked == [], f"会话锁条目泄漏: {len(leaked)} 个"
    _run(_run2())


def test_conv_lock_released_on_exception():
    """异常路径同样释放，且不留下条目。

    流式回答中途抛错（模型超时、用户断开）是最常见的退出路径；
    如果那时不释放，后续对该会话的请求会永久挂住。
    """
    async def _run2():
        from app.services.chat_service import _CONV_LOCKS, conv_lock

        with pytest.raises(RuntimeError):
            async with conv_lock(777):
                raise RuntimeError("boom")
        assert 777 not in _CONV_LOCKS, "异常路径未回收锁条目"

        # 锁仍可正常再取（没有被「锁死」）
        async with conv_lock(777):
            pass
    _run(_run2())


def test_conv_lock_waiter_keeps_entry_alive():
    """有等待者时不能回收条目：否则等待者与新来者会各拿一把锁，串行失效。

    时序：A 持锁 → B 排队 → A 退出。此时计数=1（B 已 +1），条目必须保留，
    B 拿到的是 A 用过的同一把锁；B 退出后才回收。
    """
    async def _run2():
        from app.services.chat_service import _CONV_LOCKS, conv_lock

        entered_b = asyncio.Event()

        async def _a():
            async with conv_lock(888):
                await asyncio.sleep(0.05)

        async def _b():
            async with conv_lock(888):
                entered_b.set()
                # 此刻 A 已退出，但条目应仍存在（B 还没退出）
                assert 888 in _CONV_LOCKS, "等待者占用的条目被提前回收"
                await asyncio.sleep(0.01)

        await asyncio.gather(_a(), _b())
        assert entered_b.is_set()
        assert 888 not in _CONV_LOCKS, "全部退出后应回收"
    _run(_run2())


def test_same_lock_object_shared_among_waiters():
    """并发进入时必须拿到同一个 Lock 对象（否则等于没有锁）。"""
    async def _run2():
        from app.services.chat_service import _CONV_LOCKS, conv_lock

        seen: list[int] = []
        release = asyncio.Event()

        async def _hold():
            async with conv_lock(555):
                seen.append(id(_CONV_LOCKS[555][0]))
                await release.wait()

        t = asyncio.create_task(_hold())
        await asyncio.sleep(0.02)
        # 第二个进入者（排队中）看到的对象必须是同一个
        waiter = asyncio.create_task(_hold())
        await asyncio.sleep(0.02)
        release.set()
        await asyncio.gather(t, waiter)
        assert len(set(seen)) == 1, "不同请求拿到了不同的锁对象"
    _run(_run2())
