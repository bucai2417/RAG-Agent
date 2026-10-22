"""可选组件目录：可选安装的二进制运行时（目前只有浏览器）。

设计要点（勿凭直觉改动）：

1. **纯静态数据，不联网**。版本、URL、哈希都写死在本文件里。这样：
   - 离线环境也能看到「有哪些组件、多大、什么版本」；
   - 安装时的哈希校验不依赖任何第三方元数据接口。

2. **哈希必须手工同步**。Chrome for Testing 的上游**不提供 `.sha256` 文件**
   （`chrome-win64.zip.sha256` 是 404），所以校验值只能由本仓库按固定版本内置。
   升级 `version` 时**必须同时更新 `sha256`**，否则从官方源安装会被自己的校验
   拦下（这是刻意行为：宁可装不上，也不装一个来路不明的包）。
   获取方式：完整下载该版本 zip 后 `sha256sum` / `Get-FileHash -Algorithm SHA256`。

   **某平台留空 = 尚未锁定哈希**：该平台仍能用官方源安装，但结果标记
   `verified=False`（前端显示「未验证」），`status.hash_pinned` 为 False 以便 UI 说明。
   填上哈希即自动开启强校验，无需改代码。

3. **平台键**沿用 Chrome for Testing 的命名（win64 / mac-arm64 / mac-x64 / linux64），
   便于官方 URL 与镜像 URL 直接对应。没有对应平台键 = 该平台不支持。

4. 镜像与本地 zip 两条兜底路径**不带预期哈希**，安装后 `manifest.verified=False`，
   前端会明确标注「未验证」——不假装校验过。
"""
from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ComponentSpec:
    key: str
    name: str
    summary: str
    detail: str
    version: str
    urls: dict[str, str]
    sha256: dict[str, str] = field(default_factory=dict)
    approx_size: dict[str, int] = field(default_factory=dict)
    # 平台 -> 相对 zip 根目录的可执行文件候选（glob）
    executable_globs: dict[str, list[str]] = field(default_factory=dict)
    requires_gui: bool = False
    # 安装成功后回给前端的提示（平台相关注意事项）
    hints: dict[str, str] = field(default_factory=dict)

    def url_for(self, plat: str) -> str | None:
        return self.urls.get(plat)

    def sha_for(self, plat: str) -> str | None:
        v = self.sha256.get(plat)
        return v.lower() if v else None

    def size_for(self, plat: str) -> int | None:
        return self.approx_size.get(plat)

    def globs_for(self, plat: str) -> list[str]:
        return self.executable_globs.get(plat) or []

    def supports(self, plat: str) -> bool:
        return plat in self.urls


_BASE_URL = "https://storage.googleapis.com/chrome-for-testing-public/{version}/{plat}/chrome-{plat}.zip"

_VERSION = "156.0.8078.47"


def _u(plat: str) -> str:
    return _BASE_URL.format(version=_VERSION, plat=plat)


BROWSER_CHROMIUM = ComponentSpec(
    key="browser_chromium",
    name="浏览器运行时（Chromium）",
    summary="让 AI 能操作网页：抓正文、点击、填表、截图，可复用登录态。",
    detail=(
        "内置固定版本的 Chrome for Testing，独立配置目录，不影响你日常用的浏览器。\n"
        "首次使用请在它弹出的窗口里登录目标网站，登录态会保存下来供 AI 复用。\n"
        "已知限制：这是自动化测试版浏览器，Google 账号登录可能被判定为「不安全的浏览器」，"
        "涉及 Google 服务的站点建议改用系统 Chrome。"
    ),
    version=_VERSION,
    urls={
        "win64": _u("win64"),
        "mac-arm64": _u("mac-arm64"),
        "mac-x64": _u("mac-x64"),
        "linux64": _u("linux64"),
    },
    # 升级 version 时必须同步更新这里的哈希（官方不提供 checksum 文件）
    # 校验值 = 完整下载该平台 zip 后的 sha256；空串表示尚未锁定（安装结果标「未验证」）
    sha256={
        "win64": "a4e0d57245e167e98a9d465af1e3774d2b4ec9f04e483d19bcd97a6f63cf46dd",
        "mac-arm64": "ad7f2b49a4dc679af286b1b1b386670d93ca7868079f69012d21ba0ac8293df3",
        "mac-x64": "5a38b8e3d621514fff0775dcae678a152ce378890188c88eef79fc97aa59fccd",
        "linux64": "e6c94490f0b6b337225aeb2d2a5034858516b7017356980ba94f5176d8774a50",
    },
    approx_size={
        "win64": 209154801,
        "mac-arm64": 194932023,
        "mac-x64": 205405847,
        "linux64": 200206486,
    },
    executable_globs={
        # win64 解压时剥掉 chrome-win64/ 公共根（规避 Windows 260 字符路径限制），
        # 所以候选里同时保留带根与不带根两种写法
        "win64": ["chrome.exe", "chrome-win64/chrome.exe", "*/chrome.exe", "**/chrome.exe"],
        "mac-arm64": ["chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                      "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                      "*/Google Chrome for Testing.app/Contents/MacOS/*"],
        "mac-x64": ["chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                    "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                    "*/Google Chrome for Testing.app/Contents/MacOS/*"],
        "linux64": ["chrome-linux64/chrome", "chrome", "*/chrome"],
    },
    requires_gui=True,
    hints={
        "darwin": "首次运行若被 Gatekeeper 拦截，请在终端执行：xattr -dr com.apple.quarantine \"<安装路径>\"",
        "linux": "无桌面环境下需以无头模式启动（无法扫码/输密码登录）。",
    },
)

_SPECS: dict[str, ComponentSpec] = {BROWSER_CHROMIUM.key: BROWSER_CHROMIUM}


def platform_key() -> str:
    """当前平台键：win64 / mac-arm64 / mac-x64 / linux64；其他返回空串。"""
    if sys.platform == "win32":
        return "win64"
    if sys.platform == "darwin":
        return "mac-arm64" if platform.machine().lower() in ("arm64", "aarch64") else "mac-x64"
    if sys.platform.startswith("linux"):
        # 目前只准备 x86_64 的包；ARM Linux 无对应上游产物
        return "linux64" if platform.machine().lower() in ("x86_64", "amd64") else "linux-arm64"
    return ""


def list_specs() -> list[ComponentSpec]:
    return list(_SPECS.values())


def get_spec(key: str) -> ComponentSpec | None:
    return _SPECS.get(key)
