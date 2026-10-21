"""技能市场服务：把「搜索 → 预览 → 安装/更新」串起来。

复用而非重写：
- 搜索：`app.services.skill_search_service.search_skills`（自带中文映射、SKILL.md 探测、限流处理）
- 下载/解压/去重/落库：`app.services.skill_pack_service`
- SSRF 守卫：`app.agents.tools.builtin._check_url`
- GitHub 鉴权 token：`settings.github_token`（系统设置里已有）

设计要点（勿凭直觉改动）：

1. **精选清单优先，且是限流时的兜底**。未认证 GitHub search 只有 10 次/分钟且按 IP 共享，
   一个租户里几个人同时打开市场就能打满。精选清单是静态常量，永远可用。
   因此：精选命中排前面；GitHub 失败但有精选 → 返回精选 + note，**不返回 error**。

2. **预览走 raw.githubusercontent.com 而不是 API**。raw 内容不限流（CDN），
   预览 SKILL.md 正文不消耗 API 配额；但文件清单只能用 tree API（core 类），
   限流时降级为空列表 + limitations 提示，绝不假装「没有文件」。

3. **缓存是配额保护，不是性能优化**。搜索缓存 5 分钟、详情缓存 30 分钟，
   风格对齐 `app/services/update_service.py`。

4. **安装必须区分单技能/多技能仓库**。多技能仓库不带 subpath 时
   `skill_pack_service.extract_zip` 会直接抛错，所以本服务先探测再决定：
   单技能直接装、多技能交回前端让用户选（或整包装成「技能集合」）。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger("skill_market")

SEARCH_TTL = 300          # 5 分钟
DETAIL_TTL = 30 * 60      # 30 分钟

# key -> (expires_at, payload)
_search_cache: dict[tuple, tuple[float, dict]] = {}
_detail_cache: dict[tuple, tuple[float, dict]] = {}

_GH_RE = re.compile(r"https?://github\.com/([^/]+)/([^/?#]+?)(?:\.git)?/?(?:#(.+))?$")


def clear_cache() -> None:
    """清空市场缓存（测试与「刷新」按钮用）。"""
    _search_cache.clear()
    _detail_cache.clear()


# ---------------------------------------------------------------- 工具函数


def parse_github_uri(uri: str | None) -> tuple[str, str] | None:
    """把技能包来源解析为 (owner/repo, subpath)。

    兼容两种写法：
    - `https://github.com/owner/repo`（单技能，来源只记仓库）
    - `https://github.com/owner/repo#skills/data-query`（多技能，导入时用 `#subpath` 记录）
    非 GitHub 来源返回 None —— 市场只认 GitHub 来源做「已安装」匹配。
    """
    if not uri:
        return None
    m = _GH_RE.match(uri.strip())
    if not m:
        return None
    owner, repo, sub = m.group(1), m.group(2), (m.group(3) or "")
    return f"{owner}/{repo}", sub.strip("/")


def _norm_repo(repo: str) -> str:
    """归一化仓库标识：去协议/结尾斜杠/.git，小写以匹配 vikiboss/Repo 之类大小写差异。"""
    s = (repo or "").strip().lower()
    s = re.sub(r"^https?://github\.com/", "", s)
    s = re.sub(r"\.git$", "", s)
    return s.strip("/")


def _get_token() -> str | None:
    try:
        from app.core.config import settings

        return (getattr(settings, "github_token", "") or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 已安装映射


async def installed_map(db, tenant_id: int) -> dict[str, dict]:
    """本租户已安装的 GitHub 技能：key = `owner/repo` 或 `owner/repo#subpath`（小写）。

    单次查询搞定，避免搜索结果逐条查库（N+1）。
    返回 {key: {skill_id, name, version, subpath}}。
    """
    from sqlalchemy import select

    from app.models import Skill, SkillPackage

    rows = (
        await db.execute(
            select(SkillPackage, Skill)
            .join(Skill, Skill.id == SkillPackage.skill_id, isouter=True)
            .where(SkillPackage.tenant_id == tenant_id)
        )
    ).all()

    out: dict[str, dict] = {}
    for pkg, sk in rows:
        parsed = parse_github_uri(pkg.source_uri)
        if not parsed:
            continue
        repo, sub = parsed
        key = f"{_norm_repo(repo)}#{sub}" if sub else _norm_repo(repo)
        out[key] = {
            "skill_id": (sk.id if sk else pkg.skill_id),
            "name": (sk.name if sk else pkg.name),
            "version": pkg.version,
            "subpath": sub,
        }
    return out


def _lookup_installed(installed: dict[str, dict], repo: str, subpath: str) -> dict | None:
    """按 (repo, subpath) 命中已安装记录。

    多技能仓库：优先精确匹配 repo#subpath；单技能仓库：匹配裸 repo。
    注意不要跨 subpath 回退 —— 同一仓库不同子技能是两个独立技能。
    """
    nr = _norm_repo(repo)
    sub = (subpath or "").strip("/")
    if sub:
        return installed.get(f"{nr}#{sub}")
    return installed.get(nr)


# ---------------------------------------------------------------- 搜索


def _curated_candidates(keywords: str) -> list[dict]:
    from app.services.skill_market_catalog import filter_curated

    out: list[dict] = []
    for s in filter_curated(keywords):
        out.append({
            "repo": s.repo,
            "name": s.name,
            "description": s.description,
            "stars": 0,
            "html_url": f"https://github.com/{s.repo}",
            "sub_skills": [s.subpath] if s.subpath else [],
            "source": "curated",
            "installed": None,
        })
    return out


async def search_market(
    db, *, tenant_id: int, keywords: str, limit: int = 20,
) -> dict:
    """市场搜索：精选 + GitHub 实时，合并去重后按「精选优先、其次 star」排序。

    返回 {"candidates", "mapped_terms", "error", "note"}。
    限流但有精选结果时 error=None（带 note），保证界面永远有内容。
    """
    from app.services import skill_search_service as sss

    kw = (keywords or "").strip()
    limit = max(1, min(int(limit or 20), 50))
    cache_key = (_norm_repo(kw), limit)
    now = time.time()
    hit = _search_cache.get(cache_key)
    if hit and hit[0] > now:
        return hit[1]

    curated = _curated_candidates(kw)
    gh_error: str | None = None
    gh_cands: list = []
    mapped_terms: list[str] = []
    note: str | None = None

    try:
        r = await sss.search_skills(kw, token=_get_token(), limit=limit) if kw else {
            "candidates": [], "mapped_terms": [], "error": None, "note": None,
        }
        gh_cands = r.get("candidates") or []
        mapped_terms = r.get("mapped_terms") or []
        gh_error = r.get("error")
        note = r.get("note")
    except Exception as e:  # noqa: BLE001
        gh_error = f"GitHub 搜索失败：{str(e)[:150]}"
        logger.warning("market_search_failed", err=str(e)[:200])

    # 合并：精选在前；同 (repo, subpath) 只保留精选那条（精选有人工核验的描述）
    merged: dict[str, dict] = {}
    for c in curated:
        for sub in (c["sub_skills"] or [""]):
            key = f"{_norm_repo(c['repo'])}#{sub}" if sub else _norm_repo(c["repo"])
            merged[key] = {**c, "sub_skills": c["sub_skills"]}

    for cand in gh_cands:
        repo = getattr(cand, "repo", "") or ""
        if not repo:
            continue
        subs = list(getattr(cand, "sub_skills", []) or [])
        # 未探测出子技能时按裸仓库占位，安装阶段再决定单/多技能
        keys = [f"{_norm_repo(repo)}#{s}" for s in subs] or [_norm_repo(repo)]
        if any(k in merged for k in keys):
            continue  # 精选已覆盖，跳过
        merged[keys[0]] = {
            "repo": repo,
            "name": (subs[0].rsplit("/", 1)[-1] if subs else repo.split("/")[-1]),
            "description": getattr(cand, "description", "") or "",
            "stars": int(getattr(cand, "stars", 0) or 0),
            "html_url": getattr(cand, "html_url", "") or f"https://github.com/{repo}",
            "sub_skills": subs,
            "source": "github",
            "installed": None,
        }

    # 已安装标记（db 为空表示纯搜索场景，如工具调用时无需打标）
    if db is not None:
        try:
            installed = await installed_map(db, tenant_id)
            for item in merged.values():
                for sub in (item["sub_skills"] or [""]):
                    hit_inst = _lookup_installed(installed, item["repo"], sub)
                    if hit_inst:
                        item["installed"] = hit_inst
                        break
        except Exception:  # noqa: BLE001
            logger.exception("market_installed_map_failed")

    items = list(merged.values())
    if gh_error:
        if curated:
            note = "GitHub 搜索受限，当前仅显示精选技能。可在系统设置配置 GitHub Token 提升配额。"
        else:
            note = note or None

    result = {
        "candidates": items[:limit],
        "mapped_terms": mapped_terms,
        "error": None if (curated or items) else gh_error,
        "note": note,
    }
    _search_cache[cache_key] = (now + SEARCH_TTL, result)
    return result


# ---------------------------------------------------------------- 详情预览


async def _raw_skill_md(repo: str, subpath: str, *, token: str | None) -> tuple[str | None, str | None]:
    """从 raw.githubusercontent.com 读取 SKILL.md，返回 (正文, 错误)。不消耗 API 配额。

    优化（均有实测依据）：
    - **先试 main，不查默认分支**：查 `/repos/{owner}/{repo}` 要额外一次 API 往返（~0.3s）
      且消耗配额，而绝大多数技能仓库默认分支就是 main。main 失败再回退 master，
      最后才在两者都不行时查默认分支（少数用 develop/trunk 的仓库）。
    - **复用同一个 httpx client**：raw 是 CDN，两次请求走同一连接只要 ~0.08s，
      新建连接则要 ~0.3-0.7s（TLS 握手）。
    - **缩小超时**：raw 内容通常 <100KB，正常 1-2s 内完成。连接超时 5s、读超时 15s，
      避免在劣化网络下把整个请求挂到 20s+（前端已有骨架屏，快速失败比长时间挂起好）。
    """
    import httpx

    from app.agents.tools.builtin import _check_url
    from app.services.skill_pack_service import _github_default_branch, _github_repo_slug

    slug = _github_repo_slug(f"https://github.com/{repo}") or (*repo.split("/", 1),)
    owner, name = slug[0], slug[1]

    prefix = (subpath or "").strip("/")
    rel = f"{prefix}/SKILL.md" if prefix else "SKILL.md"
    last_err: str | None = None

    timeout = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        async def _try(branch: str) -> tuple[str | None, int | None]:
            url = f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{rel}"
            err = _check_url(url, None)
            if err:
                return err, None
            try:
                resp = await c.get(url, headers={"User-Agent": "rag-platform"})
            except Exception as e:  # noqa: BLE001
                return f"网络错误：{type(e).__name__}", None
            if resp.status_code == 200:
                return resp.text, 200
            return f"HTTP {resp.status_code}", resp.status_code

        for b in ("main", "master"):
            text, status = await _try(b)
            if status == 200:
                return text, None
            last_err = text

        # main/master 都不存在时才查默认分支（仅多一次 API 往返，且只发生在罕见情况）
        default = await _github_default_branch(owner, name)
        if default and default not in ("main", "master"):
            text, status = await _try(default)
            if status == 200:
                return text, None
            last_err = text

    return None, f"未找到 SKILL.md（{last_err or '未知错误'}）"


async def _tree_files(repo: str, subpath: str, *, token: str | None) -> tuple[list[dict], str | None]:
    """取技能目录下的文件清单，返回 (files, limitation)。

    两条路径（均有实测依据）：
    1. **tree API**（权威，含精确字节数），但属 core 配额 60 次/小时的共享池，
       未认证时很容易打满（实测连续预览几个技能就能耗尽）。
    2. **GitHub HTML 页面抓取**（兜底）：core 配额耗尽时解析
       `github.com/{repo}/tree/{branch}/{subpath}` 的 embedded JSON 拿文件名。
       HTML 页面不受 API 限流，但拿不到文件大小 —— 用 0 表示未知，不编造。

    限流/失败时返回 ([], 原因) —— 由调用方放进 limitations，不当作错误。
    """
    from app.services.skill_search_service import _gh_get, _rate_limited

    prefix = (subpath or "").strip("/") + "/" if subpath else ""
    last_err: str | None = None

    for branch in ("main", "master"):
        try:
            status, data, headers = await _gh_get(
                f"/repos/{repo}/git/trees/{branch}",
                params={"recursive": "1"}, token=token,
            )
        except Exception as e:  # noqa: BLE001
            last_err = f"文件清单获取失败：{str(e)[:120]}"
            continue
        if _rate_limited(status, headers):
            last_err = "文件清单不可用（GitHub 限流）"
            break  # 限流时换分支也没用，直接走 HTML 兜底
        if status != 200 or not isinstance(data, dict):
            last_err = f"文件清单不可用（HTTP {status}）"
            continue
        files: list[dict] = []
        for node in data.get("tree", []):
            if node.get("type") != "blob":
                continue
            p = str(node.get("path") or "")
            if prefix:
                if not p.startswith(prefix):
                    continue
                p = p[len(prefix):]
            if not p:
                continue
            files.append({"path": p, "size": int(node.get("size") or 0)})
        return files[:500], None

    # 兜底：HTML 页面（不受 API 配额限制，但无文件大小、只列一层目录）
    for branch in ("main", "master"):
        names, html_err = await _html_listing(repo, branch, subpath)
        if names:
            return [{"path": n, "size": 0} for n in names[:500]], (
                "GitHub API 限流：文件大小未知，子目录仅列出目录名（未展开）"
            )
        last_err = html_err or last_err
    return [], last_err or "文件清单不可用"


async def _html_listing(repo: str, branch: str, subpath: str) -> tuple[list[str], str | None]:
    """从 GitHub 仓库目录页的 embedded JSON 解析文件名（API 限流时的兜底）。

    页面把目录内容嵌在 `react-app.embeddedData` 的
    `payload.codeViewFileTreeLayoutRoute.fileTree["<subpath>"].items` 里
    （实测路径；`""` 是仓库根，其余键是各目录的绝对路径）。
    只取 `name` + `contentType`，不解析其他字段。
    """
    import json
    import re

    import httpx

    from app.agents.tools.builtin import _check_url

    sub = (subpath or "").strip("/")
    path = f"/tree/{branch}/{sub}" if sub else f"/tree/{branch}"
    url = f"https://github.com/{repo}{path}"
    err = _check_url(url, None)
    if err:
        return [], err

    timeout = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
            resp = await c.get(url, headers={"User-Agent": "rag-platform"})
    except Exception as e:  # noqa: BLE001
        return [], f"页面获取失败：{type(e).__name__}"
    if resp.status_code != 200:
        return [], f"页面 HTTP {resp.status_code}"

    # 抽取 embedded JSON（属性顺序可能随前端版本变化，故用宽松正则）
    for m in re.finditer(
        r'<script[^>]*type="application/json"[^>]*data-target="react-app\.embeddedData"[^>]*>(.*?)</script>',
        resp.text, re.DOTALL,
    ):
        try:
            payload = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        file_tree = (
            ((payload.get("payload") or {}).get("codeViewFileTreeLayoutRoute") or {}).get("fileTree")
        ) or {}
        entry = file_tree.get(sub)
        if not isinstance(entry, dict):
            continue
        names: list[str] = []
        for it in entry.get("items") or []:
            name = it.get("name")
            if not name:
                continue
            # 目录用尾斜杠标注：页面只给一层，无法递归展开；标出来比静默丢弃更诚实
            names.append(f"{name}/" if it.get("contentType") == "directory" else str(name))
        if names:
            return names, None
    return [], "页面结构无法解析（GitHub 前端可能已变更）"
    return [], "文件清单不可用（仓库分支探测失败）"


def _infer_entry_scripts(files: list[dict]) -> list[dict]:
    """按文件清单推断入口脚本（不平铺目录，只取 scripts/ tools/ 下的直接子文件）。"""
    from pathlib import Path

    from app.services.skill_runtime import SCRIPT_DIRS, SCRIPT_RUNTIMES

    out: list[dict] = []
    seen: set[str] = set()
    for f in files:
        parts = f["path"].split("/")
        if len(parts) != 2 or parts[0] not in SCRIPT_DIRS:
            continue
        rt = SCRIPT_RUNTIMES.get(Path(parts[1]).suffix.lower())
        if not rt:
            continue
        name = Path(parts[1]).stem
        if name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "path": f["path"], "description": "",
                    "runtime": rt, "args": "stdin", "outputs": []})
    return out


async def get_market_detail(repo: str, *, subpath: str = "", token: str | None = None) -> dict:
    """市场详情：SKILL.md 正文 + 文件清单 + 入口脚本。全部只读，不落盘。"""
    from app.services.skill_pack_service import parse_skill_md

    repo = (repo or "").strip()
    subpath = (subpath or "").strip("/")
    if "/" not in repo:
        raise ValueError("仓库标识格式应为 owner/repo")

    cache_key = (_norm_repo(repo), subpath)
    now = time.time()
    hit = _detail_cache.get(cache_key)
    if hit and hit[0] > now:
        return hit[1]

    token = token if token is not None else _get_token()

    # 并发：正文走 raw CDN（不限流、不耗 API 配额），文件清单走 tree API（core 配额）。
    # 两者互不依赖，串行会多付一次完整往返（实测 ~0.4s）。
    # 文件清单失败不致命（降级为 limitation），正文失败才抛错。
    import asyncio

    (text, err), (files, limitation) = await asyncio.gather(
        _raw_skill_md(repo, subpath, token=token),
        _tree_files(repo, subpath, token=token),
    )
    if err:
        raise ValueError(err)

    meta, body = parse_skill_md(text)

    limitations: list[str] = []
    if limitation:
        limitations.append(limitation)
    elif not files:
        limitations.append("未获取到文件清单（仓库可能为空或分支探测失败）")

    result = {
        "repo": repo,
        "subpath": subpath,
        "name": str(meta.get("name") or (subpath.rsplit("/", 1)[-1] if subpath else repo.split("/")[-1])),
        "description": str(meta.get("description") or ""),
        "version": str(meta["version"]) if meta.get("version") else None,
        "body_md": body,
        "files": files,
        "entry_scripts": _infer_entry_scripts(files),
        "html_url": f"https://github.com/{repo}" + (f"/tree/main/{subpath}" if subpath else ""),
        "limitations": limitations,
    }
    _detail_cache[cache_key] = (now + DETAIL_TTL, result)
    return result


# ---------------------------------------------------------------- 安装


async def install_from_market(
    db, *, tenant_id: int, owner_id: int, repo: str, subpath: str = "",
    upgrade_skill_id: int | None = None,
) -> dict:
    """从市场安装/更新一个技能。

    - upgrade_skill_id 非空 → 用原仓库地址覆盖已有技能包（行为同 /skills/{id}/upgrade）
    - 指定 subpath → 装该子技能
    - 未指定 subpath → 先探测：单技能直接装；多技能返回 multi_skill 由前端选择
    """
    from app.core.errors import ValidationError
    from app.services import skill_pack_service as sps

    repo = (repo or "").strip()
    subpath = (subpath or "").strip("/")
    if "/" not in repo:
        raise ValidationError("仓库标识格式应为 owner/repo")
    url = f"https://github.com/{repo}"

    if upgrade_skill_id is not None:
        _, pkg = await sps.upgrade_from_url(
            db, tenant_id=tenant_id, owner_id=owner_id, url=url,
            skill_id=upgrade_skill_id, subpath=subpath or None,
        )
        return {"ok": True, "upgraded": True, "skill_id": upgrade_skill_id,
                "package_id": pkg.id, "version": pkg.version, "files": len(pkg.files or [])}

    if subpath:
        return await sps.import_from_url(
            db, tenant_id=tenant_id, owner_id=owner_id, url=url, subpath=subpath,
        )

    # 未指定子技能：下载后探测，单技能自动装，多技能交回前端
    data = await sps.fetch_url_zip(url)
    subs = sps.skill_subdirs(data)
    if len(subs) > 1:
        return {"multi_skill": True, "repo": repo,
                "sub_skills": subs,
                "message": f"该仓库含 {len(subs)} 个技能，请用 subpath 指定其中一个"}
    return await sps.import_from_url(
        db, tenant_id=tenant_id, owner_id=owner_id, url=url,
    )


async def install_collection_from_market(
    db, *, tenant_id: int, owner_id: int, repo: str,
) -> dict:
    """整包安装多技能仓库为「技能集合」（父技能 + N 个子技能）。"""
    from app.core.errors import ValidationError
    from app.services import skill_pack_service as sps

    repo = (repo or "").strip()
    if "/" not in repo:
        raise ValidationError("仓库标识格式应为 owner/repo")
    return await sps.import_collection_from_url(
        db, tenant_id=tenant_id, owner_id=owner_id, url=f"https://github.com/{repo}",
    )
