"""Explicit, bounded PDF page routing for CLI/background use, not synchronous HTTP preview."""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
import uuid
from typing import Any, Protocol

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError, PdfStreamError

from app.config import settings
from app.rag.document import DocumentBlock, ParsedDocument
from app.rag.ocr.adapter import page_blocks
from app.rag.ocr.mineru import MinerUClient, NativeDocument, OcrParseError
from app.rag.parser import _block, _finish


class PageParser(Protocol):
    def parse_page(self, content: bytes, filename: str = "page.pdf") -> NativeDocument: ...


def _nested_images(resources: Any, seen: set[int], depth: int = 0) -> bool:
    if depth > 16:
        raise OcrParseError("routing", "PDF 图像资源嵌套过深")
    resources = resources.get_object() if hasattr(resources, "get_object") else resources
    objects = resources.get("/XObject", {})
    objects = objects.get_object() if hasattr(objects, "get_object") else objects
    for value in objects.values():
        obj = value.get_object() if hasattr(value, "get_object") else value
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if obj.get("/Subtype") == "/Image":
            return True
        if obj.get("/Subtype") == "/Form" and _nested_images(
            obj.get("/Resources", {}), seen, depth + 1
        ):
            return True
    return False


def _page_signal(page: Any) -> tuple[str, float, int, bool]:
    area = float(page.mediabox.width) * float(page.mediabox.height)
    covered = 0.0
    images = 0

    def visit(operator: bytes, operands: Any, cm: Any, tm: Any) -> None:
        nonlocal covered, images
        if operator == b"INLINE IMAGE":
            images += 1
            covered += abs(float(cm[0]) * float(cm[3]) - float(cm[1]) * float(cm[2]))
            return
        if operator != b"Do" or not operands:
            return
        resources = page.get("/Resources", {})
        resources = resources.get_object() if hasattr(resources, "get_object") else resources
        objects = resources.get("/XObject", {})
        objects = objects.get_object() if hasattr(objects, "get_object") else objects
        obj = objects.get(operands[0])
        obj = obj.get_object() if hasattr(obj, "get_object") else obj
        if obj and obj.get("/Subtype") == "/Image":
            images += 1
            # A PDF image paints a unit square through the current transformation matrix.
            covered += abs(float(cm[0]) * float(cm[3]) - float(cm[1]) * float(cm[2]))

    text = page.extract_text(visitor_operand_before=visit) or ""
    resources = page.get("/Resources", {})
    resources = resources.get_object() if hasattr(resources, "get_object") else resources
    objects = resources.get("/XObject", {})
    objects = objects.get_object() if hasattr(objects, "get_object") else objects
    uncertain = any(
        obj.get("/Subtype") == "/Form" and _nested_images(obj.get("/Resources", {}), set())
        for value in objects.values()
        for obj in [value.get_object() if hasattr(value, "get_object") else value]
    )
    return text, min(1.0, covered / area) if area > 0 else 0.0, images, uncertain


