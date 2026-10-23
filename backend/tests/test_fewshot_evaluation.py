from __future__ import annotations

import pytest

from app.engine.fewshot import select_fewshot
from app.engine.fewshot_evaluation import (
    RUBRIC_DIMENSIONS,
    build_fewshot_evaluation,
    create_model_reviewed_sample,
    model_review_candidate,
    review_fewshot_evaluation,
)
from app.models import (
    ContentItem,
    GenerationTask,
    QualityRecord,
    QuestionTemplate,
)


def review(verdict: str = "pass") -> dict:
    return {
        "reviewer_model": "independent-reviewer-v1",
        "rubric_version": "rubric-v3",
        "judgments": {
            dimension: {"verdict": verdict, "evidence": f"evidence for {dimension}"}
            for dimension in RUBRIC_DIMENSIONS
        },
    }


def candidate(candidate_id, item_id, source_family, payload, status="passed"):
    return {
        "candidate_id": candidate_id,
        "item_id": item_id,
        "source_family": source_family,
        "payload": payload,
        "review": {**review(), "source": "model", "status": status},
    }


def seed_item(db):
    template = QuestionTemplate(
        id="template-row-fixture",
        type_id="fixture",
        name="Fixture",
        version=1,
        input_schema={"type": "object"},
        output_schema={
            "type": "object",
            "required": ["stem"],
            "properties": {"stem": {"type": "string", "minLength": 1}},
        },
        quality_rules=[],
        gen_prompt={},
        run_config={"fewshot": {"enabled": True}},
    )
    task = GenerationTask(
        id="task-model-review",
        template_id=template.type_id,
        params={"knowledge_point": "noun", "difficulty": "中"},
        quantity=1,
        status="succeeded",
    )
    db.add(template)
    db.flush()
    db.add(task)
    db.flush()
    item = ContentItem(
        id="item-model-review",
        task_id=task.id,
        template_id="fixture",
        payload={"stem": "A reviewed example."},
        status="passed",
    )
    db.add(item)
    db.commit()
    return template, task, item


def test_model_reviewed_sample_preserves_model_provenance_and_is_not_human(db):
    _, _, item = seed_item(db)

    sample = create_model_reviewed_sample(db, item, review(), source_family="book-a")

    assert sample.source == "model_review"
    assert sample.meta["review"]["source"] == "model"
    assert sample.meta["review"]["reviewer_model"] == "independent-reviewer-v1"
    assert sample.meta["review"]["rubric_version"] == "rubric-v3"
    assert set(sample.meta["review"]["judgments"]) == set(RUBRIC_DIMENSIONS)
    assert sample.meta["review"]["payload_hash"]
    assert sample.meta["source_family"] == "book-a"
    assert db.query(QualityRecord).filter_by(item_id=item.id).count() == 0


@pytest.mark.parametrize(("verdict", "expected"), [("fail", "rejected"), ("unresolved", "pending")])
def test_failed_or_uncertain_model_review_remains_out_of_fewshot(db, verdict, expected):
    template, _, item = seed_item(db)
    assessment = review()
    assessment["judgments"]["answer_correctness"]["verdict"] = verdict

    sample = create_model_reviewed_sample(db, item, assessment, source_family="book-a")
    sample.purpose = "fewshot"
    db.commit()

    assert sample.meta["review"]["status"] == expected
    assert sample.meta.get("evaluation_approval") is None
    assert db.query(QualityRecord).count() == 0
    assert not select_fewshot(db, template, {"knowledge_point": "noun"})["selected"]


