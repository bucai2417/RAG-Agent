"""技能搜索服务：把用户的中文能力词转成有效检索词，搜索可安装的技能包（SKILL.md）。

设计要点（均经实测确认，勿凭直觉改动）：

1. **中文直接搜 GitHub 基本无召回**（`q=油价 skill` → 0 结果），必须先做中→英能力词映射。
2. **不能把 `SKILL.md` 当搜索词**（`q=fuel price SKILL.md` → 0 结果）：GitHub 按字面匹配，
   技能仓库 README 通常不含该字符串，加了反而毁掉召回。正确做法是先用能力词搜仓库，
   再对候选探测 `**/SKILL.md`（走 tree API，属 core 类，配额比 search 宽松）来过滤出真技能仓库。
3. **未认证 search API 仅 10 次/分钟且 IP 共享**，极易打满。查询数要克制（≤3 条），
   探测仅对 top-N 候选做；限流时返回明确错误，绝不编造结果。

搜索源设计为可插拔（`_SOURCES`），后续加"市场源"只需追加一个实现 `search()` 的源。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol

from app.core.logging import get_logger

logger = get_logger("skill_search")

GITHUB_API = "https://api.github.com"

# 中→英能力词映射（纯数据；后续可增量维护）。键为中文能力词，值为英文检索词。
_ZH_EN_HINTS: dict[str, list[str]] = {
    "油价": ["fuel price", "gas price"],
    "油价查询": ["fuel price"],
    "天气": ["weather"],
    "天气预报": ["weather forecast"],
    "汇率": ["exchange rate"],
    "新闻": ["news"],
    "日报": ["daily news"],
    "热搜": ["trending", "hot search"],
    "热榜": ["trending"],
    "股票": ["stock price"],
    "金价": ["gold price"],
    "翻译": ["translate"],
    "日历": ["calendar"],
    "农历": ["lunar calendar"],
    "快递": ["express tracking", "parcel"],
    "论文": ["paper", "arxiv"],
    "地图": ["map geocode"],
    "百科": ["encyclopedia"],
    "菜谱": ["recipe"],
    "票价": ["ticket price"],
    "历史": ["history"],
    "化学元素": ["chemical element"],
    "壁纸": ["wallpaper"],
    "二维码": ["qrcode"],
    "票房": ["box office"],
    "游戏": ["game"],
    "办公": ["office document"],
    "总结": ["summarize"],
    "摘要": ["summarize"],
    "翻译文档": ["translate document"],
}


@dataclass
class Candidate:
    repo: str  # owner/repo
    description: str = ""
    stars: int = 0
    html_url: str = ""
    sub_skills: list[str] = field(default_factory=list)  # ["skills/data-query", ...]


def map_keywords(keywords: str) -> list[str]:
    """中文能力词 → 一组英文检索词。

    - 命中映射表：返回映射词（可多个）。
    - 纯 ASCII/英文：原样返回（去首尾+小写）。
    - 未命中中文：返回原词（低频兜底，靠后续 in:readme 等，但召回可能偏低）。
    """
    kw = (keywords or "").strip()
    if not kw:
        return []
    out: list[str] = []
    for zh, ens in _ZH_EN_HINTS.items():
        if zh in kw:
            for e in ens:
                if e not in out:
                    out.append(e)
    if out:
        return out
    # 纯英文/数字：直通
    if all(ord(c) < 128 for c in kw):
        return [kw.lower()]
    # 未命中的中文：原词兜底
    return [kw]


def build_queries(terms: list[str]) -> list[str]:
    """把检索词转成 GitHub search 的 q 组合（克制在 2-3 条，省配额）。

    关键（实测）：
    - **不能把 `SKILL.md` 当搜索词**（会毁掉召回）。
    - 用 `in:name,description` 限定，比默认（含 readme）更能命中**能力型技能仓库**；
      默认搜索会把"README 里提到该词的无关大仓库"顶上来（如搜 fuel price 出以太坊 gas price）。
    - 追加一个带 `skill` 的查询，专门捞名字/描述含 skill 的能力型仓库。
    """
    queries: list[str] = []
    for t in terms[:1]:
        t = t.strip()
        if not t:
            continue
        queries.append(f"{t} in:name,description")
        queries.append(f"{t} skill")
    if terms:
        t = terms[0].strip()
        if t:
            queries.append(f"{t} agent skill")
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out[:3]


async def _gh_get(path: str, *, params: dict | None = None, token: str | None = None, timeout: int = 20):
    """带 SSRF 校验 + 可选鉴权的 GitHub GET。返回 (status_code, json|None, headers)。

    SSRF 校验走 `_check_url_async`：同步版里的 `socket.getaddrinfo` 会阻塞事件循环。
    """
    import httpx

    from app.agents.tools.builtin import _check_url_async

    url = f"{GITHUB_API}{path}"
    err = await _check_url_async(url, None)  # 只校验协议与内网（github.com 为公网，通过）
    if err:
        raise ValueError(err)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "rag-platform"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        resp = await c.get(url, params=params, headers=headers)
        data = None
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = None
        return resp.status_code, data, resp.headers


def _rate_limited(status: int, headers) -> bool:
    if status == 403 and str(headers.get("x-ratelimit-remaining", "")) == "0":
        return True
    return status in (429,)


class SkillSource(Protocol):
    """技能搜索源协议（为后续「市场源」预留）。"""

    async def search(self, terms: list[str], *, token: str | None, per_page: int) -> tuple[list[Candidate], str | None]: ...


class GitHubRepoSource:
    """从 GitHub 仓库搜索发现技能源。"""

    name = "github"

    async def search(self, terms: list[str], *, token: str | None, per_page: int) -> tuple[list[Candidate], str | None]:
        queries = build_queries(terms)
        merged: dict[str, Candidate] = {}
        for q in queries:
            try:
                status, data, headers = await _gh_get(
                    "/search/repositories",
                    params={"q": q, "per_page": per_page, "sort": "stars", "order": "desc"},
                    token=token,
                )
            except Exception as e:  # noqa: BLE001
                return [], f"搜索请求失败：{str(e)[:150]}"
            if _rate_limited(status, headers):
                return [], ("GitHub 搜索接口限流（未认证仅 10 次/分钟）。"
                            "请稍后重试，或在系统设置里配置 GitHub Token 提升配额。")
            if status != 200 or not isinstance(data, dict):
                continue
            for it in data.get("items", []):
                repo = it.get("full_name")
                if not repo:
                    continue
                if repo not in merged:
                    merged[repo] = Candidate(
                        repo=repo,
                        description=(it.get("description") or "")[:300],
                        stars=int(it.get("stargazers_count") or 0),
                        html_url=it.get("html_url") or f"https://github.com/{repo}",
                    )
        cands = sorted(merged.values(), key=lambda c: c.stars, reverse=True)
        return cands, None


# 搜索源清单（可插拔）。后续「市场源」在此追加即可。
_SOURCES: list[SkillSource] = [GitHubRepoSource()]


async def probe_skill_md(repo: str, *, token: str | None = None) -> list[str]:
    """探测仓库内所有 SKILL.md 所在目录（tree API，core 类配额）。

    返回可作 subpath 的目录列表；空列表表示该仓库无 SKILL.md（不是技能仓库）。
    """
    for branch in ("main", "master"):
        try:
            status, data, headers = await _gh_get(
                f"/repos/{repo}/git/trees/{branch}",
                params={"recursive": "1"}, token=token,
            )
        except Exception:  # noqa: BLE001
            return []
        if status == 200 and isinstance(data, dict):
            dirs: list[str] = []
            for node in data.get("tree", []):
                p = str(node.get("path") or "")
                if p.upper().endswith("SKILL.MD"):
                    parent = p.rsplit("/", 1)[0] if "/" in p else ""
                    dirs.append(parent)
            return dirs
        if _rate_limited(status, headers):
            return []
    return []


async def search_skills(
    keywords: str, *, token: str | None = None, limit: int = 5, probe_top: int = 12,
) -> dict:
    """主入口：映射关键词 → 多源搜索 → 合并去重 → 探测 SKILL.md 过滤 → 返回候选。

    排序策略（重要）：把**探测确认含 SKILL.md 的仓库排在前面**，其次才按 star。
    原因：真技能仓库（如 vikiboss/60s-skills）star 常很低，纯按 star 排序会被
    "README 里恰好提到该词的大仓库"淹没（如搜 fuel price 出以太坊 gas price）。
    probe_top 默认 12：多探几个才能把被淹没的真技能捞出来（每次探测 1 request）。

    返回 {"candidates": [...], "mapped_terms": [...], "error": str|None, "note": str|None}
    """
    kw = (keywords or "").strip()
    if not kw:
        return {"candidates": [], "mapped_terms": [], "error": "请提供搜索关键词", "note": None}

    terms = map_keywords(kw)
    note = None
    if terms == [kw] and not all(ord(c) < 128 for c in kw):
        note = "未命中中→英能力词映射，召回可能偏低；可换个更通用的说法或直接给英文关键词。"

    merged: dict[str, Candidate] = {}
    error: str | None = None
    for src in _SOURCES:
        cands, err = await src.search(terms, token=token, per_page=30)
        if err:
            error = err
        for c in cands:
            if c.repo not in merged:
                merged[c.repo] = c

    # 探测池排序：**名字/描述含 skill 关键词的优先**（真技能仓库强信号），其次按 star。
    # 关键：真技能仓库（如 vikiboss/60s-skills ⭐47、*-skill ⭐0）star 极低，
    # 纯按 star 排序会被"README 恰好提到该词的大仓库"挤掉探测机会。
    def _pool_key(c: Candidate) -> tuple:
        blob = (c.repo + " " + (c.description or "")).lower()
        skillish = ("skill" in blob) or ("claud" in blob) or ("agent" in blob)
        return (skillish, c.stars)
    pool = sorted(merged.values(), key=_pool_key, reverse=True)[: max(probe_top, limit)]

    # 探测候选是否含 SKILL.md（确认真技能仓库）+ 取子技能目录。
    # 提前终止：已确认足够多的真技能仓库（>= limit）就停，省配额（每次探测 1 request）。
    confirmed = 0
    for c in pool:
        if confirmed >= max(limit, 3):
            break
        try:
            dirs = await asyncio.wait_for(probe_skill_md(c.repo, token=token), timeout=15)
        except Exception:  # noqa: BLE001
            dirs = []
        c.sub_skills = dirs
        if dirs:
            confirmed += 1

    # 排序：确认含 SKILL.md 的优先（真技能仓库），其次按 star
    pool.sort(key=lambda c: (len(c.sub_skills) > 0, c.stars), reverse=True)
    if error and not any(c.sub_skills for c in pool):
        # 完全没找到真技能且发生限流 → 报错更诚实
        return {"candidates": [], "mapped_terms": terms, "error": error, "note": note}
    return {"candidates": pool[:limit], "mapped_terms": terms, "error": None, "note": note}
