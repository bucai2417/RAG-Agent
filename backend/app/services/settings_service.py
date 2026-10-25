"""系统设置服务：字段元数据、启动叠加、读取、更新、重启。

设计：
- FIELD_META 描述每个可配置字段（标签/分组/类型/是否需重启/是否敏感）。
- apply_overrides(db)：启动时把 system_setting 表的值叠加到 settings 单例。
- update_config(db, updates)：校验 → 类型转换 → setattr（热生效）→ 落库。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.models import SystemSetting

logger = get_logger("settings")

# 分组顺序（前端 Tabs 顺序）
GROUPS: list[tuple[str, str]] = [
    ("app", "应用"),
    ("retrieval", "检索与重排"),
    ("security", "内容安全"),
    ("connector", "外部知识库连接器"),
    ("mcp", "MCP"),
    ("scheduler", "定时任务"),
    ("log", "日志"),
    ("storage", "存储"),
    ("provider", "默认模型"),
    ("advanced", "高级（谨慎修改）"),
]

# key -> 元数据。kind: bool|int|float|str|csv|secret|json
FIELD_META: dict[str, dict[str, Any]] = {
    # ---- 应用 ----
    "app_name": {"label": "应用名称", "group": "app", "kind": "str"},
    "app_env": {"label": "运行环境", "group": "app", "kind": "str", "restart": True,
                "help": "dev / prod"},
    "debug": {"label": "调试模式", "group": "app", "kind": "bool", "restart": True},
    "port": {"label": "服务端口", "group": "app", "kind": "int", "restart": True,
             "help": "修改后需重启；重启后请用新端口访问"},
    "api_prefix": {"label": "API 前缀", "group": "app", "kind": "str", "restart": True},
    # ---- 检索与重排 ----
    "retrieval_top_k": {"label": "默认返回条数 top_k", "group": "retrieval", "kind": "int"},
    "retrieval_candidate_k": {"label": "召回候选数 candidate_k", "group": "retrieval", "kind": "int"},
    "retrieval_vec_min": {"label": "向量召回阈值", "group": "retrieval", "kind": "float",
                          "help": "0 = 不过滤"},
    "retrieval_bm25_min": {"label": "BM25 召回阈值", "group": "retrieval", "kind": "float",
                           "help": "0 = 不过滤"},
    "retrieval_lexical_guard": {
        "label": "词面依据复核（丢弃无依据的低分命中）", "group": "retrieval", "kind": "bool",
        "help": "向量余弦不为 0 只说明共享了几个字；开启后，低分且查询词在片段里找不到"
                "依据的命中会被丢弃，避免无关文档被当成参考资料展示。"
                "关闭后按原始召回结果返回（检索调试页可临时关掉看全量候选）",
    },
    "retrieval_vec_guard_floor": {
        "label": "复核豁免分数线", "group": "retrieval", "kind": "float",
        "help": "原始相似度高于该值的命中不做词面复核，直接保留（保护「换了个说法」的语义命中）。"
                "0 = 不豁免，全部按词面判断；强语义模型建议 0.3~0.5",
    },
    "rerank_enabled": {"label": "启用重排", "group": "retrieval", "kind": "bool"},
    "rerank_pool": {"label": "重排候选数", "group": "retrieval", "kind": "int"},
    "rerank_allow_fake": {"label": "允许假重排（local_hash）", "group": "retrieval", "kind": "bool",
                          "help": "仅自测用"},
    "embedding_fallback_local": {"label": "向量失败回退本地", "group": "retrieval", "kind": "bool"},
    "inject_capabilities_default": {"label": "默认注入平台能力摘要给 AI", "group": "retrieval", "kind": "bool"},
    "query_rewrite_enabled": {"label": "多轮追问查询改写", "group": "retrieval", "kind": "bool",
                              "help": "结合历史把「它呢？」等追问改写成独立问题再检索（耗一次 LLM 调用）"},
    "mmr_enabled": {"label": "检索结果去冗余（MMR）", "group": "retrieval", "kind": "bool"},
    "mmr_lambda": {"label": "MMR 相关性权重", "group": "retrieval", "kind": "float",
                   "help": "0-1，越大越偏相关、越小越偏多样"},
    "rrf_k": {"label": "RRF 常数 k", "group": "retrieval", "kind": "int"},
    "rrf_weight_vector": {"label": "融合权重·向量", "group": "retrieval", "kind": "float"},
    "rrf_weight_bm25": {"label": "融合权重·关键词", "group": "retrieval", "kind": "float"},
    "rrf_weight_external": {"label": "融合权重·外部源", "group": "retrieval", "kind": "float"},
    "github_token": {"label": "GitHub API Token（技能搜索用）", "group": "retrieval", "kind": "secret",
                     "help": "配置后技能搜索配额由 10 次/分钟升至 30 次/分钟；只读公开仓库无需勾选任何 scope"},
    "vector_max_scan": {"label": "向量扫描上限（0=不限）", "group": "retrieval", "kind": "int"},
    # ---- 上下文压缩 ----
    "context_window_tokens": {
        "label": "模型上下文窗口（token）", "group": "retrieval", "kind": "int",
        "help": "该会话所使用模型的上下文窗口。0 = 按模型名自动推断"
                "（gpt-4o 128k、deepseek 64k、qwen 32k、本地小模型 8k…）。"
                "压缩阈值由它推导，换模型不必再手改阈值",
    },
    "context_window_ratio": {
        "label": "压缩阈值占窗口比例", "group": "retrieval", "kind": "float",
        "help": "默认 0.7，余量留给输出、检索资料与工具定义。调大能更充分地利用窗口，"
                "但过大会让单轮请求贴近上限（0.3~0.95）",
    },
    "context_output_reserve_tokens": {
        "label": "输出预留（token）", "group": "retrieval", "kind": "int",
        "help": "从阈值里扣掉给模型输出的空间，默认 4096；超过窗口 25% 时按 25% 计",
    },
    "context_compress_threshold": {
        "label": "上下文压缩阈值（token，0=自动）", "group": "retrieval", "kind": "int",
        "help": "会话上下文超过该值就把早期对话摘要成一段，只保留最近若干条原文。"
                "0 = 自动（窗口 × 比例 − 输出预留）；填正数则固定使用该值。"
                "问答页输入框右侧圆环满格即触发压缩",
    },
    "context_keep_recent": {
        "label": "压缩后至少保留最近消息数", "group": "retrieval", "kind": "int",
        "help": "压缩完成后**至少**以原文保留的消息条数（下限）。"
                "实际保留多少由 token 预算决定——只要预算够，会保留更多轮次。0 = 默认 6",
    },
    # ---- 在线更新 ----
    "update_check_enabled": {"label": "允许检查更新", "group": "advanced", "kind": "bool",
                             "help": "关闭后「关于/更新」不再请求 GitHub Release"},
    "update_repo": {"label": "更新来源仓库（owner/repo）", "group": "advanced", "kind": "str",
                    "help": "留空使用内置默认；私有仓库需先配置 GitHub Token"},
    # ---- 内容安全 ----
    "security_guard_enabled": {"label": "启用内容安全检测", "group": "security", "kind": "bool"},
    "security_guard_block_threshold": {"label": "拦截阈值", "group": "security", "kind": "int"},
    "security_guard_flag_threshold": {"label": "标记阈值", "group": "security", "kind": "int"},
    "security_guard_wrap_context": {"label": "给检索内容加边界标记", "group": "security", "kind": "bool"},
    "security_guard_sensitive_words": {"label": "敏感词（逗号分隔）", "group": "security", "kind": "str"},
    # ---- 账号与登录安全 ----
    "registration_enabled": {"label": "开放自助注册", "group": "security", "kind": "bool",
                             "help": "开启后登录页出现「注册」入口；新账号需管理员审核才能登录。默认关闭"},
    "require_admin_approval": {"label": "注册后需管理员审核", "group": "security", "kind": "bool",
                               "help": "关闭则注册后可直接登录，建议保持开启"},
    "login_rate_per_min": {"label": "登录限流（次/分钟/IP）", "group": "security", "kind": "int",
                           "help": "单 IP 每分钟登录尝试上限，0=关闭。防暴力破解与撞库"},
    "login_max_failures": {"label": "连续失败锁定阈值", "group": "security", "kind": "int",
                           "help": "同一账号连续失败多少次后锁定，0=不锁定"},
    "login_lock_minutes": {"label": "锁定时长（分钟）", "group": "security", "kind": "int"},
    # ---- 连接器 ----
    "connector_allow_private": {"label": "允许连接内网/环回", "group": "connector", "kind": "bool",
                                "help": "企业内网部署外部知识库时开启"},
    "connector_default_timeout": {"label": "出站超时（秒）", "group": "connector", "kind": "int"},
    "connector_max_top_k": {"label": "外部源召回上限", "group": "connector", "kind": "int"},
    "webhook_allow_private_callback": {
        "label": "允许工作流回调内网地址", "group": "connector", "kind": "bool",
        "help": "企业自建客服台/ERP 部署在内网时开启。只影响 Webhook 回调，不影响知识库连接器",
    },
    "workflow_run_timeout_seconds": {
        "label": "工作流单次运行超时（秒）", "group": "advanced", "kind": "int",
        "help": "整条链的执行预算，超时中止。0 或留空用默认 600 秒；单个智能体可用 config.run_timeout_seconds 覆盖",
    },
    # ---- MCP ----
    "mcp_allow_stdio": {"label": "允许本地 stdio MCP", "group": "mcp", "kind": "bool",
                        "help": "开启后会执行本地命令，注意安全"},
    "mcp_stdio_allowed_cmds": {"label": "stdio 命令白名单", "group": "mcp", "kind": "str",
                               "help": "逗号分隔（首 token）"},
    "mcp_default_timeout": {"label": "MCP 调用超时（秒）", "group": "mcp", "kind": "int"},
    "mcp_stdio_idle_seconds": {"label": "stdio 空闲回收（秒）", "group": "mcp", "kind": "int"},
    # ---- 邮件入库 ----
    "email_poll_interval_seconds": {"label": "邮件源轮询周期（秒）", "group": "mcp", "kind": "int",
                                    "restart": True, "help": "0 = 关闭；默认 300" },
    # ---- 客服 ----
    "service_notify_on_reply": {"label": "客户回复时通知客服", "group": "scheduler", "kind": "bool",
                                "help": "全局默认；单个工单可在详情里用「提醒开关」覆盖" },
    # ---- 定时任务 ----
    "scheduler_max_concurrency": {"label": "并发上限", "group": "scheduler", "kind": "int"},
    "scheduler_max_retries_cap": {"label": "重试次数硬上限", "group": "scheduler", "kind": "int"},
    "scheduler_history_keep": {"label": "每任务保留历史条数", "group": "scheduler", "kind": "int"},
    "provider_health_interval_min": {"label": "Provider 健康检查周期（分钟）", "group": "scheduler",
                                     "kind": "int", "restart": True, "help": "0 = 关闭"},
    # ---- 日志 ----
    "log_file": {"label": "日志文件路径", "group": "log", "kind": "str", "restart": True},
    "log_max_bytes": {"label": "单文件大小上限（字节）", "group": "log", "kind": "int", "restart": True},
    "log_backup_count": {"label": "轮转备份数", "group": "log", "kind": "int", "restart": True},
    "log_retention_days": {"label": "备份保留天数", "group": "log", "kind": "int"},
    "log_cleanup_interval_hours": {"label": "自动清理周期（小时）", "group": "log", "kind": "int",
                                   "restart": True, "help": "0 = 关闭"},
    # ---- 存储 ----
    "storage_backend": {"label": "存储后端", "group": "storage", "kind": "str", "restart": True},
    "storage_local_dir": {"label": "本地存储目录", "group": "storage", "kind": "str", "restart": True},
    "skill_pack_dir": {"label": "技能包目录", "group": "storage", "kind": "str", "restart": True},
    # ---- 默认模型 ----
    "default_llm_base_url": {"label": "默认对话 Base URL", "group": "provider", "kind": "str"},
    "default_llm_api_key": {"label": "默认对话 API Key", "group": "provider", "kind": "secret"},
    "default_llm_model": {"label": "默认对话模型", "group": "provider", "kind": "str"},
    "default_embedding_base_url": {"label": "默认向量 Base URL", "group": "provider", "kind": "str"},
    "default_embedding_api_key": {"label": "默认向量 API Key", "group": "provider", "kind": "secret"},
    "default_embedding_model": {"label": "默认向量模型", "group": "provider", "kind": "str"},
    # ---- 高级（谨慎）----
    "database_url": {"label": "数据库连接串", "group": "advanced", "kind": "secret", "restart": True,
                     "help": "改动需重启；重启时会自动迁移到最新 schema，失败则回退到原库"},
    "db_pool_size": {"label": "连接池大小", "group": "advanced", "kind": "int", "restart": True,
                     "help": "仅 PostgreSQL/MySQL 生效；多副本时总连接数不要超过数据库 max_connections"},
    "db_max_overflow": {"label": "连接池溢出上限", "group": "advanced", "kind": "int", "restart": True,
                        "help": "池满后允许临时新建的连接数；耗尽后请求等待 pool_timeout 秒"},
    "db_pool_timeout": {"label": "取连接超时（秒）", "group": "advanced", "kind": "int", "restart": True,
                        "help": "等不到空闲连接时返回 503，而不是无限挂起"},
    "db_pool_recycle": {"label": "连接回收（秒）", "group": "advanced", "kind": "int", "restart": True,
                        "help": "防止防火墙/中间件掐断空闲连接导致首批请求失败"},
    "db_busy_timeout": {"label": "SQLite 写锁等待（秒）", "group": "advanced", "kind": "int", "restart": True,
                        "help": "SQLite 单写者；调大可减少 database is locked"},
    "db_auto_migrate": {"label": "启动时自动迁移", "group": "advanced", "kind": "bool", "restart": True,
                        "help": "关闭后需自行执行 alembic upgrade head"},
    "vector_backend": {"label": "向量后端", "group": "advanced", "kind": "str", "restart": True,
                       "help": "auto / pgvector / numpy"},
    "embedding_dim": {"label": "向量维度", "group": "advanced", "kind": "int", "restart": True,
                      "help": "改动会影响向量列，需谨慎"},
    "task_backend": {"label": "任务后端", "group": "advanced", "kind": "str", "restart": True,
                     "help": "memory / celery"},
    "redis_url": {"label": "Redis 地址", "group": "advanced", "kind": "str"},
    "secret_key": {"label": "密钥 secret_key", "group": "advanced", "kind": "secret", "restart": True,
                   "help": "改动会使已加密的凭证/令牌失效，请谨慎"},
    "algorithm": {"label": "JWT 算法", "group": "advanced", "kind": "str", "restart": True},
    "access_token_expire_minutes": {"label": "访问令牌有效期（分钟）", "group": "advanced", "kind": "int"},
    "refresh_token_expire_minutes": {"label": "刷新令牌有效期（分钟）", "group": "advanced", "kind": "int"},
    "file_token_expire_minutes": {"label": "文件签名 URL 有效期（分钟）", "group": "advanced", "kind": "int"},
    "cors_origins": {"label": "CORS 允许来源（逗号分隔）", "group": "advanced", "kind": "str", "restart": True},
}

SECRET_KEYS = {k for k, m in FIELD_META.items() if m.get("kind") == "secret"}


def _coerce(key: str, raw: Any) -> Any:
    """把前端传入的值转成 settings 字段期望的类型。"""
    meta = FIELD_META.get(key)
    if not meta:
        raise ValidationError(f"未知配置项：{key}")
    kind = meta["kind"]
    try:
        if kind == "bool":
            if isinstance(raw, bool):
                return raw
            s = str(raw).strip().lower()
            if s in ("true", "1", "yes", "on", "是"):
                return True
            if s in ("false", "0", "no", "off", "否", ""):
                return False
            raise ValueError("不是布尔值")
        if kind == "int":
            return int(raw)
        if kind == "float":
            return float(raw)
        if kind == "csv":
            if isinstance(raw, list):
                return ",".join(str(x) for x in raw)
            return str(raw)
        if kind == "json":
            if isinstance(raw, (dict, list)):
                return raw
            import json

            return json.loads(raw)
        # str / secret
        return str(raw)
    except (ValueError, TypeError) as e:
        raise ValidationError(f"「{meta['label']}」值非法：{e}") from e


async def apply_overrides(db: AsyncSession) -> int:
    """启动时把 system_setting 的覆盖值叠加到 settings 单例。返回应用条数。"""
    from app.core.config import settings

    rows = (await db.execute(select(SystemSetting))).scalars().all()
    applied = 0
    for row in rows:
        if row.key not in FIELD_META:
            logger.warning("settings_override_unknown", key=row.key)
            continue
        try:
            setattr(settings, row.key, _coerce(row.key, row.value))
            applied += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("settings_override_failed", key=row.key, err=str(e)[:200])
    if applied:
        logger.info("settings_overrides_applied", count=applied)
    return applied


def get_config() -> list[dict]:
    """返回全部可配置项（分组 + 元数据 + 当前值）；secret 值脱敏。"""
    from app.core.config import settings

    # 启动期字段集合：改动需重启的项，若当前值 != .env 默认值意义不大，直接给当前值
    out: list[dict] = []
    for key, meta in FIELD_META.items():
        cur = getattr(settings, key, None)
        is_secret = meta.get("kind") == "secret"
        out.append({
            "key": key,
            "label": meta["label"],
            "group": meta["group"],
            "kind": meta["kind"],
            "restart": bool(meta.get("restart")),
            "help": meta.get("help"),
            "is_set": bool(cur) if is_secret else None,
            "value": ("••••••" if (is_secret and cur) else ("" if is_secret else cur)),
        })
    return out


async def update_config(
    db: AsyncSession, *, user_id: int, updates: dict[str, Any]
) -> dict:
    """校验 → 类型转换 → setattr（热生效）→ upsert 落库。返回 {restart_required, changed}。"""
    from app.core.config import settings

    if not updates:
        raise ValidationError("没有要更新的配置")
    changed: list[str] = []
    restart_required = False
    for key, raw in updates.items():
        if key not in FIELD_META:
            raise ValidationError(f"未知配置项：{key}")
        # secret 字段：前端回显是掩码，未改动则跳过
        if FIELD_META[key]["kind"] == "secret" and isinstance(raw, str) and set(raw) <= {"•"}:
            continue
        val = _coerce(key, raw)
        setattr(settings, key, val)
        if FIELD_META[key].get("restart"):
            restart_required = True
        changed.append(key)
        # upsert
        row = (
            await db.execute(select(SystemSetting).where(SystemSetting.key == key))
        ).scalar_one_or_none()
        if row:
            row.value = val
            row.updated_by = user_id
        else:
            db.add(SystemSetting(key=key, value=val, updated_by=user_id))
    await db.flush()
    return {"restart_required": restart_required, "changed": changed}


def groups() -> list[dict]:
    return [{"key": k, "label": v} for k, v in GROUPS]
