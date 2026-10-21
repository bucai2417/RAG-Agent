"""外部 IM 渠道配置（QQ机器人/微信/企业微信/飞书）。密钥经 core.crypto 加密。"""
from __future__ import annotations

from sqlalchemy import JSON, BigInteger, Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import IdMixin, TenantMixin, TimestampMixin


class Channel(Base, IdMixin, TimestampMixin, TenantMixin):
    __tablename__ = "channel"

    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # qqbot/wxclaw/wework/feishu
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 含加密后的密钥
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 外部用户落地的默认租户（不填则用渠道所属租户）
    default_tenant_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    default_kb_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)   # 渠道默认知识库
    # 检索范围：auto=按用户权限自动 / custom=仅指定库 / off=不检索（纯聊天）
    kb_mode: Mapped[str] = mapped_column(String(8), default="auto")
    # 服务模式：qa=只答（不转人工）/ support=客服（识别转人工关键词并建单）
    service_mode: Mapped[str] = mapped_column(String(8), default="qa")
    # 默认语言（多语言客服：zh/en/ja/... 空=自动判断）
    default_lang: Mapped[str | None] = mapped_column(String(16), nullable=True)
    default_agent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # 默认智能体
    reply_mode: Mapped[str] = mapped_column(String(16), default="rag")  # rag/smart/agent/workflow
    command_prefix: Mapped[str] = mapped_column(String(8), default="/")
    greeting: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 工作中提示：工作流型智能体执行时把节点进度发给客户（默认关）
    progress_hint: Mapped[bool] = mapped_column(Boolean, default=False)
    # 群聊进度提示策略：off（不发，默认）/ first（只发第一条）/ throttle（按间隔节流）
    # 群聊每节点都发会刷屏，所以默认不在群里发；需要时按策略收窄频率。
    group_progress_mode: Mapped[str] = mapped_column(String(16), default="off")
    group_progress_interval: Mapped[int] = mapped_column(Integer, default=15)  # 秒（throttle 用）
    # ---- 渠道附件入库（外部用户发来的文档进知识库）----
    # off=不入库(默认,最安全) / bound=仅已绑定内部账号的渠道用户 / all=所有渠道用户(管理员显式开)
    attach_ingest: Mapped[str] = mapped_column(String(8), default="off")
    attach_kb_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 空→回落 default_kb_ids
    attach_max_mb: Mapped[int] = mapped_column(Integer, default=20)  # 单文件大小上限
    attach_types: Mapped[str | None] = mapped_column(String(256), nullable=True)  # 逗号分隔扩展名白名单，空=默认集
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/deleted
    # 运行时状态（UI 展示）
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
