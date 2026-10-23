"""Real evaluation behavior with a fake provider; no network or official inputs."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.engine.evaluation_protocol import write_manifest
from app.engine.real_evaluation import EvaluationLedger, run_generation
from app.errors import EvaluationTransportError, EvaluationUnresolvedError

BACKEND = Path(__file__).parents[1]
VALID = {
    "stem": "Test-only question?",
    "options": ["one", "two", "three", "four"],
    "answer": "A",
    "explanation": "Test-only explanation.",
}


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
        self.options = None
        self.chat = SimpleNamespace(completions=self)

    def with_options(self, **kwargs):
        self.options = kwargs
        return self

    def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(response)))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
        )


def local_manifest():
    template = yaml.safe_load((BACKEND / "app/templates/single_choice.yaml").read_text("utf-8"))
    return {
        "run_id": "fixture-run",
        "protocol": {
            "call_limits": {
                "generate": {"semantic": 5, "physical": 15},
                "qc": {"semantic": 6, "physical": 18},
                "reference_solve": {"semantic": 6, "physical": 18},
                "reference_review": {"semantic": 6, "physical": 18},
            }
        },
        "frozen": {
            "templates": {"single_choice": template},
            "config": {
                "generation": {"temperature": 0.4, "timeout_seconds": 45},
                "profiles": {"generation": {"model_name": "fixture-model"}},
                "prices": {
                    "fixture-model": {
                        "prompt_per_1k": 1.0,
                        "completion_per_1k": 2.0,
                        "currency": "fixture-unit",
                        "verified": True,
                    }
                },
            },
            "dataset": {
                "sets": {
                    "exploration": [
                        {
                            "case_id": "fixture-case",
                            "template_id": "single_choice",
                            "params": {
                                "knowledge_point": "test only",
                                "difficulty": "中",
                                "grade": 10,
                                "constraints": "self-authored revision",
                                "quantity": 1,
                            },
                        }
                    ]
                }
            },
        },
        "identities": {"templates": "fixture-identity"},
        "execution_plan": [
            {
                "path_id": arm,
                "case_id": "fixture-case",
                "set_name": "exploration",
                "arm": arm,
                "first_draft_id": "shared-draft",
                "state": "planned",
            }
            for arm in ("single_attempt", "blind_retry", "feedback_retry")
        ],
        "provider_calls": [],
        "artifacts": [],
    }


def test_local_configuration_failure_is_not_a_physical_generation_attempt(tmp_path):
    manifest = local_manifest()
    del manifest["frozen"]["config"]["prices"]["fixture-model"]
    client = FakeClient([])
    traces = []
    path = tmp_path / "run.json"
    with pytest.raises(KeyError, match="fixture-model"):
        run_generation(path, manifest, client=client, trace_sink=lambda **row: traces.append(row))
    saved = json.loads(path.read_text("utf-8"))
    assert client.requests == traces == saved["provider_calls"] == []
    assert saved["generation_results"]["single_attempt"]["attempts"] == []
    assert saved["execution_plan"][0]["state"] == "failed"
    assert saved["generation_results"]["single_attempt"]["error_type"] == "KeyError"


def test_local_failure_after_invalid_response_keeps_only_real_attempts(tmp_path):
    manifest = local_manifest()
    manifest["execution_plan"] = [manifest["execution_plan"][1]]

    class PriceRemovedClient(FakeClient):
        def create(self, **kwargs):
            response = super().create(**kwargs)
            del manifest["frozen"]["config"]["prices"]["fixture-model"]
            return response

    client = PriceRemovedClient([{}])
    traces = []
    path = tmp_path / "run.json"
    with pytest.raises(KeyError, match="fixture-model"):
        run_generation(path, manifest, client=client, trace_sink=lambda **row: traces.append(row))
    saved = json.loads(path.read_text("utf-8"))
    assert len(client.requests) == len(traces) == len(saved["provider_calls"]) == 1
    assert traces[0]["success"] is False
    assert saved["generation_results"]["blind_retry"]["attempts"] == [
        {"attempt": 1, "source_call_id": "shared-draft:0", "replayed": False}
    ]
    assert saved["execution_plan"][0]["state"] == "failed"


def test_shared_valid_draft_has_one_physical_trace_and_three_revalidated_paths(tmp_path):
    manifest = local_manifest()
    path = tmp_path / "run.json"
    write_manifest(path, manifest)
    client = FakeClient([VALID])
    traces = []
    run_generation(path, manifest, client=client, trace_sink=lambda **row: traces.append(row))

    saved = json.loads(path.read_text("utf-8"))
    assert len(client.requests) == len(traces) == len(saved["provider_calls"]) == 1
    assert client.options == {"max_retries": 0, "timeout": 45}
    assert client.requests[0]["temperature"] == 0.4
    assert "self-authored revision" in client.requests[0]["messages"][1]["content"]
    assert all(p["state"] == "completed" for p in saved["execution_plan"])
    assert [p["payload"] for p in saved["generation_results"].values()] == [VALID] * 3
    assert all(
        p["attempts"][0]["source_call_id"] == "shared-draft:0"
        for p in saved["generation_results"].values()
    )
    assert saved["provider_calls"][0]["cost"] == 0.2
    assert saved["generation_accounting"]["physical_cost"] == 0.2
    assert all(a["logical_cost"] == 0.2 for a in saved["generation_accounting"]["arms"].values())

    run_generation(path, saved, client=client, trace_sink=lambda **row: traces.append(row))
    assert len(client.requests) == len(traces) == 1


def test_failed_first_draft_is_shared_and_retries_have_distinct_messages(tmp_path):
    manifest = local_manifest()
    path = tmp_path / "run.json"
    client = FakeClient([{}, {}, {}, VALID])
    traces = []
    run_generation(path, manifest, client=client, trace_sink=lambda **row: traces.append(row))
    assert len(client.requests) == len(traces) == 4
    assert [row["success"] for row in traces] == [False, False, False, True]
    assert client.requests[0]["messages"] == client.requests[1]["messages"]
    assert client.requests[1]["messages"] == client.requests[2]["messages"]
    assert len(client.requests[3]["messages"]) > len(client.requests[0]["messages"])
    assert manifest["execution_plan"][0]["state"] == "failed"
    assert manifest["execution_plan"][1]["state"] == "failed"
    assert manifest["execution_plan"][2]["state"] == "completed"
    assert [len(row["attempts"]) for row in manifest["generation_results"].values()] == [1, 3, 2]
    assert manifest["generation_accounting"]["physical_call_count"] == 4


def test_interrupted_send_and_unknown_acceptance_are_never_resent(tmp_path):
    path = tmp_path / "run.json"
    manifest = local_manifest()
    client = FakeClient([TimeoutError("never store provider credentials or raw exception")])
    traces = []
    run_generation(path, manifest, client=client, trace_sink=lambda **row: traces.append(row))
    assert len(client.requests) == len(traces) == 1
    assert manifest["provider_calls"][0]["state"] == "unresolved"
    assert all(p["state"] == "unresolved" for p in manifest["execution_plan"])
    assert all(
        row["attempts"] == [{"attempt": 1, "source_call_id": "shared-draft:0"}]
        for row in manifest["generation_results"].values()
    )
    assert "never store provider credentials" not in path.read_text("utf-8")
    run_generation(path, manifest, client=client, trace_sink=lambda **row: traces.append(row))
    assert len(client.requests) == len(traces) == 1


def test_only_affirmatively_unaccepted_transient_errors_get_two_transport_retries(tmp_path):
    manifest = local_manifest()
    client = FakeClient(
        [
            EvaluationTransportError(accepted=False, completed=False, transient=True),
            EvaluationTransportError(accepted=False, completed=False, transient=True),
            VALID,
        ]
    )
    traces = []
    run_generation(
        tmp_path / "run.json", manifest, client=client, trace_sink=lambda **row: traces.append(row)
    )
    assert len(client.requests) == len(traces) == len(manifest["provider_calls"]) == 3
    assert [r["state"] for r in manifest["provider_calls"]] == [
        "not_accepted",
        "not_accepted",
        "completed",
    ]
    assert all(len(r["attempts"]) == 1 for r in manifest["generation_results"].values())


def test_response_persisted_before_validation_can_resume_without_send(tmp_path):
    manifest = local_manifest()
    path = tmp_path / "run.json"
    traces = []
    client = FakeClient([VALID])
    with pytest.raises(KeyboardInterrupt):

        def interrupted(**row):
            raise KeyboardInterrupt()

        run_generation(path, manifest, client=client, trace_sink=interrupted)
    assert manifest["provider_calls"][0]["state"] == "completed"
    assert manifest["provider_calls"][0]["trace_state"] == "in_flight"
    run_generation(path, manifest, client=client, trace_sink=lambda **row: traces.append(row))
    assert len(client.requests) == 1
    assert traces == []  # The interrupted Trace write may already have committed.
    assert all(p["state"] == "completed" for p in manifest["execution_plan"])
    assert manifest["generation_accounting"]["trace_coverage"]["complete"] is False


def test_pending_response_gets_trace_on_local_validation_and_unknown_cost_stays_unknown(tmp_path):
    manifest = local_manifest()
    path = tmp_path / "run.json"
    traces = []
    ledger = EvaluationLedger(path, manifest, trace_sink=lambda **row: traces.append(row))
    reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])
    first = ledger.call(
        "call", [], stage="qc", model="fixture-model", price={}, invoke=lambda: reply
    )
    replay = ledger.call(
        "call",
        [],
        stage="qc",
        model="fixture-model",
        price={},
        invoke=lambda: pytest.fail("persisted response must not be sent"),
    )
    assert first.source_call_id == replay.source_call_id == "call" and replay.replayed
    ledger.record_validation(replay.source_call_id, success=False)
    ledger.record_validation(replay.source_call_id, success=False)
    assert len(traces) == 1
    assert traces[0]["cost"] is None and traces[0]["prompt_tokens"] is None
    assert traces[0]["usage_reported"] is False
    assert manifest["provider_calls"][0]["cost_status"] == "unknown"


@pytest.mark.parametrize(
    "template_id, field, count", [("cloze", "blanks", 5), ("reading", "questions", 3)]
)
def test_fixed_teaching_cardinality_is_enforced_by_real_validator(
    tmp_path, template_id, field, count
):
    manifest = local_manifest()
    original = yaml.safe_load((BACKEND / f"app/templates/{template_id}.yaml").read_text("utf-8"))
    manifest["frozen"]["templates"] = {template_id: original}
    case = manifest["frozen"]["dataset"]["sets"]["exploration"][0]
    case["template_id"] = template_id
    case["params"]["blank_count" if template_id == "cloze" else "question_count"] = count
    manifest["execution_plan"] = manifest["execution_plan"][:1]
    item = dict(VALID)
    if template_id == "cloze":
        item = {"index": 1, "options": VALID["options"], "answer": "A", "explanation": "test"}
    client = FakeClient([{"passage": "Test-only ____ passage.", field: [item]}])
    run_generation(tmp_path / "run.json", manifest, client=client, trace_sink=lambda **row: None)
    assert manifest["execution_plan"][0]["state"] == "failed"
    assert len(client.requests) == 1
    assert str(count) in client.requests[0]["messages"][1]["content"]
    assert original["output_schema"]["properties"][field].get("maxItems") != count


def test_provider_identity_is_recomputed_before_clients_are_created(monkeypatch):
    from app.engine import real_evaluation
    from app.versioning import hash_value, model_profile_hash

    provider = SimpleNamespace(
        id="provider",
        name="test",
        base_url="https://test.invalid/v1",
        status="enabled",
        key_revision="key-v1",
    )
    provider.config_hash = hash_value(
        {
            "name": provider.name,
            "base_url": provider.base_url,
            "status": provider.status,
            "key_revision": provider.key_revision,
        }
    )
    profile = SimpleNamespace(
        name="test",
        provider="test",
        provider_id="provider",
        provider_record=provider,
        provider_config_hash=provider.config_hash,
        model_name="test-model",
        cost_tier="test",
        is_default=False,
        status="enabled",
        max_fallbacks=0,
        budget_per_task=None,
    )
    profile.model_hash = model_profile_hash(profile)
    frozen = dict(
        name="test",
        provider_id="provider",
        provider_config_hash=provider.config_hash,
        profile_hash=profile.model_hash,
        model_name="test-model",
        model_family="test-family",
    )
    manifest = {"frozen": {"config": {"profiles": {"generation": frozen}}}}
    session = SimpleNamespace(scalars=lambda statement: SimpleNamespace(all=lambda: [profile]))
    created = []
    monkeypatch.setattr(
        real_evaluation, "client_for_profile", lambda p: created.append(p) or "client"
    )
    assert real_evaluation.verified_clients(manifest, session) == {"generation": "client"}
    provider.base_url = "https://different.invalid/v1"  # stored hashes deliberately remain stale
    with pytest.raises(ValueError, match="identity"):
        real_evaluation.verified_clients(manifest, session)
    assert len(created) == 1


@pytest.mark.parametrize(
    "error",
    [
        EvaluationTransportError(accepted=None, completed=False, transient=True),
        EvaluationTransportError(accepted=False, completed=None, transient=True),
        EvaluationTransportError(accepted=True, completed=True, transient=True),
    ],
)
def test_ambiguous_or_accepted_transport_never_retries(tmp_path, error):
    manifest = local_manifest()
    client = FakeClient([error])
    run_generation(tmp_path / "run.json", manifest, client=client, trace_sink=lambda **row: None)
    assert len(client.requests) == 1
    assert all(path["state"] == "unresolved" for path in manifest["execution_plan"])
    assert manifest["generation_accounting"]["physical_cost"] is None


def test_transport_and_semantic_attempts_are_capped_across_resume(tmp_path):
    manifest = local_manifest()
    error = EvaluationTransportError(accepted=False, completed=False, transient=True)
    client = FakeClient([error, error, error])
    path = tmp_path / "run.json"
    run_generation(path, manifest, client=client, trace_sink=lambda **row: None)
    run_generation(
        path, json.loads(path.read_text("utf-8")), client=client, trace_sink=lambda **row: None
    )
    assert len(client.requests) == 3
    assert all(item["state"] == "failed" for item in manifest["execution_plan"])
    manifest = local_manifest()
    client = FakeClient([{}] * 5)
    run_generation(tmp_path / "other.json", manifest, client=client, trace_sink=lambda **row: None)
    assert len(client.requests) == 5  # N + 4F; two retries per retry arm
    assert [len(row["attempts"]) for row in manifest["generation_results"].values()] == [1, 3, 3]


def test_malformed_provider_envelope_is_durable_unresolved(tmp_path):
    manifest = local_manifest()
    ledger = EvaluationLedger(tmp_path / "run.json", manifest, trace_sink=lambda **row: None)
    with pytest.raises(EvaluationUnresolvedError):
        ledger.call(
            "broken",
            [],
            stage="generate",
            model="test",
            price={},
            invoke=lambda: SimpleNamespace(choices=[]),
        )
    assert manifest["provider_calls"][0]["state"] == "unresolved"
    assert manifest["provider_calls"][0]["cost"] is None
    with pytest.raises(EvaluationUnresolvedError):
        ledger.call(
            "broken",
            [],
            stage="generate",
            model="test",
            price={},
            invoke=lambda: pytest.fail("uncertain request replayed"),
        )


def test_non_durable_trace_return_never_reports_complete_coverage(tmp_path):
    manifest = local_manifest()
    client = FakeClient([VALID])
    path = tmp_path / "run.json"
    run_generation(path, manifest, client=client, trace_sink=lambda **row: False)
    saved = json.loads(path.read_text("utf-8"))
    assert saved["provider_calls"][0]["trace_state"] == "failed"
    assert saved["generation_accounting"]["trace_coverage"]["complete"] is False
    run_generation(path, saved, client=client, trace_sink=lambda **row: pytest.fail("re-emitted"))
    assert len(client.requests) == 1


def test_interruption_before_response_preserves_unknown_and_forbids_resend(tmp_path):
    manifest = local_manifest()
    path = tmp_path / "run.json"

    def interrupted():
        raise KeyboardInterrupt

    ledger = EvaluationLedger(path, manifest, trace_sink=lambda **row: None)
    with pytest.raises(KeyboardInterrupt):
        ledger.call("interrupted", [], stage="qc", model="test", price={}, invoke=interrupted)
    saved = json.loads(path.read_text("utf-8"))
    assert saved["provider_calls"][0]["state"] == "in_flight"
    assert saved["provider_calls"][0]["cost_status"] == "unknown"
    ledger = EvaluationLedger(path, saved, trace_sink=lambda **row: None)
    with pytest.raises(EvaluationUnresolvedError):
        ledger.call(
            "interrupted",
            [],
            stage="qc",
            model="test",
            price={},
            invoke=lambda: pytest.fail("interrupted send replayed"),
        )
