"""Provider 注册表：按用途（chat/embedding/rerank）解析配置并实例化驱动。

支持：
- 数据库驱动（从 model_provider + model_config 读配置）
- 运行时切换（配置变更后 invalidate 缓存）
- 降级链（同用途按 priority 排序，主失败试下一个）
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.models import ModelConfig, ModelProvider

# (provider_id, kind, base_url, api_key, timeout, extra_headers) 作为缓存键
_DRIVER_CACHE: dict[tuple, Any] = {}


@dataclass
class ResolvedModel:
    config_id: int
    purpose: str
    model_name: str
    provider_id: int
    provider_kind: str
    base_url: str
    api_key: str | None
    timeout: int
    extra_headers: dict | None
    embedding_dim: int | None
    params: dict
    max_retries: int = 2


def _build_driver(
    kind: str, base_url: str, api_key: str | None, timeout: int, extra_headers: dict | None,
    max_retries: int = 2,
):
    key = (kind, base_url, api_key, timeout, str(extra_headers), max_retries)
    if key in _DRIVER_CACHE:
        return _DRIVER_CACHE[key]

    if kind in ("anthropi", "claud"):
        from app.providers.drivers.anthropi import AnthropiDriver

        drv = AnthropiDriver(base_url=base_url, api_key=api_key, timeout=timeout,
                             extra_headers=extra_headers, max_retries=max_retries)
    elif kind == "ollama":
        from app.providers.drivers.ollama import OllamaDriver

        drv = OllamaDriver(base_url=base_url, api_key=api_key, timeout=timeout,
                           extra_headers=extra_headers, max_retries=max_retries)
    elif kind == "local_st":
        # 本地离线向量模型（sentence-transformers）：base_url 存模型标识（如 BAAI/bge-small-zh-v1.5），
        # 模型文件由 app.providers.local_embed 负责下载/缓存，此处只做实例化
        from app.providers.drivers.local_st import LocalSTDriver

        drv = LocalSTDriver(model_id=base_url or "", device="cpu")
    elif kind == "local_bge":
        # 已被 local_st 取代；保留仅为兼容既有配置（FlagEmbedding 依赖很重，不再推荐）
        from app.providers.drivers.local_bge import LocalBGEDriver

        drv = LocalBGEDriver(model_path=base_url, device="cpu")
    elif kind == "local_hash":
        from app.providers.drivers.local_hash import LocalHashDriver

        dim = 1024
        if extra_headers and extra_headers.get("dim"):
            dim = int(extra_headers["dim"])
        drv = LocalHashDriver(dim=dim)
    elif kind == "local_agent":
        from app.providers.drivers.local_agent import LocalAgentDriver

        tool_name = (extra_headers or {}).get("tool_name", "knowledge_retrieval")
        drv = LocalAgentDriver(tool_name=tool_name)
    else:  # openai / azure / vllm / tei / custom 都走 OpenAI 兼容
        from app.providers.drivers.openai_compat import OpenAICompatibleDriver

        drv = OpenAICompatibleDriver(
            base_url=base_url, api_key=api_key, timeout=timeout, extra_headers=extra_headers,
            max_retries=max_retries,
        )
    _DRIVER_CACHE[key] = drv
    return drv


def invalidate_cache() -> None:
    _DRIVER_CACHE.clear()


async def resolve_models(
    db: AsyncSession, *, tenant_id: int, purpose: str, config_id: int | None = None
) -> list[ResolvedModel]:
    """解析某用途的模型配置链（按 priority 升序，供降级）。"""
    stmt = (
        select(ModelConfig, ModelProvider)
        .join(ModelProvider, ModelConfig.provider_id == ModelProvider.id)
        .where(
            ModelConfig.purpose == purpose,
            ModelConfig.status == "active",
            ModelProvider.status == "active",
            (ModelConfig.tenant_id == tenant_id) | (ModelConfig.tenant_id.is_(None)),
        )
        .order_by(ModelConfig.priority.asc())
    )
    if config_id:
        stmt = stmt.where(ModelConfig.id == config_id)

    rows = (await db.execute(stmt)).all()
    resolved: list[ResolvedModel] = []
    for mc, mp in rows:
        resolved.append(
            ResolvedModel(
                config_id=mc.id,
                purpose=mc.purpose,
                model_name=mc.model_name,
                provider_id=mp.id,
                provider_kind=mp.kind,
                base_url=mp.base_url,
                api_key=mp.api_key,
                timeout=mp.timeout,
                extra_headers=mp.extra_headers,
                embedding_dim=mc.embedding_dim,
                params=mc.params or {},
                max_retries=int(getattr(mp, "max_retries", 2) or 0),
            )
        )
    return resolved


async def get_llm(
    db: AsyncSession, *, tenant_id: int, config_id: int | None = None
) -> tuple[Any, ResolvedModel]:
    models = await resolve_models(db, tenant_id=tenant_id, purpose="chat", config_id=config_id)
    if not models:
        raise ValidationError("未配置对话模型，请先在模型管理中配置 Provider 与 chat 模型")
    rm = models[0]
    return _build_driver(rm.provider_kind, rm.base_url, rm.api_key, rm.timeout, rm.extra_headers, rm.max_retries), rm


async def get_embedding(
    db: AsyncSession, *, tenant_id: int, config_id: int | None = None
) -> tuple[Any, ResolvedModel]:
    models = await resolve_models(db, tenant_id=tenant_id, purpose="embedding", config_id=config_id)
    if not models and config_id:
        # KB 指定的向量模型已停用/删除：静默回退到租户默认，避免该库整体检索失败
        models = await resolve_models(db, tenant_id=tenant_id, purpose="embedding")
    if not models:
        raise ValidationError("未配置向量模型，请先在模型管理中配置 embedding 模型")
    rm = models[0]
    return _build_driver(rm.provider_kind, rm.base_url, rm.api_key, rm.timeout, rm.extra_headers, rm.max_retries), rm


async def get_rerank(
    db: AsyncSession, *, tenant_id: int, config_id: int | None = None
) -> tuple[Any, ResolvedModel] | None:
    models = await resolve_models(db, tenant_id=tenant_id, purpose="rerank", config_id=config_id)
    if not models:
        return None
    rm = models[0]
    return _build_driver(rm.provider_kind, rm.base_url, rm.api_key, rm.timeout, rm.extra_headers, rm.max_retries), rm
