"""办公文档结构化读写：按「可寻址元素」读取与编辑。

与现有 `parse_file` 的区别：
- `parse_file` 输出扁平的 text + pages，适合入库切片
- 本模块输出**可寻址的元素树**（页号 / 段号 / 表格坐标 / 幻灯片形状），
  让 AI 能精确说「把第 3 段改成 X」「Sheet1!B2 设为 100」，而不只是整篇替换

设计要点（勿凭直觉改动）：

1. **读取与编辑共用同一套地址**。read 返回的 `address` 可直接作为 edit 的
   目标，AI 不需要自己推导坐标。

2. **编辑一律另存新文件**，绝不原地改用户上传的源文件 —— 与现有
   EditFileTool 的语义保持一致，也便于保留原件做对比。

3. **按格式选择最小改动面**：
   - docx：python-docx 段落/表格（保留样式）
   - xlsx：openpyxl 单元格/行（保留公式与格式）
   - pptx：python-pptx 文本框架（保留版式）
   - pdf：PyMuPDF 只支持文本层替换（复杂排版会错位，明确告知局限）
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.errors import ValidationError

MAX_ELEMENTS = 3000  # 单个文档返回的元素上限，防止超大表格撑爆响应

# 编辑接口逐条容错（一条坏地址不该让整批失败），但调用方必须能分辨
# 「全部成功」与「一条都没生效」——否则 AI 会把空操作当成功继续往下走。
# 约定：未生效的条目一律以 SKIP_SUFFIX 结尾，count_applied 据此统计。
SKIP_SUFFIX = "（未生效）"


def _skip(address: str, reason: str) -> str:
    """构造一条「未生效」的变更说明。"""
    return f"{address}: {reason}{SKIP_SUFFIX}"


def count_applied(changes: list[str]) -> int:
    """变更列表里真正生效的条数。"""
    return sum(1 for c in changes if not c.endswith(SKIP_SUFFIX))


@dataclass
class Element:
    """一个可寻址的内容元素。"""
    address: str          # 例如 "p:3" / "t:2:0:1" / "Sheet1!B2" / "s:1:body"
    kind: str             # paragraph / table_cell / cell / shape
    text: str
    meta: dict = field(default_factory=dict)  # {page, style, row, col, ...}


@dataclass
class DocOutline:
    kind: str                       # docx / xlsx / pptx / pdf
    elements: list[Element] = field(default_factory=list)
    sheets: list[str] = field(default_factory=list)      # xlsx 专用
    page_count: int = 0
    truncated: bool = False
    limitations: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 读取


def read_outline(path: Path, ext: str | None = None) -> DocOutline:
    """解析文档为可寻址元素列表。"""
    ext = (ext or path.suffix).lower().lstrip(".")
    if ext == "docx":
        return _read_docx(path)
    if ext in ("xlsx", "xlsm"):
        return _read_xlsx(path)
    if ext == "pptx":
        return _read_pptx(path)
    if ext == "pdf":
        return _read_pdf(path)
    raise ValidationError(f"不支持的结构化读取格式：{ext}（支持 docx/xlsx/pptx/pdf）")


def _read_docx(path: Path) -> DocOutline:
    from docx import Document as DocxDocument

    doc = DocxDocument(str(path))
    out = DocOutline(kind="docx")
    for i, para in enumerate(doc.paragraphs):
        if len(out.elements) >= MAX_ELEMENTS:
            out.truncated = True
            break
        text = (para.text or "").strip()
        if not text:
            continue
        style = (para.style.name or "") if para.style is not None else ""
        out.elements.append(Element(
            address=f"p:{i}", kind="paragraph", text=text,
            meta={"index": i, "style": style},
        ))

    for ti, table in enumerate(doc.tables):
        for ri, row in enumerate(table.rows):
            for ci, cell in enumerate(row.cells):
                if len(out.elements) >= MAX_ELEMENTS:
                    out.truncated = True
                    break
                text = (cell.text or "").strip()
                if not text:
                    continue
                out.elements.append(Element(
                    address=f"t:{ti}:{ri}:{ci}", kind="table_cell", text=text,
                    meta={"table": ti, "row": ri, "col": ci},
                ))
    out.limitations.append("docx 仅提取段落与表格文本；页眉页脚、文本框、图片内文字未包含")
    return out


def _read_xlsx(path: Path) -> DocOutline:
    from openpyxl import load_workbook

    wb = load_workbook(str(path), data_only=True)
    out = DocOutline(kind="xlsx", sheets=list(wb.sheetnames))
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if len(out.elements) >= MAX_ELEMENTS:
                    out.truncated = True
                    break
                if cell.value is None or str(cell.value).strip() == "":
                    continue
                out.elements.append(Element(
                    address=f"{ws.title}!{cell.coordinate}",
                    kind="cell",
                    text=str(cell.value),
                    meta={"sheet": ws.title, "row": cell.row, "col": cell.column},
                ))
    out.limitations.append("xlsx 使用 data_only 读取，公式单元格返回的是缓存值而非公式本身")
    return out


def _read_pptx(path: Path) -> DocOutline:
    from pptx import Presentation

    prs = Presentation(str(path))
    out = DocOutline(kind="pptx", page_count=len(prs.slides))
    for si, slide in enumerate(prs.slides, start=1):
        for shi, shape in enumerate(slide.shapes):
            if len(out.elements) >= MAX_ELEMENTS:
                out.truncated = True
                break
            if not getattr(shape, "has_text_frame", False):
                continue
            text = "\n".join(
                (p.text or "").strip() for p in shape.text_frame.paragraphs
            ).strip()
            if not text:
                continue
            out.elements.append(Element(
                address=f"s:{si}:{shi}", kind="shape", text=text,
                meta={"slide": si, "shape_index": shi,
                      "shape_name": getattr(shape, "name", "")},
            ))
    out.limitations.append("pptx 仅提取文本框内容；图表、SmartArt、表格单元格文字未包含")
    return out


def _read_pdf(path: Path) -> DocOutline:
    import fitz  # PyMuPDF

    out = DocOutline(kind="pdf")
    with fitz.open(path) as doc:
        out.page_count = doc.page_count
        for i, page in enumerate(doc, start=1):
            if len(out.elements) >= MAX_ELEMENTS:
                out.truncated = True
                break
            # 按块切分：块的 bounding box 可作为定位线索
            for bi, block in enumerate(page.get_text("blocks")):
                if len(out.elements) >= MAX_ELEMENTS:
                    out.truncated = True
                    break
                text = (block[4] or "").strip() if len(block) > 4 else ""
                if not text:
                    continue
                out.elements.append(Element(
                    address=f"pg:{i}:{bi}", kind="block", text=text,
                    meta={"page": i, "block": bi,
                          "bbox": [round(v, 1) for v in block[:4]]},
                ))
    out.limitations.append(
        "PDF 只能按文本块读取；写入依赖文本层遮盖重绘（会拆成独立文本块），"
        "复杂排版/扫描件不可编辑"
    )
    return out


# ---------------------------------------------------------------- 编辑


def apply_edits(path: Path, ext: str, edits: list[dict]) -> tuple[bytes, list[str]]:
    """按地址应用编辑，返回 (新文件字节, 变更说明)。

    edits 每项：
      {"address": "p:3" | "t:0:1:2" | "Sheet1!B2" | "s:2:0" | "pg:1:0",
       "text": "新文本"}
    或 {"address": "Sheet1!A1", "text": ..., "row_append": [...]} 等。
    """
    if not edits:
        raise ValidationError("没有要应用的编辑")
    if ext == "docx":
        return _edit_docx(path, edits)
    if ext in ("xlsx", "xlsm"):
        return _edit_xlsx(path, edits)
    if ext == "pptx":
        return _edit_pptx(path, edits)
    if ext == "pdf":
        return _edit_pdf(path, edits)
    raise ValidationError(f"不支持的结构化编辑格式：{ext}")


def _edit_docx(path: Path, edits: list[dict]) -> tuple[bytes, list[str]]:
    from docx import Document as DocxDocument

    doc = DocxDocument(str(path))
    changes: list[str] = []
    for e in edits:
        addr = str(e.get("address") or "")
        new_text = str(e.get("text") or "")
        if addr.startswith("p:"):
            idx = int(addr[2:])
            if idx >= len(doc.paragraphs):
                changes.append(_skip(addr, "段落不存在"))
                continue
            para = doc.paragraphs[idx]
            # 保留首个 run 的样式，清空其余 run
            if para.runs:
                para.runs[0].text = new_text
                for r in para.runs[1:]:
                    r.text = ""
            else:
                para.add_run(new_text)
            changes.append(f"{addr}: 段落已更新")
        elif addr.startswith("t:"):
            _, ti, ri, ci = addr.split(":")
            doc.tables[int(ti)].rows[int(ri)].cells[int(ci)].text = new_text
            changes.append(f"{addr}: 单元格已更新")
        else:
            changes.append(_skip(addr, "未知地址"))
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue(), changes


def _edit_xlsx(path: Path, edits: list[dict]) -> tuple[bytes, list[str]]:
    from openpyxl import load_workbook

    wb = load_workbook(str(path))  # 保留公式
    changes: list[str] = []
    for e in edits:
        addr = str(e.get("address") or "")
        if addr.startswith("append:"):
            sheet = addr.split(":", 1)[1]
            ws = wb[sheet] if sheet in wb.sheetnames else wb.active
            rows = e.get("rows") or []
            for row in rows:
                ws.append(list(row) if isinstance(row, (list, tuple)) else [row])
            changes.append(f"{sheet}: 追加 {len(rows)} 行")
            continue
        if "!" not in addr:
            changes.append(_skip(addr, "缺少工作表名"))
            continue
        sheet, coord = addr.rsplit("!", 1)
        if sheet not in wb.sheetnames:
            changes.append(_skip(addr, f"工作表 {sheet} 不存在"))
            continue
        wb[sheet][coord] = e.get("text")
        changes.append(f"{addr}: 已写入")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), changes


def _edit_pptx(path: Path, edits: list[dict]) -> tuple[bytes, list[str]]:
    from pptx import Presentation

    prs = Presentation(str(path))
    changes: list[str] = []
    for e in edits:
        addr = str(e.get("address") or "")
        if not addr.startswith("s:"):
            changes.append(_skip(addr, "未知地址"))
            continue
        _, si, shi = addr.split(":")
        slides = list(prs.slides)
        slide_idx, shape_idx = int(si) - 1, int(shi)
        if slide_idx >= len(slides):
            changes.append(_skip(addr, "幻灯片不存在"))
            continue
        shapes = list(slides[slide_idx].shapes)
        if shape_idx >= len(shapes):
            changes.append(_skip(addr, "形状不存在"))
            continue
        shape = shapes[shape_idx]
        if not getattr(shape, "has_text_frame", False):
            changes.append(_skip(addr, "不是文本框"))
            continue
        # 保留首段格式，其余段落清空
        tf = shape.text_frame
        paras = tf.paragraphs
        new_text = str(e.get("text") or "")
        first = paras[0]
        if first.runs:
            first.runs[0].text = new_text
            for r in first.runs[1:]:
                r.text = ""
        else:
            first.text = new_text
        for p in paras[1:]:
            p.text = ""
        changes.append(f"{addr}: 文本框已更新")
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue(), changes


def _edit_pdf(path: Path, edits: list[dict]) -> tuple[bytes, list[str]]:
    """PDF 文本替换：基于 PyMuPDF 的文本层搜索。

    局限：只替换能精确匹配的文本片段；跨行断字、扫描件、嵌入字体子集
    都可能导致失败或错位，失败项会明确列出。
    """
    import fitz

    doc = fitz.open(str(path))
    changes: list[str] = []
    for e in edits:
        addr = str(e.get("address") or "")
        find = str(e.get("find") or "")
        replace = str(e.get("text") or "")
        if not find:
            changes.append(_skip(addr, "PDF 编辑需要 find 字段（按原文搜索）"))
            continue
        page_no = None
        if addr.startswith("pg:"):
            try:
                page_no = int(addr.split(":")[1])
            except (IndexError, ValueError):
                page_no = None
        pages = [doc[page_no - 1]] if page_no and 1 <= page_no <= doc.page_count else list(doc)
        hit = 0
        for page in pages:
            rects = page.search_for(find)
            for r in rects:
                page.add_redact_annot(r, text=replace)
                hit += 1
            if rects:
                page.apply_redactions()
        changes.append(f"{addr}: 替换 {hit} 处" if hit else _skip(addr, f"未找到「{find}」"))
    buf = io.BytesIO()
    doc.save(buf, garbage=3, deflate=True)
    doc.close()
    return buf.getvalue(), changes


# ---------------------------------------------------------------- 对外接口


def outline_to_dict(o: DocOutline, *, limit: int = 300) -> dict:
    """把 outline 转成给 AI 的紧凑结构（限制条数）。"""
    items = o.elements[:limit]
    return {
        "kind": o.kind,
        "page_count": o.page_count,
        "sheets": o.sheets,
        "element_count": len(o.elements),
        "truncated": o.truncated or len(o.elements) > limit,
        "elements": [
            {"address": e.address, "kind": e.kind, "text": e.text[:500], **e.meta}
            for e in items
        ],
        "limitations": o.limitations,
    }
