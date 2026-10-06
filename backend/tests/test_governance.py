"""P2 治理与 P1-5 运维脚本的纯单元回归测试。"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.model_governance import referenced_profile_names, validate_template_model_references
from app.outbox import _send_event, replay_dead
from app.versioning import ensure_model_profile_hash, model_profile_hash
from scripts.migration_precheck import _manual_heads


def test_model_profile_hash_changes_when_executable_config_changes() -> None:
    profile = SimpleNamespace(
        name="standard",
        provider="qwen",
        model_name="model-a",
        cost_tier="standard",
        is_default=True,
        status="enabled",
        max_fallbacks=1,
        budget_per_task=None,
        model_hash=None,
    )
    first = model_profile_hash(profile)
    profile.model_name = "model-b"
    second = model_profile_hash(profile)
    assert first != second
    assert ensure_model_profile_hash(profile) == second
    assert profile.model_hash == second


def test_referenced_profile_names_support_string_and_routing_map() -> None:
    assert referenced_profile_names({"model_profile": "standard"}) == {"standard"}
    assert referenced_profile_names({"model_profile": {"default": "standard", "hard": "high"}}) == {
        "standard",
        "high",
    }
    assert referenced_profile_names({"model_profile": {"default": 1}}) == set()


def test_validate_template_model_references_reports_missing_profiles() -> None:
    template = SimpleNamespace(
        type_id="reading",
        status="enabled",
        tenant_id=None,
        run_config={"model_profile": {"default": "standard", "hard": "missing"}},
    )

    class Query:
        def __init__(self, values):
            self.values = values

        def all(self):
            return self.values

        def filter(self, *_args):
            return self

    class Session:
        def query(self, entity):
            parent_class = getattr(getattr(entity, "parent", None), "class_", None)
            if getattr(parent_class, "__name__", "") == "ModelProfile":
                return Query([("standard",)])
            if getattr(entity, "__name__", "") == "ModelProfile":
                return Query([("standard",)])
            return Query([template])

    errors = validate_template_model_references(Session())
    assert errors == ["模板 reading 引用了不存在的模型档案: missing"]


def test_manual_heads_handles_multiline_merge_revision() -> None:
    versions_dir = Path(__file__).parents[1] / "alembic" / "versions"
    assert _manual_heads(versions_dir) == ["opt079_public_merge"]


def test_migration_precheck_adds_backend_root_for_direct_script_execution() -> None:
    script = Path(__file__).parents[1] / "scripts" / "migration_precheck.py"
    assert "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))" in script.read_text(
        encoding="utf-8"
    )


def test_batch_a_migration_does_not_duplicate_automatic_indexes() -> None:
    migration = (
        Path(__file__).parents[1] / "alembic" / "versions" / "m3_01_batch_a_p0.py"
    ).read_text(encoding="utf-8")
    assert "nullable=False, index=True" not in migration
    assert (
        "op.create_index('ix_generation_task_item_task_id'" in migration
        or 'op.create_index("ix_generation_task_item_task_id"' in migration
    )
    assert (
        "op.create_index('ix_generation_task_item_tenant_id'" in migration
        or 'op.create_index("ix_generation_task_item_tenant_id"' in migration
    )


def test_recovery_drill_exposes_two_phase_checkpoint_and_outbox_commands() -> None:
    script = Path(__file__).parents[1] / "scripts" / "recovery_drill.py"
    content = script.read_text(encoding="utf-8")
    assert 'choices=("checkpoint-start", "checkpoint-resume", "outbox")' in content
    assert "PostgresSaver" in content
    assert "replay_dead(session, event.id)" in content


def test_compose_requires_runtime_secrets_instead_of_inline_defaults() -> None:
    compose = Path(__file__).parents[2] / "deploy" / "docker-compose.yml"
    content = compose.read_text(encoding="utf-8")
    for variable in (
        "POSTGRES_PASSWORD",
        "JWT_SECRET",
        "SEED_ADMIN_PASSWORD",
        "LLM_API_KEY",
        "EMBEDDING_API_KEY",
        "LANGFUSE_NEXTAUTH_SECRET",
        "LANGFUSE_SALT",
        "LANGFUSE_ENCRYPTION_KEY",
    ):
        assert f"${{{variable}:?" in content
    assert "change-me-english-edit-jwt-secret" not in content
    assert "admin123" not in content


def test_send_event_passes_stable_event_id_as_celery_task_id() -> None:
    calls = []
    event = SimpleNamespace(
        event_id="task:abc:dispatch",
        event_type="generation_dispatch",
        task_id="abc",
        payload={"task_id": "abc"},
    )

    def sender(*args, **kwargs):
        calls.append((args, kwargs))

    _send_event(event, sender)
    assert calls == [
        (
            ("app.worker.tasks.process_generation_task", ["abc"]),
            {"task_id": "task:abc:dispatch"},
        )
    ]


def test_replay_dead_restores_pending_and_preserves_attempts(db) -> None:
    from app.models import GenerationTask, QuestionTemplate, TaskOutbox

    db.add(
        QuestionTemplate(
            id="template-outbox",
            type_id="single_choice",
            name="单选",
            version=1,
            input_schema={},
            output_schema={},
            quality_rules=[],
            gen_prompt={},
            run_config={},
            status="enabled",
        )
    )
    db.flush()
    db.add(GenerationTask(id="task-outbox", template_id="single_choice", params={}, quantity=1))
    event = TaskOutbox(
        id="outbox-1",
        event_id="task:outbox:dispatch",
        task_id="task-outbox",
        event_type="generation_dispatch",
        payload={"task_id": "task-outbox"},
        status="dead",
        attempts=5,
        last_error="broker unavailable",
        available_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.add(event)
    db.commit()

    restored = replay_dead(db, "outbox-1")
    assert restored.status == "pending"
    assert restored.attempts == 5
    assert restored.last_error == "broker unavailable"

    with pytest.raises(ValueError, match="is not dead"):
        replay_dead(db, "outbox-1")
