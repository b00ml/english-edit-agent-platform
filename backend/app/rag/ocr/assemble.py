"""Assemble only committed page checkpoints; normalize order/context without re-running OCR."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from app.rag.document import ParsedDocument
from app.rag.parser import _block, _finish


def assemble(
    filename: str, source_hash: str, page_count: int, documents: list[ParsedDocument], job_id: str
) -> ParsedDocument:
    blocks = []
    warnings: list[str] = []
    routes: list[dict[str, Any]] = []
    snapshots = []
    selected = []
    previous_page = None
    previous_chapter = None
    for document in documents:
        numbers = document["stats"]["selected_pages"]
        if len(numbers) != 1 or document["source_hash"] != source_hash:
            raise ValueError("OCR 单页检查点不属于当前资料")
        number = numbers[0]
        if previous_page is not None and number <= previous_page:
            raise ValueError("OCR 页检查点顺序错误")
        if previous_page is not None and number != previous_page + 1:
            blocks.append(
                _block(
                    "page_break",
                    "",
                    page=number,
                    locator={"page_no": number},
                    meta={"reset_section_context": True},
                )
            )
            warnings.append(f"PDF 第 {number} 页前选页不连续，重置章节上下文")
            previous_chapter = None
        selected.append(number)
        previous_page = number
        for block in copy.deepcopy(document["blocks"]):
            if block["block_type"] == "heading" and block["level"] == 1:
                chapter = re.sub(r"[\s|｜]", "", block["text"])
                if block["meta"].get("chapter_header_candidate") and chapter == previous_chapter:
                    continue
                previous_chapter = chapter
            blocks.append(block)
        warnings.extend(
            message for message in document["warnings"] if "仅处理所选 PDF 页" not in message
        )
        routes.extend(document["stats"]["page_routes"])
        snapshots.extend(json.loads(document["original_text"] or "[]"))
    partial = len(selected) != page_count
    if partial:
        warnings.append("仅完成所选 PDF 页，未完成全文件解析，不能正式入库")
    result = _finish(
        filename,
        blocks,
        list(dict.fromkeys(warnings)),
        original_text=json.dumps(snapshots, ensure_ascii=False),
        stats={
            "page_count": page_count,
            "selected_pages": selected,
            "page_routes": routes,
            "partial_document": partial,
            "ocr_job_id": job_id,
            "ocr_page_count": sum(route["route"] == "ocr" for route in routes),
        },
    )
    result["source_hash"] = source_hash
    result["parser_version"] = "rag-parser-v2+mineru-middle2-v1"
    return result
