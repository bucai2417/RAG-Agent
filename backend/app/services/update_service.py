"""更新检查服务：查询 GitHub Release，判断是否有新版本；桌面版可下载并应用更新。

设计要点（勿凭直觉改动）：

1. **复用 `skill_search_service._gh_get`** —— 它已带 SSRF 校验（`_check_url`，放行
   github.com 等公网）与限流识别，无需另造一套出站逻辑。
2. **不引入 `packaging` 依赖**：仓库 `requirements.txt` 里没有，且更新检查是
   安全路径，不该为它新增依赖。自写语义化版本比较即可满足 `1.0.10 > 1.0.9`。
3. **未认证 GitHub API 仅 60 次/小时/IP**，故加内存缓存 + ETag 透传（304 不扣配额）。
4. **优雅降级是硬要求**：离线/限流/超时一律返回 `ok=False`，绝不抛 500、
   绝不臆造版本号（臆造会让用户看到不存在的「新版本」）。
5. 部署形态决定「更新」的含义：只有桌面版能自更新；Docker/源码只回报可执行命令。
6. **Windows 不允许覆盖运行中的 exe**：本模块只负责「下载 → 校验 → 解压到暂存区 →
   写信号文件」，真正的换盘必须由应用退出后的外部脚本（`apply_update.bat`）执行。
   见 `write_apply_script()`。
"""
from __future__ import annotations

import asyncio
import re
import time

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("update")

GITHUB_API = "https://api.github.com"

# 缓存 TTL（秒）：避免每次打开页面都打 GitHub（未认证仅 60 次/小时）
_CACHE_TTL = 15 * 60
_cache: dict[str, object] = {"ts": 0.0, "data": None}

# 并发的 check_update 合并成一个在途请求。
#
# 为什么需要：前端 MainLayout 每次挂载都调一次，多标签页/多用户同时刷新时会有
# N 个请求同时穿过缓存（冷启动缓存必空），各自打一次 GitHub。实测 5 并发
# force=true 的尾延迟从 0.34s 涨到 1.4s，未认证配额（60 次/小时）也浪费得很快。
# 用一个 asyncio.Task 做 single-flight：第一个请求负责取，其余等它的结果。
_inflight: asyncio.Task | None = None

# 预发布标记：默认不计为「有更新」，避免把 rc/beta 推给普通用户
_PRERELEASE_RE = re.compile(r"-")


def _parse_version(v: str) -> tuple[int, ...] | None:
    """把 'v1.2.3' / '1.2.3-rc1' 解析为可比较的元组。无法解析返回 None。"""
    s = (v or "").strip().lstrip("vV")
    # 只取主版本部分（忽略 -rc1 / +build 等后缀）
    core = re.split(r"[-+]", s, maxsplit=1)[0]
    if not core:
        return None
    parts = core.split(".")
    out: list[int] = []
    for p in parts:
        if not p.isdigit():
            return None
        out.append(int(p))
    return tuple(out) if out else None


def is_newer(latest: str, current: str) -> bool:
    """latest 是否比 current 新。任一侧不可解析时返回 False（保守，不误报更新）。"""
    a = _parse_version(latest)
    b = _parse_version(current)
    if a is None or b is None:
        return False
    # 补齐长度后逐段比较（1.0 → 1.0.0）
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return a > b


def is_prerelease(tag: str) -> bool:
    """tag 是否为预发布（含 -rc/-beta/-alpha 等）。"""
    return bool(_PRERELEASE_RE.search((tag or "").strip().lstrip("vV")))


def detect_deployment() -> str:
    """判定部署形态：desktop（可自更新）/ docker / source。"""
    import os
    import sys
    from pathlib import Path

    # Docker：存在 /.dockerenv 或 /proc/1/cgroup 含 docker/kubepods
    try:
        if Path("/.dockerenv").exists():
            return "docker"
        if Path("/proc/1/cgroup").exists():
            txt = Path("/proc/1/cgroup").read_text(errors="ignore")
            if "docker" in txt or "kubepods" in txt:
                return "docker"
    except Exception:  # noqa: BLE001
        pass
    if os.environ.get("RAG_DEPLOYMENT", "").strip() in ("desktop", "docker", "source"):
        return os.environ["RAG_DEPLOYMENT"].strip()
    # 冻结态（PyInstaller）视为桌面版；桌面版由 Electron 注入 RAG_DATA_DIR
    if getattr(sys, "frozen", False):
        return "desktop"
    return "source"


