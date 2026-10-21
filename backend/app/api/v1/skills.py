"""技能接口：能力包(prompt_pack)与工具技能(tool)的 CRUD。"""
from __future__ import annotations

import re
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.config import settings
from app.middleware.auth_dep import load_principal_set, require_permission
from app.services.audit_service import audited
from app.models import Skill, User
from app.schemas.agent import SkillCreate, SkillOut, SkillUpdate

router = APIRouter(prefix="/skills", tags=["skill"])


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return s or f"skill-{int(time.time())}"


async def _get_pkg_for_skill(db: AsyncSession, skill_id: int, tenant_id: int):
    """按 skill_id 取技能包：先按 Skill.package_id（集合子技能与父共享同一包），再回退 SkillPackage.skill_id。"""
    from app.models import SkillPackage

    sk = await db.get(Skill, skill_id)
    pkg = None
    if sk is not None and sk.package_id:
        pkg = (await db.execute(
            select(SkillPackage).where(
                SkillPackage.id == sk.package_id, SkillPackage.tenant_id == tenant_id
            )
        )).scalars().first()
    if pkg is None:
        pkg = (await db.execute(
            select(SkillPackage).where(
                SkillPackage.skill_id == skill_id, SkillPackage.tenant_id == tenant_id
            )
        )).scalars().first()
    return pkg


@router.get("", response_model=list[SkillOut])
async def list_skills(
    user: User = Depends(require_permission("skill:read")),
    db: AsyncSession = Depends(get_db),
) -> list[Skill]:
    rows = (
        await db.execute(
            select(Skill).where(Skill.tenant_id == user.tenant_id).order_by(Skill.id.desc())
        )
    ).scalars().all()
    return list(rows)


@router.post("", response_model=SkillOut)
@audited("skill.create", "skill")
async def create_skill(
    body: SkillCreate,
    user: User = Depends(require_permission("skill:edit")),
    db: AsyncSession = Depends(get_db),
) -> Skill:
    slug = body.slug or _slugify(body.name)
    exists = (
        await db.execute(
            select(Skill).where(Skill.tenant_id == user.tenant_id, Skill.slug == slug)
        )
    ).scalar_one_or_none()
    if exists:
        raise ConflictError("标识已存在")
    sk = Skill(
        tenant_id=user.tenant_id,
        owner_id=user.id,
        name=body.name,
        slug=slug,
        description=body.description,
        icon=body.icon,
        kind=body.kind,
        prompt_template=body.prompt_template,
        params_schema=body.params_schema,
        tool_ids=body.tool_ids,
        kb_ids=body.kb_ids,
        model_config_id=body.model_config_id,
        tool_def=body.tool_def,
    )
    db.add(sk)
    await db.flush()
    return sk


@router.patch("/{skill_id}", response_model=SkillOut)
@audited("skill.update", "skill", id_arg="skill_id")
async def update_skill(
    skill_id: int,
    body: SkillUpdate,
    user: User = Depends(require_permission("skill:edit")),
    db: AsyncSession = Depends(get_db),
) -> Skill:
    sk = await db.get(Skill, skill_id)
    if not sk or sk.tenant_id != user.tenant_id:
        raise NotFoundError("技能不存在")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(sk, k, v)
    await db.flush()
    return sk


