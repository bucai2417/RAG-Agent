"""渠道运行时管理器：随应用生命周期启动/停止所有启用的渠道。

每个渠道跑在独立 task 上（connect 内部自带重连循环），单渠道异常不影响其它。
"""
from __future__ import annotations

import asyncio

from app.channels.base import InboundMessage
from app.channels.registry import build_adapter
from app.core.errors import ValidationError
from app.core.logging import get_logger

logger = get_logger("channel")


class ChannelManager:
    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task] = {}
        self._adapters: dict[int, object] = {}
        self._kinds: dict[int, str] = {}
        self._stop = False

    async def _on_message(self, channel_id: int, msg: InboundMessage) -> None:
        """适配器回调：交由 dispatcher 处理。"""
        from app.channels.dispatcher import handle_inbound
        from app.core.db import AsyncSessionLocal
        from app.models import Channel

        adapter = self._adapters.get(channel_id)
        async with AsyncSessionLocal() as db:
            ch = await db.get(Channel, channel_id)
            if not ch or not ch.enabled:
                return
            try:
                await handle_inbound(adapter, ch, msg, send=True)
            except Exception:  # noqa: BLE001
                logger.exception("channel_handle_failed", channel_id=channel_id, kind=ch.kind)

    async def _run_channel(self, channel_id: int, kind: str, config: dict) -> None:
        try:
            adapter = build_adapter(kind, config=config, on_message=lambda m: self._on_message(channel_id, m))
        except ValidationError as e:
            # 配置缺失（未填 bot_id/secret 等）属于预期内的用户状态，不打 traceback
            logger.warning("channel_build_failed", channel_id=channel_id, kind=kind, err=str(e)[:300])
            await self._set_status(channel_id, connected=False, error=str(e)[:300])
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("channel_build_failed", channel_id=channel_id, kind=kind)
            await self._set_status(channel_id, connected=False, error=str(e)[:300])
            return
        self._adapters[channel_id] = adapter
        self._kinds[channel_id] = kind  # 记录 kind，供按 kind 兜底发送
        await self._set_status(channel_id, connected=True, error=None)
        try:
            await adapter.connect()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("channel_run_failed", channel_id=channel_id)
            await self._set_status(channel_id, connected=False, error=str(e)[:300])
        finally:
            try:
                await adapter.disconnect()
            except Exception:  # noqa: BLE001
                pass
            await self._set_status(channel_id, connected=False)

    async def _set_status(self, channel_id: int, *, connected: bool, error: str | None = "__keep__") -> None:
        from app.core.db import AsyncSessionLocal
        from app.models import Channel

        try:
            async with AsyncSessionLocal() as db:
                ch = await db.get(Channel, channel_id)
                if ch:
                    ch.connected = connected
                    if error != "__keep__":
                        ch.last_error = error
                    await db.commit()
        except Exception:  # noqa: BLE001
            pass

    async def start_all(self) -> None:
        """读取启用渠道并逐个启动（长连接）。"""
        from sqlalchemy import select

        from app.core.db import AsyncSessionLocal
        from app.models import Channel
        from app.services.channel_service import decrypt_config

        self._stop = False
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(Channel).where(Channel.enabled.is_(True), Channel.status == "active")
            )).scalars().all()
            specs = [(c.id, c.kind, decrypt_config(c.config)) for c in rows]

        for cid, kind, cfg in specs:
            if kind in ("qqbot", "wxclaw", "wework", "feishu"):
                self._start_one(cid, kind, cfg)
        logger.info("channels_started", count=len(self._tasks))

    def _start_one(self, channel_id: int, kind: str, config: dict) -> None:
        if channel_id in self._tasks:
            return
        task = asyncio.create_task(self._run_channel(channel_id, kind, config))
        self._tasks[channel_id] = task

    async def restart(self, channel_id: int) -> None:
        """配置变更后重启单个渠道。"""
        await self._stop_one(channel_id)
        from app.core.db import AsyncSessionLocal
        from app.models import Channel
        from app.services.channel_service import decrypt_config

        async with AsyncSessionLocal() as db:
            ch = await db.get(Channel, channel_id)
            if not ch or not ch.enabled:
                return
            kind, cfg = ch.kind, decrypt_config(ch.config)
        self._start_one(channel_id, kind, cfg)

    async def _stop_one(self, channel_id: int) -> None:
        task = self._tasks.pop(channel_id, None)
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._adapters.pop(channel_id, None)
        self._kinds.pop(channel_id, None)

    # ---- 主动发送（供通知出口 / 工作流推送节点复用）----
    def get_adapter(self, channel_id: int):
        """返回运行中的 adapter（未启动则为 None）。"""
        return self._adapters.get(channel_id)

    def is_connected(self, channel_id: int) -> bool:
        return channel_id in self._adapters

    def find_running_by_kind(self, kind: str) -> int | None:
        """按 kind 找当前运行中的渠道 id（同一 kind 通常只有一个活跃渠道）。

        用于历史会话的回发兜底：渠道删除/重建后 id 变化，旧会话的 settings.channel_id 会失效。
        """
        for cid, k in self._kinds.items():
            if k == kind:
                return cid
        return None

    async def send(self, channel_id: int, out) -> bool:
        """向指定渠道主动发送一条消息（复用运行中的长连接 adapter）。

        adapter 未就绪返回 False（不临时拉起——长连接拉起成本高且会缺入站回调）。
        """
        adapter = self._adapters.get(channel_id)
        if adapter is None:
            logger.warning("channel_send_not_ready", channel_id=channel_id)
            return False
        try:
            return bool(await adapter.send_message(out))
        except Exception:  # noqa: BLE001
            logger.exception("channel_send_failed", channel_id=channel_id)
            return False

    async def send_with_fallback(self, channel_id: int, kind: str | None, out) -> bool:
        """发送：优先指定 channel_id，失败则按 kind 找到运行中的渠道重试。

        解决「渠道删除/重建后 id 变化，历史会话回发失败」问题。
        """
        if self._adapters.get(channel_id) is not None:
            return await self.send(channel_id, out)
        if kind:
            cid = self.find_running_by_kind(kind)
            if cid is not None:
                logger.info("channel_send_fallback_by_kind", want=channel_id, use=cid, kind=kind)
                return await self.send(cid, out)
        logger.warning("channel_send_not_ready", channel_id=channel_id, kind=kind)
        return False

    async def stop_all(self) -> None:
        self._stop = True
        for cid in list(self._tasks.keys()):
            await self._stop_one(cid)


channel_manager = ChannelManager()
