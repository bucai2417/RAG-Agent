"""知识库、KB 成员、文档、文档 ACL、分块（含权限元数据）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, VectorType
from app.models.base import ROW_VERSION_COL, IdMixin, RowVersionMixin, TenantMixin, TimestampMixin

# vis_scope 取值
VIS_KB_DEFAULT = 0   # 继承知识库可见性
VIS_KB_PUBLIC = 1    # 知识库公开
VIS_RESTRICTED = 2   # 受限（读 acl_allow / acl_deny）


class KnowledgeBase(Base, IdMixin, TimestampMixin, TenantMixin, RowVersionMixin):
    __tablename__ = "knowledge_base"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # public=租户全员可见, internal=按成员授权, private=仅显式成员
    visibility: Mapped[str] = mapped_column(String(16), default="internal", index=True)
    # 数据来源：local=本地上传入库, external=外部 RAG 系统联邦检索
    source_type: Mapped[str] = mapped_column(String(16), default="local", index=True)
    # 索引方式：vector=向量+关键词混合（默认）｜ keyword=纯关键词(BM25)，不需要 embedding 模型
    index_mode: Mapped[str] = mapped_column(String(16), default="vector", index=True)
    connector_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)  # generic_http/dify/ragflow/fastgpt/mcp
    connector_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # base_url/api_key(密文)/...
    embedding_model_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    chunk_strategy: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    vector_backend: Mapped[str] = mapped_column(String(16), default="auto")
    embedding_dim: Mapped[int] = mapped_column(Integer, default=1024)
    doc_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active")
    owner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __mapper_args__ = {"version_id_col": ROW_VERSION_COL}


class KBMember(Base, IdMixin, TimestampMixin, TenantMixin):
    """知识库成员授权。principal_id 采用统一编码（见 services/permission.py）。"""

    __tablename__ = "kb_member"
    __table_args__ = (
        UniqueConstraint("kb_id", "principal_id", name="uq_kb_member_kb_principal"),
    )

    kb_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    principal_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    perm_level: Mapped[str] = mapped_column(String(16), default="viewer")  # viewer/editor/manager
    granted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class DocumentFolder(Base, IdMixin, TimestampMixin, TenantMixin):
    """知识库内的文档文件夹。"""

    __tablename__ = "document_folder"

    kb_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    parent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class Document(Base, IdMixin, TimestampMixin, TenantMixin, RowVersionMixin):
    __tablename__ = "document"

    kb_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    folder_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    # file=上传文件（解析入库）；entry=直接录入的图文条目（content 正文 + attachments 配套附件）
    kind: Mapped[str] = mapped_column(String(16), default="file", index=True)
    source_type: Mapped[str] = mapped_column(String(16), default="upload")
    source_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    file_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_ext: Mapped[str | None] = mapped_column(String(16), nullable=True)
    file_size: Mapped[int] = mapped_column(BigInteger, default=0)
    mime: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    # 图文条目：手录正文 + 配套附件 [{file_key, name, mime, size}]（命中后随回答发给提问者）
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachments: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # pending/parsing/chunking/embedding/ready/failed/disabled
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    visibility: Mapped[str] = mapped_column(String(16), default="inherit")  # inherit/public/restricted
    progress: Mapped[int] = mapped_column(SmallInteger, default=0)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 结构化报错（阶段/上游/建议）
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 字符串标签数组
    uploaded_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __mapper_args__ = {"version_id_col": ROW_VERSION_COL}


class DocumentACL(Base, IdMixin, TimestampMixin):
    """文档级例外授权（document.visibility=restricted 时生效）。下轮启用完整逻辑。"""

    __tablename__ = "document_acl"

    document_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    principal_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    effect: Mapped[str] = mapped_column(String(8), default="allow")  # allow/deny


class DocumentVersion(Base, IdMixin, TimestampMixin, TenantMixin):
    """文档版本快照：重新处理/覆盖上传前归档上一版正文与分块，支持查看与回滚。"""

    __tablename__ = "document_version"

    doc_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)  # 被归档的版本号
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 归档时的解析正文（截断保存，避免体积失控）
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    # 分块快照：[{ordinal, chunk_type, content, page, section}]，用于回滚重建
    chunk_snapshot: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 归档原因：reprocess=重新处理 / reupload=覆盖上传 / manual=手动
    reason: Mapped[str] = mapped_column(String(16), default="reprocess")
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class Chunk(Base, IdMixin, TimestampMixin, TenantMixin):
    __tablename__ = "chunk"

    kb_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    doc_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    parent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # 子块的父指针
    chunk_type: Mapped[str] = mapped_column(String(8), default="flat")  # parent/child/flat
    ordinal: Mapped[int] = mapped_column(Integer, default=0)  # 文档内顺序
    content: Mapped[str] = mapped_column(Text, nullable=False)
    parent_content: Mapped[str | None] = mapped_column(Text, nullable=True)  # 冗余存父块全文
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section: Mapped[str | None] = mapped_column(String(512), nullable=True)
    embedding = mapped_column(VectorType(), nullable=True)
    tsv: Mapped[str | None] = mapped_column(Text, nullable=True)  # 关键词检索文本
    # ==== 权限元数据 ====
    vis_scope: Mapped[int] = mapped_column(SmallInteger, default=VIS_KB_DEFAULT)
    acl_allow: Mapped[list | None] = mapped_column(JSON, nullable=True)  # bigint[] 语义
    acl_deny: Mapped[list | None] = mapped_column(JSON, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
