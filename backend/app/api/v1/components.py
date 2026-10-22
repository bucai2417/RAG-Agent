"""可选组件接口：查看状态、安装（官方源 / 镜像 URL / 本地 zip）、进度、卸载。

权限：查看用 tool:read，安装/卸载用 tool:manage（与「测试工具」「新建工具」同级）。
安装是后台任务（209MB 下载可能数分钟），接口立即返回，前端轮询 progress。

审计：本模块没有 db 依赖，故用 record_audit_async 独立写审计（失败不影响主流程）。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel

from app.core.errors import ValidationError
from app.middleware.auth_dep import require_permission
from app.models import User
from app.services import component_service as CS
from app.services.audit_service import record_audit_async

router = APIRouter(prefix="/components", tags=["component"])


class InstallIn(BaseModel):
    source: str = "official"   # official | url
    url: str | None = None


async def _audit(action: str, key: str, result: str = "success", error: str | None = None) -> None:
    await record_audit_async(action=action, resource_type="component",
                             resource_id=key, result=result, error=error)


def _unlink_quiet(p) -> None:
    try:
        Path(p).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


@router.get("")
async def list_components(
    user: User = Depends(require_permission("tool:read")),
) -> list[dict]:
    """全部可选组件 + 当前平台的安装状态与进度。"""
    return CS.list_status()


@router.get("/{key}")
async def component_status(
    key: str,
    user: User = Depends(require_permission("tool:read")),
) -> dict:
    return CS.status(key)


@router.post("/{key}/install")
async def component_install(
    key: str,
    body: InstallIn,
    user: User = Depends(require_permission("tool:manage")),
) -> dict:
    """启动后台安装。official 用内置 URL 与内置 sha256；url 为用户自填镜像。"""
    try:
        ret = CS.start_install(key, source=body.source, url=body.url)
    except Exception as e:
        await _audit("component.install", key, result="failure", error=str(e)[:200])
        raise
    await _audit("component.install", key)
    return ret


@router.post("/{key}/install-upload")
async def component_install_upload(
    key: str,
    file: UploadFile = File(...),
    user: User = Depends(require_permission("tool:manage")),
) -> dict:
    """从上传的 zip 安装（离线环境的兜底路径）。"""
    name = (file.filename or "").lower()
    if not name.endswith(".zip"):
        raise ValidationError("请上传 .zip 安装包")
    tmp = await CS.save_upload(file)
    try:
        ret = CS.start_install(key, source="upload", local_zip=Path(tmp))
    except Exception as e:
        # 启动失败（如平台不支持/已在安装）时别留下临时文件
        _unlink_quiet(tmp)
        await _audit("component.install_upload", key, result="failure", error=str(e)[:200])
        raise
    if not ret.get("started"):
        # 任务没起来（重复点击/已装同版本）：后台协程不会接管这个文件，这里自己清
        _unlink_quiet(tmp)
    await _audit("component.install_upload", key)
    return ret


@router.get("/{key}/progress")
async def component_progress(
    key: str,
    user: User = Depends(require_permission("tool:read")),
) -> dict:
    """轮询安装进度（含 phase/received/total/error）。"""
    st = CS.status(key)   # 顺带校验 key 存在
    return {**st["progress"], "installed": st["installed"], "executable": st["executable"]}


@router.delete("/{key}")
async def component_uninstall(
    key: str,
    user: User = Depends(require_permission("tool:manage")),
) -> dict:
    try:
        ret = CS.uninstall(key)
    except Exception as e:
        await _audit("component.uninstall", key, result="failure", error=str(e)[:200])
        raise
    await _audit("component.uninstall", key)
    return ret