def parse_pdf_with_ocr(
    filename: str,
    content: bytes,
    *,
    page_numbers: list[int] | None = None,
    client: PageParser | None = None,
) -> ParsedDocument:
    if not content or len(content) > settings.RAG_OCR_MAX_INPUT_BYTES:
        raise OcrParseError("input", "本地 PDF 为空或超过 OCR 输入字节上限")
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise OcrParseError("input", "加密 PDF 必须先解密")
        count = len(reader.pages)
        selected = page_numbers if page_numbers is not None else list(range(1, count + 1))
        if (
            not selected
            or len(selected) > settings.RAG_OCR_MAX_PAGES
            or selected != sorted(set(selected))
            or any(type(page) is not int or not 1 <= page <= count for page in selected)
        ):
            raise OcrParseError(
                "input", "请选择有序、不重复且在范围内的 PDF 页码，页数不得超过配置上限"
            )
        parse_trace_id = str(uuid.uuid4())
        blocks: list[DocumentBlock] = []
        warnings = [
            "本地 OCR/普通文本按页路由；版面、英文词边界、跨页续表和源资料正确性仍需人工核验"
        ]
        routes = []
        snapshots = []
        engine = client
        previous_chapter: str | None = None
        previous_page: int | None = None
        for number in selected:
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
                previous_chapter = None
                warnings.append(f"PDF 所选页在第 {number} 页前不连续，已重置章节上下文")
            previous_page = number
            page = reader.pages[number - 1]
            try:
                text, ratio, images, uncertain = _page_signal(page)
            except (PdfReadError, PdfStreamError, ValueError, TypeError) as exc:
                # Don't silently accept an extraction failure as an empty/blank page.
                raise OcrParseError(
                    "routing", f"第 {number} 页无法可靠判断文字/图像（{type(exc).__name__}）"
                ) from exc
            chars = sum(not char.isspace() for char in text)
            scanned = (
                uncertain
                or ratio >= settings.RAG_PDF_SCAN_IMAGE_RATIO
                or (chars < settings.RAG_PDF_MIN_TEXT_CHARS and images > 0)
            )
            route: dict[str, Any] = {
                "page_no": number,
                "native_non_empty_chars": chars,
                "image_area_ratio_estimate": ratio,
                "image_count": images,
                "nested_image_resources_uncertain": uncertain,
                "route": "ocr" if scanned else "native" if text.strip() else "blank",
            }
            routes.append(route)
            if uncertain:
                warnings.append(f"PDF 第 {number} 页含 Form 内图像，面积无法可靠测定，保守走 OCR")
            if scanned:
                if engine is None:
                    engine = MinerUClient()
                writer = PdfWriter()
                writer.add_page(page)
                buffer = io.BytesIO()
                writer.write(buffer)
                page_started = time.monotonic()
                page_content = buffer.getvalue()
                result = engine.parse_page(page_content, f"page-{number}.pdf")
                route["ocr_latency_seconds"] = round(time.monotonic() - page_started, 3)
                route["ocr_input_hash"] = hashlib.sha256(page_content).hexdigest()
                route["trace_id"] = parse_trace_id
                converted, messages = page_blocks(result, number)
                for block in converted:
                    if block["block_type"] == "heading" and block["level"] == 1:
                        chapter_key = re.sub(r"[\s|｜]", "", block["text"])
                        if (
                            block["meta"].get("chapter_header_candidate")
                            and chapter_key == previous_chapter
                        ):
                            continue
                        previous_chapter = chapter_key
                    blocks.append(block)
                warnings.extend(messages)
                snapshots.append(
                    {"source_page_no": number, "native_result": result.model_dump(by_alias=True)}
                )
                route["ocr_version"] = result.metadata.producer.version
                route["ocr_output_hash"] = hashlib.sha256(
                    result.model_dump_json().encode()
                ).hexdigest()
            elif text.strip():
                warnings.append(f"PDF 第 {number} 页使用普通文字抽取，未承诺其表格/多栏布局正确")
                blocks.append(
                    _block(
                        "paragraph",
                        text,
                        page=number,
                        locator={"page_no": number, "bbox": None, "bbox_units": None},
                        meta={"parser_quality": "text"},
                    )
                )
            else:
                warnings.append(f"PDF 第 {number} 页无文字/直接图像，按空页保留；未调用 OCR")
                blocks.append(_block("page_break", "", page=number, locator={"page_no": number}))
        incomplete = len(selected) != count
        if incomplete:
            warnings.append("仅处理所选 PDF 页，不代表整份资料已解析；当前禁止此部分结果正式入库")
        doc = _finish(
            filename,
            blocks,
            warnings,
            original_text=json.dumps(snapshots, ensure_ascii=False),
            stats={
                "parse_trace_id": parse_trace_id,
                "page_count": count,
                "selected_pages": selected,
                "page_routes": routes,
                "partial_document": incomplete,
                "ocr_page_count": sum(r["route"] == "ocr" for r in routes),
                "image_signal_method": (
                    "pypdf direct image CTM area estimate; "
                    "nested Form images conservatively use OCR"
                ),
            },
        )
        doc["source_hash"] = hashlib.sha256(content).hexdigest()
        doc["parser_version"] = "rag-parser-v2+mineru-middle2-v1"
        return doc
    except (PdfReadError, PdfStreamError) as exc:
        raise OcrParseError("input", "PDF 无法读取") from exc
