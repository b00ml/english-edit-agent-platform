"""OCR contracts use synthetic layouts and real tiny PDFs, never cloud/model calls."""

from __future__ import annotations

import hashlib
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from app.config import Settings, settings
from app.errors import DocumentParseError, InvalidKnowledgeError
from app.rag.chunking import ChunkingConfig, split_document
from app.rag.ocr.adapter import page_blocks
from app.rag.ocr.mineru import MinerUClient, OcrParseError, local_url, validate_native
from app.rag.ocr.pipeline import parse_pdf_with_ocr
from app.rag.parser import _finish
from app.rag.preview import preview_document
from app.rag.tables import expand_html_table
from app.services.knowledge_service import KnowledgeService

TABLE = (
    '<table><tr><td colspan="2">Category</td><td>Example</td></tr>'
    '<tr><td rowspan="2">Noun</td><td>person</td><td>Alice</td></tr>'
    "<tr><td>country</td><td>China</td></tr></table>"
)


def native(blocks=None):
    return {
        "schema": "docvortex.middle",
        "schema_version": "2.0",
        "metadata": {"producer": {"name": "mineru", "version": "4.0.10"}},
        "pages": [
            {
                "page_idx": 0,
                "blocks": blocks
                or [
                    {
                        "type": "paragraph_title",
                        "index": 0,
                        "level": 2,
                        "bbox": [0.1, 0.1, 0.9, 0.2],
                        "content": [{"type": "text", "content": "第一章 名词"}],
                    },
                    {
                        "type": "table",
                        "index": 1,
                        "bbox": [0.1, 0.3, 0.9, 0.8],
                        "content": [
                            {
                                "type": "table_body",
                                "index": 1,
                                "bbox": [0.1, 0.3, 0.9, 0.7],
                                "content": TABLE,
                            },
                            {
                                "type": "table_footnote",
                                "index": 2,
                                "content": [{"type": "text", "content": "Keep source notes."}],
                            },
                        ],
                    },
                ],
            }
        ],
        "is_full_document": True,
    }


def pdf(kinds):
    writer = PdfWriter()
    for kind in kinds:
        page = writer.add_blank_page(width=400, height=600)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        resources = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        drawing = b""
        if kind in {"scan", "scan_with_text"}:
            image = DecodedStreamObject()
            image.set_data(b"\xff\xff\xff")
            image.update(
                {
                    NameObject("/Type"): NameObject("/XObject"),
                    NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(1),
                    NameObject("/Height"): NumberObject(1),
                    NameObject("/BitsPerComponent"): NumberObject(8),
                    NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                }
            )
            resources[NameObject("/XObject")] = DictionaryObject(
                {NameObject("/Im0"): writer._add_object(image)}
            )
            drawing += b"q 400 0 0 600 0 0 cm /Im0 Do Q\n"
        if kind in {"native", "scan_with_text"}:
            drawing += (
                b"BT /F1 12 Tf 20 500 Td (He works every day. This is a native text layer.) Tj ET"
            )
        page[NameObject("/Resources")] = resources
        stream = DecodedStreamObject()
        stream.set_data(drawing)
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class FakeParser:
    def __init__(self):
        self.calls = []

    def parse_page(self, content, filename="page.pdf"):
        self.calls.append((content, filename))
        return validate_native(native())


def test_merge_grid_preserves_labels_and_origin_spans():
    table = expand_html_table(TABLE)
    assert table["rows"] == [
        ["Category", "Category", "Example"],
        ["Noun", "person", "Alice"],
        ["Noun", "country", "China"],
    ]
    assert table["spans"][1]["colspan"] == 1
    assert any(
        span["row"] == 1 and span["column"] == 0 and span["rowspan"] == 2 for span in table["spans"]
    )
    assert table["normalized_row_to_source"] == [1, 2]
    assert table["original_html"] == TABLE


def test_multiheader_is_combined_with_row_mapping_and_empty_cells():
    html = (
        '<table><thead><tr><th rowspan="2">Name</th><th colspan="2">Usage</th></tr>'
        "<tr><th>English</th><th>Chinese</th></tr></thead>"
        "<tr><td>x | y</td><td></td><td>例子</td></tr></table>"
    )
    result = expand_html_table(html)
    assert result["header_rows"] == 2
    assert "Usage > English" in result["text"] and r"x \| y" in result["text"]
    assert result["rows"][2][1] == ""
    assert result["normalized_row_to_source"] == [2]


