"""邮件入库服务：IMAP 拉取 → 正文/附件落库 → 走入库流水线。

安全：
- 只读拉取（BODY.PEEK / 不设 \\Seen），不删除、不移动用户邮件。
- 密码经 core.crypto 加密存储；连接失败写 last_error 不抛。
- 按邮件 UID 去重（seen_uids），避免重复入库。
"""
from __future__ import annotations

import email
import email.header
import imaplib
import re
import time
from email.message import Message as EmailMessage
from functools import partial

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

logger = get_logger("email_ingest")

# 可入库的附件扩展名（与 document.ALLOWED_EXT 保持一致）
INGESTABLE_EXT = {
    "pdf", "docx", "xlsx", "xls", "pptx", "md", "markdown",
    "html", "htm", "csv", "txt", "text", "log",
}
_MAX_UID_KEEP = 500  # 每源保留的已处理 UID 上限


def _safe_decode(payload: bytes, charset: str | None) -> str:
    candidates = [charset, "utf-8", "gbk", "latin-1"]
    for enc in candidates:
        if not enc:
            continue
        try:
            return payload.decode(enc, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="ignore")


def _decode_header(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        parts = email.header.decode_header(raw)
    except Exception:  # noqa: BLE001
        return str(raw)
    out = []
    for data, enc in parts:
        if isinstance(data, bytes):
            out.append(_safe_decode(data, enc))
        else:
            out.append(str(data))
    text = "".join(out)
    # 兜底：非标准客户端把 UTF-8 直接塞进头部（未按 RFC2047 编码），Python 按 latin-1 读成乱码。
    # 若还原后是合法 UTF-8 且含 CJK，则采用还原结果。
    if any("À" <= ch <= "ÿ" for ch in text):
        try:
            recovered = text.encode("latin-1").decode("utf-8")
            if any("一" <= ch <= "鿿" for ch in recovered):
                return recovered
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return text


def _conn_params(src, account=None):
    """返回 IMAP 连接参数（统一字段名）。src 是 EmailSource，account 是可选的关联 MailAccount。

    若 src 引用了个人邮箱账号（source_account_id），则用该账号的 IMAP 凭证；
    否则用 src 自带字段。返回一个命名空间对象，供 _fetch_messages/test_source 使用。
    """
    import types

    if account is not None:
        return types.SimpleNamespace(
            imap_host=account.imap_host, imap_port=account.imap_port,
            use_ssl=account.imap_use_ssl, username=account.imap_username,
            password=account.imap_password, folder=account.folder or src.folder,
        )
    return types.SimpleNamespace(
        imap_host=src.imap_host, imap_port=src.imap_port,
        use_ssl=src.use_ssl, username=src.username,
        password=src.password, folder=src.folder,
    )


async def _resolve_account(db, src):
    """若 src 关联了个人邮箱账号则取回（校验归属）；否则返回 None。"""
    aid = getattr(src, "source_account_id", None)
    if not aid:
        return None
    from app.models import MailAccount

    acc = await db.get(MailAccount, aid)
    # 归属校验：账号须与本邮件源同租户（防越权借用）
    if not acc or acc.tenant_id != src.tenant_id:
        return None
    return acc


def _fetch_messages(src, *, first_run_cap: int = 500, account=None) -> tuple[list[dict], int, int | None]:
    """同步拉取（在线程中执行）。

    可靠增量：用 IMAP UID 游标（`UID SEARCH UID cursor+1:*`）替代「最近 N 封」，
    支持任意积压。返回 (消息列表, 新的最大 UID, UIDVALIDITY)。

    - UIDVALIDITY 变化 = 邮箱重建，游标归零，从头拉一次（受 first_run_cap 限制）。
    - cursor=0（首次）也走受限全量，避免一次拉爆。
    """
    from app.core.crypto import decrypt

    cp = _conn_params(src, account)
    pwd = decrypt(cp.password) if cp.password else ""
    if cp.use_ssl:
        conn = imaplib.IMAP4_SSL(cp.imap_host, cp.imap_port)
    else:
        conn = imaplib.IMAP4(cp.imap_host, cp.imap_port)
    conn.login(cp.username, pwd)
    out: list[dict] = []
    max_uid = int(getattr(src, "uid_cursor", 0) or 0)
    validity: int | None = None
    try:
        typ, sel = conn.select(cp.folder, readonly=True)  # 只读，避免标记已读
        if typ != "OK":
            return [], max_uid, validity
        # 读 UIDVALIDITY（SELECT 响应含 [UIDVALIDITY n]）
        try:
            raw = sel[0].decode("utf-8", "ignore") if sel and sel[0] else ""
            m = re.search(r"UIDVALIDITY\s+(\d+)", raw)
            if m:
                validity = int(m.group(1))
        except Exception:  # noqa: BLE001
            validity = None
        cursor = int(getattr(src, "uid_cursor", 0) or 0)
        prev_validity = getattr(src, "uid_validity", None)
        # UIDVALIDITY 变化（或有值但我们上次没记）→ 邮箱重建，游标归零
        if validity is not None and prev_validity is not None and validity != prev_validity:
            cursor = 0
        seen = set(str(u) for u in (src.seen_uids or []))
        if cursor > 0:
            typ, data = conn.uid("search", None, f"UID {cursor + 1}:*")
        else:
            typ, data = conn.uid("search", None, "ALL")
        if typ != "OK":
            return [], max_uid, validity
        uids = data[0].split() if data and data[0] else []
        if cursor <= 0 and len(uids) > first_run_cap:
            uids = uids[-first_run_cap:]  # 首次/重建：只取最近 first_run_cap 封
        for raw_uid in uids:
            uid = raw_uid.decode() if isinstance(raw_uid, bytes) else str(raw_uid)
            try:
                uid_int = int(uid)
            except (TypeError, ValueError):
                continue
            if uid_int > max_uid:
                max_uid = uid_int
            if uid in seen:
                continue
            typ, msg_data = conn.uid("fetch", raw_uid, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            msg: EmailMessage = email.message_from_bytes(msg_data[0][1])
            subject = _decode_header(msg.get("Subject"))
            sender = _decode_header(msg.get("From"))
            text = ""
            attachments: list[dict] = []
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = str(part.get("Content-Disposition") or "")
                fname = _decode_header(part.get_filename())
                if fname or "attachment" in disp:
                    if fname:
                        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
                        if ext in INGESTABLE_EXT:
                            payload = part.get_payload(decode=True)
                            if payload:
                                attachments.append({"name": fname, "data": payload})
                elif ctype == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        text += _safe_decode(payload, part.get_content_charset())
            out.append({"uid": uid, "subject": subject, "from": sender,
                        "text": text, "attachments": attachments})
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        conn.logout()
    return out, max_uid, validity


def _matches(src, msg: dict) -> bool:
    """按发件人/主题关键词过滤。"""
    allow = [x.strip().lower() for x in (src.allow_from or "").split(",") if x.strip()]
    if allow and not any(a in (msg.get("from") or "").lower() for a in allow):
        return False
    kws = [x.strip() for x in (src.subject_keywords or "").split(",") if x.strip()]
    if kws and not any(k in (msg.get("subject") or "") for k in kws):
        return False
    return True


# 单个标签/标签总数上限（防脏数据膨胀）
_MAX_TAG_LEN = 32
_MAX_TAGS = 20


def _resolve_tags(src, subject: str, body: str) -> list[str]:
    """计算该邮件文档的标签：固定 auto_tags + 按 tag_rules 命中（主题/正文关键词）。

    tag_rules 形如 {"发票": ["财务"], "合同": ["法务", "重要"]}；命中即合并其标签。
    """
    tags: list[str] = []

    def _add(t: str):
        s = str(t).strip()[:_MAX_TAG_LEN]
        if s and s not in tags and len(tags) < _MAX_TAGS:
            tags.append(s)

    for t in (getattr(src, "auto_tags", None) or []):
        _add(t)
    rules = getattr(src, "tag_rules", None) or {}
    if isinstance(rules, dict):
        hay = f"{subject or ''} {body or ''}"
        for kw, vals in rules.items():
            if kw and str(kw) in hay:
                for v in (vals if isinstance(vals, list) else [vals]):
                    _add(v)
    return tags


async def _ingest_one(db: AsyncSession, src, msg: dict) -> int:
    """把一封邮件（正文 + 附件）入库。返回新增文档数。"""
    from app.ingest.storage import get_storage
    from app.models import Document
    from app.tasks.ingest_tasks import enqueue_document

    storage = get_storage()
    created = 0
    subject = msg.get("subject") or f"邮件 {msg.get('uid')}"
    sender = msg.get("from") or ""

    docs: list[tuple[str, bytes]] = []
    if src.ingest_mode in ("both", "body") and msg.get("text"):
        body_name = f"{subject[:60]}.txt"
        docs.append((body_name, msg["text"].encode("utf-8")))
    if src.ingest_mode in ("both", "attach"):
        for att in msg.get("attachments") or []:
            docs.append((att["name"], att["data"]))

    for filename, data in docs:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"
        if ext not in INGESTABLE_EXT:
            continue
        file_key, chash = storage.save(tenant_id=src.tenant_id, filename=filename, data=data)
        dup = (await db.execute(
            select(Document).where(Document.kb_id == src.kb_id, Document.content_hash == chash)
        )).scalar_one_or_none()
        if dup:
            try:
                storage.delete(file_key)
            except Exception:  # noqa: BLE001
                pass
            continue
        doc = Document(
            tenant_id=src.tenant_id, kb_id=src.kb_id,
            title=f"[邮件] {filename}" if ext == "txt" else filename,
            source_type="email", source_uri=sender[:512],
            file_key=file_key, file_name=filename, file_ext=ext,
            file_size=len(data), content_hash=chash, status="pending",
            uploaded_by=src.uploaded_by, metadata_={"email_subject": subject, "email_from": sender},
            tags=_resolve_tags(src, subject, msg.get("text") or "") or None,
        )
        db.add(doc)
        await db.flush()
        await enqueue_document(doc.id)
        created += 1
    return created


async def sync_source(db: AsyncSession, src) -> dict:
    """拉取一个邮件源的新邮件并入库。返回统计。"""
    import asyncio

    if not src.enabled:
        return {"ok": False, "message": "该邮件源已禁用"}
    account = await _resolve_account(db, src)
    try:
        messages, max_uid, validity = await asyncio.wait_for(
            asyncio.to_thread(partial(_fetch_messages, src, first_run_cap=500, account=account)),
            timeout=60,
        )
    except Exception as e:  # noqa: BLE001
        src.status = "error"
        src.last_error = str(e)[:500]
        await db.flush()
        logger.warning("email_fetch_failed", source_id=src.id, err=str(e)[:200])
        return {"ok": False, "message": f"拉取失败：{str(e)[:200]}"}

    seen = list(src.seen_uids or [])
    total = 0
    failed = 0
    for msg in messages:
        if not _matches(src, msg):
            seen.append(msg["uid"])
            continue
        try:
            total += await _ingest_one(db, src, msg)
        except Exception:  # noqa: BLE001
            failed += 1
            logger.exception("email_ingest_one_failed", uid=msg.get("uid"))
        seen.append(msg["uid"])
    src.seen_uids = seen[-_MAX_UID_KEEP:]
    # 游标推进：整批无失败才推进到 max_uid（失败则下次凭 seen_uids 兜底重试，不漏）
    if failed == 0 and max_uid > (src.uid_cursor or 0):
        src.uid_cursor = max_uid
    if validity is not None:
        src.uid_validity = validity
    src.last_sync_at = int(time.time() * 1000)
    src.ingested_count = (src.ingested_count or 0) + total
    src.status = "active"
    src.last_error = None
    await db.flush()
    return {"ok": True, "message": f"拉取 {len(messages)} 封，新增入库 {total} 篇", "ingested": total}


async def test_source(src, *, account=None) -> dict:
    """测试连接（登录 + 选文件夹）。account 非空则用个人邮箱账号的 IMAP 凭证。"""
    import asyncio

    from app.core.crypto import decrypt

    cp = _conn_params(src, account)

    def _test() -> str:
        pwd = decrypt(cp.password) if cp.password else ""
        if cp.use_ssl:
            conn = imaplib.IMAP4_SSL(cp.imap_host, cp.imap_port)
        else:
            conn = imaplib.IMAP4(cp.imap_host, cp.imap_port)
        try:
            conn.login(cp.username, pwd)
            typ, data = conn.select(cp.folder, readonly=True)
            if typ != "OK":
                raise ValueError(f"无法打开文件夹 {cp.folder}")
            count = int(data[0]) if data and data[0] else 0
            return f"连接成功，文件夹「{cp.folder}」有 {count} 封邮件"
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass

    try:
        msg = await asyncio.wait_for(asyncio.to_thread(_test), timeout=30)
        return {"ok": True, "message": msg}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)[:300]}
