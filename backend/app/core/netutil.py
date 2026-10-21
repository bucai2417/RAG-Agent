"""本机访问地址探测：启动时告诉用户该用哪个 URL 打开后台。

为什么需要：服务默认绑定 0.0.0.0，日志里只有内部事件，普通使用者（尤其同事/客户）
不知道要用哪个地址访问、也不知道同一局域网内其他人能不能连。启动时打一次访问地址，
比事后翻文档更有效。

设计要点：
- **不依赖 uvicorn 内部对象**：lifespan 在 socket 绑定之前执行，拿不到实际监听端口，
  所以按 CLI 参数 → 环境变量 → settings 的优先级自行推断。
- **绑定 127.0.0.1 时不展示局域网地址**：那些地址根本连不上，展示反而是误导。
"""
from __future__ import annotations

import socket
import sys


def _arg_value(flag: str) -> str | None:
    """从 sys.argv 里取 `--port 6677` / `--port=6677` 形式的值。"""
    argv = sys.argv
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def resolve_bind(default_port: int) -> tuple[str, int]:
    """推断当前进程实际监听的 (host, port)。

    优先级：命令行参数 > 环境变量（RAG_HOST/RAG_PORT/PORT）> settings 默认值。
    """
    import os

    host = _arg_value("--host") or os.environ.get("RAG_HOST") or "0.0.0.0"
    port_raw = (
        _arg_value("--port")
        or os.environ.get("RAG_PORT")
        or os.environ.get("PORT")
        or str(default_port)
    )
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        port = default_port
    return host, port


def _is_loopback(host: str) -> bool:
    h = (host or "").strip().lower()
    return h in ("127.0.0.1", "localhost", "::1")


def local_ipv4_addresses() -> list[str]:
    """列出本机非环回 IPv4 地址（用于展示局域网访问地址）。

    优先用「UDP connect 不发包」技巧拿到默认出口网卡地址（最可能是同事要用的那个），
    再用 getaddrinfo 补充其它网卡。去重并跳过环回、链路本地地址。

    虚拟网卡（VMware/Hyper-V/WSL 的 192.168.137.x、192.168.56.x 等 Host-Only 网段）
    也过滤掉 —— 它们只在本机内部有效，展示给同事反而造成误解。
    """
    found: list[str] = []

    # 默认路由出口地址（UDP 只设置目标，不会真的发包）
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        if ip:
            found.append(ip)
    except OSError:
        pass
    finally:
        s.close()

    # 其它网卡
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass

    out: list[str] = []
    for ip in found:
        if not ip or ip in out:
            continue
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        # 常见虚拟网卡 Host-Only 网段（Windows 热点 / VMware / VirtualBox / WSL）
        if ip.startswith(("192.168.137.", "192.168.56.", "192.168.64.", "192.168.99.")):
            continue
        out.append(ip)
    return out


def access_urls(host: str, port: int) -> list[dict]:
    """返回可访问地址列表：[{scope, url, note}]。

    scope: local（本机）/ lan（同局域网）/ all（监听所有网卡时的占位说明）
    """
    urls: list[dict] = [{"scope": "local", "url": f"http://127.0.0.1:{port}"}]

    if _is_loopback(host):
        # 只监听环回：局域网不可达，不展示误导性地址
        return urls

    for ip in local_ipv4_addresses():
        urls.append({"scope": "lan", "url": f"http://{ip}:{port}"})

    if not any(u["scope"] == "lan" for u in urls):
        urls.append({
            "scope": "lan",
            "url": f"http://<本机局域网IP>:{port}",
            "note": "未探测到局域网 IPv4 地址（可用 ipconfig 查看）",
        })
    return urls
