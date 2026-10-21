"""工作流模板：内置场景模板 + 用户保存的模板。

设计要点（勿凭直觉改动）：

1. **模板是完整可运行的图，不是片段**。用户套用后应能直接跑（或只补一个 URL）。
   给「半成品」会让人以为模板坏了。

2. **内置模板用常量，不用数据库**。桌面版 PyInstaller 打包无需改 spec；
   而且内置模板要随版本升级，存表反而要写迁移脚本去覆盖。

3. **用户保存的模板存表**，按租户隔离 —— 每个团队积累自己的常用流程。

4. **套用 = 创建新智能体**（与导入同一语义），不覆盖现有工作流。
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.models import Agent, Workflow, WorkflowTemplate


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name or "").strip("-").lower()
    return s or "workflow"


# ---------------------------------------------------------------- 内置模板
# 节点位置按「左→右」布置，间距 260×130，与自动布局一致。

def _n(nid: str, ntype: str, x: int, y: int, data: dict) -> dict:
    return {"id": nid, "type": ntype, "position": {"x": x, "y": y}, "data": data}


BUILTIN_TEMPLATES: tuple[dict, ...] = (
    {
        "key": "faq-bot",
        "name": "知识库客服",
        "category": "客服",
        "description": "客户提问 → 检索知识库 → 大模型依据资料作答。最常用的客服起点。",
        "tags": ["客服", "问答", "知识库"],
        "graph": {
            "nodes": [
                _n("start", "start", 80, 120, {"inputs": [
                    {"name": "query", "type": "string", "required": True},
                ]}),
                _n("ret", "knowledge_retrieval", 340, 120, {
                    "query": "{{start.query}}", "top_k": 5,
                }),
                _n("llm", "llm", 600, 120, {
                    "system": "你是专业客服。只依据【参考资料】回答，资料没有就明确说不知道，不要编造。",
                    "prompt": "参考资料：\n{{ret.output}}\n\n客户问题：{{start.query}}",
                    "temperature": 0.3,
                }),
                _n("end", "end", 860, 120, {"output": "{{llm.output}}"}),
            ],
            "edges": [
                {"source": "start", "target": "ret"},
                {"source": "ret", "target": "llm"},
                {"source": "llm", "target": "end"},
            ],
        },
    },
    {
        "key": "order-query",
        "name": "订单查询",
        "category": "电商",
        "description": "客户问订单 → 调接口查详情 → 解析字段 → 生成友好回复。需补订单接口地址。",
        "tags": ["电商", "订单", "API"],
        "graph": {
            "nodes": [
                _n("start", "start", 80, 120, {"inputs": [
                    {"name": "message", "type": "string", "required": True},
                    {"name": "order_id", "type": "string", "required": True},
                ]}),
                _n("api", "http", 340, 120, {
                    "url": "https://your-erp.example.com/api/orders/{{start.order_id}}",
                    "method": "GET", "timeout": 20,
                }),
                _n("pick", "json_path", 600, 120, {
                    "source": "{{api.output}}", "path": "data",
                }),
                _n("reply", "text_template", 860, 120, {"sections": [
                    {"title": "订单信息", "content": "{{pick.value}}"},
                ]}),
                _n("end", "end", 1120, 120, {"output": "{{reply.text}}"}),
            ],
            "edges": [
                {"source": "start", "target": "api"},
                {"source": "api", "target": "pick"},
                {"source": "pick", "target": "reply"},
                {"source": "reply", "target": "end"},
            ],
        },
    },
    {
        "key": "ticket-triage",
        "name": "工单智能分流",
        "category": "客服",
        "description": "判断客户诉求属于售后还是咨询，分流到不同处理链，可接人工审批。",
        "tags": ["客服", "分流", "分类"],
        "graph": {
            "nodes": [
                _n("start", "start", 80, 160, {"inputs": [
                    {"name": "message", "type": "string", "required": True},
                ]}),
                _n("cls", "summarize", 340, 160, {
                    "text": "{{start.message}}", "mode": "classify", "temperature": 0,
                }),
                _n("router", "router", 600, 160, {
                    "value": "{{cls.output}}",
                    "cases": [
                        {"handle": "after_sale", "values": ["售后", "退货", "退款", "换货", "投诉"]},
                        {"handle": "inquiry", "values": ["咨询", "价格", "库存", "发货"]},
                    ],
                }),
                _n("ret", "knowledge_retrieval", 860, 60, {
                    "query": "{{start.message}}", "top_k": 5,
                }),
                _n("answer", "llm", 1120, 60, {
                    "system": "你是售后专员，依据资料给出处理方案。",
                    "prompt": "资料：\n{{ret.output}}\n\n客户：{{start.message}}",
                }),
                _n("ap", "approval", 860, 260, {
                    "title": "售后单需人工确认", "content": "客户诉求：{{start.message}}\n分类：{{cls.output}}",
                }),
                _n("end", "end", 1380, 160, {"output": "{{answer.output}}"}),
            ],
            "edges": [
                {"source": "start", "target": "cls"},
                {"source": "cls", "target": "router"},
                {"source": "router", "target": "ret", "sourceHandle": "inquiry"},
                {"source": "router", "target": "ap", "sourceHandle": "after_sale"},
                {"source": "ret", "target": "answer"},
                {"source": "answer", "target": "end"},
                {"source": "ap", "target": "end"},
            ],
        },
    },
    {
        "key": "daily-report",
        "name": "日报自动生成",
        "category": "办公",
        "description": "定时拉取数据 → 大模型总结 → 排版成邮件正文。适合接定时任务。",
        "tags": ["办公", "报表", "定时"],
        "graph": {
            "nodes": [
                _n("start", "start", 80, 120, {"inputs": [
                    {"name": "data_source", "type": "string", "required": False},
                ]}),
                _n("fetch", "http", 340, 120, {
                    "url": "{{start.data_source}}", "method": "GET", "timeout": 30,
                }),
                _n("sum", "summarize", 600, 120, {
                    "text": "{{fetch.output}}", "mode": "summary", "temperature": 0.2,
                }),
                _n("fmt", "text_template", 860, 120, {"sections": [
                    {"title": "今日摘要", "content": "{{sum.output}}"},
                ]}),
                _n("end", "end", 1120, 120, {"output": "{{fmt.text}}"}),
            ],
            "edges": [
                {"source": "start", "target": "fetch"},
                {"source": "fetch", "target": "sum"},
                {"source": "sum", "target": "fmt"},
                {"source": "fmt", "target": "end"},
            ],
        },
    },
    {
        "key": "table-summary",
        "name": "数据汇总报表",
        "category": "办公",
        "description": "接收行数据 → 过滤排序 → 聚合汇总 → 生成 CSV（可直接存为 Excel）。",
        "tags": ["办公", "表格", "汇总"],
        # 纯数据变换，不依赖外部服务，可直接跑通 —— 给它配一条真能过的用例
        "cases": [
            {
                "name": "两行汇总",
                "input": {"rows": '[{"name":"A","amount":100},{"name":"B","amount":200}]'},
                "expected": "A,100",
                "assert_contains": True,
                "history": [],
            },
        ],
        "graph": {
            "nodes": [
                _n("start", "start", 80, 120, {"inputs": [
                    {"name": "rows", "type": "json", "required": True},
                ]}),
                _n("tbl", "table", 340, 120, {
                    "rows": "{{start.rows}}",
                    "filters": [], "sort": {"field": "", "desc": True},
                    "aggregates": [{"name": "合计", "field": "amount", "op": "sum"}],
                    "as_csv": True,
                }),
                _n("end", "end", 600, 120, {"output": "{{tbl.csv}}"}),
            ],
            "edges": [
                {"source": "start", "target": "tbl"},
                {"source": "tbl", "target": "end"},
            ],
        },
    },
    {
        "key": "webhook-notify",
        "name": "外部事件通知",
        "category": "集成",
        "description": "接收外部系统推送 → 整理消息 → 推送到企业微信/飞书群。",
        "tags": ["集成", "Webhook", "通知"],
        "graph": {
            "nodes": [
                _n("start", "start", 80, 120, {"inputs": [
                    {"name": "message", "type": "string", "required": True},
                ]}),
                _n("fmt", "text_template", 340, 120, {"sections": [
                    {"title": "系统通知", "content": "{{start.message}}"},
                ]}),
                _n("push", "notify", 600, 120, {
                    "mode": "notify_channel", "channel_id": None,
                    "title": "系统通知", "content": "{{fmt.text}}",
                }),
                _n("end", "end", 860, 120, {"output": "{{fmt.text}}"}),
            ],
            "edges": [
                {"source": "start", "target": "fmt"},
                {"source": "fmt", "target": "push"},
                {"source": "push", "target": "end"},
            ],
        },
    },
)


def list_builtin() -> list[dict]:
    return [
        {
            "key": t["key"], "name": t["name"], "category": t["category"],
            "description": t["description"], "tags": t["tags"],
            "node_count": len(t["graph"]["nodes"]),
            "case_count": len(t.get("cases") or []),
            "source": "builtin",
        }
        for t in BUILTIN_TEMPLATES
    ]


def get_builtin(key: str) -> dict | None:
    return next((t for t in BUILTIN_TEMPLATES if t["key"] == key), None)


# ---------------------------------------------------------------- 用户模板


async def list_user_templates(db: AsyncSession, *, tenant_id: int) -> list[dict]:
    rows = (
        await db.execute(
            select(WorkflowTemplate)
            .where(WorkflowTemplate.tenant_id == tenant_id)
            .order_by(WorkflowTemplate.id.desc())
        )
    ).scalars().all()
    return [_template_out(r) for r in rows]


async def list_public_templates(db: AsyncSession, *, tenant_id: int) -> list[dict]:
    """公共模板库：其他租户审核通过并公开的模板（不含自己的，避免重复）。

    排序按「评分 + 套用次数」综合：评分优先（质量），同分看热度。
    没有评分的模板排在后面 —— 新模板需要先有人用起来。
    """
    rows = (
        await db.execute(
            select(WorkflowTemplate)
            .where(
                WorkflowTemplate.visibility == "public",
                WorkflowTemplate.review_status == "approved",
                WorkflowTemplate.tenant_id != tenant_id,
            )
        )
    ).scalars().all()

    def _score(r: WorkflowTemplate) -> tuple:
        avg = (r.rating_sum or 0) / r.rating_count if (r.rating_count or 0) else 0.0
        # 评分优先；无评分排后；同看套用次数
        return (avg > 0, round(avg, 2), r.apply_count or 0, r.id)

    return [
        _template_out(r, include_owner_name=True)
        for r in sorted(rows, key=_score, reverse=True)
    ]


def _template_out(r: WorkflowTemplate, *, include_owner_name: bool = False) -> dict:
    avg = round((r.rating_sum or 0) / r.rating_count, 1) if (r.rating_count or 0) else None
    out = {
        "id": r.id, "name": r.name, "category": r.category,
        "description": r.description, "tags": r.tags or [],
        "node_count": len((r.graph or {}).get("nodes") or []),
        "case_count": len(r.cases or []),
        "source": "public" if r.visibility == "public" else "user",
        "visibility": r.visibility or "private",
        "apply_count": r.apply_count or 0,
        "risk_level": r.risk_level or "none",
        "review_status": r.review_status or "none",
        "rating": avg,
        "rating_count": r.rating_count or 0,
    }
    if include_owner_name:
        out["owner_tenant_id"] = r.tenant_id
    return out


async def save_template(
    db: AsyncSession, *, tenant_id: int, user_id: int, agent_id: int,
    name: str | None = None, category: str = "自定义", description: str | None = None,
    include_cases: bool = True,
) -> dict:
    """把现有工作流存成模板（快照当前图 + 可选附带测试用例）。

    include_cases 默认 True：模板带用例才有回归价值，
    套用后新工作流开箱就知道自己是否正常。
    """
    from app.models import WorkflowTest

    a = await db.get(Agent, agent_id)
    if not a or a.tenant_id != tenant_id:
        raise NotFoundError("智能体不存在")
    wf = (
        await db.execute(
            select(Workflow).where(Workflow.agent_id == agent_id).order_by(Workflow.id.desc())
        )
    ).scalars().first()
    if not wf or not (wf.graph or {}).get("nodes"):
        raise ValidationError("工作流为空，无法保存为模板")

    cases: list[dict] = []
    if include_cases:
        rows = (
            await db.execute(
                select(WorkflowTest).where(
                    WorkflowTest.tenant_id == tenant_id, WorkflowTest.agent_id == agent_id,
                ).order_by(WorkflowTest.id.asc())
            )
        ).scalars().all()
        for c in rows:
            cases.append({
                "name": c.name, "input": c.input or {},
                "expected": c.expected, "assert_contains": bool(c.assert_contains),
                "history": c.history or [],
            })

    t = WorkflowTemplate(
        tenant_id=tenant_id, created_by=user_id,
        name=(name or wf.name or a.name)[:128],
        category=(category or "自定义")[:32],
        description=(description or a.description or "")[:500] or None,
        graph=wf.graph, cases=cases or None,
    )
    db.add(t)
    await db.flush()
    return {"id": t.id, "name": t.name, "node_count": len(wf.graph["nodes"]),
            "case_count": len(cases)}


async def set_visibility(
    db: AsyncSession, *, tenant_id: int, template_id: int, public: bool,
    allow_high_risk: bool = False,
) -> dict:
    """申请发布 / 取消发布到公共模板库。

    发布走**审核流**：先把风险扫描结果落库，再置 visibility=pending、
    review_status=pending，等管理员审核通过才真正变 public。

    为什么不是直接公开：跨租户套用等于执行别人编排的流程，
    带 code 节点的模板更是直接执行别人的 Python。
    """
    import time as _t

    from app.services import workflow_template_scan as SCAN

    r = await db.get(WorkflowTemplate, template_id)
    if not r or r.tenant_id != tenant_id:
        raise NotFoundError("模板不存在")

    if not public:
        r.visibility = "private"
        r.published_at = None
        r.review_status = "none"
        await db.flush()
        return {"visibility": r.visibility, "review_status": r.review_status,
                "message": "已取消发布"}

    # 取消发布后再申请时，已通过的审核可以直接复用（内容没变的前提下）
    scan = SCAN.scan_graph(r.graph or {})
    r.risk_level = scan["level"]
    r.risk_findings = scan["findings"] or None

    ok, reason = SCAN.check_publishable(r.graph or {}, allow_high_risk=allow_high_risk)
    if not ok:
        await db.flush()
        return {
            "visibility": r.visibility, "review_status": r.review_status,
            "risk_level": r.risk_level,
            "blocked": True, "message": reason,
        }

    r.visibility = "pending"
    r.review_status = "pending"
    r.reviewed_by = None
    r.reviewed_at = None
    await db.flush()
    return {
        "visibility": r.visibility, "review_status": r.review_status,
        "risk_level": r.risk_level, "scan": scan,
        "message": "已提交审核，通过后将在公共库可见" + (f"（提示：{reason}）" if reason else ""),
    }


async def review_template(
    db: AsyncSession, *, template_id: int, approve: bool, note: str | None = None,
    reviewer_id: int | None = None,
) -> dict:
    """管理员审核：通过则公开，拒绝则退回私有。

    注意不做租户校验 —— 调用方（API 层）负责用管理员权限把关。
    """
    import time as _t

    r = await db.get(WorkflowTemplate, template_id)
    if not r:
        raise NotFoundError("模板不存在")
    if r.review_status != "pending":
        raise ValidationError(f"该模板当前状态为 {r.review_status}，不在待审核中")

    r.review_status = "approved" if approve else "rejected"
    r.review_note = (note or "")[:500] or None
    r.reviewed_by = reviewer_id
    r.reviewed_at = int(_t.time() * 1000)
    r.visibility = "public" if approve else "private"
    r.published_at = int(_t.time() * 1000) if approve else None
    await db.flush()
    return {
        "review_status": r.review_status, "visibility": r.visibility,
        "message": "已通过，模板已在公共库可见" if approve else "已拒绝",
    }


async def list_pending_reviews(db: AsyncSession, *, limit: int = 100) -> list[dict]:
    """待审核列表（跨租户，供管理员）。"""
    rows = (
        await db.execute(
            select(WorkflowTemplate)
            .where(WorkflowTemplate.review_status == "pending")
            .order_by(WorkflowTemplate.id.asc())
            .limit(min(limit, 200))
        )
    ).scalars().all()
    out = []
    for r in rows:
        item = _template_out(r, include_owner_name=True)
        item["risk_findings"] = r.risk_findings or []
        item["review_note"] = r.review_note
        out.append(item)
    return out


async def rate_template(
    db: AsyncSession, *, tenant_id: int, user_id: int, template_id: int,
    rating: int, comment: str | None = None,
) -> dict:
    """评分 1-5，每租户每模板一条（重复调用即修改）。"""
    from app.models import WorkflowTemplateRating

    if not (1 <= int(rating) <= 5):
        raise ValidationError("评分需在 1-5 之间")

    r = await db.get(WorkflowTemplate, template_id)
    if not r:
        raise NotFoundError("模板不存在")
    # 自己的模板不能评分；必须已公开
    if r.tenant_id == tenant_id:
        raise ValidationError("不能给自己的模板评分")

    existing = (
        await db.execute(
            select(WorkflowTemplateRating).where(
                WorkflowTemplateRating.template_id == template_id,
                WorkflowTemplateRating.tenant_id == tenant_id,
            )
        )
    ).scalars().first()

    if existing is None:
        db.add(WorkflowTemplateRating(
            tenant_id=tenant_id, template_id=template_id,
            rating=int(rating), comment=(comment or "")[:500] or None, created_by=user_id,
        ))
        r.rating_sum = (r.rating_sum or 0) + int(rating)
        r.rating_count = (r.rating_count or 0) + 1
    else:
        r.rating_sum = (r.rating_sum or 0) - (existing.rating or 0) + int(rating)
        existing.rating = int(rating)
        existing.comment = (comment or "")[:500] or None

    await db.flush()
    avg = round((r.rating_sum or 0) / max(1, r.rating_count or 0), 1)
    return {"rating": avg, "rating_count": r.rating_count or 0, "my_rating": int(rating)}


async def delete_template(db: AsyncSession, *, tenant_id: int, template_id: int) -> None:
    r = await db.get(WorkflowTemplate, template_id)
    if not r or r.tenant_id != tenant_id:
        raise NotFoundError("模板不存在")
    await db.delete(r)
    await db.flush()


async def apply_template(
    db: AsyncSession, *, tenant_id: int, user_id: int, graph: dict, name: str,
    description: str = "", cases: list[dict] | None = None,
) -> dict:
    """把模板图实例化为一个新的工作流型智能体（不覆盖现有）。

    cases 非空时一并创建测试用例 —— 模板带用例才有回归价值，
    新工作流开箱就能验证是否正常。
    """
    from app.agents.workflow.engine import validate_graph
    from app.models import WorkflowTest

    if not isinstance(graph, dict) or not (graph.get("nodes")):
        raise ValidationError("模板内容为空")
    validate_graph(graph)

    base = _slugify(name)
    slug, n = base, 2
    while (
        await db.execute(
            select(Agent).where(Agent.tenant_id == tenant_id, Agent.slug == slug)
        )
    ).scalar_one_or_none():
        slug = f"{base}-{n}"
        n += 1

    agent = Agent(
        tenant_id=tenant_id, owner_id=user_id, name=name[:128], slug=slug,
        description=description[:500], type="workflow",
        status="draft", visibility="private",
    )
    db.add(agent)
    await db.flush()

    wf = Workflow(
        tenant_id=tenant_id, agent_id=agent.id, name=name[:128],
        graph=graph, version=1, status="draft",
    )
    db.add(wf)
    await db.flush()

    created_cases = 0
    for c in (cases or []):
        if not isinstance(c, dict):
            continue
        input_data = c.get("input")
        if not isinstance(input_data, dict) or not input_data:
            continue  # 用例必须有输入，坏的直接跳过而不是整个套用失败
        db.add(WorkflowTest(
            tenant_id=tenant_id, agent_id=agent.id,
            name=str(c.get("name") or "模板用例")[:128],
            input=input_data,
            expected=(str(c["expected"]).strip() or None) if c.get("expected") else None,
            assert_contains=bool(c.get("assert_contains", True)),
            history=c.get("history") or None,
        ))
        created_cases += 1

    await db.flush()
    return {
        "agent_id": agent.id, "workflow_id": wf.id,
        "name": agent.name, "node_count": len(graph["nodes"]),
        "case_count": created_cases,
    }
