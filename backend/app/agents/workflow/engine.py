"""工作流引擎：DAG 拓扑执行 + 变量引用解析 + 条件分支跳过。"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.services.permission import PrincipalSet

_VAR_RE = re.compile(r"\{\{\s*([\w\-\.]+)\s*\}\}")


class WorkflowPaused(Exception):
    """人工审批节点暂停执行。"""

    def __init__(self, node_id: str, info: dict) -> None:
        super().__init__(f"等待审批: {node_id}")
        self.node_id = node_id
        self.info = info
        # 附加到此处的已执行变量/记录，供调用方做 resume 快照
        self.variables: dict = {}
        self.records: list[dict] = []


def _resolve_path(ctx: dict, path: str) -> Any:
    cur: Any = ctx
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            raise ValidationError(f"变量不存在: {path}")
    return cur


def render(tpl: Any, variables: dict) -> Any:
    """渲染模板。整串恰为一个 {{x}} 时返回原始对象；否则字符串拼接。"""
    if not isinstance(tpl, str):
        return tpl
    m = _VAR_RE.fullmatch(tpl.strip())
    if m:
        return _resolve_path(variables, m.group(1))
    return _VAR_RE.sub(lambda mm: str(_resolve_path(variables, mm.group(1))), tpl)


def _render_deep(obj: Any, variables: dict) -> Any:
    if isinstance(obj, dict):
        return {k: _render_deep(v, variables) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_render_deep(v, variables) for v in obj]
    return render(obj, variables)


def validate_graph(graph: dict) -> None:
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    if not nodes:
        raise ValidationError("工作流为空")
    starts = [n for n in nodes if n.get("type") == "start"]
    if len(starts) != 1:
        raise ValidationError("必须且仅有一个 start 节点")
    if not any(n.get("type") == "end" for n in nodes):
        raise ValidationError("缺少 end 节点")
    ids = {n["id"] for n in nodes}
    for e in edges:
        if e.get("source") not in ids or e.get("target") not in ids:
            raise ValidationError(f"边引用了不存在的节点: {e}")


def topo_sort(graph: dict) -> list[str]:
    nodes = {n["id"]: n for n in graph["nodes"]}
    indeg = {nid: 0 for nid in nodes}
    succ: dict[str, list[str]] = defaultdict(list)
    for e in graph.get("edges") or []:
        succ[e["source"]].append(e["target"])
        indeg[e["target"]] = indeg.get(e["target"], 0) + 1
    q = deque([nid for nid, d in indeg.items() if d == 0])
    order: list[str] = []
    while q:
        nid = q.popleft()
        order.append(nid)
        for t in succ[nid]:
            indeg[t] -= 1
            if indeg[t] == 0:
                q.append(t)
    if len(order) != len(nodes):
        raise ValidationError("工作流存在环")
    return order


@dataclass
class NodeContext:
    db: AsyncSession
    ps: PrincipalSet
    run_id: int
    tenant_id: int
    variables: dict = field(default_factory=dict)
    inputs: dict = field(default_factory=dict)
    emit: Callable[[dict], Awaitable[None]] | None = None
    # 继承自 agent 的默认配置（节点未单独配置时回退）
    default_model_config_id: int | None = None
    default_kb_ids: list[int] | None = None
    default_system: str | None = None
    default_temperature: float | None = None
    # 审批决策 {node_id: "approve"|"reject"}（恢复执行时由调用方注入）
    approval: dict | None = None
    # 当前节点可见的上游变量（执行器内需要时读取，如 loop）
    scope: dict = field(default_factory=dict)
    # 工具级权限码集合（协作 agent 节点用）
    perms: set[str] | None = None
    # 协作调用链（防循环），agent 节点用
    agent_chain: list[int] = field(default_factory=list)
    # Token 用量累计：LLM 类节点每次调用都把 usage 累加到这里，
    # 运行结束时汇总写入 WorkflowRun，供多租户成本核算。
    usage: dict = field(default_factory=lambda: {
        "prompt_tokens": 0, "completion_tokens": 0, "calls": 0,
    })

    def add_usage(self, u: dict | None) -> None:
        """累加一次模型调用的用量。结构容错：不同 provider 字段名略有差异。"""
        if not isinstance(u, dict):
            return
        self.usage["prompt_tokens"] += int(
            u.get("prompt_tokens") or u.get("input_tokens") or 0
        )
        self.usage["completion_tokens"] += int(
            u.get("completion_tokens") or u.get("output_tokens") or 0
        )
        self.usage["calls"] += 1


# ==================== 节点执行器 ====================
# LLM 节点的系统提示兜底：节点与 agent 都未配置 system 时使用，保证引用规约不缺位
_LLM_FALLBACK_SYSTEM = (
    "你是企业知识助手。若上下文中带有【参考资料】或带编号的检索片段，请依据资料作答并在句末标注 [n] 引用，"
    "且不得编造引用；资料未覆盖时明确说明，并区分「知识库内容」与「模型自身知识」。用简体中文作答。"
)


async def _exec_start(node: dict, data: dict, ctx: NodeContext) -> dict:
    out = {}
    for inp in node.get("data", {}).get("inputs", []):
        name = inp.get("name")
        out[name] = ctx.inputs.get(name, inp.get("default"))
    return out


async def _exec_llm(node: dict, data: dict, ctx: NodeContext) -> dict:
    from app.providers.base import ChatMessage
    from app.providers.registry import get_llm

    d = node.get("data", {})
    # 节点未配置时回退到 agent 默认；两者都空时用「引用规约」兜底，
    # 保证工作流里的 AI 与问答页一样会区分【参考资料】与自身知识、标注 [n] 引用。
    system = d.get("system") or ctx.default_system or _LLM_FALLBACK_SYSTEM
    prompt = data.get("prompt") or d.get("prompt", "")
    model_config_id = d.get("model_config_id") or ctx.default_model_config_id
    llm, rm = await get_llm(ctx.db, tenant_id=ctx.tenant_id, config_id=model_config_id)
    msgs = []
    if system:
        msgs.append(ChatMessage(role="system", content=system))
    msgs.append(ChatMessage(role="user", content=str(prompt)))
    temp = d.get("temperature")
    if temp is None:
        temp = ctx.default_temperature if ctx.default_temperature is not None else 0.3
    res = await llm.chat(msgs, model=rm.model_name, temperature=temp)
    ctx.add_usage(res.usage)
    return {"output": res.content, "usage": res.usage}


async def _exec_retrieval(node: dict, data: dict, ctx: NodeContext) -> dict:
    from app.services.retrieval_service import retrieve

    d = node.get("data", {})
    query = str(data.get("query") or d.get("query", ""))
    # 节点未配置知识库时回退到 agent 绑定（空则全库，权限过滤兜底）
    kb_ids = d.get("kb_ids") or ctx.default_kb_ids or None
    top_k = int(d.get("top_k") or 5)
    resp = await retrieve(ctx.db, ps=ctx.ps, query=query, kb_ids=kb_ids, top_k=top_k)
    lines = []
    cites = []
    for i, c in enumerate(resp.chunks, start=1):
        src = c.doc_title or "文档"
        if c.page:
            src += f" 第{c.page}页"
        lines.append(f"[{i}] {src}\n{c.content}")
        cites.append({"chunk_id": c.chunk_id, "doc_id": c.doc_id, "doc_title": c.doc_title, "page": c.page, "score": c.score})
    body = "\n\n".join(lines)
    # 与对话路径一致：给检索内容加边界标记，降低间接注入（工作流里的 LLM 节点同样受益）
    from app.core.config import settings

    if body and settings.security_guard_enabled and settings.security_guard_wrap_context:
        from app.services.security_guard import wrap_untrusted

        body = wrap_untrusted(body)
    return {"output": body, "citations": cites, "count": len(resp.chunks)}


async def _exec_condition(node: dict, data: dict, ctx: NodeContext) -> dict:
    """白名单表达式求值（支持 ==,!=,>,<,>=,<=,contains）。"""
    d = node.get("data", {})
    expr = str(data.get("expression") or d.get("expression", "")).strip()
    result = _eval_expr(expr)
    return {"branch": "true" if result else "false", "value": result}


async def _exec_switch(node: dict, data: dict, ctx: NodeContext) -> dict:
    """多路分支：按 cases 顺序求值，命中即走对应 sourceHandle；全不中走 default。"""
    cases = data.get("cases") or (node.get("data") or {}).get("cases") or []
    for i, c in enumerate(cases):
        if not isinstance(c, dict):
            continue
        expr = str(c.get("expr") or "").strip()
        handle = str(c.get("handle") or f"case{i+1}")
        if expr and _eval_expr(expr):
            return {"branch": handle, "matched": handle, "value": True}
    return {"branch": "default", "matched": None, "value": False}


async def _exec_parallel(node: dict, data: dict, ctx: NodeContext) -> dict:
    """并行网关标记节点：真正的分支并发由 execute_graph 处理，这里只返回分支数。"""
    n = len([e for e in (node.get("_succ_count") or [])]) if node.get("_succ_count") else 0
    return {"branches": n}


async def _exec_join(node: dict, data: dict, ctx: NodeContext) -> dict:
    """汇聚节点（正常路径不会走到，由并行区域执行器写入输出）。"""
    return {"merged": True}



def _eval_expr(expr: str) -> bool:
    import re as _re

    expr = expr.strip()
    # 支持 AND / OR（先 OR 后 AND，AND 优先级更高）
    if _re.search(r"\s+OR\s+", expr, _re.IGNORECASE):
        parts = _re.split(r"\s+OR\s+", expr, flags=_re.IGNORECASE)
        return any(_eval_expr(p) for p in parts)
    if _re.search(r"\s+AND\s+", expr, _re.IGNORECASE):
        parts = _re.split(r"\s+AND\s+", expr, flags=_re.IGNORECASE)
        return all(_eval_expr(p) for p in parts)
    for op in (" contains ", " in "):
        if op in expr:
            left, right = expr.split(op, 1)
            l = left.strip().strip('"').strip("'")
            r = right.strip().strip('"').strip("'")
            return (r in l) if op == " contains " else (l in r)
    m = _re.match(r"^(.*?)(==|!=|>=|<=|>|<)(.*)$", expr)
    if m:
        l = _coerce(m.group(1).strip())
        r = _coerce(m.group(3).strip())
        op = m.group(2)
        try:
            if op == "==": return l == r
            if op == "!=": return l != r
            if op == ">": return l > r
            if op == "<": return l < r
            if op == ">=": return l >= r
            if op == "<=": return l <= r
        except TypeError:
            return False
    # 纯值：非空/非假即真
    return bool(_coerce(expr))


def _coerce(s: str) -> Any:
    s = s.strip().strip('"').strip("'")
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return s


async def _exec_http(node: dict, data: dict, ctx: NodeContext) -> dict:
    import httpx

    from app.agents.tools.builtin import _check_url

    d = node.get("data", {})
    url = str(data.get("url") or d.get("url", ""))
    method = (d.get("method") or "GET").upper()
    err = _check_url(url, d.get("allow_hosts"))
    if err:
        raise ValidationError(f"HTTP 节点被拒绝: {err}")

    def _as_obj(v, default):
        if v is None:
            return default
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:  # noqa: BLE001
                return default
        return v

    # 支持自定义请求头（如 Authorization）、查询参数与 JSON 请求体
    headers = {str(k): str(v) for k, v in (_as_obj(d.get("headers"), {}) or {}).items()}
    params = _as_obj(d.get("params"), {}) or {}
    json_body = _as_obj(d.get("json_body"), None)
    body = data.get("body") or d.get("body_template")
    async with httpx.AsyncClient(
        timeout=d.get("timeout", 20), follow_redirects=bool(d.get("follow_redirects", True))
    ) as client:
        resp = await client.request(
            method, url, headers=headers or None, params=params or None,
            content=body.encode() if body else None, json=json_body,
        )
    return {"output": resp.text[:65536], "status": resp.status_code}


async def _exec_end(node: dict, data: dict, ctx: NodeContext) -> dict:
    d = node.get("data", {})
    return {"output": data.get("output", d.get("output", ""))}


async def _exec_notify(node: dict, data: dict, ctx: NodeContext) -> dict:
    """消息推送节点：把内容推送到通知渠道或已接入的外部渠道。

    data（已渲染）：
      mode: "notify_channel"（默认）| "external_channel"
      channel_id: NotifyChannel.id 或 Channel.id
      target / target_type: external 模式下的接收对象（群/个人 id）
      content: 要推送的文本（支持 {{上游.output}}）
      title: 通知标题（notify_channel 模式，默认「工作流消息」）
    """
    from app.notifiers.base import NotificationMessage
    from app.notifiers.registry import build_notifier

    d = node.get("data", {})
    mode = str(d.get("mode") or "notify_channel")
    content = str(data.get("content") or d.get("content") or "").strip()
    if not content:
        raise ValidationError("推送节点内容为空")
    channel_id = int(d.get("channel_id") or 0)
    if not channel_id:
        raise ValidationError("推送节点未选择渠道")

    if mode == "external_channel":
        from app.channels.base import OutboundMessage
        from app.channels.manager import channel_manager

        target = str(data.get("target") or d.get("target") or "").strip()
        if not target:
            raise ValidationError("推送节点未填写接收对象 id")
        ttype = str(d.get("target_type") or "group").lower()
        kwargs = {"group_id": target} if ttype == "group" else {"user_id": target}
        ok = await channel_manager.send_with_fallback(channel_id, None, OutboundMessage(content=content, **kwargs))
        if not ok:
            raise ValidationError("外部渠道未连接或发送失败（请确认渠道已启用并在线）")
        return {"output": content, "sent": True}

    # notify_channel：走标准通知链路（站内/webhook/企微/钉钉/邮件/外部渠道）
    from app.models import NotifyChannel

    row = await ctx.db.get(NotifyChannel, channel_id)
    if not row or row.tenant_id != ctx.tenant_id:
        raise ValidationError("通知渠道不存在")
    cfg = dict(row.config or {})
    if row.kind == "inapp":
        cfg.setdefault("user_id", ctx.ps.user_id)
    notifier = build_notifier(row.kind, tenant_id=ctx.tenant_id, config=cfg)
    title = str(data.get("title") or d.get("title") or "工作流消息")
    ok = await notifier.send(NotificationMessage(
        title=title, body=content, level=str(d.get("level") or "info"),
        kind="workflow", ref_type="workflow_run", ref_id=ctx.run_id,
    ))
    return {"output": content, "sent": bool(ok)}


async def _exec_variable(node: dict, data: dict, ctx: NodeContext) -> dict:
    """变量赋值：下游可用 {{本节点id.变量名}} 引用。"""
    out: dict = {}
    for a in (data.get("assignments") or []):
        name = str(a.get("name") or "").strip()
        if name:
            out[name] = a.get("value")  # 已被 _render_deep 渲染
    return out or {"output": ""}


def _run_python(code: str, payload: dict, timeout: int) -> str:
    """在隔离子进程中执行 Python（stdin JSON → stdout JSON）。"""
    import json as _json
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "main.py"
        f.write_text(code, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-I", "-S", str(f)],
            input=_json.dumps(payload, ensure_ascii=False).encode(),
            capture_output=True, timeout=timeout, cwd=td,
        )
        if proc.returncode != 0:
            raise ValidationError(f"代码节点退出码 {proc.returncode}: {proc.stderr.decode('utf-8', 'ignore')[:400]}")
        return proc.stdout.decode("utf-8", "ignore")[:65536]


async def _exec_code(node: dict, data: dict, ctx: NodeContext) -> dict:
    """代码/脚本节点：执行 Python 片段。安全：-I -S 是隔离非沙箱，默认需管理员开启。"""
    import asyncio
    import json as _json

    d = node.get("data", {})
    code = str(d.get("code") or "")
    if not code.strip():
        raise ValidationError("代码节点内容为空")
    timeout = min(int(d.get("timeout") or 15), 60)
    payload = {"inputs": ctx.inputs, "args": data.get("args") or {}, "scope": ctx.scope}
    try:
        raw = await asyncio.wait_for(asyncio.to_thread(_run_python, code, payload, timeout), timeout=timeout + 3)
    except asyncio.TimeoutError:
        raise ValidationError("代码节点执行超时")
    try:
        parsed = _json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"output": parsed}
    except _json.JSONDecodeError:
        return {"output": raw}


async def _exec_loop(node: dict, data: dict, ctx: NodeContext) -> dict:
    """循环/批量：对 items 逐项执行内联 body，收集结果。"""
    import json as _json

    d = node.get("data", {})
    items = data.get("items")
    if isinstance(items, str):
        try:
            items = _json.loads(items)
        except Exception:  # noqa: BLE001
            items = [x for x in items.splitlines() if x.strip()]
    if not isinstance(items, list):
        items = []
    item_var = d.get("item_var") or "item"
    cap = min(int(d.get("max_iterations") or 50), 200)
    body = d.get("body") or {}
    results: list = []
    errors: list = []
    for idx, item in enumerate(items[:cap]):
        scope = {**ctx.scope, item_var: item, "item": item, "index": idx}
        try:
            results.append(await _run_loop_body(body, scope, ctx))
        except Exception as e:  # noqa: BLE001
            errors.append({"index": idx, "error": str(e)[:200]})
            results.append(None)
    out = {"results": results, "count": len(results), "errors": errors,
           "output": "\n".join(str(r) for r in results if r is not None)}
    # 默认严格模式：有失败项则整节点失败（下游不会误把缺失项当成功）；可显式 tolerant=true 容忍
    if errors and not d.get("tolerant"):
        raise ValidationError(f"循环执行有 {len(errors)}/{len(results)} 项失败：{errors[:3]}")
    return out


async def _run_loop_body(body: dict, scope: dict, ctx: NodeContext):
    """执行一次循环体（op: template / llm / code）。"""
    op = body.get("op") or "template"
    if op == "template":
        return render(body.get("template", ""), scope)
    if op == "llm":
        from app.providers.base import ChatMessage
        from app.providers.registry import get_llm

        system = _render_deep(body.get("system") or ctx.default_system or "", scope)
        prompt = _render_deep(body.get("prompt") or "", scope)
        llm, rm = await get_llm(ctx.db, tenant_id=ctx.tenant_id, config_id=ctx.default_model_config_id)
        msgs = []
        if system:
            msgs.append(ChatMessage(role="system", content=str(system)))
        msgs.append(ChatMessage(role="user", content=str(prompt)))
        temp = ctx.default_temperature if ctx.default_temperature is not None else 0.3
        res = await llm.chat(msgs, model=rm.model_name, temperature=temp)
        ctx.add_usage(res.usage)
        return res.content
    if op == "code":
        import asyncio

        code = str(body.get("code") or "")
        timeout = min(int(body.get("timeout") or 15), 60)
        payload = {"scope": scope}
        raw = await asyncio.wait_for(asyncio.to_thread(_run_python, code, payload, timeout), timeout=timeout + 3)
        return raw.strip()
    raise ValidationError(f"未知循环体类型: {op}")


async def _exec_approval(node: dict, data: dict, ctx: NodeContext) -> dict:
    """人工审批节点：无决策时暂停（HITL）。"""
    d = node.get("data", {})
    decision = (ctx.approval or {}).get(node["id"]) if ctx.approval else None
    if not decision:
        raise WorkflowPaused(node["id"], {
            "title": d.get("title") or "需要人工审批",
            "content": str(data.get("content") or d.get("content") or ""),
        })
    return {"approved": decision == "approve", "comment": decision}


async def _exec_file(node: dict, data: dict, ctx: NodeContext) -> dict:
    """文件处理节点：读取/生成/转换，复用 file_tools。"""
    from app.agents.tools.base import ToolContext
    from app.agents.tools.file_tools import ConvertFileToTool, GenerateFileTool, ReadFileTool

    d = node.get("data", {})
    op = str(d.get("op") or "read")
    tctx = ToolContext(
        db=ctx.db, ps=ctx.ps, tenant_id=ctx.tenant_id, user_id=0,
        conversation_id=None, agent_id=None, config={},
    )
    args = data.get("args") or {}
    if op == "read":
        res = await ReadFileTool().run(args, tctx)
    elif op == "generate":
        res = await GenerateFileTool().run(args, tctx)
    elif op == "convert":
        res = await ConvertFileToTool().run(args, tctx)
    else:
        raise ValidationError(f"未知文件操作: {op}")
    if res.is_error:
        raise ValidationError(res.content)
    out = {"output": res.content}
    if res.data:
        out.update(res.data)
    return out


async def _exec_agent(node: dict, data: dict, ctx: NodeContext) -> dict:
    """调用另一个智能体（协作）。以发起者身份运行，防循环。"""
    from app.agents.runner import AgentRunner

    d = node.get("data", {})
    target_id = int(d.get("agent_id") or 0)
    if not target_id:
        raise ValidationError("agent 节点未配置目标智能体")

    # 防循环 + 深度限制
    if target_id in ctx.agent_chain:
        raise ValidationError(f"检测到智能体循环调用: {target_id}")
    if len(ctx.agent_chain) >= int(d.get("max_depth") or 3):
        raise ValidationError("智能体调用深度超限")

    from app.models import Agent, Conversation

    target = await ctx.db.get(Agent, target_id)
    if not target or target.tenant_id != ctx.tenant_id:
        raise ValidationError("目标智能体不存在")

    conv = Conversation(
        tenant_id=ctx.tenant_id, user_id=ctx.ps.user_id,
        title=f"[协作] {target.name}", kb_ids=target.kb_ids or [],
        settings={"workflow_run_id": ctx.run_id},
    )
    ctx.db.add(conv)
    await ctx.db.flush()

    prompt = str(data.get("input") or d.get("input_template") or "")
    ctx.agent_chain.append(target_id)
    runner = AgentRunner(
        ctx.db, agent=target, ps=ctx.ps, conversation=conv,
        history=[], perms=ctx.perms if ctx.perms is not None else set(), summary=None,
    )
    text, cites = "", []
    try:
        async for evt in runner.run(prompt, mode_id=d.get("mode_id")):
            if evt.get("type") == "delta":
                text += evt.get("text", "")
            elif evt.get("type") == "citations":
                cites.extend(evt.get("citations") or [])
            elif evt.get("type") == "pending_action":
                # 工作流内不支持挂起确认：拒绝写操作
                raise ValidationError("协作智能体触发了写操作，请在工作流外处理")
    finally:
        ctx.agent_chain.pop()
    return {"output": text, "citations": cites}


NODE_EXECUTORS: dict[str, Callable] = {
    "start": _exec_start,
    "llm": _exec_llm,
    "knowledge_retrieval": _exec_retrieval,
    "condition": _exec_condition,
    "switch": _exec_switch,
    "http": _exec_http,
    "file": _exec_file,
    "variable": _exec_variable,
    "code": _exec_code,
    "loop": _exec_loop,
    "approval": _exec_approval,
    "agent": _exec_agent,
    "notify": _exec_notify,
    "parallel": _exec_parallel,
    "join": _exec_join,
    "end": _exec_end,
}

# 扩展节点（电商/办公场景）：定义在 nodes_extra，这里合并进同一注册表，
# 执行路径、retry、变量渲染完全复用，无需改动主循环。
from app.agents.workflow.nodes_extra import EXTRA_EXECUTORS as _EXTRA_EXECUTORS  # noqa: E402

NODE_EXECUTORS.update(_EXTRA_EXECUTORS)

# 已知节点类型（供 AI 生成与前端校验用）
KNOWN_NODE_TYPES: tuple[str, ...] = tuple(NODE_EXECUTORS.keys())

# 这些字段不在执行前渲染（含循环局部变量，由执行器内逐项渲染）
_DEFERRED_RENDER: dict[str, tuple[str, ...]] = {"loop": ("body",)}

# 并行分支内暂不支持的节点类型（v1：保证并发执行的正确性）
_PARALLEL_FORBIDDEN = ("condition", "switch", "parallel", "join", "approval", "loop")


def _resolve_parallel_region(
    p_id: str,
    nodes: dict,
    succ: dict[str, list[tuple[str, str | None]]],
    pred: dict[str, int],
) -> tuple[list[list[str]], str]:
    """解析 parallel→...(线性分支)...→join 区域。

    返回 (各分支的节点链列表, join 节点 id)。分支必须是线性链（单出边），
    且都汇聚到同一 join（入度≥2）。
    """
    heads = [t for (t, _) in succ.get(p_id, [])]
    if len(heads) < 2:
        raise ValidationError("parallel 节点至少需要两条出边（两个并行分支）")
    branches: list[list[str]] = []
    joins: list[str] = []
    for h in heads:
        path: list[str] = []
        cur = h
        while True:
            if cur in path:
                raise ValidationError("并行分支存在环")
            if pred.get(cur, 0) >= 2:
                joins.append(cur)
                break
            path.append(cur)
            ntype = nodes[cur]["type"]
            if ntype in _PARALLEL_FORBIDDEN:
                raise ValidationError(f"并行分支内暂不支持 {ntype} 节点（v1）")
            out = succ.get(cur, [])
            if len(out) > 1:
                raise ValidationError(f"并行分支内的节点 {cur} 不支持再分叉（v1）")
            if not out:
                raise ValidationError("并行分支未汇聚到 join 节点")
            cur = out[0][0]
        branches.append(path)
    if len(set(joins)) != 1:
        raise ValidationError("并行分支必须汇聚到同一个 join 节点")
    join_id = joins[0]
    if nodes[join_id]["type"] != "join":
        raise ValidationError(f"并行分支的汇聚点 {join_id} 必须是 join 节点")
    return branches, join_id


async def _run_parallel_region(
    p_id: str, nodes: dict, succ, pred, base_variables: dict, ctx: NodeContext
) -> tuple[dict, list[dict], str, list[list[str]], list[dict]]:
    """并发执行 parallel 区域：每条分支独立 session + NodeContext 副本，asyncio.gather 并发。

    返回 (合并后的变量, 各分支输出列表, join_id, 分支链, 分支内节点记录)。
    最后一项用于把分支节点写进 NodeRun —— 早先分支节点既不发事件也不落记录，
    编排页看不到它们执行过。
    """
    branches, join_id = _resolve_parallel_region(p_id, nodes, succ, pred)
    branch_records: list[dict] = []

    async def _one(path: list[str], branch_idx: int) -> dict:
        from app.core.db import AsyncSessionLocal

        bvars = dict(base_variables)
        outs: dict = {}
        async with AsyncSessionLocal() as bdb:
            bctx = NodeContext(
                db=bdb, ps=ctx.ps, run_id=ctx.run_id, tenant_id=ctx.tenant_id,
                # 分支内的进度事件直接转发给主 ctx.emit：
                # 事件带 branch 标记，前端按 node_id 定位节点，不会交错错乱。
                # 早先 emit=None 会让画布上分支节点一直灰着，看起来像卡死。
                emit=ctx.emit,
                default_model_config_id=ctx.default_model_config_id,
                default_kb_ids=ctx.default_kb_ids,
                default_system=ctx.default_system,
                default_temperature=ctx.default_temperature,
                approval=ctx.approval, perms=ctx.perms,
                agent_chain=list(ctx.agent_chain or []),
            )
            for nid in path:
                nd = nodes[nid]
                data = _render_deep(nd.get("data") or {}, {**bvars, "inputs": ctx.inputs})
                bctx.scope = {**bvars, "inputs": ctx.inputs}
                ex = NODE_EXECUTORS.get(nd["type"])
                if not ex:
                    raise ValidationError(f"未知节点类型: {nd['type']}")
                if ctx.emit:
                    await ctx.emit({"type": "node_started", "node_id": nid,
                                    "node_type": nd["type"], "branch": branch_idx})
                t0 = time.monotonic()
                out = await ex(nd, data, bctx)
                bvars[nid] = out
                outs[nid] = out
                if ctx.emit:
                    await ctx.emit({
                        "type": "node_finished", "node_id": nid, "node_type": nd["type"],
                        "status": "success", "input": data, "output": out,
                        "latency_ms": int((time.monotonic() - t0) * 1000),
                        "branch": branch_idx,
                    })
                branch_records.append({
                    "node_id": nid, "node_type": nd["type"], "seq": 0, "status": "success",
                    "input": data, "output": out,
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                })
        return outs

    results = await asyncio.gather(
        *[_one(p, i) for i, p in enumerate(branches)], return_exceptions=True
    )
    merged: dict = {}
    branch_list: list[dict] = []
    for r in results:
        if isinstance(r, BaseException):
            raise r
        merged.update(r)
        branch_list.append(r)
    return merged, branch_list, join_id, branches, branch_records



async def execute_graph(
    graph: dict, run_input: dict, ctx: NodeContext, resume: dict | None = None
) -> tuple[dict, list[dict]]:
    """执行 DAG。返回 (最终输出, 节点运行记录列表)。

    resume={"variables": {...}} 时，已完成节点直接复用缓存输出（用于审批恢复）。
    """
    validate_graph(graph)
    # 以本次调用的 run_input 为准（ctx.inputs 可能是创建时的默认值）
    ctx.inputs = run_input
    nodes = {n["id"]: n for n in graph["nodes"]}
    succ: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
    pred: dict[str, int] = defaultdict(int)
    for e in graph["edges"]:
        succ[e["source"]].append((e["target"], e.get("sourceHandle")))
        pred[e["target"]] += 1
    order = topo_sort(graph)

    active: set[str] = set()
    start = next(n for n in graph["nodes"] if n["type"] == "start")
    active.add(start["id"])
    variables: dict[str, dict] = dict((resume or {}).get("variables") or {})
    completed = set(variables.keys())
    records: list[dict] = []
    seq = 0
    # 并行区域内的节点（由 parallel 区域统一执行，主循环跳过）
    region_nodes: dict[str, str] = {}  # node_id → parallel_id

    for nid in order:
        node = nodes[nid]
        # 并行区域内的节点：主循环跳过（已由 parallel 区域并发执行并写入 variables）
        if nid in region_nodes:
            continue
        if nid not in active:
            records.append({"node_id": nid, "node_type": node["type"], "seq": seq, "status": "skipped"})
            if ctx.emit:
                await ctx.emit({"type": "node_finished", "node_id": nid, "node_type": node["type"], "status": "skipped"})
            seq += 1
            continue

        # ---- 并行区域：并发执行所有分支，归并到 join，跳过区域内的普通节点 ----
        if node["type"] == "parallel" and nid not in completed:
            if ctx.emit:
                await ctx.emit({"type": "node_started", "node_id": nid, "node_type": "parallel"})
            t0 = time.monotonic()
            merged, branch_list, join_id, branches, branch_records = await _run_parallel_region(
                nid, nodes, succ, pred, variables, ctx
            )
            lat = int((time.monotonic() - t0) * 1000)
            variables.update(merged)
            variables[nid] = {"branches": len(branches)}
            variables[join_id] = {"branches": branch_list}
            # 登记区域内全部节点，主循环跳过
            for path in branches:
                for b in path:
                    region_nodes[b] = nid
            region_nodes[join_id] = nid
            # 分支内节点也落记录（seq 统一排在 parallel 之前）
            for br in branch_records:
                records.append({**br, "seq": seq})
                seq += 1
            records.append({"node_id": nid, "node_type": "parallel", "seq": seq, "status": "success",
                            "output": {"branches": len(branches)}, "latency_ms": lat})
            records.append({"node_id": join_id, "node_type": "join", "seq": seq + 1, "status": "success",
                            "output": {"branches": branch_list}})
            seq += 2
            if ctx.emit:
                await ctx.emit({"type": "node_finished", "node_id": nid, "status": "success",
                                "output": {"branches": len(branches)}, "latency_ms": lat})
                await ctx.emit({"type": "node_finished", "node_id": join_id, "status": "success"})
            # join 的下游激活
            for tgt, _ in succ[join_id]:
                active.add(tgt)
            continue

        # 恢复模式：已完成节点复用缓存，但仍需重放分支激活
        if nid in completed:
            out = variables[nid]
            records.append({"node_id": nid, "node_type": node["type"], "seq": seq, "status": "success",
                            "output": out, "replayed": True})
            if node["type"] in ("condition", "switch"):
                taken = out.get("branch")
                for tgt, handle in succ[nid]:
                    if handle == taken:
                        active.add(tgt)
            else:
                for tgt, _ in succ[nid]:
                    active.add(tgt)
            seq += 1
            continue
        raw = node.get("data") or {}
        defer = _DEFERRED_RENDER.get(node["type"], ())
        if defer:
            renderable = {k: v for k, v in raw.items() if k not in defer}
            data = _render_deep(renderable, {**variables, "inputs": ctx.inputs})
            for k in defer:
                data[k] = raw.get(k)
        else:
            data = _render_deep(raw, {**variables, "inputs": ctx.inputs})
        executor = NODE_EXECUTORS.get(node["type"])
        if not executor:
            raise ValidationError(f"未知节点类型: {node['type']}")
        ctx.scope = {**variables, "inputs": ctx.inputs}
        if ctx.emit:
            await ctx.emit({"type": "node_started", "node_id": nid, "node_type": node["type"]})
        retries = int((node.get("data") or {}).get("retry") or 0)
        attempt = 0
        out = None
        last_err: Exception | None = None
        t0 = time.monotonic()
        while attempt <= retries:
            try:
                out = await executor(node, data, ctx)
                last_err = None
                break
            except WorkflowPaused as p:
                p.variables = dict(variables)
                p.records = records + [{
                    "node_id": nid, "node_type": node["type"], "seq": seq, "status": "waiting",
                }]
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
                attempt += 1
        latency_ms = int((time.monotonic() - t0) * 1000)
        if last_err is not None:
            records.append({"node_id": nid, "node_type": node["type"], "seq": seq, "status": "failed",
                            "error_msg": str(last_err)[:300], "retry_count": attempt - 1, "latency_ms": latency_ms})
            if ctx.emit:
                await ctx.emit({"type": "node_finished", "node_id": nid, "status": "failed",
                                "error": str(last_err)[:300], "latency_ms": latency_ms})
            raise last_err
        variables[nid] = out
        records.append({"node_id": nid, "node_type": node["type"], "seq": seq, "status": "success",
                        "input": data, "output": out, "retry_count": attempt, "latency_ms": latency_ms})
        if ctx.emit:
            await ctx.emit({"type": "node_finished", "node_id": nid, "status": "success",
                            "input": data, "output": out, "latency_ms": latency_ms})
        seq += 1

        if node["type"] in ("condition", "switch"):
            taken = out.get("branch")
            for tgt, handle in succ[nid]:
                if handle == taken:
                    active.add(tgt)
        else:
            for tgt, _ in succ[nid]:
                active.add(tgt)

    # 取实际执行成功的 end 节点输出（条件分支下可能有多个 end）
    executed_ends = [
        variables[n["id"]]
        for n in graph["nodes"]
        if n["type"] == "end" and n["id"] in variables
    ]
    if executed_ends:
        return executed_ends[-1], records
    end = next(n for n in graph["nodes"] if n["type"] == "end")
    return variables.get(end["id"], {}), records
