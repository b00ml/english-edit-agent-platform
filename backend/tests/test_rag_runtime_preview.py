"""No-text and proxy-size regressions discovered with actual course PDFs."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter

from app.errors import KnowledgeNoIndexableTextError
from app.models import KnowledgeChunk, KnowledgeDocument
from app.services.knowledge_service import KnowledgeService


def blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=400, height=600)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("extension", ["pdf", "txt"])
def test_preview_no_text_is_explicit_and_no_model_is_invoked(db, monkeypatch, extension):
    def forbidden(*args, **kwargs):
        raise AssertionError("Preview must never call any embedding API")

    monkeypatch.setattr("app.rag.indexer.embed_texts", forbidden)
    content = blank_pdf() if extension == "pdf" else b"   "
    result = KnowledgeService(db).preview_file(f"empty.{extension}", content)
    assert result["indexable"] is False and result["chunks"] == []
    assert result["reason_code"] == "KNOWLEDGE_NO_INDEXABLE_TEXT"
    assert result["message"]
    assert db.query(KnowledgeDocument).count() == db.query(KnowledgeChunk).count() == 0


def test_scanned_or_blank_pdf_upload_refuses_before_embedding_or_database_write(db, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Empty PDF must be refused before paid embedding")

    monkeypatch.setattr("app.rag.indexer.embed_texts", forbidden)
    user = SimpleNamespace(id="u", tenant_id=None, role="researcher")
    with pytest.raises(KnowledgeNoIndexableTextError, match="OCR"):
        KnowledgeService(db).upload_file("scanned.pdf", blank_pdf(), "教材", None, user)
    assert db.query(KnowledgeChunk).count() == db.query(KnowledgeDocument).count() == 0


def test_text_preview_is_indexable_without_cloud_calls(db, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Preview must not embed")

    monkeypatch.setattr("app.rag.indexer.embed_texts", forbidden)
    result = KnowledgeService(db).preview_file(
        "grammar.md", b"# Grammar\n\nSubject verb agreement."
    )
    assert result["indexable"] is True and result["reason_code"] is None and result["chunks"]


def test_nginx_body_limit_allows_backend_file_limit_plus_multipart_overhead():
    import re

    config = (Path(__file__).parents[2] / "frontend/nginx.conf").read_text(encoding="utf-8")
    match = re.search(r"client_max_body_size\s+(\d+)m\s*;", config)
    assert match and int(match[1]) > 10


def test_image_build_excludes_workspace_api_and_worker_backups():
    ignored = (Path(__file__).parents[1] / ".dockerignore").read_text(encoding="utf-8")
    assert "app/api/routes_step1.py" in ignored and "app/api/routes_step2.py" in ignored
    assert "app/worker/tasks_backup.py" in ignored