@pytest.mark.parametrize(
    "body",
    [
        '<table><tr><td rowspan="0">x</td></tr></table>',
        '<table><tr><td colspan="9999999">x</td></tr></table>',
        '<table><tr><td rowspan="x">x</td></tr></table>',
        '<table><tr><td rowspan="2">x</td></tr></table>',
        "<table><tr><td>x<table><tr><td>y</td></tr></table></td></tr></table>",
        "<table><tr><td>x</td></tr>",
        '<table><tr><td colspan="2">a</td><td rowspan="2">b</td></tr>'
        '<tr><td colspan="3">c</td></tr></table>',
    ],
)
def test_bad_spans_and_incomplete_tables_fail_explicitly(body):
    with pytest.raises(DocumentParseError):
        expand_html_table(body)


def test_adapter_preserves_page_bbox_footnote_and_heading_level():
    blocks, warnings = page_blocks(validate_native(native()), 8)
    assert blocks[0]["level"] == 1
    table = next(block for block in blocks if block["block_type"] == "table")
    assert table["page_no"] == 8 and table["source_locator"]["bbox_units"] == "normalized_page"
    assert table["meta"]["rows"][2][0] == "Noun"
    assert table["source_locator"]["bbox_frame"] == "mineru_rendered_page"
    note = blocks[-1]
    assert note["text"] == "Keep source notes." and note["source_locator"]["bbox"] is None
    assert note["meta"]["related_table_id"] == table["meta"]["table_id"]
    assert warnings  # native td-only table has an inferred candidate header


def test_adapter_reading_order_warning_and_native_index_restoration():
    blocks = [
        {"type": "paragraph_title", "index": 5, "content": "第一章 Test"},
        {"type": "text", "index": 0, "content": "Earlier body"},
    ]
    result, warnings = page_blocks(validate_native(native(blocks)), 1)
    assert result[0]["text"] == "Earlier body"
    assert any("顺序" in warning for warning in warnings)
    assert any("晚于正文" in warning for warning in warnings)


def test_table_plaintext_is_kept_but_not_fabricated_into_cells():
    result, warnings = page_blocks(
        validate_native(
            native([{"type": "table_body", "index": 0, "content": "Noun   Alice   China"}])
        ),
        2,
    )
    assert result[0]["block_type"] == "paragraph"
    assert result[0]["meta"]["table_structure_degraded"]
    assert "Alice" in result[0]["text"] and any("降级" in item for item in warnings)


def test_headers_and_page_numbers_stay_in_native_snapshot_not_body():
    data = native(
        [
            {"type": "header", "index": 0, "content": "running header"},
            {"type": "text", "index": 1, "content": "real body"},
            {"type": "page_number", "index": 2, "content": "102"},
        ]
    )
    blocks, warnings = page_blocks(validate_native(data), 1)
    assert [block["text"] for block in blocks] == ["real body"]
    assert len(warnings) == 2


@pytest.mark.parametrize("change", ["schema", "version", "bbox", "page", "duplicate", "partial"])
def test_native_contract_fails_closed(change):
    data = native()
    if change == "schema":
        data["schema_version"] = "1.0"
    elif change == "version":
        data["metadata"]["producer"]["version"] = "3.0.0"
    elif change == "bbox":
        data["pages"][0]["blocks"][0]["bbox"] = [0, 0, float("nan"), 1]
    elif change == "page":
        data["pages"][0]["page_idx"] = 9
    elif change == "duplicate":
        data["pages"][0]["blocks"][1]["index"] = 0
    elif change == "partial":
        data["is_full_document"] = False
    with pytest.raises(OcrParseError):
        validate_native(data)


@pytest.mark.parametrize(
    "url",
    [
        "https://mineru.net/api",
        "http://8.8.8.8",
        "http://user:key@localhost",
        "http://localhost?token=x",
    ],
)
def test_cloud_and_url_credentials_are_rejected(url):
    with pytest.raises(OcrParseError):
        local_url(url)


def test_mixed_pdf_routes_actual_source_pages_and_keeps_hash():
    content = pdf(["native", "scan", "blank"])
    engine = FakeParser()
    doc = parse_pdf_with_ocr("mixed.pdf", content, client=engine)
    assert [route["route"] for route in doc["stats"]["page_routes"]] == ["native", "ocr", "blank"]
    assert len(engine.calls) == 1 and engine.calls[0][1] == "page-2.pdf"
    assert doc["source_hash"] == hashlib.sha256(content).hexdigest()
    assert any(block["page_no"] == 2 and block["block_type"] == "table" for block in doc["blocks"])
    assert doc["stats"]["partial_document"] is False
    assert '"source_page_no": 2' in doc["original_text"]


