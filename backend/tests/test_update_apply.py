"""更新应用（下载/校验/解压/换盘脚本）测试。

全部离线：下载用假的 httpx 流式响应，换盘只验证脚本生成（不真跑 bat）。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import zipfile

from app.services import update_service as U


# ---------- 版本号白名单（防路径穿越/参数注入）----------

def test_is_safe_version_accepts_normal():
    assert U.is_safe_version("1.0.0")
    assert U.is_safe_version("1.0.10")
    assert U.is_safe_version("2.0")


def test_is_safe_version_rejects_traversal_and_junk():
    assert not U.is_safe_version("1.0/../../evil")
    assert not U.is_safe_version("..\\..\\windows")
    assert not U.is_safe_version("1.0.0 & calc")
    assert not U.is_safe_version("v1.0.0")   # 不含 v 前缀（tag 有、版本号无）
    assert not U.is_safe_version("")
    assert not U.is_safe_version("1.0.0\nrm -rf /")


# ---------- 下载 URL 校验 ----------
# 纯字符串部分（协议/白名单）同步可判；DNS/SSRF 那半必须异步调用，
# 否则同步 socket.getaddrinfo 会阻塞事件循环。

def test_check_download_url_rejects_non_https():
    assert U._check_download_url_scheme("http://github.com/x.zip") is not None
    assert U._check_download_url_scheme("ftp://github.com/x.zip") is not None


def test_check_download_url_rejects_foreign_host():
    assert U._check_download_url_scheme("https://evil.example.com/x.zip") is not None


def test_check_download_url_accepts_github():
    assert asyncio.run(
        U._check_download_url("https://github.com/241793/RAG-Agent/releases/download/v1.0.0/x.zip")
    ) is None
    assert asyncio.run(U._check_download_url("https://objects.githubusercontent.com/x.zip")) is None


def test_check_download_url_async_rejects_non_https():
    assert asyncio.run(U._check_download_url("http://github.com/x.zip")) is not None
    assert asyncio.run(U._check_download_url("ftp://github.com/x.zip")) is not None


# ---------- 下载 + 校验 ----------

class _FakeStream:
    def __init__(self, chunks, status=200, headers=None, url="https://github.com/x/y.zip"):
        self._chunks = chunks
        self.status_code = status
        self.headers = headers or {}
        self.url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_bytes(self, n=0):
        for c in self._chunks:
            yield c


class _FakeClient:
    def __init__(self, chunks, status=200, headers=None, url="https://github.com/x/y.zip"):
        self._chunks = chunks
        self._status = status
        self._headers = headers or {}
        self._url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url):
        return _FakeStream(self._chunks, self._status, self._headers, self._url)


def _patch_httpx(monkeypatch, chunks, status=200, headers=None, final_url="https://github.com/x/y.zip"):
    import httpx
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: _FakeClient(chunks, status, headers, final_url)
    )


def test_download_success_and_digest_verified(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_updates_root", lambda: tmp_path)
    payload = b"hello-update-payload" * 1000
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    _patch_httpx(monkeypatch, [payload[i:i+4096] for i in range(0, len(payload), 4096)],
                 headers={"content-length": str(len(payload))})

    r = asyncio.new_event_loop().run_until_complete(
        U.download_update(url="https://github.com/x/y.zip", digest=digest, version="1.0.1")
    )
    assert r["ok"] is True
    assert r["verified"] is True
    assert r["size"] == len(payload)
    assert U.get_progress()["phase"] == "verifying"  # 下载完成后进入校验/已过校验


def test_download_digest_mismatch_discards(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_updates_root", lambda: tmp_path)
    payload = b"tampered!!"
    _patch_httpx(monkeypatch, [payload])
    r = asyncio.new_event_loop().run_until_complete(
        U.download_update(url="https://github.com/x/y.zip",
                          digest="sha256:" + "0" * 64, version="1.0.1")
    )
    assert r["ok"] is False
    assert "校验失败" in r["error"]
    # 篡改文件必须被删除，不得留在磁盘
    assert not (tmp_path / "download" / "update.zip").exists()


def test_download_http_error(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_updates_root", lambda: tmp_path)
    _patch_httpx(monkeypatch, [b"x"], status=404)
    r = asyncio.new_event_loop().run_until_complete(
        U.download_update(url="https://github.com/x/y.zip", digest=None, version="1.0.1")
    )
    assert r["ok"] is False and "HTTP 404" in r["error"]


def test_download_rejects_bad_url(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_updates_root", lambda: tmp_path)
    r = asyncio.new_event_loop().run_until_complete(
        U.download_update(url="https://evil.example.com/x.zip", digest=None, version="1.0.1")
    )
    assert r["ok"] is False
    assert "白名单" in r["error"]


def test_download_blocks_redirect_to_foreign_host(monkeypatch, tmp_path):
    """回归：初始 URL 合法但被 302 到非白名单主机时必须中止。"""
    monkeypatch.setattr(U, "_updates_root", lambda: tmp_path)
    _patch_httpx(monkeypatch, [b"payload"], final_url="https://evil.example.com/dropped.zip")
    r = asyncio.new_event_loop().run_until_complete(
        U.download_update(url="https://github.com/x/y.zip", digest=None, version="1.0.1")
    )
    assert r["ok"] is False
    assert "重定向" in r["error"]
    assert not (tmp_path / "download" / "update.zip").exists()


# ---------- 解压 ----------

def _make_zip(path, entries):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)


def test_extract_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_updates_root", lambda: tmp_path)
    zp = tmp_path / "u.zip"
    _make_zip(zp, {"RAG知识库.exe": b"exe", "resources/backend/rag-backend.exe": b"backend"})
    r = U.extract_update(zip_path=str(zp), version="1.0.1")
    assert r["ok"] is True
    assert (tmp_path / "staging" / "v1.0.1" / "RAG知识库.exe").exists()
    assert U.get_progress()["phase"] == "ready"


def test_extract_blocks_zip_slip(monkeypatch, tmp_path):
    """含 ../ 的条目必须被拒绝（Zip Slip）。"""
    monkeypatch.setattr(U, "_updates_root", lambda: tmp_path)
    zp = tmp_path / "evil.zip"
    _make_zip(zp, {"../../evil.exe": b"pwn"})
    r = U.extract_update(zip_path=str(zp), version="1.0.1")
    assert r["ok"] is False
    assert "非法路径" in r["error"]
    # 绝不能写到 staging 之外
    assert not (tmp_path.parent / "evil.exe").exists()


def test_extract_replaces_previous_same_version(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_updates_root", lambda: tmp_path)
    # 先放一个残留文件
    stale = tmp_path / "staging" / "v1.0.1" / "OLD.txt"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("stale")
    zp = tmp_path / "u.zip"
    _make_zip(zp, {"NEW.txt": b"new"})
    r = U.extract_update(zip_path=str(zp), version="1.0.1")
    assert r["ok"] is True
    assert not stale.exists(), "同版本残留应被清掉"
    assert (tmp_path / "staging" / "v1.0.1" / "NEW.txt").exists()


# ---------- 换盘脚本 ----------

def test_apply_script_uses_arguments_not_literals():
    """脚本必须从 %1..%4 取路径（后端不知道安装目录），不得硬编码路径。"""
    s = U.build_apply_script()
    assert "%~1" in s and "%~2" in s and "%~3" in s
    assert "robocopy" in s
    assert "/MIR" not in s, "不得用 /MIR，避免 purge 删掉被占用的文件"
    assert ":rollback" in s, "必须含回滚分支"
    assert "tasklist" in s, "必须等待主进程退出"


def test_apply_script_loop_uses_goto_not_paren_block():
    """回归：块内 %n% 在解析期展开会让计数永不增长。必须用 goto 循环。"""
    s = U.build_apply_script()
    assert ":wait" in s and "goto wait" in s


def test_write_apply_script_creates_file(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_updates_root", lambda: tmp_path)
    p = U.write_apply_script()
    assert p.endswith("apply_update.bat")
    from pathlib import Path
    content = Path(p).read_text(encoding="utf-8-sig")
    assert "robocopy" in content


def test_write_pending_and_signal(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "_updates_root", lambda: tmp_path)
    sig = U.write_apply_signal()
    assert sig.endswith("apply.requested")
    assert (tmp_path / "apply.requested").exists()
    pend = U.write_pending(version="1.0.1", staging_dir=str(tmp_path / "staging" / "v1.0.1"),
                           script_path="x.bat")
    import json
    data = json.loads(open(pend, encoding="utf-8").read())
    assert data["version"] == "1.0.1"


def test_reset_progress():
    U.reset_progress()
    p = U.get_progress()
    assert p["phase"] == "idle" and p["received"] == 0 and p["error"] is None
