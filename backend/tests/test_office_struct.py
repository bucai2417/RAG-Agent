"""办公文档结构化读写：读取元素树、按地址编辑、另存新文件。"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.core.errors import ValidationError
from app.services import office_struct_service as S


@pytest.fixture()
def tmpdir_path():
    return Path(tempfile.mkdtemp())


# ---------------------------------------------------------------- docx


def _make_docx(p: Path):
    from docx import Document

    d = Document()
    d.add_heading("项目周报", 0)
    d.add_paragraph("本周完成了技能市场功能。")
    d.add_paragraph("下周计划：模板优化。")
    t = d.add_table(rows=2, cols=3)
    for ci, v in enumerate(("模块", "状态", "负责人")):
        t.cell(0, ci).text = v
    for ci, v in enumerate(("技能市场", "已完成", "张三")):
        t.cell(1, ci).text = v
    d.save(p)


def test_read_docx_elements(tmpdir_path):
    p = tmpdir_path / "a.docx"
    _make_docx(p)
    o = S.read_outline(p)
    assert o.kind == "docx"
    addrs = [e.address for e in o.elements]
    assert "p:0" in addrs and "p:1" in addrs
    assert "t:0:1:1" in addrs
    # 空段落不返回
    assert all(e.text.strip() for e in o.elements)


def test_edit_docx_paragraph_and_cell(tmpdir_path):
    p = tmpdir_path / "a.docx"
    _make_docx(p)
    data, changes = S.apply_edits(p, "docx", [
        {"address": "p:2", "text": "下周计划：工作流模板优化。"},
        {"address": "t:0:1:1", "text": "已验收"},
    ])
    out = tmpdir_path / "b.docx"
    out.write_bytes(data)
    o = S.read_outline(out)
    m = {e.address: e.text for e in o.elements}
    assert m["p:2"] == "下周计划：工作流模板优化。"
    assert m["t:0:1:1"] == "已验收"
    assert len(changes) == 2


def test_edit_docx_missing_paragraph_reported(tmpdir_path):
    p = tmpdir_path / "a.docx"
    _make_docx(p)
    _, changes = S.apply_edits(p, "docx", [{"address": "p:999", "text": "x"}])
    assert any("不存在" in c for c in changes)


def test_edit_docx_preserves_other_content(tmpdir_path):
    p = tmpdir_path / "a.docx"
    _make_docx(p)
    data, _ = S.apply_edits(p, "docx", [{"address": "p:1", "text": "改过了"}])
    out = tmpdir_path / "b.docx"
    out.write_bytes(data)
    o = S.read_outline(out)
    texts = [e.text for e in o.elements]
    assert "改过了" in texts
    assert "项目周报" in texts, "其他段落应保留"


# ---------------------------------------------------------------- xlsx


def _make_xlsx(p: Path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["产品", "数量", "单价"])
    ws.append(["A", 10, 100])
    ws.append(["B", 5, 200])
    wb.save(p)


def test_read_xlsx_elements(tmpdir_path):
    p = tmpdir_path / "a.xlsx"
    _make_xlsx(p)
    o = S.read_outline(p)
    assert o.kind == "xlsx"
    assert o.sheets == ["Sheet1"]
    m = {e.address: e.text for e in o.elements}
    assert m["Sheet1!A1"] == "产品"
    assert m["Sheet1!B2"] == "10"


def test_edit_xlsx_cell(tmpdir_path):
    p = tmpdir_path / "a.xlsx"
    _make_xlsx(p)
    data, changes = S.apply_edits(p, "xlsx", [{"address": "Sheet1!B2", "text": 99}])
    out = tmpdir_path / "b.xlsx"
    out.write_bytes(data)
    o = S.read_outline(out)
    assert {e.address: e.text for e in o.elements}["Sheet1!B2"] == "99"


def test_edit_xlsx_append_rows(tmpdir_path):
    p = tmpdir_path / "a.xlsx"
    _make_xlsx(p)
    data, changes = S.apply_edits(p, "xlsx", [
        {"address": "append:Sheet1", "rows": [["C", 3, 50]]},
    ])
    out = tmpdir_path / "b.xlsx"
    out.write_bytes(data)
    o = S.read_outline(out)
    texts = [e.text for e in o.elements]
    assert "C" in texts and "50" in texts


def test_edit_xlsx_unknown_sheet_reported(tmpdir_path):
    p = tmpdir_path / "a.xlsx"
    _make_xlsx(p)
    _, changes = S.apply_edits(p, "xlsx", [{"address": "Nope!A1", "text": 1}])
    assert any("不存在" in c for c in changes)


def test_edit_xlsx_preserves_formula(tmpdir_path):
    """写入不应破坏其他单元格的公式。"""
    from openpyxl import Workbook

    p = tmpdir_path / "f.xlsx"
    wb = Workbook()
    ws = wb.active
    ws["A1"] = 10
    ws["A2"] = 20
    ws["A3"] = "=SUM(A1:A2)"
    wb.save(p)

    data, _ = S.apply_edits(p, "xlsx", [{"address": "B1", "text": "新增"}])
    out = tmpdir_path / "f2.xlsx"
    out.write_bytes(data)

    from openpyxl import load_workbook

    wb2 = load_workbook(str(out))
    assert wb2.active["A3"].value == "=SUM(A1:A2)"


# ---------------------------------------------------------------- pptx


def _make_pptx(p: Path):
    from pptx import Presentation

    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "季度汇报"
    s.placeholders[1].text = "要点一\n要点二"
    prs.save(p)


def test_read_pptx_elements(tmpdir_path):
    p = tmpdir_path / "a.pptx"
    _make_pptx(p)
    o = S.read_outline(p)
    assert o.kind == "pptx"
    assert o.page_count == 1
    m = {e.address: e.text for e in o.elements}
    assert m["s:1:0"] == "季度汇报"


def test_edit_pptx_text_frame(tmpdir_path):
    p = tmpdir_path / "a.pptx"
    _make_pptx(p)
    data, changes = S.apply_edits(p, "pptx", [{"address": "s:1:0", "text": "2026 年季度汇报"}])
    out = tmpdir_path / "b.pptx"
    out.write_bytes(data)
    o = S.read_outline(out)
    assert {e.address: e.text for e in o.elements}["s:1:0"] == "2026 年季度汇报"


def test_edit_pptx_missing_slide(tmpdir_path):
    p = tmpdir_path / "a.pptx"
    _make_pptx(p)
    _, changes = S.apply_edits(p, "pptx", [{"address": "s:99:0", "text": "x"}])
    assert any("不存在" in c for c in changes)


# ---------------------------------------------------------------- pdf


def _make_pdf(p: Path):
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Contract No: ABC-123", fontsize=14)
    doc.save(p)
    doc.close()


def test_read_pdf_blocks(tmpdir_path):
    p = tmpdir_path / "a.pdf"
    _make_pdf(p)
    o = S.read_outline(p)
    assert o.kind == "pdf"
    assert o.page_count == 1
    assert any("ABC-123" in e.text for e in o.elements)


def test_edit_pdf_replaces_text(tmpdir_path):
    p = tmpdir_path / "a.pdf"
    _make_pdf(p)
    data, changes = S.apply_edits(p, "pdf", [
        {"address": "pg:1:0", "find": "ABC-123", "text": "XYZ-999"},
    ])
    out = tmpdir_path / "b.pdf"
    out.write_bytes(data)
    o = S.read_outline(out)
    joined = " ".join(e.text for e in o.elements)
    assert "XYZ-999" in joined
    assert "ABC-123" not in joined


def test_edit_pdf_missing_text_reported(tmpdir_path):
    p = tmpdir_path / "a.pdf"
    _make_pdf(p)
    _, changes = S.apply_edits(p, "pdf", [
        {"address": "pg:1:0", "find": "不存在的文本", "text": "X"},
    ])
    assert any("未找到" in c for c in changes)


def test_edit_pdf_requires_find(tmpdir_path):
    p = tmpdir_path / "a.pdf"
    _make_pdf(p)
    _, changes = S.apply_edits(p, "pdf", [{"address": "pg:1:0", "text": "x"}])
    assert any("find" in c for c in changes)


# ---------------------------------------------------------------- 通用


def test_unsupported_format(tmpdir_path):
    p = tmpdir_path / "a.txt"
    p.write_text("hi", encoding="utf-8")
    with pytest.raises(ValidationError):
        S.read_outline(p)
    with pytest.raises(ValidationError):
        S.apply_edits(p, "txt", [{"address": "x", "text": "y"}])


def test_empty_edits_rejected(tmpdir_path):
    p = tmpdir_path / "a.docx"
    _make_docx(p)
    with pytest.raises(ValidationError):
        S.apply_edits(p, "docx", [])


def test_outline_to_dict_shape(tmpdir_path):
    p = tmpdir_path / "a.docx"
    _make_docx(p)
    d = S.outline_to_dict(S.read_outline(p), limit=5)
    assert d["kind"] == "docx"
    assert d["element_count"] > 0
    assert len(d["elements"]) <= 5
    assert "address" in d["elements"][0]
    assert d["limitations"]


def test_outline_truncates_long_text(tmpdir_path):
    from docx import Document

    p = tmpdir_path / "long.docx"
    d = Document()
    d.add_paragraph("x" * 2000)
    d.save(p)
    out = S.outline_to_dict(S.read_outline(p))
    assert len(out["elements"][0]["text"]) <= 500


# ---------------------------------------------------------------- 工具层


def test_tools_registered():
    from app.agents.tools.registry import registry

    names = {t.name for t in registry.all_builtin()}
    assert "read_doc_structure" in names
    assert "edit_doc_structure" in names


def test_edit_tool_is_write_kind():
    from app.agents.tools.office_struct_tools import (
        EditDocStructureTool,
        ReadDocStructureTool,
    )

    assert EditDocStructureTool().kind == "write"
    assert ReadDocStructureTool().kind == "read"


# ---------------------------------------------------------------- 未生效判定


def test_count_applied_ignores_skipped():
    """跳过项的说明必须以 SKIP_SUFFIX 结尾，否则统计会把它算成成功。"""
    changes = [
        "p:1: 段落已更新",
        S._skip("p:999", "段落不存在"),
        "Sheet1!B2: 已写入",
    ]
    assert S.count_applied(changes) == 2
    assert S.count_applied([S._skip("x:1", "未知地址")]) == 0
    assert S.count_applied([]) == 0


def test_all_skipped_edits_are_reported_as_skips(tmpdir_path):
    """每种格式的失败分支都要带上跳过标记（否则工具层无从判定「一条都没生效」）。"""
    p = tmpdir_path / "a.docx"
    _make_docx(p)
    _, changes = S.apply_edits(p, "docx", [
        {"address": "p:999", "text": "x"},
        {"address": "zzz:1", "text": "x"},
    ])
    assert len(changes) == 2
    assert S.count_applied(changes) == 0
    assert all(c.endswith(S.SKIP_SUFFIX) for c in changes)


def test_mixed_edits_count_only_applied(tmpdir_path):
    p = tmpdir_path / "a.docx"
    _make_docx(p)
    _, changes = S.apply_edits(p, "docx", [
        {"address": "p:1", "text": "改过了"},
        {"address": "p:999", "text": "x"},
    ])
    assert S.count_applied(changes) == 1


def test_pdf_missing_find_counts_as_skip(tmpdir_path):
    """PDF 忘了给 find：属于「未生效」，不能让调用方以为替换成功。"""
    import fitz

    p = tmpdir_path / "a.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Hello World")
    doc.save(p)
    doc.close()

    _, changes = S.apply_edits(p, "pdf", [{"address": "pg:1:0", "text": "x"}])
    assert changes and S.count_applied(changes) == 0


def _put_in_storage(tmpdir_path, monkeypatch) -> str:
    """把一份 docx 放进（已隔离到 data/test_files 的）真实 storage，返回 file_key。"""
    from app.ingest.storage import get_storage

    src = tmpdir_path / "a.docx"
    _make_docx(src)
    key, _ = get_storage().save(tenant_id=1, filename="a.docx", data=src.read_bytes())
    return key


def test_edit_tool_errors_out_when_nothing_applied(tmpdir_path, monkeypatch):
    """工具层：全部地址无效时必须 is_error=True，且不产出「已生成文件」。

    回归：此前所有地址都被跳过时仍返回「已生成修改后的文件」，AI 会当成成功。
    """
    import asyncio

    from app.agents.tools import office_struct_tools as T
    from app.agents.tools.base import ToolContext
    from app.middleware.auth_dep import PrincipalSet

    fk = _put_in_storage(tmpdir_path, monkeypatch)
    ctx = ToolContext(db=None, ps=PrincipalSet(user_id=1, tenant_id=1),
                      tenant_id=1, user_id=1)
    tool = T.EditDocStructureTool()
    res = asyncio.run(tool.execute(
        {"file_key": fk, "edits": [{"address": "p:999", "text": "x"}]}, ctx))
    assert res.is_error is True
    assert "没有任何编辑生效" in res.content
    assert res.data["applied"] == 0
    assert "file_key" not in res.data, "全部落空时不应产出文件"


def test_edit_tool_reports_partial_skip(tmpdir_path, monkeypatch):
    """部分生效：产出文件，但 applied/skipped 要分开报，便于前端标黄。"""
    import asyncio

    from app.agents.tools import office_struct_tools as T
    from app.agents.tools.base import ToolContext
    from app.middleware.auth_dep import PrincipalSet

    fk = _put_in_storage(tmpdir_path, monkeypatch)

    class _DB:
        """只满足 _save_artifact 的最小接口（add + flush）。"""

        def __init__(self):
            self.added = []

        def add(self, obj):
            self.added.append(obj)

        async def flush(self):
            return None

    ctx = ToolContext(db=_DB(), ps=PrincipalSet(user_id=1, tenant_id=1),
                      tenant_id=1, user_id=1)
    tool = T.EditDocStructureTool()
    res = asyncio.run(tool.execute({
        "file_key": fk,
        "edits": [
            {"address": "p:1", "text": "改过了"},
            {"address": "p:999", "text": "x"},
        ],
    }, ctx))
    assert res.is_error is False
    assert res.data["applied"] == 1 and res.data["skipped"] == 1
    assert "1 处生效" in res.content and "1 处跳过" in res.content
    assert res.data["file_key"].endswith(".docx")
