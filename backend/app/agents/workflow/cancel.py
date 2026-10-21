"""运行中的工作流任务注册表：支持从外部取消。

为什么需要独立注册表：
- 工作流跑在后台 task 里（stream_workflow / run_workflow_for_channel / webhook）
- 用户点「停止」时要能立刻中止，而不是等它跑完
- 单个进程内的内存表足够（多副本场景下取消只能命中执行该任务的那个副本，
  这是可接受的：反正任务本身也是单副本执行的）

设计要点：
- 用 run_id 作键，进程内唯一
- cancel() 只发 asyncio 取消信号，状态落库由调用方的 finally 负责
- 注册/注销必须成对，用 context manager 避免泄漏
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from app.core.logging import get_logger

logger = get_logger("workflow.cancel")

# run_id → 执行该运行的 asyncio.Task
_RUNNING: dict[int, asyncio.Task] = {}


def register(run_id: int, task: asyncio.Task) -> None:
    _RUNNING[run_id] = task


def unregister(run_id: int) -> None:
    _RUNNING.pop(run_id, None)


def is_running(run_id: int) -> bool:
    t = _RUNNING.get(run_id)
    return bool(t and not t.done())


def running_run_ids() -> list[int]:
    return [rid for rid, t in _RUNNING.items() if not t.done()]


def cancel(run_id: int) -> bool:
    """请求取消。返回是否找到并发出取消信号。"""
    task = _RUNNING.get(run_id)
    if not task or task.done():
        return False
    task.cancel()
    logger.info("workflow_cancel_requested", run_id=run_id)
    return True


@asynccontextmanager
async def track(run_id: int) -> AsyncIterator[None]:
    """把当前 task 登记为「正在执行 run_id」，退出时自动注销。"""
    task = asyncio.current_task()
    if task is not None:
        register(run_id, task)
    try:
        yield
    finally:
        unregister(run_id)
