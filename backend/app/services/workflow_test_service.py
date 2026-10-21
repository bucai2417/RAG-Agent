"""工作流测试用例：把调试对话固化下来，改完工作流一键回归。

设计要点（勿凭直觉改动）：

1. **断言是"包含"而不是"相等"**。LLM 输出天然不确定，精确相等会让用例永远红。
   默认用 expected 作为子串匹配；用户可关掉断言只做「能跑通」的冒烟检查。

2. **期望值是可选的**。只存输入 + 期望包含片段，不存完整输出快照 ——
   存快照会在每次改提示词后大面积失效，维护成本高于收益。

3. **用例挂在 agent 上而不是 graph 上**。工作流图会随编辑变化，
   但用例描述的是「这个助手应该怎么表现」，理应跨图版本存活。
"""
from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.models import Agent, Workflow, WorkflowTest


def _normalize_history(history: list[dict] | None) -> list[dict] | None:
    """规范化多轮历史：只保留 user/assistant 且内容非空的条目。

    传入空列表视为「单轮用例」，存 None 让语义更清晰。
    """
    if not history:
        return None
    out: list[dict] = []
    for h in history:
        if not isinstance(h, dict):
            continue
        role = str(h.get("role") or "").strip()
        content = str(h.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        out.append({"role": role, "content": content[:2000]})
    return out or None


async def list_cases(db: AsyncSession, *, tenant_id: int, agent_id: int) -> list[dict]:
    rows = (
        await db.execute(
            select(WorkflowTest).where(
                WorkflowTest.tenant_id == tenant_id, WorkflowTest.agent_id == agent_id
            ).order_by(WorkflowTest.id.asc())
        )
    ).scalars().all()
    return [_to_out(r) for r in rows]


def _to_out(r: WorkflowTest) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "input": r.input or {},
        "expected": r.expected,
        "assert_contains": bool(r.assert_contains),
        "history": r.history or [],
        "last_status": r.last_status,
        "last_latency_ms": r.last_latency_ms,
        "last_actual": r.last_actual,
        "last_run_at": r.last_run_at,
    }


async def create_case(
    db: AsyncSession, *, tenant_id: int, agent_id: int, name: str,
    input_data: dict, expected: str | None = None, assert_contains: bool = True,
    history: list[dict] | None = None,
) -> dict:
    a = await db.get(Agent, agent_id)
    if not a or a.tenant_id != tenant_id:
        raise NotFoundError("智能体不存在")
    if not (name or "").strip():
        raise ValidationError("用例名称不能为空")
    if not isinstance(input_data, dict) or not input_data:
        raise ValidationError("用例输入不能为空")

    r = WorkflowTest(
        tenant_id=tenant_id, agent_id=agent_id,
        name=name.strip()[:128], input=input_data,
        expected=(expected or "").strip() or None,
        assert_contains=assert_contains,
        history=_normalize_history(history),
    )
    db.add(r)
    await db.flush()
    return _to_out(r)


async def update_case(db: AsyncSession, *, tenant_id: int, case_id: int, **fields) -> dict:
    r = await db.get(WorkflowTest, case_id)
    if not r or r.tenant_id != tenant_id:
        raise NotFoundError("用例不存在")
    if "name" in fields and fields["name"] is not None:
        r.name = str(fields["name"]).strip()[:128] or r.name
    if "input" in fields and fields["input"] is not None:
        if not isinstance(fields["input"], dict):
            raise ValidationError("输入必须是对象")
        r.input = fields["input"]
    if "expected" in fields:
        r.expected = (str(fields["expected"]).strip() or None) if fields["expected"] is not None else None
    if "assert_contains" in fields and fields["assert_contains"] is not None:
        r.assert_contains = bool(fields["assert_contains"])
    if "history" in fields:
        r.history = _normalize_history(fields["history"])
    await db.flush()
    return _to_out(r)


async def delete_case(db: AsyncSession, *, tenant_id: int, case_id: int) -> None:
    r = await db.get(WorkflowTest, case_id)
    if not r or r.tenant_id != tenant_id:
        raise NotFoundError("用例不存在")
    await db.delete(r)
    await db.flush()


