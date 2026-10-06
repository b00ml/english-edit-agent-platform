"""Main structure/relation defaults, legacy restoration, and source-required generation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, settings
from app.errors import RagContextRequiredError
from app.models import KnowledgeDocument, OcrJob
from app.rag import retriever
from app.rag.indexer import configured_chunking
from app.rag.preview import preview_document
from app.services.knowledge_service import KnowledgeService
from app.services.ocr_review import ReviewApproval, approve, plan
from app.services.ocr_service import OcrService
from app.worker.ocr_index import run_index
from app.workflow import graph as workflow
from tests.test_ocr_review import ready
from tests.test_rag_str12 import actor, source


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RAG_CHUNK_LAYOUT", "structure")
    monkeypatch.setattr(settings, "RAG_CONTEXT_MODE", "relation")
    monkeypatch.setattr(settings, "RAG_QUERY_PLANNING_MODE", "off")
    monkeypatch.setattr(settings, "RAG_QUERY_EXPANSION_MODE", "off")
    monkeypatch.setattr(settings, "RAG_RERANK_MODE", "off")
    monkeypatch.setattr(settings, "RAG_STRUCTURE_ENABLED", True)
    monkeypatch.setattr(
        "app.rag.indexer.embed_texts", lambda texts, **kw: [[1.0] + [0.0] * 1023 for _ in texts]
    )
    monkeypatch.setattr(
        retriever, "embed_texts", lambda texts, **kw: [[1.0] + [0.0] * 1023 for _ in texts]
    )
    monkeypatch.setattr(retriever, "record_lifecycle_event", lambda **kw: None)


@pytest.fixture()
def ocr_runtime(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[OcrService, SimpleNamespace, sessionmaker[Session]]:
    monkeypatch.setattr(settings, "RAG_OCR_STORAGE_DIR", str(tmp_path / "ocr"))
    monkeypatch.setattr(settings, "RAG_OCR_ENGINE", "mineru")
    monkeypatch.setattr(settings, "RAG_OCR_URL", "http://localhost:16580")
    monkeypatch.setattr("app.services.ocr_review.send_index", lambda _: None)
    owner = SimpleNamespace(id=None, tenant_id="owner", role="admin", status="active")
    return OcrService(db, lambda _: None), owner, sessionmaker(bind=db.get_bind())


def test_default_config_and_explicit_legacy_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ["RAG_CHUNK_LAYOUT", "RAG_CONTEXT_MODE"]:
        monkeypatch.delenv(key, raising=False)
    cfg = Settings(_env_file=None)
    assert (cfg.RAG_CHUNK_LAYOUT, cfg.RAG_CONTEXT_MODE) == ("structure", "relation")
    old = Settings(_env_file=None, RAG_CHUNK_LAYOUT="legacy", RAG_CONTEXT_MODE="legacy")
    assert (old.RAG_CHUNK_LAYOUT, old.RAG_CONTEXT_MODE) == ("legacy", "legacy")
    assert configured_chunking().layout == "structure"
    assert configured_chunking("legacy").layout == "legacy"


def test_default_structural_preview_crosses_pages_and_legacy_remains_explicit() -> None:
    doc = source()
    structural = preview_document(doc)
    old = preview_document(doc, chunk_layout="legacy")
    assert structural["chunk_layout"] == "structure" and old["chunk_layout"] == "legacy"
    assert any(len(chunk["pages"]) > 1 for chunk in structural["chunks"])
    assert all(chunk["content_start"] is None for chunk in structural["chunks"])
    assert any(chunk["content_start"] is not None for chunk in old["chunks"])


@pytest.mark.parametrize("layout", [None, "legacy"])
def test_native_upload_inherits_default_or_honors_explicit_rollback(
    db: Session, layout: str | None
) -> None:
    result = KnowledgeService(db).upload_file(
        "grammar.md",
        b"# Nouns\n\nA noun names a person, place, thing or idea.",
        "教材",
        "名词",
        actor(),
        chunk_layout=layout,
    )
    doc = db.query(KnowledgeDocument).one()
    assert result["chunks"] > 0
    assert doc.meta["chunk_layout"] == (layout or "structure")


def test_catalog_and_native_preview_share_current_configuration(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.routes import router
    from app.database import get_db
    from app.security import get_current_user

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: actor()
    with TestClient(app) as client:
        for layout, mode in [("structure", "relation"), ("legacy", "legacy")]:
            monkeypatch.setattr(settings, "RAG_CHUNK_LAYOUT", layout)
            monkeypatch.setattr(settings, "RAG_CONTEXT_MODE", mode)
            defaults = client.get("/api/knowledge/points").json()["defaults"]
            assert defaults["chunk_layout"] == layout and defaults["context_mode"] == mode
            assert defaults["context_max_chars"] == settings.RAG_CONTEXT_MAX_CHARS
            response = client.post(
                "/api/knowledge/preview",
                files={"file": ("rules.md", b"# Unit\n\nAn attributed rule.", "text/markdown")},
            )
            assert response.status_code == 200 and response.json()["chunk_layout"] == layout
    assert db.query(KnowledgeDocument).count() == 0


def test_unindexed_ocr_plan_uses_structure_without_paid_calls(
    db: Session,
    ocr_runtime: tuple[OcrService, SimpleNamespace, sessionmaker[Session]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = ready(db, ocr_runtime)
    monkeypatch.setattr("app.rag.indexer.embed_texts", lambda *a, **kw: pytest.fail("free preview"))
    result = plan(db, identifier, ocr_runtime[1])
    assert result["chunk_layout"] == "structure"
    assert ocr_runtime[0].summary(db.get(OcrJob, identifier))["index_present"] is False
    assert db.query(KnowledgeDocument).count() == 0


@pytest.mark.parametrize("historical_missing_layout", [False, True])
def test_existing_ocr_index_restores_legacy_without_migration_on_read(
    db: Session,
    ocr_runtime: tuple[OcrService, SimpleNamespace, sessionmaker[Session]],
    historical_missing_layout: bool,
) -> None:
    identifier = ready(db, ocr_runtime)
    result = plan(db, identifier, ocr_runtime[1], chunk_layout="legacy")
    body = ReviewApproval(
        preview_hash=result["preview_hash"],
        plan_hash=result["plan_hash"],
        source_reviewed=True,
        warnings_acknowledged=True,
        paid_embedding_acknowledged=True,
        chunk_layout="legacy",
    )
    approve(db, identifier, body, ocr_runtime[1])
    run_index(identifier, factory=ocr_runtime[2])
    job = db.get(OcrJob, identifier)
    db.refresh(job)
    doc = db.get(KnowledgeDocument, job.indexed_document_id)
    if historical_missing_layout:
        job.approval = {k: v for k, v in job.approval.items() if k != "chunk_layout"}
        doc.meta = {k: v for k, v in doc.meta.items() if k != "chunk_layout"}
        db.commit()
    before = doc.content_hash, doc.meta["index_revision"]
    details = ocr_runtime[0].summary(job)
    assert details["review_settings"]["chunk_layout"] == "legacy"
    assert (doc.content_hash, doc.meta["index_revision"]) == before
    assert db.query(KnowledgeDocument).count() == 1


@pytest.mark.parametrize("name", ["single_choice", "cloze", "reading"])
def test_builtin_knowledge_templates_require_source_and_reference_review(name: str) -> None:
    data = yaml.safe_load(
        (Path(__file__).parents[1] / "app/templates" / (name + ".yaml")).read_text(encoding="utf-8")
    )
    assert data["version"] == 3
    assert data["run_config"]["rag"] == {"mode": "required", "require_human_verification": True}
    assert "knowledge_point" in data["input_schema"]["required"]


def test_source_required_generation_refuses_no_match_before_chat(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = SimpleNamespace(type_id="fixture", run_config={"rag": {"mode": "required"}})
    monkeypatch.setattr(workflow, "_load_template", lambda *a: template)
    monkeypatch.setattr(workflow, "generate_with_fallback", lambda *a, **kw: pytest.fail("no chat"))
    with pytest.raises(RagContextRequiredError):
        workflow.generate_node(
            {
                "params": {"template_id": "fixture", "knowledge_point": "名词"},
                "trace_id": "no-source:0",
                "task_id": "no-source",
            },
            db,
        )


def test_generation_injects_real_indexed_segments_but_preserves_original_params(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    KnowledgeService(db).upload_file(
        "grammar.md",
        b"# Nouns\n\nA noun names a person, place, thing or idea.",
        "教材",
        "名词",
        actor(),
    )
    template = SimpleNamespace(
        type_id="fixture",
        run_config={"rag": {"mode": "required", "require_human_verification": True}},
    )
    monkeypatch.setattr(workflow, "_load_template", lambda *a: template)
    captured = {}

    def generated(
        template: SimpleNamespace, params: dict[str, Any], *a: object, **kw: object
    ) -> dict[str, str]:
        captured.update(params)
        return {"stem": "Which word is a noun?"}

    monkeypatch.setattr(workflow, "generate_with_fallback", generated)
    params = {"template_id": "fixture", "knowledge_point": "名词", "tenant_id": "owner"}
    result = workflow.generate_node(
        {"params": params, "trace_id": "source:0", "task_id": "source"}, db
    )
    assert "rag_context" not in params
    assert "A noun names" in captured["rag_context"]
    assert result["rag_provenance"]["require_review"] is True
    assert result["rag_provenance"]["protocol_version"] == "rag-context-v2"
    assert result["rag_provenance"]["citations"]
