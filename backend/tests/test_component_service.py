"""可选组件：假 zip 安装 / 哈希校验 / zip-slip / 文件数上限 / 内置优先。

全部本地，不联网，不下载真实浏览器包。
"""
from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from app.core.errors import NotFoundError, ValidationError
from app.services import component_catalog as CAT
from app.services import component_service as CS


@pytest.fixture()
def comp_dir(tmp_path, monkeypatch):
    """把组件根目录指向临时目录，避免污染 data/components。"""
    d = tmp_path / "components"
    d.mkdir()
    from app.core.config import settings

    monkeypatch.setattr(settings, "component_dir", str(d), raising=False)
    # _root() 走的是 settings.component_path 属性，它读 component_dir
    yield d
    # 清理模块级进度/忙标记，避免测试间串味
    CS._progress.clear()
    CS._BUSY.clear()


def _make_zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def _fake_browser_zip(tmp_path: Path) -> Path:
    """造一个结构类似 Chrome for Testing win64 的最小包。"""
    return _make_zip(tmp_path / "chrome-win64.zip", {
        "chrome-win64/chrome.exe": b"MZ fake exe",
        "chrome-win64/icudtl.dat": b"data",
        "chrome-win64/locales/zh-CN.pak": b"pak",
    })


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ------------------------------------------------------------------ 目录与状态


def test_unknown_component_rejected(comp_dir):
    with pytest.raises(NotFoundError):
        CS.status("nope")


def test_status_not_installed(comp_dir):
    st = CS.status("browser_chromium")
    assert st["key"] == "browser_chromium"
    assert st["version"] == CAT.BROWSER_CHROMIUM.version
    assert st["installed"] is False
    assert st["executable"] is None
    assert st["install_path"] is None
    assert st["progress"]["phase"] == "idle"
    # 未安装时仍应给出体积预期，供前端展示
    assert st["size_bytes"] == CAT.BROWSER_CHROMIUM.size_for(st["platform"])


def test_catalog_platform_keys(comp_dir):
    # 四个平台的 URL 都应按 Chrome for Testing 的命名拼出来
    urls = CAT.BROWSER_CHROMIUM.urls
    assert set(urls) == {"win64", "mac-arm64", "mac-x64", "linux64"}
    assert urls["win64"].endswith(f"/{CAT.BROWSER_CHROMIUM.version}/win64/chrome-win64.zip")


# ------------------------------------------------------------------ 安装


