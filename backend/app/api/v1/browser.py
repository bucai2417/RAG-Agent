"""浏览器管理接口：检测、启动、停止本机 Chromium 系浏览器（带调试端口）。

用途：让 AI 的浏览器工具能复用用户已登录的会话，而不必手动敲命令行。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends

from app.core.errors import ValidationError
from app.models import User
from app.middleware.auth_dep import require_permission
from app.services import browser_manager as BM

router = APIRouter(prefix="/browser", tags=["browser"])


@router.get("/status")
async def browser_status(
    user: User = Depends(require_permission("tool:invoke")),
) -> dict:
    """当前浏览器状态：是否运行、调试端口是否就绪、检测到的浏览器列表。"""
    return BM.get_state()


@router.post("/launch")
async def browser_launch(
    executable: str | None = None,
    port: int = BM.DEFAULT_PORT,
    headless: bool = False,
    user: User = Depends(require_permission("tool:invoke")),
) -> dict:
    """启动带调试端口的浏览器（自动检测本机 Chrome/Edge）。

    非 headless 时会弹出浏览器窗口，可在其中登录目标网站；
    登录态保存在应用数据目录下的 browser_profile，下次启动可复用。
    """
    if port < 1024 or port > 65535:
        raise ValidationError("端口需在 1024-65535 之间")
    # launch() 是同步的（起子进程 + 最长 15 秒轮询调试端口），
    # 直接在事件循环里跑会把整个后端卡住，必须丢线程。
    return await asyncio.to_thread(
        BM.launch, executable=executable, port=port, headless=headless
    )


@router.post("/stop")
async def browser_stop(
    user: User = Depends(require_permission("tool:invoke")),
) -> dict:
    """停止浏览器。

    - 由本服务启动的实例：直接结束进程。
    - 后端重启后句柄丢失、但用的是本服务 profile 的实例：通过 CDP 优雅关闭。
    - 用户自己开的调试浏览器（非本服务 profile）：不动它。
    """
    # stop() 内部走 CDP 关闭（同步阻塞），同样放到线程里避免卡事件循环
    return await asyncio.to_thread(BM.stop)