def test_fewshot_comparison_excludes_item_family_and_near_duplicates():
    report = build_fewshot_evaluation(
        candidates=[
            candidate("c1", "i1", "family-a", {"stem": "The cat sits on the mat."}),
            candidate("c2", "i2", "family-b", {"stem": "Yesterday she walks to school."}),
            candidate("c3", "i3", "family-c", {"stem": "A unique unseen question."}),
            candidate("c4", "i4", "family-d", {"stem": "Pending review sample."}, status="pending"),
            candidate("c5", "i5", "family-e", {"stem": "A clean candidate."}),
        ],
        evaluation_cases=[
            {
                "case_id": "e1",
                "item_id": "i9",
                "source_family": "family-a",
                "payload": {"stem": "A separate question."},
            },
            {
                "case_id": "e2",
                "item_id": "i8",
                "source_family": "family-z",
                "payload": {"stem": "Yesterday, she walks to school!"},
            },
            {
                "case_id": "e3",
                "item_id": "i3",
                "source_family": "family-z",
                "payload": {"stem": "Another question."},
            },
            {
                "case_id": "e4",
                "item_id": "i4",
                "source_family": "family-z",
                "payload": {"stem": "A held out question."},
            },
        ],
        outcomes={
            "e1": {"without_fewshot": True, "with_fewshot": True},
            "e2": {"without_fewshot": False, "with_fewshot": True},
            "e3": {"without_fewshot": True, "with_fewshot": False},
        },
        config={"dataset_version": "v1", "fewshot_enabled": True},
    )

    assert report["candidate_split"]["eligible_candidate_ids"] == ["c5"]
    assert report["candidate_split"]["excluded"] == {
        "pending_or_failed_review": 1,
        "same_source_family": 1,
        "near_duplicate": 1,
        "same_item": 1,
    }
    assert report["effect"]["paired_denominator"] == 3
    assert report["effect"]["without_fewshot"]["numerator"] == 2
    assert report["effect"]["with_fewshot"]["numerator"] == 2
    assert report["effect"]["delta"] == 0.0
    assert report["status"] == "pending_review"


def test_fewshot_comparison_detects_chinese_near_duplicates():
    report = build_fewshot_evaluation(
        candidates=[candidate("c1", "i1", "source-a", {"stem": "小明每天早上去学校学习英语。"})],
        evaluation_cases=[
            {
                "case_id": "e1",
                "item_id": "i2",
                "source_family": "source-b",
                "payload": {"stem": "小明每天早上去学校学习英语！"},
            }
        ],
        outcomes={},
        config={"dataset_version": "v1"},
    )

    assert report["candidate_split"]["eligible_candidate_ids"] == []
    assert report["candidate_split"]["excluded"]["near_duplicate"] == 1


def test_fewshot_comparison_reports_negative_effect_without_clamping():
    report = build_fewshot_evaluation(
        candidates=[],
        evaluation_cases=[
            {
                "case_id": "e1",
                "item_id": "i1",
                "source_family": "source-a",
                "payload": {"stem": "A test case."},
            }
        ],
        outcomes={"e1": {"without_fewshot": True, "with_fewshot": False}},
        config={"dataset_version": "v1"},
    )

    assert report["effect"]["paired_denominator"] == 1
    assert report["effect"]["delta"] == -1.0


def test_model_reviewed_sample_is_not_selected_until_evaluation_approved(db):
    template, _, item = seed_item(db)
    sample = create_model_reviewed_sample(db, item, review(), source_family="book-a")
    sample.purpose = "fewshot"
    report = build_fewshot_evaluation(
        candidates=[model_review_candidate(sample)],
        evaluation_cases=[
            {
                "case_id": "e1",
                "item_id": "other",
                "source_family": "book-b",
                "payload": {"stem": "Different example."},
            }
        ],
        outcomes={"e1": {"without_fewshot": False, "with_fewshot": True}},
        config={"dataset_version": "v1"},
    )
    review_fewshot_evaluation(db, report, reviewer="owner")
    db.commit()

    pending = select_fewshot(db, template, {"knowledge_point": "noun"})
    assert not pending["selected"]
    assert pending["skipped"]["model_review_not_approved"] == 1

    review_fewshot_evaluation(db, report, reviewer="owner", approve=True)
    db.commit()
    selected = select_fewshot(db, template, {"knowledge_point": "noun"})
    assert selected["selected"][0]["sample_id"] == sample.id
    assert selected["selected"][0]["reviewer_model"] == "independent-reviewer-v1"
    assert selected["selected"][0]["review_source"] == "model_reference_review"
    assert selected["selected"][0]["reviewer"] is None
    assert selected["semantic_status"] == "model_reviewed_not_human_attested"
    assert db.query(QualityRecord).filter_by(item_id=item.id).count() == 0


def test_empty_paired_denominator_cannot_approve_model_sample(db):
    _, _, item = seed_item(db)
    sample = create_model_reviewed_sample(db, item, review(), source_family="book-a")
    report = build_fewshot_evaluation(
        candidates=[model_review_candidate(sample)],
        evaluation_cases=[
            {
                "case_id": "e1",
                "item_id": "other",
                "source_family": "book-b",
                "payload": {"stem": "Different example."},
            }
        ],
        outcomes={},
        config={"dataset_version": "v1"},
    )

    with pytest.raises(ValueError, match="without paired outcomes"):
        review_fewshot_evaluation(db, report, reviewer="owner", approve=True)

    assert sample.meta.get("evaluation_approval") is None