async def _run_one(db: AsyncSession, *, case: WorkflowTest, agent: Agent, wf: Workflow, ps, perms) -> dict:
    """执行单个用例，返回 {status, actual, latency_ms, error}。"""
    from app.agents.workflow.engine import NodeContext, WorkflowPaused, execute_graph
    from app.agents.workflow.runner import start_input_name

    inputs = dict(case.input or {})
    primary = inputs.get(start_input_name(wf.graph)) or inputs.get("message") or inputs.get("query") or ""
    inputs[start_input_name(wf.graph)] = primary

    # 多轮用例：把历史拼成 history 变量（与渠道场景同格式），供指代消解
    hist_lines: list[str] = []
    for h in (case.history or [])[-10:]:
        if not isinstance(h, dict):
            continue
        role = "客户" if h.get("role") == "user" else "客服"
        content = str(h.get("content") or "").strip()
        if content:
            hist_lines.append(f"{role}：{content[:300]}")
    if hist_lines:
        inputs["history"] = "\n".join(hist_lines)

    ctx = NodeContext(
        db=db, ps=ps, run_id=0, tenant_id=case.tenant_id, inputs=inputs,
        default_model_config_id=agent.model_config_id, default_kb_ids=agent.kb_ids,
        default_system=agent.system_prompt,
        default_temperature=(agent.config or {}).get("temperature"), perms=perms,
    )

    t0 = time.time()
    try:
        final, _ = await execute_graph(wf.graph, inputs, ctx)
        actual = str(final.get("output", final)) if isinstance(final, dict) else str(final)
        latency = int((time.time() - t0) * 1000)

        if case.assert_contains and case.expected:
            ok = case.expected in actual
            return {
                "status": "passed" if ok else "failed",
                "actual": actual[:2000], "latency_ms": latency,
                "error": None if ok else f"期望包含「{case.expected}」，实际输出不含该内容",
            }
        # 未设期望或关闭断言：能跑完就算通过（冒烟）
        return {"status": "passed", "actual": actual[:2000], "latency_ms": latency, "error": None}
    except WorkflowPaused as p:
        return {
            "status": "waiting", "actual": "",
            "latency_ms": int((time.time() - t0) * 1000),
            "error": f"停在审批节点 {p.node_id}（用例不支持交互式审批）",
        }
    except Exception as e:  # noqa: BLE001
        return {
            "status": "error", "actual": "",
            "latency_ms": int((time.time() - t0) * 1000),
            "error": str(e)[:500],
        }


async def run_cases(
    db: AsyncSession, *, tenant_id: int, agent_id: int, case_ids: list[int] | None = None,
) -> dict:
    """批量跑用例（默认全部）。结果回写到用例记录，便于列表页展示。"""
    from app.middleware.auth_dep import get_user_permission_codes, load_principal_set
    from app.models import WorkflowRun  # noqa: F401

    a = await db.get(Agent, agent_id)
    if not a or a.tenant_id != tenant_id:
        raise NotFoundError("智能体不存在")

    wf = (
        await db.execute(
            select(Workflow).where(Workflow.agent_id == agent_id).order_by(Workflow.id.desc())
        )
    ).scalars().first()
    if not wf or not wf.graph or not (wf.graph.get("nodes")):
        raise ValidationError("工作流未配置，无法执行用例")

    stmt = select(WorkflowTest).where(
        WorkflowTest.tenant_id == tenant_id, WorkflowTest.agent_id == agent_id
    )
    if case_ids:
        stmt = stmt.where(WorkflowTest.id.in_(case_ids))
    cases = (await db.execute(stmt.order_by(WorkflowTest.id.asc()))).scalars().all()
    if not cases:
        return {"total": 0, "passed": 0, "failed": 0, "results": []}

    # 以调用者身份执行（用例不放大权限）
    from app.models import User

    owner_id = a.owner_id
    owner = await db.get(User, owner_id) if owner_id else None
    if owner is not None:
        ps = await load_principal_set(db, owner)
        perms = await get_user_permission_codes(db, owner)
    else:
        from app.services.permission import PrincipalSet

        ps = PrincipalSet(user_id=0, tenant_id=tenant_id)
        perms = set()

    results: list[dict] = []
    passed = failed = 0
    for case in cases:
        r = await _run_one(db, case=case, agent=a, wf=wf, ps=ps, perms=perms)
        case.last_status = r["status"]
        case.last_actual = r["actual"]
        case.last_latency_ms = r["latency_ms"]
        case.last_run_at = int(time.time() * 1000)
        if r["status"] == "passed":
            passed += 1
        else:
            failed += 1
        results.append({
            "id": case.id, "name": case.name, "status": r["status"],
            "expected": case.expected, "actual": r["actual"],
            "latency_ms": r["latency_ms"], "error": r["error"],
        })

    await db.flush()
    return {
        "total": len(cases), "passed": passed, "failed": failed,
        "results": results,
    }
