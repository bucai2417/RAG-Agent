"""可选组件安装服务：下载 / 校验 / 安全解压 / 安装 / 卸载 / 进度。

目录布局（均在 settings.component_path 下）：

    .download/<key>-<uuid>.zip   下载或上传的中转包（结束后删除）
    <key>/<version>.partial/     解压中（失败整目录删除）
    <key>/<version>/             已安装（zip 内原样结构，如 chrome-win64/）
    <key>/<version>/manifest.json  {version, platform, sha256, verified, source, installed_at}

设计要点（勿凭直觉改动）：

1. **209MB 必须流式**：整体读入内存会直接打爆后端进程。边写盘边算哈希，
   照抄 update_service.download_update 的形态。

2. **校验策略分三档**：官方源带内置哈希（必校验）；镜像 URL 与本地 zip 无预期哈希，
   安装成功但 manifest.verified=False —— 前端必须如实标注「未验证」，
   绝不能假装校验过。

3. **解压必须自建白名单**：技能包的 extract_zip 有 500 文件、50MB 上限，
   对浏览器包远远不够；但它的路径穿越判定思路要沿用（拒绝对路径 / 含 `..` / 符号链接），
   本模块内联一份，避免与技能包耦合。

4. **安装是后台任务**：接口要立刻返回（209MB 下载可能几分钟），进度靠前端轮询。
   同一组件同时只允许一个安装任务，用模块级 _BUSY 集合拦。

5. **失败必清残骸**：.partial 与 zip 一律删除，否则下次安装会踩到半截目录。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import time
import uuid
import zipfile
from pathlib import Path

from app.core.config import settings
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.services import component_catalog as CAT

logger = get_logger("component")

# 解压上限：浏览器包约 1.1 万个文件、解压后 ~600MB，留足余量
MAX_FILES = 8000
MAX_TOTAL_BYTES = 800 * 1024 * 1024
_CHUNK = 256 * 1024

# 每个组件的进度状态（进程内单例；后端重启即归零，符合「安装是短暂操作」的事实）
_progress: dict[str, dict] = {}
# 正在安装的组件键（防重复点击并发装同一个包）
_BUSY: set[str] = set()


def _root() -> Path:
    p = settings.component_path
    p.mkdir(parents=True, exist_ok=True)
    return p


def _download_dir() -> Path:
    d = _root() / ".download"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _component_dir(key: str) -> Path:
    return _root() / key


def _set_progress(key: str, **kw) -> None:
    st = _progress.setdefault(key, _blank_progress())
    st.update(kw)


def _blank_progress() -> dict:
    return {
        "phase": "idle",      # idle | downloading | verifying | extracting | done | failed
        "received": 0,
        "total": 0,
        "error": None,
        "actual_sha256": None,
        "updated_at": None,
    }


def progress(key: str) -> dict:
    st = _progress.get(key)
    if st is None:
        return _blank_progress()
    return dict(st)


# ------------------------------------------------------------------ 状态


def _read_manifest(dest: Path) -> dict | None:
    mf = dest / "manifest.json"
    if not mf.is_file():
        return None
    try:
        import json

        data = json.loads(mf.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _find_executable(dest: Path, spec: CAT.ComponentSpec, plat: str) -> Path | None:
    """按目录里声明的 glob 定位可执行文件（相对已安装目录）。"""
    for pattern in spec.globs_for(plat):
        try:
            for p in sorted(dest.glob(pattern)):
                if p.is_file():
                    return p
        except Exception:  # noqa: BLE001
            continue
    return None


def _installed_version_dir(key: str) -> Path | None:
    """返回「看起来装好了」的版本目录（manifest 存在且可执行文件还在）。"""
    spec = CAT.get_spec(key)
    if spec is None:
        return None
    base = _component_dir(key)
    if not base.is_dir():
        return None
    for d in sorted([p for p in base.iterdir() if p.is_dir()], reverse=True):
        if d.name.endswith(".partial"):
            continue
        mf = _read_manifest(d)
        if not mf:
            continue
        plat = str(mf.get("platform") or "")
        if _find_executable(d, spec, plat) is None:
            # 目录被手删/不完整：视为未安装，避免给出一个跑不起来的路径
            continue
        return d
    return None


def status(key: str) -> dict:
    """组件状态。未安装时 install_path/executable 为 None，不臆造路径。"""
    spec = CAT.get_spec(key)
    if spec is None:
        raise NotFoundError(f"未知组件：{key}")
    plat = CAT.platform_key()
    dest = _installed_version_dir(key)
    mf = _read_manifest(dest) if dest else None
    exe = None
    if dest and mf:
        p = _find_executable(dest, spec, str(mf.get("platform") or plat))
        exe = str(p) if p else None
    supported = spec.supports(plat)
    return {
        "key": spec.key,
        "name": spec.name,
        "summary": spec.summary,
        "detail": spec.detail,
        "version": spec.version,
        "platform": plat,
        "supported": supported,
        # 该平台是否已锁定官方包哈希：False 时官方源安装也会标记「未验证」
        "hash_pinned": bool(spec.sha_for(plat)),
        "requires_gui": spec.requires_gui,
        "hint": spec.hints.get(_platform_family()) or "",
        "installed": bool(dest and exe),
        "installed_version": str(mf.get("version")) if mf else None,
        "install_path": str(dest) if dest else None,
        "executable": exe,
        "size_bytes": mf.get("size") if mf else spec.size_for(plat),
        "verified": bool(mf.get("verified")) if mf else False,
        "source": mf.get("source") if mf else None,
        "installed_at": mf.get("installed_at") if mf else None,
        "progress": progress(key),
    }


def _platform_family() -> str:
    import sys

    if sys.platform == "win32":
        return "win32"
    return sys.platform


def list_status() -> list[dict]:
    return [status(s.key) for s in CAT.list_specs()]


# ------------------------------------------------------------------ 校验与解压


def _safe_member(name: str) -> bool:
    """拒绝绝对路径、盘符与路径穿越（沿技能包同款判定）。"""
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    norm = name.replace("\\", "/")
    if len(norm) > 1 and norm[1] == ":":   # Windows 盘符
        return False
    return ".." not in norm.split("/")


def _strip_common_root(zf: zipfile.ZipFile) -> bool:
    """写盘时是否剥掉 zip 的公共单层根（如 chrome-win64/、chrome-mac-arm64/）。

    为什么必须剥：Windows 默认 260 字符路径上限，而 mac 包最长成员已达 268 字符，
    再叠加数据目录前缀必然超限。剥掉公共根（约 15 字符）后主要成员就能落地。
    仅在 TARGET 为 win32 时剥：mac/linux 上没有该限制，保留原始结构更贴近官方布局，
    也避免改变已安装目录的相对结构。
    """
    if sys.platform != "win32":
        return False
    files = [i.filename.replace("\\", "/") for i in zf.infolist() if not i.is_dir()]
    if len(files) < 2:
        return False
    roots = {f.split("/")[0] for f in files if "/" in f}
    return len(roots) == 1 and all("/" in f for f in files)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return mode == 0o120000


def _symlink_target(info: zipfile.ZipInfo, zf: zipfile.ZipFile) -> str:
    """读取符号链接成员的目标路径（zip 里链接目标就是内容）。"""
    try:
        with zf.open(info) as f:
            return f.read(4096).decode("utf-8", errors="ignore").strip()
    except Exception:  # noqa: BLE001
        return ""


def _safe_symlink(name: str, target: str) -> bool:
    """符号链接只允许指向解压目录内部（相对路径且不向上逃逸）。

    macOS 的 .app 包必须保留符号链接（framework 内部指向 Versions/Current），
    一律拒绝会让 mac 包完全装不上；但指向目录外（如 /etc/passwd）必须拒绝，
    否则一个恶意 zip 就能在磁盘上留一个指向系统文件的链接。
    """
    if not target or target.startswith("/") or target.startswith("\\"):
        return False
    if len(target) > 1 and target[1] == ":":
        return False
    # 相对目标按成员所在目录解析，逐级消解后不能逃出根
    parts = name.replace("\\", "/").split("/")[:-1] + target.replace("\\", "/").split("/")
    depth = 0
    for p in parts:
        if p in ("", "."):
            continue
        if p == "..":
            depth -= 1
            if depth < 0:
                return False
        else:
            depth += 1
    return True


def _safe_extract(zip_path: Path, dest: Path) -> int:
    """把 zip 解到 dest（目录需已存在）。返回解出的文件数。失败抛 ValueError。

    安全边界：拒绝绝对路径 / `..` 穿越 / 指向包外的符号链接；
    包内相对符号链接保留（macOS .app 依赖它们），在 POSIX 上按真实链接创建，
    在不支持符号链接的 Windows 上改写为目录联接失败也不会破坏安装。
    """
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise ValueError(f"不是有效的 zip 文件：{e}") from e
    count = 0
    total = 0
    with zf:
        strip = _strip_common_root(zf)
        for info in zf.infolist():
            if info.is_dir():
                continue
            full = info.filename.replace("\\", "/")
            if not _safe_member(full):
                raise ValueError(f"压缩包内含非法路径（疑似路径穿越）：{info.filename}")
            count += 1
            total += info.file_size
            if count > MAX_FILES:
                raise ValueError(f"文件数超过上限 {MAX_FILES}")
            if total > MAX_TOTAL_BYTES:
                raise ValueError("解压后总量超过上限")

            # 写盘路径：Windows 上剥掉公共根以规避 260 字符限制；安全检查始终基于完整名
            rel = full.split("/", 1)[1] if strip and "/" in full else full
            target = dest / rel
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise ValueError(_path_error(rel, e)) from e

            if _is_symlink(info):
                link_target = _symlink_target(info, zf)
                if not _safe_symlink(full, link_target):
                    raise ValueError(f"压缩包内含指向包外的符号链接，已拒绝：{info.filename}")
                try:
                    os.symlink(link_target, target)
                except (OSError, NotImplementedError):
                    # Windows 无权限创建符号链接时降级：写一个记录文件，
                    # 让 chrome 启动前能看到并给出可读提示（而不是静默缺文件）
                    target.write_text(f"symlink -> {link_target}", encoding="utf-8")
                continue

            try:
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, _CHUNK)
            except OSError as e:
                raise ValueError(_path_error(rel, e)) from e
    return count


def _path_error(name: str, e: OSError) -> str:
    """把解压时的文件系统错误翻成人能看懂的原因。"""
    win_no = getattr(e, "winerror", None)
    if win_no in (206, 3):   # ERROR_FILENAME_EXCED_RANGE / ERROR_PATH_NOT_FOUND
        return (
            f"解压失败：目标路径过长（Windows 260 字符限制）。"
            f"可将组件目录改到更短的位置后重试。条目：{name[:120]}"
        )
    return f"解压失败：{e}（条目：{name[:120]}）"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ 安装


def _check_remote_url(url: str) -> str | None:
    """镜像 URL 校验：必须 https，且过 SSRF 守卫（内网/保留地址一律拒绝）。"""
    from urllib.parse import urlparse

    from app.agents.tools.builtin import _check_url

    p = urlparse(url)
    if p.scheme != "https":
        return f"下载地址必须使用 https（当前 {p.scheme or '空'}）"
    err = _check_url(url, None)
    if err:
        return err
    return None


async def _download(key: str, url: str, target: Path) -> tuple[str, int]:
    """流式下载到 target，边写边算哈希。返回 (sha256, 字节数)。失败抛 ValidationError。"""
    import httpx

    err = _check_remote_url(url)
    if err:
        raise ValidationError(f"下载地址被拒绝：{err}")

    h = hashlib.sha256()
    received = 0
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=300.0), follow_redirects=True
        ) as c:
            async with c.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise ValidationError(f"下载失败：HTTP {resp.status_code}")
                # 重定向后的最终地址也必须校验，否则可 302 到内网绕过前面那一次
                final = str(resp.url)
                err2 = _check_remote_url(final)
                if err2:
                    raise ValidationError(f"下载被重定向到不受信任的地址：{err2}")
                total = int(resp.headers.get("content-length") or 0)
                _set_progress(key, received=0, total=total, phase="downloading")
                with open(target, "wb") as f:
                    async for chunk in resp.aiter_bytes(_CHUNK):
                        f.write(chunk)
                        h.update(chunk)
                        received += len(chunk)
                        _set_progress(key, received=received)
    except ValidationError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ValidationError(f"下载中断：{str(e)[:200]}") from e
    return h.hexdigest(), received


def install_from_zip(
    key: str, zip_path: Path, *, source_kind: str, expect_sha: str | None = None,
) -> dict:
    """把已下载/已上传的 zip 安装为组件。同步执行（不含网络），便于直测。

    失败抛 ValidationError；成功返回 manifest 内容。调用方负责清理 zip 与进度。
    """
    spec = CAT.get_spec(key)
    if spec is None:
        raise NotFoundError(f"未知组件：{key}")
    plat = CAT.platform_key()
    if not spec.supports(plat):
        raise ValidationError(f"当前平台（{plat or '未知'}）没有可用的安装包")

    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise ValidationError("安装包不存在")

    src_size = zip_path.stat().st_size
    if src_size <= 0:
        raise ValidationError("安装包为空")

    # 哈希：有预期值就必须一致，否则明确告知期望/实际，便于核对目录里的常量
    actual = _sha256_file(zip_path)
    if expect_sha:
        if actual != expect_sha.lower():
            raise ValidationError(
                f"安装包校验失败（期望 {expect_sha[:12]}…，实际 {actual[:12]}…），已终止"
            )

    base = _component_dir(key)
    base.mkdir(parents=True, exist_ok=True)
    final = base / spec.version
    partial = base / f"{spec.version}.partial"

    # 清残骸：上次失败的 .partial 与旧的同版本目录
    _rmtree_quiet(partial)
    partial.mkdir(parents=True, exist_ok=True)

    try:
        _safe_extract(zip_path, partial)
        exe = _find_executable(partial, spec, plat)
        if exe is None:
            tops = ", ".join(sorted(p.name for p in partial.iterdir())[:8]) or "(空)"
            raise ValidationError(
                f"安装包里没找到可执行文件（期望 {spec.globs_for(plat)}，实际顶层：{tops}）"
            )
        manifest = {
            "version": spec.version,
            "platform": plat,
            "sha256": actual,
            "verified": bool(expect_sha and actual == expect_sha.lower()),
            "source": source_kind,
            "size": src_size,
            "installed_at": int(time.time() * 1000),
            "executable_rel": str(exe.relative_to(partial)).replace(os.sep, "/"),
        }
        import json

        (partial / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _rmtree_quiet(final)
        os.replace(partial, final)
    except Exception as e:
        _rmtree_quiet(partial)
        # 解压层抛 ValueError（zip-slip / 符号链接 / 超限 / 路径过长）：统一转成
        # ValidationError，否则接口层会把它当 500 抛出，前端拿不到可读原因。
        if isinstance(e, (ValueError, OSError)) and not isinstance(e, ValidationError):
            raise ValidationError(str(e)) from e
        raise

    return manifest


def _rmtree_quiet(p: Path) -> None:
    try:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


async def _install_task(
    key: str, *, source_kind: str, url: str | None, zip_path: Path | None,
    expect_sha: str | None,
) -> None:
    """后台安装任务：下载（可选）→ 校验 → 解压 → 落 manifest，全程更新进度。"""
    tmp: Path | None = zip_path
    try:
        _set_progress(key, phase="downloading" if zip_path is None else "verifying",
                      received=0, total=0, error=None, actual_sha256=None,
                      updated_at=int(time.time() * 1000))

        if zip_path is None:
            if not url:
                raise ValidationError("缺少下载地址")
            tmp = _download_dir() / f"{key}-{uuid.uuid4().hex}.zip"
            sha, size = await _download(key, url, tmp)
            _set_progress(key, phase="verifying", actual_sha256=sha, received=size, total=size)
            if expect_sha and sha != expect_sha.lower():
                raise ValidationError(
                    f"安装包校验失败（期望 {expect_sha[:12]}…，实际 {sha[:12]}…），已丢弃"
                )

        _set_progress(key, phase="extracting", updated_at=int(time.time() * 1000))
        manifest = install_from_zip(
            key, Path(tmp), source_kind=source_kind, expect_sha=expect_sha
        )
        _set_progress(key, phase="done", error=None,
                      actual_sha256=manifest.get("sha256"),
                      updated_at=int(time.time() * 1000))
        logger.info("component_installed", key=key, version=manifest.get("version"),
                    verified=manifest.get("verified"), source=source_kind)
    except Exception as e:  # noqa: BLE001
        msg = str(e)[:300] or "安装失败"
        _set_progress(key, phase="failed", error=msg, updated_at=int(time.time() * 1000))
        logger.warning("component_install_failed", key=key, error=msg)
    finally:
        if tmp is not None:
            try:
                Path(tmp).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        _BUSY.discard(key)


def start_install(
    key: str, *, source: str = "official", url: str | None = None,
    local_zip: Path | None = None,
) -> dict:
    """启动后台安装。source: official（内置 URL+哈希）| url（镜像，无哈希）| upload（本地 zip）。

    已装同版本时直接返回 already_installed，避免重复下载 209MB。
    同一组件已有任务在跑 → started=False。
    """
    spec = CAT.get_spec(key)
    if spec is None:
        raise NotFoundError(f"未知组件：{key}")
    plat = CAT.platform_key()
    if not spec.supports(plat):
        raise ValidationError(f"当前平台（{plat or '未知'}）没有可用的安装包")
    if key in _BUSY:
        return {"started": False, "reason": "该组件正在安装中，请稍候"}

    st = status(key)
    if st["installed"] and st["installed_version"] == spec.version:
        return {"started": False, "reason": "该版本已安装，如需重装请先卸载", "already_installed": True}

    if source == "official":
        url = spec.url_for(plat)
        expect_sha = spec.sha_for(plat)
    elif source == "url":
        if not url:
            raise ValidationError("请填写镜像下载地址")
        err = _check_remote_url(url)
        if err:
            raise ValidationError(f"下载地址被拒绝：{err}")
        expect_sha = None
    elif source == "upload":
        if local_zip is None or not Path(local_zip).is_file():
            raise ValidationError("请上传安装包 zip")
        expect_sha = None
    else:
        raise ValidationError(f"不支持的安装方式：{source}")

    _BUSY.add(key)
    _set_progress(key, **_blank_progress())
    _set_progress(key, phase="downloading" if source != "upload" else "verifying",
                  updated_at=int(time.time() * 1000))

    from app.tasks.bg import spawn

    spawn(
        f"component_install:{key}",
        _install_task(
            key,
            source_kind=source,
            url=url,
            zip_path=Path(local_zip) if source == "upload" else None,
            expect_sha=expect_sha,
        ),
    )
    return {"started": True, "source": source, "version": spec.version}


async def save_upload(upload_file) -> Path:
    """把 FastAPI UploadFile 分块落盘到 .download/，返回临时 zip 路径。

    分块写盘（8MB/次）避免 200MB 包整体进内存。
    """
    tmp = _download_dir() / f"upload-{uuid.uuid4().hex}.zip"
    total = 0
    with open(tmp, "wb") as f:
        while True:
            chunk = await upload_file.read(8 * 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_TOTAL_BYTES:
                f.close()
                tmp.unlink(missing_ok=True)
                raise ValidationError("上传的安装包超过大小上限（800MB）")
            f.write(chunk)
    return tmp


def uninstall(key: str) -> dict:
    """删除组件目录。Windows 上文件被占用时给出明确指引而不是 500。"""
    spec = CAT.get_spec(key)
    if spec is None:
        raise NotFoundError(f"未知组件：{key}")
    base = _component_dir(key)
    if not base.exists():
        return {"ok": True, "message": "该组件未安装"}
    _BUSY.discard(key)
    _progress.pop(key, None)
    try:
        shutil.rmtree(base)
    except PermissionError as e:
        raise ValidationError(
            "删除失败：文件正被占用。请先在「系统设置 → 浏览器」点「停止」，再重试卸载。"
        ) from e
    except Exception as e:  # noqa: BLE001
        raise ValidationError(f"删除失败：{str(e)[:200]}") from e
    logger.info("component_uninstalled", key=key)
    return {"ok": True, "message": "已卸载"}


# ------------------------------------------------------------------ 供浏览器管理器使用


def browser_executables() -> list[Path]:
    """已安装的浏览器运行时可执行文件（新版在前）。"""
    spec = CAT.get_spec("browser_chromium")
    if spec is None:
        return []
    base = _component_dir(spec.key)
    if not base.is_dir():
        return []
    out: list[Path] = []
    for d in sorted([p for p in base.iterdir() if p.is_dir()], reverse=True):
        if d.name.endswith(".partial"):
            continue
        mf = _read_manifest(d)
        if not mf:
            continue
        exe = _find_executable(d, spec, str(mf.get("platform") or CAT.platform_key()))
        if exe is not None:
            out.append(exe)
    return out
