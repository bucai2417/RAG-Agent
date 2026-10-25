"""文档接口：上传、列表、详情、进度、删除、重灌。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.concurrency import check_row_version
from app.core.db import get_db
from app.core.errors import NotFoundError, PermissionDeniedError, ValidationError
from app.core.logging import get_logger
from app.ingest.storage import get_storage
from app.middleware.auth_dep import load_principal_set, require_permission
from app.services.audit_service import audited
from app.models import Document, KBMember, KnowledgeBase, User
from app.schemas.kb import DocumentOut
from app.tasks.ingest_tasks import enqueue_document

router = APIRouter(prefix="/documents", tags=["document"])
logger = get_logger("document")

ALLOWED_EXT = {
    "pdf", "docx", "xlsx", "xls", "pptx", "md", "markdown",
    "html", "htm", "csv", "txt", "text", "log",
}

# 可在浏览器内联预览的 MIME
INLINE_MIME = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "txt": "text/plain; charset=utf-8",
    "text": "text/plain; charset=utf-8",
    "md": "text/plain; charset=utf-8",
    "markdown": "text/plain; charset=utf-8",
    "csv": "text/plain; charset=utf-8",
    "log": "text/plain; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "htm": "text/html; charset=utf-8",
}


@router.post("/upload", response_model=DocumentOut)
@audited("doc.upload", "document")
async def upload_document(
    kb_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_permission("doc:upload")),
    db: AsyncSession = Depends(get_db),
) -> Document:
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.tenant_id != user.tenant_id:
        raise NotFoundError("知识库不存在")
    await _ensure_edit(db, user, kb)

    filename = file.filename or "untitled"
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext not in ALLOWED_EXT:
        raise PermissionDeniedError(f"不支持的文件类型: .{ext}")

    data = await file.read()
    # 配额（0 = 不限）：在读入内存后、落盘前判断，避免先写文件再回滚留下孤儿文件
    from app.services import tenant_service as _ts

    await _ts.ensure_document_quota(db, user.tenant_id, len(data))

    storage = get_storage()
    file_key, content_hash = storage.save(tenant_id=user.tenant_id, filename=filename, data=data)

    # 去重：同库内已有相同内容哈希的文档，直接复用，不重复入库
    dup = (
        await db.execute(
            select(Document)
            .where(
                Document.kb_id == kb_id,
                Document.content_hash == content_hash,
                Document.tenant_id == user.tenant_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if dup is not None:
        try:
            storage.delete(file_key)  # 释放重复落盘的文件
        except Exception:  # noqa: BLE001
            pass
        logger.info("doc_dedup", kb_id=kb_id, existing_id=dup.id)
        return dup

    doc = Document(
        tenant_id=user.tenant_id,
        kb_id=kb_id,
        title=Path(filename).stem,
        source_type="upload",
        file_key=file_key,
        file_name=filename,
        file_ext=ext,
        file_size=len(data),
        content_hash=content_hash,
        status="pending",
        uploaded_by=user.id,
    )
    db.add(doc)
    await db.flush()
    doc_id = doc.id
    await db.commit()

    # 投递异步处理
    await enqueue_document(doc_id)
    logger.info("doc_uploaded", document_id=doc_id, kb_id=kb_id)
    return doc


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    kb_id: int,
    before_id: int | None = None,
    limit: int | None = None,
    user: User = Depends(require_permission("doc:read")),
    db: AsyncSession = Depends(get_db),
) -> list[Document]:
    """列出某知识库的文档（按 id 倒序）。

    分页：传 limit（+可选 before_id）时只返回该页；不传则返回全部（兼容文档管理页的
    全量展示/批量操作场景）。
    """
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.tenant_id != user.tenant_id:
        raise NotFoundError("知识库不存在")
    await _ensure_read(db, user, kb)
    stmt = select(Document).where(Document.kb_id == kb_id)
    if before_id:
        stmt = stmt.where(Document.id < before_id)
    stmt = stmt.order_by(Document.id.desc())
    if limit is not None:
        stmt = stmt.limit(max(1, min(limit, 500)))
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.get("/{doc_id}", response_model=DocumentOut)
async def get_document(
    doc_id: int,
    user: User = Depends(require_permission("doc:read")),
    db: AsyncSession = Depends(get_db),
) -> Document:
    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise NotFoundError("文档不存在")
    await _ensure_read(db, user, await db.get(KnowledgeBase, doc.kb_id))
    return doc


@router.get("/{doc_id}/content")
async def get_document_content(
    doc_id: int,
    user: User = Depends(require_permission("doc:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """读取文档原文内容（供在线预览）。优先读原文件，失败则用 chunk 拼接。"""
    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise NotFoundError("文档不存在")
    await _ensure_read(db, user, await db.get(KnowledgeBase, doc.kb_id))

    text = ""
    source = "chunks"
    # 1) 优先原始文件（文本类可直接读）
    if doc.file_key and doc.file_ext in ("txt", "text", "md", "markdown", "csv", "log", "html", "htm"):
        try:
            raw = get_storage().read(doc.file_key)
            for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
                try:
                    text = raw.decode(enc)
                    source = "file"
                    break
                except UnicodeDecodeError:
                    continue
        except Exception:  # noqa: BLE001
            pass

    # 2) 回退：按顺序拼 chunk
    if not text:
        from sqlalchemy import select as _select

        from app.models import Chunk

        rows = (
            await db.execute(
                _select(Chunk.content)
                .where(Chunk.doc_id == doc_id, Chunk.chunk_type != "parent")
                .order_by(Chunk.ordinal)
            )
        ).all()
        text = "\n\n".join(r[0] for r in rows)

    return {
        "id": doc.id,
        "title": doc.title,
        "file_name": doc.file_name,
        "file_ext": doc.file_ext,
        "page_count": doc.page_count,
        "source": source,
        "content": text[:200000],
    }


@router.get("/{doc_id}/raw")
async def get_document_raw(
    doc_id: int,
    user: User = Depends(require_permission("doc:read")),
    db: AsyncSession = Depends(get_db),
):
    """原始文件流（inline），供 PDF/图片/文本在线预览。Office 前端走下载或文本预览。"""
    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise NotFoundError("文档不存在")
    await _ensure_read(db, user, await db.get(KnowledgeBase, doc.kb_id))
    if not doc.file_key:
        raise NotFoundError("文档无原始文件")
    path = get_storage().path(doc.file_key)
    if not path.exists():
        raise NotFoundError("原始文件已丢失")
    ext = (doc.file_ext or "").lower()
    media_type = INLINE_MIME.get(ext, "application/octet-stream")
    disposition = "inline" if ext in INLINE_MIME else "attachment"
    return FileResponse(
        path,
        media_type=media_type,
        filename=doc.file_name or f"{doc.title}.{ext}",
        content_disposition_type=disposition,
    )


# 分块编辑/删除/拆分 入参
from pydantic import BaseModel as _ChunkBM  # noqa: E402


class ChunkUpdateIn(_ChunkBM):
    content: str


class ChunkSplitIn(_ChunkBM):
    offset: int  # 在 content 中的切分位置（字符）


async def _reembed_chunk(db: AsyncSession, chunk) -> None:
    """对单个可嵌入块重新生成向量并写回。失败不阻断（保留旧向量）。"""
    from app.providers.registry import get_embedding

    if chunk.chunk_type == "parent":
        return
    try:
        # 用该块所属 KB 指定的向量模型（若有），与入库/检索保持一致
        kb = await db.get(KnowledgeBase, chunk.kb_id)
        # 纯关键词库不做向量化：只更新关键词检索文本，避免触碰 embedding
        if kb and getattr(kb, "index_mode", "vector") == "keyword":
            chunk.tsv = chunk.content
            return
        kb_model_id = getattr(kb, "embedding_model_id", None) if kb else None
        emb_driver, rm = await get_embedding(db, tenant_id=chunk.tenant_id, config_id=kb_model_id)
        vecs = await emb_driver.embed([chunk.content], model=rm.model_name)
        if vecs:
            from app.retrieval.vector_store.store import get_vector_store

            await get_vector_store().add_embeddings(db, [(chunk.id, vecs[0])])
            chunk.tsv = chunk.content  # 关键词检索文本（bigram 分词在检索期进行）
    except Exception as e:  # noqa: BLE001
        logger.warning("chunk_reembed_failed", chunk_id=chunk.id, err=str(e)[:200])


async def _refresh_doc_count(db: AsyncSession, doc) -> None:
    from sqlalchemy import func as _func

    from app.models import Chunk as _Chunk

    cnt = (
        await db.execute(select(_func.count()).select_from(_Chunk).where(_Chunk.doc_id == doc.id))
    ).scalar_one()
    doc.chunk_count = int(cnt or 0)
    await _refresh_kb_counts(db, doc.kb_id)


async def _refresh_kb_counts(db: AsyncSession, kb_id: int) -> None:
    """据实重算知识库的文档数与分块数（删除/新增后调用，避免计数只增不减）。"""
    from sqlalchemy import func as _func

    from app.models import Chunk as _Chunk

    kb = await db.get(KnowledgeBase, kb_id)
    if not kb:
        return
    doc_total = (
        await db.execute(
            select(_func.count()).select_from(Document).where(
                Document.kb_id == kb_id, Document.status != "deleted"
            )
        )
    ).scalar_one()
    chunk_total = (
        await db.execute(select(_func.count()).select_from(_Chunk).where(_Chunk.kb_id == kb_id))
    ).scalar_one()
    kb.doc_count = int(doc_total or 0)
    kb.chunk_count = int(chunk_total or 0)


@router.put("/{doc_id}/chunks/{chunk_id}")
@audited("doc.chunk_update", "document", id_arg="doc_id")
async def update_chunk(
    doc_id: int,
    chunk_id: int,
    body: ChunkUpdateIn,
    user: User = Depends(require_permission("doc:update")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.models import Chunk

    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise NotFoundError("文档不存在")
    kb = await db.get(KnowledgeBase, doc.kb_id)
    await _ensure_edit(db, user, kb)
    chunk = await db.get(Chunk, chunk_id)
    if not chunk or chunk.doc_id != doc_id:
        raise NotFoundError("分块不存在")

    content = (body.content or "").strip()
    if not content:
        raise PermissionDeniedError("分块内容不能为空")
    chunk.content = content
    chunk.token_count = len(content)
    chunk.tsv = content
    await _reembed_chunk(db, chunk)
    await _refresh_doc_count(db, doc)
    await db.flush()
    from app.retrieval import bm25_cache

    bm25_cache.invalidate_tenant(doc.tenant_id)
    return {"message": "已更新分块"}


@router.delete("/{doc_id}/chunks/{chunk_id}")
@audited("doc.chunk_delete", "document", id_arg="doc_id")
async def delete_chunk(
    doc_id: int,
    chunk_id: int,
    user: User = Depends(require_permission("doc:update")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.models import Chunk

    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise NotFoundError("文档不存在")
    kb = await db.get(KnowledgeBase, doc.kb_id)
    await _ensure_edit(db, user, kb)
    chunk = await db.get(Chunk, chunk_id)
    if not chunk or chunk.doc_id != doc_id:
        raise NotFoundError("分块不存在")
    await db.delete(chunk)
    await db.flush()
    await _refresh_doc_count(db, doc)
    await db.flush()
    from app.retrieval import bm25_cache

    bm25_cache.invalidate_tenant(doc.tenant_id)
    return {"message": "已删除分块"}


@router.post("/{doc_id}/chunks/{chunk_id}/split")
@audited("doc.chunk_split", "document", id_arg="doc_id")
async def split_chunk(
    doc_id: int,
    chunk_id: int,
    body: ChunkSplitIn,
    user: User = Depends(require_permission("doc:update")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.models import Chunk

    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise NotFoundError("文档不存在")
    kb = await db.get(KnowledgeBase, doc.kb_id)
    await _ensure_edit(db, user, kb)
    chunk = await db.get(Chunk, chunk_id)
    if not chunk or chunk.doc_id != doc_id:
        raise NotFoundError("分块不存在")
    text = chunk.content
    off = body.offset
    if off <= 0 or off >= len(text):
        raise PermissionDeniedError("切分位置需在分块内部（0 < offset < 长度）")

    head, tail = text[:off], text[off:]
    # 尾部后移 ordinal，为拆分出的新块腾出位置
    from sqlalchemy import update as _upd

    await db.execute(
        _upd(Chunk)
        .where(Chunk.doc_id == doc_id, Chunk.ordinal > chunk.ordinal)
        .values(ordinal=Chunk.ordinal + 1)
    )
    new_c = Chunk(
        tenant_id=chunk.tenant_id,
        kb_id=chunk.kb_id,
        doc_id=doc_id,
        ordinal=chunk.ordinal + 1,
        chunk_type=chunk.chunk_type if chunk.chunk_type != "parent" else "flat",
        content=tail,
        token_count=len(tail),
        page=chunk.page,
        vis_scope=chunk.vis_scope,
        acl_allow=chunk.acl_allow,
        acl_deny=chunk.acl_deny,
        enabled=True,
        tsv=tail,
    )
    chunk.content = head
    chunk.token_count = len(head)
    chunk.tsv = head
    db.add(new_c)
    await db.flush()
    await _reembed_chunk(db, chunk)
    await _reembed_chunk(db, new_c)
    await _refresh_doc_count(db, doc)
    await db.flush()
    from app.retrieval import bm25_cache

    bm25_cache.invalidate_tenant(doc.tenant_id)
    return {"message": "已拆分分块", "new_chunk_id": new_c.id}


# ===== 批量操作 =====
from pydantic import BaseModel as _BatchBM  # noqa: E402


class BatchIdsIn(_BatchBM):
    ids: list[int]


class BatchMoveIn(_BatchBM):
    ids: list[int]
    folder_id: int | None = None


class BatchVisibilityIn(_BatchBM):
    ids: list[int]
    visibility: str  # inherit/public/restricted


async def _load_owned_docs(db: AsyncSession, user: User, ids: list[int]) -> list[Document]:
    if not ids:
        return []
    if len(ids) > 500:
        raise PermissionDeniedError("单次批量操作不超过 500 条")
    rows = (
        await db.execute(
            select(Document).where(Document.id.in_(ids), Document.tenant_id == user.tenant_id)
        )
    ).scalars().all()
    # 校验每个文档所在库的可编辑权限
    kb_cache: dict[int, KnowledgeBase | None] = {}
    for d in rows:
        if d.kb_id not in kb_cache:
            kb_cache[d.kb_id] = await db.get(KnowledgeBase, d.kb_id)
        await _ensure_edit(db, user, kb_cache[d.kb_id])
    return list(rows)


@router.post("/batch-delete")
@audited("doc.batch_delete", "document")
async def batch_delete(
    body: BatchIdsIn,
    user: User = Depends(require_permission("doc:delete")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from sqlalchemy import delete as sql_delete

    from app.models import Chunk

    docs = await _load_owned_docs(db, user, body.ids)
    storage = get_storage()
    kb_ids = set()
    for doc in docs:
        kb_ids.add(doc.kb_id)
        await db.execute(sql_delete(Chunk).where(Chunk.doc_id == doc.id))
        if doc.file_key:
            try:
                storage.delete(doc.file_key)
            except Exception:  # noqa: BLE001
                pass
        await db.delete(doc)
    await db.flush()
    for kid in kb_ids:
        await _refresh_kb_counts(db, kid)
    if docs:
        from app.retrieval import bm25_cache

        bm25_cache.invalidate_tenant(user.tenant_id)
    return {"message": f"已删除 {len(docs)} 个文档", "count": len(docs)}


@router.post("/batch-move")
@audited("doc.batch_move", "document")
async def batch_move(
    body: BatchMoveIn,
    user: User = Depends(require_permission("doc:update")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    docs = await _load_owned_docs(db, user, body.ids)
    for doc in docs:
        doc.folder_id = body.folder_id
    await db.flush()
    return {"message": f"已移动 {len(docs)} 个文档", "count": len(docs)}


@router.post("/batch-visibility")
@audited("doc.batch_visibility", "document")
async def batch_visibility(
    body: BatchVisibilityIn,
    user: User = Depends(require_permission("doc:acl_manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if body.visibility not in ("inherit", "public", "restricted"):
        raise PermissionDeniedError("visibility 只能为 inherit/public/restricted")
    docs = await _load_owned_docs(db, user, body.ids)
    for doc in docs:
        doc.visibility = body.visibility
    await db.flush()
    from app.services.doc_acl_service import recompute_doc_acl

    for doc in docs:
        await recompute_doc_acl(db, doc.id)
    await db.flush()
    return {"message": f"已更新 {len(docs)} 个文档可见性", "count": len(docs)}


@router.post("/batch-reprocess")
@audited("doc.batch_reprocess", "document")
async def batch_reprocess(
    body: BatchIdsIn,
    user: User = Depends(require_permission("doc:update")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    docs = await _load_owned_docs(db, user, body.ids)
    ids: list[int] = []
    for doc in docs:
        doc.status = "pending"
        doc.progress = 0
        doc.error_msg = None
        doc.error_detail = None
        ids.append(doc.id)
    await db.commit()
    for did in ids:
        await enqueue_document(did)
    return {"message": f"已重新提交 {len(ids)} 个文档", "count": len(ids)}


@router.post("/batch-merge-export")
@audited("doc.batch_merge_export", "document")
async def batch_merge_export(
    body: BatchIdsIn,
    fmt: str = "docx",
    user: User = Depends(require_permission("doc:read")),
    db: AsyncSession = Depends(get_db),
):
    """把选中的多篇文档内容合并导出为一个文件（md/docx/pdf/txt）。"""
    import io

    from app.services.export_service import documents_to_markdown

    if fmt not in ("md", "docx", "pdf", "txt"):
        fmt = "docx"
    docs = await _load_owned_docs_read(db, user, body.ids)
    if not docs:
        raise NotFoundError("未找到可导出的文档")
    md = await documents_to_markdown(db, docs=docs)
    from app.agents.tools.file_tools import _render

    if fmt == "txt":
        data, mime = md.encode("utf-8"), "text/plain"
    else:
        data, mime = _render({"format": fmt, "content": md})
    filename = f"合并导出-{len(docs)}篇.{fmt}"
    from app.core.http_utils import content_disposition

    return StreamingResponse(
        io.BytesIO(data), media_type=mime,
        headers={"Content-Disposition": content_disposition(filename)},
    )


async def _load_owned_docs_read(db: AsyncSession, user: User, ids: list[int]) -> list[Document]:
    """读取权限校验（比 _load_owned_docs 宽松：只需可读）。"""
    if not ids:
        return []
    if len(ids) > 100:
        raise PermissionDeniedError("单次最多合并 100 篇")
    rows = (await db.execute(
        select(Document).where(Document.id.in_(ids), Document.tenant_id == user.tenant_id)
    )).scalars().all()
    kb_cache: dict[int, KnowledgeBase | None] = {}
    out: list[Document] = []
    for d in rows:
        if d.kb_id not in kb_cache:
            kb_cache[d.kb_id] = await db.get(KnowledgeBase, d.kb_id)
        try:
            await _ensure_read(db, user, kb_cache[d.kb_id])
            out.append(d)
        except PermissionDeniedError:
            continue
    return out


# ===== 文档标签 =====
class TagsIn(_BatchBM):
    tags: list[str]
    row_version: int | None = None  # 乐观锁，可选

@router.put("/{doc_id}/tags", response_model=DocumentOut)
@audited("doc.tags", "document", id_arg="doc_id")
async def set_document_tags(
    doc_id: int,
    body: TagsIn,
    user: User = Depends(require_permission("doc:update")),
    db: AsyncSession = Depends(get_db),
) -> Document:
    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise NotFoundError("文档不存在")
    kb = await db.get(KnowledgeBase, doc.kb_id)
    await _ensure_edit(db, user, kb)
    check_row_version(doc, body.row_version)
    # 去重、去空、限长
    seen: list[str] = []
    for t in body.tags:
        t = (t or "").strip()
        if t and t not in seen and len(t) <= 32:
            seen.append(t)
    doc.tags = seen[:20]
    await db.flush()
    return doc


@router.get("/{doc_id}/chunks")
async def get_document_chunks(
    doc_id: int,
    page: int = 1,
    page_size: int = 50,
    user: User = Depends(require_permission("doc:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """查看文档的分块列表（用于调试解析切分效果）。"""
    from sqlalchemy import func as _func

    from app.models import Chunk

    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise NotFoundError("文档不存在")
    await _ensure_read(db, user, await db.get(KnowledgeBase, doc.kb_id))

    base = select(Chunk).where(Chunk.doc_id == doc_id)
    total = (await db.execute(select(_func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        await db.execute(
            base.order_by(Chunk.ordinal).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": c.id,
                "ordinal": c.ordinal,
                "chunk_type": c.chunk_type,
                "content": c.content,
                "parent_content": c.parent_content,
                "page": c.page,
                "section": c.section,
                "token_count": c.token_count,
                "vis_scope": c.vis_scope,
                "has_embedding": bool(getattr(c, "embedding", None)),
            }
            for c in rows
        ],
    }


@router.post("/{doc_id}/reprocess", response_model=DocumentOut)
@audited("doc.reprocess", "document", id_arg="doc_id")
async def reprocess_document(
    doc_id: int,
    user: User = Depends(require_permission("doc:update")),
    db: AsyncSession = Depends(get_db),
) -> Document:
    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise NotFoundError("文档不存在")
    kb = await db.get(KnowledgeBase, doc.kb_id)
    await _ensure_edit(db, user, kb)
    doc.status = "pending"
    doc.progress = 0
    doc.error_msg = None
    doc.error_detail = None
    await db.commit()
    await enqueue_document(doc_id)
    return doc


# ==================== 文档版本历史 ====================
@router.get("/{doc_id}/versions")
async def list_document_versions(
    doc_id: int,
    user: User = Depends(require_permission("doc:read")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """列出该文档的历史版本（不含正文与分块快照，仅元信息）。"""
    from app.models import DocumentVersion

    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise NotFoundError("文档不存在")
    rows = (
        await db.execute(
            select(DocumentVersion)
            .where(DocumentVersion.doc_id == doc_id)
            .order_by(DocumentVersion.version.desc())
        )
    ).scalars().all()
    return [
        {
            "id": v.id,
            "version": v.version,
            "title": v.title,
            "char_count": v.char_count,
            "chunk_count": v.chunk_count,
            "reason": v.reason,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "current": v.version == (doc.version or 1),
        }
        for v in rows
    ]


@router.get("/{doc_id}/versions/{version}")
async def get_document_version(
    doc_id: int,
    version: int,
    user: User = Depends(require_permission("doc:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """查看某版本的正文与分块快照。"""
    from app.models import DocumentVersion

    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise NotFoundError("文档不存在")
    v = (
        await db.execute(
            select(DocumentVersion).where(
                DocumentVersion.doc_id == doc_id, DocumentVersion.version == version
            )
        )
    ).scalar_one_or_none()
    if not v:
        raise NotFoundError("版本不存在")
    return {
        "id": v.id, "version": v.version, "title": v.title, "content": v.content,
        "char_count": v.char_count, "chunk_count": v.chunk_count,
        "chunk_snapshot": v.chunk_snapshot, "reason": v.reason,
        "created_at": v.created_at.isoformat() if v.created_at else None,
    }


@router.post("/{doc_id}/versions/{version}/rollback", response_model=DocumentOut)
@audited("doc.version_rollback", "document", id_arg="doc_id")
async def rollback_document_version(
    doc_id: int,
    version: int,
    user: User = Depends(require_permission("doc:update")),
    db: AsyncSession = Depends(get_db),
) -> Document:
    """回滚到指定版本：以该版本的分块快照重建分块，并重新生成向量。"""
    from app.models import DocumentVersion

    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise NotFoundError("文档不存在")
    kb = await db.get(KnowledgeBase, doc.kb_id)
    await _ensure_edit(db, user, kb)
    v = (
        await db.execute(
            select(DocumentVersion).where(
                DocumentVersion.doc_id == doc_id, DocumentVersion.version == version
            )
        )
    ).scalar_one_or_none()
    if not v or not v.chunk_snapshot:
        raise NotFoundError("版本不存在或无可回滚快照")

    # 先把当前版本归档，回滚本身也可逆
    from app.tasks.ingest_tasks import _archive_version

    if await _archive_version(db, doc, reason="manual"):
        doc.version = (doc.version or 1) + 1

    # 删除现有分块，按快照重建
    from sqlalchemy import delete as _sql_delete

    await db.execute(_sql_delete(Chunk).where(Chunk.doc_id == doc_id))
    await db.flush()
    snap = v.chunk_snapshot
    new_chunks: list[Chunk] = []
    for item in snap:
        c = Chunk(
            tenant_id=doc.tenant_id,
            kb_id=doc.kb_id,
            doc_id=doc_id,
            ordinal=item.get("ordinal", 0),
            chunk_type=item.get("chunk_type", "flat"),
            content=item.get("content", ""),
            parent_content=item.get("parent_content"),
            token_count=item.get("token_count") or len(item.get("content", "")),
            page=item.get("page"),
            section=item.get("section"),
            vis_scope=0,
            enabled=True,
        )
        db.add(c)
        new_chunks.append(c)
    await db.flush()
    # 恢复父指针（以 ordinal 映射）
    ordinal_to_id = {c.ordinal: c.id for c in new_chunks}
    for item, c in zip(snap, new_chunks):
        pi = item.get("parent_ordinal")
        if pi is not None and pi in ordinal_to_id:
            c.parent_id = ordinal_to_id[pi]

    # 正文与标题回填
    if v.content:
        doc.content = v.content
    if v.title:
        doc.title = v.title
    doc.char_count = v.char_count or doc.char_count
    doc.chunk_count = len(new_chunks)
    doc.status = "ready"
    doc.progress = 100
    doc.error_msg = None
    doc.error_detail = None
    await db.flush()
    await _refresh_kb_counts(db, doc.kb_id)
    await db.commit()

    # 重建向量（异步，失败不影响回滚结果）；纯关键词库跳过向量化
    is_keyword_kb = bool(kb and getattr(kb, "index_mode", "vector") == "keyword")
    embeddable = [c for c in new_chunks if c.chunk_type != "parent"]
    if embeddable and not is_keyword_kb:
        try:
            from app.providers.registry import get_embedding

            kb_model_id = getattr(kb, "embedding_model_id", None) if kb else None
            emb_driver, rm = await get_embedding(db, tenant_id=doc.tenant_id, config_id=kb_model_id)
            vecs = await emb_driver.embed([c.content for c in embeddable], model=rm.model_name)
            from app.retrieval.vector_store.store import get_vector_store

            await get_vector_store().add_embeddings(db, [(c.id, x) for c, x in zip(embeddable, vecs)])
            await db.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("rollback_reembed_failed", document_id=doc_id, err=str(e)[:200])

    # 使 BM25 缓存失效
    try:
        from app.retrieval import bm25_cache

        bm25_cache.invalidate_tenant(doc.tenant_id)
    except Exception:  # noqa: BLE001
        pass
    await db.refresh(doc)
    return doc


@router.delete("/{doc_id}")
@audited("doc.delete", "document", id_arg="doc_id")
async def delete_document(
    doc_id: int,
    user: User = Depends(require_permission("doc:delete")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise NotFoundError("文档不存在")
    kb = await db.get(KnowledgeBase, doc.kb_id)
    await _ensure_edit(db, user, kb)

    from sqlalchemy import delete as sql_delete

    from app.models import Chunk

    await db.execute(sql_delete(Chunk).where(Chunk.doc_id == doc_id))
    if doc.file_key:
        try:
            get_storage().delete(doc.file_key)
        except Exception:  # noqa: BLE001
            pass
    kb_id = doc.kb_id
    await db.delete(doc)
    await db.flush()
    await _refresh_kb_counts(db, kb_id)
    return {"message": "已删除"}


async def _ensure_read(db: AsyncSession, user: User, kb: KnowledgeBase | None) -> None:
    """文档读取前的知识库可见性校验（与 kb._ensure_access 同语义）。

    public：本租户全员；internal：仅内部用户（外部客户不得）；private：owner 与显式成员。
    """
    if not kb:
        raise NotFoundError("知识库不存在")
    if kb.visibility == "public":
        return
    if kb.visibility == "internal" and getattr(user, "user_type", "internal") != "external":
        return
    if user.is_admin or kb.owner_id == user.id:
        return
    ps = await load_principal_set(db, user)
    member = (
        await db.execute(
            select(KBMember).where(
                KBMember.kb_id == kb.id,
                KBMember.principal_id.in_(ps.principals),
            )
        )
    ).scalars().first()
    if not member:
        raise PermissionDeniedError("无权访问该知识库")


async def _ensure_edit(db: AsyncSession, user: User, kb: KnowledgeBase | None) -> None:
    if not kb:
        raise NotFoundError("知识库不存在")
    if user.is_admin or kb.owner_id == user.id:
        return
    ps = await load_principal_set(db, user)
    member = (
        await db.execute(
            select(KBMember).where(
                KBMember.kb_id == kb.id,
                KBMember.principal_id.in_(ps.principals),
                KBMember.perm_level.in_(["editor", "manager"]),
            )
        )
    ).scalars().first()
    if not member:
        raise PermissionDeniedError("无编辑权限")


# ===== 文档级 ACL =====
from pydantic import BaseModel  # noqa: E402

from app.models import DocumentACL  # noqa: E402
from app.services.doc_acl_service import recompute_doc_acl  # noqa: E402


class DocAclIn(BaseModel):
    principal_type: str  # user/department/role/group
    principal_id: int
    effect: str = "allow"  # allow/deny


class DocVisibilityIn(BaseModel):
    visibility: str  # inherit/public/restricted
    row_version: int | None = None  # 乐观锁，可选


def _principal_of(principal_type: str, principal_id: int) -> int:
    from app.services import permission as P

    mapping = {
        "user": P.user_principal,
        "department": P.dept_principal,
        "role": P.role_principal,
        "group": P.group_principal,
    }
    if principal_type not in mapping:
        raise PermissionDeniedError(f"不支持的授权对象类型: {principal_type}")
    return mapping[principal_type](principal_id)


@router.get("/{doc_id}/acl")
async def get_doc_acl(
    doc_id: int,
    user: User = Depends(require_permission("doc:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise NotFoundError("文档不存在")
    await _ensure_read(db, user, await db.get(KnowledgeBase, doc.kb_id))
    rows = (
        await db.execute(select(DocumentACL).where(DocumentACL.document_id == doc_id))
    ).scalars().all()
    from app.services.permission import describe_principals

    names = await describe_principals(db, [r.principal_id for r in rows])
    return {
        "visibility": doc.visibility,
        "items": [
            {
                "id": r.id, "principal_id": r.principal_id, "effect": r.effect,
                "principal_name": names.get(r.principal_id),
            }
            for r in rows
        ],
    }


@router.post("/{doc_id}/acl")
@audited("doc.acl_add", "document", id_arg="doc_id")
async def add_doc_acl(
    doc_id: int,
    body: DocAclIn,
    user: User = Depends(require_permission("doc:acl_manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise NotFoundError("文档不存在")
    kb = await db.get(KnowledgeBase, doc.kb_id)
    await _ensure_edit(db, user, kb)
    if body.effect not in ("allow", "deny"):
        raise PermissionDeniedError("effect 只能为 allow 或 deny")

    pid = _principal_of(body.principal_type, body.principal_id)
    db.add(
        DocumentACL(
            document_id=doc_id,
            principal_id=pid,
            effect=body.effect,
        )
    )
    # 添加 ACL 自动把文档设为 restricted（否则 ACL 不生效）
    if doc.visibility != "restricted":
        doc.visibility = "restricted"
    await db.flush()
    await recompute_doc_acl(db, doc_id)
    await db.flush()
    return {"message": "已添加授权"}


@router.delete("/{doc_id}/acl/{acl_id}")
@audited("doc.acl_delete", "document", id_arg="doc_id")
async def delete_doc_acl(
    doc_id: int,
    acl_id: int,
    user: User = Depends(require_permission("doc:acl_manage")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise NotFoundError("文档不存在")
    kb = await db.get(KnowledgeBase, doc.kb_id)
    await _ensure_edit(db, user, kb)
    acl = await db.get(DocumentACL, acl_id)
    if not acl or acl.document_id != doc_id:
        raise NotFoundError("授权不存在")
    await db.delete(acl)
    await db.flush()
    await recompute_doc_acl(db, doc_id)
    await db.flush()
    return {"message": "已移除授权"}


@router.patch("/{doc_id}/visibility", response_model=DocumentOut)
@audited("doc.visibility", "document", id_arg="doc_id")
async def set_doc_visibility(
    doc_id: int,
    body: DocVisibilityIn,
    user: User = Depends(require_permission("doc:acl_manage")),
    db: AsyncSession = Depends(get_db),
) -> Document:
    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise NotFoundError("文档不存在")
    kb = await db.get(KnowledgeBase, doc.kb_id)
    await _ensure_edit(db, user, kb)
    check_row_version(doc, body.row_version)
    if body.visibility not in ("inherit", "public", "restricted"):
        raise PermissionDeniedError("visibility 只能为 inherit/public/restricted")
    doc.visibility = body.visibility
    await db.flush()
    await recompute_doc_acl(db, doc_id)
    await db.flush()
    return doc


# ===== 文档文件夹 =====
from pydantic import BaseModel as _BM  # noqa: E402

from app.models import DocumentFolder  # noqa: E402


class FolderIn(_BM):
    name: str
    parent_id: int | None = None
    sort: int | None = None


class FolderPatchIn(_BM):
    name: str | None = None
    sort: int | None = None
    parent_id: int | None = None


@router.get("/folders/list")
async def list_folders(
    kb_id: int,
    user: User = Depends(require_permission("doc:read")),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.tenant_id != user.tenant_id:
        raise NotFoundError("知识库不存在")
    rows = (
        await db.execute(select(DocumentFolder).where(DocumentFolder.kb_id == kb_id).order_by(DocumentFolder.sort))
    ).scalars().all()
    return [{"id": f.id, "name": f.name, "parent_id": f.parent_id, "sort": f.sort} for f in rows]


@router.post("/folders")
async def create_folder(
    kb_id: int,
    body: FolderIn,
    user: User = Depends(require_permission("doc:update")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.tenant_id != user.tenant_id:
        raise NotFoundError("知识库不存在")
    f = DocumentFolder(
        tenant_id=user.tenant_id, kb_id=kb_id, name=body.name,
        parent_id=body.parent_id, sort=body.sort or 0,
    )
    db.add(f)
    await db.flush()
    return {"id": f.id, "name": f.name, "parent_id": f.parent_id, "sort": f.sort}


@router.patch("/folders/{folder_id}")
async def update_folder(
    folder_id: int,
    body: FolderPatchIn,
    user: User = Depends(require_permission("doc:update")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """重命名 / 调整排序 / 移动文件夹。"""
    f = await db.get(DocumentFolder, folder_id)
    if not f or f.tenant_id != user.tenant_id:
        raise NotFoundError("文件夹不存在")
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise ValidationError("文件夹名称不能为空")
        f.name = name
    if body.sort is not None:
        f.sort = body.sort
    if body.parent_id is not None:
        if body.parent_id == folder_id:
            raise ValidationError("不能把文件夹移动到自身")
        # 防环：新父不能是自己的子孙
        if body.parent_id:
            cur = await db.get(DocumentFolder, body.parent_id)
            seen = set()
            while cur and cur.id not in seen:
                if cur.id == folder_id:
                    raise ValidationError("不能把文件夹移动到自己的子文件夹下")
                seen.add(cur.id)
                cur = await db.get(DocumentFolder, cur.parent_id) if cur.parent_id else None
        f.parent_id = body.parent_id
    await db.flush()
    return {"id": f.id, "name": f.name, "parent_id": f.parent_id, "sort": f.sort}


@router.put("/folders/order")
async def reorder_folders(
    kb_id: int,
    body: dict,
    user: User = Depends(require_permission("doc:update")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """批量调整同级文件夹顺序。body: {"ordered_ids": [id, ...]}"""
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.tenant_id != user.tenant_id:
        raise NotFoundError("知识库不存在")
    ordered = body.get("ordered_ids") or []
    for idx, fid in enumerate(ordered):
        f = await db.get(DocumentFolder, fid)
        if f and f.kb_id == kb_id and f.tenant_id == user.tenant_id:
            f.sort = idx
    await db.flush()
    return {"message": "已排序", "count": len(ordered)}


@router.delete("/folders/{folder_id}")
async def delete_folder(
    folder_id: int,
    user: User = Depends(require_permission("doc:update")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    f = await db.get(DocumentFolder, folder_id)
    if not f or f.tenant_id != user.tenant_id:
        raise NotFoundError("文件夹不存在")
    # 该文件夹下的文档移到根目录
    from sqlalchemy import update as _upd

    await db.execute(_upd(Document).where(Document.folder_id == folder_id).values(folder_id=None))
    await db.delete(f)
    await db.flush()
    return {"message": "已删除"}


@router.patch("/{doc_id}/folder")
async def move_document(
    doc_id: int,
    body: dict,
    user: User = Depends(require_permission("doc:update")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id:
        raise NotFoundError("文档不存在")
    doc.folder_id = body.get("folder_id")
    await db.flush()
    return {"message": "已移动"}


# ==================== 图文知识条目 ====================
# 直接录入正文（content）+ 配套图片/附件（attachments）；命中后随回答发给提问者。
from fastapi import Form  # noqa: E402

ENTRY_ATTACH_EXT = {
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "svg",
    "pdf", "docx", "xlsx", "pptx", "md", "txt", "csv", "zip",
}


async def _save_entry_attachments(db, tenant_id: int, files: list[UploadFile]) -> list[dict]:
    """把上传的图片/附件存 storage，返回 [{file_key,name,mime,size}]。"""
    storage = get_storage()
    out: list[dict] = []
    for f in files or []:
        name = f.filename or "file"
        ext = Path(name).suffix.lower().lstrip(".")
        if ext and ext not in ENTRY_ATTACH_EXT:
            raise PermissionDeniedError(f"不支持的附件类型: .{ext}")
        data = await f.read()
        if not data:
            continue
        if len(data) > 20 * 1024 * 1024:
            raise PermissionDeniedError(f"附件过大（>{20}MB）: {name}")
        key, _ = storage.save(tenant_id=tenant_id, filename=name, data=data)
        mime = f.content_type or ""
        out.append({"file_key": key, "name": name, "mime": mime, "size": len(data)})
    return out


@router.post("/kbs/{kb_id}/entries", response_model=DocumentOut)
@audited("doc.entry_create", "document", id_arg="kb_id")
async def create_entry(
    kb_id: int,
    title: str = Form(...),
    content: str = Form(...),
    files: list[UploadFile] = File(default=[]),
    user: User = Depends(require_permission("doc:upload")),
    db: AsyncSession = Depends(get_db),
) -> Document:
    """新建图文知识条目：手录正文 + 可选配套图片/附件（命中后自动发送给提问者）。"""
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb or kb.tenant_id != user.tenant_id:
        raise NotFoundError("知识库不存在")
    await _ensure_edit(db, user, kb)
    if not (content or "").strip():
        raise PermissionDeniedError("条目正文不能为空")

    attachments = await _save_entry_attachments(db, user.tenant_id, files)
    doc = Document(
        tenant_id=user.tenant_id, kb_id=kb_id, title=(title or "未命名条目").strip()[:200],
        kind="entry", source_type="manual", content=content.strip(),
        attachments=attachments or None, status="pending", uploaded_by=user.id,
    )
    db.add(doc)
    await db.flush()
    doc_id = doc.id
    await db.commit()
    await enqueue_document(doc_id)
    await db.refresh(doc)
    return doc


@router.patch("/{doc_id}/entry", response_model=DocumentOut)
@audited("doc.entry_update", "document", id_arg="doc_id")
async def update_entry(
    doc_id: int,
    body: dict,
    user: User = Depends(require_permission("doc:update")),
    db: AsyncSession = Depends(get_db),
) -> Document:
    """编辑图文条目的标题/正文（改动后重灌分块）。"""
    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id or doc.kind != "entry":
        raise NotFoundError("图文条目不存在")
    kb = await db.get(KnowledgeBase, doc.kb_id)
    await _ensure_edit(db, user, kb)
    check_row_version(doc, body.get("row_version"))
    if "title" in body and body["title"]:
        doc.title = str(body["title"]).strip()[:200]
    if "content" in body and body["content"] is not None:
        doc.content = str(body["content"]).strip()
    doc.status = "pending"
    await db.flush()
    await db.commit()
    await enqueue_document(doc_id)
    await db.refresh(doc)
    return doc


@router.post("/{doc_id}/attachments", response_model=DocumentOut)
@audited("doc.entry_attach", "document", id_arg="doc_id")
async def add_entry_attachments(
    doc_id: int,
    files: list[UploadFile] = File(...),
    user: User = Depends(require_permission("doc:update")),
    db: AsyncSession = Depends(get_db),
) -> Document:
    """给图文条目追加配套附件。"""
    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id or doc.kind != "entry":
        raise NotFoundError("图文条目不存在")
    kb = await db.get(KnowledgeBase, doc.kb_id)
    await _ensure_edit(db, user, kb)
    new_atts = await _save_entry_attachments(db, user.tenant_id, files)
    doc.attachments = list(doc.attachments or []) + new_atts
    await db.flush()
    await db.commit()
    await db.refresh(doc)
    return doc


@router.delete("/{doc_id}/attachments/{idx}")
@audited("doc.entry_attach_del", "document", id_arg="doc_id")
async def remove_entry_attachment(
    doc_id: int,
    idx: int,
    user: User = Depends(require_permission("doc:update")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """删除图文条目的第 idx 个附件。"""
    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id or doc.kind != "entry":
        raise NotFoundError("图文条目不存在")
    atts = list(doc.attachments or [])
    if idx < 0 or idx >= len(atts):
        raise NotFoundError("附件不存在")
    removed = atts.pop(idx)
    doc.attachments = atts or None
    await db.flush()
    # 释放磁盘文件
    try:
        get_storage().delete(removed.get("file_key"))
    except Exception:  # noqa: BLE001
        pass
    return {"message": "已删除"}


@router.get("/{doc_id}/attachments/{idx}/download")
async def download_entry_attachment(
    doc_id: int,
    idx: int,
    user: User = Depends(require_permission("doc:read")),
    db: AsyncSession = Depends(get_db),
):
    """下载图文条目的第 idx 个配套附件（需登录，复用 storage）。"""
    doc = await db.get(Document, doc_id)
    if not doc or doc.tenant_id != user.tenant_id or doc.kind != "entry":
        raise NotFoundError("图文条目不存在")
    atts = list(doc.attachments or [])
    if idx < 0 or idx >= len(atts):
        raise NotFoundError("附件不存在")
    a = atts[idx]
    try:
        data = get_storage().read(a["file_key"])
    except Exception as e:  # noqa: BLE001
        raise NotFoundError(f"附件读取失败: {str(e)[:100]}") from e
    import io as _io

    from urllib.parse import quote as _quote

    fname = a.get("name") or "file"
    return StreamingResponse(
        _io.BytesIO(data), media_type=a.get("mime") or "application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_quote(fname)}"},
    )