def _hint_for(deployment: str, version: str, image: str) -> str:
    """不同部署形态对应的人工更新方式。"""
    if deployment == "docker":
        return (
            "在「关于/更新」中点击「立即更新」：会下载源码包覆盖容器内 /app 并自动重启"
            "（依赖有变化时自动 pip install）。\n"
            "也可在宿主机手动升级镜像：docker compose pull && docker compose up -d\n"
            "数据位于挂载卷 ./rag-web/backend/data，更新或重建容器都不会丢失"
        )
    if deployment == "source":
        return (
            "在「关于/更新」中点击「立即更新」：会下载源码包覆盖 backend/ 与 web/dist/ "
            "并自动重启（依赖有变化时自动 pip install）。也可手动："
            "git pull && cd web && npm ci && npm run build，然后重启后端"
        )
    return "在「关于/更新」中点击「立即更新」，下载完成后会自动重启应用"


def _strip_deploy_guide(body: str) -> str:
    """剥掉 Release 正文里的「部署教程」段落，只留更新内容。

    CI 生成的 Release 正文 = 更新内容 + 部署教程，用 HTML 注释标记分隔：
      <!-- deploy-guide:start --> ... <!-- deploy-guide:end -->
    应用内只需要「改了什么」，部署说明在 GitHub 页面看即可。
    若没有标记（旧版本 Release 或手写正文），原样返回。

    注意：标记本身在 GitHub 页面不渲染，所以对页面展示无副作用。
    """
    import re

    if not body:
        return body
    cleaned = re.sub(
        r"<!--\s*deploy-guide:start\s*-->.*?<!--\s*deploy-guide:end\s*-->",
        "",
        body,
        flags=re.S | re.I,
    )
    # 去掉因此留下的分隔线/多余空行
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    cleaned = re.sub(r"-{3,}\s*$", "", cleaned).strip()
    return cleaned


def _pick_asset(assets: list[dict], deployment: str = "desktop") -> dict | None:
    """按部署形态挑选更新包。

    - desktop → *win-x64.zip（PyInstaller/Electron 桌面包）
    - docker  → *docker.zip（后端源码 + 已构建的 web/dist，容器内可直接覆盖）
    - source  → 同样用 *docker.zip（同为「源码 + 已构建前端」，只是覆盖到项目目录）
    两种包都要求是 zip；找不到返回 None。
    """
    want = "docker" if deployment in ("docker", "source") else "win-x64"
    for a in assets:
        name = (a.get("name") or "").lower()
        if name.endswith(".zip") and want in name:
            return a
    return None


async def check_update(*, force: bool = False) -> dict:
    """检查是否有新版本。永不抛异常，失败时返回 {"ok": False, "error": ...}。

    并发安全：同进程内的并发调用合并成一次 GitHub 请求（single-flight），
    避免多标签页/多用户同时刷新时把未认证配额（60 次/小时）打光。
    """
    global _inflight

    # 1) 缓存命中直接返回（非强制刷新）
    if not force:
        cached = _cache.get("data")
        if (
            cached is not None
            and _cache.get("repo") == _repo_name()
            and (time.time() - float(_cache.get("ts") or 0)) < _CACHE_TTL
        ):
            return {**cached, "cached": True}  # type: ignore[arg-type]

    # 2) 已有在途请求：等它，不重复打 GitHub
    if _inflight is not None and not _inflight.done():
        try:
            result = await asyncio.shield(_inflight)
            return {**result, "cached": True}
        except Exception:  # noqa: BLE001
            pass  # 在途请求异常：落到下面自己取一次

    # 3) 由本次请求负责取
    task = asyncio.create_task(_check_update_uncached())
    _inflight = task
    try:
        return await task
    finally:
        if _inflight is task:
            _inflight = None


def _repo_name() -> str:
    return (getattr(settings, "update_repo", "") or "241793/RAG-Agent").strip()


