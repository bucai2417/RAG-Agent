"""更新检查服务测试：版本比较、部署形态判定、优雅降级、缓存。

httpx 出站被 mock（不经网络），验证逻辑与错误路径。
"""
from __future__ import annotations

import asyncio

from app.services import update_service as U


# ---------- 版本比较 ----------

def test_parse_version_forms():
    assert U._parse_version("1.0.0") == (1, 0, 0)
    assert U._parse_version("v1.2.3") == (1, 2, 3)
    assert U._parse_version("V2.0") == (2, 0)
    assert U._parse_version("1.2.3-rc1") == (1, 2, 3)
    assert U._parse_version("1.2.3+build.5") == (1, 2, 3)
    assert U._parse_version("") is None
    assert U._parse_version("abc") is None
    assert U._parse_version("1.x.0") is None


def test_is_newer_semver():
    assert U.is_newer("1.0.1", "1.0.0")
    assert U.is_newer("1.1.0", "1.0.9")
    # 数值比较而非字符串：1.0.10 > 1.0.9
    assert U.is_newer("1.0.10", "1.0.9")
    assert U.is_newer("2.0.0", "1.99.99")
    assert not U.is_newer("1.0.0", "1.0.0")
    assert not U.is_newer("1.0.0", "1.0.1")
    # 长度补齐：1.0 == 1.0.0
    assert not U.is_newer("1.0", "1.0.0")
    assert not U.is_newer("1.0.0", "1.0")
    # v 前缀与预发布后缀
    assert U.is_newer("v1.0.1", "1.0.0")
    assert U.is_newer("1.0.1-rc1", "1.0.0")


def test_is_newer_unparseable_is_conservative():
    """不可解析时不得误报有更新。"""
    assert not U.is_newer("abc", "1.0.0")
    assert not U.is_newer("1.0.1", "xyz")
    assert not U.is_newer(None, "1.0.0")


def test_is_prerelease():
    assert U.is_prerelease("v1.0.1-rc1")
    assert U.is_prerelease("1.0.0-beta.2")
    assert not U.is_prerelease("v1.0.0")
    assert not U.is_prerelease("1.0.0")


# ---------- 部署形态 ----------

def test_detect_deployment_source(monkeypatch):
    monkeypatch.delenv("RAG_DEPLOYMENT", raising=False)
    # 源码态（非 frozen、无 /.dockerenv）应为 source
    assert U.detect_deployment() == "source"


def test_detect_deployment_env_override(monkeypatch):
    monkeypatch.setenv("RAG_DEPLOYMENT", "docker")
    assert U.detect_deployment() == "docker"
    monkeypatch.setenv("RAG_DEPLOYMENT", "desktop")
    assert U.detect_deployment() == "desktop"


# ---------- 资源挑选 ----------

def test_pick_asset_prefers_win_x64():
    assets = [
        {"name": "RAG-knowledgebase-v1.0.0-src.zip"},
        {"name": "RAG-knowledgebase-v1.0.0-win-x64.zip"},
    ]
    assert U._pick_asset(assets)["name"].endswith("win-x64.zip")
    assert U._pick_asset([]) is None
    assert U._pick_asset([{"name": "only-src.zip"}]) is None


# ---------- check_update 降级路径 ----------

def _fake_gh(status, data, headers=None, raises=None):
    async def _inner(path, *, params=None, token=None, timeout=20):
        if raises:
            raise raises
        return status, data, (headers or {})
    return _inner


def test_check_update_offline_returns_error(monkeypatch):
    """网络失败 → ok=False + error，且不抛异常（端点不得 500）。"""
    import app.services.skill_search_service as S
    monkeypatch.setattr(S, "_gh_get", _fake_gh(0, None, raises=OSError("network down")))
    U.clear_cache()
    r = asyncio.new_event_loop().run_until_complete(U.check_update())
    assert r["ok"] is False
    assert r["update_available"] is False
    assert "无法连接" in r["error"]
    U.clear_cache()


