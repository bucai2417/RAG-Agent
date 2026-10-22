"""判断前端产物是否需要重新构建（供 start.bat 调用，避免每次启动都全量构建）。

为什么不用 PowerShell：`Get-ChildItem -Recurse` 在本项目上要 ~20s，
而同样的 os.scandir 遍历只要 ~0.3s。启动脚本用 Python 判断更划算。

用法::

    python scripts/needs_build.py --web-dir ../web        # 需要构建时退出码 1
    python scripts/needs_build.py --web-dir ../web --force

退出码：
    0  产物是最新的，可以跳过构建
    1  需要构建（产物缺失，或源码比产物新）
    2  参数错误（web 目录不存在）
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 参与比较的输入：源码目录 + 影响产物的配置文件
_WATCHED_DIRS = ("src", "public")
_WATCHED_FILES = ("index.html", "package.json", "vite.config.ts", "tsconfig.json", "package-lock.json")
_SKIP_DIRS = {".git", ".vite", "dist"}


def _newest_mtime(root: Path) -> float:
    """返回 root 下所有输入文件的最新修改时间。"""
    newest = 0.0

    def scan(directory: Path) -> None:
        nonlocal newest
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                if entry.name in _SKIP_DIRS:
                    continue
                scan(Path(entry.path))
            else:
                try:
                    newest = max(newest, entry.stat().st_mtime)
                except OSError:
                    continue

    for name in _WATCHED_DIRS:
        sub = root / name
        if sub.is_dir():
            scan(sub)
    for name in _WATCHED_FILES:
        f = root / name
        if f.is_file():
            try:
                newest = max(newest, f.stat().st_mtime)
            except OSError:
                continue
    return newest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--web-dir", required=True)
    ap.add_argument("--force", action="store_true", help="无论是否最新都返回 1")
    args = ap.parse_args(argv)

    root = Path(args.web_dir).resolve()
    if not root.is_dir():
        return 2
    if args.force:
        return 1

    built = root / "dist" / "index.html"
    if not built.is_file():
        return 1
    try:
        built_mtime = built.stat().st_mtime
    except OSError:
        return 1

    return 1 if _newest_mtime(root) > built_mtime else 0


if __name__ == "__main__":
    sys.exit(main())
