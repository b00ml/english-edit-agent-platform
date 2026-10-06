"""Real PostgresSaver + separate processes. Requires a migrated TEST_DATABASE_URL.

LLM and RAG are deterministic local stubs; no Redis, Docker or paid API is required.
These tests are intentionally not executed without a PostgreSQL test database.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

# Child process uses this script as its entrypoint, not pytest import setup.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.integration


def child(args: list[str]) -> None:
    # Spawned process must set database config BEFORE importing the app singleton.
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    os.environ["CHECKPOINTER_BACKEND"] = "postgres"
    os.environ["ENVIRONMENT"] = "development"
    from langgraph.checkpoint.postgres import PostgresSaver

    from app.database import SessionLocal
    from app.workflow import graph as wf

    phase, task_id, template_id, mode, counter_file = args
    counter = Path(counter_file)

    def generated(*_args, **_kwargs):
        with counter.open("a", encoding="utf-8") as file:
            file.write("generated\n")
        return {"stem": "deterministic test only"}

    wf.generate_with_fallback = generated
    wf.run_quality_check = lambda *_args, **_kwargs: (
        65.0 if mode == "gray" else 90.0,
        {"a": 65.0 if mode == "gray" else 90.0},
    )
    wf.build_rag_context = lambda *_args, **_kwargs: ""
    assert isinstance(wf._get_checkpointer(), PostgresSaver), "MemorySaver is not a recovery proof"
    if mode == "crash" and phase == "start":
        wf.qc_node = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated process failure")
        )
    with SessionLocal() as db:
        thread = f"{task_id}:0"
        if phase == "review":
            result = wf.resume_human_review(
                thread, {"approved": True, "score": 100, "reviewer_id": "test-human"}, db
            )
        else:
            try:
                result = wf.run_generation(task_id, template_id, {}, db, thread)
            except RuntimeError:
                if mode != "crash" or phase != "start":
                    raise
                result = {"status": "expected-crash"}
        sys.stdout.write(
            json.dumps({"status": result.get("status"), "content_id": result.get("content_id")})
            + "\n"
        )
    wf.reset_checkpointer()


def invoke(phase: str, task_id: str, template_id: str, mode: str, counter: Path) -> dict:
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            phase,
            task_id,
            template_id,
            mode,
            str(counter),
        ],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=60,
        check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("mode", ["complete", "crash", "gray"])
def test_business_checkpoint_survives_process_exit(pg_engine, tmp_path, mode, request):
    from sqlalchemy.orm import sessionmaker

    SessionLocal = sessionmaker(bind=pg_engine)
    from app.models import ContentItem, GenerationTask, QualityRecord, QuestionTemplate

    template_id = f"recovery_{uuid.uuid4().hex}"
    with SessionLocal() as db:
        db.add(
            QuestionTemplate(
                type_id=template_id,
                name="Recovery test",
                version=1,
                input_schema={},
                output_schema={"required": ["stem"]},
                quality_rules=[{"id": "a", "weight": 1}],
                gen_prompt={},
                run_config={
                    "quality_threshold": 70,
                    "human_review": {"enabled": mode == "gray", "gray_margin": 10},
                },
            )
        )
        db.flush()
        task = GenerationTask(template_id=template_id, params={}, quantity=1)
        db.add(task)
        db.commit()
        task_id = task.id

    # Cross-process recovery must commit rows; clean only this UUID-owned task,
    # template and checkpoint thread, never truncate the reused database.
    def clean_own_records() -> None:
        from sqlalchemy import text

        from app.models import AppNotification, QualityEvaluation, SamplePool, TraceLog

        with SessionLocal() as cleanup:
            ids = [row.id for row in cleanup.query(ContentItem).filter_by(task_id=task_id)]
            cleanup.query(TraceLog).filter_by(task_id=task_id).delete(synchronize_session=False)
            cleanup.query(QualityEvaluation).filter_by(task_id=task_id).delete(
                synchronize_session=False
            )
            if ids:
                cleanup.query(QualityRecord).filter(QualityRecord.item_id.in_(ids)).delete(
                    synchronize_session=False
                )
                cleanup.query(SamplePool).filter(SamplePool.item_id.in_(ids)).delete(
                    synchronize_session=False
                )
                cleanup.query(AppNotification).filter(AppNotification.related_id.in_(ids)).delete(
                    synchronize_session=False
                )
            cleanup.query(ContentItem).filter_by(task_id=task_id).delete(synchronize_session=False)
            cleanup.query(GenerationTask).filter_by(id=task_id).delete(synchronize_session=False)
            cleanup.query(QuestionTemplate).filter_by(type_id=template_id).delete(
                synchronize_session=False
            )
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                if cleanup.execute(text("SELECT to_regclass(:name)"), {"name": table}).scalar():
                    cleanup.execute(
                        text(f"DELETE FROM {table} WHERE thread_id=:thread"),
                        {"thread": f"{task_id}:0"},
                    )
            cleanup.commit()

    request.addfinalizer(clean_own_records)
    counter = tmp_path / "generation-calls.txt"
    first = invoke("start", task_id, template_id, mode, counter)
    if mode == "gray":
        assert first["status"] == "awaiting_review"
        final = invoke("review", task_id, template_id, mode, counter)
        assert final["status"] == "review_passed"
    else:
        final = invoke("resume", task_id, template_id, mode, counter)
        assert final["status"] == "stored"
    replay = invoke("resume", task_id, template_id, mode, counter)
    assert replay["content_id"] == final["content_id"]
    assert counter.read_text(encoding="utf-8").splitlines() == ["generated"]
    with SessionLocal() as db:
        assert db.query(ContentItem).filter_by(task_id=task_id).count() == 1
        item = db.query(ContentItem).filter_by(task_id=task_id).one()
        if mode == "gray":
            assert item.status == "passed"
            assert (
                db.query(QualityRecord).filter_by(item_id=item.id, source="manual_review").count()
                == 1
            )


if __name__ == "__main__":
    child(sys.argv[1:])
