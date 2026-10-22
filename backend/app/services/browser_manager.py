"""浏览器管理：检测可用浏览器、一键启动带调试端口的实例、自动拉起。

设计要点（勿凭直觉改动）：

1. **内置运行时优先，系统浏览器兜底**。
   内置运行时由用户在「工具 → 可选组件」自己点安装（固定版本 Chrome for Testing），
   落在 data/components 下；没装时退回本机已有的 Chrome/Edge。
   刻意不把浏览器直接打进安装包：会让安装包从几十 MB 涨到 300MB+，
   而且需要精确控制版本、不受用户浏览器升级影响时，让用户自己装一份更可控。

2. **独立 profile 目录，不复用用户日常配置**。
   带 `--user-data-dir` 启动会创建全新配置，避免污染用户正在用的浏览器
   （Chrome 不允许两个实例共用同一 profile 且其中一个开调试端口）。
   代价是**首次需要在这个窗口里登录一次**，之后 profile 复用即可保持登录。

3. **首次启动建议非 headless**。headless 无法扫码/输密码登录，
   而复用登录态正是这套能力的核心价值。

4. **进程由后端管理**。启动后记录 PID，提供停止接口；进程退出时清理。
"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.config import DATA_DIR
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger

logger = get_logger("browser_mgr")

# 调试 profile 放在 data 目录下，随应用数据一起保留（登录态可复用）
PROFILE_DIR = DATA_DIR / "browser_profile"
DEFAULT_PORT = 9222

# 各平台常见安装路径（按优先级）
_CANDIDATES: dict[str, list[str]] = {
    "win32": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    ],
    "darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ],
    "linux": [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/microsoft-edge",
    ],
}

# 没装浏览器时给出的官方下载入口（按平台）
_DOWNLOAD_LINKS: dict[str, list[tuple[str, str]]] = {
    "win32": [
        ("Google Chrome（Windows 安装包）", "https://www.google.com/chrome/"),
        ("Microsoft Edge（Windows 安装包）", "https://www.microsoft.com/edge"),
    ],
    "darwin": [
        ("Google Chrome（macOS 安装包）", "https://www.google.com/chrome/"),
        ("Microsoft Edge（macOS 安装包）", "https://www.microsoft.com/edge"),
    ],
    "linux": [
        ("Google Chrome（Linux .deb/.rpm）", "https://www.google.com/chrome/"),
        ("Chromium（发行版软件源）", "https://www.chromium.org/getting-involved/download-chromium/"),
    ],
}


@dataclass
class BrowserInfo:
    path: str
    name: str
    family: str            # chrome | edge | chromium
    source: str = "system"  # bundled（可选组件安装的内置运行时）| system


@dataclass
class BrowserState:
    running: bool = False
    port: int = DEFAULT_PORT
    pid: int | None = None
    profile_dir: str = ""
    started_at: int | None = None
    error: str | None = None


# 后端管理的浏览器进程（模块级单例，单进程内有效）
_proc: subprocess.Popen | None = None
_state = BrowserState(profile_dir=str(PROFILE_DIR))


def _bundled_browsers() -> list[BrowserInfo]:
    """可选组件安装的内置运行时（新版在前）。未安装时返回空列表。"""
    try:
        from app.services import component_service as CS

        exes = CS.browser_executables()
    except Exception:  # noqa: BLE001
        # 组件服务异常绝不能连带浏览器检测失败（用户仍有系统浏览器可用）
        return []
    out: list[BrowserInfo] = []
    for exe in exes:
        try:
            rp = str(Path(exe).resolve())
        except OSError:
            continue
        out.append(BrowserInfo(path=rp, name="内置 Chromium（可选组件）", family="chromium",
                               source="bundled"))
    return out


def detect_browsers() -> list[BrowserInfo]:
    """可用浏览器列表：**内置运行时在前**，系统浏览器在后（顺序即默认优先级）。"""
    found: list[BrowserInfo] = []
    seen: set[str] = set()
    for b in _bundled_browsers():
        if b.path in seen:
            continue
        seen.add(b.path)
        found.append(b)

    key = "win32" if sys.platform == "win32" else "darwin" if sys.platform == "darwin" else "linux"
    for p in _CANDIDATES.get(key, []):
        if not p:
            continue
        path = Path(p)
        if not path.is_file():
            continue
        rp = str(path.resolve())
        if rp in seen:
            continue
        seen.add(rp)
        low = path.name.lower()
        if "edge" in low or "msedge" in low:
            family, name = "edge", "Microsoft Edge"
        elif "chromium" in low:
            family, name = "chromium", "Chromium"
        else:
            family, name = "chrome", "Google Chrome"
        found.append(BrowserInfo(path=rp, name=name, family=family, source="system"))
    return found


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def download_hints() -> list[dict]:
    """当前平台可用的浏览器官方下载入口（未装浏览器时引导用户去装）。"""
    key = "win32" if sys.platform == "win32" else "darwin" if sys.platform == "darwin" else "linux"
    return [{"name": n, "url": u} for n, u in _DOWNLOAD_LINKS.get(key, [])]


def missing_browser_guide() -> dict:
    """「本机没有任何可用浏览器」时的结构化指引，供前端/工具复用。

    install_url 指向「工具」页的可选组件抽屉，用户在那里一键装内置运行时；
    装不了（离线/无权限）时再按 links 里的官方入口自行安装系统浏览器。
    """
    return {
        "title": "本机未检测到浏览器",
        "summary": (
            "AI 的网页工具需要一个 Chromium 系浏览器（Chrome / Edge / Chromium）。"
            "可以选择安装内置运行时，或自行安装系统浏览器后重试。"
        ),
        # 两条路径按推荐顺序：内置运行时（应用内一键装）优先，系统浏览器兜底
        "options": [
            {
                "kind": "bundled",
                "recommended": True,
                "title": "安装内置浏览器运行时（推荐）",
                "detail": (
                    "在「工具」页右上角点「可选组件」→ 安装「浏览器运行时（Chromium）」。"
                    "约 200MB，官方源下载会校验内置 sha256；装完无需重启，AI 即可调用网页工具。"
                ),
                "route": "/admin/tools",
                "query": {"components": "1", "key": "browser_chromium"},
                "requires_permission": "tool:manage",
            },
            {
                "kind": "system",
                "recommended": False,
                "title": "自行安装系统浏览器",
                "detail": (
                    "装好后回到「系统设置 → 浏览器」点「启动浏览器」即可，"
                    "本机已装 Chrome/Edge 时无需安装内置运行时。"
                ),
                "links": download_hints(),
                "route": "/admin/browser",
            },
        ],
    }


def is_debug_port_ready(port: int = DEFAULT_PORT) -> bool:
    """调试端口是否可用（有浏览器在监听）。"""
    return _port_open(port)


def get_state() -> dict:
    """当前状态：进程是否存活 + 端口是否就绪。"""
    global _proc
    alive = _proc is not None and _proc.poll() is None
    if not alive:
        _proc = None
        _state.running = False
        _state.pid = None
    port_ready = is_debug_port_ready(_state.port)
    browsers = detect_browsers()
    return {
        "running": _state.running or port_ready,
        "managed": _state.running,
        # managed=False 但端口就绪：浏览器仍在跑，但句柄已丢失（例如后端重启过），
        # 此时可通过 CDP Browser.close 优雅关闭，见 stop()。
        "port_ready": port_ready,
        "pid": _state.pid,
        "port": _state.port,
        "profile_dir": str(PROFILE_DIR),
        "started_at": _state.started_at,
        "error": _state.error,
        "bundled_installed": _bundled_installed(),
        "browsers": [
            {"name": b.name, "family": b.family, "path": b.path, "source": b.source}
            for b in browsers
        ],
        # 一个都没检测到：前端据此直接展示安装引导，而不是等用户点「启动」才报错
        "no_browser": not browsers,
        "install_guide": None if browsers else missing_browser_guide(),
    }


def _bundled_installed() -> bool:
    """是否已通过「可选组件」安装内置运行时。"""
    try:
        from app.services import component_service as CS

        return bool(CS.browser_executables())
    except Exception:  # noqa: BLE001
        return False


def _adopt_existing(port: int) -> bool:
    """若占用端口的实例用的是本服务 profile，则重新取得「管理权」。

    后端重启后 subprocess 句柄丢失，但浏览器还在跑、profile 仍是我们的。
    通过 CDP 读取 /json/version 无法直接拿到 user-data-dir，因此改用
    profile 目录里的锁文件特征判断：我们的 profile 始终存在 SingletonLock
    （Windows 下是 lockfile），且端口与我们记录的一致。
    """
    global _state
    try:
        lock = PROFILE_DIR / ("lockfile" if sys.platform == "win32" else "SingletonLock")
        if not lock.exists():
            return False
        _state.running = True  # 视为「可管理」，stop() 会用 CDP 关闭
        _state.port = port
        if _state.started_at is None:
            _state.started_at = int(lock.stat().st_mtime * 1000)
        return True
    except Exception:  # noqa: BLE001
        return False


def launch(
    *, executable: str | None = None, port: int = DEFAULT_PORT, headless: bool = False,
    start_url: str = "about:blank",
) -> dict:
    """启动一个带调试端口的浏览器实例。

    executable 为空时自动选第一个检测到的浏览器。
    已在运行则直接返回（幂等），避免重复拉起。
    """
    global _proc

    state = get_state()
    if state["port_ready"]:
        # 端口已被占用。若句柄还在，说明就是我们自己启动的，直接复用；
        # 若句柄丢失，尝试确认是不是本服务的 profile（后端重启后的孤儿实例）。
        adopted = _adopt_existing(port)
        return {
            **get_state(),
            "message": (
                "调试端口已就绪，复用现有浏览器"
                if adopted else "调试端口已被其他浏览器占用，直接复用该实例"
            ),
        }

    browsers = detect_browsers()
    if not browsers:
        # 错误信息要能让用户直接照着做：先说应用内一键装，再说去哪下系统浏览器
        links = "；".join(f"{n}：{u}" for n, u in ((x["name"], x["url"]) for x in download_hints()))
        raise NotFoundError(
            "本机未检测到 Chrome / Edge / Chromium。"
            "推荐在「工具 → 可选组件」安装内置浏览器运行时（约 200MB，装完无需重启）；"
            "或自行安装系统浏览器后重试"
            + (f"（下载地址：{links}）" if links else "")
            + "；也可在启动参数中指定可执行文件路径。"
        )

    if executable:
        target = Path(executable)
        if not target.is_file():
            raise ValidationError(f"指定的浏览器路径不存在：{executable}")
        exe = str(target)
        chosen = next((b for b in browsers if b.path == str(target.resolve())), None)
    else:
        # 顺序即优先级：内置运行时（可选组件）在前，系统浏览器在后
        chosen = browsers[0]
        exe = chosen.path

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        # 调试端口默认只绑 loopback；显式声明，避免被绑定到所有网卡
        "--remote-debugging-address=127.0.0.1",
    ]
    if headless:
        args.append("--headless=new")
    # 避免被自动化检测的常见标记（部分站点会据此拦页面）
    args.append("--disable-blink-features=AutomationControlled")
    args.append(start_url)

    try:
        creationflags = 0
        if sys.platform == "win32":
            # 独立进程组，避免随子进程树一起被回收
            creationflags = 0x00000008  # DETACHED_PROCESS
        _proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception as e:  # noqa: BLE001
        _state.error = str(e)[:300]
        raise ValidationError(f"启动浏览器失败：{str(e)[:200]}") from e

    _state.running = True
    _state.pid = _proc.pid
    _state.port = port
    _state.started_at = int(time.time() * 1000)
    _state.error = None

    # 等端口就绪（最多 15 秒）
    deadline = time.time() + 15
    while time.time() < deadline:
        if _port_open(port):
            break
        if _proc.poll() is not None:
            _state.running = False
            _state.error = f"浏览器进程已退出（code={_proc.returncode}）"
            raise ValidationError(_state.error)
        time.sleep(0.3)
    else:
        _state.error = "浏览器启动超时（15 秒内调试端口未就绪）"
        raise ValidationError(_state.error)

    logger.info("browser_launched", pid=_proc.pid, port=port, exe=exe,
                source=(chosen.source if chosen else "custom"))
    msg = (
        "浏览器已启动。首次使用请在这个窗口里登录目标网站，"
        "之后登录态会保存在应用数据目录中。"
    )
    if chosen is not None and chosen.source == "bundled":
        msg += (
            "\n注意：这是内置的 Chrome for Testing，Google 账号登录可能被判定为"
            "「不安全的浏览器」；涉及 Google 服务的站点建议改用系统 Chrome。"
        )
    return {**get_state(), "message": msg}


def stop(*, force_cdp: bool = True) -> dict:
    """停止浏览器。

    两种情况：
    1. 进程句柄还在 → 直接 terminate，最干净。
    2. 句柄丢失但端口仍就绪（后端重启过）→ 用 CDP Browser.close 优雅关闭。
       force_cdp=False 时不动它，避免误关用户自己开的调试浏览器。
    """
    global _proc
    had_handle = _proc is not None and _proc.poll() is None

    if had_handle:
        pid = _proc.pid
        try:
            _proc.terminate()
            _proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                _proc.kill()
            except Exception:  # noqa: BLE001
                pass
        _proc = None
        _state.running = False
        _state.pid = None
        _state.started_at = None
        logger.info("browser_stopped", pid=pid)
        return {**get_state(), "message": "已停止浏览器"}

    if not is_debug_port_ready(_state.port):
        _state.running = False
        _state.pid = None
        return {**get_state(), "message": "没有在运行的浏览器"}

    if not force_cdp:
        return {
            **get_state(),
            "message": "该浏览器不是由本服务启动的，未做处理",
        }

    # 句柄丢失：走 CDP 关闭
    ok = _run_cdp_shutdown(_state.port)
    if not ok:
        return {**get_state(), "message": "无法关闭浏览器（CDP 拒绝连接），请手动关闭窗口"}
    _state.running = False
    _state.pid = None
    _state.started_at = None
    return {**get_state(), "message": "已通过 CDP 关闭浏览器"}


def _run_cdp_shutdown(port: int) -> bool:
    """在同步语境里跑异步的 Browser.close。"""
    from app.services import cdp_client as C

    endpoint = f"http://127.0.0.1:{port}"
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(C.shutdown_browser(endpoint))
    # 已有事件循环（FastAPI 里本该走异步接口，这里保底）
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(C.shutdown_browser(endpoint))).result(timeout=10)


def ensure_running(*, auto_launch: bool = True, port: int = DEFAULT_PORT) -> tuple[bool, str]:
    """供工具调用：确保调试端口可用。

    返回 (是否可用, 提示信息)。auto_launch=False 时只检查不启动。
    """
    if is_debug_port_ready(port):
        return True, ""
    if not auto_launch:
        return False, (
            "浏览器调试端口未开启。可在「系统设置 → 浏览器」点「启动浏览器」，"
            "或先手动以 --remote-debugging-port=9222 启动 Chrome。"
            "若本机没有 Chrome/Edge，可在「工具 → 可选组件」安装内置浏览器运行时。"
        )
    try:
        launch(port=port)
        return True, "已自动启动浏览器（首次需在该窗口登录目标网站）"
    except NotFoundError as e:
        # 没装浏览器是最常见的一类失败，单独带上「去哪装」的指引
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, f"无法自动启动浏览器：{str(e)[:200]}"


def cleanup_on_shutdown() -> None:
    """应用退出时清理：只在进程仍由我们持有时终止。"""
    global _proc
    if _proc is not None and _proc.poll() is None:
        try:
            _proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    _proc = None