def test_full_image_pdf_with_embedded_text_still_routes_to_ocr():
    engine = FakeParser()
    doc = parse_pdf_with_ocr("scan.pdf", pdf(["scan_with_text"]), client=engine)
    assert len(engine.calls) == 1
    assert doc["stats"]["page_routes"][0]["image_area_ratio_estimate"] == 1


def test_native_pdf_never_constructs_ocr_client(monkeypatch):
    def forbidden():
        raise AssertionError("Native text must not run OCR")

    monkeypatch.setattr("app.rag.ocr.pipeline.MinerUClient", forbidden)
    assert parse_pdf_with_ocr("text.pdf", pdf(["native"]))["stats"]["ocr_page_count"] == 0


@pytest.mark.parametrize("pages", [[2, 1], [1, 1], [0], [9], []])
def test_invalid_page_selection_refuses_before_ocr(pages):
    with pytest.raises(OcrParseError, match="请选择"):
        parse_pdf_with_ocr(
            "test.pdf", pdf(["scan", "native"]), page_numbers=pages, client=FakeParser()
        )


def test_page_and_byte_limits_do_not_claim_complete_preview(monkeypatch):
    monkeypatch.setattr(settings, "RAG_OCR_MAX_PAGES", 1)
    content = pdf(["scan", "native"])
    with pytest.raises(OcrParseError):
        parse_pdf_with_ocr("test.pdf", content, client=FakeParser())
    doc = parse_pdf_with_ocr("test.pdf", content, page_numbers=[1], client=FakeParser())
    result = preview_document(doc)
    assert result["indexable"] is False and result["chunks"]
    assert result["reason_code"] == "KNOWLEDGE_PARTIAL_DOCUMENT"
    monkeypatch.setattr(settings, "RAG_OCR_MAX_INPUT_BYTES", 10)
    with pytest.raises(OcrParseError):
        parse_pdf_with_ocr("test.pdf", content)


