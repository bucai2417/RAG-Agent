"""首次运行初始化：默认租户、管理员、默认 Provider 与模型配置、权限角色。

被 scripts/seed.py 与桌面启动入口 run_server.py 复用（幂等）。
放在 app 包内以确保 PyInstaller 能收集。
"""
from __future__ import annotations

from sqlalchemy import select

from app.core.config import settings
from app.core.db import AsyncSessionLocal, init_models
from app.core.security import hash_password
from app.models import ModelConfig, ModelProvider, Tenant, User


class SchemaMigrationError(RuntimeError):
    """schema 迁移失败：此时继续启动只会把问题变成满屏 500，应当直接中止。"""


async def seed_all() -> None:
    """幂等播种。已存在的数据跳过。

    迁移失败抛 SchemaMigrationError（调用方应中止启动）；
    其余播种失败（如某条内置数据写不进去）只影响功能完整性，不阻止启动。
    """
    try:
        await init_models()  # 走 alembic 迁移到最新 schema（见 app/core/migrations.py）
    except Exception as e:  # noqa: BLE001
        raise SchemaMigrationError(f"数据库迁移失败：{e}") from e
    async with AsyncSessionLocal() as db:
        tenant = (
            await db.execute(select(Tenant).where(Tenant.slug == "default"))
        ).scalar_one_or_none()
        if not tenant:
            tenant = Tenant(name="默认组织", slug="default", plan="default")
            db.add(tenant)
            await db.flush()

        admin = (
            await db.execute(
                select(User).where(User.tenant_id == tenant.id, User.username == "admin")
            )
        ).scalar_one_or_none()
        if not admin:
            admin = User(
                tenant_id=tenant.id,
                username="admin",
                display_name="系统管理员",
                email="admin@example.com",
                password_hash=hash_password("admin123"),
                is_admin=True,
            )
            db.add(admin)
            await db.flush()

        has_provider = (await db.execute(select(ModelProvider))).scalars().first()
        if not has_provider and (
            settings.default_llm_api_key or settings.default_embedding_api_key
        ):
            base = settings.default_llm_base_url or settings.default_embedding_base_url
            key = settings.default_llm_api_key or settings.default_embedding_api_key
            provider = ModelProvider(
                tenant_id=tenant.id, name="默认云端 Provider", kind="openai",
                base_url=base, api_key=key,
            )
            db.add(provider)
            await db.flush()
            if settings.default_llm_model:
                db.add(ModelConfig(
                    tenant_id=tenant.id, provider_id=provider.id, purpose="chat",
                    model_name=settings.default_llm_model, display_name=settings.default_llm_model,
                    is_default=True, priority=10,
                ))
            if settings.default_embedding_model:
                db.add(ModelConfig(
                    tenant_id=tenant.id, provider_id=provider.id, purpose="embedding",
                    model_name=settings.default_embedding_model, display_name=settings.default_embedding_model,
                    embedding_dim=settings.embedding_dim, is_default=True, priority=10,
                ))

        from app.services.permission_seed import (
            backfill_admin_super,
            backfill_viewer_role,
            seed_permissions_and_roles,
        )

        role_ids = await seed_permissions_and_roles(db)
        await backfill_admin_super(db, tenant.id, role_ids)
        await backfill_viewer_role(db, tenant.id, role_ids)
        await _backfill_external_users(db)
        await _seed_builtin_agents(db, tenant.id, admin.id)
        await _seed_builtin_persona_templates(db, tenant.id, admin.id)

        # 叠加 Web 端系统设置的覆盖值（启动早期，确保端口等启动期项生效）
        from app.services.settings_service import apply_overrides

        await apply_overrides(db)

        await db.commit()


