"""Run the frozen structured-output retry cases with a deterministic offline adapter."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import yaml

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.engine import structured_output  # noqa: E402


def canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _payload(template_id: str) -> dict:
    options = ["walked", "walks", "walking", "will walk"]
    if template_id == "single_choice":
        return {"stem": "Yesterday, she ___ to school.", "options": options, "answer": "A", "explanation": "Past simple."}
    choice = {"index": 1, "answer": "A", "options": options, "explanation": "Past simple."}
    if template_id == "cloze":
        return {"passage": "Yesterday, she ____ to school.", "blanks": [choice]}
    return {"passage": "Yesterday, she walked to school.", "questions": [{"stem": "How did she travel?", "options": options, "answer": "A", "explanation": "The passage says she walked."}]}


def _run_arm(cases: list[dict], templates: dict, max_retry: int, feedback: bool) -> dict:
    outcomes = []
    adapter_calls = 0
    total_latency = 0.0
    initial_passes = 0
    initial_failures = 0
    for case in cases:
        template = SimpleNamespace(**templates[case["template_id"]])
        template.run_config = {**template.run_config, "max_retry": max_retry}

        case_adapter_calls = 0
        first_attempt_pass = False

        def adapter(messages, attempt):
            nonlocal adapter_calls
            nonlocal case_adapter_calls
            nonlocal first_attempt_pass
            adapter_calls += 1
            case_adapter_calls += 1
            start = time.perf_counter()
            initially_bad = int(case["case_id"].rsplit("-", 1)[1]) % 2 == 1
            has_feedback = any(
                message["role"] == "user" and "你上一次输出的 JSON 未通过" in message["content"]
                for message in messages[2:]
            )
            should_fail = initially_bad and (attempt == 1 or not has_feedback)
            if attempt == 1:
                first_attempt_pass = not should_fail
            response = "{not-json" if should_fail else json.dumps(_payload(case["template_id"]))
            elapsed = (time.perf_counter() - start) * 1000
            return response, elapsed, None

        start = time.perf_counter()
        try:
            structured_output.generate_structured(
                template,
                case["params"],
                model_profile=None,
                trace_id=f"offline-eval:{case['case_id']}:{max_retry}:{feedback}",
                response_adapter=adapter,
                retry_feedback=feedback,
            )
            success = True
            failure = None
        except Exception as exc:  # evaluation records a failed case rather than dropping it
            success = False
            failure = type(exc).__name__
        case_latency = (time.perf_counter() - start) * 1000
        total_latency += case_latency
        if first_attempt_pass:
            initial_passes += 1
        if not first_attempt_pass:
            initial_failures += 1
        outcomes.append({
            "case_id": case["case_id"],
            "first_attempt_schema_pass": first_attempt_pass,
            "attempts": case_adapter_calls,
            "latency_ms": round(case_latency, 3),
            "final_schema_success": success,
            "failure_type": failure,
        })

    succeeded = sum(item["final_schema_success"] for item in outcomes)
    initial_failed = initial_failures
    recovered = sum(item["final_schema_success"] and not item["first_attempt_schema_pass"] for item in outcomes)
    return {
        "denominators": {"planned_cases": len(cases), "completed_cases": len(outcomes)},
        "counts": {
            "first_attempt_schema_pass": initial_passes,
            "first_attempt_schema_pass_denominator": len(cases),
            "initially_failed": initial_failed,
            "recovered": recovered,
            "recovery_denominator": initial_failed,
            "final_schema_success": succeeded,
            "missing": 0,
            "failed": len(cases) - succeeded,
            "cancelled": 0,
            "unresolved": 0,
        },
        "rates": {
            "first_attempt_schema_pass": initial_passes / len(cases) if cases else None,
            "recovery_among_initial_failures": recovered / initial_failed if initial_failed else None,
            "final_schema_success": succeeded / len(cases) if cases else None,
        },
        "attempts": {"adapter_calls": adapter_calls, "mean_per_case": adapter_calls / len(cases) if cases else None},
        "latency_ms": {"total": round(total_latency, 3), "mean_per_case": round(total_latency / len(cases), 3) if cases else None},
        "cost": {"complete": False, "total": None, "reason": "offline adapter has no provider usage or billing data"},
        "outcomes": outcomes,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--cases", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    frozen = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = frozen["cases"]
    templates = {
        path.stem: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted((BACKEND / "app" / "templates").glob("*.yaml"))
    }
    structured_output.record_trace = lambda **kwargs: None
    arms = {
        "single_attempt": _run_arm(cases, templates, 1, False),
        "blind_retry": _run_arm(cases, templates, 3, False),
        "feedback_retry": _run_arm(cases, templates, 3, True),
    }
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=BACKEND, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        revision = "unknown"
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["template_id"]] = counts.get(case["template_id"], 0) + 1
    prompt_files = sorted((BACKEND / "prompts").glob("*.st"))
    skill_files = sorted((BACKEND / "skills").glob("*"))
    prompt_hashes = {
        str(path.relative_to(BACKEND)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in prompt_files + [path for path in skill_files if path.is_file()]
    }
    report = {
        "case_count": len(cases),
        "cases_by_template": counts,
        "provider_calls": 0,
        "arms": arms,
        "provenance": {
            "code_revision": revision,
            "run_time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "case_hashes": [canonical_hash(case) for case in cases],
            "dataset_hash": canonical_hash(frozen),
            "model_config": "deterministic synthetic adapter; no provider/model invoked",
            "template_config_hashes": {key: canonical_hash(value) for key, value in templates.items()},
            "runtime": {"python": sys.version.split()[0]},
            "prompt_config_hashes": prompt_hashes,
        },
        "limitations": [
            "Synthetic adapter tests retry and schema-validation mechanics only; it is not a model quality benchmark.",
            "The adapter scripts 15 first-attempt failures; blind retries repeat them, while feedback retries recover them. These rates are scripted, not empirical.",
            "Latency is local validation/adapter wall time and cannot be compared with provider end-to-end latency.",
            "Cost is unknown and excluded; unknown cost is never treated as zero.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
