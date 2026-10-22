"""办公文档结构化读写工具：让 AI 能按「地址」精确读取与修改文档。

与 `read_file` / `edit_file` 的分工：
- `read_file`：整篇文本，适合「帮我总结这份文档」
- `read_doc_structure`：元素树（段落/单元格/幻灯片/文本块 + 地址），
  适合「第 3 段写了什么」「这个表第 2 行第 3 列是什么」
- `edit_file`：整篇查找替换，适合全局改名
- `edit_doc_structure`：按地址精确改写，适合「把第 3 段改成 X」

三者共用同一份 storage 与 Artifact 登记逻辑。
"""
from __future__ import annotations

from pathlib import Path

from app.agents.tools.base import ToolContext, ToolResult, WriteToolMixin
from app.agents.tools.file_tools import _check_tenant_key, _save_artifact, _stem
from app.ingest.storage import get_storage
from app.services import office_struct_service as S

_EXT_MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pdf": "application/pdf",
}


class ReadDocStructureTool:
    """读取文档的可寻址元素树。"""

    name = "read_doc_structure"
    description = (
        "按「可寻址元素」读取 docx/xlsx/pptx/pdf：返回段落、表格单元格、幻灯片文本框、"
        "PDF 文本块，每项带 address。适合需要精确定位的任务（引用某段、改某个单元格）。\n"
        "只是要概括全文时用 read_file 更省 token。\n"
        "返回的 address 可直接作为 edit_doc_structure 的地址。"
    )
    required_permission = "file:read"
    kind = "read"
    parameters = {
        "type": "object",
        "properties": {
            "file_key": {"type": "string", "description": "文件 file_key"},
            "limit": {"type": "integer", "description": "最多返回多少元素，默认 300"},
            "kind_filter": {
                "type": "string",
                "description": "可选：只返回某类元素（paragraph / table_cell / cell / shape / block）",
            },
        },
        "required": ["file_key"],
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        fk = str(args.get("file_key") or "")
        err = _check_tenant_key(ctx, fk)
        if err:
            return ToolResult(content=err, is_error=True)
        src = get_storage().path(fk)
        if not src.is_file():
            return ToolResult(content="文件不存在", is_error=True)

        ext = src.suffix.lower().lstrip(".")
        try:
            outline = S.read_outline(src, ext)
        except Exception as e:  # noqa: BLE001
            return ToolResult(content=f"读取失败：{str(e)[:300]}", is_error=True)

        kind_filter = str(args.get("kind_filter") or "").strip()
        if kind_filter:
            outline.elements = [e for e in outline.elements if e.kind == kind_filter]

        limit = max(1, min(int(args.get("limit") or 300), 1000))
        data = S.outline_to_dict(outline, limit=limit)

        lines = [f"{data['kind']} 文档：{data['element_count']} 个可编辑元素"]
        if data["sheets"]:
            lines.append(f"工作表：{'、'.join(data['sheets'])}")
        if data["page_count"]:
            lines.append(f"页/幻灯片数：{data['page_count']}")
        if data["truncated"]:
            lines.append(f"（仅列出前 {len(data['elements'])} 个）")
        lines.append("")
        for el in data["elements"]:
            lines.append(f"[{el['address']}] {el['text'][:200]}")
        if data["limitations"]:
            lines.append("")
            lines.append("局限：" + "；".join(data["limitations"]))
        return ToolResult(content="\n".join(lines), data=data)


class EditDocStructureTool(WriteToolMixin):
    """按地址精确改写文档。"""

    name = "edit_doc_structure"
    description = (
        "按 address 精确改写 docx/xlsx/pptx/pdf，另存为新文件供下载（不改原文件）。\n"
        "address 来自 read_doc_structure 的返回，例如：\n"
        "  docx: p:3（第 4 段）/ t:0:1:2（第 1 张表第 2 行第 3 列）\n"
        "  xlsx: Sheet1!B2 / append:Sheet1（追加行，需配 rows）\n"
        "  pptx: s:2:0（第 2 页第 1 个形状）\n"
        "  pdf:  pg:1:0 需同时给 find（按原文搜索替换，PDF 无法按元素定位）\n"
        "批量编辑一次传多条 edits，顺序应用。"
    )
    required_permission = "file:write"
    parameters = {
        "type": "object",
        "properties": {
            "file_key": {"type": "string", "description": "源文件 file_key"},
            "edits": {
                "type": "array",
                "items": {"type": "object"},
                "description": (
                    "编辑列表，每项 {address, text}；xlsx 追加行用 "
                    "{address:'append:Sheet1', rows:[[...],[...]]}；"
                    "pdf 用 {address:'pg:1:0', find:'原文', text:'新文本'}"
                ),
            },
            "output_filename": {"type": "string", "description": "输出文件名（默认 原名-edited.扩展名）"},
        },
        "required": ["file_key", "edits"],
    }

    def summarize(self, args: dict) -> str:
        n = len(args.get("edits") or [])
        return f"精确编辑 {args.get('file_key')}（{n} 处）"

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        fk = str(args.get("file_key") or "")
        err = _check_tenant_key(ctx, fk)
        if err:
            return ToolResult(content=err, is_error=True)

        edits = args.get("edits")
        if not isinstance(edits, list) or not edits:
            return ToolResult(content="edits 不能为空", is_error=True)
        for e in edits:
            if not isinstance(e, dict) or not e.get("address"):
                return ToolResult(content="每条编辑都必须有 address", is_error=True)

        src = get_storage().path(fk)
        if not src.is_file():
            return ToolResult(content="源文件不存在", is_error=True)

        ext = src.suffix.lower().lstrip(".")
        if ext not in _EXT_MIME:
            return ToolResult(content=f"不支持的结构化编辑格式：{ext}", is_error=True)

        try:
            data, changes = S.apply_edits(src, ext, edits)
        except Exception as e:  # noqa: BLE001
            return ToolResult(content=f"编辑失败：{str(e)[:300]}", is_error=True)

        applied = S.count_applied(changes)
        summary = "\n".join(f"- {c}" for c in changes)
        # 一条都没落地：不能返回「已生成文件」的成功结果 —— AI 会据此
        # 认为改写完成，继续在错误的前提上往下做。
        if applied == 0:
            return ToolResult(
                content=(
                    f"没有任何编辑生效（共 {len(changes)} 条）：\n{summary}\n\n"
                    "请先用 read_doc_structure 确认 address，再重试。"
                ),
                is_error=True,
                data={"changes": changes, "applied": 0},
            )

        out_name = str(args.get("output_filename") or f"{_stem(src.name)}-edited.{ext}")
        if not out_name.lower().endswith(f".{ext}"):
            out_name = f"{out_name}.{ext}"

        file_key, art, _ = _save_artifact(ctx, out_name, data, _EXT_MIME[ext])
        await ctx.db.flush()

        skipped = len(changes) - applied
        head = f"已生成修改后的文件：{out_name}（{applied} 处生效"
        head += f"，{skipped} 处跳过）" if skipped else "）"
        return ToolResult(
            content=f"{head}\n{summary}",
            data={
                "file_key": file_key, "artifact_id": art.id, "name": out_name,
                "mime": _EXT_MIME[ext], "size": len(data),
                "url": f"/api/v1/chat/attachments/{file_key}",
                "changes": changes, "applied": applied, "skipped": skipped,
                # 部分跳过仍然产出了文件，不算失败；但把 skipped 交给前端标黄，
                # 并让模型在阅读结果时能看到哪些地址没命中。
            },
        )


OFFICE_STRUCT_TOOLS = [ReadDocStructureTool(), EditDocStructureTool()]
