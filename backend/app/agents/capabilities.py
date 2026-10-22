"""平台能力感知：生成「平台功能总览 + 当前账号能力」摘要，注入 AI system。

目的：让 AI 知道本平台有哪些功能模块、当前账号有哪些权限、能调用哪些工具，
从而准确回答"你能干什么"、并在无权限时明确告知（而非报错或幻觉）。
注意：这只是"告知"，真正的准入仍由后端 require_permission / tool_allowed 强制。
"""
from __future__ import annotations

# 平台功能目录：(模块名, 一句话用途, 所需权限码 | None)
PLATFORM_FEATURES: list[tuple[str, str, str | None]] = [
    ("智能问答", "多轮对话，可结合知识库回答（RAG）、切换模型、查看历史", "chat:use"),
    ("知识库", "创建/管理知识库，配置分块策略与向量模型", "kb:read"),
    ("文档管理", "上传/解析/分块文档，支持 PDF/Office/图片，可重灌、打标签、设权限", "doc:read"),
    ("检索调试", "测试向量+关键词混合检索效果", "retrieval:query"),
    ("智能体", "配置 AI 助手（提示词/技能/知识库/工具），支持多轮工具调用", "agent:read"),
    ("技能", "为智能体扩展能力（提示词包 / 工具 / 脚本）", "skill:read"),
    ("工具", "内置与外部 HTTP 工具，供智能体调用", "tool:read"),
    ("MCP 服务器", "对接外部 MCP（Model Context Protocol）工具服务，同步后自动注册为 AI 工具", "mcp:read"),
    ("工作流", "可视化编排多节点任务（LLM/检索/条件/循环/审批/协作）", "workflow:read"),
    ("定时任务", "按时间(cron/间隔/一次性)或事件(document.ready/document.failed/workflow.completed/workflow.failed)自动触发提示词/工作流；支持失败重试、任务级通知(总是/仅成功/仅失败/从不)；执行结果只在定时任务页的执行记录里，不占用问答会话", "schedule:read"),
    ("通知渠道", "配置结果通知出口：站内消息/Webhook/企业微信/钉钉/邮件/外部渠道(把已接入的 QQ/微信/企微/飞书 兼作推送出口)", "notify:read"),
    ("外部渠道", "对接 QQ/微信/企业微信/飞书：既能做问答，也可作通知出口与工作流推送节点；渠道有「服务模式」(只答/客服)；外部客户与内部用户隔离（不进用户列表、不得读 internal 库）；用户发 /myuid 查 id、/ticket 转人工、/record 智能录单", "channel:read"),
    ("成员与权限", "管理用户/角色/部门/用户组，控制各功能访问权限", "user:read"),
    ("模型管理", "配置对话/向量/重排模型供应商与端点", "model:read"),
    ("审计与日志", "查看操作审计、系统运行日志、内容安全", "audit:read"),
    ("文件管理", "统一管理上传与生成的产物文件", "file:manage"),
    ("邮件入库", "配置邮箱(IMAP)，把邮件正文与附件自动入库到知识库；员工把资料发到该邮箱即可被检索", "kb:read"),
    ("客服工单", "工单全生命周期：分类/标签/SLA超时/满意度；渠道转人工建单、对外 API 可建单查单、客服回复回发渠道；支持多语言客服（/lang 或渠道默认语言）", "service:read"),
    ("智能录单", "定义录单模板，把通话记录/聊天/邮件等文本交给 AI 抽成结构化台账，可导出 Excel/CSV", "record:read"),
    ("用量统计", "查看 token 消耗与调用统计", "model:read"),
    ("API 密钥", "生成密钥供外部系统调用平台（OpenAI 兼容接口）", "apikey:read"),
    ("单点登录", "配置 SSO（企业微信/钉钉/飞书）", "sso:read"),
]


def current_capabilities(perms: set[str], is_admin: bool) -> list[dict]:
    """返回当前账号对每个平台功能的可用性。"""
    out: list[dict] = []
    for name, desc, perm in PLATFORM_FEATURES:
        ok = is_admin or perm is None or "*" in perms or perm in perms
        out.append({"name": name, "desc": desc, "perm": perm, "allowed": ok})
    return out


# 命中"能力/权限"类问法 → 注入完整能力摘要（否则只给极短横幅，省 token）
_CAP_KEYWORDS = (
    "你能做", "你能干", "你会什么", "会什么", "能做什么", "能干什么", "有什么功能",
    "有哪些功能", "支持哪些", "支持什么", "有没有权限", "有哪些权限", "权限",
    "能不能", "可不可以", "能管理", "能操作", "功能清单", "功能列表",
    "你是谁", "介绍一下你", "如何使用", "怎么使用", "帮我做什么",
)


