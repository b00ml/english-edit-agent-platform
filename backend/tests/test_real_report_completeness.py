"""Evidence reports preserve generation denominators and physical/logical accounting."""

import copy

import pytest
from test_real_generation import VALID, FakeClient, local_manifest

from app.engine.real_evaluation import run_generation
from app.engine.real_experiment import build_evidence_report
from app.errors import EvaluationTransportError


def test_report_measures_failed_shared_draft_recovery_and_unique_errors(tmp_path):
    manifest = local_manifest()
    run_generation(
        tmp_path / "run.json",
        manifest,
        client=FakeClient([{}, {}, {}, VALID]),
        trace_sink=lambda **_: None,
    )
    report = build_evidence_report(manifest)
    metrics = report["generation_metrics"]
    assert metrics["first_draft"] == {
        "passed": 0,
        "failed": 3,
        "unknown": 0,
        "denominator": 3,
        "pass_rate": 0.0,
    }
    assert metrics["recovery"] == {"recovered": 1, "denominator": 3, "rate": 1 / 3}
    assert metrics["final_valid"] == {"count": 1, "denominator": 3, "rate": 1 / 3}
    assert metrics["extra_requests"] == {"physical_sends": 3, "semantic_requests": 3}
    assert report["arms"]["feedback_retry"]["generation_metrics"]["recovery"]["rate"] == 1
    assert report["arms"]["single_attempt"]["generation_metrics"]["recovery"]["rate"] == 0
    errors = report["generation_errors"]["schema"]
    assert errors["frequency"] == 3
    assert errors["affected_inputs"] == ["fixture-case"]
    assert errors["affected_paths"] == ["blind_retry", "feedback_retry", "single_attempt"]
    assert errors["reproducibility"] == errors["causal_attribution"] == "unknown"
    assert report["templates"]["single_choice"]["generation_errors"]["schema"] == errors


def test_tokens_distinguish_actual_reuse_missing_usage_and_known_subtotals(tmp_path):
    manifest = local_manifest()
    run_generation(
        tmp_path / "run.json", manifest, client=FakeClient([VALID]), trace_sink=lambda **_: None
    )
    report = build_evidence_report(manifest)
    assert report["physical"]["tokens"]["total_tokens"] == {
        "total": 150,
        "known_subtotal": 150,
        "reported_calls": 1,
        "denominator": 1,
    }
    assert report["templates"]["single_choice"]["logical"]["tokens"]["total_tokens"]["total"] == 450
    assert report["arms"]["blind_retry"]["logical"]["tokens"]["prompt_tokens"]["total"] == 100
    assert report["generation_metrics"]["recovery"] == {
        "recovered": 0,
        "denominator": 0,
        "rate": None,
    }
    unknown = copy.deepcopy(manifest["provider_calls"][0])
    unknown.update(call_id="interrupted", request_id="interrupted", usage=None)
    manifest["provider_calls"].append(unknown)
    tokens = build_evidence_report(manifest)["physical"]["tokens"]
    assert tokens["total_tokens"] == {
        "total": None,
        "known_subtotal": 150,
        "reported_calls": 1,
        "denominator": 2,
    }
    assert tokens["completion_tokens"]["known_subtotal"] == 50


@pytest.mark.parametrize(
    ("category", "payload"),
    [
        ("json_syntax", "malformed"),
        ("schema", {}),
        ("content_rule", {**VALID, "options": ["one", "ONE", "two", "three"]}),
    ],
)
def test_validation_classes_are_durable_and_shared_failures_are_not_inflated(
    tmp_path, category, payload
):
    class RawClient(FakeClient):
        def create(self, **kwargs):
            response = super().create(**kwargs)
            if payload == "malformed":
                response.choices[0].message.content = "{"
            return response

    manifest = local_manifest()
    manifest["execution_plan"] = [manifest["execution_plan"][0]]
    run_generation(
        tmp_path / "run.json", manifest, client=RawClient([payload]), trace_sink=lambda **_: None
    )
    output = manifest["provider_calls"][0]["validations"][0]["output"]
    assert output["error_summary"]
    assert output["validation_category"] == category
    report = build_evidence_report(manifest)
    assert set(report["generation_errors"]) == {category}
    assert report["generation_errors"][category]["frequency"] == 1


def test_no_validation_is_unknown_and_transport_sends_are_not_semantic_retries(tmp_path):
    manifest = local_manifest()
    run_generation(
        tmp_path / "run.json",
        manifest,
        client=FakeClient(
            [EvaluationTransportError(accepted=False, completed=False, transient=True), VALID]
        ),
        trace_sink=lambda **_: None,
    )
    report = build_evidence_report(manifest)
    assert report["generation_metrics"]["first_draft"]["passed"] == 3
    assert report["generation_metrics"]["extra_requests"] == {
        "physical_sends": 1,
        "semantic_requests": 0,
    }
    assert report["generation_errors"]["call_error"]["frequency"] == 1
    logical = report["arms"]["blind_retry"]["logical"]
    assert logical["call_count"] == 2
    assert logical["tokens"]["total_tokens"] == {
        "total": None,
        "known_subtotal": 150,
        "reported_calls": 1,
        "denominator": 2,
    }
    completed = manifest["provider_calls"][-1]
    completed["validations"] = []
    metrics = build_evidence_report(manifest)["generation_metrics"]
    assert metrics["first_draft"]["unknown"] == 3
    assert metrics["first_draft"]["failed"] == 0
    assert metrics["recovery"]["rate"] is None


def test_partial_usage_and_conflicting_validation_preserve_unknown_denominators(tmp_path):
    manifest = local_manifest()
    run_generation(
        tmp_path / "run.json", manifest, client=FakeClient([VALID]), trace_sink=lambda **_: None
    )
    call = manifest["provider_calls"][0]
    call["usage"].pop("completion_tokens")
    call["validations"].append({"success": False, "output": {"error_summary": "legacy"}})
    report = build_evidence_report(manifest)
    assert report["physical"]["tokens"]["prompt_tokens"]["total"] == 100
    assert report["physical"]["tokens"]["completion_tokens"]["total"] is None
    assert report["physical"]["tokens"]["total_tokens"]["total"] is None
    assert report["generation_metrics"]["first_draft"]["unknown"] == 3
    assert report["generation_errors"]["unknown_validation"]["frequency"] == 1


def test_missing_attempt_identity_does_not_invent_first_draft_or_extra_request_counts(tmp_path):
    manifest = local_manifest()
    run_generation(
        tmp_path / "run.json", manifest, client=FakeClient([VALID]), trace_sink=lambda **_: None
    )
    for result in manifest["generation_results"].values():
        del result["attempts"][0]["attempt"]
    metrics = build_evidence_report(manifest)["generation_metrics"]
    assert metrics["first_draft"] == {
        "passed": 0,
        "failed": 0,
        "unknown": 3,
        "denominator": 3,
        "pass_rate": 0.0,
    }
    assert metrics["extra_requests"] == {"physical_sends": None, "semantic_requests": None}
