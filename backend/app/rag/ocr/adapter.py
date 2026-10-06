"""MinerU middle 2.0 to project blocks; retains native evidence, never guesses a bbox."""

from __future__ import annotations

import re
from typing import Any

from app.errors import DocumentParseError
from app.rag.document import BlockType, DocumentBlock
from app.rag.ocr.mineru import Element, NativeDocument
from app.rag.parser import _block
from app.rag.tables import expand_html_table


def element_text(element: Element) -> str:
    if isinstance(element.content, str):
        return element.content
    # Native inline spans already contain spacing; don't concatenate block paragraphs here.
    return "".join(element_text(child) for child in element.content)


def heading_level(text: str, native: int | None) -> int:
    if re.match(r"^第[一二三四五六七八九十百零〇0-9]+章", text):
        return 1
    if re.match(r"^第[一二三四五六七八九十百零〇0-9]+节", text):
        return 2
    if re.match(r"^[一二三四五六七八九十]+[、.]", text):
        return 3
    return native or 3


def page_blocks(
    document: NativeDocument, source_page: int
) -> tuple[list[DocumentBlock], list[str]]:
    warnings: list[str] = []
    result: list[DocumentBlock] = []
    original = document.pages[0].blocks
    # Native semantic index, not a naive top-left geometric sort across columns/callouts.
    if all(item.index is not None for item in original):
        ordered = sorted(original, key=lambda item: item.index or 0)
        if original != ordered:
            warnings.append(f"PDF 第 {source_page} 页按引擎 block index 恢复顺序，请核对版面")
    else:
        ordered = original
        warnings.append(f"PDF 第 {source_page} 页缺少部分引擎顺序索引，保留返回顺序")
    has_body = False

    def convert(
        element: Element, parent: Element | None = None, table_id: str | None = None
    ) -> None:
        nonlocal has_body
        kind = element.type
        text = element_text(element).strip()
        bbox = element.bbox  # Never substitute parent's bbox as a child's own measured bbox.
        locator: dict[str, Any] = {
            "page_no": source_page,
            "parser": "mineru",
            "mineru_block_index": element.index,
            "bbox": bbox,
            "bbox_units": "normalized_page" if bbox is not None else None,
            "bbox_frame": "mineru_rendered_page" if bbox is not None else None,
        }
        if parent is not None:
            locator["parent_mineru_block_index"] = parent.index
        meta: dict[str, Any] = {
            "native_type": kind,
            "native_snapshot": element.model_dump(exclude_none=True),
            "parser_quality": "ocr",
            "ocr_engine": "mineru",
            "ocr_version": document.metadata.producer.version,
        }
        if parent is not None:
            meta["table_continues_prev"] = (parent.model_extra or {}).get("continues_prev") is True
        if table_id:
            meta["related_table_id"] = table_id
        if kind == "paragraph_title" and re.match(r"^【(?:答案|解析)】", text):
            kind = "text"
            meta["content_role"] = "answer_or_explanation"
        if kind == "header" and re.match(r"^第[一二三四五六七八九十百零〇0-9]+章", text):
            kind = "title"
            meta["chapter_header_candidate"] = True
            warnings.append(f"PDF 第 {source_page} 页章节性页眉作为候选章标题保留，需核对")
        if kind in {"header", "footer", "page_number"}:
            # Exclude running headers from body; retain the original native snapshot.
            warnings.append(f"PDF 第 {source_page} 页{kind} 不进正文，保留快照")
            return
        if kind in {"table", "image", "chart"} and isinstance(element.content, list):
            group_id = f"page:{source_page}:table:{element.index}" if kind == "table" else None
            for child in element.content:
                convert(child, element, group_id)
            return
        if kind in {"image_body", "chart_body"}:
            warnings.append(f"PDF 第 {source_page} 页图像/图表未索引，需核对")
            return
        if not text:
            if kind not in {"image", "chart"}:
                warnings.append(f"PDF 第 {source_page} 页 {kind} 块无识别文字")
            return
        if kind == "table_body":
            meta["table_id"] = table_id or f"page:{source_page}:table:{element.index}"
            try:
                table = expand_html_table(text)
            except DocumentParseError:
                # Keep native text rather than fabricate a logical grid.
                warnings.append(f"PDF 第 {source_page} 页表格结构降级，仅保留原生文本，请核对")
                meta["table_structure_degraded"] = True
                result.append(
                    _block("paragraph", text, page=source_page, locator=locator, meta=meta)
                )
            else:
                warnings.extend(
                    f"PDF 第 {source_page} 页：{message}" for message in table.pop("warnings")
                )
                normalized = table.pop("text")
                meta.update(table)
                locator["table_id"] = meta["table_id"]
                result.append(
                    _block("table", normalized, page=source_page, locator=locator, meta=meta)
                )
            has_body = True
        elif kind in {"paragraph_title", "title"}:
            level = heading_level(text, element.level)
            if has_body and level in {1, 2}:
                warnings.append(f"PDF 第 {source_page} 页章/节标题晚于正文，阅读顺序需核对")
            meta["title"] = text
            meta["native_heading_level"] = element.level
            result.append(
                _block("heading", text, level=level, page=source_page, locator=locator, meta=meta)
            )
        else:
            if kind not in {
                "text",
                "list",
                "table_caption",
                "table_footnote",
                "image_caption",
                "chart_caption",
            }:
                warnings.append(f"PDF 第 {source_page} 页未专门适配 {kind}，识别内容保留为正文")
            block_kind: BlockType = "list" if kind == "list" else "paragraph"
            result.append(_block(block_kind, text, page=source_page, locator=locator, meta=meta))
            has_body = True

    for element in ordered:
        convert(element)
    if not any(block["text"].strip() for block in result):
        raise DocumentParseError(f"PDF 第 {source_page} 页 OCR 无有效正文，不允许伪装为成功")
    return result, warnings
