"""技能包服务：从本地 zip / URL / GitHub 导入技能包（参考 Claud Agent Skills 格式）。

技能包结构：
  SKILL.md   —— YAML frontmatter(name/description) + 正文（Markdown 说明）
  scripts/   —— 可选，Python 脚本（作为可执行工具，默认禁用）
  其他资源文件

安全：
- 限制解压总量/文件数，防路径穿越
- URL 导入走 SSRF 校验
- 脚本执行默认禁用，需显式开启
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("skill_pack")

MAX_TOTAL_BYTES = 50 * 1024 * 1024  # 解压后总量上限
MAX_FILES = 500

# 运行时/入口发现统一收敛到 skill_runtime（此处 re-export，保持历史 import 兼容）
from app.services.skill_runtime import (  # noqa: E402
    NODE_MANIFEST,  # noqa: F401
    NODE_MODULES,  # noqa: F401
    SCRIPT_DIRS,
    SCRIPT_RUNTIMES,
    has_node_deps,  # noqa: F401
    load_manifest,
    scan_entry_scripts,
)


@dataclass
class ParsedPack:
    name: str = ""
    description: str = ""
    body_md: str = ""
    version: str | None = None
    files: list[dict] = field(default_factory=list)  # [{path,size}]
    entry_scripts: list[dict] = field(default_factory=list)  # [{name,path,description}]
    pack_dir: str = ""
    content_hash: str = ""
    script_meta: dict = field(default_factory=dict)  # 临时：脚本清单声明（解析用）


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 YAML frontmatter，返回 (meta, body)。"""
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    raw, body = m.group(1), m.group(2)
    meta: dict = {}
    try:
        import yaml

        meta = yaml.safe_load(raw) or {}
    except Exception:  # noqa: BLE001
        # 退化：手撸 key: value
        for line in raw.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    return meta, body


def _safe_member(name: str) -> bool:
    """拒绝路径穿越与绝对路径。"""
    if name.startswith("/") or name.startswith("\\"):
        return False
    parts = re.split(r"[\\/]", name)
    return ".." not in parts


def parse_skill_md(text: str) -> tuple[dict, str]:
    return _parse_frontmatter(text)