async def _seed_builtin_agents(db, tenant_id: int, admin_id: int) -> None:
    """播种平台内置智能体（幂等，按 slug 去重）。

    约定：内置体**不预置 model_config_id**——对话模型由使用者在界面自行选择，
    避免种子阶段把某个尚未配置的模型写死；工具默认全开，使用者可按需关闭。
    """
    from app.models import Agent

    builtins = [
        {
            "slug": "group-manager",
            "name": "QQ管理助手",
            "description": "企业级群管机器人：被 @ 时可执行禁言、移人、设管理员、发公告、置顶/撤回、搜消息、查统计、创建定时任务（日报周报月报、夜间静默）。需先设为群管理员才具备管理权限。",
            "system_prompt": (
                "你是本平台的企业群管助手。被 @ 时按用户意图行动：\n"
                "1) 要「看效果/要一段代码」→ 直接输出 ```html 代码块（前端可预览，零副作用），不要调用 generate_file；\n"
                "2) 明确要「文件/下载/导出」→ 才调用 generate_file 生成；\n"
                "3) 群管理操作（禁言/移人/设管理员/公告/置顶/撤回/搜消息/统计/定时任务）→ 调用对应的 room_* 工具；若无权限，说明需要先把你设为群管理员。\n"
                "回答用 Markdown，引用资料标注 [n]，不编造引用编号。"
            ),
            "type": "agent",
            "status": "published",
            "visibility": "tenant",
            "is_builtin": True,
            "tool_config": {"max_turns": 8},
            "config": {"temperature": 0.3},
        },
    ]
    for spec in builtins:
        exist = (
            await db.execute(
                select(Agent).where(Agent.tenant_id == tenant_id, Agent.slug == spec["slug"])
            )
        ).scalar_one_or_none()
        if exist:
            # 已存在则仅补齐内置标记（兼容历史数据），避免覆盖使用者的自定义改动
            if not exist.is_builtin:
                exist.is_builtin = True
                await db.flush()
            continue
        db.add(Agent(
            tenant_id=tenant_id, owner_id=admin_id, name=spec["name"], slug=spec["slug"],
            description=spec["description"], type=spec.get("type", "agent"),
            system_prompt=spec.get("system_prompt", ""),
            model_config_id=spec.get("model_config_id"),  # 内置体不预置模型
            tool_config=spec.get("tool_config"),
            config=spec.get("config"),
            status=spec.get("status", "published"),
            visibility=spec.get("visibility", "tenant"),
            is_builtin=True,
        ))
        await db.flush()


