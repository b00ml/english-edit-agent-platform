"""Whole, human-approved examples; isolated from RAG evidence and generation constraints."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.engine.content_validation import inspect_content
from app.models import ContentItem, GenerationTask, QualityRecord, QuestionTemplate, SamplePool
from app.rag.knowledge_points import normalized
from app.versioning import hash_value


def select_fewshot(
    session: Session,
    template: QuestionTemplate,
    params: dict[str, Any],
    *,
    task_id: str | None = None,
) -> dict[str, Any]:
    # Any at content JSON/template configuration boundary only.
    options = (template.run_config or {}).get("fewshot", {})
    enabled = options.get("enabled", settings.FEWSHOT_ENABLED)
    report: dict[str, Any] = {
        "enabled": bool(enabled),
        "selected": [],
        "skipped": {},
        "context": "",
        "semantic_status": "human_reviewed_not_expert_gold",
    }
    if not enabled:
        return report
    limit = min(
        settings.FEWSHOT_MAX_EXAMPLES,
        int(options.get("max_examples", settings.FEWSHOT_MAX_EXAMPLES)),
    )
    budget = min(
        settings.FEWSHOT_MAX_CHARS, int(options.get("max_chars", settings.FEWSHOT_MAX_CHARS))
    )
    kp = str(params.get("knowledge_point") or "").strip()
    if not kp or limit < 1 or budget < 1:
        report["reason"] = "no_knowledge_point_or_budget"
        return report
    # No cross-tenant fallback, even for admin-created tasks. Shared NULL is its own tenant.
    rows = (
        session.query(SamplePool)
        .filter(
            SamplePool.template_id == template.type_id,
            SamplePool.tenant_id == params.get("tenant_id"),
            SamplePool.purpose == "fewshot",
        )
        .order_by(SamplePool.created_at.desc(), SamplePool.id)
        .limit(settings.FEWSHOT_CANDIDATE_LIMIT)
        .all()
    )
    selected: list[dict[str, Any]] = []
    used = 0
    seen: set[str] = set()
    for sample in rows:
        reason = ""
        item = session.get(ContentItem, sample.item_id)
        task = session.get(GenerationTask, item.task_id) if item else None
        reviewer = (
            session.query(QualityRecord)
            .filter(
                QualityRecord.item_id == sample.item_id,
                QualityRecord.source == "manual_review",
                QualityRecord.tenant_id == params.get("tenant_id"),
            )
            .order_by(QualityRecord.created_at.desc(), QualityRecord.id.desc())
            .first()
        )
        if normalized(sample.knowledge_point or "") != normalized(kp):
            reason = "knowledge_point_mismatch"
        elif (
            item is None
            or item.tenant_id != params.get("tenant_id")
            or item.status not in {"passed", "published"}
            or item.template_id != template.type_id
        ):
            reason = "source_not_eligible"
        elif (
            task is None
            or task.id == task_id
            or task.tenant_id != item.tenant_id
            or task.template_id != template.type_id
        ):
            reason = "same_task_or_missing_source"
        elif (
            reviewer is None
            or reviewer.score != 100
            or not reviewer.reviewer
            or reviewer.reviewer == "unknown"
        ):
            reason = "human_review_missing"
        elif hash_value(sample.payload) != hash_value(item.payload):
            reason = "stale_payload"
        elif (item.provenance or {}).get("require_review") and not (
            (item.provenance or {}).get("reference_review") or {}
        ).get("verified"):
            reason = "reference_unverified"
        elif not inspect_content(sample.payload, template.output_schema, template.run_config).valid:
            reason = "current_schema_invalid"
        else:
            digest = hash_value(sample.payload)
            entry = {
                "knowledge_point": sample.knowledge_point,
                "difficulty": (task.params or {}).get("difficulty"),
                "example": sample.payload,
            }
            text = json.dumps(entry, ensure_ascii=False, sort_keys=True)
            if digest in seen:
                reason = "duplicate_payload"
            elif used + len(text) + (1 if selected else 0) > budget:
                reason = "whole_example_exceeds_budget"
            else:
                selected.append(entry)
                used += len(text) + (1 if len(selected) > 1 else 0)
                seen.add(digest)
                report["selected"].append(
                    {
                        "sample_id": sample.id,
                        "item_id": item.id,
                        "payload_hash": digest,
                        "review_record_id": reviewer.id,
                        "reviewer": reviewer.reviewer,
                        "template_version": template.version,
                    }
                )
        if reason:
            report["skipped"][reason] = report["skipped"].get(reason, 0) + 1
        if len(selected) >= limit:
            break
    report["context"] = "\n".join(
        json.dumps(entry, ensure_ascii=False, sort_keys=True) for entry in selected
    )
    report["chars"] = used
    return report