async def _check_update_uncached() -> dict:
    """真正做 GitHub 查询的分支（不带缓存判断）。"""
    from app.services.skill_search_service import _gh_get, _rate_limited

    current = settings.app_version
    deployment = detect_deployment()
    repo = _repo_name()
    image = f"ghcr.io/{repo.lower()}"

    base = {
        "ok": True,
        "current": current,
        "deployment": deployment,
        "repo": repo,
        "releases_url": f"https://github.com/{repo}/releases",
    }

    if not getattr(settings, "update_check_enabled", True):
        return {**base, "update_available": False, "latest": current,
                "disabled": True, "error": "更新检查已在系统设置中关闭"}

    token = getattr(settings, "github_token", "") or None
    try:
        status, data, headers = await _gh_get(
            f"/repos/{repo}/releases/latest", token=token, timeout=15
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("update_check_failed", err=str(e)[:200])
        return {**base, "ok": False, "update_available": False, "latest": None,
                "error": f"无法连接更新服务：{str(e)[:120]}"}

    if _rate_limited(status, headers):
        return {**base, "ok": False, "update_available": False, "latest": None,
                "error": "GitHub API 已限流，请稍后再试或在系统设置中配置 GitHub Token"}

    if status == 404:
        return {**base, "ok": False, "update_available": False, "latest": None,
                "error": f"未找到仓库 {repo} 的 Release（仓库需为公开；私有仓库请配置 GitHub Token）"}

    if status != 200 or not isinstance(data, dict):
        return {**base, "ok": False, "update_available": False, "latest": None,
                "error": f"更新服务返回异常（HTTP {status}）"}

    tag = (data.get("tag_name") or "").strip()
    latest_ver = tag.lstrip("vV")
    notes = _strip_deploy_guide((data.get("body") or "").strip())
    assets = data.get("assets") or []
    asset = _pick_asset(assets, deployment)
    prerelease = bool(data.get("prerelease")) or is_prerelease(tag)

    result = {
        **base,
        "latest": latest_ver,
        "tag": tag,
        "update_available": is_newer(latest_ver, current),
        "prerelease": prerelease,
        "published_at": data.get("published_at"),
        "notes": notes[:8000],
        "html_url": data.get("html_url"),
        "can_self_update": deployment in ("desktop", "docker", "source") and asset is not None,
        "asset": (
            {
                "name": asset.get("name"),
                "size": asset.get("size"),
                "digest": asset.get("digest"),
                "url": asset.get("browser_download_url"),
            }
            if asset
            else None
        ),
        "hint": _hint_for(deployment, latest_ver, image),
    }

    _cache.update({"ts": time.time(), "data": result, "repo": repo})
    return {**result, "cached": False}


def clear_cache() -> None:
    """清空缓存（测试与手动刷新用）。"""
    _cache.update({"ts": 0.0, "data": None, "repo": None})


# ==================== 下载 / 校验 / 应用（桌面版自更新）====================

# 进度状态（进程内，供前端轮询）。单机自更新场景无需持久化。
_progress: dict[str, object] = {
    "phase": "idle",   # idle | downloading | verifying | extracting | ready | failed
    "total": 0,
    "received": 0,
    "error": None,
    "version": None,
}

# 允许下载的主机（GitHub 发布包会 302 到 objects.githubusercontent.com）
_ALLOWED_DL_HOSTS = ("github.com", "objects.githubusercontent.com", "codeload.github.com")


def _updates_root():
    """更新工作目录：放在可写数据目录下（桌面版 = %APPDATA%/RAG知识库/updates）。

    刻意不放安装目录 —— 否则替换安装目录时会连自己一起覆盖。
    """
    from app.core.config import BASE_DIR

    d = BASE_DIR / "updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_progress() -> dict:
    return dict(_progress)


def reset_progress() -> None:
    """重置进度（放弃更新时调用）。"""
    _progress.update({"phase": "idle", "total": 0, "received": 0, "error": None, "version": None})


def is_safe_version(v: str) -> bool:
    """版本号白名单：仅允许数字与点（如 1.0.10）。

    该值会进入路径（staging/vX.Y.Z）与脚本参数，必须严格限制，
    否则 '1.0/../../evil' 之类可造成路径穿越或参数注入。
    """
    return bool(re.fullmatch(r"\d+(\.\d+)*", (v or "").strip()))


def _set_progress(**kw) -> None:
    _progress.update(kw)


def _check_download_url_scheme(url: str) -> str | None:
    """下载 URL 的纯字符串校验部分（不碰网络，可在事件循环里直接调）。"""
    from urllib.parse import urlparse

    p = urlparse(url)
    if p.scheme != "https":
        return f"更新包必须使用 https（实际 {p.scheme}）"
    host = (p.hostname or "").lower()
    if host not in _ALLOWED_DL_HOSTS:
        return f"更新包主机不在白名单内：{host}"
    return None


async def _check_download_url(url: str) -> str | None:
    """下载前的 URL 校验：仅 https + 白名单主机 + 过 SSRF 守卫。

    异步版：DNS 解析走线程池，避免下载前的校验拖住事件循环。
    """
    from app.agents.tools.builtin import _check_url_async

    err = _check_download_url_scheme(url)
    if err:
        return err
    return await _check_url_async(url, None)


async def download_update(*, url: str, digest: str | None, version: str) -> dict:
    """流式下载更新包到 updates/download/ 并校验 sha256。永不抛异常。

    208MB 必须流式写盘，不得整体读入内存。
    """
    import hashlib

    import httpx

    err = await _check_download_url(url)
    if err:
        _set_progress(phase="failed", error=err)
        return {"ok": False, "error": err}

    dl_dir = _updates_root() / "download"
    dl_dir.mkdir(parents=True, exist_ok=True)
    # 文件名固定，避免归档；版本号已由调用方校验
    target = dl_dir / "update.zip"

    expect = None
    if digest and digest.startswith("sha256:"):
        expect = digest.split(":", 1)[1].strip().lower()

    _set_progress(phase="downloading", total=0, received=0, error=None, version=version)
    h = hashlib.sha256()
    received = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0), follow_redirects=True) as c:
            async with c.stream("GET", url) as resp:
                if resp.status_code != 200:
                    msg = f"下载失败：HTTP {resp.status_code}"
                    _set_progress(phase="failed", error=msg)
                    return {"ok": False, "error": msg}
                # 重定向后的**最终 URL 也必须校验** —— 否则恶意 Release 可先指向
                # 白名单主机、再 302 到任意地址，绕过上面的初始校验。
                final = str(resp.url)
                err2 = await _check_download_url(final)
                if err2:
                    msg = f"下载被重定向到非白名单地址，已中止：{err2}"
                    logger.error("update_redirect_blocked", final=final[:200])
                    _set_progress(phase="failed", error=msg)
                    return {"ok": False, "error": msg}
                total = int(resp.headers.get("content-length") or 0)
                _set_progress(total=total)
                with open(target, "wb") as f:
                    async for chunk in resp.aiter_bytes(1024 * 256):
                        f.write(chunk)
                        h.update(chunk)
                        received += len(chunk)
                        _set_progress(received=received)
    except Exception as e:  # noqa: BLE001
        msg = f"下载中断：{str(e)[:150]}"
        logger.warning("update_download_failed", err=msg)
        _set_progress(phase="failed", error=msg)
        return {"ok": False, "error": msg}

    # 校验
    _set_progress(phase="verifying")
    actual = h.hexdigest()
    if expect and actual != expect:
        try:
            target.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        msg = f"更新包校验失败（期望 {expect[:12]}…，实际 {actual[:12]}…），已丢弃"
        logger.error("update_digest_mismatch", expect=expect, actual=actual)
        _set_progress(phase="failed", error=msg)
        return {"ok": False, "error": msg}
    if not expect:
        # 无 digest 时仍继续，但明确告知未做完整性校验
        logger.warning("update_no_digest", version=version)

    return {"ok": True, "path": str(target), "size": received,
            "sha256": actual, "verified": bool(expect)}


