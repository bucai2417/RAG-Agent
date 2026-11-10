"""本地离线向量模型：目录扫描 / 缓存判定 / zip 导入 / 依赖探测 / 维度护栏。

全部本地，不联网、不加载真实模型（本测试环境未装 sentence-transformers）。
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

from app.core.errors import ValidationError
from app.providers import local_embed as LE


@pytest.fixture()
def model_dir(tmp_path, monkeypatch):
    """把模型根目录指向临时目录，避免污染 data/models。"""
    d = tmp_path / "models"
    d.mkdir()
    from app.core.config import settings

    monkeypatch.setattr(settings, "model_dir", str(d), raising=False)
    # 清理模块级状态，避免测试间串味
    LE._progress.clear()
    LE._BUSY.clear()
    LE._instances.clear()
    LE._dep_set(phase="idle", step="", log="", error=None, target=None, python=None)
    yield d
    LE._progress.clear()
    LE._BUSY.clear()
    LE._instances.clear()
    LE._dep_set(phase="idle", step="", log="", error=None, target=None, python=None)


def _model_zip(path: Path, *, root: str = "", with_weights: bool = True) -> Path:
    """造一个最小的 sentence-transformers 模型 zip。"""
    prefix = f"{root}/" if root else ""
    entries = {
        f"{prefix}config.json": b'{"hidden_size": 384, "model_type": "bert"}',
        f"{prefix}modules.json": b"[]",
        f"{prefix}tokenizer.json": b"{}",
    }
    if with_weights:
        entries[f"{prefix}model.safetensors"] = b"fake-weights"
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


# ------------------------------------------------------------------ 目录与推荐表


def test_recommended_models_shape(model_dir):
    assert LE.RECOMMENDED_MODELS, "推荐模型列表不能为空"
    for m in LE.RECOMMENDED_MODELS:
        for key in ("id", "label", "dim", "size", "note"):
            assert m.get(key), f"模型 {m.get('id')} 缺少字段 {key}"
        assert isinstance(m["dim"], int) and m["dim"] > 0
        # dim 表必须与之同步，否则建 Provider 时写入的向量维度会错
        assert LE._MODEL_DIMS[m["id"]] == m["dim"]


def test_cache_dir_is_project_local(model_dir):
    assert LE.cache_dir() == model_dir
    # runtime / manual 都挂在模型目录下，便于整体迁移
    assert LE.runtime_dir().parent == model_dir
    assert LE.manual_dir().parent == model_dir


# ------------------------------------------------------------------ 依赖探测


def test_dependency_status_without_sentence_transformers(model_dir):
    dep = LE.dependency_status()
    assert dep["installed"] is False
    assert dep["error"], "未安装时必须给出可读原因"
    # 无论如何都不能抛异常，否则后端启动/健康检查会被拖垮
    assert isinstance(dep["search_paths"], list)


@pytest.mark.asyncio
async def test_driver_health_reports_install_hint(model_dir):
    from app.providers.drivers.local_st import LocalSTDriver

    drv = LocalSTDriver(model_id="BAAI/bge-small-zh-v1.5")
    h = await drv.health()
    assert h.ok is False
    # 提示里必须同时有「依赖」相关字样和可操作路径（网页一键安装 or 手动命令），
    # 只说「失败」用户没法修
    assert "依赖" in h.message or "未安装" in h.message
    assert "一键安装" in h.message or "pip install" in h.message


def test_start_download_refuses_without_dependency(model_dir):
    r = LE.start_download("BAAI/bge-small-zh-v1.5")
    assert r["started"] is False
    assert "sentence-transformers" in r["reason"] or "未安装" in r["reason"]
    assert r.get("install_hint")
    # 拒绝时不能留下「正在下载」的脏状态或占用 BUSY
    assert LE.progress("BAAI/bge-small-zh-v1.5")["phase"] == "idle"
    assert "BAAI/bge-small-zh-v1.5" not in LE._BUSY


def test_start_download_rejects_bad_model_id(model_dir):
    with pytest.raises(ValidationError):
        LE.start_download("   ")


# ------------------------------------------------------------------ 缓存判定


def test_is_model_cached_false_when_absent(model_dir):
    assert LE.is_model_cached("BAAI/bge-small-zh-v1.5") is False
    assert LE.is_model_cached("") is False


def test_is_model_cached_true_after_fake_hub_dir(model_dir):
    snap = model_dir / "hub" / "models--BAAI--bge-small-zh-v1.5" / "snapshots" / "abcd1234"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}", encoding="utf-8")
    assert LE.is_model_cached("BAAI/bge-small-zh-v1.5") is True


def test_scan_local_models_reads_hub_and_manual(model_dir):
    # hf 缓存
    snap = model_dir / "hub" / "models--BAAI--bge-small-zh-v1.5" / "snapshots" / "rev1"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text('{"hidden_size": 512}', encoding="utf-8")
    (snap / "model.safetensors").write_bytes(b"x")
    # 手动导入
    manual = LE.manual_dir() / "my-local-model"
    manual.mkdir(parents=True)
    (manual / "config.json").write_text('{"hidden_size": 768}', encoding="utf-8")
    (manual / "pytorch_model.bin").write_bytes(b"x")

    models = {m["id"]: m for m in LE.scan_local_models()}
    assert models["BAAI/bge-small-zh-v1.5"]["source"] == "hf"
    assert models["BAAI/bge-small-zh-v1.5"]["dim"] == 512
    assert models["my-local-model"]["source"] == "manual"
    assert models["my-local-model"]["dim"] == 768


def test_dimension_of_known_and_unknown(model_dir):
    assert LE.dimension_of("BAAI/bge-small-zh-v1.5") == 512
    # 未知模型且未落地 → 0（前端显示「未知」）
    assert LE.dimension_of("no/such-model") == 0


# ------------------------------------------------------------------ zip 导入


def test_import_zip_flat_layout(model_dir, tmp_path):
    z = _model_zip(tmp_path / "m.zip")
    r = LE.install_from_zip(z, name="flat-model")
    assert r["ok"] is True
    assert r["model_id"] == "flat-model"
    final = LE.manual_dir() / "flat-model"
    assert (final / "config.json").is_file()
    assert (final / "model.safetensors").is_file()
    # 导入后必须能被扫描发现，否则用户导入了却用不了
    ids = {m["id"] for m in LE.scan_local_models()}
    assert "flat-model" in ids
    # 无残骸
    assert not list(LE.manual_dir().glob("*.partial"))
    assert not list(LE.manual_dir().glob("*.ready"))


def test_import_zip_with_single_root_dir(model_dir, tmp_path):
    # 网盘/GitHub 下载常见「外面套一层目录」
    z = _model_zip(tmp_path / "m2.zip", root="bge-nested")
    r = LE.install_from_zip(z, name="nested-model")
    final = LE.manual_dir() / "nested-model"
    assert (final / "config.json").is_file()
    # 不应把外层目录也带进来（否则 ST 加载会找不到 config.json）
    assert not (final / "bge-nested").is_dir()
    assert r["dim"] == 384


def test_import_zip_rejects_path_traversal(model_dir, tmp_path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../evil.txt", b"pwned")
        zf.writestr("config.json", b"{}")
    with pytest.raises(ValidationError) as ei:
        LE.install_from_zip(z, name="evil")
    assert "穿越" in str(ei.value) or "非法路径" in str(ei.value)
    # 不能在 manual 之外落地
    assert not (model_dir / "evil.txt").exists()


def test_import_zip_requires_model_files(model_dir, tmp_path):
    z = tmp_path / "notmodel.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("readme.txt", b"hello")
    with pytest.raises(ValidationError) as ei:
        LE.install_from_zip(z, name="bad")
    assert "config.json" in str(ei.value)


def test_import_rejects_symlink_member(model_dir, tmp_path):
    z = tmp_path / "link.zip"
    with zipfile.ZipFile(z, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = (0o120777 << 16) | 0o777
        zf.writestr(info, "/etc/passwd")
        zf.writestr("config.json", b"{}")
    with pytest.raises(ValidationError) as ei:
        LE.install_from_zip(z, name="link")
    assert "符号链接" in str(ei.value)


# ------------------------------------------------------------------ 删除


def test_delete_model_hub_and_manual(model_dir):
    snap = model_dir / "hub" / "models--BAAI--bge-small-zh-v1.5" / "snapshots" / "rev"
    snap.mkdir(parents=True)
    manual = LE.manual_dir() / "local-a"
    manual.mkdir(parents=True)
    (manual / "config.json").write_text("{}", encoding="utf-8")

    r = LE.delete_model("BAAI/bge-small-zh-v1.5")
    assert r["ok"] is True
    assert not (model_dir / "hub" / "models--BAAI--bge-small-zh-v1.5").exists()

    r2 = LE.delete_model("local-a")
    assert r2["ok"] is True
    assert not manual.exists()

    # 不存在时也不能报错（幂等）
    assert LE.delete_model("nothing-here")["ok"] is True


# ------------------------------------------------------------------ 运行时加载路径


def test_ensure_import_paths_inserts_runtime(model_dir):
    rt = LE.runtime_dir()
    rt.mkdir(parents=True, exist_ok=True)
    before = list(sys.path)
    try:
        paths = LE._ensure_import_paths()
        assert str(rt) in paths
        assert sys.path[0] == str(rt), "必须插到最前，优先级高于其他同名包"
        # 重复调用不重复插入
        LE._ensure_import_paths()
        assert sys.path.count(str(rt)) == 1
    finally:
        sys.path[:] = before


def test_ensure_import_paths_noop_without_dir(model_dir):
    # runtime 目录不存在时不得报错、不得污染 sys.path
    assert LE._ensure_import_paths() == []
    assert str(LE.runtime_dir()) not in sys.path


# ------------------------------------------------------------------ 依赖安装 / 卸载


def test_runtime_status_reports_not_installed(model_dir):
    st = LE.runtime_status()
    assert st["dir"] == str(LE.runtime_dir())
    assert st["installed"] is False
    assert st["size"] == 0
    assert st["busy"] is False
    # 源码态：当前解释器必须有版本号，否则前端会误判「无法自动安装」
    assert st["required_python"] in ("3.10",)
    assert st["packages"], "必须声明要装的包清单"
    assert st["progress"]["phase"] == "idle"


def test_runtime_status_detects_installed(model_dir):
    (LE.runtime_dir() / "sentence_transformers").mkdir(parents=True)
    (LE.runtime_dir() / "torch").mkdir(parents=True)
    st = LE.runtime_status()
    assert st["installed"] is True
    assert st["size"] == 0 or st["size"] >= 0


def test_dependency_status_rejects_incomplete_runtime(model_dir):
    """runtime 只有一半文件时不能报「已安装」，否则前端会不让点安装、推理又必炸。"""
    (LE.runtime_dir() / "torch").mkdir(parents=True)
    dep = LE.dependency_status()
    assert dep["installed"] is False
    assert "缺少" in dep["error"]


def test_dependency_status_ignores_stale_loaded_modules(model_dir):
    """卸载后 sys.modules 里还留着旧模块，探测必须仍然给出「未安装」。

    Runtime 里没有 runtime 目录时（源码态）依赖也可能装在 venv，这时按 import 判定；
    但 runtime 目录被人删掉后，sys.modules 里的旧模块必须被视为无效——否则
    前端点卸载后仍显示「已安装」（实测踩到）。
    """
    import types

    # 造一个「看起来装着、实际已丢文件」的 runtime：只有目录没有 torch/ST
    rt = LE.runtime_dir()
    rt.mkdir(parents=True, exist_ok=True)
    (rt / "torch").mkdir()

    fake = types.ModuleType("sentence_transformers")
    fake.__file__ = str(rt / "sentence_transformers" / "__init__.py")
    fake.__version__ = "0.0.0"
    sys.modules["sentence_transformers"] = fake
    try:
        dep = LE.dependency_status()
        assert dep["installed"] is False
    finally:
        sys.modules.pop("sentence_transformers", None)


def test_park_runtime_dir_moves_instead_of_overwriting(model_dir):
    """安装前必须整体挪走旧 runtime：Windows 上被加载的 c10.dll 无法被 pip 覆盖。"""
    rt = LE.runtime_dir()
    (rt / "torch").mkdir(parents=True)
    (rt / "torch" / "c10.dll").write_bytes(b"x")
    parked = LE._park_runtime_dir(rt)
    assert parked is not None and parked.exists()
    assert not rt.exists()
    assert LE.reap_runtime_if_pending() >= 1
    assert not parked.exists()


def test_install_task_removes_numpy_and_cleans_cache(model_dir, monkeypatch):
    """装完必须删掉 runtime/numpy：外置 numpy 与打包内的 2.x 撞 ABI。"""
    rt = LE.runtime_dir()
    # 伪造 pip：造出目录结构，其中故意塞一个 numpy 与 __pycache__
    def fake_run_pip(argv, *, on_log=None, timeout=3600):  # noqa: ARG001
        (rt / "torch").mkdir(parents=True, exist_ok=True)
        (rt / "sentence_transformers").mkdir(parents=True, exist_ok=True)
        (rt / "numpy").mkdir(parents=True, exist_ok=True)
        (rt / "numpy" / "core.py").write_text("x", encoding="utf-8")
        (rt / "torch" / "__pycache__").mkdir(parents=True, exist_ok=True)
        return True, "Successfully installed"

    monkeypatch.setattr(LE, "_run_pip", fake_run_pip)
    # dependency_status 用假的，避免依赖真实 torch/ST
    monkeypatch.setattr(LE, "dependency_status", lambda: {"installed": True, "version": "9.9.9"})
    LE._install_task(sys.executable)
    assert not (rt / "numpy").exists(), "runtime 里不能留 numpy"
    assert not (rt / "torch" / "__pycache__").exists(), "__pycache__ 要清掉省空间"
    assert LE.dependency_progress()["phase"] == "done"


def test_install_task_failure_sets_error(model_dir, monkeypatch):
    monkeypatch.setattr(LE, "_run_pip", lambda *a, **k: (False, "ERROR: No matching distribution"))
    LE._install_task(sys.executable)
    st = LE.dependency_progress()
    assert st["phase"] == "failed"
    assert "torch" in (st["error"] or "") or "安装失败" in (st["error"] or "")


def test_start_install_refuses_when_already_installed(model_dir, monkeypatch):
    monkeypatch.setattr(LE, "dependency_status", lambda: {"installed": True})
    r = LE.start_dependency_install()
    assert r["started"] is False and r.get("already_installed") is True


def test_start_install_refuses_when_busy(model_dir, monkeypatch):
    LE._dep_set(phase="running")
    try:
        r = LE.start_dependency_install()
        assert r["started"] is False and "安装中" in r["reason"]
    finally:
        LE._dep_set(phase="idle")


def test_uninstall_moves_runtime_away(model_dir):
    rt = LE.runtime_dir()
    (rt / "torch").mkdir(parents=True)
    (rt / "torch" / "x.dll").write_bytes(b"dll")
    r = LE.uninstall_dependencies()
    assert r["ok"] is True
    assert not rt.exists(), "卸载后 runtime 必须消失，_ensure_import_paths 才不会再插入"
    assert r.get("pending_cleanup")


def test_uninstall_noop_when_absent(model_dir):
    assert LE.uninstall_dependencies()["ok"] is True


def test_reap_runtime_trash(model_dir):
    trash = LE.cache_dir() / "runtime.trash-123"
    (trash / "torch").mkdir(parents=True)
    (trash / "torch" / "a.dll").write_bytes(b"x")
    assert LE.reap_runtime_if_pending() == 1
    assert not trash.exists()


def test_pip_env_strips_pythonpath(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "somewhere")
    monkeypatch.setenv("PYTHONHOME", "x")
    env = LE._pip_env()
    assert "PYTHONPATH" not in env and "PYTHONHOME" not in env
    assert env["PYTHONUTF8"] == "1"


def test_redact_hides_proxy_credentials():
    out = LE._redact("Downloading http://user:secret@mirror/a.whl")
    assert "secret" not in out
    assert "***:***@" in out


# ------------------------------------------------------------------ 维度护栏


@pytest.mark.asyncio
async def test_numpy_store_rejects_dimension_mismatch(model_dir):
    """换过向量模型后，库内旧向量与新查询向量维度不同 → 必须给可读错误。

    不加护栏时 numpy 只会抛 "matmul: dimension mismatch"，再被
    retrieval_service._one() 吞成 vector_unavailable，用户完全无感。
    """
    import numpy as np

    from app.core.db import AsyncSessionLocal, init_models
    from app.models import Chunk, Document, KnowledgeBase, Tenant
    from app.retrieval.vector_store.store import NumpyStore
    from app.services.permission import PrincipalSet, build_permission_filter

    await init_models()
    async with AsyncSessionLocal() as db:
        t = Tenant(name="DIM", slug="dim-guard")
        db.add(t)
        await db.flush()
        kb = KnowledgeBase(tenant_id=t.id, name="kb-dim", visibility="public", owner_id=1)
        db.add(kb)
        await db.flush()
        doc = Document(tenant_id=t.id, kb_id=kb.id, title="d", status="ready")
        db.add(doc)
        await db.flush()
        # 库内是 4 维（模拟旧模型留下的向量）
        for i in range(3):
            db.add(
                Chunk(
                    tenant_id=t.id, kb_id=kb.id, doc_id=doc.id, ordinal=i,
                    chunk_type="flat", content=f"旧模型分块 {i}",
                    embedding=[0.1, 0.2, 0.3, 0.4], vis_scope=0, enabled=True,
                )
            )
        await db.commit()
        tid = t.id

    async with AsyncSessionLocal() as db:
        ps = PrincipalSet(user_id=1, tenant_id=tid, is_admin=True)
        pf = build_permission_filter(ps, [kb.id])
        with pytest.raises(ValueError) as ei:
            await NumpyStore().search(
                db, query_vec=np.zeros(8, dtype=np.float32).tolist(), pf=pf, top_k=5
            )
        msg = str(ei.value)
        assert "向量维度不一致" in msg
        # 报错要给出两边维度与修复动作，否则用户不知道该怎么办
        assert "4" in msg and "8" in msg
        assert "重新处理" in msg


# ------------------------------------------------------------------ 状态探测不得阻塞


def test_dependency_status_does_not_import_heavy_modules(model_dir, monkeypatch):
    """依赖探测必须走 find_spec/dist-info，不能真的 import torch / sentence-transformers。

    回归背景：原先实现首次调用会 import 两者（实测 14.6s），而该函数被
    「进模型管理页」和「每 30 分钟健康检查」反复调用，同步执行会把事件循环
    整段钉住，整站表现为「登录后卡死」。这里用假的运行时目录 + 假的
    find_spec/metadata 断言「不 import、仍能给出正确结论」。
    """
    import importlib.util as ilu
    import types

    rt = LE.runtime_dir()
    (rt / "torch").mkdir(parents=True)
    (rt / "sentence_transformers").mkdir(parents=True)

    imported: list[str] = []

    def _fake_find_spec(name, *a, **k):  # noqa: ARG001
        # 只对这两个包返回位置；其它（如 importlib 内部）交给真实实现
        if name == "sentence_transformers":
            return types.SimpleNamespace(origin=str(rt / "sentence_transformers" / "__init__.py"),
                                         submodule_search_locations=[str(rt / "sentence_transformers")])
        if name == "torch":
            return types.SimpleNamespace(origin=str(rt / "torch" / "__init__.py"),
                                         submodule_search_locations=[str(rt / "torch")])
        return None

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def _guard_import(name, *a, **k):
        if name.split(".")[0] in ("torch", "sentence_transformers", "transformers"):
            imported.append(name)
            raise AssertionError(f"dependency_status 不应导入 {name}")
        return real_import(name, *a, **k)

    monkeypatch.setattr(ilu, "find_spec", _fake_find_spec)
    monkeypatch.setattr("builtins.__import__", _guard_import)
    monkeypatch.setattr(LE, "_package_version", lambda _n: "6.2.0")

    dep = LE.dependency_status()
    assert imported == []
    assert dep["installed"] is True and dep["torch"] is True
    assert dep["source"] == "runtime"
    assert dep["version"] == "6.2.0"


def test_dir_size_does_not_follow_symlinks(model_dir):
    """体积统计不能跟进链接：Windows 上没有目录链接，用文件链接验证不重复计入。"""
    rt = LE.runtime_dir()
    (rt / "pkg").mkdir(parents=True)
    (rt / "pkg" / "a.bin").write_bytes(b"x" * 100)
    assert LE._dir_size(rt) == 100
    assert LE._dir_size(rt / "missing") == 0


def test_runtime_size_is_cached_and_invalidated(model_dir):
    """体积统计要缓存（避免每次进页面重扫 2 万文件），安装/卸载后必须失效。"""
    rt = LE.runtime_dir()
    (rt / "torch").mkdir(parents=True)
    (rt / "sentence_transformers").mkdir(parents=True)
    (rt / "torch" / "a.bin").write_bytes(b"x" * 100)
    LE.invalidate_size_cache()
    assert LE.runtime_status()["size"] == 100
    # 目录继续变大：TTL 内仍返回缓存值（说明确实没重扫）
    (rt / "torch" / "b.bin").write_bytes(b"y" * 100)
    assert LE.runtime_status()["size"] == 100
    LE.invalidate_size_cache()
    assert LE.runtime_status()["size"] == 200


def test_uninstall_invalidates_size_cache(model_dir):
    rt = LE.runtime_dir()
    (rt / "torch").mkdir(parents=True)
    (rt / "sentence_transformers").mkdir(parents=True)
    (rt / "torch" / "a.bin").write_bytes(b"x" * 10)
    assert LE.runtime_status()["size"] == 10
    LE.uninstall_dependencies()
    assert LE.runtime_status()["size"] == 0


@pytest.mark.parametrize("bad", ["..", ".", "a/..", "../etc", "a/../.."])
def test_delete_model_rejects_path_escape(model_dir, bad):
    """model_id 来自请求体且最终要 rmtree：必须拒绝任何 . / .. 段。"""
    keep = model_dir / "important"
    keep.mkdir()
    (keep / "k.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValidationError):
        LE.delete_model(bad)
    assert (keep / "k.txt").is_file()


def test_delete_model_still_works_for_normal_ids(model_dir):
    d = LE.manual_dir() / "my-model"
    d.mkdir(parents=True)
    assert LE.delete_model("my-model")["ok"] is True
    assert not d.exists()


@pytest.mark.asyncio
async def test_driver_health_does_not_load_model(model_dir, monkeypatch):
    """健康检查（每 30 分钟批量跑）不能触发模型加载。

    否则「用户从没用过本地模型，后台却常驻数百 MB」，且批量检查会被单个模型的
    10~30 秒加载拖成几分钟——期间任何请求都要等它。
    """
    from app.providers.drivers.local_st import LocalSTDriver

    snap = model_dir / "hub" / "models--BAAI--bge-small-zh-v1.5" / "snapshots" / "rev"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text('{"hidden_size": 512}', encoding="utf-8")
    (snap / "model.safetensors").write_bytes(b"x")
    # 依赖探测直接给「已安装」，绕开真实 runtime
    monkeypatch.setattr(LE, "dependency_status", lambda: {"installed": True, "torch": True,
                                                          "version": "9", "error": "",
                                                          "search_paths": [], "source": "runtime"})
    loaded: list[str] = []

    class _Boom:
        loaded = False

        def embed_sync(self, *a, **k):  # noqa: ARG002
            loaded.append("embed")
            raise AssertionError("健康检查不应加载/推理模型")

    monkeypatch.setattr(LE, "get_embedder", lambda *_a, **_k: _Boom())

    h = await LocalSTDriver(model_id="BAAI/bge-small-zh-v1.5").health()
    assert h.ok is True
    assert loaded == []
    assert "512" in h.message  # 维度从 config.json 读出来，不靠加载
    assert "首次调用时加载" in h.message
