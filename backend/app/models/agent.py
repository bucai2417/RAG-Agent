"""智能体（Agent）平台数据模型。

一张 Agent 表通过 type 区分「工具循环 Agent」与「工作流 Agent」。
Skill 表达两种用途（kind 区分）；Tool 存自定义工具；AgentMode 存行为模式。
Workflow/WorkflowRun/NodeRun 存工作流图与运行轨迹。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import ROW_VERSION_COL, IdMixin, RowVersionMixin, TenantMixin, TimestampMixin


class Agent(Base, IdMixin, TimestampMixin, TenantMixin, RowVersionMixin):
    __tablename__ = "agent"
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_agent_tenant_slug"),)

    owner_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    icon: Mapped[str | None] = mapped_column(String(256), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[str] = mapped_column(String(16), default="agent")  # agent | workflow
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    model_config_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    kb_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 默认检索范围
    skill_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 引用的技能
    tool_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # max_turns/temperature 等
    default_mode_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft|published|disabled
    visibility: Mapped[str] = mapped_column(String(16), default="private")  # private|tenant
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 平台内置智能体：随系统播种、列表置顶、禁止删除（对话模型仍由界面选择，不预置）
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    __mapper_args__ = {"version_id_col": ROW_VERSION_COL}


class AgentMode(Base, IdMixin, TimestampMixin, TenantMixin):
    """行为模式：同一智能体的多套行为（提示词 + 工具 + 参数）。"""

    __tablename__ = "agent_mode"

    agent_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_prompt: Mapped[str] = mapped_column(Text, default="")  # 叠加在 agent 基座之后
    skill_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 非空则覆盖
    tool_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 深合并覆盖
    kb_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class Skill(Base, IdMixin, TimestampMixin, TenantMixin):
    """技能 = 能力包(prompt_pack) 或 工具(tool)。"""

    __tablename__ = "skill"
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_skill_tenant_slug"),)

    owner_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    icon: Mapped[str | None] = mapped_column(String(256), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="prompt_pack")  # prompt_pack | tool | collection

    # 能力包用
    prompt_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    params_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tool_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 带出的工具
    kb_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    model_config_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # 工具用
    tool_def: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # 技能包来源
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual | package
    body_md: Mapped[str | None] = mapped_column(Text, nullable=True)  # 技能包 SKILL.md 正文
    package_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # 技能集合（多技能仓库）：父技能 kind=collection，子技能 parent_id 指向父
    parent_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    collection_subpath: Mapped[str | None] = mapped_column(String(256), nullable=True)  # 子技能在仓库内的目录

    status: Mapped[str] = mapped_column(String(16), default="active")
    visibility: Mapped[str] = mapped_column(String(16), default="private")


class SkillPackage(Base, IdMixin, TimestampMixin, TenantMixin):
    """技能包：从本地 zip / URL / GitHub 导入的技能包。"""

    __tablename__ = "skill_package"

    skill_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    source_type: Mapped[str] = mapped_column(String(16), default="zip")  # zip|url|github
    source_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pack_dir: Mapped[str | None] = mapped_column(String(512), nullable=True)  # 解压目录(file_key)
    manifest: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # SKILL.md frontmatter
    files: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{path,size}]
    entry_scripts: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 可用脚本
    scripts_enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # 脚本是否允许执行
    import_status: Mapped[str] = mapped_column(String(16), default="ok")  # ok|failed
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class Tool(Base, IdMixin, TimestampMixin, TenantMixin):
    """自定义工具持久化（内置工具由代码注册，不入库）。"""

    __tablename__ = "tool"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_tool_tenant_name"),)

    owner_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    description: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="http")  # http | builtin
    parameters: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="active")


class Workflow(Base, IdMixin, TimestampMixin, TenantMixin, RowVersionMixin):
    """工作流图（属于 workflow 型 Agent）。"""

    __tablename__ = "workflow"

    agent_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), default="")
    graph: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {nodes:[],edges:[]}
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __mapper_args__ = {"version_id_col": ROW_VERSION_COL}


class WorkflowRun(Base, IdMixin, TenantMixin):
    __tablename__ = "workflow_run"

    workflow_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    agent_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|success|failed|canceled|waiting
    input: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    graph_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    state: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 审批暂停时的 {variables}
    pending_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 等待审批的节点
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[int] = mapped_column(BigInteger, default=0)
    finished_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # ---- 来源渠道快照（仅渠道发起的运行有值）----
    # 异步审批通过后要回推客户，此时入站消息早已处理完毕，无法再反查身份。
    # 所以在创建运行时就记下「发给谁、发到哪」，回推时直接用。
    origin_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    origin_channel_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    origin_external_user: Mapped[str | None] = mapped_column(String(128), nullable=True)
    origin_external_group: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # ---- Token 用量（多租户成本核算）----
    # 结构：{prompt_tokens, completion_tokens, calls, models: {模型名: {prompt, completion, calls}}}
    usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class NodeRun(Base, IdMixin, TenantMixin):
    __tablename__ = "node_run"

    run_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    node_type: Mapped[str] = mapped_column(String(32), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|running|success|failed|skipped|waiting
    input: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[int] = mapped_column(BigInteger, default=0)
    finished_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class WorkflowTemplate(Base, IdMixin, TimestampMixin, TenantMixin):
    """用户保存的工作流模板（内置模板在代码常量里，不存表）。

    存表是为了让每个团队积累自己的常用流程；内置模板随版本升级，
    存表反而要写迁移去覆盖，所以留在代码里。
    """

    __tablename__ = "workflow_template"

    name: Mapped[str] = mapped_column(String(128), default="")
    category: Mapped[str] = mapped_column(String(32), default="自定义")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    graph: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 随模板一起走的测试用例：[{name, input, expected, assert_contains, history}]
    # 套用时一并创建，让新工作流开箱就有回归基准。
    cases: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # ---- 跨租户共享 ----
    # visibility：private（私有）/ pending（已申请公开，待审核）/ public（公开）
    visibility: Mapped[str] = mapped_column(String(16), default="private")
    published_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 套用计数（展示热门模板，也用于判断共享价值）
    apply_count: Mapped[int] = mapped_column(Integer, default=0)

    # ---- 审核 ----
    # 审核状态：none（未申请）/ pending / approved / rejected
    review_status: Mapped[str] = mapped_column(String(16), default="none")
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reviewed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 发布时自动扫描的风险等级：none/low/medium/high（供审核员参考）
    risk_level: Mapped[str] = mapped_column(String(8), default="none")
    # 完整扫描明细：[{node_id, node_type, level, reason, hint}]
    risk_findings: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # ---- 评分 ----
    # 汇总冗余（避免每次列表都聚合）：rating_sum / rating_count
    rating_sum: Mapped[int] = mapped_column(Integer, default=0)
    rating_count: Mapped[int] = mapped_column(Integer, default=0)


class WorkflowTemplateRating(Base, IdMixin, TimestampMixin, TenantMixin):
    """模板评分：每租户对每个模板只能打一次分（可改）。

    用独立表而非只存聚合值，是为了：
    - 防重复评分（唯一约束）
    - 允许用户修改自己的评分
    - 将来做「评价内容」时不用改表结构
    """

    __tablename__ = "workflow_template_rating"
    __table_args__ = (
        UniqueConstraint("template_id", "tenant_id", name="uq_template_rating_tenant"),
    )

    template_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    rating: Mapped[int] = mapped_column(Integer, default=5)  # 1-5
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class WorkflowCallbackDelivery(Base, IdMixin, TimestampMixin, TenantMixin):
    """Webhook 异步回调的投递记录（含重试状态）。

    为什么独立成表而不是塞进 run.output：
    重试是跨请求的 —— 进程重启后还要能续投，需要独立行来更新
    status/attempts/next_attempt_at。
    """

    __tablename__ = "workflow_callback_delivery"

    run_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    # 加密存储的 HMAC 密钥（回调签名用）
    secret: Mapped[str | None] = mapped_column(String(512), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # pending（待首次投递）/ retrying（等待重试）/ sent / dead（放弃）
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # ms
    last_attempt_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class WorkflowVersion(Base, IdMixin, TimestampMixin, TenantMixin):
    """工作流版本快照：每次发布存一份，可回滚。

    与 Workflow.version 的区别：那个只是自增计数，改坏了没法恢复。
    这张表存完整 graph 快照，回滚就是把快照写回 Workflow.graph。
    """

    __tablename__ = "workflow_version"

    workflow_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    graph: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class WorkflowTest(Base, IdMixin, TimestampMixin, TenantMixin):
    """工作流测试用例：把调试对话固化成可回归的断言。

    挂在 agent 上而非图快照：图会随编辑变化，用例描述的是「这个助手应有的表现」，
    应跨图版本存活。
    """

    __tablename__ = "workflow_test"

    agent_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), default="")
    input: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # start 输入
    expected: Mapped[str | None] = mapped_column(Text, nullable=True)  # 期望输出包含的片段
    # True=断言包含（默认）；False=只做冒烟（能跑完即通过）
    assert_contains: Mapped[bool] = mapped_column(Boolean, default=True)
    # 多轮上下文：[{role: "user"|"assistant", content: "..."}]
    # 空表示单轮用例。多轮用例会把这些历史拼进 history 变量，
    # 用于验证「刚才那个订单」这类指代是否能被正确解析。
    history: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # 上次执行结果（列表页直接展示，避免每次打开都跑）
    last_status: Mapped[str | None] = mapped_column(String(16), nullable=True)  # passed/failed/error/waiting
    last_actual: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_run_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class AgentAction(Base, IdMixin, TimestampMixin, TenantMixin):
    """AI 工具写操作的待确认动作（HITL）：AI 发起 → 人工确认 → 执行。

    write 类工具不直接执行，先落此表 status=pending，前端弹确认卡片；
    确认端点用 CAS（WHERE status='pending'）防重放，确认者需具备该工具权限。
    """

    __tablename__ = "agent_action"

    conversation_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    agent_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)  # 发起者
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_kind: Mapped[str] = mapped_column(String(16), default="write")
    arguments: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    raw_tool_call: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # LLM 原始 tool_call（续跑必需）
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)  # 人类可读影响摘要
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|approved|rejected|executed|expired|failed
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    executed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class AgentVersion(Base, IdMixin, TimestampMixin, TenantMixin):
    """智能体版本快照（可回滚）。"""

    __tablename__ = "agent_version"

    agent_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
