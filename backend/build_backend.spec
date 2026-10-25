# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（目录版 onedir）。

用法（在 backend/ 目录下）：
    ./.venv/Scripts/python.exe -m PyInstaller build_backend.spec --noconfirm

产物：dist/rag-backend/  （内含 rag-backend.exe + 依赖 + web/dist 前端）
绿色版：主程序启动后，数据写在 exe 同级的 data/ 目录。
"""
import os
from pathlib import Path

# 项目根与前端 dist（唯一前端 = Vue3）
PROJECT_ROOT = Path(SPECPATH).parent          # backend/ 的上级 = 项目根
WEB_DIST = PROJECT_ROOT / "web" / "dist"
BACKEND_DIR = Path(SPECPATH)                   # backend/

# 数据文件：前端 dist + 更新日志，打进包内（冻结时经 sys._MEIPASS 访问）
datas = []
if WEB_DIST.exists():
    datas.append((str(WEB_DIST), "web/dist"))
else:
    print(f"[WARN] 前端未构建，跳过：{WEB_DIST}  （先 cd web && npm run build）")

# CHANGELOG.md：桌面版离线时更新日志也要能显示（changelog_service 读 RESOURCE_DIR）
CHANGELOG = PROJECT_ROOT / "CHANGELOG.md"
if CHANGELOG.exists():
    datas.append((str(CHANGELOG), "."))
else:
    print(f"[WARN] 更新日志缺失，桌面版将回退到 GitHub Release：{CHANGELOG}")

# 需要显式声明的隐藏导入（动态 import / C 扩展，PyInstaller 静态分析易漏）
hiddenimports = [
    "app",                           # 显式导入包，确保被收集
    "app.main",
    "app.bootstrap",
    "app.models",                    # 动态 import 注册所有表
    "app.core.migrations",           # 启动期迁移入口（动态 import alembic）
    "alembic",
    "alembic.config",
    "alembic.command",
    "alembic.script",
    "alembic.runtime.migration",
    "alembic.runtime.plugins",
    "alembic.ddl.sqlite",
    "alembic.ddl.postgresql",
    "aiosqlite",
    "greenlet",
    "sqlalchemy.dialects.sqlite.aiosqlite",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "pymupdf",
    "docx",
    "openpyxl",
    "pptx",
    "bs4",
    "lxml",
    "lxml.etree",
    "markdown",
]

# Alembic 迁移脚本属于运行时数据：alembic/env.py 与 versions/*.py 必须随包分发，
# 否则冻结版启动时会走「找不到 alembic → create_all」的降级路径。
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
ALEMBIC_DIR = BACKEND_DIR / "alembic"
if ALEMBIC_INI.is_file() and (ALEMBIC_DIR / "env.py").is_file():
    datas.append((str(ALEMBIC_INI), "."))
    datas.append((str(ALEMBIC_DIR), "alembic"))
else:
    print(f"[WARN] alembic 缺失，冻结版将退化为 create_all：{ALEMBIC_DIR}")

block_cipher = None

a = Analysis(
    ["run_server.py"],
    pathex=[str(BACKEND_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PySide2", "IPython", "pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="rag-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(PROJECT_ROOT / "desktop" / "icon.ico") if (PROJECT_ROOT / "desktop" / "icon.ico").exists() else None,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="rag-backend",
)