def test_check_update_rate_limited(monkeypatch):
    import app.services.skill_search_service as S
    monkeypatch.setattr(S, "_gh_get", _fake_gh(403, None, {"x-ratelimit-remaining": "0"}))
    U.clear_cache()
    r = asyncio.new_event_loop().run_until_complete(U.check_update())
    assert r["ok"] is False and "限流" in r["error"]
    U.clear_cache()


def test_check_update_404(monkeypatch):
    import app.services.skill_search_service as S
    monkeypatch.setattr(S, "_gh_get", _fake_gh(404, None))
    U.clear_cache()
    r = asyncio.new_event_loop().run_until_complete(U.check_update())
    assert r["ok"] is False and "未找到" in r["error"]
    U.clear_cache()


def test_check_update_parses_release(monkeypatch):
    import app.services.skill_search_service as S
    payload = {
        "tag_name": "v9.9.9",
        "body": "### 更新内容\n- 修了个大 bug",
        "published_at": "2026-01-01T00:00:00Z",
        "html_url": "https://github.com/241793/RAG-Agent/releases/tag/v9.9.9",
        "prerelease": False,
        "assets": [
            {"name": "RAG-knowledgebase-v9.9.9-src.zip", "size": 1, "digest": "sha256:aa",
             "browser_download_url": "https://github.com/x/src.zip"},
            {"name": "RAG-knowledgebase-v9.9.9-win-x64.zip", "size": 208, "digest": "sha256:bb",
             "browser_download_url": "https://github.com/x/win.zip"},
            {"name": "RAG-knowledgebase-v9.9.9-docker.zip", "size": 2, "digest": "sha256:cc",
             "browser_download_url": "https://github.com/x/docker.zip"},
        ],
    }
    monkeypatch.setattr(S, "_gh_get", _fake_gh(200, payload))
    U.clear_cache()
    r = asyncio.new_event_loop().run_until_complete(U.check_update())
    assert r["ok"] is True
    assert r["latest"] == "9.9.9"
    assert r["update_available"] is True
    assert r["notes"].startswith("### 更新内容")

    # 资产选择按部署形态：测试环境是 source → 应选 docker.zip（源码+已构建前端）
    # 不再硬编码 win-x64，避免部署形态语义变化时测试假失败
    from app.core.config import settings

    expected = "win-x64.zip" if r["deployment"] == "desktop" else "docker.zip"
    assert r["asset"]["name"].endswith(expected), f"{r['deployment']} 应选 {expected}"
    # 源码态现在也支持应用内更新（覆盖 backend/ + web/dist 后 os.execv 重启）
    assert r["can_self_update"] is True
    assert r["current"] == settings.app_version
    U.clear_cache()


def test_check_update_same_version_no_update(monkeypatch):
    import app.services.skill_search_service as S
    monkeypatch.setattr(S, "_gh_get", _fake_gh(200, {"tag_name": "v1.0.0", "assets": []}))
    U.clear_cache()
    r = asyncio.new_event_loop().run_until_complete(U.check_update())
    assert r["ok"] is True and r["update_available"] is False
    U.clear_cache()


def test_check_update_disabled_skips_network(monkeypatch):
    """关闭检查时不得发起网络请求。"""
    import app.services.skill_search_service as S
    from app.core.config import settings

    called = {"n": 0}

    async def _spy(path, *, params=None, token=None, timeout=20):
        called["n"] += 1
        return 200, {}, {}

    monkeypatch.setattr(S, "_gh_get", _spy)
    monkeypatch.setattr(settings, "update_check_enabled", False)
    U.clear_cache()
    r = asyncio.new_event_loop().run_until_complete(U.check_update())
    assert r["disabled"] is True and r["update_available"] is False
    assert called["n"] == 0
    monkeypatch.setattr(settings, "update_check_enabled", True)
    U.clear_cache()