def test_install_from_zip_success(comp_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(CAT, "platform_key", lambda: "win64")
    z = _fake_browser_zip(tmp_path)

    mf = CS.install_from_zip("browser_chromium", z, source_kind="upload")

    assert mf["platform"] == "win64"
    assert mf["verified"] is False        # 上传/镜像路径无预期哈希
    assert mf["source"] == "upload"
    exe_rel = mf["executable_rel"]
    # Windows 上会剥掉 zip 公共根（规避 260 字符路径限制），其余平台保留原结构
    assert exe_rel.endswith("chrome.exe")
    assert ".." not in exe_rel

    st = CS.status("browser_chromium")
    assert st["installed"] is True
    assert st["executable"] and Path(st["executable"]).is_file()
    assert st["verified"] is False
    assert CS.browser_executables(), "已安装的运行时必须能被浏览器管理器发现"
    # 无 .partial 残骸
    assert not list((comp_dir / "browser_chromium").glob("*.partial"))


def test_install_verifies_expected_sha(comp_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(CAT, "platform_key", lambda: "win64")
    z = _fake_browser_zip(tmp_path)
    sha = _sha(z)

    mf = CS.install_from_zip("browser_chromium", z, source_kind="official", expect_sha=sha)
    assert mf["verified"] is True

    CS.uninstall("browser_chromium")
    with pytest.raises(ValidationError) as ei:
        CS.install_from_zip("browser_chromium", z, source_kind="official",
                            expect_sha="0" * 64)
    # 报错要同时给出期望与实际前缀，便于核对目录里的常量
    assert "期望" in str(ei.value) and "实际" in str(ei.value)
    assert not (comp_dir / "browser_chromium" / CAT.BROWSER_CHROMIUM.version).exists()


def test_install_rejects_zip_slip(comp_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(CAT, "platform_key", lambda: "win64")
    z = _make_zip(tmp_path / "evil.zip", {
        "chrome-win64/chrome.exe": b"x",
        "../evil.txt": b"escaped",
    })
    with pytest.raises(ValidationError):
        CS.install_from_zip("browser_chromium", z, source_kind="upload")
    # 逃逸文件不能出现在组件根之外
    assert not (comp_dir.parent / "evil.txt").exists()
    assert not (comp_dir / "evil.txt").exists()
    assert not (comp_dir / "browser_chromium" / f"{CAT.BROWSER_CHROMIUM.version}.partial").exists()


def test_install_rejects_missing_executable(comp_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(CAT, "platform_key", lambda: "win64")
    z = _make_zip(tmp_path / "bad.zip", {"chrome-win64/readme.txt": b"nothing here"})
    with pytest.raises(ValidationError) as ei:
        CS.install_from_zip("browser_chromium", z, source_kind="upload")
    assert "可执行文件" in str(ei.value)


def test_install_rejects_too_many_files(comp_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(CAT, "platform_key", lambda: "win64")
    monkeypatch.setattr(CS, "MAX_FILES", 3)
    z = _make_zip(tmp_path / "many.zip", {
        "chrome-win64/chrome.exe": b"x",
        "chrome-win64/a": b"a",
        "chrome-win64/b": b"b",
        "chrome-win64/c": b"c",
    })
    with pytest.raises(ValidationError) as ei:
        CS.install_from_zip("browser_chromium", z, source_kind="upload")
    assert "文件数超过上限" in str(ei.value)


def test_install_rejects_escaping_symlink(comp_dir, tmp_path, monkeypatch):
    """指向包外的符号链接必须拒绝（否则能在磁盘上留一个指向系统文件的链接）。"""
    monkeypatch.setattr(CAT, "platform_key", lambda: "win64")
    p = tmp_path / "link.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("chrome-win64/chrome.exe", b"x")
        info = zipfile.ZipInfo("chrome-win64/link")
        info.external_attr = (0o120777 << 16)   # symlink 模式位
        zf.writestr(info, "../../../../etc/passwd")
    with pytest.raises(ValidationError) as ei:
        CS.install_from_zip("browser_chromium", p, source_kind="upload")
    assert "符号链接" in str(ei.value)


def test_install_allows_internal_symlink(comp_dir, tmp_path, monkeypatch):
    """macOS .app 内部相对符号链接是合法结构，必须允许（否则 mac 包装不上）。"""
    monkeypatch.setattr(CAT, "platform_key", lambda: "win64")
    p = tmp_path / "link-in.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("chrome-win64/chrome.exe", b"x")
        info = zipfile.ZipInfo("chrome-win64/lib/current")
        info.external_attr = (0o120777 << 16)
        zf.writestr(info, "../1.0/lib")     # 仍在包内
    mf = CS.install_from_zip("browser_chromium", p, source_kind="upload")
    assert mf["platform"] == "win64"


def test_install_unsupported_platform(comp_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(CAT, "platform_key", lambda: "linux-arm64")
    z = _fake_browser_zip(tmp_path)
    with pytest.raises(ValidationError) as ei:
        CS.install_from_zip("browser_chromium", z, source_kind="upload")
    assert "没有可用的安装包" in str(ei.value)


# ------------------------------------------------------------------ 卸载


def test_uninstall_removes_dir(comp_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(CAT, "platform_key", lambda: "win64")
    CS.install_from_zip("browser_chromium", _fake_browser_zip(tmp_path), source_kind="upload")
    assert CS.status("browser_chromium")["installed"] is True

    ret = CS.uninstall("browser_chromium")
    assert ret["ok"] is True
    assert CS.status("browser_chromium")["installed"] is False
    assert not (comp_dir / "browser_chromium").exists()
    # 幂等
    assert CS.uninstall("browser_chromium")["ok"] is True


# ------------------------------------------------------------------ 安装入口


def test_start_install_blocks_duplicate(comp_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(CAT, "platform_key", lambda: "win64")
    CS.install_from_zip("browser_chromium", _fake_browser_zip(tmp_path), source_kind="upload")
    ret = CS.start_install("browser_chromium", source="official")
    assert ret["started"] is False and ret.get("already_installed") is True


def test_start_install_rejects_bad_source(comp_dir):
    with pytest.raises(ValidationError):
        CS.start_install("browser_chromium", source="ftp")


def test_start_install_rejects_insecure_url(comp_dir):
    with pytest.raises(ValidationError) as ei:
        CS.start_install("browser_chromium", source="url", url="http://example.com/a.zip")
    assert "https" in str(ei.value)


def test_start_install_rejects_internal_url(comp_dir):
    with pytest.raises(ValidationError):
        CS.start_install("browser_chromium", source="url",
                         url="https://127.0.0.1/chrome.zip")


# ------------------------------------------------------------------ 浏览器优先级


def test_bundled_browser_preferred(comp_dir, tmp_path, monkeypatch):
    from app.services import browser_manager as BM

    monkeypatch.setattr(CAT, "platform_key", lambda: "win64")
    CS.install_from_zip("browser_chromium", _fake_browser_zip(tmp_path), source_kind="upload")

    # 系统候选清空，只留内置
    monkeypatch.setitem(BM._CANDIDATES, "win32", [])
    monkeypatch.setattr(BM.sys, "platform", "win32")

    found = BM.detect_browsers()
    assert found and found[0].source == "bundled"
    assert BM.get_state()["bundled_installed"] is True
    assert BM.get_state()["browsers"][0]["source"] == "bundled"


def test_detect_browsers_tolerates_missing_component_service(comp_dir, monkeypatch):
    """组件服务异常不能让浏览器检测整体失败。"""
    from app.services import browser_manager as BM

    monkeypatch.setitem(BM._CANDIDATES, "win32", [])
    monkeypatch.setattr(BM.sys, "platform", "win32")
    monkeypatch.setattr(CS, "browser_executables",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert BM.detect_browsers() == []
