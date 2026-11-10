"""pytest 配置：使用独立测试库，避免污染开发数据。

必须在导入任何 app 模块前设置环境变量（settings 与 db engine 在导入时初始化）。
"""
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# 独立测试库
_TEST_DB = BACKEND_DIR / "data" / "test_rag.db"
if _TEST_DB.exists():
    _TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB.as_posix()}"
# 独立测试文件目录：否则测试里的 storage.delete 会删掉真实 data/files 下用户的产物
os.environ["STORAGE_LOCAL_DIR"] = str(BACKEND_DIR / "data" / "test_files")
# 独立技能包目录：否则技能包导入测试会把解压目录写进真实 data/skills（造成大量孤儿包）
os.environ["SKILL_PACK_DIR"] = str(BACKEND_DIR / "data" / "test_skills")
# 独立组件目录：否则可选组件安装测试会把浏览器包解压进真实 data/components
os.environ["COMPONENT_DIR"] = str(BACKEND_DIR / "data" / "test_components")
# 独立模型目录：否则本地向量模型的缓存/导入测试会写进真实 data/models
os.environ["MODEL_DIR"] = str(BACKEND_DIR / "data" / "test_models")
os.environ["EMBEDDING_DIM"] = "64"
os.environ["TASK_BACKEND"] = "memory"


@pytest.fixture(scope="session")
def event_loop():
    """兼容旧式 fixture 写法：提供 session 级事件循环。"""
    import asyncio

    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
