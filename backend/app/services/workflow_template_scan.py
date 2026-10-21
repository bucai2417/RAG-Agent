"""工作流模板风险扫描：发布到公共库前评估图的安全风险。

为什么必须有这一步：跨租户套用一个带 `code` 节点的工作流，等于执行别人写的
Python（虽然是隔离子进程 + 资源限额，但仍能读环境变量、发网络请求）。
`http` / `http_form` / `webhook` 节点则可能把数据发往任意地址。

分级策略（按「套用后果」而非「节点存在」判断）：
- high：含 code 节点（可执行任意代码）
- medium：含对外请求节点（http / http_form / webhook / email / notify）
- low：其余

设计要点：
1. **只给结论，不阻断发布**。审核员看到风险等级再决定，工具不该越权替人拒绝。
2. **提取证据**。列出具体节点 id 与关键片段，让审核员能判断而非只看一个标签。
3. **纯静态**。不执行任何代码，不发起任何请求。
"""
from __future__ import annotations

# 节点类型 → 风险级别与说明
_RISK_RULES: dict[str, tuple[str, str]] = {
    "code": ("high", "可执行任意 Python 代码"),
    "http": ("medium", "可向任意地址发起 HTTP 请求"),
    "http_form": ("medium", "可向任意地址提交表单数据"),
    "webhook": ("medium", "可回调任意外部地址"),
    "email": ("medium", "可发送邮件到任意收件人"),
    "notify": ("medium", "可推送消息到外部渠道"),
    "agent": ("low", "可调用其他智能体"),
    "file": ("low", "可读写文件"),
}

_LEVEL_ORDER = {"low": 0, "medium": 1, "high": 2}


def scan_graph(graph: dict) -> dict:
    """扫描工作流图，返回 {level, findings, node_count}。

    level 取所有发现中的最高级；无风险节点时为 "none"。
    findings: [{node_id, node_type, level, reason, hint}]
      hint 是给审核员看的「具体可疑内容」（最长 200 字）。
    """
    nodes = (graph or {}).get("nodes") or []
    findings: list[dict] = []

    for n in nodes:
        if not isinstance(n, dict):
            continue
        ntype = str(n.get("type") or "")
        rule = _RISK_RULES.get(ntype)
        if not rule:
            continue
        level, reason = rule
        findings.append({
            "node_id": str(n.get("id") or ""),
            "node_type": ntype,
            "level": level,
            "reason": reason,
            "hint": _hint_for(n, ntype),
        })

    if not findings:
        return {"level": "none", "findings": [], "node_count": len(nodes)}

    top = max(findings, key=lambda f: _LEVEL_ORDER.get(f["level"], 0))
    return {
        "level": top["level"],
        "findings": findings,
        "node_count": len(nodes),
        "summary": _summary(findings),
    }


def _hint_for(node: dict, ntype: str) -> str:
    """从节点配置里提取一段可供人工判断的内容。"""
    data = node.get("data") or {}
    if ntype == "code":
        code = str(data.get("code") or "").strip()
        return code[:200]
    if ntype in ("http", "http_form", "webhook"):
        return str(data.get("url") or "")[:200]
    if ntype == "email":
        return f"收件人: {str(data.get('to') or '')[:120]}"
    if ntype == "notify":
        return f"渠道: {data.get('channel_id')} / 接收: {str(data.get('target') or '')[:80]}"
    if ntype == "file":
        return f"操作: {data.get('op') or 'read'}"
    if ntype == "agent":
        return f"智能体: {data.get('agent_id')}"
    return ""


def _summary(findings: list[dict]) -> str:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["level"]] = counts.get(f["level"], 0) + 1
    parts = []
    for lvl in ("high", "medium", "low"):
        if counts.get(lvl):
            parts.append(f"{lvl} {counts[lvl]} 个")
    return "、".join(parts)


def check_publishable(graph: dict, *, allow_high_risk: bool = False) -> tuple[bool, str]:
    """判断是否允许发布到公共库。返回 (允许, 原因)。

    含 code 节点的模板默认不允许直接公开 —— 这个门槛可以管理员显式放开
    （allow_high_risk），因为审核员可能确认过代码是安全的。
    """
    r = scan_graph(graph)
    if r["level"] == "none" or r["level"] == "low":
        return True, ""
    if r["level"] == "medium" and not allow_high_risk:
        # 中风险允许发布，但审核员要看到
        return True, r.get("summary", "")
    if r["level"] == "high" and not allow_high_risk:
        return False, (
            f"模板包含可执行代码节点（{r.get('summary', '')}）。"
            "如需公开，请联系管理员审核后放行。"
        )
    return True, r.get("summary", "")