@router.delete("/{skill_id}")
@audited("skill.delete", "skill", id_arg="skill_id")
async def delete_skill(
    skill_id: int,
    user: User = Depends(require_permission("skill:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    sk = await db.get(Skill, skill_id)
    if not sk or sk.tenant_id != user.tenant_id:
        raise NotFoundError("技能不存在")
    # 集合：先删子技能；共享技能包只删一次（按 package_id 定位，非 skill_id）
    if sk.kind == "collection":
        children = (await db.execute(
            select(Skill).where(Skill.parent_id == skill_id, Skill.tenant_id == user.tenant_id)
        )).scalars().all()
        for c in children:
            await db.delete(c)
        await db.flush()
    # 清理关联技能包的磁盘目录（按 package_id；子技能与父共享同一包）
    # 单独删子技能时**不删共享包**（父与其它子技能仍在用）
    is_child = bool(sk.parent_id)
    pkg = None
    if not is_child:
        if sk.package_id:
            pkg = (await db.execute(select(SkillPackage).where(SkillPackage.id == sk.package_id))).scalars().first()
        if not pkg:
            pkg = (await db.execute(select(SkillPackage).where(SkillPackage.skill_id == skill_id))).scalars().first()
    if pkg:
        skill_pack_service.remove_pack_dir(pkg.pack_dir)
        await db.delete(pkg)
    await db.delete(sk)
    await db.flush()
    return {"message": "已删除"}


# ==================== 技能包导入 ====================
from fastapi import File, UploadFile  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app.models import SkillPackage  # noqa: E402
from app.services import skill_pack_service  # noqa: E402


class ImportUrlIn(BaseModel):
    url: str
    subpath: str | None = None  # 多技能仓库时指定子技能目录（如 skills/data-query）


class ImportMarkdownIn(BaseModel):
    text: str            # SKILL.md 全文
    filename: str | None = None  # 可选，用于记录来源名


class UpgradeIn(BaseModel):
    url: str | None = None  # 从 URL 更新
    # 或用 zip 上传（走 /upgrade/upload）


class RunScriptIn(BaseModel):
    script: str
    args: dict = {}
    extra_args: list[str] = []  # 追加的命令行位置参数（如 build_all.mjs 的「输入 -o 输出」）


@router.post("/{skill_id}/scripts/run")
async def run_package_script(
    skill_id: int,
    body: RunScriptIn,
    user: User = Depends(require_permission("skill:execute")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """试跑技能包内的某个脚本（需先启用脚本执行）。"""
    from app.core.errors import ValidationError
    from app.agents.tools.registry import run_pack_script

    pkg = await _get_pkg_for_skill(db, skill_id, user.tenant_id)
    if not pkg or not pkg.pack_dir:
        raise NotFoundError("技能包不存在")
    if not pkg.scripts_enabled:
        raise ValidationError("该技能包未启用脚本执行，请先在「查看包」中开启")
    scripts = {s.get("name"): s for s in (pkg.entry_scripts or [])}
    if body.script not in scripts:
        raise ValidationError(f"脚本不在白名单内：{body.script}（可选：{', '.join(scripts) or '无'}）")
    res = await run_pack_script(pkg.pack_dir, scripts[body.script]["path"], body.args,
                                extra_args=body.extra_args, entry=scripts[body.script])
    return {"ok": not res.is_error, "content": res.content[:20000], "is_error": res.is_error}


@router.post("/import/collection/upload")
async def import_collection_upload(
    file: UploadFile = File(...),
    user: User = Depends(require_permission("skill:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """上传 zip 整体导入为「技能集合」（多技能仓库一次装为父+子）。"""
    from app.core.errors import ValidationError

    data = await file.read()
    try:
        r = await skill_pack_service.import_collection_from_upload(
            db, tenant_id=user.tenant_id, owner_id=user.id,
            filename=file.filename or "collection.zip", data=data,
        )
    except ValueError as e:
        raise ValidationError(str(e))
    from app.services.audit_service import record_audit

    if r.get("duplicate"):
        return r
    record_audit(db, action="skill.import_collection", resource_type="skill", resource_id=r["skill_id"])
    return r


@router.post("/import/upload")
async def import_upload(
    file: UploadFile = File(...),
    subpath: str | None = None,
    user: User = Depends(require_permission("skill:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """上传导入：.zip 走解包导入；单个 .md/.markdown 文本文件走 SKILL.md 导入。

    subpath：多技能 zip 时指定子技能目录（如 skills/data-query）。
    """
    from app.core.errors import ValidationError

    data = await file.read()
    fname = file.filename or "pack.zip"
    is_zip = data[:2] == b"PK"
    try:
        if not is_zip:
            text = data.decode("utf-8", errors="ignore")
            r = await skill_pack_service.import_from_markdown(
                db, tenant_id=user.tenant_id, owner_id=user.id, text=text, filename=fname,
            )
        else:
            r = await skill_pack_service.import_from_upload(
                db, tenant_id=user.tenant_id, owner_id=user.id,
                filename=fname, data=data, subpath=subpath,
            )
    except ValueError as e:
        raise ValidationError(str(e))
    from app.services.audit_service import record_audit

    if r.get("duplicate") or r.get("multi_skill"):
        return r
    record_audit(db, action="skill.import", resource_type="skill", resource_id=r["skill_id"])
    return r


@router.post("/import/markdown")
async def import_markdown(
    body: ImportMarkdownIn,
    user: User = Depends(require_permission("skill:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """从粘贴的 SKILL.md 原文创建技能（无需打包 zip）。"""
    from app.core.errors import ValidationError

    try:
        r = await skill_pack_service.import_from_markdown(
            db, tenant_id=user.tenant_id, owner_id=user.id,
            text=body.text, filename=body.filename,
        )
    except ValueError as e:
        raise ValidationError(str(e))
    from app.services.audit_service import record_audit

    if r.get("duplicate"):
        return r
    record_audit(db, action="skill.import", resource_type="skill", resource_id=r["skill_id"])
    return r


class ParseMarkdownIn(BaseModel):
    text: str


@router.post("/parse/markdown")
async def parse_markdown(
    body: ParseMarkdownIn,
    user: User = Depends(require_permission("skill:read")),
) -> dict:
    """预解析 SKILL.md 原文（不落库）：返回 name/description/是否有 frontmatter，供前端预览。"""
    from app.services.skill_pack_service import parse_skill_md

    text = (body.text or "").replace("\r\n", "\n")
    meta, body_md = parse_skill_md(text)
    name = str(meta.get("name") or "").strip()
    has_fm = text.startswith("---")
    if not name:
        m = re.search(r"^#\s+(.+)$", body_md, re.MULTILINE)
        name = (m.group(1).strip() if m else "")
    return {
        "name": name,
        "description": str(meta.get("description") or "").strip(),
        "has_frontmatter": has_fm,
        "meta": {k: v for k, v in meta.items() if k != "metadata"} if isinstance(meta, dict) else {},
        "body_preview": body_md[:2000],
    }


@router.post("/import/collection")
async def import_collection(
    body: ImportUrlIn,
    user: User = Depends(require_permission("skill:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """把多技能仓库整体导入为一个「技能集合」（父技能 + N 个子技能）。"""
    from app.core.errors import ValidationError

    try:
        r = await skill_pack_service.import_collection_from_url(
            db, tenant_id=user.tenant_id, owner_id=user.id, url=body.url,
        )
    except ValueError as e:
        raise ValidationError(str(e))
    from app.services.audit_service import record_audit

    if r.get("duplicate"):
        return r
    record_audit(db, action="skill.import_collection", resource_type="skill", resource_id=r["skill_id"])
    return r


@router.post("/import/url")

async def import_url(
    body: ImportUrlIn,
    user: User = Depends(require_permission("skill:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.core.errors import ValidationError

    try:
        r = await skill_pack_service.import_from_url(
            db, tenant_id=user.tenant_id, owner_id=user.id, url=body.url, subpath=body.subpath
        )
    except ValueError as e:
        raise ValidationError(str(e))
    from app.services.audit_service import record_audit

    if r.get("duplicate") or r.get("multi_skill"):
        return r
    record_audit(db, action="skill.import", resource_type="skill", resource_id=r["skill_id"])
    return r


@router.post("/{skill_id}/upgrade/upload")
async def upgrade_upload(
    skill_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_permission("skill:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """用上传的 zip 更新已有技能包。"""
    from app.core.errors import ValidationError

    sk = await db.get(Skill, skill_id)
    if not sk or sk.tenant_id != user.tenant_id:
        raise NotFoundError("技能不存在")
    data = await file.read()
    try:
        pack = skill_pack_service.extract_zip(data, user.tenant_id)
        _, pkg = await skill_pack_service.register_pack(
            db, tenant_id=user.tenant_id, owner_id=user.id, pack=pack,
            source_type="zip", source_uri=file.filename, upgrade_skill_id=skill_id,
        )
    except ValueError as e:
        raise ValidationError(str(e))
    from app.services.audit_service import record_audit

    record_audit(db, action="skill.upgrade", resource_type="skill", resource_id=skill_id)
    return {"skill_id": skill_id, "package_id": pkg.id, "version": pkg.version,
            "files": len(pkg.files or [])}


@router.post("/{skill_id}/upgrade")
async def upgrade_from_url(
    skill_id: int,
    body: ImportUrlIn,
    user: User = Depends(require_permission("skill:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """用 URL/GitHub 更新已有技能包。"""
    from app.core.errors import ValidationError

    sk = await db.get(Skill, skill_id)
    if not sk or sk.tenant_id != user.tenant_id:
        raise NotFoundError("技能不存在")
    try:
        _, pkg = await skill_pack_service.upgrade_from_url(
            db, tenant_id=user.tenant_id, owner_id=user.id, url=body.url,
            skill_id=skill_id, subpath=body.subpath,
        )
    except ValueError as e:
        raise ValidationError(str(e))
    from app.services.audit_service import record_audit

    record_audit(db, action="skill.upgrade", resource_type="skill", resource_id=skill_id)
    return {"skill_id": skill_id, "package_id": pkg.id, "version": pkg.version,
            "files": len(pkg.files or [])}


@router.get("/{skill_id}/package")
async def get_package(
    skill_id: int,
    user: User = Depends(require_permission("skill:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    pkg = await _get_pkg_for_skill(db, skill_id, user.tenant_id)
    if not pkg:
        raise NotFoundError("该技能不是技能包")
    sk = await db.get(Skill, skill_id)
    # 依赖与能力探测（供 UI 显示「安装依赖」按钮、清单格式提示）
    from app.services.skill_runtime import MANIFEST_TEMPLATE, has_node_deps, has_python_deps

    root = settings.skill_pack_path / pkg.pack_dir if pkg.pack_dir else None
    caps = {
        "has_node_manifest": bool(root and (root / "package.json").is_file()),
        "has_python_reqs": bool(root and has_python_deps(root)),
        "needs_node_install": bool(root and has_node_deps(root)),
        "has_manifest": bool(root and any((root / n).is_file() for n in ("skill.yaml", "skill.yml", "skill.json"))),
    }
    return {
        "id": pkg.id, "name": pkg.name, "source_type": pkg.source_type,
        "source_uri": pkg.source_uri, "version": pkg.version, "files": pkg.files,
        "entry_scripts": pkg.entry_scripts, "scripts_enabled": pkg.scripts_enabled,
        "manifest": pkg.manifest, "body_md": sk.body_md if sk else None,
        "capabilities": caps, "manifest_template": MANIFEST_TEMPLATE,
    }


TEXT_EXT = {"md", "markdown", "txt", "py", "js", "ts", "json", "yaml", "yml", "csv", "html", "css", "sh", "xml", "ini", "toml"}


def _safe_in_pack(root, rel: str):
    """解析包内相对路径，防路径穿越。"""
    import re as _re

    if rel.startswith("/") or ".." in _re.split(r"[\\/]", rel):
        return None
    target = (root / rel).resolve()
    if root.resolve() not in target.parents:
        return None
    return target if target.is_file() else None


def _safe_pack_target(root, rel: str, *, allow_new: bool = False):
    """解析包内相对路径用于**写入**。allow_new=True 时允许目标文件不存在（新建）。"""
    import re as _re

    if not rel or rel.startswith("/") or ".." in _re.split(r"[\\/]", rel):
        return None
    root = root.resolve()
    target = (root / rel).resolve()
    if root not in target.parents:
        return None
    if target.is_dir():
        return None
    if not allow_new and not target.is_file():
        return None
    return target


@router.get("/{skill_id}/package/file")
async def read_package_file(
    skill_id: int,
    path: str,
    user: User = Depends(require_permission("skill:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """读取技能包内某个文件的内容（仅文本类，防路径穿越）。"""
    pkg = await _get_pkg_for_skill(db, skill_id, user.tenant_id)
    if not pkg or not pkg.pack_dir:
        raise NotFoundError("技能包不存在")
    from app.core.config import settings

    target = _safe_in_pack(settings.skill_pack_path / pkg.pack_dir, path)
    if not target:
        raise NotFoundError("文件不存在")
    ext = target.suffix.lower().lstrip(".")
    if ext not in TEXT_EXT:
        return {"path": path, "binary": True, "size": target.stat().st_size, "content": None}
    content = target.read_text(encoding="utf-8", errors="ignore")
    return {"path": path, "binary": False, "size": target.stat().st_size, "content": content[:200000]}


class WriteFileIn(BaseModel):
    path: str
    content: str


@router.put("/{skill_id}/package/file")
@audited("skill.write_file", "skill", id_arg="skill_id")
async def write_package_file(
    skill_id: int,
    body: WriteFileIn,
    user: User = Depends(require_permission("skill:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """编辑技能包内的文本文件（如 SKILL.md、scripts/*.py）。目标不存在时新建。"""
    pkg = await _get_pkg_for_skill(db, skill_id, user.tenant_id)
    if not pkg or not pkg.pack_dir:
        raise NotFoundError("技能包不存在")
    from app.core.config import settings

    if Path(body.path).suffix.lower().lstrip(".") not in TEXT_EXT:
        raise NotFoundError("仅可编辑文本文件")
    target = _safe_pack_target(settings.skill_pack_path / pkg.pack_dir, body.path, allow_new=True)
    if not target:
        raise NotFoundError("非法路径")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body.content, encoding="utf-8")
    # 若改的是 SKILL.md，同步更新技能正文
    if target.name.upper() == "SKILL.MD":
        sk = await db.get(Skill, skill_id)
        if sk:
            _, md_body = skill_pack_service.parse_skill_md(body.content)
            sk.body_md = md_body
            sk.prompt_template = md_body
            await db.flush()
    return {"message": "已保存"}


@router.post("/{skill_id}/package/scripts")
async def toggle_scripts(
    skill_id: int,
    enabled: bool,
    user: User = Depends(require_permission("skill:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    pkg = await _get_pkg_for_skill(db, skill_id, user.tenant_id)
    if not pkg:
        raise NotFoundError("该技能不是技能包")
    pkg.scripts_enabled = enabled
    # 重新扫描入口脚本：兼容历史包（旧版只收集 scripts/*.py；Node 技能在 tools/*.mjs）
    if enabled and pkg.pack_dir:
        from app.services.skill_runtime import load_manifest, scan_entry_scripts

        root = settings.skill_pack_path / pkg.pack_dir
        rescan = scan_entry_scripts(root, manifest=load_manifest(root))
        if rescan:
            # 保留已声明的描述
            desc = {s.get("name"): s.get("description") for s in (pkg.entry_scripts or [])}
            for s in rescan:
                s["description"] = desc.get(s["name"]) or s.get("description") or ""
            pkg.entry_scripts = rescan
    await db.flush()
    from app.services.audit_service import record_audit

    record_audit(db, action="skill.toggle_scripts", resource_type="skill_package", resource_id=pkg.id)
    return {"scripts_enabled": enabled, "entry_scripts": pkg.entry_scripts}


@router.post("/{skill_id}/package/install-deps")
async def install_package_deps(
    skill_id: int,
    user: User = Depends(require_permission("skill:edit")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """安装技能包依赖：Node（package.json → npm ci）、Python（requirements.txt → pip install）。"""
    import asyncio
    import shutil
    import sys

    pkg = await _get_pkg_for_skill(db, skill_id, user.tenant_id)
    if not pkg or not pkg.pack_dir:
        raise NotFoundError("技能包不存在")
    root = settings.skill_pack_path / pkg.pack_dir

    has_node = (root / "package.json").is_file()
    has_py = (root / "requirements.txt").is_file()
    if not has_node and not has_py:
        return {"ok": False, "message": "该技能包无依赖清单（package.json / requirements.txt）", "log": ""}

    cmds: list[list[str]] = []
    if has_py:
        cmds.append([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
    if has_node:
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if not npm:
            return {"ok": False, "message": "未检测到 npm（Node 未安装或不在 PATH）", "log": ""}
        cmds.append([npm, "ci", "--no-audit", "--no-fund"])

    logs: list[str] = []
    ok = True
    for cmd in cmds:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(root),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
            logs.append(f"$ {' '.join(cmd)}\n" + (out or b"").decode("utf-8", errors="ignore"))
            ok = ok and proc.returncode == 0
        except asyncio.TimeoutError:
            logs.append(f"$ {' '.join(cmd)}\n安装超时（>15 分钟）")
            ok = False
        except Exception as e:  # noqa: BLE001
            logs.append(f"$ {' '.join(cmd)}\n安装失败：{str(e)[:200]}")
            ok = False
    from app.services.audit_service import record_audit

    record_audit(db, action="skill.install_deps", resource_type="skill_package", resource_id=pkg.id)
    return {"ok": ok, "message": "依赖安装完成" if ok else "依赖安装失败", "log": "\n".join(logs)[-4000:]}


class BridgeCallIn(BaseModel):
    tool: str
    args: dict = {}


@router.post("/bridge/call")
async def bridge_call(
    body: BridgeCallIn,
    x_skill_token: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """能力桥接：技能脚本回调平台工具（凭短期 skill token）。

    以**调用者自己的权限集**执行被点名的工具，并强制工具白名单（token 内封存）。
    仅放行 kind=read 的工具，写/管理类一律拒绝——收窄脚本的回调能力面。
    """
    import asyncio

    from app.agents.tools.base import ToolContext
    from app.agents.tools.registry import registry, tool_allowed
    from app.core.security import decode_token
    from app.models import User

    if not x_skill_token:
        raise ValidationError("缺少技能令牌")
    try:
        payload = decode_token(x_skill_token)
    except Exception:  # noqa: BLE001
        raise ValidationError("技能令牌无效")
    if payload.get("type") != "skill":
        raise ValidationError("令牌类型不匹配")
    tenant_id = int(payload.get("tenant_id") or 0)
    user_id = int(payload.get("user_id") or 0)
    allowed = set(payload.get("allowed_tools") or [])
    if allowed and body.tool not in allowed:
        raise ValidationError(f"工具 {body.tool} 不在该技能的回调白名单内")

    tool = registry.get_builtin(body.tool) or registry.get_admin(body.tool)
    if not tool:
        raise ValidationError(f"未知工具：{body.tool}")
    if getattr(tool, "kind", "read") != "read":
        raise ValidationError(f"回调仅允许只读工具，{body.tool} 为写/管理类")

    user = await db.get(User, user_id)
    if not user or user.tenant_id != tenant_id:
        raise ValidationError("调用者不存在")
    ps = await load_principal_set(db, user)
    # 权限复核：以调用者权限集判定该工具是否可用
    from app.middleware.auth_dep import get_user_permission_codes

    perms = await get_user_permission_codes(db, user)
    if not tool_allowed(tool, perms):
        raise ValidationError(f"调用者无权使用工具 {body.tool}")

    ctx = ToolContext(db=db, ps=ps, tenant_id=tenant_id, user_id=user_id, config={})
    try:
        result = await asyncio.wait_for(tool.run(body.args or {}, ctx), timeout=60)
    except asyncio.TimeoutError:
        return {"ok": False, "is_error": True, "content": "工具执行超时"}
    return {"ok": not result.is_error, "is_error": result.is_error, "content": result.content[:20000],
            "data": result.data}
