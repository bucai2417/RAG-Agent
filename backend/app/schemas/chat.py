"""检索与对话 DTO。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class RetrievalRequest(BaseModel):
    query: str
    kb_ids: list[int] | None = None
    top_k: int = Field(default=5, ge=1, le=50)
    use_hybrid: bool = True
    score_threshold: float = 0.0
    candidate_k: int = Field(default=20, ge=1, le=100)
    use_rerank: bool | None = None
    # 调试用：关掉「词面依据复核」，看到未经复核的完整候选（排查召回噪声时用）
    lexical_guard: bool = True


class RetrievedChunk(BaseModel):
    chunk_id: int
    doc_id: int
    kb_id: int
    content: str
    score: float
    page: int | None = None
    doc_title: str | None = None
    source: str = "vector"  # vector/bm25/fused/external
    external: bool = False          # 来自外部知识库
    source_uri: str | None = None   # 外部原文链接
    attachments: list | None = None  # 图文条目配套附件 [{file_key,name,mime,size}]


class RetrievalResponse(BaseModel):
    query: str
    chunks: list[RetrievedChunk]
    timing_ms: int = 0
    trusted: bool = True          # 是否有可信命中（无命中=False，供防幻觉用）
    degraded: bool = False        # 是否有召回通路降级（如向量不可用）
    warnings: list[str] = []      # 降级/异常标记


class ConversationCreate(BaseModel):
    title: str | None = None
    kb_ids: list[int] | None = None


class ConversationOut(BaseModel):
    id: int
    title: str
    kb_ids: list[int] | None = None
    model_config_id: int | None = None
    message_count: int
    settings: dict | None = None
    summary: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatRequest(BaseModel):
    conversation_id: int | None = None
    # 语义：None=未指定（沿用会话原有绑定）；[]=显式全库检索（清空绑定）；[id,...]=限定范围
    kb_ids: list[int] | None = None
    message: str
    stream: bool = True
    # 默认值取自系统设置（可在「系统设置→检索与重排」热改）；显式传入则覆盖
    top_k: int | None = None
    model_config_id: int | None = None  # 指定对话模型，空=默认
    temperature: float | None = None
    use_retrieval: bool = True  # False = 纯聊天，不检索知识库（用模型自身知识回答）
    use_tools: bool = True  # True = 允许 AI 调用平台工具（按权限过滤）；无可用工具时自动回退单轮
    allow_auto_write: bool = False  # True = 写操作直接执行；False = 走 HITL 确认
    attachments: list[dict] = Field(default_factory=list)  # [{type,file_key,...}]


class Citation(BaseModel):
    chunk_id: int
    doc_id: int
    doc_title: str | None = None
    page: int | None = None
    score: float = 0.0
    snippet: str = ""
    source_uri: str | None = None   # 外部知识库原文链接
    attachments: list | None = None  # 图文条目配套附件（命中后随回答发送）


class MessageOut(BaseModel):
    id: int
    conversation_id: int
    role: str
    content: str
    citations: list | None = None
    attachments: list | None = None
    artifacts: list | None = None
    reasoning: str | None = None
    usage: dict | None = None
    model: str | None = None
    feedback: int | None = None
    created_at: int = 0

    model_config = {"from_attributes": True}