def extract_update(*, zip_path: str, version: str) -> dict:
    """把更新包解压到 updates/staging/vX.Y.Z/。永不抛异常。

    防 Zip Slip：逐个条目校验目标路径必须落在 staging 目录内。
    """
    import zipfile

    staging = _updates_root() / "staging" / f"v{version}"
    try:
        _set_progress(phase="extracting")
        # 清掉同版本残留，保证干净解压
        if staging.exists():
            import shutil

            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)

        root = staging.resolve()
        with zipfile.ZipFile(zip_path) as z:
            for m in z.infolist():
                dest = (staging / m.filename).resolve()
                if not str(dest).startswith(str(root)):
                    msg = f"更新包含非法路径（疑似 Zip Slip）：{m.filename}"
                    logger.error("update_zip_slip", name=m.filename)
                    _set_progress(phase="failed", error=msg)
                    return {"ok": False, "error": msg}
            z.extractall(staging)

        _set_progress(phase="ready", version=version)
        return {"ok": True, "staging": str(staging)}
    except Exception as e:  # noqa: BLE001
        msg = f"解压失败：{str(e)[:150]}"
        logger.exception("update_extract_failed")
        _set_progress(phase="failed", error=msg)
        return {"ok": False, "error": msg}


def build_apply_script() -> str:
    """生成换盘脚本内容（纯函数，便于测试）。

    路径一律由**命令行参数**传入（%1=staging %2=install_dir %3=exe_name %4=log）：
    后端并不知道自己的安装目录，安装目录只有 Electron（app.getPath('exe')）能可靠给出；
    并且参数化可避免把用户路径拼进脚本体导致的转义/注入问题。

    流程：等主进程退出 → 备份 exe → robocopy 覆盖 → 启动新版；失败则回滚。
    刻意不用 /MIR：避免 purge 删掉仍被占用或不该删的文件。
    """
    return r"""@echo off
setlocal enableextensions
set "SRC=%~1"
set "DST=%~2"
set "EXE=%~3"
set "LOG=%~4"

if "%SRC%"=="" exit /b 2
if "%DST%"=="" exit /b 2
if "%EXE%"=="" set "EXE=RAG知识库.exe"

rem 日志只写纯 ASCII（不写含中文的时间戳）—— %date%/%time% 是本地编码(GBK)，
rem 与脚本的 UTF-8 混写会产生乱码，反而妨碍排障。
echo [%date% %time%] apply rc-start >> "%LOG%"

rem ---- 1) 等待主进程退出（最多 60 次 * 2s = 120s）----
rem 注意：必须用 goto 循环而非 (...) 块 —— 块内 %n% 在解析期就展开，
rem 会导致计数永不增长、循环退不出来。
set /a n=0
:wait
set /a n+=1
tasklist /FI "IMAGENAME eq %EXE%" 2>nul | find /I "%EXE%" >nul
if errorlevel 1 goto gone
if %n% GEQ 60 goto gone
timeout /t 2 /nobreak >nul
goto wait
:gone
rem 给文件锁一点释放时间
timeout /t 3 /nobreak >nul

rem ---- 2) 备份现有可执行文件 ----
if exist "%DST%\%EXE%" copy /Y "%DST%\%EXE%" "%DST%\%EXE%.bak" >nul 2>&1

rem ---- 3) 覆盖（robocopy 返回码 < 8 视为成功）----
robocopy "%SRC%" "%DST%" /E /R:3 /W:2 /NFL /NDL /NJH /NJS >nul
set RC=%ERRORLEVEL%
echo [%date% %time%] robocopy rc=%RC% >> "%LOG%"
if %RC% GEQ 8 goto rollback

rem ---- 4) 启动新版 ----
start "" "%DST%\%EXE%"
echo [%date% %time%] ok >> "%LOG%"
exit /b 0

:rollback
echo [%date% %time%] FAILED rc=%RC% rollback >> "%LOG%"
> "%~dp4.update_failed.log" echo update failed rc=%RC% rolled back
if exist "%DST%\%EXE%.bak" copy /Y "%DST%\%EXE%.bak" "%DST%\%EXE%" >nul 2>&1
start "" "%DST%\%EXE%"
exit /b 1
"""


