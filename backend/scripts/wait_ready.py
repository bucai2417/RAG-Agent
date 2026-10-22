"""启动探活：供 start.bat 与桌面端（Electron）判断后端是否已就绪。

为什么不用 curl / PowerShell：Windows 环境不一定有 curl；PowerShell 冷启动
本身要 0.7-2s，光探测就比后端启动还慢。这里直接用标准库发一次 GET。

用法::

    python scripts/wait_ready.py --port 6677 --timeout 60
    python scripts/wait_ready.py --port 6677 --probe     # 只探测一次，不等待

退出码：
    0  已就绪（/health 返回 200）
    1  等待超时 / 探测未通过
    2  参数错误
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request


def _healthy(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/health", timeout=timeout
        ) as resp:
            if resp.status != 200:
                return False
            # /health 的 status 可能因 DB 降级为 degraded，但仍算「起来了」
            body = json.loads(resp.read().decode("utf-8") or "{}")
            return body.get("status") in ("ok", "degraded")
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """端口是否已被监听（用于检测「已有实例在跑」）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--probe", action="store_true", help="只探测一次，不等待")
    args = ap.parse_args(argv)

    if args.probe:
        return 0 if _healthy(args.port, args.host) else 1

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if _healthy(args.port, args.host):
            return 0
        time.sleep(0.25)
    return 1


if __name__ == "__main__":
    sys.exit(main())
