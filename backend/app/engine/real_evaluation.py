"""Durable evaluation calls and shared-draft structured generation.

Provider acceptance is conservative: an interrupted send is never automatically retried.
Only the external transport may affirm that a transient request was not accepted.
"""

from __future__ import annotations

import copy
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from openai import OpenAI
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine.evaluation_protocol import canonical_hash, write_manifest
from app.engine.providers import client_for_profile
from app.engine.structured_output import StructuredResponse, generate_structured
from app.engine.trace import record_trace
from app.errors import EvaluationTransportError, EvaluationUnresolvedError, StructuredOutputError
from app.models import ModelProfile
from app.prompt_loader import teaching_constraints, with_frozen_assets
from app.versioning import hash_value, model_profile_hash

EvaluationResponse = StructuredResponse


def frozen_teaching_context(case: dict[str, Any]) -> dict[str, Any]:
    """Derive every role's teaching requirements from the same frozen case."""
    return teaching_constraints(
        {
            **case["params"],
            **{
                key: case[key]
                for key in ("teaching_goal", "scope", "judgment_basis")
                if key in case
            },
        }
    )


def verified_clients(manifest: dict[str, Any], session: Session) -> dict[str, OpenAI]:
    """Verify actual bound identities before constructing any evaluation client."""
    frozen = manifest["frozen"]["config"]["profiles"]
    names = {item["name"] for item in frozen.values()}
    profiles = session.scalars(
        select(ModelProfile).where(ModelProfile.name.in_(names), ModelProfile.tenant_id.is_(None))
    ).all()
    by_name = {profile.name: profile for profile in profiles}
    if len(by_name) != len(profiles) or set(by_name) != names:
        raise ValueError("Frozen profile identity is unavailable or ambiguous")
    for expected in frozen.values():
        profile = by_name[expected["name"]]
        provider = profile.provider_record
        if provider is None:
            raise ValueError("Frozen profile identity requires a bound provider")
        provider_hash = hash_value(
            {
                "name": provider.name,
                "base_url": provider.base_url,
                "status": provider.status,
                "key_revision": provider.key_revision,
            }
        )
        if not (
            profile.status == provider.status == "enabled"
            and profile.provider_id == provider.id == expected["provider_id"]
            and provider_hash
            == provider.config_hash
            == profile.provider_config_hash
            == expected["provider_config_hash"]
            and profile.model_name == expected["model_name"]
            and model_profile_hash(profile) == profile.model_hash == expected["profile_hash"]
        ):
            raise ValueError("Frozen runtime identity drift; create a new run")
    return {role: client_for_profile(by_name[item["name"]]) for role, item in frozen.items()}


def evaluation_template(
    manifest: dict[str, Any], case: dict[str, Any], max_attempts: int
) -> SimpleNamespace:
    """Copy the frozen template, enforcing the evaluation's exact teaching cardinality."""
    data = copy.deepcopy(manifest["frozen"]["templates"][case["template_id"]])
    data["run_config"] = {**data.get("run_config", {}), "max_retry": max_attempts}
    field = {"cloze": "blanks", "reading": "questions"}.get(case["template_id"])
    if field:
        count = case["params"]["blank_count" if field == "blanks" else "question_count"]
        data["output_schema"]["properties"][field].update(minItems=count, maxItems=count)
    return SimpleNamespace(**data)


def _usage(value: Any) -> dict[str, int] | None:
    counts: dict[str, int] = {}
    for name in ("prompt_tokens", "completion_tokens"):
        count = getattr(value, name, None)
        if type(count) is not int or count < 0:
            return None
        counts[name] = count
    return counts