def write_apply_script(updates_dir: str | None = None) -> str:
    """把换盘脚本写到 updates/ 下，返回脚本路径。

    写在数据目录（%APPDATA%/RAG知识库/updates）而非安装目录 —— 否则替换安装目录时
    会把正在运行的脚本自己覆盖掉。
    """
    from pathlib import Path

    root = Path(updates_dir) if updates_dir else _updates_root()
    root.mkdir(parents=True, exist_ok=True)
    script = root / "apply_update.bat"
    # 用 utf-8-sig（带 BOM）以便 cmd 配合 chcp 65001 正确读中文；换行用 CRLF
    script.write_text(build_apply_script().replace("\n", "\r\n"), encoding="utf-8-sig")
    return str(script)


def write_pending(*, version: str, staging_dir: str, script_path: str) -> str:
    """写待应用信息（版本、暂存目录、脚本路径）。后端已知这些，写在数据目录。"""
    import json

    root = _updates_root()
    f = root / "pending.json"
    f.write_text(
        json.dumps(
            {"version": version, "staging": staging_dir, "script": script_path},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return str(f)


def write_apply_signal() -> str:
    """写「可以换盘了」信号文件。

    Electron 主进程轮询到它后：读取 pending.json，自己补上安装目录与 exe 名，
    然后 spawn apply_update.bat 并退出（释放 exe 文件锁）。
    """
    root = _updates_root()
    sig = root / "apply.requested"
    sig.write_text("1", encoding="utf-8")
    return str(sig)