async def _seed_builtin_persona_templates(db, tenant_id: int, admin_id: int) -> None:
    """播种内置人设模板（幂等，按 slug 去重）。

    这些模板是「怎么写好 system_prompt」的范例：每套都写全
    角色 / 语气 / 边界 / 输出格式 四要素 —— 用户不擅长写提示词，
    模板替他把结构摆好，套用后按需微调。

    语义与 _seed_builtin_agents 一致：已存在则**只补 is_builtin 标记，
    不覆盖**用户对模板的编辑（内置模板允许编辑，仅禁止删除）。
    """
    from app.models import AgentPersonaTemplate

    templates = [
        {
            "slug": "customer-service",
            "name": "客服助手",
            "category": "客服",
            "icon": "Headset",
            "description": "礼貌耐心、先共情再解决；不臆造政策，无法确定时引导转人工",
            "system_prompt": (
                "你是本公司的客服助手。\n"
                "语气：礼貌、简洁、有同理心；先共情，再解决。\n"
                "职责：理解客户诉求 → 给出明确答复或下一步；无法确定时，先给可执行的做法，"
                "再说明需要人工介入的原因。\n"
                "边界：不臆造产品政策、价格、库存、时效等事实；没有依据时明确说「我需要为您核实」，"
                "并引导用户提供订单号或联系方式；不承诺平台无法保证的事。\n"
                "输出：先一句话结论，再分点给步骤（最多 5 步）；涉及操作时用「动作 + 预期结果」；"
                "结尾一句友好收束（不超过 15 字）。"
            ),
            "sort": 10,
        },
        {
            "slug": "translator",
            "name": "翻译助手",
            "category": "通用",
            "icon": "Connection",
            "description": "忠实通顺、保留术语；不增删事实，歧义处给提示",
            "system_prompt": (
                "你是专业翻译助手，中英互译（或用户指定的语种）。\n"
                "规则：忠实、通顺、符合目标语言习惯；保留原文的专业术语、数字、单位与专有名词。\n"
                "语气：与原文意图一致 —— 正式文本用正式语体，口语保持自然口语。\n"
                "边界：不增删事实、不做主观评价；遇到歧义时给出译文并附一句「歧义提示」；"
                "人名/品牌/法规名无法确定译法时保留原文并标注原文。\n"
                "输出：默认只输出译文；用户要求时，另起「术语对照」列表（原文 → 译文）。"
            ),
            "sort": 20,
        },
        {
            "slug": "code-reviewer",
            "name": "代码审查",
            "category": "研发",
            "icon": "DocumentChecked",
            "description": "按严重级别分级指出问题，每条给原因与改法；就事论事",
            "system_prompt": (
                "你是严谨的高级工程师，负责代码审查（Code Review）。\n"
                "关注顺序（优先级从高到低）：正确性 > 安全 > 边界与错误处理 > 可读性 > 性能 > 风格。\n"
                "语气：就事论事、建设性；指出问题同时给改进建议，不使用模糊评价（如「写得不好」）。\n"
                "边界：只基于所给代码判断，不臆测未提供的上下文；不确定处标注「需确认」。\n"
                "输出：按严重级别分组 ——【阻断】【重要】【建议】；每条含：位置、问题、原因、修改建议"
                "（必要时给示例代码）。若没有阻断或重要问题，明确说明「可合并」。"
            ),
            "sort": 30,
        },
        {
            "slug": "data-analyst",
            "name": "数据分析",
            "category": "通用",
            "icon": "DataAnalysis",
            "description": "说清口径与假设；不编造数据，区分相关与因果",
            "system_prompt": (
                "你是数据分析师。\n"
                "职责：理解分析目标 → 说明口径与假设 → 给出结论与依据 → 提示局限。\n"
                "语气：客观、量化、可复现。\n"
                "边界：不编造数据；缺少数据时说明需要哪些字段并给出分析方案；"
                "严格区分「相关」与「因果」；对样本量与偏差保持警惕。\n"
                "输出：先给「结论」（2-3 条要点），再给「依据」（引用具体数字/字段），"
                "最后给「建议下一步」；需要表格时用 Markdown 表格。"
            ),
            "sort": 40,
        },
        {
            "slug": "writing-polish",
            "name": "写作润色",
            "category": "写作",
            "icon": "Edit",
            "description": "不改原意、去冗余病句；先给全文再给修改说明",
            "system_prompt": (
                "你是中文写作与润色助手。\n"
                "目标：在不改变原意与事实的前提下，让文字更通顺、准确、有节奏；去除冗余、口语碎片与病句。\n"
                "语气：与原文文体一致（公文的端庄、营销文案的感染力、技术文档的准确）。\n"
                "边界：不添加原文没有的事实或数据；不夸大；保留作者的个人语气与专有名词。\n"
                "输出：先输出「润色后全文」，再附「修改说明」（分点列出关键改动及原因）。"
                "用户说「只改错」时，只修错别字/标点/病句，不动结构与措辞。"
            ),
            "sort": 50,
        },
        {
            "slug": "kb-qa",
            "name": "知识问答",
            "category": "通用",
            "icon": "Collection",
            "description": "仅依据知识库作答并标注 [n]；无依据时明说，不编造",
            "system_prompt": (
                "你是知识库问答助手。\n"
                "规则：仅依据检索到的知识库内容作答；每条关键结论标注引用编号 [n]，编号与所给资料一一对应。\n"
                "语气：简洁、直接、可信。\n"
                "边界：资料中没有答案时，明确回答「知识库中未找到相关内容」并给出检索建议，"
                "绝不编造事实或引用编号；区分「资料明确说明」与「据此推断」。\n"
                "输出：先一句话给出答案；需要展开时分点，并在句末标注 [n]。"
            ),
            "sort": 60,
        },
        {
            "slug": "meeting-minutes",
            "name": "会议纪要",
            "category": "写作",
            "icon": "Calendar",
            "description": "结构化提炼结论/待办/风险；待办含负责人与截止",
            "system_prompt": (
                "你是会议纪要助手。\n"
                "语气：客观、书面、无冗余。\n"
                "职责：从对话或录音转写中提炼结构化纪要，把「结论/待办」与「讨论过程」分开，"
                "待办需明确「事项 + 负责人 + 截止时间」。\n"
                "边界：忠于原意，不添加未讨论的内容；不臆测发言人意图；信息缺失处标注「待定」"
                "而非自行填补；不把讨论过程写成结论。\n"
                "输出：\n"
                "## 会议主题\n"
                "## 结论（分点）\n"
                "## 待办事项（表格：事项 | 负责人 | 截止时间 | 状态）\n"
                "## 风险与待决事项"
            ),
            "sort": 70,
        },
    ]

    for spec in templates:
        exist = (
            await db.execute(
                select(AgentPersonaTemplate).where(
                    AgentPersonaTemplate.tenant_id == tenant_id,
                    AgentPersonaTemplate.slug == spec["slug"],
                )
            )
        ).scalar_one_or_none()
        if exist:
            # 已存在：仅补内置标记，不覆盖用户编辑（内置模板允许编辑）
            if not exist.is_builtin:
                exist.is_builtin = True
                await db.flush()
            continue
        db.add(AgentPersonaTemplate(
            tenant_id=tenant_id,
            created_by=admin_id,
            name=spec["name"],
            slug=spec["slug"],
            description=spec["description"],
            system_prompt=spec["system_prompt"],
            category=spec.get("category", ""),
            icon=spec.get("icon"),
            sort=spec.get("sort", 0),
            enabled=True,
            is_builtin=True,
        ))
        await db.flush()


async def _backfill_external_users(db) -> None:
    """存量渠道虚拟用户回填 user_type=external（幂等）。

    旧版本把外部客户建成普通 User，username 形如 {kind}_{external_id}；
    另有大量历史账号 user_type 为 NULL（该列后加），统一回填为 internal。
    """
    from sqlalchemy import or_
    from sqlalchemy import update as _upd

    for kind in ("qqbot", "wxclaw", "wework", "feishu"):
        await db.execute(
            _upd(User).where(
                User.username.like(f"{kind}_%"),
                or_(User.user_type.is_(None), User.user_type != "external"),
            ).values(user_type="external")
        )
    # 剩余 NULL → internal（避免 SQL 中 != 'external' 对 NULL 求值为假而漏掉账号）
    await db.execute(
        _upd(User).where(User.user_type.is_(None)).values(user_type="internal")
    )
    await db.flush()
