"""工作流 AI 生成：自然语言需求 → DAG（graph）。

设计要点（勿凭直觉改动）：

1. **只让模型产出「节点语义」，布局由程序算**。LLM 很擅长选节点、连边、写提示词，
   但极不擅长算坐标（会重叠、越界）。所以要求它给出 nodes[{id,type,data}] 与
   edges[{source,target,sourceHandle}]，坐标由 `_layout()` 按拓扑层算法自动生成。

2. **强校验 + 自动修复**。LLM 输出常见错误：引用不存在的节点、缺 start/end、
   用了未注册的节点类型、data 里写错 key。`sanitize_graph()` 逐项修复，
   修复不了就报错并把原因说清楚 —— 不能把坏图存进去。

3. **节点规格要喂给模型**。把 `NODE_SPECS` 精简后放进提示词。规格不全会导致
   模型编造字段名，执行时静默取不到值，比报错更难查。

4. **输出必须是 JSON**。用 response_format 引导 + 正则兜底抽取，避免模型加解说词。
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.core.errors import ValidationError
from app.core.logging import get_logger

logger = get_logger("workflow_ai")

# 节点规格：给模型看的「能力清单」。新增节点时务必同步这里，
# 否则 AI 不会使用它（而不是用错 —— 用错会在 sanitize 阶段被拦下）。
NODE_SPECS: dict[str, str] = {
    "start": '入口。data={"inputs":[{"name":"query","type":"string","required":true}]}。只能有一个，无入边。',
    "end": '出口。data={"output":"{{llm_1.output}}"}。必须存在，无出边。',
    "llm": '调用大模型。data={"system":"角色设定","prompt":"用户提示，可用 {{节点id.output}} 引用上游","temperature":0.3}。输出 {{id.output}}。',
    "knowledge_retrieval": '检索知识库。data={"query":"{{start.query}}","kb_ids":[],"top_k":5}。输出 {{id.output}}（带 [n] 编号的片段）。',
    "condition": '二分支判断。data={"expression":"{{llm_1.output}} contains \'无法回答\'"}。两条出边 sourceHandle 分别为 "true"/"false"。支持 == != > < >= <= contains in 与 AND/OR。',
    "switch": '多路分支。data={"cases":[{"handle":"case1","expr":"..."}]}，出边 sourceHandle=handle，兜底走 "default"。',
    "router": '按分类结果路由（适合 AI 分类输出）。data={"value":"{{摘要_1.output}}","cases":[{"handle":"售后","values":["退货","退款"]}]}。出边 sourceHandle=handle，兜底 "default"。',
    "parallel": '并行网关。至少两条出边，各分支线性走到同一个 join。分支内不能有 condition/switch/loop/approval。',
    "join": '并行汇聚。输出 {{id.branches}} 为各分支结果数组。',
    "http": 'HTTP 请求。data={"url":"https://...","method":"GET|POST","headers":{},"body_template":"","timeout":20}。输出 {{id.output}} 与 {{id.status}}。',
    "http_form": '表单提交（电商下单/回填）。data={"url":"...","method":"POST","encoding":"form|json","fields":[{"name":"a","value":"{{start.query}}"}]}。',
    "webhook": '出站 Webhook（通知外部系统）。data={"url":"...","fields":[{"name":"text","value":"{{llm_1.output}}"}]}。',
    "file": '文件读写/生成。data={"op":"read|generate|convert","args":{...}}。generate 时 args={"filename":"报表.xlsx","content":"{{表格_1.csv}}","fmt":"xlsx"}。',
    "table": '表格处理：过滤/排序/投影/聚合/转 CSV。data={"rows":"{{http_1.output}}","filters":[{"field":"status","op":"eq","value":"未付款"}],"sort":{"field":"amount","desc":true},"fields":[{"name":"订单号","from":"order_id"}],"aggregates":[{"name":"总额","field":"amount","op":"sum"}],"limit":100,"as_csv":true}。输出 {{id.rows}} {{id.csv}} {{id.summary}}。',
    "transform": '数据清洗。data={"value":"{{start.query}}","op":"to_text|split|join|extract|to_number","separator":"\\n","pattern":"\\\\d+","group":0}。',
    "collection": '集合运算。data={"lists":["{{a.items}}","{{b.items}}"],"op":"merge|union|intersect|diff"}。输出 {{id.items}}。',
    "mapping": '数据重塑成目标结构（对接第三方 API）。data={"mappings":[{"key":"orderNo","value":"{{start.query}}"}]}。输出 {{id.data}}。',
    "json_path": '按路径取 JSON 字段。data={"source":"{{http_1.output}}","path":"data.items.0.id"}。输出 {{id.value}}。',
    "text_template": '拼接文本（邮件/报告正文）。data={"sections":[{"title":"概要","content":"{{llm_1.output}}"}]}。输出 {{id.text}}。',
    "email": '发邮件。data={"account_id":null,"to":"a@b.com","subject":"标题","body":"{{text_1.output}}"}。需事先配置个人邮箱账号。',
    "summarize": '摘要/抽取/分类/情感（内置提示词，比 llm 更省心）。data={"text":"{{http_1.output}}","mode":"summary|extract|classify|sentiment"}。',
    "batch": '分批。data={"items":"{{http_1.items}}","size":10}。输出 {{id.batches}}。',
    "loop": '逐项循环。data={"items":"{{http_1.items}}","item_var":"item","max_iterations":50,"body":{"op":"template|llm|code", ...}}。body 内用 {{item}}。',
    "code": 'Python 脚本（隔离子进程）。data={"code":"...","timeout":15}。从 stdin 读 {inputs,args,scope}，stdout 打印 JSON。',
    "variable": '变量赋值。data={"assignments":[{"name":"x","value":"{{start.query}}"}]}。输出 {{id.x}}。',
    "agent": '调用另一个智能体。data={"agent_id":null,"input_template":"{{start.query}}"}。',
    "notify": '推送消息到通知渠道。data={"mode":"notify_channel","channel_id":null,"title":"...","content":"{{llm_1.output}}"}。',
    "approval": '人工审批，命中即暂停等待。data={"title":"需要审批","content":"{{llm_1.output}}"}。',
    "delay": '延时（秒，上限 300）。data={"seconds":2}。',
}

SYSTEM_PROMPT = """你是工作流编排助手。把用户的需求转成一个可执行的有向无环图（DAG）。

