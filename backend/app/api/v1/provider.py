"""模型 Provider 配置接口：Provider/模型配置 CRUD + 连接测试。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.errors import NotFoundError, ValidationError
from app.middleware.auth_dep import get_current_user, require_admin, require_permission
from app.services.audit_service import audited
from app.models import ModelConfig, ModelProvider, User
from app.providers.base import ChatMessage
from app.providers.registry import _build_driver, invalidate_cache
from app.schemas.provider import (
    ModelConfigCreate,
    ModelConfigOut,
    ProviderCreate,
    ProviderOut,
    ProviderTestRequest,
    ProviderTestResult,
    ProviderUpdate,
)

router = APIRouter(prefix="/providers", tags=["provider"])


def _to_out(p: ModelProvider) -> ProviderOut:
    return ProviderOut(
        id=p.id,
        name=p.name,
        kind=p.kind,
        base_url=p.base_url,
        api_key_set=bool(p.api_key),
        status=p.status,
        health_status=p.health_status,
        last_check_at=p.last_check_at,
        timeout=p.timeout,
        extra_headers=p.extra_headers,
    )


@router.get("", response_model=list[ProviderOut])
async def list_providers(
    user: User = Depends(require_permission("model:read")), db: AsyncSession = Depends(get_db)
) -> list[ProviderOut]:
    rows = (
        await db.execute(
            select(ModelProvider).where(
                (ModelProvider.tenant_id == user.tenant_id) | (ModelProvider.tenant_id.is_(None))
            )
        )
    ).scalars().all()
    return [_to_out(p) for p in rows]


# ---- 本地离线向量模型 ----
# 路由顺序要求：这些 /local/* 必须定义在 /{provider_id} 系列之前，否则 "local" 会被
# 当成 provider_id 走进 int 解析而报 422。
#
# 线程化要求：这里每个端点都会碰文件系统（扫描模型目录、统计 runtime 体积、
# 解压 zip、删除目录）。**必须 `asyncio.to_thread`**——这些函数挂的目录里有
# 2~3 万个文件，同步跑会把整个事件循环钉住：实测进一次模型管理页，
# `/local/status` 要 11.8s，期间其它请求（含 /health）全部无响应，
# 用户看到的就是「登录后整站卡死」。判据是「这个调用会不会等磁盘」，会就丢线程池。


@router.get("/local/status")
async def local_embedding_status(
    user: User = Depends(require_permission("model:read")),
) -> dict:
    """本地向量模型的依赖状态、缓存目录与模型清单（不联网）。"""
    import asyncio

    return await asyncio.to_thread(_collect_local_status)


def _collect_local_status() -> dict:
    """在 worker 线程里拼装本地模型面板数据（同步函数，勿在事件循环里直接调用）。"""
    from app.providers import local_embed

    dep = local_embed.dependency_status()
    cached = {m["id"]: m for m in local_embed.scan_local_models()}
    models: list[dict] = []
    seen: set[str] = set()
    for m in local_embed.RECOMMENDED_MODELS:
        item = dict(m)
        local = cached.get(m["id"])
        item["cached"] = bool(local)
        item["path"] = local["path"] if local else None
        item["source"] = local["source"] if local else None
        item["progress"] = local_embed.progress(m["id"])
        models.append(item)
        seen.add(m["id"])
    # 用户自己导入/手拷的模型（不在推荐列表里）也要能看到，否则导入了却无处使用
    for mid, local in cached.items():
        if mid in seen:
            continue
        models.append(
            {
                "id": mid,
                "label": local.get("label") or mid,
                "dim": local.get("dim") or 0,
                "size": "",
                "note": "本地导入",
                "cached": True,
                "path": local["path"],
                "source": local["source"],
                "progress": local_embed.progress(mid),
            }
        )
    return {
        "installed": dep["installed"],
        "torch": dep["torch"],
        "version": dep["version"],
        "error": dep["error"],
        "cache_dir": str(local_embed.cache_dir()),
        "runtime_dir": str(local_embed.runtime_dir()),
        "models": models,
        "install_hint": local_embed.install_hint(),
        "mirror": local_embed.hf_endpoint(),
        "dependency": local_embed.runtime_status(),
    }


@router.get("/local/progress")
async def local_embedding_progress(
    model_id: str,
    user: User = Depends(require_permission("model:read")),
) -> dict:
    """轮询下载进度。纯内存读，不需要线程化。"""
    from app.providers import local_embed

    return {"model_id": model_id, **local_embed.progress(model_id)}


# ---- 本地推理依赖（torch + sentence-transformers）一键安装 / 卸载 ----


@router.get("/local/dependency")
async def local_dependency_status(
    user: User = Depends(require_permission("model:read")),
) -> dict:
    """runtime 可选依赖的状态：是否已装、占多少空间、能否自动安装、安装进度。"""
    import asyncio

    from app.providers import local_embed

    return await asyncio.to_thread(local_embed.runtime_status)


@router.get("/local/dependency/progress")
async def local_dependency_progress(
    user: User = Depends(require_permission("model:read")),
) -> dict:
    """轮询依赖安装进度（含 pip 日志尾部）。纯内存读，不需要线程化。"""
    from app.providers import local_embed

    return local_embed.dependency_progress()


@router.post("/local/dependency/install")
@audited("provider.local_dep_install", "model_provider")
async def local_dependency_install(
    user: User = Depends(require_admin),
) -> dict:
    """后台安装依赖到 <数据目录>/data/models/runtime，立刻返回，前端轮询进度。"""
    import asyncio

    from app.providers import local_embed

    # 冻结态要先探测解释器（起子进程），别在事件循环里等
    return await asyncio.to_thread(local_embed.start_dependency_install)


@router.delete("/local/dependency")
@audited("provider.local_dep_uninstall", "model_provider")
async def local_dependency_uninstall(
    user: User = Depends(require_admin),
) -> dict:
    """卸载依赖目录并释放已加载的模型实例（被占用的文件下次启动清理）。"""
    import asyncio

    from app.providers import local_embed

    # 800MB / 2.7 万文件的目录改名/删除，同步跑会明显卡住事件循环
    return await asyncio.to_thread(local_embed.uninstall_dependencies)


@router.post("/local/download")
@audited("provider.local_download", "model_provider")
async def download_local_model(
    body: dict,
    user: User = Depends(require_admin),
) -> dict:
    """启动后台下载指定模型。立刻返回，前端轮询 /local/progress。"""
    import asyncio

    from app.providers import local_embed

    model_id = str((body or {}).get("model_id", "")).strip()
    source = str((body or {}).get("source") or "hf")
    if not model_id:
        raise ValidationError("请指定 model_id")
    # start_download 内部会扫缓存目录判断「是否已下载」
    return await asyncio.to_thread(local_embed.start_download, model_id, source=source)


@router.post("/local/import")
@audited("provider.local_import", "model_provider")
async def import_local_model(
    file: UploadFile = File(...),
    name: str | None = None,
    user: User = Depends(require_admin),
) -> dict:
    """上传模型 zip 导入（完全离线场景用）。zip 内含 config.json + 权重文件。"""
    import asyncio
    import tempfile
    from pathlib import Path as _Path

    from app.providers import local_embed

    if file.filename and not file.filename.lower().endswith(".zip"):
        raise ValidationError("请上传 zip 格式的模型包")
    tmp_dir = _Path(tempfile.mkdtemp(prefix="rag-model-upload-"))
    tmp_zip = tmp_dir / "model.zip"
    try:
        total = 0
        with open(tmp_zip, "wb") as f:
            while True:
                chunk = await file.read(8 * 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > 2 * 1024 * 1024 * 1024:
                    raise ValidationError("模型包超过 2GB 上限")
                f.write(chunk)
        if total == 0:
            raise ValidationError("上传的模型包为空")
        # 解压 400MB+ 权重必须离开事件循环，否则上传期间整站请求一起挂住
        return await asyncio.to_thread(local_embed.install_from_zip, tmp_zip, name=name)
    finally:
        import shutil as _shutil

        _shutil.rmtree(tmp_dir, ignore_errors=True)


@router.delete("/local/models/{model_id:path}")
@audited("provider.local_delete", "model_provider")
async def delete_local_model(
    model_id: str,
    user: User = Depends(require_admin),
) -> dict:
    """删除已下载/导入的本地模型。"""
    import asyncio

    from app.providers import local_embed

    return await asyncio.to_thread(local_embed.delete_model, model_id)


@router.post("", response_model=ProviderOut)
@audited("provider.create", "model_provider")
async def create_provider(
    body: ProviderCreate,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ProviderOut:
    p = ModelProvider(
        tenant_id=user.tenant_id,
        name=body.name,
        kind=body.kind,
        base_url=body.base_url,
        api_key=body.api_key,
        extra_headers=body.extra_headers,
        timeout=body.timeout,
        max_retries=body.max_retries,
    )
    db.add(p)
    await db.flush()
    invalidate_cache()
    return _to_out(p)


@router.patch("/{provider_id}", response_model=ProviderOut)
@audited("provider.update", "model_provider", id_arg="provider_id")
async def update_provider(
    provider_id: int,
    body: ProviderUpdate,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ProviderOut:
    p = await db.get(ModelProvider, provider_id)
    if not p:
        raise NotFoundError("Provider 不存在")
    for field, val in body.model_dump(exclude_unset=True).items():
        setattr(p, field, val)
    await db.flush()
    invalidate_cache()
    return _to_out(p)


@router.delete("/{provider_id}")
@audited("provider.delete", "model_provider", id_arg="provider_id")
async def delete_provider(
    provider_id: int, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    p = await db.get(ModelProvider, provider_id)
    if not p:
        raise NotFoundError("Provider 不存在")
    # 级联清理该 Provider 下的模型配置，避免产生指向不存在 Provider 的孤儿配置
    cfg_count = (
        await db.execute(
            select(func.count()).select_from(ModelConfig).where(ModelConfig.provider_id == provider_id)
        )
    ).scalar_one()
    if cfg_count:
        await db.execute(delete(ModelConfig).where(ModelConfig.provider_id == provider_id))
    await db.delete(p)
    invalidate_cache()
    return {"message": f"已删除（同时清理 {cfg_count} 个关联模型配置）" if cfg_count else "已删除"}


@router.post("/{provider_id}/test", response_model=ProviderTestResult)
async def test_provider(
    provider_id: int,
    body: ProviderTestRequest,
    user: User = Depends(require_permission("model:read")),
    db: AsyncSession = Depends(get_db),
) -> ProviderTestResult:
    p = await db.get(ModelProvider, provider_id)
    if not p:
        raise NotFoundError("Provider 不存在")
    drv = _build_driver(p.kind, p.base_url, p.api_key, p.timeout, p.extra_headers, getattr(p, 'max_retries', 2))
    t0 = time.time()
    try:
        if body.purpose == "embedding":
            model = body.model_name
            if not model:
                mc = (
                    await db.execute(
                        select(ModelConfig).where(
                            ModelConfig.provider_id == provider_id, ModelConfig.purpose == "embedding"
                        )
                    )
                ).scalars().first()
                model = mc.model_name if mc else "text-embedding-3-small"
            vecs = await drv.embed([body.text or "测试文本"], model=model)
            return ProviderTestResult(
                ok=True, latency_ms=int((time.time() - t0) * 1000), message=f"向量维度={len(vecs[0])}"
            )
        else:
            model = body.model_name
            if not model:
                mc = (
                    await db.execute(
                        select(ModelConfig).where(
                            ModelConfig.provider_id == provider_id, ModelConfig.purpose == "chat"
                        )
                    )
                ).scalars().first()
                model = mc.model_name if mc else "gpt-4o-mini"
            res = await drv.chat([ChatMessage(role="user", content=body.text or "你好")], model=model)
            return ProviderTestResult(
                ok=True,
                latency_ms=int((time.time() - t0) * 1000),
                message=f"回复: {res.content[:80]}",
            )
    except Exception as e:  # noqa: BLE001
        return ProviderTestResult(ok=False, latency_ms=int((time.time() - t0) * 1000), message=str(e)[:300])


@router.post("/{provider_id}/health")
async def check_provider_health(
    provider_id: int,
    purpose: str = "chat",
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """对 Provider 做一次健康检查，回写 health_status / last_check_at。"""
    from app.core.errors import UpstreamError

    p = await db.get(ModelProvider, provider_id)
    if not p:
        raise NotFoundError("Provider 不存在")
    drv = _build_driver(p.kind, p.base_url, p.api_key, p.timeout, p.extra_headers, getattr(p, 'max_retries', 2))
    t0 = time.time()
    ok = False
    message = ""
    try:
        try:
            h = await drv.health(purpose=purpose)
        except TypeError:
            h = await drv.health()  # 部分驱动 health() 不接受 purpose
        ok = bool(getattr(h, "ok", False))
        message = getattr(h, "message", "") or ("正常" if ok else "失败")
    except Exception as e:  # noqa: BLE001
        detail = str(e)
        if isinstance(e, UpstreamError):
            detail = str(e)
        message = detail[:300]

    p.health_status = "ok" if ok else "fail"
    p.last_check_at = int(time.time() * 1000)
    await db.flush()
    return {
        "ok": ok, "message": message, "latency_ms": int((time.time() - t0) * 1000),
        "health_status": p.health_status,
        "last_check_at": p.last_check_at,
    }


# ---- 模型配置 ----
@router.get("/configs/all", response_model=list[ModelConfigOut])
async def list_model_configs(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[ModelConfigOut]:
    rows = (
        await db.execute(
            select(ModelConfig).where(
                (ModelConfig.tenant_id == user.tenant_id) | (ModelConfig.tenant_id.is_(None))
            )
        )
    ).scalars().all()
    return [ModelConfigOut.model_validate(r) for r in rows]


@router.post("/configs", response_model=ModelConfigOut)
@audited("model_config.create", "model_config")
async def create_model_config(
    body: ModelConfigCreate,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ModelConfigOut:
    mc = ModelConfig(
        tenant_id=user.tenant_id,
        provider_id=body.provider_id,
        purpose=body.purpose,
        model_name=body.model_name,
        display_name=body.display_name or body.model_name,
        params=body.params,
        embedding_dim=body.embedding_dim,
        is_default=body.is_default,
        priority=body.priority,
    )
    db.add(mc)
    await db.flush()
    invalidate_cache()
    return ModelConfigOut.model_validate(mc)


@router.get("/{provider_id}/models")
async def list_provider_models(
    provider_id: int,
    user: User = Depends(require_permission("model:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """拉取该 Provider 的可用模型列表（调用远端 /v1/models）。"""
    p = await db.get(ModelProvider, provider_id)
    if not p:
        raise NotFoundError("Provider 不存在")
    drv = _build_driver(p.kind, p.base_url, p.api_key, p.timeout, p.extra_headers, getattr(p, 'max_retries', 2))
    # 兜底：驱动不一定都实现了 list_models（Protocol 不强制），缺实现时给出
    # 明确提示而不是让前端收到 AttributeError 的 500
    if not hasattr(drv, "list_models"):
        return {"ok": False, "models": [],
                "message": f"该 Provider 类型（{p.kind}）不支持自动获取模型列表，请手动填写模型标识"}
    try:
        models = await drv.list_models()
        return {"ok": True, "models": models}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "models": [], "message": str(e)[:300]}


class ModelConfigUpdate(BaseModel):
    model_name: str | None = None
    display_name: str | None = None
    purpose: str | None = None
    params: dict | None = None
    embedding_dim: int | None = None
    is_default: bool | None = None
    priority: int | None = None
    status: str | None = None


@router.patch("/configs/{config_id}", response_model=ModelConfigOut)
@audited("model_config.update", "model_config", id_arg="config_id")
async def update_model_config(
    config_id: int,
    body: ModelConfigUpdate,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ModelConfigOut:
    mc = await db.get(ModelConfig, config_id)
    if not mc or mc.tenant_id != user.tenant_id:
        raise NotFoundError("模型配置不存在")
    # 设为默认时，把同 purpose 的其它配置取消默认
    if body.is_default:
        others = (
            await db.execute(
                select(ModelConfig).where(
                    ModelConfig.tenant_id == mc.tenant_id,
                    ModelConfig.purpose == (body.purpose or mc.purpose),
                    ModelConfig.id != mc.id,
                )
            )
        ).scalars().all()
        for o in others:
            o.is_default = False
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(mc, k, v)
    await db.flush()
    invalidate_cache()
    return ModelConfigOut.model_validate(mc)


@router.delete("/configs/{config_id}")
@audited("model_config.delete", "model_config", id_arg="config_id")
async def delete_model_config(
    config_id: int,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    mc = await db.get(ModelConfig, config_id)
    if not mc or mc.tenant_id != user.tenant_id:
        raise NotFoundError("模型配置不存在")
    await db.delete(mc)
    await db.flush()
    invalidate_cache()
    return {"message": "已删除"}