def is_capability_query(query: str) -> bool:
    """判断是否在问平台能力/权限（决定是否注入完整能力摘要）。"""
    q = (query or "").strip()
    if not q:
        return False
    return any(k in q for k in _CAP_KEYWORDS)


def build_tool_hint(is_admin: bool = False) -> str:
    """工具能力提示（**与知识库内容无关**，检索命中时也应注入）。

    与 build_capability_banner 的区别：横幅含"你在哪个平台"这类身份声明，检索命中知识库时
    可能与之抢答；而本提示只讲"你有哪些工具、什么场景调什么"，不涉及身份，安全常驻。
    """
    base = (
        "[工具能力] 你可调用平台工具完成实际动作（不是只做文字回复）。"
        "常见：知识库检索 knowledge_retrieval；读写/生成文件 generate_file/read_file；"
        "表格统计 analyze_table；生成正式文档 generate_office_doc；个人待办 manage_reminder；"
        "个人邮箱 manage_mail（收发自己的邮件）；管理自己的会话 manage_conversation。"
        "**要按「第几段/某单元格」精确读写 docx/xlsx/pptx/pdf 时**，"
        "先用 read_doc_structure 拿到可寻址元素（address），再用 edit_doc_structure 按地址改写"
        "（它会另存新文件，不改原件）；只概括全文用 read_file 更省 token。"
        "**需要登录态或 JS 渲染的网页**用浏览器工具：browser_tabs / browser_read / "
        "browser_action（点击、填表、截图、跳转）；公开网页优先 http_request 更省资源。"
    )
    if is_admin:
        base += (
            "管理能力（需权限，写操作会先弹确认）：建/改知识库、文档、智能体、技能、定时任务、"
            "渠道、用户/角色/部门、模型配置等；"
            "**把聊天/通话/邮件等文本抽成结构化台账用 extract_records**（先 list_templates 拿模板 id）；"
            "录单模板用 manage_record_template；待办/会话见上。"
            "不确认某项平台操作能否做时，用 check_my_capabilities 查权限。"
        )
    base += (
        " 原则：用户给了可执行的数据/诉求时，**优先调用相应工具直接完成**，"
        "而不是把内容整理成表格再反问——除非确实缺关键信息。"
    )
    return base


def build_capability_banner(is_admin: bool = False) -> str:
    """常驻极短横幅（替代完整能力摘要，省 token）。"""
    role = "管理员" if is_admin else "用户"
    return (
        f"[平台能力] 你在「企业 RAG 知识库平台」（当前账号：{role}）。"
        "查询平台功能与本账号权限用 check_my_capabilities。"
        "仅当要执行**平台管理/运维操作**（建知识库、管理用户/角色/部门、配定时任务、改模型配置等）"
        "且手上没有对应工具时，才用 find_tools 检索。"
        "普通任务（调外部接口、读写/生成文件、知识库检索、按技能说明执行）请**直接使用你已拥有的工具**"
        "（如 http_request / generate_file / knowledge_retrieval），不要调用 find_tools。"
    )


