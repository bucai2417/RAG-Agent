"""后台：扫描到期的 Webhook 回调投递记录并重试。

为什么用轮询而不是「失败时 sleep 后重试」：
- 进程重启 / 滚动发布时，sleep 中的协程会丢，投递记录不会
- 轮询让重试对多副本也安全（谁扫到谁投，靠 next_attempt_at 抢占）

间隔 15 秒：回调退避最短是 5 秒，15 秒足够及时又不会空转太频繁。
"""
from __future__ import annotations

from app.core.logging import get_logger

logger = get_logger("callback_poll")


async def flush_once() -> dict:
    """处理一轮到期投递。返回 {sent, retry, dead}。"""
    from app.services.workflow_callback_service import flush_ready

    try:
        stats = await flush_ready(limit=50)
        if stats["sent"] or stats["dead"]:
            logger.info("callback_flush_done", **stats)
        return stats
    except Exception:  # noqa: BLE001
        logger.exception("callback_flush_failed")
        return {"sent": 0, "retry": 0, "dead": 0}


async def periodic_callback_flush(interval_seconds: int = 15) -> None:
    """后台循环：定期重投失败的回调。interval<=0 关闭。"""
    import asyncio

    if interval_seconds <= 0:
        return
    while True:
        try:
            await flush_once()
        except Exception:  # noqa: BLE001
            logger.exception("callback_flush_loop_failed")
        await asyncio.sleep(interval_seconds)
