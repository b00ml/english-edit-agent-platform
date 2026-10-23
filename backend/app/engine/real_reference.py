"""Two fresh blind reference calls with durable physical accounting and model provenance."""

from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.engine.evaluation_protocol import canonical_hash
from app.engine.real_evaluation import EvaluationLedger
from app.engine.reference_review import ReferenceRubric
from app.engine.trace import record_trace
from app.errors import EvaluationTransportError, EvaluationUnresolvedError
from app.prompt_loader import load_prompt, render
from app.skill_registry import get_skill_by_id

RUBRIC_VERSION = "blind-reference-v1"
# Frozen manifests, template-specific content and SDK envelopes are dynamic schema boundaries.


class SolveAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    index: int = Field(ge=1)
    answer: Literal["A", "B", "C", "D"] | None
    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Independent solve reason must contain evidence")
        return value


class IndependentSolve(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    answers: list[SolveAnswer] = Field(min_length=1)


class QuestionReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    index: int = Field(ge=1)
    rubric: ReferenceRubric


class ContentReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    questions: list[QuestionReview] = Field(min_length=1)


def _solve_packet(template_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Structural allowlist: generated answers, explanations and arbitrary metadata cannot enter."""
    if template_id == "single_choice":
        return {"questions": [{"index": 1, "stem": payload["stem"], "options": payload["options"]}]}
    if template_id == "cloze":
        return {
            "passage": payload["passage"],
            "questions": [
                {"index": i, "options": item["options"]}
                for i, item in enumerate(payload["blanks"], 1)
            ],
        }
    if template_id == "reading":
        return {
            "passage": payload["passage"],
            "questions": [
                {"index": i, "stem": item["stem"], "options": item["options"]}
                for i, item in enumerate(payload["questions"], 1)
            ],
        }
    raise ValueError("Reference review requires a supported frozen template")


def _verdict(rubric: dict[str, Any]) -> bool | None:
    verdicts = [dimension["verdict"] for dimension in rubric.values()]
    return False if "fail" in verdicts else None if "unresolved" in verdicts else True


def _phase(
    ledger: EvaluationLedger,
    result: dict[str, Any],
    phase: str,
    packet: dict[str, Any],
    count: int,
    config: dict[str, Any],
    transport: Any,
    repairs: int,
    solve: IndependentSolve | None = None,
    answers: list[str] | None = None,
) -> IndependentSolve | ContentReview | None:
    model = config["profiles"]["reference"]["model_name"]
    schema = IndependentSolve if phase == "solve" else ContentReview
    messages = [
        {
            "role": "system",
            "content": render(
                load_prompt(f"reference-{phase}-system.st"),
                {
                    "schema": json.dumps(schema.model_json_schema(), ensure_ascii=False),
                    "skill": get_skill_by_id("reference_review"),
                },
            ),
        },
        {
            "role": "user",
            "content": render(
                load_prompt(f"reference-{phase}-user.st"),
                {
                    "input_data": json.dumps(packet, ensure_ascii=False),
                },
            ),
        },
    ]
    for attempt in range(repairs + 1):
        call_id = canonical_hash([ledger.manifest["run_id"], result["content_id"], phase, attempt])
        result["source_call_ids"].append(call_id)
        ledger.save()
        response = ledger.call(
            call_id,
            messages,
            stage=f"reference_{phase}",
            model=model,
            price=config["prices"][model],
            metadata={
                "rubric_version": RUBRIC_VERSION,
                "settings": config["generation"],
                "model_profile": config["profiles"]["reference"],
            },
            invoke=lambda: transport.chat.completions.create(
                model=model,
                messages=messages,
                temperature=config["generation"]["temperature"],
                response_format={"type": "json_object"},
            ),
        )
        result["source_call_ids"][-1] = response.source_call_id
        error = None
        try:
            parsed = schema.model_validate_json(response.text)
            items = parsed.answers if isinstance(parsed, IndependentSolve) else parsed.questions
            if [item.index for item in items] != list(range(1, count + 1)):
                raise ValueError("Return every expected question exactly once in index order")
            if isinstance(parsed, ContentReview) and solve is not None and answers is not None:
                for item, solved, generated in zip(parsed.questions, solve.answers, answers):
                    verdict = item.rubric.answer_correctness.verdict
                    if verdict == "pass" and solved.answer != generated:
                        raise ValueError("Passing answer correctness contradicts independent solve")
        except (ValidationError, ValueError) as exc:
            error = str(exc)
        ledger.record_validation(
            response.source_call_id,
            success=error is None,
            prompt_version=RUBRIC_VERSION,
            input_data={"messages": messages, "model_profile": config["profiles"]["reference"]},
            output_data={"raw": response.text, "validation_error": error},
        )
        if error is None:
            return parsed
        result.setdefault("format_errors", []).append({"phase": phase, "error": error})
        messages += [
            {"role": "assistant", "content": response.text},
            {
                "role": "user",
                "content": render(
                    load_prompt("reference-format-repair-user.st"),
                    {
                        "validation_error": error,
                    },
                ),
            },
        ]
    result.update(state="failed", error=f"{phase} format repairs exhausted")
    return None


def _account(manifest: dict[str, Any]) -> None:
    results = manifest["reference_results"]
    calls = {
        r["call_id"]: r for r in manifest["provider_calls"] if r["stage"].startswith("reference_")
    }
    paired = []
    for planned in manifest["execution_plan"]:
        row = results[planned["path_id"]]
        judge = manifest.get("judge_results", {}).get(planned["path_id"], {})
        # Link only after blind model calls; reference completion never requires a Judge label.
        row["judge_state"] = judge.get("state", "missing")
        row["judge_pass"] = judge.get("passed") if judge.get("state") == "completed" else None
        row["judge_source_call_ids"] = judge.get("source_call_ids", [])
        if row.get("reference_pass") is not None and row["judge_pass"] is not None:
            paired.append(row)
    n = len(paired)
    agreement = sum(r["reference_pass"] is r["judge_pass"] for r in paired)
    expected = None
    if n:
        ref_yes = sum(r["reference_pass"] is True for r in paired) / n
        judge_yes = sum(r["judge_pass"] is True for r in paired) / n
        expected = ref_yes * judge_yes + (1 - ref_yes) * (1 - judge_yes)
    manifest["reference_accounting"] = {
        "state_counts": dict(Counter(row["state"] for row in results.values())),
        "full_plan_denominator": len(manifest["execution_plan"]),
        "physical_call_count": len(calls),
        "physical_cost": (
            sum(r["cost"] for r in calls.values())
            if all(r.get("cost") is not None for r in calls.values())
            else None
        ),
        "trace_coverage": {
            "complete": all(r["trace_state"] == "completed" for r in calls.values()),
            "unresolved_call_ids": sorted(
                k for k, r in calls.items() if r["trace_state"] != "completed"
            ),
        },
        "pairwise": {
            "denominator": n,
            "agreement_count": agreement,
            "agreement_rate": agreement / n if n else None,
            "kappa": (
                (agreement / n - expected) / (1 - expected)
                if expected is not None and expected < 1
                else None
            ),
            "interpretation": "Model agreement only; not human accuracy",
        },
    }


def run_reference_review(
    path: Path,
    manifest: dict[str, Any],
    *,
    client: Any,
    trace_sink: Callable[..., bool | None] = record_trace,
) -> dict[str, Any]:
    """Independently solve then review each unique final content; preserve every planned path."""
    ledger = EvaluationLedger(path, manifest, trace_sink=trace_sink)
    frozen = manifest["frozen"]
    config = frozen["config"]
    transport = client.with_options(max_retries=0, timeout=config["generation"]["timeout_seconds"])
    repairs = manifest["protocol"]["reference_max_format_repairs"]
    if not 0 <= repairs <= 2:
        raise ValueError("Reference format repairs must be within the frozen maximum of two")
    results = manifest.setdefault("reference_results", {})
    for planned in manifest["execution_plan"]:
        path_id = planned["path_id"]
        if results.get(path_id, {}).get("state") in {
            "completed",
            "failed",
            "unresolved",
            "cancelled",
        }:
            continue
        generation = manifest.get("generation_results", {}).get(path_id, {})
        if planned["state"] != "completed" or not isinstance(generation.get("payload"), dict):
            results[path_id] = {
                "state": "cancelled" if planned["state"] == "cancelled" else "missing",
                "generation_state": planned["state"],
                "source_call_ids": [],
            }
            continue
        case = next(
            row
            for row in frozen["dataset"]["sets"][planned["set_name"]]
            if row["case_id"] == planned["case_id"]
        )
        payload = generation["payload"]
        content_id = canonical_hash({"case": case, "payload": payload})
        prior = next(
            (
                r
                for r in results.values()
                if r.get("content_id") == content_id and r["state"] != "in_flight"
            ),
            None,
        )
        if prior is not None:
            results[path_id] = copy.deepcopy(prior)
            continue
        result: dict[str, Any] = {
            "state": "in_flight",
            "content_id": content_id,
            "rubric_version": RUBRIC_VERSION,
            "model_profile": copy.deepcopy(config["profiles"]["reference"]),
            "settings": copy.deepcopy(config["generation"]),
            "source_call_ids": [],
        }
        results[path_id] = result
        packet = _solve_packet(case["template_id"], payload)
        count = {"single_choice": 1, "cloze": 5, "reading": 3}[case["template_id"]]
        try:
            solve = _phase(ledger, result, "solve", packet, count, config, transport, repairs)
            if not isinstance(solve, IndependentSolve):
                continue
            result["solve"] = solve.model_dump()
            items = (
                [payload]
                if case["template_id"] == "single_choice"
                else payload["blanks" if case["template_id"] == "cloze" else "questions"]
            )
            review_packet = {
                "content": payload,
                "independent_solve": solve.model_dump(),
                "constraints": {
                    k: case["params"][k]
                    for k in (
                        "grade",
                        "knowledge_point",
                        "difficulty",
                        "quantity",
                        "constraints",
                        "blank_count",
                        "question_count",
                        "topic",
                    )
                    if k in case["params"]
                },
            }
            review = _phase(
                ledger,
                result,
                "review",
                review_packet,
                count,
                config,
                transport,
                repairs,
                solve,
                [item["answer"] for item in items],
            )
            if not isinstance(review, ContentReview):
                continue
            questions = review.model_dump()["questions"]
            for item in questions:
                item["reference_pass"] = _verdict(item["rubric"])
            verdicts = [q["reference_pass"] for q in questions]
            passed = False if False in verdicts else None if None in verdicts else True
            result.update(
                state="unresolved" if passed is None else "completed",
                reference_pass=passed,
                questions=questions,
            )
        except EvaluationTransportError as exc:
            result.update(state="failed", error=str(exc))
        except EvaluationUnresolvedError as exc:
            result.update(state="unresolved", error=str(exc))
        finally:
            ledger.save()
    _account(manifest)
    ledger.save()
    return manifest
