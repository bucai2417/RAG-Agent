"""桌面/独立部署启动入口。

被 PyInstaller 作为 --entry 打包，也用于命令行直接启动后端。
从环境变量读取 HOST/PORT（Electron 启动子进程时注入）。
"""
from __future__ import annotations

import asyncio
import os

import uvicorn

# 显式导入 app，确保 PyInstaller 能静态追踪到整个 app 包（含所有子模块/表模型）
from app.bootstrap import seed_all
from app.main import app as _app


def main() -> None:
    # 首次运行自动初始化（幂等：已有数据则跳过），确保内置 admin 与角色存在。
    # 同时叠加 Web 端「系统设置」的覆盖值（端口等启动期项在此之后读取才生效）。
    from app.bootstrap import SchemaMigrationError

    try:
        asyncio.run(seed_all())
    except SchemaMigrationError as e:
        # schema 迁移失败必须中止：带着旧 schema 跑只会把「升级失败」变成满屏 500，
        # 而且下一次启动还是同样的错，越跑越难排查。
        import sys
        import traceback

        traceback.print_exc()
        print(f"[bootstrap] {e}，启动中止。", file=sys.stderr)
        print(
            "[bootstrap] 排查建议：\n"
            "  1) 确认 DATABASE_URL 指向的库可连接；\n"
            "  2) 执行 `python -m alembic upgrade head` 查看具体迁移错误；\n"
            "  3) 若确实要跳过自动迁移启动，设置 RAG_SKIP_MIGRATIONS=1。",
            file=sys.stderr,
        )
        raise SystemExit(1) from e
    except Exception as e:  # noqa: BLE001
        # 播种失败（如某条内置数据）不阻止启动：服务仍可用，缺的东西可在 Web 端补
        print(f"[bootstrap] 初始化部分失败（不影响启动）: {e}")

    from app.core.config import settings

    host = os.environ.get("RAG_HOST", "127.0.0.1")
    port = int(os.environ.get("RAG_PORT") or os.environ.get("PORT") or settings.port)

    # 冻结/生产环境关闭热重载与调试
    uvicorn.run(
        _app,
        host=host,
        port=port,
        log_level=os.environ.get("RAG_LOG_LEVEL", "info"),
        access_log=False,
    )


if __name__ == "__main__":
    main()