硬性规则：
1. 只输出 JSON，不要任何解释文字、不要 markdown 代码块。
2. 必须恰好一个 "start" 节点，至少一个 "end" 节点。
3. 边（edges）的 source/target 必须是已定义节点的 id。
4. 节点 id 用小写英文 + 下划线，如 start、query_llm、router_1。不要用中文。
5. **不要输出 position 坐标**，布局由系统自动生成。
6. 分支节点（condition/switch/router）的有向边要带 sourceHandle：
   condition → "true"/"false"；switch/router → 各自 case 的 handle 或 "default"。
7. 节点引用上游输出用 `{{节点id.output}}`，字段名必须来自节点规格。
8. 优先用最少节点实现需求；不要为了"丰富"加无用节点。
9. 需要外部数据但用户没给接口时，用 http 节点并留空 url，不要编造 URL。

输出结构：
{"name":"工作流名称","description":"一句话说明","nodes":[{"id":"start","type":"start","data":{...}}],"edges":[{"source":"start","target":"llm_1"}]}
"""


def _specs_text() -> str:
    return "\n".join(f"- {k}: {v}" for k, v in NODE_SPECS.items())


# ---------------------------------------------------------------- 校验与修复


def _strip_code_fence(text: str) -> str:
    s = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
    if m:
        s = m.group(1).strip()
    # 兜底：截取第一个 { 到最后一个 }
    if not s.startswith("{"):
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            s = s[i:j + 1]
    return s


def parse_graph_json(text: str) -> dict:
    """从模型输出里抽出图 JSON。失败抛 ValidationError（带原文片段便于排查）。"""
    raw = _strip_code_fence(text)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        raise ValidationError(f"AI 返回的不是合法 JSON：{e}。原文片段：{raw[:200]}")
    if not isinstance(data, dict):
        raise ValidationError("AI 返回的顶层结构应为 JSON 对象")
    return data


def _auto_layout(nodes: list[dict], edges: list[dict]) -> None:
    """按拓扑分层计算坐标：同层纵排，层间横向推进。

    LLM 不做布局是刻意的：让它算坐标必然重叠。这里用简单的最长路径分层，
    环会被 engine 的 validate_graph 拦下，所以这里不处理环。
    """
    ids = [n["id"] for n in nodes]
    indeg = {i: 0 for i in ids}
    succ: dict[str, list[str]] = {i: [] for i in ids}
    for e in edges:
        s, t = e.get("source"), e.get("target")
        if s in succ and t in indeg:
            succ[s].append(t)
            indeg[t] += 1

    # 分层：BFS，层号 = 最长路径
    depth = {i: 0 for i in ids}
    queue = [i for i in ids if indeg[i] == 0]
    # 用拓扑序推进（Kahn）
    from collections import deque

    q = deque(queue)
    order: list[str] = []
    tmp = dict(indeg)
    while q:
        cur = q.popleft()
        order.append(cur)
        for t in succ.get(cur, []):
            depth[t] = max(depth.get(t, 0), depth.get(cur, 0) + 1)
            tmp[t] -= 1
            if tmp[t] == 0:
                q.append(t)

    by_depth: dict[int, list[str]] = {}
    for nid in ids:
        by_depth.setdefault(depth.get(nid, 0), []).append(nid)

    X0, Y0, DX, DY = 80, 80, 260, 130
    pos: dict[str, dict] = {}
    for d, group in by_depth.items():
        for i, nid in enumerate(group):
            pos[nid] = {"x": X0 + d * DX, "y": Y0 + i * DY}
    for n in nodes:
        n["position"] = pos.get(n["id"], {"x": X0, "y": Y0})


def sanitize_graph(raw: dict) -> tuple[dict, list[str]]:
    """修复模型输出的常见问题，返回 (graph, warnings)。

    可修复：缺 start/end、未知节点类型、悬空边、重复 id、ids 非字符串。
    不可修复（抛错）：节点为空、图有环。
    """
    from app.agents.workflow.engine import NODE_EXECUTORS

    warnings: list[str] = []
    nodes_in = raw.get("nodes") or []
    if not isinstance(nodes_in, list) or not nodes_in:
        raise ValidationError("AI 生成的图没有节点")

    nodes: list[dict] = []
    seen: set[str] = set()
    # 旧 id → 新 id：模型常写出 "http 1" / "http.1" 这类非法 id，
    # 清洗后必须同步改写边，否则边会全部指向不存在的节点。
    id_remap: dict[str, str] = {}
    for i, n in enumerate(nodes_in):
        if not isinstance(n, dict):
            continue
        raw_id = str(n.get("id") or f"node_{i + 1}").strip()
        nid = re.sub(r"[^\w\-]", "_", raw_id) or f"node_{i + 1}"
        base, k = nid, 2
        while nid in seen:
            nid = f"{base}_{k}"
            k += 1
        seen.add(nid)
        id_remap[raw_id] = nid

        ntype = str(n.get("type") or "").strip()
        if ntype not in NODE_EXECUTORS:
            warnings.append(f"忽略未知节点类型 {ntype!r}（id={nid}）")
            continue
        data = n.get("data")
        nodes.append({
            "id": nid,
            "type": ntype,
            "data": data if isinstance(data, dict) else {},
            **( {"position": n["position"]} if isinstance(n.get("position"), dict) else {} ),
        })

    if not nodes:
        raise ValidationError("AI 生成的节点类型全部无法识别")

    # start / end 补齐
    if not any(n["type"] == "start" for n in nodes):
        nodes.insert(0, {"id": "start", "type": "start",
                         "data": {"inputs": [{"name": "query", "type": "string", "required": True}]}})
        warnings.append("补上了缺失的 start 节点")
    if not any(n["type"] == "end" for n in nodes):
        nodes.append({"id": "end", "type": "end", "data": {"output": ""}})
        warnings.append("补上了缺失的 end 节点")
    ids = {n["id"] for n in nodes}
    edges: list[dict] = []
    edge_seen: set[tuple] = set()
    for e in raw.get("edges") or []:
        if not isinstance(e, dict):
            continue
        s_raw = str(e.get("source") or "")
        t_raw = str(e.get("target") or "")
        s = id_remap.get(s_raw, re.sub(r"[^\w\-]", "_", s_raw))
        t = id_remap.get(t_raw, re.sub(r"[^\w\-]", "_", t_raw))
        if s not in ids or t not in ids:
            warnings.append(f"忽略悬空边 {s_raw} → {t_raw}")
            continue
        if s == t:
            warnings.append(f"忽略自环边 {s}")
            continue
        sh = e.get("sourceHandle")
        sh = str(sh) if sh not in (None, "") else None
        key = (s, t, sh)
        if key in edge_seen:
            continue
        edge_seen.add(key)
        edges.append({"id": f"e_{s}_{t}" + (f"_{sh}" if sh else ""),
                      "source": s, "target": t,
                      **({"sourceHandle": sh} if sh else {})})

    graph = {"nodes": nodes, "edges": edges, "name": str(raw.get("name") or "").strip(),
             "description": str(raw.get("description") or "").strip()}

    # 孤立 end 兜底：模型常忘记把最后一个节点连到 end，这会让图跑完后 end 不执行。
    # 自动接到「没有出边的叶子节点」上（多个叶子就都接，符合结束语义）。
    end_nodes = [n for n in nodes if n["type"] == "end"]
    for en in end_nodes:
        has_in = any(e["target"] == en["id"] for e in edges)
        if has_in:
            continue
        sources = {e["source"] for e in edges}
        leaves = [
            n["id"] for n in nodes
            if n["type"] not in ("end",) and n["id"] not in sources
        ]
        for leaf in leaves:
            edges.append({
                "id": f"e_{leaf}_{en['id']}",
                "source": leaf,
                "target": en["id"],
            })
        if leaves:
            warnings.append(f"自动把 end 接到叶子节点 {', '.join(leaves)}")

    # 统一布局（覆盖模型可能给出的坐标，保证不重叠）
    _auto_layout(graph["nodes"], edges)

    # 最终校验（环、start 唯一等）
    from app.agents.workflow.engine import validate_graph

    try:
        validate_graph({**graph, "nodes": graph["nodes"]})
    except ValidationError as e:
        raise ValidationError(f"AI 生成的图结构无效：{e}")

    return graph, warnings


# ---------------------------------------------------------------- 主入口


async def generate_graph_from_text(
    db, *, tenant_id: int, prompt: str, model_config_id: int | None = None,
) -> dict:
    """自然语言 → graph。返回 {graph, warnings, name, description}。"""
    from app.providers.base import ChatMessage
    from app.providers.registry import get_llm

    text = (prompt or "").strip()
    if not text:
        raise ValidationError("请描述你想要的工作流")

    llm, rm = await get_llm(db, tenant_id=tenant_id, config_id=model_config_id)
    msgs = [
        ChatMessage(role="system", content=SYSTEM_PROMPT + "\n\n可用节点规格：\n" + _specs_text()),
        ChatMessage(role="user", content=f"需求：{text}\n\n请输出 JSON。"),
    ]
    res = await llm.chat(msgs, model=rm.model_name, temperature=0.2)
    raw = parse_graph_json(res.content)
    graph, warnings = sanitize_graph(raw)

    if warnings:
        logger.info("workflow_ai_sanitized", count=len(warnings), warnings=warnings[:10])

    return {
        "graph": graph,
        "warnings": warnings,
        "name": graph.get("name") or "AI 工作流",
        "description": graph.get("description") or text[:200],
        "node_count": len(graph["nodes"]),
    }
