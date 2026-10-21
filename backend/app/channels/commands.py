"""渠道指令系统：以命令前缀（默认 /）开头，路由到对应处理器。

每个 handler 签名：async (ctx: CommandContext, args: list[str]) -> str
返回要回复给用户的文本。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, Channel, ChannelUser, KnowledgeBase, User


@dataclass
class CommandContext:
    db: AsyncSession
    channel: Channel
    channel_user: ChannelUser
    user: User
    ps: object  # PrincipalSet
    available_kbs: list[KnowledgeBase] = field(default_factory=list)
    external_group: str | None = None  # 群聊时的群 id（/myuid 用）


HELP_TEXT = """可用指令：
/help            查看帮助
/kb list         查看可用知识库
/kb use <id>     切换知识库（如 /kb use 1）
/kb off          关闭知识库检索（纯聊天）
/agent list      查看可用智能体
/agent use <id>  切换智能体
/agent off       取消智能体，回到知识库问答
/run list        查看可执行的工作流
/run <id> <参数> 执行指定工作流（如 /run 5 订单号 A123）
/new             新建会话（清空上下文）
/whoami          查看当前身份与设置
/myuid           查看我的 id（用于接收定时任务/工作流的推送）
/ticket <问题>   转人工建工单；/ticket list 看我的工单；/ticket close 关闭
/record list     查看录单模板；/record <模板id> <文本> 智能录单
/lang <代码>     切换回答语言，如 /lang en、/lang zh、/lang ja
/bind            获取绑定码（管理员在后台绑定到内部账号后，你在渠道里使用其权限）
直接输入内容即可提问。"""


async def cmd_help(ctx: CommandContext, args: list[str]) -> str:
    return HELP_TEXT


async def cmd_kb(ctx: CommandContext, args: list[str]) -> str:
    sub = args[0].lower() if args else "list"
    cu = ctx.channel_user
    if sub == "list":
        if not ctx.available_kbs:
            return "当前没有可用的知识库（请联系管理员授权）。"
        cur = set(cu.default_kb_ids or ctx.channel.default_kb_ids or [])
        lines = ["可用知识库："]
        for k in ctx.available_kbs:
            mark = " ✓" if k.id in cur else ""
            lines.append(f"  [{k.id}] {k.name}（{k.doc_count} 文档）{mark}")
        lines.append("用 /kb use <id> 切换，/kb off 关闭检索。")
        return "\n".join(lines)
    if sub == "off":
        cu.default_kb_ids = []
        await ctx.db.flush()
        return "已关闭知识库检索，将使用模型自身知识回答。"
    if sub == "use":
        if len(args) < 2 or not args[1].isdigit():
            return "用法：/kb use <id>"
        kid = int(args[1])
        ids = {k.id for k in ctx.available_kbs}
        if kid not in ids:
            return f"知识库 #{kid} 不存在或无权访问。"
        cu.default_kb_ids = [kid]
        await ctx.db.flush()
        name = next((k.name for k in ctx.available_kbs if k.id == kid), str(kid))
        return f"已切换到知识库：{name}"
    return "未知子命令。用法：/kb list | /kb use <id> | /kb off"


async def cmd_agent(ctx: CommandContext, args: list[str]) -> str:
    sub = args[0].lower() if args else "list"
    cu = ctx.channel_user
    if sub == "list":
        rows = (
            await ctx.db.execute(
                select(Agent).where(Agent.tenant_id == ctx.user.tenant_id, Agent.status == "active")
            )
        ).scalars().all()
        if not rows:
            return "当前没有可用的智能体。"
        lines = ["可用智能体："]
        for a in rows:
            mark = " ✓" if cu.agent_id == a.id else ""
            lines.append(f"  [{a.id}] {a.name}{mark}")
        return "\n".join(lines) + "\n用 /agent use <id> 切换，/agent off 取消。"
    if sub == "off":
        cu.agent_id = None
        await ctx.db.flush()
        return "已取消智能体，回到知识库问答模式。"
    if sub == "use":
        if len(args) < 2 or not args[1].isdigit():
            return "用法：/agent use <id>"
        aid = int(args[1])
        a = await ctx.db.get(Agent, aid)
        if not a or a.tenant_id != ctx.user.tenant_id:
            return f"智能体 #{aid} 不存在或无权访问。"
        cu.agent_id = aid
        await ctx.db.flush()
        return f"已切换到智能体：{a.name}（发消息即由它回答）"
    return "未知子命令。用法：/agent list | /agent use <id> | /agent off"


async def cmd_new(ctx: CommandContext, args: list[str]) -> str:
    ctx.channel_user.conversation_id = None
    await ctx.db.flush()
    return "已开始新会话，上下文已清空。"


async def cmd_whoami(ctx: CommandContext, args: list[str]) -> str:
    cu = ctx.channel_user
    kbs = cu.default_kb_ids or ctx.channel.default_kb_ids or []
    agent = ""
    if cu.agent_id:
        a = await ctx.db.get(Agent, cu.agent_id)
        agent = a.name if a else str(cu.agent_id)
    return (
        f"渠道：{ctx.channel.name}（{ctx.channel.kind}）\n"
        f"身份：{cu.display_name or cu.external_id}\n"
        f"知识库：{kbs or '未设置'}\n"
        f"智能体：{agent or '未启用'}\n"
        f"回复模式：{ {'rag': '知识库问答', 'smart': '智能问答', 'agent': '智能体', 'workflow': '工作流'}.get(ctx.channel.reply_mode, ctx.channel.reply_mode) }"
    )


async def cmd_myuid(ctx: CommandContext, args: list[str]) -> str:
    """查看可推送标识：私聊看用户 id，群聊看群 id。用于配置通知/推送接收方。"""
    cu = ctx.channel_user
    lines = [f"渠道：{ctx.channel.name}（{ctx.channel.kind}）"]
    if ctx.external_group:
        lines.append(f"群 id：{ctx.external_group}")
        lines.append("（如需把通知/工作流结果推到本群，请在「通知渠道 → 外部渠道」的接收对象填上面的群 id，类型选「群」）")
    lines.append(f"我的用户 id：{cu.external_id}")
    lines.append("（如需私聊推送，接收对象填上面的用户 id，类型选「个人」）")
    return "\n".join(lines)


async def cmd_run(ctx: CommandContext, args: list[str]) -> str:
    """按需执行工作流：/run list 列出可执行工作流；/run <id|名称> [参数] 执行。

    与「渠道默认工作流」互补：默认工作流处理所有消息，`/run` 用于客户主动调用
    某个特定流程（如「查订单」「开票」），适合「一个客服渠道 + 多个业务动作」的形态。
    """
    from app.agents.workflow.runner import run_workflow_for_channel
    from app.models import Workflow, WorkflowRun

    cu = ctx.channel_user
    sub = args[0] if args else ""

    rows = (
        await ctx.db.execute(
            select(Workflow).where(Workflow.tenant_id == ctx.user.tenant_id)
            .order_by(Workflow.id.desc()).limit(50)
        )
    ).scalars().all()
    # 只挂到 workflow 型智能体且图非空的工作流才可执行
    runnable: list[tuple[int, str, object]] = []
    for wf in rows:
        if not (wf.graph or {}).get("nodes"):
            continue
        a = await ctx.db.get(Agent, wf.agent_id) if wf.agent_id else None
        if not a or a.type != "workflow":
            continue
        runnable.append((a.id, f"{a.name}（#{a.id}）", (a, wf)))

    if not sub or sub.lower() == "list":
        if not runnable:
            return "当前没有可执行的工作流（需管理员在「智能体 → 工作流」里发布）。"
        lines = ["可执行的工作流："]
        for aid, label, _ in runnable:
            lines.append(f"  [#{aid}] {label}")
        lines.append("用 /run <id> <参数> 执行，例如 /run 5 订单号 A123")
        return "\n".join(lines)

    # 解析目标：id 或名称模糊匹配
    target = None
    if sub.isdigit():
        target = next((r for r in runnable if r[0] == int(sub)), None)
    if target is None:
        target = next((r for r in runnable if sub in r[1]), None)
    if target is None:
        return f"没找到工作流「{sub}」。发送 /run list 查看可用的。"

    aid, label, (agent, wf) = target
    payload = " ".join(args[1:]).strip() or "开始"
    cu.agent_id = aid  # 记住选择，后续普通消息也走它
    await ctx.db.flush()

    import time

    from app.models import Conversation, WorkflowRun

    conv = await ctx.db.get(Conversation, cu.conversation_id) if cu.conversation_id else None
    run = WorkflowRun(
        tenant_id=ctx.user.tenant_id, workflow_id=wf.id, agent_id=aid,
        user_id=ctx.user.id, conversation_id=cu.conversation_id, status="running",
        input={"query": payload}, graph_snapshot=wf.graph, created_at=int(time.time() * 1000),
    )
    ctx.db.add(run)
    await ctx.db.commit()
    run_id = run.id

    from app.middleware.auth_dep import get_user_permission_codes

    perms = await get_user_permission_codes(ctx.db, ctx.user)
    r = await run_workflow_for_channel(
        graph=wf.graph, query=payload, run_id=run_id, tenant_id=ctx.user.tenant_id,
        ps=ctx.ps, db=ctx.db, conversation=conv, attachments=None,
        default_model_config_id=agent.model_config_id, default_kb_ids=agent.kb_ids,
        default_system=agent.system_prompt,
        default_temperature=(agent.config or {}).get("temperature"),
        perms=perms,
    )
    return r.get("text") or "（工作流无输出）"


async def cmd_ticket(ctx: CommandContext, args: list[str]) -> str:
    """客服工单：/ticket <问题> 建工单；/ticket 查看我的工单；/ticket close 关闭。"""
    from app.services.service_ticket_service import (
        close_ticket, create_ticket, find_open_ticket, list_tickets,
    )

    sub = args[0].lower() if args else ""
    cu = ctx.channel_user
    if sub == "close":
        tk = await find_open_ticket(ctx.db, tenant_id=cu.tenant_id, channel_id=ctx.channel.id,
                                   external_user=cu.external_id, external_group=ctx.external_group)
        if not tk:
            return "你没有进行中的工单。"
        await close_ticket(ctx.db, tk, resolution="用户主动关闭")
        return f"已关闭工单 #{tk.id}。"
    if sub == "list" or not args:
        rows = await list_tickets(ctx.db, tenant_id=cu.tenant_id, limit=10)
        mine = [t for t in rows if t.external_user == cu.external_id]
        if not mine:
            return "你还没有工单。发送「/ticket 问题描述」即可创建，或直接说「转人工」。"
        lines = [f"我的工单（{len(mine)}）："]
        for t in mine[:5]:
            lines.append(f"  #{t.id} [{t.status}] {t.subject}")
        return "\n".join(lines)
    # /ticket <问题> → 建单
    content = " ".join(args)
    tk = await create_ticket(
        ctx.db, tenant_id=cu.tenant_id, subject=content[:80], content=content,
        channel_id=ctx.channel.id, channel_kind=ctx.channel.kind,
        external_user=cu.external_id, external_group=ctx.external_group,
        channel_user_id=cu.id, conversation_id=cu.conversation_id,
    )
    return f"已创建工单 #{tk.id}，客服会尽快回复你。"


async def cmd_record(ctx: CommandContext, args: list[str]) -> str:
    """智能录单：/record list 列模板；/record <模板id> <文本> 抽取入库。"""
    from sqlalchemy import select as _select

    from app.models import RecordEntry, RecordTemplate

    sub = args[0].lower() if args else "list"
    if sub == "list" or not args:
        rows = (await ctx.db.execute(
            _select(RecordTemplate).where(RecordTemplate.tenant_id == ctx.channel_user.tenant_id,
                                          RecordTemplate.enabled.is_(True))
        )).scalars().all()
        if not rows:
            return "当前没有可用的录单模板，请联系管理员在「智能录单」中创建。"
        lines = ["可用录单模板："]
        for t in rows:
            lines.append(f"  #{t.id} {t.name}")
        lines.append("用法：/record <模板id> <文本内容>，如 /record 1 客户张三 下单A123 金额199")
        return "\n".join(lines)
    if not sub.isdigit():
        return "用法：/record list 查看模板；/record <模板id> <文本> 抽取。"
    from app.services.record_extract_service import extract_records

    tid = int(sub)
    text = " ".join(args[1:]).strip()
    if not text:
        return "请提供要抽取的文本，如 /record 1 客户张三 下单A123 金额199"
    t = await ctx.db.get(RecordTemplate, tid)
    if not t or t.tenant_id != ctx.channel_user.tenant_id or not t.enabled:
        return f"模板 #{tid} 不存在或未启用。"
    try:
        rows = await extract_records(ctx.db, tenant_id=ctx.channel_user.tenant_id, template=t, text=text)
    except Exception as e:  # noqa: BLE001
        return f"抽取失败：{str(e)[:200]}"
    # 已绑定内部账号的渠道用户（可信、继承内部权限）→ 自动确认；未绑定的外部客户 → 保持草稿待核对。
    init_status = "confirmed" if ctx.channel_user.bound_user_id else "draft"
    for row in rows:
        ctx.db.add(RecordEntry(
            tenant_id=ctx.channel_user.tenant_id, template_id=t.id, data=row,
            source_type="channel", raw_text=text[:8000], created_by=ctx.user.id, status=init_status,
        ))
    await ctx.db.flush()
    first = rows[0] if rows else {}
    summary = "；".join(f"{k}={v}" for k, v in list(first.items())[:6])
    tip = "，已自动确认" if init_status == "confirmed" else ""
    return f"已录入 {len(rows)} 条{tip}（模板「{t.name}」）：{summary}"


# 支持的语言代码 → 名称（多语言客服）
_LANG_NAMES = {"zh": "中文", "en": "English", "ja": "日本語", "ko": "한국어", "es": "Español",
               "fr": "Français", "de": "Deutsch", "ru": "Русский", "pt": "Português"}


async def cmd_lang(ctx: CommandContext, args: list[str]) -> str:
    """切换回答语言（多语言客服）。/lang <代码>；/lang 查看当前。"""
    cu = ctx.channel_user
    if not args:
        cur = cu.lang or "(自动)"
        return f"当前回答语言：{cur}。可用：{'、'.join(f'{k}({v})' for k, v in _LANG_NAMES.items())}\n用法：/lang en"
    code = args[0].strip().lower()
    if code in ("auto", "off"):
        cu.lang = None
        await ctx.db.flush()
        return "已恢复自动判断语言。"
    if code not in _LANG_NAMES:
        return f"不支持的语言：{code}。可用：{'、'.join(_LANG_NAMES.keys())}"
    cu.lang = code
    await ctx.db.flush()
    return f"已切换为 {_LANG_NAMES[code]}，之后我会用该语言回答。"


async def cmd_bind(ctx: CommandContext, args: list[str]) -> str:
    """生成一次性绑定码，让管理员在后台把本渠道身份绑定到内部账号（继承其权限）。

    /bind        生成绑定码
    /bind status 查看当前绑定状态
    """
    import secrets

    cu = ctx.channel_user
    sub = args[0].lower() if args else ""
    if sub == "status":
        if cu.bound_user_id:
            return f"你已绑定内部账号 #{cu.bound_user_id}，在渠道里用其真实权限。"
        return "你尚未绑定内部账号（当前为外部客户，仅只读）。发送 /bind 获取绑定码。"
    if cu.bound_user_id:
        return f"你已绑定内部账号 #{cu.bound_user_id}。如需解绑请联系管理员。"
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    cu.bind_code = code
    await ctx.db.flush()
    return (f"你的绑定码：{code}\n"
            "请让管理员在后台「渠道身份绑定」页（智能体 → 渠道身份绑定）用此码把你的渠道身份绑定到内部账号，"
            "之后在渠道里就使用该账号的权限。")


def detect_lang(text: str) -> str | None:
    """轻量语言检测：CJK 占比高→zh，含大量假名→ja，否则含拉丁字母→en。仅作兜底。"""
    if not text:
        return None
    cjk = sum(1 for c in text if "一" <= c <= "鿿")
    kana = sum(1 for c in text if "ぁ" <= c <= "ヿ")
    latin = sum(1 for c in text if "a" <= c.lower() <= "z")
    total = len(text.strip())
    if total == 0:
        return None
    if kana > 0 and kana >= cjk * 0.3:
        return "ja"
    if cjk / total > 0.25:
        return "zh"
    if latin / total > 0.3:
        return "en"
    return None


# 指令表（指令名不含前缀）
COMMANDS = {
    "help": cmd_help,
    "kb": cmd_kb,
    "agent": cmd_agent,
    "run": cmd_run,
    "new": cmd_new,
    "whoami": cmd_whoami,
    "myuid": cmd_myuid,
    "ticket": cmd_ticket,
    "record": cmd_record,
    "lang": cmd_lang,
    "bind": cmd_bind,
}


def is_command(text: str, prefix: str = "/") -> bool:
    return bool(text) and text.startswith(prefix)


def parse_command(text: str, prefix: str = "/") -> tuple[str, list[str]]:
    """解析 "/kb use 1" → ("kb", ["use", "1"])。"""
    body = text[len(prefix):].strip()
    parts = body.split()
    if not parts:
        return "help", []
    return parts[0].lower(), parts[1:]
