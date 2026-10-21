"""后台任务守护：统一管理散落的 fire-and-forget asyncio 任务。

为什么需要它（实测过的坑）：
1. **GC 回收**：`asyncio.create_task()` 的返回值若无强引用，事件循环只持弱引用，
   任务可能在执行中被垃圾回收，静默消失。
2. **异常静默**：任务内未捕获的异常只会打到 asyncio 的默认 handler，无人汇聚。
3. **退出不收敛**：进程退出时这些任务不被取消/等待，可能留下半写状态。

用法：把 `asyncio.create_task(coro)` 换成 `spawn(name, coro)`。
"""
from __future__ import annotations

import asyncio
from typing import Coroutine

from app.core.logging import get_logger

logger = get_logger("tasks.bg")

# 持强引用，防 GC
_TASKS: set[asyncio.Task] = set()
# 关键任务：外部对接的异步回调等，退出时应优先让其完成而不是立刻取消。
# 用任务名前缀标记（避免引入额外的元数据结构）。
_CRITICAL_PREFIXES: tuple[str, ...] = ("hook_",)


def _is_critical(t: asyncio.Task) -> bool:
    name = t.get_name() or ""
    return any(name.startswith(p) for p in _CRITICAL_PREFIXES)


def spawn(name: str, coro: Coroutine) -> asyncio.Task:
    """起一个受管后台任务：持强引用 + 完成后自动汇出异常、从集合移除。"""
    task = asyncio.ensure_future(coro)
    _TASKS.add(task)
    task.set_name(name)

    def _done(t: asyncio.Task) -> None:
        _TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.exception("bg_task_failed", task=name, error=str(exc)[:300])

    task.add_done_callback(_done)
    return task


def pending_count() -> int:
    return len(_TASKS)


async def shutdown(timeout: float = 5.0) -> None:
    """退出时：取消所有在途后台任务并等待（有超时兜底，避免卡死退出）。

    关键任务（外部对接的异步回调）先给一小段宽限期让其自然完成，
    再统一取消 —— 否则一次重启就会让对接方永远等不到回调。
    """
    if not _TASKS:
        return
    logger.info("bg_tasks_cancel", count=len(_TASKS))

    tasks = list(_TASKS)
    critical = [t for t in tasks if _is_critical(t) and not t.done()]
    others = [t for t in tasks if t not in critical]

    # 关键任务：先等一会儿（最多 timeout），能跑完的就跑完
    if critical:
        try:
            await asyncio.wait_for(
                asyncio.gather(*critical, return_exceptions=True), timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.warning("bg_critical_shutdown_timeout", count=len(critical))

    # 其余任务及仍未完成的关键任务：取消
    remaining = [t for t in tasks if not t.done()]
    for t in remaining:
        t.cancel()
    if remaining:
        try:
            await asyncio.wait_for(
                asyncio.gather(*remaining, return_exceptions=True), timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.warning("bg_tasks_shutdown_timeout", count=len(remaining))
    _TASKS.clear()