def _cost(usage: dict[str, Any] | None, price: dict[str, Any]) -> dict[str, Any]:
    if (
        usage is None
        or price.get("verified") is not True
        or not all(
            isinstance(price.get(name), (int, float)) and price[name] >= 0
            for name in ("prompt_per_1k", "completion_per_1k")
        )
    ):
        return {
            "cost": None,
            "prompt_cost": None,
            "completion_cost": None,
            "cost_status": "unknown",
            "currency": price.get("currency"),
        }
    prompt = usage["prompt_tokens"] * price["prompt_per_1k"] / 1000
    completion = usage["completion_tokens"] * price["completion_per_1k"] / 1000
    return {
        "cost": prompt + completion,
        "prompt_cost": prompt,
        "completion_cost": completion,
        "cost_status": "complete",
        "currency": price.get("currency"),
    }


class EvaluationLedger:
    """One local durable ledger for generation, Judge and independent reference calls."""

    def __init__(
        self,
        path: Path,
        manifest: dict[str, Any],
        *,
        trace_sink: Callable[..., bool | None] = record_trace,
        persist: Callable[[], None] | None = None,
    ) -> None:
        self.path = path
        self.manifest = manifest
        self.trace_sink = trace_sink
        self.persist = persist

    def save(self) -> None:
        if self.persist is not None:
            self.persist()
        else:
            write_manifest(self.path, self.manifest, overwrite=True)

    def call(
        self,
        call_id: str,
        messages: list[dict[str, str]],
        *,
        stage: str,
        model: str,
        invoke: Callable[[], Any],
        price: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> EvaluationResponse:
        """Persist before send and after response; completed responses are reused locally."""
        identity = canonical_hash(
            {
                "messages": messages,
                "stage": stage,
                "model": model,
                "price": price,
                "metadata": metadata,
            }
        )
        records = self.manifest.setdefault("provider_calls", [])
        for transport_attempt in range(3):
            physical_id = (
                call_id if transport_attempt == 0 else f"{call_id}:transport-{transport_attempt}"
            )
            existing = next((row for row in records if row["call_id"] == physical_id), None)
            if existing is not None:
                if existing["request_hash"] != identity:
                    raise ValueError("Call identity drift; use a new run")
                if existing["state"] == "not_accepted":
                    if existing["retry_allowed"]:
                        continue
                    raise EvaluationTransportError(accepted=False, completed=False, transient=False)
                if existing["state"] != "completed":
                    existing["state"] = "unresolved"
                    self.save()
                    raise EvaluationUnresolvedError("Existing provider request cannot be resent")
                return EvaluationResponse(
                    existing["response_text"],
                    existing["latency_ms"],
                    SimpleNamespace(**existing["usage"]) if existing["usage"] else None,
                    physical_id,
                    True,
                )
            limits = self.manifest["protocol"]["call_limits"][stage]
            stage_calls = [item for item in records if item["stage"] == stage]
            if len(stage_calls) >= limits["physical"]:
                raise ValueError(f"{stage} physical call limit reached")
            semantic_ids = {item["request_id"] for item in stage_calls}
            if call_id not in semantic_ids and len(semantic_ids) >= limits["semantic"]:
                raise ValueError(f"{stage} semantic call limit reached")
            row: dict[str, Any] = {
                "call_id": physical_id,
                "request_id": call_id,
                "trace_id": physical_id,
                "stage": stage,
                "model": model,
                "transport_attempt": transport_attempt + 1,
                "messages": copy.deepcopy(messages),
                "metadata": copy.deepcopy(metadata),
                "request_hash": identity,
                "state": "in_flight",
                "trace_state": "pending",
                "usage": None,
                "response_text": None,
                "latency_ms": None,
                **_cost(None, price),
            }
            records.append(row)
            self.save()
            start = time.perf_counter_ns()
            try:
                response = invoke()
                text = response.choices[0].message.content
                if not isinstance(text, str):
                    raise ValueError("Provider response has no text content")
                usage = _usage(getattr(response, "usage", None))
            except (
                Exception
            ) as exc:  # noqa: BLE001 - persist unknown acceptance, never infer safe resend
                safe = (
                    isinstance(exc, EvaluationTransportError)
                    and exc.accepted is False
                    and exc.completed is False
                )
                row.update(
                    state="not_accepted" if safe else "unresolved",
                    usage=None,
                    response_text=None,
                    latency_ms=(time.perf_counter_ns() - start) / 1e6,
                    error_type=type(exc).__name__,
                    retry_allowed=safe
                    and isinstance(exc, EvaluationTransportError)
                    and exc.transient
                    and transport_attempt < 2,
                )
                row.update(_cost(None, price))
                if safe:
                    row.update(
                        cost=0.0, prompt_cost=0.0, completion_cost=0.0, cost_status="complete"
                    )
                self.save()
                self.record_validation(
                    physical_id, success=False, output_data={"error_type": type(exc).__name__}
                )
                if row["retry_allowed"]:
                    continue
                if safe:
                    raise EvaluationTransportError(
                        accepted=False, completed=False, transient=False
                    ) from None
                raise EvaluationUnresolvedError(
                    "Provider acceptance or billing is unknown"
                ) from None
            row.update(
                state="completed",
                response_text=text,
                latency_ms=(time.perf_counter_ns() - start) / 1e6,
                usage=usage,
            )
            row.update(_cost(row["usage"], price))
            self.save()
            return EvaluationResponse(
                row["response_text"],
                row["latency_ms"],
                getattr(response, "usage", None),
                physical_id,
            )
        raise EvaluationTransportError(accepted=False, completed=False, transient=False)

    def source_call_ids(self, request_id: str) -> list[str]:
        """Return only physical rows recorded for this logical request, in send order."""
        return [
            row["call_id"]
            for row in self.manifest["provider_calls"]
            if row["request_id"] == request_id
        ]

    def record_validation(self, source_call_id: str, **trace: Any) -> None:
        """Record validation locally, and emit each physical request's Trace at most once."""
        row = next(r for r in self.manifest["provider_calls"] if r["call_id"] == source_call_id)
        row.setdefault("validations", []).append(
            {"success": trace.get("success"), "output": trace.get("output_data")}
        )
        if row["trace_state"] != "pending":
            self.save()
            return
        row["trace_state"] = "in_flight"
        self.save()
        usage = row["usage"] or {}
        trace.update(
            trace_id=row["trace_id"],
            model=row["model"],
            stage=row["stage"],
            latency_ms=row["latency_ms"],
            cost=row["cost"],
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            usage_reported=row["usage"] is not None,
            prompt_cost=row["prompt_cost"],
            completion_cost=row["completion_cost"],
        )
        trace.setdefault("input_data", {"messages": row["messages"]})
        metadata = row["metadata"] or {}
        settings = metadata.get("settings", {})
        request_kwargs = {
            "temperature": settings.get("temperature"),
            "max_tokens": settings.get("max_output_tokens"),
            "response_format": {"type": "json_object"},
        }
        trace["snapshot_data"] = {
            **trace.get("snapshot_data", {}),
            "messages": copy.deepcopy(row["messages"]),
            "response_text": row["response_text"],
            "model": row["model"],
            **request_kwargs,
            "request_kwargs": request_kwargs,
            "model_profile": metadata.get("model_profile"),
            "settings": settings,
        }
        durable = self.trace_sink(**trace)
        row["trace_state"] = "failed" if durable is False else "completed"
        self.save()


def _account(manifest: dict[str, Any]) -> None:
    calls = {row["call_id"]: row for row in manifest["provider_calls"]}

    def total(ids: set[str]) -> float | None:
        return (
            sum(calls[key]["cost"] for key in ids)
            if all(calls[key].get("cost") is not None for key in ids)
            else None
        )

    arms: dict[str, Any] = {}
    for path in manifest["execution_plan"]:
        result = manifest["generation_results"].get(path["path_id"], {})
        entry = arms.setdefault(path["arm"], {"source_call_ids": []})
        entry["source_call_ids"].extend(a["source_call_id"] for a in result.get("attempts", []))
    for entry in arms.values():
        requested = set(entry["source_call_ids"])
        requested.update(calls[key]["request_id"] for key in list(requested) if key in calls)
        ids = {
            key
            for key, row in calls.items()
            if key in requested or row.get("request_id") in requested
        }
        entry.update(logical_cost=total(ids), logical_call_count=len(ids))
    generation_ids = {key for key, row in calls.items() if row["stage"] == "generate"}
    manifest["generation_accounting"] = {
        "physical_cost": total(generation_ids),
        "physical_call_count": len(generation_ids),
        "arms": arms,
        "trace_coverage": {
            "complete": all(calls[key]["trace_state"] == "completed" for key in generation_ids),
            "unresolved_call_ids": sorted(
                key for key in generation_ids if calls[key]["trace_state"] != "completed"
            ),
        },
    }


@with_frozen_assets
def run_generation(
    path: Path,
    manifest: dict[str, Any],
    *,
    client: Any,
    trace_sink: Callable[..., bool | None] = record_trace,
    path_ids: set[str] | None = None,
    persist: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Run frozen paths using the production validator; physical first drafts are shared."""
    ledger = EvaluationLedger(path, manifest, trace_sink=trace_sink, persist=persist)
    config = manifest["frozen"]["config"]
    model = config["profiles"]["generation"]["model_name"]
    generation = config["generation"]
    transport = client.with_options(max_retries=0, timeout=generation["timeout_seconds"])
    results = manifest.setdefault("generation_results", {})
    for planned in manifest["execution_plan"]:
        if path_ids is not None and planned["path_id"] not in path_ids:
            continue
        if planned["state"] in {"completed", "failed", "unresolved", "cancelled"}:
            continue
        case = next(
            c
            for c in manifest["frozen"]["dataset"]["sets"][planned["set_name"]]
            if c["case_id"] == planned["case_id"]
        )
        template = evaluation_template(
            manifest, case, 1 if planned["arm"] == "single_attempt" else 3
        )
        result: dict[str, Any] = {"attempts": []}
        results[planned["path_id"]] = result
        current_id = ""

        def adapter(messages: list[dict[str, str]], attempt: int) -> EvaluationResponse:
            nonlocal current_id
            request_id = (
                f'{planned["first_draft_id"]}:0'
                if attempt == 1
                else f'{planned["path_id"]}:{attempt - 1}'
            )
            try:
                response = ledger.call(
                    request_id,
                    messages,
                    stage="generate",
                    model=model,
                    price=config["prices"][model],
                    metadata={
                        "settings": generation,
                        "model_profile": config["profiles"]["generation"],
                    },
                    invoke=lambda: transport.chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=generation["temperature"],
                        max_tokens=generation["max_output_tokens"],
                        response_format={"type": "json_object"},
                    ),
                )
            finally:
                sources = ledger.source_call_ids(request_id)
                if sources:
                    result["attempts"].append({"attempt": attempt, "source_call_id": sources[-1]})
                ledger.save()
            current_id = response.source_call_id
            result["attempts"][-1].update(replayed=response.replayed)
            ledger.save()
            return response

        def recorder(**trace: Any) -> None:
            ledger.record_validation(current_id, **trace)

        planned["state"] = "in_flight"
        ledger.save()
        try:
            result["payload"] = generate_structured(
                template,
                frozen_teaching_context(case),
                SimpleNamespace(model_name=model),
                trace_id=planned["path_id"],
                template_id=case["template_id"],
                response_adapter=adapter,
                retry_feedback=planned["arm"] == "feedback_retry",
                trace_sink=recorder,
            )
            planned["state"] = "completed"
        except (StructuredOutputError, EvaluationTransportError) as exc:
            planned["state"] = "failed"
            result["error"] = str(exc)
        except EvaluationUnresolvedError as exc:
            planned["state"] = "unresolved"
            result["error"] = str(exc)
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            planned["state"] = "failed"
            result.update(error=str(exc), error_type=type(exc).__name__)
            ledger.save()
            raise
        _account(manifest)
        ledger.save()
    return manifest