async def build_capability_brief(
    db, *, perms: set[str], is_admin: bool, tool_config: dict | None = None,
    skill_ids: list[int] | None = None, tenant_id: int = 0,
) -> str:
    """生成注入用的能力摘要文本。tools 为空则跳过工具清单。"""
    caps = current_capabilities(perms, is_admin)
    allowed = [c["name"] for c in caps if c["allowed"]]
    denied = [f"{c['name']}（需 {c['perm']}）" for c in caps if not c["allowed"] and c["perm"]]

    lines = ["[平台能力] 你正在「企业 RAG 知识库平台」中工作。平台功能："]
    for c in caps:
        mark = "✓" if c["allowed"] else "✗"
        lines.append(f"  {mark} {c['name']}：{c['desc']}")
    lines.append("")
    if is_admin:
        lines.append("[当前账号] 管理员，拥有全部权限。")
    else:
        lines.append(f"[当前账号] 可用的功能：{'、'.join(allowed) or '（无）'}。")
        if denied:
            lines.append(f"不可用（无权限）：{'、'.join(denied)}。")

    # 可用工具清单
    if tool_config is not None:
        try:
            from app.agents.tools.registry import resolve_tools

            tools = await resolve_tools(
                db, tool_config=tool_config, skill_ids=skill_ids or [],
                tenant_id=tenant_id, perms=perms,
            )
            if tools:
                names = "、".join(f"{t.name}" for t in tools)
                lines.append(f"[可调用工具] {names}")
        except Exception:  # noqa: BLE001
            pass

    lines.append("回答「你能做什么」时据实说明；遇到无权限的操作，明确告知缺少什么权限，不要假装能做。")
    lines.append("办公文档：需要生成正式文档（会议纪要/周报日报/待办清单/公文通知）时，用 generate_office_doc "
                 "（按模板填字段，工具负责规范排版含表格）；通用文件（表格/PPT/自由正文）用 generate_file；"
                 "改写用户上传的 docx/xlsx 用 edit_file；转格式用 convert_file_to。")
    lines.append("表格数据问答：对 Excel/CSV 做统计（总额/平均/分组汇总等）用 analyze_table"
                 "（传 file_key 或 doc_id + column + agg）；纯预览用 read_file。")
    if tool_config is not None and is_admin:
        lines.append("你具备管理权限：可直接操作用户/角色/部门/用户组、知识库（含按部门/角色授权、统计查询）、"
                     "智能体（含克隆/版本回滚/模式）、技能（含导入/更新/脚本试跑）、工具台、"
                     "MCP 服务器（接入/测试/同步外部 MCP 工具）、定时任务（创建/删除/立即运行，"
                     "'X 分钟后提醒'用一次性任务）、Webhook 令牌、API 密钥、文档（批量/ACL/标签/分块编辑）、"
                     "文件库（列出/删除/清理产物）、外部渠道（含测试连接）、工作流（发布/运行/审批）、"
                     "模型 Provider 与配置（含测试/健康检查/列模型）、邮件入库源、系统设置等——写操作会先弹确认。"
                     "定时任务用 create_scheduled_task 创建（'X 分钟后提醒'用一次性任务）；"
                     "查某任务跑没跑成功用 list_task_runs；通知出口（站内/企微/钉钉/邮件/Webhook/外部渠道）用 manage_notify_channel 配置；"
                     "改文档分块用 manage_doc_chunk，测模型连通性用 test_provider，查知识库规模用 query_kb_stats；"
                     "跑工作流用 run_workflow（含审批 approve_workflow_run）；配置邮箱自动入库用 manage_email_source；"
                     "导出/分享对话用 export_conversation，合并导出文档用 merge_export_documents，改系统设置用 manage_system_settings；"
                     "列出/删除/清理产物文件用 manage_files，试跑技能包脚本用 run_skill_script；"
                     "客服工单（渠道转人工生成）用 manage_service_ticket 查看/回复/关闭；"
                     "把文本抽成结构化台账用 extract_records（先 list_templates 拿模板 id）。"
                     "录单模板用 manage_record_template（建模板/字段、看台账、导出 CSV）；"
                     "客服话术库用 manage_quick_reply（增删改查）；渠道身份绑定内部账号用 manage_channel_user；"
                     "站内通知用 manage_notification（给同事发通知、标记已读）；"
                     "问答评测用 manage_eval（建数据集/加题/跑评测/看结果）。")
    else:
        lines.append("个人待办/日程用 manage_reminder（创建/列出/完成/删除，可设到期时间与重复）；"
                     "管理自己的问答会话用 manage_conversation（列出/重命名/删除/导出）；"
                     "个人邮箱收发用 manage_mail（列账号/看收件箱/读信/写信/回复/标记，只能操作自己的邮箱）。")
    return "\n".join(lines)


async def check_permission_brief(perms: set[str], is_admin: bool, *, query: str = "") -> str:
    """给"查权限"工具用：返回当前账号权限摘要，或针对 query 匹配的功能说明。"""
    caps = current_capabilities(perms, is_admin)
    if query:
        q = query.strip()
        hits = [c for c in caps if q in c["name"] or q in (c["desc"] or "")]
        if not hits:
            return f"未找到与「{q}」相关的平台功能。可查询：{'、'.join(c['name'] for c in caps)}"
        lines = []
        for c in hits:
            if c["allowed"]:
                lines.append(f"「{c['name']}」：可用。{c['desc']}")
            else:
                lines.append(f"「{c['name']}」：无权限（需要 {c['perm']}）。{c['desc']}")
        return "\n".join(lines)

    if is_admin:
        return f"当前账号是管理员，拥有全部权限。平台功能：{'、'.join(c['name'] for c in caps)}"
    allowed = [c["name"] for c in caps if c["allowed"]]
    denied = [f"{c['name']}({c['perm']})" for c in caps if not c["allowed"] and c["perm"]]
    return (
        f"当前账号可用功能：{'、'.join(allowed) or '（无）'}\n"
        f"无权限功能：{'、'.join(denied) or '（无）'}"
    )