def test_partial_upload_never_embeds_or_writes_db(db, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Partial result must not embed")

    monkeypatch.setattr("app.rag.indexer.embed_texts", forbidden)
    doc = parse_pdf_with_ocr(
        "test.pdf", pdf(["scan", "native"]), page_numbers=[1], client=FakeParser()
    )
    with pytest.raises(InvalidKnowledgeError, match="部分"):
        KnowledgeService(db).upload_text(
            doc, "教材", "test", None, None, SimpleNamespace(tenant_id=None)
        )


def test_table_chunks_keep_inherited_merge_label_header_and_source_slice():
    blocks, warnings = page_blocks(validate_native(native()), 8)
    doc = _finish("test.pdf", blocks, warnings)
    chunks, diagnostics = split_document(
        doc, ChunkingConfig(chunk_size=100, chunk_overlap=10, min_chunk_chars=10)
    )
    tables = [chunk for chunk in chunks if chunk.table_id]
    assert tables and diagnostics.table_chunks > 0
    for chunk in chunks:
        assert chunk.content == doc["text"][chunk.start : chunk.end]
    assert all("Category" in chunk.embedding_content for chunk in tables)
    assert all(chunk.page_no == 8 and chunk.source_locator[0]["bbox"] for chunk in tables)
    assert any("Noun" in chunk.content and "China" in chunk.content for chunk in tables)


def http_handler(mode="ok", seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        path, method = request.url.path, request.method
        if path == "/v1/health":
            return httpx.Response(200, json={"status": "healthy"})
        if path == "/v1/uploads" and method == "POST":
            return httpx.Response(
                200,
                json={
                    "id": "u1",
                    "status": "pending",
                    "upload_url": "http://8.8.8.8/content" if mode == "foreign" else "/upload/u1",
                },
            )
        if path == "/upload/u1":
            return httpx.Response(204)
        if path.endswith("/complete"):
            return httpx.Response(200, json={"file": {"id": "f1"}})
        if path == "/v1/parse/jobs" and method == "POST":
            return httpx.Response(200, json={"job_id": "j1", "status": "queued"})
        if path == "/v1/parse/jobs/j1" and method == "DELETE":
            return httpx.Response(204)
        if path == "/v1/parse/jobs/j1":
            if mode == "timeout":
                raise httpx.ReadTimeout("private upstream token", request=request)
            if mode == "partial":
                return httpx.Response(200, json={"job_id": "j1", "status": "partial"})
            return httpx.Response(
                200,
                json={
                    "job_id": "j1",
                    "status": "completed",
                    "files": [{"output_files": {"middle_json": {"file_id": "out1"}}}],
                },
            )
        if path == "/v1/files/out1/content":
            if mode == "badjson":
                return httpx.Response(200, content=b"not json secret")
            if mode == "oversize":
                return httpx.Response(200, content=b"x" * 2048)
            return httpx.Response(200, json=native())
        return httpx.Response(500)

    return handler


def test_v1_http_client_uploads_bytes_and_validates_native_result(monkeypatch):
    monkeypatch.setattr("app.rag.ocr.mineru.time.sleep", lambda _: None)
    requests = []
    client = MinerUClient(
        url="http://127.0.0.1:16580", transport=httpx.MockTransport(http_handler(seen=requests))
    )
    result = client.parse_page(b"fake-pdf")
    assert result.metadata.producer.version == "4.0.10"
    upload = next(request for request in requests if request.method == "PUT")
    assert upload.content == b"fake-pdf"
    submit = next(request for request in requests if request.url.path == "/v1/parse/jobs")
    assert json.loads(submit.content)["tier"] == "basic"
    assert json.loads(submit.content)["output_formats"] == ["middle_json"]


@pytest.mark.parametrize("mode", ["foreign", "timeout", "partial", "badjson", "oversize"])
def test_http_failure_never_returns_partial_or_exposes_upstream_secrets(monkeypatch, mode):
    monkeypatch.setattr("app.rag.ocr.mineru.time.sleep", lambda _: None)
    if mode == "oversize":
        monkeypatch.setattr(settings, "RAG_OCR_MAX_RESULT_BYTES", 1024)
    client = MinerUClient(
        url="http://localhost:16580", transport=httpx.MockTransport(http_handler(mode))
    )
    with pytest.raises(OcrParseError) as error:
        client.parse_page(b"pdf")
    assert "secret" not in str(error.value) and "private upstream token" not in str(error.value)


def test_disabled_engine_does_not_select_cloud_default(monkeypatch):
    monkeypatch.setattr(settings, "RAG_OCR_ENGINE", "off")
    with pytest.raises(OcrParseError, match="未开启"):
        MinerUClient()


@pytest.mark.parametrize(
    "field,value",
    [("RAG_OCR_MAX_PAGES", 0), ("RAG_OCR_PAGE_TIMEOUT", 0), ("RAG_PDF_SCAN_IMAGE_RATIO", 2)],
)
def test_ocr_settings_limits_are_validated(field, value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{field: value})


def test_chapter_like_header_is_preserved_as_heading_candidate():
    data = native(
        [
            {"type": "header", "index": 0, "content": "第二章|名词"},
            {"type": "text", "index": 1, "content": "Body content"},
        ]
    )
    blocks, warnings = page_blocks(validate_native(data), 2)
    assert blocks[0]["block_type"] == "heading" and blocks[0]["level"] == 1
    assert blocks[0]["meta"]["chapter_header_candidate"]
    assert any("候选" in warning for warning in warnings)


def test_nested_form_image_is_not_misclassified_as_blank():
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=600)
    image = DecodedStreamObject()
    image.set_data(b"\xff\xff\xff")
    image.update(
        {
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(1),
            NameObject("/Height"): NumberObject(1),
            NameObject("/BitsPerComponent"): NumberObject(8),
            NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
        }
    )
    form = DecodedStreamObject()
    form.set_data(b"q 400 0 0 600 0 0 cm /Im0 Do Q")
    from pypdf.generic import ArrayObject

    form.update(
        {
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject([NumberObject(value) for value in [0, 0, 400, 600]]),
            NameObject("/Resources"): DictionaryObject(
                {NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): image})}
            ),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/XObject"): DictionaryObject({NameObject("/Form0"): form})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"/Form0 Do")
    page[NameObject("/Contents")] = stream
    output = io.BytesIO()
    writer.write(output)
    engine = FakeParser()
    doc = parse_pdf_with_ocr("form.pdf", output.getvalue(), client=engine)
    assert len(engine.calls) == 1
    assert doc["stats"]["page_routes"][0]["nested_image_resources_uncertain"]
    assert any("保守走 OCR" in warning for warning in doc["warnings"])


def test_timeout_requests_best_effort_cancellation(monkeypatch):
    monkeypatch.setattr("app.rag.ocr.mineru.time.sleep", lambda _: None)
    requests = []
    client = MinerUClient(
        url="http://localhost:16580",
        transport=httpx.MockTransport(http_handler("timeout", requests)),
    )
    with pytest.raises(OcrParseError):
        client.parse_page(b"pdf")
    assert any(
        request.method == "DELETE" and request.url.path.endswith("/j1") for request in requests
    )


