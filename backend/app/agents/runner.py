"""Agent 引擎：ReAct 工具循环 + 技能/模式组装。

产出统一事件流（yield dict），由 API 层转 SSE：
  meta | delta | tool_call | tool_result | citations | usage | done | error
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.tools.base import ToolContext, ToolResult
from app.agents.tools.registry import resolve_tools, to_openai_schema, tool_allowed
from app.core.logging import get_logger
from app.models import Agent, AgentMode, Conversation, Message, Skill, UsageLog
from app.providers.base import ChatMessage, ToolCall, ToolCallDelta
from app.providers.registry import get_llm
from app.services.audit_service import record_audit
from app.services.permission import PrincipalSet

logger = get_logger("agent")

DEFAULT_MAX_TURNS = 6
LLM_TIMEOUT = 120
TOOL_TIMEOUT = 60

# 引用纪律（智能体侧）：与问答页 SYSTEM_PROMPT 的引用规则对齐，避免两入口行为不一致
_CITATION_RULE = (
    "若你使用了检索到的知识库资料作答，请在相应句子后标注 [n] 引用编号（与资料条目一致），"
    "且不得编造引用编号或给自身知识标注编号；资料未覆盖时明确说明「知识库中未找到相关信息」。"
)


@dataclass
class EffectiveConfig:
    system_prompt: str = ""
    skill_ids: list[int] = field(default_factory=list)
    kb_ids: list[int] = field(default_factory=list)
    tool_config: dict = field(default_factory=dict)
    model_config_id: int | None = None
    params: dict = field(default_factory=dict)

    @property
    def max_turns(self) -> int:
        return int(self.tool_config.get("max_turns") or DEFAULT_MAX_TURNS)


async def _render_skill_prompt(db: AsyncSession, skills: list[Skill], params: dict) -> str:
    blocks = []
    # 总纲：让模型明确「这些技能要按步骤执行，不要只当知识复述」
    if skills:
        blocks.append(
            "【可用技能】下面是已启用的技能说明。当用户请求匹配某技能时，**按该技能的执行步骤"
            "直接调用相应工具完成**（例如说明里写了用 http_request 调接口，就直接调 http_request），"
            "不要仅凭记忆回答、也不要只调 find_tools 就作罢。"
        )
    for sk in skills:
        # 技能集合：只注入子技能目录（名+描述），正文由 AI 用 load_skill 按需加载
        if sk.kind == "collection":
            children = (await db.execute(
                select(Skill).where(
                    Skill.parent_id == sk.id, Skill.status == "active"
                ).order_by(Skill.id)
            )).scalars().all()
            if not children:
                continue
            lines = [
                f"【技能集合：{sk.name}】含以下子技能；当用户的请求匹配某个子技能时，"
                f"先调用 load_skill(skill_id=<该子技能id>) 加载其完整说明，再按说明操作："
            ]
            for c in children:
                lines.append(f"  - [{c.id}] {c.name}：{c.description or ''}")
            blocks.append("\n".join(lines))
            continue
        # 普通 prompt_pack（含被"单独挂载"的集合子技能）：注入正文
        if sk.kind == "prompt_pack" and sk.prompt_template:
            tpl = sk.prompt_template
            # 支持每技能命名空间：params[sk.slug] 覆盖全局 params
            local = {**params, **(params.get(sk.slug) or {}), "skill_name": sk.name}
            try:
                rendered = tpl.format(**local)
            except (KeyError, IndexError, ValueError):
                rendered = tpl
            blocks.append(f"【技能：{sk.name}】\n{rendered}")
    return "\n\n".join(blocks)


async def build_effective(db: AsyncSession, agent: Agent, mode: AgentMode | None) -> EffectiveConfig:
    """合并 agent 基座与 mode 覆盖，并渲染技能提示词。"""
    cfg = EffectiveConfig(
        system_prompt=agent.system_prompt or "",
        skill_ids=list(agent.skill_ids or []),
        kb_ids=list(agent.kb_ids or []),
        tool_config=dict(agent.tool_config or {}),
        model_config_id=agent.model_config_id,
        params=dict(agent.config or {}),
    )
    if mode:
        if mode.system_prompt:
            cfg.system_prompt = (cfg.system_prompt + "\n\n" + mode.system_prompt).strip()
        if mode.skill_ids:
            cfg.skill_ids = list(mode.skill_ids)
        if mode.kb_ids:
            cfg.kb_ids = list(mode.kb_ids)
        if mode.tool_config:
            # 深合并（mode 覆盖同名键）
            merged = {**cfg.tool_config}
            for k, v in mode.tool_config.items():
                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                    merged[k] = {**merged[k], **v}
                else:
                    merged[k] = v
            cfg.tool_config = merged
        if mode.params:
            cfg.params = {**cfg.params, **mode.params}

    # 渲染 prompt_pack 技能，注入 system
    if cfg.skill_ids:
        rows = (
            await db.execute(
                select(Skill).where(Skill.id.in_(cfg.skill_ids), Skill.status == "active")
            )
        ).scalars().all()
        skill_block = await _render_skill_prompt(db, rows, cfg.params)
        if skill_block:
            cfg.system_prompt = (cfg.system_prompt + "\n\n" + skill_block).strip()
        # 能力包带出的知识库
        for sk in rows:
            if sk.kind == "prompt_pack" and sk.kb_ids:
                cfg.kb_ids = list({*cfg.kb_ids, *sk.kb_ids})
    return cfg


def _finalize_tool_calls(acc: dict[int, dict]) -> list[ToolCall]:
    out = []
    for _, s in sorted(acc.items()):
        out.append(
            ToolCall(
                id=s.get("id") or f"call_{_}",
                name=s.get("name") or "",
                arguments=s.get("arguments") or "{}",
            )
        )
    return out


def _accumulate(acc: dict[int, dict], deltas: list[ToolCallDelta]) -> None:
    for d in deltas:
        slot = acc.setdefault(d.index, {"id": "", "name": "", "arguments": ""})
        if d.id:
            slot["id"] = d.id
        if d.name:
            slot["name"] = d.name
        slot["arguments"] += d.arguments_delta


class AgentRunner:
    def __init__(
        self,
        db: AsyncSession,
        *,
        agent: Agent,
        ps: PrincipalSet,
        conversation: Conversation | None,
        history: list[ChatMessage] | None = None,
        perms: set[str] | None = None,
        summary: str | None = None,
        model_override: int | None = None,
        images: list[dict] | None = None,
        persist: bool = True,
        allow_auto_write: bool = False,
        room_id: int | None = None,
        extra_tools: list | None = None,
    ) -> None:
        self.db = db
        self.agent = agent
        self.ps = ps
        self.conversation = conversation
        # persist=False（如定时任务）：不落 user/assistant 消息、不改会话计数，也不落 AgentAction。
        self.persist = persist and conversation is not None
        # allow_auto_write=True（如外部渠道绑定内部账号）：写工具直接执行，不走网页端 HITL 确认。
        self.allow_auto_write = allow_auto_write
        # 群聊作用域：机器人在某群被 @ 时注入，供房间作用域工具（群管）使用
        self.room_id = room_id
        # 额外工具（房间作用域群管工具）：随本次运行装配，不走 tool_config
        self.extra_tools = extra_tools or []
        self.history = history or []
        # 当前用户权限码集合：用于工具级过滤与二次校验。
        # 默认空集（安全）：未显式传 perms 时不给任何工具权限，避免越权。
        self.perms: set[str] = perms if perms is not None else set()
        # 历史压缩摘要（作为 system 前置，超长对话用）
        self.summary = summary
        # 本次运行的模型覆盖（Playground 按次切模型）
        self.model_override = model_override
        # 本轮随消息上传的图片（供视觉模型）
        self.images = images or None
        # 本次运行使用的行为模式 id（HITL 续跑时用它还原 mode 级配置，避免丢失）
        self.mode_id: int | None = None

    def _inject_caps(self, eff) -> bool:
        """是否注入平台能力摘要：agent.config 显式关则关，否则用全局默认。"""
        from app.core.config import settings

        cfg = getattr(eff, "params", None) or {}
        if isinstance(cfg, dict) and "inject_capabilities" in cfg:
            return bool(cfg["inject_capabilities"])
        agent_cfg = getattr(self.agent, "config", None) or {}
        if isinstance(agent_cfg, dict) and "inject_capabilities" in agent_cfg:
            return bool(agent_cfg["inject_capabilities"])
        return bool(getattr(settings, "inject_capabilities_default", True))

    async def _group_manager_brief(self) -> str | None:
        """群管机器人的角色说明：告诉它当前群与可用群管理工具。"""
        from sqlalchemy import select

        from app.models import ChatRoom, ChatRoomMember

        room = await self.db.get(ChatRoom, self.room_id)
        if not room:
            return None
        role = "member"
        if room.owner_id == self.agent.id:
            role = "owner"
        else:
            m = (
                await self.db.execute(
                    select(ChatRoomMember).where(
                        ChatRoomMember.room_id == room.id, ChatRoomMember.agent_id == self.agent.id
                    )
                )
            ).scalar_one_or_none()
            if m and m.role:
                role = m.role
        tool_names = "、".join(t.name for t in self.extra_tools)
        is_admin = role in ("owner", "admin")
        lines = [
            f"【群管模式】你正在群「{room.name}」中作为机器人被 @。",
            f"你在本群的角色：{'群主' if role == 'owner' else '管理员' if role == 'admin' else '普通成员'}。",
            "你可以使用以下群管理工具（本群作用域）：" + tool_names + "。",
        ]
        if is_admin:
            lines.append(
                "你是本群管理员，被要求时可主动执行：禁言/解禁、移出成员、设为/取消管理员、"
                "发公告、置顶消息、撤回消息、开启/关闭全员禁言、搜索消息、查看统计、"
                "创建定时任务（日报/周报/月报、夜间静默等）、发群里通知。"
                "执行管理操作前简要说明你将要做什么；操作成功后简短汇报结果。"
            )
        else:
            lines.append(
                "你是普通成员，只能查询（成员/消息搜索/统计/置顶公告）与发言；"
                "需要禁言/踢人等管理操作时，请提示需要先把本机器人设为群管理员。"
            )
        # 输出格式：先看用户是「要看效果」还是「要一个文件」，两者走不同路径
        lines.append(
            "【输出格式·先看用户要「效果」还是「文件」】用 Markdown 组织回答：标题用 #/##，列举用 -，"
            "表格用 GFM 表格，要点加粗用 **；引用检索资料标注 [n]，不要编造编号；"
            "群里是快节奏场景，除非被要求，控制在 200 字以内。\n"
            "**用户要「看效果/要一段代码」时（原话如「帮我写个页面」「做个动画」「画个图表」「演示一下」"
            "「看看效果」「阳光明媚的页面」）——只把完整代码放进带语言标记的代码块 ```html ... ```，"
            "不要调用 generate_file。** 代码块上方自带「预览」按钮，群成员点一下就能看到渲染结果，"
            "这是零副作用的，生成文件反而是多余的。\n"
            "代码块要求：语言标记必须是 html（写 SVG 也用 ```html 包住，内部内联 <svg> 标签）；"
            "内容必须是完整可独立运行的单文件（含 <!DOCTYPE html>、内联 CSS/JS），不许写「……」省略。"
        )
        # 防「嘴上说不调工具」：仅在用户明确要文件时才调 generate_file
        lines.append(
            "【重要·禁止空口声称】只有当用户明确要「文件」时才调用 generate_file 工具"
            "——即原话含「生成/给我/发我一个文件」「下载」「保存到本地」「导出 docx/pdf/xlsx」"
            "「发附件」等。\n"
            "说要文件却只回复「已生成 xxx.html」而不调工具，是欺骗：界面上不会出现任何东西。"
            "调用后文件会自动作为附件出现在你的消息里，此时不要再重复粘贴大段源码。\n"
            "**拿不准时按「看效果」处理**：先给 ```html 代码块，末尾补一句「需要文件的话我可以直接生成」。"
        )
        return "\n".join(lines)

    async def run(self, query: str, mode_id: int | None = None) -> AsyncIterator[dict]:
        t0 = time.time()
        mode = None
        if mode_id:
            mode = await self.db.get(AgentMode, mode_id)
        elif self.agent.default_mode_id:
            mode = await self.db.get(AgentMode, self.agent.default_mode_id)
        # 记住本次所用模式，供 HITL 续跑还原（_create_pending_action 会落到 raw_tool_call）
        self.mode_id = mode.id if mode else None

        eff = await build_effective(self.db, self.agent, mode)
        yield {"type": "meta", "agent_id": self.agent.id, "mode_id": mode.id if mode else None}

        # 内容安全：输入侧检测（高风险直接拦截）
        from app.core.config import settings as _settings

        if _settings.security_guard_enabled:
            from app.services.security_guard import detect

            sensitive = [w.strip() for w in _settings.security_guard_sensitive_words.split(",") if w.strip()]
            g = detect(
                query, extra_sensitive=sensitive,
                block_threshold=_settings.security_guard_block_threshold,
                flag_threshold=_settings.security_guard_flag_threshold,
            )
            if g.blocked:
                yield {"type": "error", "message": f"输入被安全策略拦截（{g.risk_level}）: {g.summary()}"}
                yield {"type": "done", "blocked": True}
                return

        # 落用户消息（persist=False 时跳过）
        if self.persist:
            user_msg = Message(
                tenant_id=self.ps.tenant_id,
                conversation_id=self.conversation.id,
                role="user",
                content=query,
                created_at=int(time.time() * 1000),
            )
            self.db.add(user_msg)
            await self.db.commit()

        tools = await resolve_tools(
            self.db,
            tool_config=eff.tool_config,
            skill_ids=eff.skill_ids,
            tenant_id=self.ps.tenant_id,
            perms=self.perms,
            is_external=bool(getattr(self.ps, "is_external", False)),
        )
        # 房间作用域群管工具：随本次运行装配（仅群聊机器人，按其在群内角色放行）
        if self.extra_tools:
            seen = {t.name for t in tools}
            for t in self.extra_tools:
                if t.name not in seen and tool_allowed(t, self.perms):
                    tools.append(t)
                    seen.add(t.name)

        llm, rm = await get_llm(self.db, tenant_id=self.ps.tenant_id,
                                config_id=self.model_override or eff.model_config_id)

        messages: list[ChatMessage] = []
        if eff.system_prompt:
            messages.append(ChatMessage(role="system", content=eff.system_prompt))
        # 群管机器人：告知其在群内角色与可用群管理能力
        if self.room_id and self.extra_tools:
            role_brief = await self._group_manager_brief()
            if role_brief:
                messages.append(ChatMessage(role="system", content=role_brief))
        # 引用纪律（与问答页对齐）：用到检索资料时标注 [n]，不得编造引用编号
        if self._inject_caps(eff):
            messages.append(ChatMessage(role="system", content=_CITATION_RULE))
        # 平台能力感知：让 AI 知道平台功能 + 当前账号权限 + 可用工具
        if self._inject_caps(eff):
            try:
                from app.agents.capabilities import build_capability_brief

                brief = await build_capability_brief(
                    self.db, perms=self.perms, is_admin=bool(getattr(self.ps, "is_admin", False)),
                    tool_config=eff.tool_config, skill_ids=eff.skill_ids,
                    tenant_id=self.ps.tenant_id,
                )
                if brief:
                    messages.append(ChatMessage(role="system", content=brief))
            except Exception:  # noqa: BLE001
                pass
        if self.summary:
            messages.append(ChatMessage(role="system", content=f"[历史对话摘要]\n{self.summary}"))
        messages.extend(self.history)
        messages.append(ChatMessage(role="user", content=query, images=self.images))

        collected_text = ""
        collected_reasoning = ""
        citations: list = []
        usage_total: dict = {}
        tool_records: list[dict] = []
        turns = 0
        suspended = False

        try:
            if not tools:
                # 退化：单次生成
                async for evt in self._single_turn(llm, rm, messages, eff):
                    if evt["type"] == "delta":
                        collected_text += evt["text"]
                    elif evt["type"] == "reasoning":
                        collected_reasoning += evt["text"]
                    yield evt
            else:
                async for evt in self._react_loop(llm, rm, messages, tools, eff, tool_records):
                    if evt["type"] == "delta":
                        collected_text += evt["text"]
                    elif evt["type"] == "reasoning":
                        collected_reasoning += evt["text"]
                    elif evt["type"] == "citations":
                        citations.extend(evt.get("citations") or [])
                    elif evt["type"] == "usage":
                        usage_total.update(evt.get("usage") or {})
                    elif evt["type"] == "turn":
                        turns = evt["turn"]
                        continue
                    elif evt["type"] == "_suspended":
                        # HITL：写操作挂起，等管理员确认后续跑；此处不落助手消息
                        suspended = True
                        continue
                    yield evt
        except Exception as e:  # noqa: BLE001
            logger.exception("agent_run_failed", agent_id=self.agent.id)
            yield {"type": "error", "message": str(e)[:300]}

        latency = int((time.time() - t0) * 1000)
        if suspended:
            # 挂起：等待管理员确认，由 /tool-confirm 续跑，此处不再落库
            await self.db.commit()
            return
        # 引用转 dict（Pydantic 对象不能直接进 JSON 列）
        citations_json = [
            c.model_dump() if hasattr(c, "model_dump") else c for c in citations
        ]
        # usage 归一化（统一契约）
        from app.services.usage_service import normalize_usage

        norm = normalize_usage(usage_total)
        # 收集 AI 产物（供前端下载）
        artifacts = [
            {k: r["data"].get(k) for k in ("file_key", "artifact_id", "name", "mime", "size", "url")}
            for r in tool_records
            if r.get("data") and r["data"].get("artifact_id")
        ]
        # 落助手消息（persist=False 时仅记 UsageLog，不落消息/不改计数）
        asst_id: int | None = None
        if self.persist:
            asst = Message(
                tenant_id=self.ps.tenant_id,
                conversation_id=self.conversation.id,
                role="assistant",
                content=collected_text,
                reasoning=collected_reasoning or None,
                citations=citations_json or None,
                tool_calls=tool_records or None,
                artifacts=artifacts or None,
                usage=norm,
                model=rm.model_name,
                latency_ms=latency,
                created_at=int(time.time() * 1000),
            )
            self.db.add(asst)
            self.conversation.message_count = (self.conversation.message_count or 0) + 2
            asst_id = asst.id
        self.db.add(
            UsageLog(
                tenant_id=self.ps.tenant_id,
                user_id=self.ps.user_id,
                conversation_id=self.conversation.id if self.conversation else None,
                model_config_id=rm.config_id,
                purpose="chat",
                prompt_tokens=norm["prompt_tokens"],
                completion_tokens=norm["completion_tokens"],
                total_tokens=norm["total_tokens"],
                cached_tokens=norm["cached_tokens"],
                latency_ms=latency,
                success=True,
                created_at=int(time.time() * 1000),
            )
        )
        await self.db.commit()
        await self._maybe_schedule_compress()

        # used_ids：答案真正标注了哪几个 [n]，前端据此区分「已引用」与「仅检索到」。
        # 与问答页口径一致（见 chat_service._citation_usage）。
        used_ids: list[int] = []
        try:
            from app.services.citation_check import check as _cite_used

            used_ids = [
                i for i in (_cite_used(collected_text, citations_json).get("used_ids") or [])
                if 1 <= i <= len(citations_json)
            ]
        except Exception:  # noqa: BLE001
            pass
        yield {"type": "citations", "citations": citations_json, "used_ids": used_ids}
        # 引用真实性校验（与问答页一致）：答案若出现不存在的 [n] 编号则告警
        try:
            from app.services.citation_check import check as _cite_check

            cc = _cite_check(collected_text, citations_json)
            if not cc.get("ok", True):
                yield {"type": "citation_check", "warning": "答案包含可能不实的引用编号", **cc}
        except Exception:  # noqa: BLE001
            pass
        yield {"type": "usage", "usage": norm, "latency_ms": latency, "turns": turns}
        yield {"type": "done", "message_id": asst_id}

    async def _maybe_schedule_compress(self) -> None:
        """落库后异步触发上下文压缩（不阻塞响应）。仅持久化会话需要。

        不再用 self.history 预判阈值：它是裁到最近 N 条的窗口，长度恒定，
        判不出整段会话是否超限（旧实现因此永不触发）。阈值判断交给
        maybe_compress 在独立 session 里读全量历史完成。
        submit 是协程，必须 await，否则协程不会执行。
        """
        if not self.persist or self.conversation is None:
            return
        try:
            from app.services.context_service import _compress_job
            from app.tasks.queue import submit

            cid = self.conversation.id
            await submit(f"compress:{cid}", lambda: _compress_job(cid))
        except Exception:  # noqa: BLE001
            logger.exception("schedule_compress_failed")

    def _audit_tool(self, tc: ToolCall, result: ToolResult, denied: bool = False,
                    autonomy: str = "read") -> None:
        """记录 AI 工具调用审计（进审计表，随业务 session 提交）。

        - autonomy：read=只读 / auto=写操作**自动执行**（未经人工确认）/ confirmed=人工确认后执行 / denied=无权限。
        - sensitive：敏感操作（改数据/对外/高危）→ 审计显著标记，便于在日志页过滤查看。
        """
        from app.middleware.request_id import actor_type_ctx

        try:
            from app.agents.tools.base import is_sensitive, tool_risk_level
            from app.agents.tools.registry import registry

            tool = registry.get_admin(tc.name) or registry.get_builtin(tc.name)
            sensitive = bool(tool and is_sensitive(tool))
            risk = tool_risk_level(tool) if tool else 0
            record_audit(
                self.db,
                action="agent.tool_call",
                resource_type="tool",
                resource_id=tc.name,
                after={
                    "arguments": tc.arguments[:1000],
                    "is_error": result.is_error,
                    "latency_ms": result.latency_ms,
                    "conversation_id": self.conversation.id if self.conversation else None,
                    "sensitive": sensitive,
                    "risk_level": risk,
                    "autonomy": autonomy,
                },
                result="failure" if (denied or result.is_error) else "success",
                error="permission_denied" if denied else None,
                tenant_id=self.ps.tenant_id,
                actor_id=self.ps.user_id,
                actor_type=actor_type_ctx.get() or "user",
            )
        except Exception:  # noqa: BLE001
            logger.exception("tool_audit_failed", tool=tc.name)

    async def _create_pending_action(self, tool, tc: ToolCall, args: dict):
        """为 write 工具创建待确认动作。"""
        import uuid

        from app.models import AgentAction

        summary = ""
        if hasattr(tool, "summarize"):
            try:
                summary = tool.summarize(args)
            except Exception:  # noqa: BLE001
                summary = f"将调用 {tool.name}"
        action = AgentAction(
            tenant_id=self.ps.tenant_id,
            conversation_id=self.conversation.id if self.conversation else None,
            agent_id=self.agent.id,
            user_id=self.ps.user_id,
            tool_name=tool.name,
            tool_kind="write",
            arguments=args,
            raw_tool_call={
                "id": tc.id, "name": tc.name, "arguments": tc.arguments,
                "mode_id": self.mode_id,  # 续跑时还原 mode 级配置
            },
            summary=summary,
            status="pending",
            idempotency_key=uuid.uuid4().hex,
            expires_at=int(time.time() * 1000) + 10 * 60 * 1000,
        )
        self.db.add(action)
        await self.db.commit()
        return action

    async def resume_action(self, action, tool, args: dict, tool_result_text: str) -> AsyncIterator[dict]:
        """管理员确认后：把工具结果回灌 LLM，续跑剩余轮次。

        messages 跨 SSE 需重建：[system] + history + [assistant(tool_calls)] + [tool(result)]。
        mode 还原：从 action.raw_tool_call.mode_id 取回本次运行所用的行为模式，
        否则续跑会丢失 mode 级的 system_prompt / kb 范围 / tool_config 覆盖。
        """
        raw = action.raw_tool_call or {}
        restore_mode_id = raw.get("mode_id") or self.mode_id
        mode = None
        if restore_mode_id:
            mode = await self.db.get(AgentMode, restore_mode_id)
        elif self.agent.default_mode_id:
            mode = await self.db.get(AgentMode, self.agent.default_mode_id)
        self.mode_id = mode.id if mode else None
        eff = await build_effective(self.db, self.agent, mode)
        tools = await resolve_tools(
            self.db, tool_config=eff.tool_config, skill_ids=eff.skill_ids,
            tenant_id=self.ps.tenant_id, perms=self.perms,
            is_external=bool(getattr(self.ps, "is_external", False)),
        )
        llm, rm = await get_llm(self.db, tenant_id=self.ps.tenant_id,
                                config_id=self.model_override or eff.model_config_id)

        messages: list[ChatMessage] = []
        if eff.system_prompt:
            messages.append(ChatMessage(role="system", content=eff.system_prompt))
        if self._inject_caps(eff):
            messages.append(ChatMessage(role="system", content=_CITATION_RULE))
        if self.summary:
            messages.append(ChatMessage(role="system", content=f"[历史对话摘要]\n{self.summary}"))
        messages.extend(self.history)
        messages.append(
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id=raw.get("id", "call_0"), name=raw.get("name", tool.name), arguments=raw.get("arguments", "{}"))],
            )
        )
        messages.append(
            ChatMessage(role="tool", content=tool_result_text, tool_call_id=raw.get("id", "call_0"), name=tool.name)
        )

        collected_text = ""
        collected_reasoning = ""
        tool_records: list[dict] = []
        usage_total: dict = {}
        turns = 0
        t0 = time.time()
        try:
            async for evt in self._react_loop(llm, rm, messages, tools, eff, tool_records):
                if evt["type"] == "delta":
                    collected_text += evt["text"]
                elif evt["type"] == "reasoning":
                    collected_reasoning += evt["text"]
                elif evt["type"] == "usage":
                    usage_total.update(evt.get("usage") or {})
                elif evt["type"] == "turn":
                    turns = evt["turn"]
                    continue
                elif evt["type"] == "_suspended":
                    await self.db.commit()
                    return
                yield evt
        except Exception as e:  # noqa: BLE001
            logger.exception("agent_resume_failed", agent_id=self.agent.id)
            yield {"type": "error", "message": str(e)[:300]}

        latency = int((time.time() - t0) * 1000)
        from app.services.usage_service import normalize_usage as _norm

        norm = _norm(usage_total)
        artifacts = [
            {k: r["data"].get(k) for k in ("file_key", "artifact_id", "name", "mime", "size", "url")}
            for r in tool_records
            if r.get("data") and r["data"].get("artifact_id")
        ]
        asst_id: int | None = None
        if self.persist:
            asst = Message(
                tenant_id=self.ps.tenant_id,
                conversation_id=self.conversation.id,
                role="assistant",
                content=collected_text,
                reasoning=collected_reasoning or None,
                tool_calls=tool_records or None,
                artifacts=artifacts or None,
                usage=norm,
                model=rm.model_name,
                latency_ms=latency,
                created_at=int(time.time() * 1000),
            )
            self.db.add(asst)
            self.conversation.message_count = (self.conversation.message_count or 0) + 2
            asst_id = asst.id
        await self.db.commit()
        await self._maybe_schedule_compress()
        yield {"type": "usage", "usage": norm, "latency_ms": latency, "turns": turns}
        # 上下文占用快照：智能体页输入框的圆环与问答页共用同一口径
        if self.conversation is not None:
            try:
                from app.services.context_service import context_stats

                yield {"type": "context", **(await context_stats(self.db, self.conversation))}
            except Exception:  # noqa: BLE001
                logger.exception("context_stats_failed")
        yield {"type": "done", "message_id": asst_id}

    async def _single_turn(self, llm, rm, messages, eff: EffectiveConfig) -> AsyncIterator[dict]:
        """无工具：单次流式生成。"""
        temp = float(eff.params.get("temperature", 0.3))
        stream = await llm.chat(messages, model=rm.model_name, stream=True, temperature=temp)
        async for chunk in stream:
            if chunk.reasoning:
                yield {"type": "reasoning", "text": chunk.reasoning}
            if chunk.delta:
                yield {"type": "delta", "text": chunk.delta}
            if chunk.usage:
                yield {"type": "usage", "usage": chunk.usage}
            if chunk.finish:
                break

    async def _react_loop(
        self, llm, rm, messages, tools, eff: EffectiveConfig, tool_records: list[dict]
    ) -> AsyncIterator[dict]:
        """委托给共享的 react.run_react_loop（HITL/审计通过回调接入）。"""
        from app.agents.react import run_react_loop

        async def _audit(tc, result, denied: bool, autonomy: str = "read"):
            self._audit_tool(tc, result, denied=denied, autonomy=autonomy)

        async def _pending(tool, tc, args):
            tool_records.append({
                "id": tc.id, "name": tc.name, "arguments": tc.arguments,
                "result": "（待确认，未执行）", "is_error": False, "data": None,
            })
            return await self._create_pending_action(tool, tc, args)

        max_turns = int(getattr(eff, "max_turns", 6) or 6)
        temp = float(eff.params.get("temperature", 0.3))
        # persist=False（定时任务）或 allow_auto_write（渠道绑定内部账号）：
        # 无人值守确认环节，写工具直接执行，不走 HITL 挂起。
        pending = _pending if (self.persist and not self.allow_auto_write) else None
        async for evt in run_react_loop(
            db=self.db, ps=self.ps, perms=self.perms, llm=llm, rm=rm,
            messages=messages, tools=tools,
            conversation_id=self.conversation.id if self.conversation else None,
            agent_id=self.agent.id, room_id=self.room_id, tool_config=eff.tool_config,
            temperature=temp, max_turns=max_turns,
            allow_auto_write=(not self.persist) or self.allow_auto_write,
            on_audit=_audit, on_pending_action=pending,
        ):
            if evt.get("type") == "tool_result":
                tool_records.append({
                    "id": evt.get("id"), "name": evt.get("name"), "arguments": "",
                    "result": (evt.get("content") or "")[:2000],
                    "is_error": evt.get("is_error"),
                    "data": evt.get("data") if isinstance(evt.get("data"), dict) else None,
                })
            yield evt

async def load_history(db: AsyncSession, conversation_id: int, limit: int = 6) -> list[ChatMessage]:
    rows = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.role.in_(("user", "assistant")))
            .order_by(Message.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [ChatMessage(role=m.role, content=m.content) for m in reversed(rows) if m.content]