def list_skill_paths(data: bytes) -> list[str]:
    """列出 zip 内所有 SKILL.md 的相对路径（识别单技能/多技能仓库）。

    单技能仓库 → ["SKILL.md"] 或 ["xxx/SKILL.md"]；
    多技能仓库 → ["skills/data-query/SKILL.md", "skills/weather-query/SKILL.md", ...]。
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValueError(f"不是有效的 zip 文件: {e}") from e
    out: list[str] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if Path(name).name.upper() == "SKILL.MD" and _safe_member(name):
            out.append(name)
    return out


def _strip_common_root(paths: list[str]) -> list[str]:
    """去掉 GitHub zip 的外层单一公共目录（如 `repo-main/`）。不剥 `.`/`..`。"""
    if not paths:
        return paths
    parts = [p.split("/") for p in paths]
    # 所有路径都至少两层，且首段相同且非 `.`/`..` → 剥掉首段
    if all(len(p) > 1 for p in parts):
        first = parts[0][0]
        if first not in ("", ".", "..") and all(p[0] == first for p in parts):
            return ["/".join(p[1:]) for p in parts]
    return paths


def _common_root(paths: list[str]) -> str:
    """返回所有路径共同的单层根段（无则空串）。不把 `.`/`..` 当根（防绕过路径校验）。"""
    if not paths:
        return ""
    parts = [p.split("/") for p in paths]
    if all(len(p) > 1 for p in parts):
        first = parts[0][0]
        if first not in ("", ".", "..") and all(p[0] == first for p in parts):
            return first
    return ""


def skill_subdirs(data: bytes) -> list[str]:
    """把 SKILL.md 路径转成可作 subpath 的目录（去掉 /SKILL.md，去掉 GitHub 外层根）。

    返回如 [""] 表示单技能在根；["skills/data-query", "skills/weather-query"] 表示多技能。
    """
    paths = _strip_common_root(list_skill_paths(data))
    out: list[str] = []
    for p in paths:
        parent = p.rsplit("/", 1)[0] if "/" in p else ""
        if parent not in out:
            out.append(parent)
    return out


def extract_zip(data: bytes, tenant_id: int, *, subpath: str | None = None) -> ParsedPack:
    """安全解压 zip 到技能包目录，返回解析结果。

    subpath：多技能仓库时指定只提取的子目录（如 "skills/data-query"），
    提取后摊平到包根（SKILL.md 在根、scripts/ 在根）。
    多技能仓库不指定 subpath → 报错（避免静默只取其中一个）。
    """
    pack = ParsedPack()

    # 多技能保护：识别 zip 内 SKILL.md 分布（路径已剥外层根）
    all_paths = _strip_common_root(list_skill_paths(data))
    dirs = []
    for p in all_paths:
        d = p.rsplit("/", 1)[0] if "/" in p else ""
        if d not in dirs:
            dirs.append(d)
    prefix = (subpath or "").strip("/").replace("\\", "/")
    if not prefix and len(dirs) > 1:
        raise ValueError(
            "该仓库包含多个技能，请用 subpath 指定其中一个："
            + "、".join(d or "(根目录)" for d in dirs)
        )
    # 单技能（唯一 SKILL.md 且不在根）：自动把其所在目录当作提取前缀，摊平到根
    if not prefix and len(dirs) == 1 and dirs[0]:
        prefix = dirs[0]

    # 计算 zip 全体的公共单层根（GitHub zip 的 repo-main/），循环内统一剥离
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValueError(f"不是有效的 zip 文件: {e}") from e
    root_seg = _common_root([i.filename.replace("\\", "/") for i in zf.infolist() if not i.is_dir()])
    root_prefix = (root_seg + "/") if root_seg else ""

    dest_root = settings.skill_pack_path / str(tenant_id)
    dest_root.mkdir(parents=True, exist_ok=True)
    key = uuid.uuid4().hex
    dest = dest_root / key
    dest.mkdir(parents=True, exist_ok=True)

    total = 0
    count = 0
    skill_md = None

    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        # 先剥掉公共根，再匹配 prefix
        stripped = name[len(root_prefix):] if root_prefix and name.startswith(root_prefix) else name
        if prefix:
            if not (stripped == prefix or stripped.startswith(prefix + "/")):
                continue
            rel = stripped[len(prefix) + 1:] if len(stripped) > len(prefix) else ""
            if not rel:
                continue
        else:
            rel = stripped
        if not _safe_member(rel):
            raise ValueError(f"非法路径（疑似路径穿越）: {rel}")
        count += 1
        total += info.file_size
        if count > MAX_FILES:
            raise ValueError(f"文件数超过上限 {MAX_FILES}")
        if total > MAX_TOTAL_BYTES:
            raise ValueError("解压总量超过上限")

        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            content = src.read()
            out.write(content)
        pack.files.append({"path": rel, "size": info.file_size})

        low = rel.lower()
        if Path(rel).name.upper() == "SKILL.MD" or low.endswith("skill.md"):
            skill_md = content.decode("utf-8", errors="ignore")
        # 脚本清单（可选）：scripts/manifest.json 或 manifest.json 的 scripts 字段
        if low.endswith("manifest.json"):
            try:
                mf = json.loads(content.decode("utf-8", errors="ignore"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                mf = None
            if isinstance(mf, dict):
                for key_ in ("scripts", "entry_scripts"):
                    if isinstance(mf.get(key_), list):
                        pack.script_meta[key_] = mf[key_]

    if skill_md is not None:
        meta, body = parse_skill_md(skill_md)
        pack.name = str(meta.get("name") or "")
        pack.description = str(meta.get("description") or "")
        pack.version = str(meta["version"]) if meta.get("version") else None
        pack.body_md = body
        # SKILL.md frontmatter 里的 scripts 声明（优先）
        if isinstance(meta.get("scripts"), list):
            pack.script_meta["scripts"] = meta["scripts"]

    # 入口脚本：优先读 skill.yaml 清单，否则扫描 scripts/ 与 tools/（多运行时：py/node/sh）
    pack.entry_scripts = scan_entry_scripts(dest, manifest=load_manifest(dest))

    # 回填脚本描述：meta 里按 name 匹配
    decl: dict[str, str] = {}
    for key_ in ("scripts", "entry_scripts"):
        for item in pack.script_meta.get(key_) or []:
            if isinstance(item, dict) and item.get("name"):
                decl[str(item["name"])] = str(item.get("description") or "")
    if decl:
        for scr in pack.entry_scripts:
            if not scr.get("description") and scr["name"] in decl:
                scr["description"] = decl[scr["name"]]

    pack.pack_dir = f"{tenant_id}/{key}"
    # hash 掺入 subpath：同 zip 的不同子技能不能被去重误判为同一包
    hash_input = data + (b"#" + subpath.encode() if subpath else b"")
    pack.content_hash = hashlib.sha256(hash_input).hexdigest()
    return pack


@dataclass
class ParsedCollection:
    """多技能仓库：整包保留结构，解析出各子技能。"""
    name: str = ""
    description: str = ""
    version: str | None = None
    pack_dir: str = ""
    content_hash: str = ""
    files: list[dict] = field(default_factory=list)          # [{path,size}] 整包文件
    subs: list[dict] = field(default_factory=list)           # [{subpath,name,description,body_md,entry_scripts}]


def parse_collection(data: bytes, tenant_id: int) -> ParsedCollection:
    """把多技能 zip 整包解压到一个目录（保留 skills/*/ 结构），并解析出各子技能。

    - 保留原结构（不摊平），文件路径为剥掉 GitHub 外层公共根后的相对路径。
    - 每个 SKILL.md 视为一个子技能；其所在目录即 subpath。
    """
    col = ParsedCollection()

    subs_paths = _strip_common_root(list_skill_paths(data))
    if len(subs_paths) < 1:
        raise ValueError("该包未包含任何 SKILL.md")

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValueError(f"不是有效的 zip 文件: {e}") from e
    root_seg = _common_root([i.filename.replace("\\", "/") for i in zf.infolist() if not i.is_dir()])
    root_prefix = (root_seg + "/") if root_seg else ""

    dest_root = settings.skill_pack_path / str(tenant_id)
    dest_root.mkdir(parents=True, exist_ok=True)
    key = uuid.uuid4().hex
    dest = dest_root / key
    dest.mkdir(parents=True, exist_ok=True)

    total = 0
    count = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        rel = name[len(root_prefix):] if root_prefix and name.startswith(root_prefix) else name
        if not rel:
            continue
        if not _safe_member(rel):
            raise ValueError(f"非法路径（疑似路径穿越）: {rel}")
        count += 1
        total += info.file_size
        if count > MAX_FILES:
            raise ValueError(f"文件数超过上限 {MAX_FILES}")
        if total > MAX_TOTAL_BYTES:
            raise ValueError("解压总量超过上限")
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            out.write(src.read())
        col.files.append({"path": rel, "size": info.file_size})

    # 组装子技能
    for sp in subs_paths:
        subpath = sp.rsplit("/", 1)[0] if "/" in sp else ""
        md_file = dest / sp
        try:
            text = md_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        meta, body = parse_skill_md(text)
        cname = str(meta.get("name") or "").strip()
        if not cname:
            m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
            cname = (m.group(1).strip() if m else (subpath.rsplit("/", 1)[-1] or "skill")).strip()
        # 该子技能目录下的脚本（多运行时；子目录若有 skill.yaml 则以其为准）
        pre = (subpath + "/") if subpath else ""
        sub_root = (dest / subpath) if subpath else dest
        sub_manifest = load_manifest(sub_root) if sub_root.is_dir() else None
        if sub_manifest and sub_manifest.entrypoints:
            entry_scripts = scan_entry_scripts(sub_root, manifest=sub_manifest)
            for e in entry_scripts:  # 补全为包内完整相对路径
                e["path"] = f"{pre}{e['path']}"
        else:
            entry_scripts = []
            seen_names: set[str] = set()
            for f in col.files:
                fp = f["path"]
                if not fp.startswith(pre):
                    continue
                rel_sub = fp[len(pre):]
                parts = rel_sub.split("/")
                if len(parts) != 2 or parts[0] not in SCRIPT_DIRS:
                    continue
                rt = SCRIPT_RUNTIMES.get(Path(parts[1]).suffix.lower())
                if not rt:
                    continue
                name = Path(parts[1]).stem
                if name in seen_names:
                    continue
                seen_names.add(name)
                entry_scripts.append({"name": name, "path": fp, "description": "",
                                      "runtime": rt, "args": "stdin", "outputs": []})
        col.subs.append({
            "subpath": subpath, "name": cname,
            "description": str(meta.get("description") or ""),
            "body_md": body, "entry_scripts": entry_scripts,
        })

    col.pack_dir = f"{tenant_id}/{key}"
    col.content_hash = hashlib.sha256(data).hexdigest()
    return col



def _slugify(name: str) -> str:
    import time as _t

    s = re.sub(r"[^a-zA-Z0-9]+", "-", name or "").strip("-").lower()
    return s or f"skill-{int(_t.time())}"


def create_pack_from_markdown(
    text: str, tenant_id: int, *, extra_files: dict[str, str] | None = None,
) -> ParsedPack:
    """从 SKILL.md 原文创建技能包（无需 zip）。

    - text：SKILL.md 全文（YAML frontmatter + Markdown 正文），也兼容纯 Markdown
      （无 frontmatter 时用一级标题作 name）。
    - extra_files：可选附加文本文件 {相对路径: 内容}，如 {"scripts/run.py": "..."}。
    """
    text = (text or "").replace("\r\n", "\n")
    if not text.strip():
        raise ValueError("SKILL.md 内容不能为空")

    meta, body = parse_skill_md(text)
    name = str(meta.get("name") or "").strip()
    if not name:
        # 退化：取正文首个一级标题，再退化为首行
        m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        name = (m.group(1).strip() if m else (body.strip().splitlines() or [""])[0][:60]).strip()
    if not name:
        raise ValueError("无法确定技能名称：请在 frontmatter 提供 name，或写一个一级标题")

    description = str(meta.get("description") or "").strip()
    version = str(meta["version"]) if meta.get("version") else None

    dest_root = settings.skill_pack_path / str(tenant_id)
    dest_root.mkdir(parents=True, exist_ok=True)
    key = uuid.uuid4().hex
    dest = dest_root / key
    dest.mkdir(parents=True, exist_ok=True)

    files: list[dict] = []

    skill_md_bytes = text.encode("utf-8")
    (dest / "SKILL.md").write_bytes(skill_md_bytes)
    files.append({"path": "SKILL.md", "size": len(skill_md_bytes)})

    for rel, content in (extra_files or {}).items():
        if not rel or not _safe_member(rel):
            raise ValueError(f"非法附加文件路径：{rel}")
        content = (content or "").replace("\r\n", "\n")
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        files.append({"path": rel, "size": len(content.encode("utf-8"))})
    # 入口脚本：优先读 skill.yaml 清单，否则扫描 scripts/ 与 tools/（多运行时）
    entry_scripts = scan_entry_scripts(dest, manifest=load_manifest(dest))

    pack = ParsedPack(
        name=name, description=description, body_md=body, version=version,
        files=files, entry_scripts=entry_scripts,
        pack_dir=f"{tenant_id}/{key}",
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    # SKILL.md frontmatter 里的 scripts 声明 → 回填脚本描述
    decl: dict[str, str] = {}
    for item in (meta.get("scripts") if isinstance(meta.get("scripts"), list) else []) or []:
        if isinstance(item, dict) and item.get("name"):
            decl[str(item["name"])] = str(item.get("description") or "")
    if decl:
        for scr in pack.entry_scripts:
            if not scr.get("description") and scr["name"] in decl:
                scr["description"] = decl[scr["name"]]
    return pack


async def _unique_slug(db, tenant_id: int, base: str) -> str:
    from sqlalchemy import select

    from app.models import Skill

    base_slug = _slugify(base or "skill-pack")
    slug = base_slug
    n = 1
    while (
        await db.execute(select(Skill).where(Skill.tenant_id == tenant_id, Skill.slug == slug))
    ).scalar_one_or_none():
        slug = f"{base_slug}-{n}"
        n += 1
    return slug


async def _find_duplicate(db, tenant_id: int, content_hash: str | None):
    from sqlalchemy import select

    from app.models import SkillPackage

    if not content_hash:
        return None
    return (
        await db.execute(
            select(SkillPackage).where(
                SkillPackage.tenant_id == tenant_id, SkillPackage.content_hash == content_hash
            )
        )
    ).scalars().first()


async def register_pack(
    db, *, tenant_id: int, owner_id: int, pack: ParsedPack,
    source_type: str, source_uri: str | None, upgrade_skill_id: int | None = None,
):
    """把解析好的技能包落库为 Skill + SkillPackage。

    upgrade_skill_id 非空则「更新」已有技能（覆盖正文与包字段，清旧目录）；否则新建。
    """
    from sqlalchemy import select

    from app.models import Skill, SkillPackage

    if upgrade_skill_id is not None:
        sk = await db.get(Skill, upgrade_skill_id)
        if not sk or sk.tenant_id != tenant_id:
            raise ValueError("待更新的技能不存在")
        pkg = (
            await db.execute(
                select(SkillPackage).where(SkillPackage.skill_id == upgrade_skill_id)
            )
        ).scalars().first()
        old_dir = pkg.pack_dir if pkg else None
        if pack.name:
            sk.name = pack.name
        if pack.description:
            sk.description = pack.description
        sk.body_md = pack.body_md
        sk.prompt_template = pack.body_md or sk.prompt_template
        if pkg is None:
            pkg = SkillPackage(
                tenant_id=tenant_id, skill_id=sk.id, name=pack.name or sk.name,
                source_type=source_type, source_uri=source_uri,
                pack_dir=pack.pack_dir, content_hash=pack.content_hash, version=pack.version,
                manifest={"name": pack.name, "description": pack.description},
                files=pack.files, entry_scripts=pack.entry_scripts,
                scripts_enabled=False, created_by=owner_id,
            )
            db.add(pkg)
            await db.flush()
            sk.package_id = pkg.id
        else:
            pkg.name = pack.name or pkg.name
            pkg.source_type = source_type
            pkg.source_uri = source_uri
            pkg.pack_dir = pack.pack_dir
            pkg.content_hash = pack.content_hash
            pkg.version = pack.version
            pkg.files = pack.files
            pkg.entry_scripts = pack.entry_scripts
            pkg.manifest = {"name": pack.name, "description": pack.description}
        await db.flush()
        if old_dir and old_dir != pack.pack_dir:
            remove_pack_dir(old_dir)
        return sk, pkg

    slug = await _unique_slug(db, tenant_id, pack.name or "skill-pack")
    sk = Skill(
        tenant_id=tenant_id,
        owner_id=owner_id,
        name=pack.name or slug,
        slug=slug,
        description=pack.description,
        kind="prompt_pack",
        prompt_template=pack.body_md or "",
        source="package",
        body_md=pack.body_md,
    )
    db.add(sk)
    await db.flush()

    pkg = SkillPackage(
        tenant_id=tenant_id,
        skill_id=sk.id,
        name=pack.name,
        source_type=source_type,
        source_uri=source_uri,
        content_hash=pack.content_hash,
        version=pack.version,
        pack_dir=pack.pack_dir,
        manifest={"name": pack.name, "description": pack.description},
        files=pack.files,
        entry_scripts=pack.entry_scripts,
        scripts_enabled=False,  # 默认不允许执行脚本
        created_by=owner_id,
    )
    db.add(pkg)
    await db.flush()
    sk.package_id = pkg.id
    await db.flush()
    return sk, pkg


async def _import_common(
    db, *, tenant_id: int, owner_id: int, pack: ParsedPack,
    source_type: str, source_uri: str | None, upgrade: bool,
) -> dict:
    """统一入口：去重判定 → 新建或更新。"""
    dup = await _find_duplicate(db, tenant_id, pack.content_hash)
    if dup and not upgrade:
        remove_pack_dir(pack.pack_dir)  # 丢弃刚解压的重复目录
        from app.models import Skill

        sk = await db.get(Skill, dup.skill_id) if dup.skill_id else None
        return {
            "duplicate": True,
            "existing_skill_id": dup.skill_id,
            "existing_skill_name": sk.name if sk else "",
        }
    target = dup.skill_id if (dup and upgrade) else None
    sk, pkg = await register_pack(
        db, tenant_id=tenant_id, owner_id=owner_id, pack=pack,
        source_type=source_type, source_uri=source_uri, upgrade_skill_id=target,
    )
    return {"duplicate": False, "skill_id": sk.id, "package_id": pkg.id,
            "name": sk.name, "files": len(pack.files)}


async def import_from_upload(
    db, *, tenant_id: int, owner_id: int, filename: str, data: bytes,
    subpath: str | None = None, upgrade: bool = False,
) -> dict:
    subs = skill_subdirs(data)
    if subpath is None and len(subs) > 1:
        return {"multi_skill": True, "sub_skills": subs,
                "message": f"该包含 {len(subs)} 个技能，请用 subpath 指定其中一个"}
    pack = extract_zip(data, tenant_id, subpath=subpath)
    return await _import_common(
        db, tenant_id=tenant_id, owner_id=owner_id, pack=pack,
        source_type="zip",
        source_uri=(f"{filename}#{subpath}" if subpath else filename), upgrade=upgrade,
    )


async def import_from_url(
    db, *, tenant_id: int, owner_id: int, url: str,
    subpath: str | None = None, upgrade: bool = False,
) -> dict:
    data = await fetch_url_zip(url)
    subs = skill_subdirs(data)
    if subpath is None and len(subs) > 1:
        return {"multi_skill": True, "url": url, "sub_skills": subs,
                "message": f"该仓库含 {len(subs)} 个技能，请用 subpath 指定其中一个"}
    pack = extract_zip(data, tenant_id, subpath=subpath)
    source_type = "github" if "github" in url else "url"
    return await _import_common(
        db, tenant_id=tenant_id, owner_id=owner_id, pack=pack,
        source_type=source_type,
        source_uri=(f"{url}#{subpath}" if subpath else url), upgrade=upgrade,
    )


async def import_from_markdown(
    db, *, tenant_id: int, owner_id: int, text: str,
    filename: str | None = None, upgrade: bool = False,
) -> dict:
    """从 SKILL.md 原文创建技能包（粘贴文本或上传单个 .md）。"""
    pack = create_pack_from_markdown(text, tenant_id)
    return await _import_common(
        db, tenant_id=tenant_id, owner_id=owner_id, pack=pack,
        source_type="markdown", source_uri=filename or (pack.name + ".md"), upgrade=upgrade,
    )


async def upgrade_from_url(
    db, *, tenant_id: int, owner_id: int, url: str, skill_id: int,
    subpath: str | None = None,
):
    """用 URL/GitHub 覆盖已有技能包（拉最新 zip → 解析 → 覆盖 skill/package）。

    subpath：多技能仓库必须指定，否则 extract_zip 会因「包含多个技能」拒绝提取；
    来源会以 `url#subpath` 记录，保证「已安装」匹配与后续升级都能还原子技能。

    调用方负责校验 skill_id 属于该租户并记录审计。
    返回 (skill, package)。
    """
    data = await fetch_url_zip(url)
    pack = extract_zip(data, tenant_id, subpath=subpath)
    source_type = "github" if "github" in url else "url"
    source_uri = f"{url}#{subpath}" if subpath else url
    return await register_pack(
        db, tenant_id=tenant_id, owner_id=owner_id, pack=pack,
        source_type=source_type, source_uri=source_uri, upgrade_skill_id=skill_id,
    )


async def register_collection(
    db, *, tenant_id: int, owner_id: int, col: ParsedCollection,
    source_type: str, source_uri: str | None, name_hint: str | None = None,
):
    """把整包解析结果落库：一个父 Skill(kind=collection) + N 个子 Skill + 共享 SkillPackage。"""
    from app.models import Skill, SkillPackage

    col_name = name_hint or source_uri or "skill-collection"
    # 集合名：优先用来源仓库名
    if source_uri:
        m = re.match(r"https?://github\.com/([^/]+/[^/?#]+?)(?:\.git)?/?$", source_uri)
        if m:
            col_name = m.group(1)
    parent_slug = await _unique_slug(db, tenant_id, col_name or "collection")
    parent = Skill(
        tenant_id=tenant_id, owner_id=owner_id, name=col_name,
        slug=parent_slug, kind="collection", source="package",
        description=f"技能集合（{len(col.subs)} 个子技能）",
    )
    db.add(parent)
    await db.flush()

    pkg = SkillPackage(
        tenant_id=tenant_id, skill_id=parent.id, name=col_name,
        source_type=source_type, source_uri=source_uri,
        content_hash=col.content_hash, version=col.version,
        pack_dir=col.pack_dir,
        manifest={"name": col_name, "children": len(col.subs)},
        files=col.files,
        entry_scripts=[s for sub in col.subs for s in sub["entry_scripts"]],
        scripts_enabled=False, created_by=owner_id,
    )
    db.add(pkg)
    await db.flush()
    parent.package_id = pkg.id
    await db.flush()

    children: list[dict] = []
    for sub in col.subs:
        cslug = await _unique_slug(db, tenant_id, sub["name"] or sub["subpath"] or "skill")
        child = Skill(
            tenant_id=tenant_id, owner_id=owner_id, name=sub["name"],
            slug=cslug, kind="prompt_pack", source="package",
            description=sub["description"], body_md=sub["body_md"],
            prompt_template=sub["body_md"], package_id=pkg.id,
            parent_id=parent.id, collection_subpath=sub["subpath"],
        )
        db.add(child)
        await db.flush()
        children.append({"id": child.id, "name": child.name, "subpath": sub["subpath"]})
    return parent, pkg, children


async def _import_collection_common(
    db, *, tenant_id: int, owner_id: int, data: bytes,
    source_type: str, source_uri: str | None, name_hint: str | None = None,
) -> dict:
    """整包导入为技能集合（含去重）。"""
    col = parse_collection(data, tenant_id)
    dup = await _find_duplicate(db, tenant_id, col.content_hash)
    if dup:
        remove_pack_dir(col.pack_dir)
        from app.models import Skill

        sk = await db.get(Skill, dup.skill_id) if dup.skill_id else None
        return {"duplicate": True, "existing_skill_id": dup.skill_id,
                "existing_skill_name": sk.name if sk else ""}
    parent, pkg, children = await register_collection(
        db, tenant_id=tenant_id, owner_id=owner_id, col=col,
        source_type=source_type, source_uri=source_uri, name_hint=name_hint,
    )
    return {"collection": True, "skill_id": parent.id, "package_id": pkg.id,
            "name": parent.name, "count": len(children), "children": children}


async def import_collection_from_url(
    db, *, tenant_id: int, owner_id: int, url: str,
) -> dict:
    data = await fetch_url_zip(url)
    source_type = "github" if "github" in url else "url"
    return await _import_collection_common(
        db, tenant_id=tenant_id, owner_id=owner_id, data=data,
        source_type=source_type, source_uri=url,
    )


async def import_collection_from_upload(
    db, *, tenant_id: int, owner_id: int, filename: str, data: bytes,
) -> dict:
    return await _import_collection_common(
        db, tenant_id=tenant_id, owner_id=owner_id, data=data,
        source_type="zip", source_uri=filename, name_hint=Path(filename).stem,
    )




def remove_pack_dir(pack_dir: str | None) -> None:
    """删除技能包磁盘目录（强校验位于 skill_pack_path 之下，防越界删除）。"""
    if not pack_dir:
        return
    import shutil

    root = settings.skill_pack_path.resolve()
    target = (settings.skill_pack_path / pack_dir).resolve()
    if root == target or root not in target.parents:
        logger.warning("skill_pack_remove_refused", pack_dir=pack_dir)
        return
    shutil.rmtree(target, ignore_errors=True)


async def gc_orphan_packs(db, *, min_age_hours: int = 1, dry_run: bool = False) -> dict:
    """清理孤儿技能包目录：磁盘上存在、但 DB 无任何记录、且超过 min_age_hours。

    用途：兜底回收异常中断/历史版本遗留的解压目录（如测试污染、导入中途失败）。
    **保留**刚创建（可能正在入库）的目录，避免与并发的导入流程竞争。
    返回 {removed, kept, freed_mb, errors, dry_run}。
    """
    import shutil
    import time as _t

    from sqlalchemy import select as _select

    from app.models import SkillPackage

    root = settings.skill_pack_path
    if not root.is_dir():
        return {"removed": 0, "kept": 0, "freed_mb": 0.0, "errors": 0}

    rows = (await db.execute(_select(SkillPackage.pack_dir).where(SkillPackage.pack_dir.is_not(None)))).all()
    known = {str(r[0]) for r in rows}
    cutoff = _t.time() - min_age_hours * 3600

    removed = kept = errors = 0
    freed = 0
    for tenant_dir in root.iterdir():
        if not tenant_dir.is_dir():
            continue
        for pack in tenant_dir.iterdir():
            if not pack.is_dir():
                continue
            key = f"{tenant_dir.name}/{pack.name}"
            if key in known:
                continue
            try:
                if pack.stat().st_mtime > cutoff:  # 太新：可能在入库中，保留
                    kept += 1
                    continue
                size = sum(f.stat().st_size for f in pack.rglob("*") if f.is_file())
                if not dry_run:
                    shutil.rmtree(pack, ignore_errors=True)
                removed += 1
                freed += size
            except OSError:
                errors += 1
        try:
            if not dry_run and tenant_dir.is_dir() and not any(tenant_dir.iterdir()):
                tenant_dir.rmdir()
        except OSError:
            pass
    logger.info("skill_pack_gc", removed=removed, kept=kept, freed_mb=round(freed / 1024 / 1024, 2))
    return {"removed": removed, "kept": kept, "freed_mb": round(freed / 1024 / 1024, 2),
            "errors": errors, "dry_run": dry_run}


def resolve_github_zip(url: str) -> str:
    """把 GitHub 仓库地址转为可下载的 zip 直链（默认 main 分支，仅作首选项）。

    注意：仓库默认分支可能是 master 等，故真实下载走 fetch_url_zip 的候选回退逻辑，
    本函数只用于显式构造 main 直链的兜底场景。
    """
    if "github.com" in url and not url.endswith(".zip"):
        m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", url)
        if m:
            user, repo = m.group(1), m.group(2)
            return f"https://codeload.github.com/{user}/{repo}/zip/refs/heads/main"
    return url


def _github_repo_slug(url: str) -> tuple[str, str] | None:
    """从 GitHub 仓库 URL 解析 (owner, repo)；非仓库地址返回 None。"""
    if "github.com" not in url or url.endswith(".zip"):
        return None
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        return None
    return m.group(1), m.group(2)


async def _github_default_branch(owner: str, repo: str) -> str | None:
    """查 GitHub 仓库默认分支（core API）。失败返回 None。"""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(f"https://api.github.com/repos/{owner}/{repo}",
                            headers={"User-Agent": "rag-platform", "Accept": "application/vnd.github+json"})
            if r.status_code == 200:
                return (r.json() or {}).get("default_branch") or None
    except Exception:  # noqa: BLE001
        return None
    return None


async def fetch_url_zip(url: str) -> bytes:
    """下载 URL 的 zip（走 SSRF 校验）。

    GitHub 仓库：查默认分支后构造 codeload 直链；分支探测失败则依次回退 main/master。
    """
    import httpx

    from app.agents.tools.builtin import _check_url

    slug = _github_repo_slug(url)
    candidates: list[str] = []
    if slug:
        owner, repo = slug
        branch = await _github_default_branch(owner, repo)
        if branch:
            candidates.append(f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}")
        for b in ("main", "master"):
            u = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{b}"
            if u not in candidates:
                candidates.append(u)
    else:
        candidates.append(resolve_github_zip(url))

    last_err: str | None = None
    for u in candidates:
        err = _check_url(u, None)
        if err:
            raise ValueError(err)
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
                resp = await c.get(u)
                if resp.status_code == 200 and resp.content[:2] == b"PK":
                    return resp.content
                last_err = f"HTTP {resp.status_code}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)[:150]
    raise ValueError(f"下载技能包失败：{last_err or '未知错误'}（可能是仓库不存在或非公开）")
