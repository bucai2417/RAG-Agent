"""技能市场服务：精选兜底、合并去重、限流降级、已安装映射、详情预览。

全部 mock，不依赖真实网络（`_gh_get` / `search_skills` / raw 请求均被替换）。
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import skill_market_catalog as C
from app.services import skill_market_service as M


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------- 精选清单 ----------------
def test_curated_filter_empty_returns_all():
    assert len(C.filter_curated("")) == len(C.CURATED_SKILLS)


def test_curated_filter_by_tag():
    names = [s.name for s in C.filter_curated("油价")]
    assert "data-query" in names


def test_curated_filter_unknown_returns_empty():
    assert C.filter_curated("zzz-不存在的能力") == []


def test_curated_all_have_subpath():
    """全部是多技能仓库条目，subpath 必须非空，否则安装会因「包含多个技能」失败。"""
    assert all(s.subpath for s in C.CURATED_SKILLS)


# ---------------- URI 解析 ----------------
def test_parse_github_uri_plain():
    assert M.parse_github_uri("https://github.com/acme/fuel") == ("acme/fuel", "")


def test_parse_github_uri_with_subpath():
    assert M.parse_github_uri("https://github.com/acme/skills#skills/data-query") == (
        "acme/skills", "skills/data-query")


def test_parse_github_uri_non_github():
    assert M.parse_github_uri("zip") is None
    assert M.parse_github_uri("https://example.com/x.zip") is None
    assert M.parse_github_uri(None) is None


# ---------------- 搜索 ----------------
def _patch_search(monkeypatch, *, cands, error=None, note=None):
    from app.services import skill_search_service as sss

    async def _fake(kw, *, token=None, limit=5, **kwargs):
        return {"candidates": cands, "mapped_terms": [], "error": error, "note": note}

    monkeypatch.setattr(sss, "search_skills", _fake)


def test_search_curated_first(monkeypatch):
    from app.services.skill_search_service import Candidate

    _patch_search(monkeypatch, cands=[
        Candidate(repo="someone/other", description="unrelated", stars=999,
                  html_url="https://github.com/someone/other", sub_skills=["skills/x"]),
    ])
    M.clear_cache()
    r = _run(M.search_market(None, tenant_id=1, keywords="油价"))
    assert r["candidates"][0]["source"] == "curated"
    assert r["candidates"][0]["name"] == "data-query"


def test_search_dedupes_curated_and_github(monkeypatch):
    from app.services.skill_search_service import Candidate

    _patch_search(monkeypatch, cands=[
        Candidate(repo="vikiboss/60s-skills", description="dup", stars=47,
                  html_url="https://github.com/vikiboss/60s-skills",
                  sub_skills=["skills/data-query"]),
    ])
    M.clear_cache()
    r = _run(M.search_market(None, tenant_id=1, keywords="油价"))
    repos = [(c["repo"], c["source"]) for c in r["candidates"]]
    # 同一 repo#subpath 只出现一次，且保留精选那条
    assert repos.count(("vikiboss/60s-skills", "curated")) == 1
    assert all(s != "github" for _, s in repos if _ == "vikiboss/60s-skills")


def test_search_rate_limited_falls_back_to_curated(monkeypatch):
    _patch_search(monkeypatch, cands=[], error="GitHub 搜索接口限流")
    M.clear_cache()
    r = _run(M.search_market(None, tenant_id=1, keywords="天气"))
    assert r["error"] is None, "有精选兜底时不应报错"
    assert r["note"] and "受限" in r["note"]
    assert r["candidates"]


def test_search_rate_limited_no_curated_returns_error(monkeypatch):
    _patch_search(monkeypatch, cands=[], error="GitHub 搜索接口限流")
    M.clear_cache()
    r = _run(M.search_market(None, tenant_id=1, keywords="zzz-不存在的能力"))
    assert r["error"]
    assert not r["candidates"]


def test_search_cache_hit(monkeypatch):
    calls = {"n": 0}

    async def _fake(kw, *, token=None, limit=5, **kwargs):
        calls["n"] += 1
        return {"candidates": [], "mapped_terms": [], "error": None, "note": None}

    from app.services import skill_search_service as sss
    monkeypatch.setattr(sss, "search_skills", _fake)
    M.clear_cache()
    _run(M.search_market(None, tenant_id=1, keywords="天气"))
    _run(M.search_market(None, tenant_id=1, keywords="天气"))
    assert calls["n"] == 1, "第二次应命中缓存"


def test_search_empty_keywords_returns_curated_without_github(monkeypatch):
    calls = {"n": 0}

    async def _fake(kw, *, token=None, limit=5, **kwargs):
        calls["n"] += 1
        return {"candidates": [], "mapped_terms": [], "error": None, "note": None}

    from app.services import skill_search_service as sss
    monkeypatch.setattr(sss, "search_skills", _fake)
    M.clear_cache()
    r = _run(M.search_market(None, tenant_id=1, keywords="", limit=50))
    assert calls["n"] == 0, "空关键词不该打 GitHub"
    assert len(r["candidates"]) == len(C.CURATED_SKILLS)


# ---------------- 详情 ----------------
def test_detail_branch_fallback(monkeypatch):
    """main 404 → 回退 master 成功。"""
    import httpx

    async def _fake_default(owner, repo):
        return None

    async def _fake_raw(repo, subpath, *, token):
        return "---\nname: demo\ndescription: 演示\n---\n正文", None

    monkeypatch.setattr(M, "_raw_skill_md", _fake_raw)

    async def _tree(repo, subpath, *, token):
        return [{"path": "SKILL.md", "size": 10}], None

    monkeypatch.setattr(M, "_tree_files", _tree)
    M.clear_cache()
    d = _run(M.get_market_detail("acme/demo"))
    assert d["name"] == "demo"
    assert d["body_md"].strip() == "正文"


def test_detail_tree_rate_limited_sets_limitation(monkeypatch):
    async def _fake_raw(repo, subpath, *, token):
        return "---\nname: demo\n---\n正文", None

    async def _tree(repo, subpath, *, token):
        return [], "文件清单不可用（GitHub 限流）"

    monkeypatch.setattr(M, "_raw_skill_md", _fake_raw)
    monkeypatch.setattr(M, "_tree_files", _tree)
    M.clear_cache()
    d = _run(M.get_market_detail("acme/demo"))
    assert d["files"] == []
    assert any("限流" in x for x in d["limitations"])


def test_detail_raw_failure_raises(monkeypatch):
    async def _fake_raw(repo, subpath, *, token):
        return None, "未找到 SKILL.md（HTTP 404）"

    monkeypatch.setattr(M, "_raw_skill_md", _fake_raw)
    M.clear_cache()
    with pytest.raises(ValueError):
        _run(M.get_market_detail("acme/nope"))


def test_detail_invalid_repo():
    M.clear_cache()
    with pytest.raises(ValueError):
        _run(M.get_market_detail("not-a-repo"))


# ---------------- 入口脚本推断 ----------------
def test_infer_entry_scripts():
    files = [
        {"path": "SKILL.md", "size": 1},
        {"path": "scripts/run.py", "size": 1},
        {"path": "tools/build.mjs", "size": 1},
        {"path": "scripts/notes.txt", "size": 1},
        {"path": "scripts/nested/deep.py", "size": 1},  # 非直接子文件，忽略
    ]
    names = {s["name"]: s["runtime"] for s in M._infer_entry_scripts(files)}
    assert names == {"run": "python", "build": "node"}


# ---------------- 已安装映射 ----------------
class _Pkg:
    def __init__(self, source_uri, version="1.0"):
        self.source_uri = source_uri
        self.version = version
        self.skill_id = 7
        self.name = "pkg"


class _Skill:
    def __init__(self, id_=7, name="pkg-skill"):
        self.id = id_
        self.name = name


def test_installed_map_parses_subpath(monkeypatch):
    class _Res:
        def all(self):
            return [
                (_Pkg("https://github.com/acme/skills#skills/data-query"), _Skill(7, "dq")),
                (_Pkg("https://github.com/acme/single"), _Skill(8, "single")),
                (_Pkg("zip-file.zip"), _Skill(9, "zip")),
            ]

    class _DB:
        async def execute(self, *a, **k):
            return _Res()

    m = _run(M.installed_map(_DB(), tenant_id=1))
    assert m["acme/skills#skills/data-query"]["skill_id"] == 7
    assert m["acme/single"]["skill_id"] == 8
    assert len(m) == 2, "非 GitHub 来源不应进映射"


def test_lookup_installed_does_not_cross_subpaths():
    installed = {"acme/skills#skills/a": {"skill_id": 1}, "acme/skills#skills/b": {"skill_id": 2}}
    assert M._lookup_installed(installed, "acme/skills", "skills/a")["skill_id"] == 1
    assert M._lookup_installed(installed, "acme/skills", "skills/b")["skill_id"] == 2
    assert M._lookup_installed(installed, "acme/skills", "") is None
