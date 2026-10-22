"""FastAPI 应用装配（统一入口：API + 前端静态文件同端口）。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1.router import api_router
from app.core.config import RESOURCE_DIR, settings
from app.core.db import init_models
from app.core.errors import AppError, app_error_handler, unhandled_error_handler
from app.core.logging import get_logger, setup_logging
from app.middleware.request_id import RequestContextMiddleware
from app.tasks.queue import start_workers, stop_workers

setup_logging()
logger = get_logger("main")

# 前端构建产物目录（唯一前端 = Vue3）：web/dist，资源前缀 /assets。
# 源码运行：项目根/web/dist；冻结运行：_MEIPASS/web/dist（PyInstaller --add-data 打入）。
WEB_DIST = RESOURCE_DIR / "web" / "dist"


async def _apply_pending_db() -> None:
    """启动期应用「数据库切换」标记（崩溃兜底）。

    若 data/pending_db.txt 存在：用目标库重建 engine 并试建表 →
    成功则把目标 URL 提升为生效配置；失败则回退到 previous_url 继续启动。
    这样切库失败不会把服务锁死。
    """
    import json

    from sqlalchemy import select

    import app.core.db as dbm
    from app.core.config import DATA_DIR
    from app.models import SystemSetting

    f = DATA_DIR / "pending_db.txt"
    if not f.exists():
        return
    try:
        info = json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.warning("pending_db_unreadable")
        f.unlink(missing_ok=True)
        return

    target = (info.get("target_url") or "").strip()
    prev = (info.get("previous_url") or "").strip()
    if not target:
        f.unlink(missing_ok=True)
        return

    try:
        await dbm.reinit_engine(target)
        await dbm.init_models_for(dbm.async_engine)
        # 成功：把目标 URL 提升为生效配置
        try:
            from app.core.db import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                row = (await db.execute(
                    select(SystemSetting).where(SystemSetting.key == "database_url")
                )).scalar_one_or_none()
                if row:
                    row.value = target
                else:
                    db.add(SystemSetting(key="database_url", value=target))
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("pending_db_persist_failed")
        logger.info("db_switched", target=target)
    except Exception as e:  # noqa: BLE001
        logger.error("db_switch_failed_fallback", target=target, err=str(e)[:200])
        if prev:
            try:
                await dbm.reinit_engine(prev)
                await dbm.init_models_for(dbm.async_engine)
                logger.warning("db_fallback_to_previous", previous=prev)
            except Exception:  # noqa: BLE001
                logger.exception("db_fallback_failed")
    finally:
        f.unlink(missing_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _apply_pending_db()
    await init_models()
    # 同步内置角色的名称/范围/描述（升级后文案变更无需手动 seed；仅更新元数据，不动权限）
    try:
        from app.core.db import AsyncSessionLocal
        from app.services.permission_seed import sync_role_meta

        async with AsyncSessionLocal() as _db:
            await sync_role_meta(_db)
    except Exception:  # noqa: BLE001
        logger.exception("role_meta_sync_failed")
    # 叠加 Web 端「系统设置」的覆盖值（热生效项立即生效；启动期项影响本进程）
    try:
        from app.core.db import AsyncSessionLocal
        from app.services.settings_service import apply_overrides

        async with AsyncSessionLocal() as _db:
            await apply_overrides(_db)
    except Exception:  # noqa: BLE001
        logger.exception("settings_overrides_failed")
    if settings.task_backend == "memory":
        await start_workers(2)

    # HITL 恢复：把「审批通过但未执行完」的卡死动作标记失败（进程重启兜底）
    try:
        from app.core.db import AsyncSessionLocal
        from app.tasks.hitl_recover import recover_stuck_actions

        async with AsyncSessionLocal() as _hdb:
            n = await recover_stuck_actions(_hdb)
            if n:
                logger.warning("hitl_recovered_on_startup", count=n)
    except Exception:  # noqa: BLE001
        logger.exception("hitl_recover_failed")

    # 后台产物过期清理循环
    import asyncio

    from app.tasks.artifact_tasks import periodic_cleanup
    from app.tasks.scheduler_tasks import scheduler_loop

    cleanup_task = asyncio.create_task(periodic_cleanup())
    scheduler_task = asyncio.create_task(scheduler_loop())

    # Provider 健康检查（周期可配，0=关闭）
    from app.tasks.health_tasks import periodic_health_check

    health_task = asyncio.create_task(
        periodic_health_check(settings.provider_health_interval_min * 60)
    )

    # 日志自动清理（按保留天数）
    from app.tasks.log_tasks import periodic_log_cleanup

    log_task = asyncio.create_task(
        periodic_log_cleanup(settings.log_cleanup_interval_hours * 3600)
    )

    # 邮件入库源轮询
    from app.tasks.email_tasks import periodic_email_poll

    email_task = asyncio.create_task(
        periodic_email_poll(settings.email_poll_interval_seconds)
    )

    # Webhook 回调重试：扫描到期的待投递记录并按退避重发
    from app.tasks.callback_tasks import periodic_callback_flush

    callback_task = asyncio.create_task(periodic_callback_flush(15))

    # 个人邮箱账号轮询（收件箱）
    from app.tasks.mail_tasks import periodic_mail_poll

    mail_task = asyncio.create_task(
        periodic_mail_poll(getattr(settings, "email_poll_interval_seconds", 300) or 300)
    )

    # 客服工单 SLA 超时扫描
    from app.tasks.sla_tasks import periodic_sla_check

    sla_task = asyncio.create_task(periodic_sla_check(300))

    # 待办/日程提醒扫描
    from app.tasks.reminder_tasks import periodic_reminder_check

    reminder_task = asyncio.create_task(periodic_reminder_check(60))

    # Docker 在线更新看门狗：仅容器部署启用（覆盖源码后退出，由 compose 拉起）
    docker_update_task = None
    try:
        from app.services.update_service import detect_deployment

        if detect_deployment() == "docker":
            from app.tasks.docker_update_tasks import periodic_docker_update_check

            docker_update_task = asyncio.create_task(periodic_docker_update_check(10))
            logger.info("docker_update_watchdog_started")
    except Exception:  # noqa: BLE001
        logger.exception("docker_update_watchdog_start_failed")

    # 外部 IM 渠道宿主（长连接）
    from app.channels.manager import channel_manager

    try:
        await channel_manager.start_all()
    except Exception:  # noqa: BLE001
        logger.exception("channels_start_failed")

    logger.info(
        "app_started",
        db=settings.database_url.split("://")[0],
        vector=settings.resolved_vector_backend,
        tasks=settings.task_backend,
    )

    # 打印访问地址（放在启动日志末尾，最醒目）
    try:
        from app.core.netutil import access_urls, resolve_bind

        _host, _port = resolve_bind(settings.port)
        for u in access_urls(_host, _port):
            logger.info(
                "server_access_url",
                scope=u["scope"],
                url=u["url"],
                **({"note": u["note"]} if u.get("note") else {}),
            )
        logger.info("server_bind", host=_host, port=_port)
    except Exception:  # noqa: BLE001
        logger.exception("access_url_log_failed")

    yield
    cleanup_task.cancel()
    scheduler_task.cancel()
    health_task.cancel()
    log_task.cancel()
    email_task.cancel()
    callback_task.cancel()
    mail_task.cancel()
    sla_task.cancel()
    reminder_task.cancel()
    if docker_update_task is not None:
        docker_update_task.cancel()
    try:
        await channel_manager.stop_all()
    except Exception:  # noqa: BLE001
        pass
    # MCP stdio 长驻进程清理
    from app.services.mcp_manager import mcp_manager

    try:
        await mcp_manager.stop_all()
    except Exception:  # noqa: BLE001
        pass
    # 浏览器子进程清理：只终止由本进程拉起的实例，不动用户自己开的调试浏览器
    from app.services.browser_manager import cleanup_on_shutdown as browser_cleanup

    try:
        browser_cleanup()
    except Exception:  # noqa: BLE001
        pass
    # 取消并等待在途后台任务（工作流/评测/调度/事件等），防半写与资源泄漏
    from app.tasks.bg import shutdown as bg_shutdown

    try:
        await bg_shutdown()
    except Exception:  # noqa: BLE001
        pass
    if settings.task_backend == "memory":
        await stop_workers()
    logger.info("app_stopped")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs",
    openapi_url="/openapi.json",
)

app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(Exception, unhandled_error_handler)

app.include_router(api_router, prefix=settings.api_prefix)

# OpenAI 兼容端点（根路径 /v1，供内部系统用现成 SDK 接入）
from app.api.v1.openai_compat import router as openai_router  # noqa: E402

app.include_router(openai_router)


@app.get("/health")
async def health() -> dict:
    """存活 + 就绪探活：真实探测 DB 连通性与关键子系统状态（供运维/负载均衡使用）。"""
    checks: dict = {}
    overall = "ok"

    # DB 连通性（真实执行一次轻查询）
    try:
        from sqlalchemy import text as _text

        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as _db:
            await _db.execute(_text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["db"] = f"fail: {str(e)[:120]}"
        overall = "degraded"

    # 后台任务积压
    try:
        from app.tasks.bg import pending_count

        checks["bg_tasks"] = pending_count()
    except Exception:  # noqa: BLE001
        pass

    # 任务队列（memory 后端）
    checks["task_backend"] = settings.task_backend

    return {
        "status": overall,
        "version": settings.app_version,
        "db_driver": settings.database_url.split("://")[0],
        "vector_backend": settings.resolved_vector_backend,
        "checks": checks,
    }


# ---- 前端静态托管（统一入口：浏览器只访问本端口）----
# 单一前端：/assets 挂产物资源；/ 与未匹配路由一律返回 index.html。
if (WEB_DIST / "index.html").is_file():
    assets_dir = WEB_DIST / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(str(WEB_DIST / "index.html"))

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        """SPA 前端路由回退：非 API 路径一律返回 index.html。"""
        if full_path.startswith(("api/", "v1/")) or full_path in ("docs", "openapi.json", "health"):
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Not Found")
        candidate = WEB_DIST / full_path
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(WEB_DIST / "index.html"))
else:
    logger.warning("web_dist_missing", path=str(WEB_DIST), hint="前端未构建，仅提供 API。运行 cd web && npm run build")