def test_upstream_page_failure_does_not_fallback_to_partial_document():
    class FailedParser:
        def parse_page(self, content, filename="page.pdf"):
            raise OcrParseError("poll", "fail")

    with pytest.raises(OcrParseError):
        parse_pdf_with_ocr("mixed.pdf", pdf(["native", "scan"]), client=FailedParser())


def test_ordinary_http_preview_never_implicitly_uses_enabled_ocr(db, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("HTTP preview must remain native-only")

    monkeypatch.setattr(settings, "RAG_OCR_ENGINE", "mineru")
    monkeypatch.setattr("app.rag.ocr.mineru.MinerUClient.parse_page", forbidden)
    result = KnowledgeService(db).preview_file("scan.pdf", pdf(["scan"]))
    assert not result["indexable"] and result["chunks"] == []


def test_ocr_preview_cli_writes_only_artifact_and_refuses_source_overwrite(tmp_path, monkeypatch):
    from scripts import ocr_preview

    source = tmp_path / "source.pdf"
    source.write_bytes(pdf(["native"]))
    output = tmp_path / "preview.json"
    monkeypatch.setattr(
        "sys.argv", ["ocr_preview", "--input", str(source), "--output", str(output)]
    )
    assert ocr_preview.main() == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["indexable"] and result["chunks"]
    original = source.read_bytes()
    monkeypatch.setattr(
        "sys.argv", ["ocr_preview", "--input", str(source), "--output", str(source)]
    )
    with pytest.raises(SystemExit):
        ocr_preview.main()
    assert source.read_bytes() == original


def test_answer_marker_does_not_replace_semantic_section_heading():
    data = native(
        [
            {"type": "paragraph_title", "index": 0, "content": "第一章 Grammar"},
            {"type": "text", "index": 1, "content": "Question stem with options A and B."},
            {"type": "paragraph_title", "index": 2, "level": 2, "content": "【答案】A"},
            {"type": "text", "index": 3, "content": "Answer explanation."},
        ]
    )
    blocks, warnings = page_blocks(validate_native(data), 1)
    assert blocks[2]["block_type"] == "paragraph"
    assert blocks[2]["meta"]["content_role"] == "answer_or_explanation"
    chunks, _ = split_document(
        _finish("test.pdf", blocks, warnings), ChunkingConfig(chunk_size=200, min_chunk_chars=10)
    )
    assert all("【答案】A" not in chunk.section_path for chunk in chunks)


def test_selected_page_gap_resets_stale_section_context():
    class Parser:
        def __init__(self):
            self.counter = 0

        def parse_page(self, content, filename="page.pdf"):
            self.counter += 1
            blocks = (
                [
                    {"type": "paragraph_title", "index": 0, "content": "第一章 Old"},
                    {"type": "text", "index": 1, "content": "Earlier page body"},
                ]
                if self.counter == 1
                else [{"type": "text", "index": 0, "content": "Later page with unknown heading"}]
            )
            return validate_native(native(blocks))

    doc = parse_pdf_with_ocr(
        "gap.pdf", pdf(["scan", "blank", "scan"]), page_numbers=[1, 3], client=Parser()
    )
    chunks, _ = split_document(doc, ChunkingConfig(chunk_size=200, min_chunk_chars=10))
    assert any(chunk.page_no == 1 and chunk.section_path for chunk in chunks)
    assert all(not chunk.section_path for chunk in chunks if chunk.page_no == 3)
    assert any("不连续" in warning for warning in doc["warnings"])


@pytest.mark.parametrize("version", ["4.0.10", "3.0.0", "unknown"])
def test_live_cache_version_is_validated_without_cloud_fallback(version):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"version": version}))
    client = MinerUClient(url="http://localhost:16580", transport=transport)
    if version == "4.0.10":
        assert client.server_version() == version
    else:
        with pytest.raises(OcrParseError):
            client.server_version()


def test_background_cancel_check_cancels_submitted_upstream_job(monkeypatch):
    requests = []

    def handler(request):
        response = http_handler("ok", requests)(request)
        return response

    def cancelled():
        return any(request.url.path == "/v1/parse/jobs" for request in requests)

    monkeypatch.setattr("app.rag.ocr.mineru.time.sleep", lambda _: None)
    client = MinerUClient(
        url="http://localhost:16580", transport=httpx.MockTransport(handler), cancel_check=cancelled
    )
    with pytest.raises(OcrParseError, match="取消"):
        client.parse_page(b"pdf")
    assert any(
        request.method == "DELETE" and request.url.path.endswith("/j1") for request in requests
    )
