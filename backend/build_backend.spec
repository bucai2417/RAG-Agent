# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（目录版 onedir）。

用法（在 backend/ 目录下）：
    ./.venv/Scripts/python.exe -m PyInstaller build_backend.spec --noconfirm

产物：dist/rag-backend/  （内含 rag-backend.exe + 依赖 + web/dist 前端）
绿色版：主程序启动后，数据写在 exe 同级的 data/ 目录。
"""
import os
from pathlib import Path

# 可选打包开关：是否把「本地离线向量模型」的推理依赖（torch + sentence-transformers）打进包内。
# 默认**不打**：torch CPU 版约 520MB，打进来会让 win-unpacked 从 390MB 涨到 1.2GB+。
# 不打时，用户在「模型管理 → 本地离线向量模型」按提示把依赖装到
# <数据目录>/data/models/runtime/，后端通过 app.providers.local_embed._ensure_import_paths()
# 显式插 sys.path 加载（冻结态读不到系统 site-packages）。
# 需要开箱即用时报：set RAG_BUNDLE_LOCAL_EMBED=1 再执行 PyInstaller。
BUNDLE_LOCAL_EMBED = os.environ.get("RAG_BUNDLE_LOCAL_EMBED") == "1"

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

# 可选：本地离线向量模型依赖（见文件头的 RAG_BUNDLE_LOCAL_EMBED 说明）
# torch 不要用 collect_all —— 它会把测试/分布式模块全拖进来；靠 hiddenimports 触发
# _pyinstaller_hooks_contrib 的 hook-torch（内含 collect_submodules + collect_dynamic_libs + MKL）。
excludes = ["matplotlib", "IPython", "notebook", "pytest"]
if BUNDLE_LOCAL_EMBED:
    from PyInstaller.utils.hooks import collect_all

    for _pkg in (
        "sentence_transformers", "transformers", "tokenizers", "safetensors",
        "huggingface_hub", "scipy", "sklearn", "tqdm", "regex", "filelock",
        "fsspec", "yaml", "jinja2", "sympy", "networkx",
    ):
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    hiddenimports += ["torch", "torch.nn", "torch.nn.functional"]
    excludes += ["onnxruntime", "tensorflow", "flax"]
    print("[INFO] RAG_BUNDLE_LOCAL_EMBED=1：已收集本地向量模型依赖（包体积约 +800MB~1GB）")

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
    excludes=["tkinter", "PyQt5", "PySide2"] + excludes,
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
