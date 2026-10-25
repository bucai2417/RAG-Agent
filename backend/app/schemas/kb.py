"""知识库与文档 DTO。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class KBCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    visibility: str = "internal"  # public/internal/private
    icon: str | None = None
    embedding_model_id: int | None = None
    chunk_strategy: dict | None = None
    source_type: str = "local"  # local=向量知识库 | entry=图文知识库 | external=外部知识库
    index_mode: str = "vector"  # vector=向量+关键词 | keyword=纯关键词(不需要 embedding)
    connector_kind: str | None = None
    connector_config: dict | None = None


class KBUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    visibility: str | None = None
    icon: str | None = None
    embedding_model_id: int | None = None
    chunk_strategy: dict | None = None
    settings: dict | None = None
    source_type: str | None = None
    index_mode: str | None = None
    connector_kind: str | None = None
    connector_config: dict | None = None
    # 乐观锁：客户端回传上次读到的 row_version；不传则不做前置校验
    row_version: int | None = None


class ConnectorTestIn(BaseModel):
    connector_kind: str
    connector_config: dict
    query: str = "测试"
    top_k: int = 3


class MemberIn(BaseModel):
    principal_type: str = "user"  # user/department/role/group
    principal_id: int
    perm_level: str = "viewer"  # viewer/editor/manager


class KBOut(BaseModel):
    id: int
    name: str
    description: str | None = None
    icon: str | None = None
    visibility: str
    source_type: str = "local"
    index_mode: str = "vector"
    connector_kind: str | None = None
    embedding_model_id: int | None = None
    embedding_dim: int
    doc_count: int
    chunk_count: int
    owner_id: int | None = None
    my_perm: str | None = None  # 当前用户对该库的有效级别 owner/manager/editor/viewer（仅详情接口填）
    created_at: datetime
    # 乐观锁：编辑时需原样回传，否则并发修改会被拒绝
    row_version: int = 1

    model_config = {"from_attributes": True}

    @field_validator("source_type", mode="before")
    @classmethod
    def _default_source_type(cls, v):
        return v or "local"

    @field_validator("index_mode", mode="before")
    @classmethod
    def _default_index_mode(cls, v):
        # 存量行该列可能为 NULL（后加列），兜底为 vector，避免响应校验 500
        return v if isinstance(v, str) and v else "vector"


class DocumentOut(BaseModel):
    id: int
    kb_id: int
    title: str
    kind: str = "file"
    file_name: str | None = None
    file_ext: str | None = None
    file_size: int
    content: str | None = None
    attachments: list | None = None
    status: str
    visibility: str
    progress: int
    error_msg: str | None = None
    error_detail: dict | None = None
    page_count: int
    char_count: int
    chunk_count: int
    tags: list[str] | None = None
    created_at: datetime
    # 乐观锁：编辑时需原样回传，否则并发修改会被拒绝
    row_version: int = 1

    model_config = {"from_attributes": True}

    # 存量行可能 kind/status/visibility 为 NULL（后加列），兜底为默认值，
    # 否则 from_attributes 取到 None 会触发响应校验 500。
    @field_validator("kind", mode="before")
    @classmethod
    def _kind_default(cls, v):
        return v if isinstance(v, str) and v else "file"

    @field_validator("status", mode="before")
    @classmethod
    def _status_default(cls, v):
        return v if isinstance(v, str) and v else "pending"

    @field_validator("visibility", mode="before")
    @classmethod
    def _visibility_default(cls, v):
        return v if isinstance(v, str) and v else "inherit"
