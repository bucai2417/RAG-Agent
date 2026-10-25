"""智能体 / 技能 / 工具 DTO。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# ---- Agent ----
class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    slug: str | None = None
    description: str | None = None
    icon: str | None = None
    type: str = "agent"  # agent | workflow
    system_prompt: str = ""
    model_config_id: int | None = None
    kb_ids: list[int] | None = None
    skill_ids: list[int] | None = None
    tool_config: dict | None = None
    config: dict | None = None


class AgentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    system_prompt: str | None = None
    model_config_id: int | None = None
    kb_ids: list[int] | None = None
    skill_ids: list[int] | None = None
    tool_config: dict | None = None
    config: dict | None = None
    status: str | None = None
    visibility: str | None = None
    default_mode_id: int | None = None
    # 乐观锁：客户端回传上次读到的 row_version；不传则不做前置校验
    row_version: int | None = None


class AgentOut(BaseModel):
    id: int
    name: str
    slug: str
    description: str | None = None
    icon: str | None = None
    type: str
    system_prompt: str
    model_config_id: int | None = None
    kb_ids: list | None = None
    skill_ids: list | None = None
    tool_config: dict | None = None
    config: dict | None = None
    default_mode_id: int | None = None
    status: str
    visibility: str
    owner_id: int | None = None
    is_builtin: bool = False
    created_at: datetime
    # 乐观锁：编辑时原样回传
    row_version: int = 1

    model_config = {"from_attributes": True}


class AgentRunRequest(BaseModel):
    message: str
    conversation_id: int | None = None
    mode_id: int | None = None
    model_config_id: int | None = None  # 本次覆盖对话模型
    kb_ids: list[int] | None = None


class ToolConfirmRequest(BaseModel):
    action_id: int
    decision: str = "approve"  # approve | reject


# ---- Mode ----
class ModeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = None
    system_prompt: str = ""
    skill_ids: list[int] | None = None
    tool_config: dict | None = None
    kb_ids: list[int] | None = None
    params: dict | None = None
    is_default: bool = False
    sort: int = 0


class ModeUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    skill_ids: list[int] | None = None
    tool_config: dict | None = None
    kb_ids: list[int] | None = None
    params: dict | None = None
    is_default: bool | None = None
    sort: int | None = None


class ModeOut(BaseModel):
    id: int
    agent_id: int
    name: str
    description: str | None = None
    system_prompt: str
    skill_ids: list | None = None
    tool_config: dict | None = None
    kb_ids: list | None = None
    params: dict | None = None
    is_default: bool
    sort: int

    model_config = {"from_attributes": True}


# ---- Skill ----
class SkillCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    slug: str | None = None
    description: str | None = None
    icon: str | None = None
    kind: str = "prompt_pack"  # prompt_pack | tool
    prompt_template: str | None = None
    params_schema: dict | None = None
    tool_ids: list[int] | None = None
    kb_ids: list[int] | None = None
    model_config_id: int | None = None
    tool_def: dict | None = None


class SkillUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    prompt_template: str | None = None
    params_schema: dict | None = None
    tool_ids: list[int] | None = None
    kb_ids: list[int] | None = None
    model_config_id: int | None = None
    tool_def: dict | None = None
    body_md: str | None = None  # 技能包 SKILL.md 正文，可编辑
    status: str | None = None


class SkillOut(BaseModel):
    id: int
    name: str
    slug: str
    description: str | None = None
    icon: str | None = None
    kind: str
    prompt_template: str | None = None
    params_schema: dict | None = None
    tool_ids: list | None = None
    kb_ids: list | None = None
    model_config_id: int | None = None
    tool_def: dict | None = None
    source: str = "manual"
    body_md: str | None = None
    package_id: int | None = None
    parent_id: int | None = None
    collection_subpath: str | None = None
    status: str
    owner_id: int | None = None

    model_config = {"from_attributes": True}


# ---- Tool ----
class ToolCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    display_name: str = ""
    description: str
    parameters: dict | None = None
    source: dict | None = None


class ToolOut(BaseModel):
    id: int
    name: str
    display_name: str
    description: str
    kind: str
    parameters: dict | None = None
    source: dict | None = None
    enabled: bool
    status: str
    builtin: bool = False

    model_config = {"from_attributes": True}


class ToolTestRequest(BaseModel):
    args: dict = Field(default_factory=dict)


# ---- 人设模板 ----
class PersonaTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    system_prompt: str = ""
    category: str = ""
    icon: str | None = None
    tags: list[str] | None = None
    sort: int = 0
    enabled: bool = True


class PersonaTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    description: str | None = None
    system_prompt: str | None = None
    category: str | None = None
    icon: str | None = None
    tags: list[str] | None = None
    sort: int | None = None
    enabled: bool | None = None


class PersonaTemplateOut(BaseModel):
    id: int
    name: str
    slug: str | None = None
    description: str | None = None
    system_prompt: str
    category: str
    icon: str | None = None
    tags: list | None = None
    sort: int
    enabled: bool
    is_builtin: bool

    model_config = {"from_attributes": True}
