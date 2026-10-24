"""全局配置：从 .env 读取，支持数据库/向量/存储/任务多后端切换。

路径策略（打包友好）：
- 源码运行：BASE_DIR = 项目根目录；数据在 backend/data/
- 冻结运行（PyInstaller）：BASE_DIR = 可执行文件所在目录（绿色版：数据跟随 exe），
  只读资源（前端 dist）在 sys._MEIPASS。
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _base_dir() -> Path:
    """可写根目录：
    - 冻结态：优先环境变量 RAG_DATA_DIR；否则用 %APPDATA%/RAG知识库，
      避免写入只读的安装目录（Program Files）。
    - 源码态：backend 目录。
    """
    if _is_frozen():
        env_dir = os.environ.get("RAG_DATA_DIR")
        if env_dir:
            return Path(env_dir)
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        return Path(appdata) / "RAG知识库"
    return Path(__file__).resolve().parent.parent.parent  # backend/


def _project_root() -> Path:
    """项目根目录（含 web/ 前端）。源码态 = backend 的上级。"""
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent.parent  # 项目根


def _resource_dir() -> Path:
    """只读资源目录：冻结时是 _MEIPASS，否则是项目根（web/dist 所在）。"""
    if _is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return _project_root()


BASE_DIR = _base_dir()          # 可写：数据库、上传文件、.env
RESOURCE_DIR = _resource_dir()  # 只读：前端 dist 等

# 数据统一放 BASE_DIR/data（冻结态默认为 %APPDATA%/RAG知识库/data）
DATA_DIR = BASE_DIR / "data"
# 确保数据目录存在（SQLite 需目录已存在，冻结态尤其关键）
DATA_DIR.mkdir(parents=True, exist_ok=True)

_SECRET_PLACEHOLDER = "change-me-to-a-long-random-string-in-production"
_SECRET_FILE = DATA_DIR / ".secret_key"


def _load_or_create_secret() -> str:
    """获取 JWT 签名密钥。

    优先用环境/配置里的非占位值；否则在 DATA_DIR 生成一个随机密钥并持久化。
    绝不允许用占位默认值签名（否则任何人都能伪造任意用户的 JWT）。
    首次生成后一致持久化，重启不会导致已签发 token 失效。
    """
    env_val = os.environ.get("SECRET_KEY") or os.environ.get("secret_key")
    if env_val and env_val.strip() and env_val.strip() != _SECRET_PLACEHOLDER:
        return env_val.strip()
    try:
        if _SECRET_FILE.exists():
            v = _SECRET_FILE.read_text(encoding="utf-8").strip()
            if v and v != _SECRET_PLACEHOLDER:
                return v
        import secrets as _secrets

        v = _secrets.token_urlsafe(48)
        _SECRET_FILE.write_text(v, encoding="utf-8")
        try:
            os.chmod(_SECRET_FILE, 0o600)
        except OSError:
            pass
        return v
    except OSError:
        # 极端只读环境：退化为进程内随机（重启后 token 失效，但绝不使用占位密钥）
        import secrets as _secrets

        return _secrets.token_urlsafe(48)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )
    # 应用
    app_name: str = "RAG Knowledge Base"
    app_version: str = "1.0.2"
    app_env: str = "prod" if _is_frozen() else "dev"
    debug: bool = not _is_frozen()
    api_prefix: str = "/api/v1"
    port: int = 6677

    # 数据库
    database_url: str = f"sqlite+aiosqlite:///{(DATA_DIR / 'rag.db').as_posix()}"

    # 向量
    vector_backend: str = "auto"  # auto | pgvector | numpy
    embedding_dim: int = 1024
    vector_max_scan: int = 0  # NumpyStore 单次扫描上限（0=不限，防超大库 OOM）

    # 检索
    retrieval_top_k: int = 5
    retrieval_candidate_k: int = 20
    retrieval_vec_min: float = 0.0    # 向量召回前置门（0=不过滤）
    retrieval_bm25_min: float = 0.0   # BM25 召回前置门（0=不过滤）
    # 词面依据复核：低分（< floor）且查询词在片段里找不到依据的命中直接丢弃。
    # 向量余弦不是 0 只说明"共享了几个字"，小库里无关文档很容易占满 top_k，
    # 用户看到的就是回答下挂着几份毫不相关的资料。floor=0 表示只看词面依据、
    # 不设分数豁免，适用于小库或 local_hash 这类弱向量模型。
    retrieval_lexical_guard: bool = True
    retrieval_vec_guard_floor: float = 0.0
    rerank_enabled: bool = False      # 是否启用重排
    rerank_pool: int = 30             # 进重排的候选数
    rerank_allow_fake: bool = False   # 允许 local_hash 假重排（仅自测）
    embedding_fallback_local: bool = True  # 上游 embedding 失败时回退到本地 local_hash（保证入库）
    provider_health_interval_min: int = 30  # Provider 健康检查周期（分钟，0=关闭）
    inject_capabilities_default: bool = True  # 向 AI 注入「平台能力 + 账号权限」摘要
    # 上下文压缩：会话历史超过阈值时把早期对话摘要进 Conversation.summary，只保留最近 N 条原文。
    # 输入框右侧的圆形占用环用的就是这个阈值，改这里环口也跟着变。
    context_compress_threshold: int = 4000   # 触发压缩的 token 阈值（0=用内置默认 4000）
    context_keep_recent: int = 6             # 压缩后仍以原文保留的最近消息条数（0=用内置默认 6）
    github_token: str = ""  # 搜索技能用（GitHub API Token）；配置后 search 限流 10→30 次/分钟
    # 在线更新检查
    update_check_enabled: bool = True   # 是否允许「检查更新」
    update_repo: str = "241793/RAG-Agent"  # 更新来源仓库（owner/repo，需公开或配 github_token）
    # 检索质量增强
    query_rewrite_enabled: bool = True   # 多轮追问：结合历史改写检索 query
    mmr_enabled: bool = True             # 融合后做 MMR 去冗余（多样性）
    mmr_lambda: float = 0.7              # MMR 相关性/多样性权衡
    rrf_k: int = 60                      # RRF 常数
    rrf_weight_vector: float = 1.0       # 向量路权重
    rrf_weight_bm25: float = 1.0         # BM25 路权重
    rrf_weight_external: float = 1.0     # 外部源权重

    # 外部知识库连接器
    connector_allow_private: bool = False  # 允许外部源指向内网/环回（企业内网部署时开启）
    connector_default_timeout: int = 15    # 连接器出站超时（秒）
    connector_max_top_k: int = 20          # 外部源单次召回的候选上限

    # Webhook 回调：企业自建客服台/ERP 常部署在内网，回调地址天然是 10.x / 192.168.x。
    # 与 connector_allow_private 分开，是为了收窄安全面 —— 只想接通内网客服系统时
    # 不必同时放开知识库连接器。
    webhook_allow_private_callback: bool = False

    # 工作流单次运行的执行预算（秒）。单节点都有超时，但整条链没有上限时
    # loop + LLM 可能跑几十分钟、长期占用连接与模型配额。
    workflow_run_timeout_seconds: int = 600

    # MCP（Model Context Protocol）
    mcp_allow_stdio: bool = False           # 是否允许本地 stdio 型 MCP server（会执行命令）
    mcp_stdio_allowed_cmds: str = "python,python3,node,npx,uvx"  # stdio 命令白名单（首 token）
    mcp_default_timeout: int = 30           # MCP 调用超时（秒）
    mcp_stdio_idle_seconds: int = 600       # stdio 长驻进程空闲回收（秒）

    # 客服
    service_notify_on_reply: bool = True    # 客户在工单里有新回复时通知客服（全局默认；工单级可覆盖）
    # 邮件入库（IMAP）
    email_poll_interval_seconds: int = 300  # 邮件源轮询周期（秒，0=关闭）

    # 安全
    # 默认占位值 → 启动时自动生成随机密钥并持久化到 data/.secret_key（见 _load_or_create_secret）
    secret_key: str = _load_or_create_secret()
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440
    refresh_token_expire_minutes: int = 10080
    file_token_expire_minutes: int = 30  # 文件访问签名 URL 有效期
    # 生成产物默认保留天数（0=永不过期）；到期由后台清理任务回收（文件+记录）
    artifact_ttl_days: int = 30
    # 登录防爆破
    login_rate_per_min: int = 10             # 单 IP 每分钟登录尝试上限（0=关闭）
    login_max_failures: int = 5              # 连续失败多少次锁定账号
    login_lock_minutes: int = 15             # 锁定时长（分钟）
    # 自助注册默认关闭：公网部署时避免被随意注册、灌满审核队列。
    # 需要开放时在「系统设置 → 安全」里打开，无需改代码。
    registration_enabled: bool = False       # 是否允许自助注册（注册后待审核）
    require_admin_approval: bool = True      # 注册用户是否必须管理员审核后才能登录

    # 存储
    storage_backend: str = "local"
    storage_local_dir: str = str(DATA_DIR / "files")
    # 技能包解压目录
    skill_pack_dir: str = str(DATA_DIR / "skills")
    # 可选组件安装目录（浏览器运行时等由用户点按安装的二进制）
    component_dir: str = str(DATA_DIR / "components")
    # 运行日志落盘（轮转）；空字符串=只输出到 stdout
    log_file: str = str(DATA_DIR / "logs" / "app.log")
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 5
    log_retention_days: int = 7            # 备份日志保留天数
    log_cleanup_interval_hours: int = 6    # 自动清理周期（小时，0=关闭）

    # 任务
    task_backend: str = "memory"  # memory | celery
    redis_url: str = "redis://localhost:6379/1"
    # 定时任务
    scheduler_max_concurrency: int = 4       # 同时在跑的定时任务数上限
    scheduler_max_retries_cap: int = 10      # 单任务重试次数硬上限（防放大）
    scheduler_history_keep: int = 200        # 每任务保留的历史条数

    # 内容安全（本地检测器）
    security_guard_enabled: bool = True
    security_guard_block_threshold: int = 70  # 达到则拦截
    security_guard_flag_threshold: int = 30  # 达到则标记
    security_guard_wrap_context: bool = True  # 是否给检索内容加边界标记
    security_guard_sensitive_words: str = ""  # 逗号分隔的敏感词

    # 默认 Provider
    default_llm_base_url: str = ""
    default_llm_api_key: str = ""
    default_llm_model: str = "gpt-4o-mini"
    default_embedding_base_url: str = ""
    default_embedding_api_key: str = ""
    default_embedding_model: str = "text-embedding-3-small"

    # CORS
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    def model_post_init(self, __context) -> None:
        """兜底：若 .env / 环境仍带入占位密钥（或空），替换为生成/持久化的随机密钥。

        这样无论配置来源如何，运行时都不会用可预测的默认值签名 JWT。
        """
        if not self.secret_key or self.secret_key == _SECRET_PLACEHOLDER:
            object.__setattr__(self, "secret_key", _load_or_create_secret())

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        return "postgres" in self.database_url

    @property
    def is_mysql(self) -> bool:
        return "mysql" in self.database_url

    @property
    def resolved_vector_backend(self) -> str:
        """解析向量后端：auto 时依据 DB 类型决定。"""
        if self.vector_backend != "auto":
            return self.vector_backend
        return "pgvector" if self.is_postgres else "numpy"

    @property
    def storage_dir(self) -> Path:
        p = Path(self.storage_local_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def skill_pack_path(self) -> Path:
        p = Path(self.skill_pack_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def component_path(self) -> Path:
        p = Path(self.component_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