def test_check_update_cache_hit_avoids_second_call(monkeypatch):
    import app.services.skill_search_service as S
    called = {"n": 0}

    async def _spy(path, *, params=None, token=None, timeout=20):
        called["n"] += 1
        return 200, {"tag_name": "v1.0.0", "assets": []}, {}

    monkeypatch.setattr(S, "_gh_get", _spy)
    U.clear_cache()
    loop = asyncio.new_event_loop()
    loop.run_until_complete(U.check_update())
    r2 = loop.run_until_complete(U.check_update())
    assert called["n"] == 1, "第二次应命中缓存，不再请求"
    assert r2["cached"] is True
    # force 强制刷新
    loop.run_until_complete(U.check_update(force=True))
    assert called["n"] == 2
    U.clear_cache()


# ---------- Release 正文：剥离部署教程 ----------

def test_strip_deploy_guide_removes_section():
    """应用内只展示更新内容，部署教程段落应被剥掉。"""
    body = """## [1.0.1] - 2026-10-17

### 新增
- Docker 在线更新

---

<!-- deploy-guide:start -->
## 🚀 部署与升级

### Docker
```bash
docker compose up -d
```
<!-- deploy-guide:end -->"""
    out = U._strip_deploy_guide(body)
    assert "Docker 在线更新" in out
    assert "部署与升级" not in out
    assert "deploy-guide" not in out
    assert "docker compose up -d" not in out


def test_strip_deploy_guide_keeps_body_without_marker():
    """没有标记（旧 Release / 手写正文）时原样保留，不能误删内容。"""
    body = "## [1.0.0]\n\n- 首发"
    assert U._strip_deploy_guide(body) == body


def test_strip_deploy_guide_handles_empty():
    assert U._strip_deploy_guide("") == ""


def test_strip_deploy_guide_multiline_and_case_insensitive():
    body = "更新A\n\n<!--DEPLOY-GUIDE:START-->\n部署说明\n第二行\n<!--deploy-guide:end-->\n\n更新B"
    out = U._strip_deploy_guide(body)
    assert "更新A" in out and "更新B" in out
    assert "部署说明" not in out


# ---------- 并发合并（single-flight）----------

def test_check_update_concurrent_requests_share_one_network_call(monkeypatch):
    """并发请求只打一次 GitHub：前端多标签页/多用户同时刷新时不得放大配额消耗。"""
    import app.services.skill_search_service as S

    called = {"n": 0}

    async def _slow_gh(path, *, params=None, token=None, timeout=20):
        called["n"] += 1
        await asyncio.sleep(0.05)  # 模拟网络往返
        return 200, {"tag_name": "v1.0.0", "assets": []}, {}

    monkeypatch.setattr(S, "_gh_get", _slow_gh)
    U.clear_cache()
    U._inflight = None

    async def _run():
        return await asyncio.gather(*[U.check_update(force=True) for _ in range(5)])

    results = asyncio.new_event_loop().run_until_complete(_run())
    assert all(r["ok"] is True for r in results)
    assert called["n"] == 1, f"应合并成 1 次网络请求，实际 {called['n']} 次"
    U.clear_cache()
    U._inflight = None


def test_check_update_inflight_failure_does_not_poison_next_call(monkeypatch):
    """在途请求失败后，后续请求必须能重新发起，而不是永久拿到失败结果。"""
    import app.services.skill_search_service as S

    state = {"n": 0}

    async def _flaky(path, *, params=None, token=None, timeout=20):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("network down")
        return 200, {"tag_name": "v1.0.0", "assets": []}, {}

    monkeypatch.setattr(S, "_gh_get", _flaky)
    U.clear_cache()
    U._inflight = None
    loop = asyncio.new_event_loop()

    first = loop.run_until_complete(U.check_update(force=True))
    assert first["ok"] is False
    second = loop.run_until_complete(U.check_update(force=True))
    assert second["ok"] is True and second["latest"] == "1.0.0"
    U.clear_cache()
    U._inflight = None
