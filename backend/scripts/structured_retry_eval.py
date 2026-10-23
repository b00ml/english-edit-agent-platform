"""Freeze, validate, or explicitly execute structured-output evaluation inputs."""

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
from app.engine.evaluation_protocol import (  # noqa: E402
    freeze_protocol,
    resume_manifest,
    write_manifest,
)
from app.engine.real_evaluation import run_generation, verified_clients  # noqa: E402
from app.engine.reference_review import (  # noqa: E402
    ReferenceAssessment,
    build_review_packets,
    review_packet_id,
    summarize_reference_reviews,
)


def canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _payload(template_id: str) -> dict:
    options = ["walked", "walks", "walking", "will walk"]
    if template_id == "single_choice":
        return {
            "stem": "Yesterday, she ___ to school.",
            "options": options,
            "answer": "A",
            "explanation": "Past simple.",
        }
    choice = {"index": 1, "answer": "A", "options": options, "explanation": "Past simple."}
    if template_id == "cloze":
        return {"passage": "Yesterday, she ____ to school.", "blanks": [choice]}
    return {
        "passage": "Yesterday, she walked to school.",
        "questions": [
            {
                "stem": "How did she travel?",
                "options": options,
                "answer": "A",
                "explanation": "The passage says she walked.",
            }
        ],
    }


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
        generated_payload = None

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
            generated_payload = structured_output.generate_structured(
                template,
                case["params"],
                model_profile=None,
                trace_id=f"offline-eval:{case['case_id']}:{max_retry}:{feedback}",
                response_adapter=adapter,
                retry_feedback=feedback,
                trace_sink=lambda **row: None,
            )
            success = True
            failure = None
        except Exception as exc:  # noqa: BLE001 - retain failed cases in the report denominator
            success = False
            failure = type(exc).__name__
        case_latency = (time.perf_counter() - start) * 1000
        total_latency += case_latency
        if first_attempt_pass:
            initial_passes += 1
        if not first_attempt_pass:
            initial_failures += 1
        outcomes.append(
            {
                "case_id": case["case_id"],
                "first_attempt_schema_pass": first_attempt_pass,
                "attempts": case_adapter_calls,
                "latency_ms": round(case_latency, 3),
                "final_schema_success": success,
                "payload": generated_payload if success else None,
                "failure_type": failure,
            }
        )

    succeeded = sum(item["final_schema_success"] for item in outcomes)
    initial_failed = initial_failures
    recovered = sum(
        item["final_schema_success"] and not item["first_attempt_schema_pass"] for item in outcomes
    )
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
            "recovery_among_initial_failures": (
                recovered / initial_failed if initial_failed else None
            ),
            "final_schema_success": succeeded / len(cases) if cases else None,
        },
        "attempts": {
            "adapter_calls": adapter_calls,
            "mean_per_case": adapter_calls / len(cases) if cases else None,
        },
        "latency_ms": {
            "total": round(total_latency, 3),
            "mean_per_case": round(total_latency / len(cases), 3) if cases else None,
        },
        "cost": {
            "complete": False,
            "total": None,
            "reason": "offline adapter has no provider usage or billing data",
        },
        "outcomes": outcomes,
    }


def _interview_brief(evaluation: dict, summary: dict) -> str:
    case_count = evaluation["case_count"]
    distribution = ", ".join(
        f"{count} {template}" for template, count in sorted(evaluation["cases_by_template"].items())
    )
    coverage = summary["assessment_coverage"]
    reference_passes = summary["confirmed_reference_pass_rate"]
    comparison = summary["model_judge_comparison"]
    lines = [
        "# Exploratory evaluation interview brief",
        "",
        f"Sample: {case_count} fixed inputs ({distribution}); exploratory sample only.",
        "Method: paired single attempt, equal-budget blind and feedback retries on the same "
        "inputs. A deterministic synthetic adapter scripts failures and recovery; reference "
        "packets hide the arm and Judge result and deduplicate identical content per input.",
        "",
        "Observed harness results (scripted mechanics, not empirical model quality):",
        "",
    ]
    for arm_name, arm in evaluation["arms"].items():
        counts = arm["counts"]
        lines.append(
            f"- {arm_name}: schema-valid {counts['final_schema_success']}/"
            f"{arm['denominators']['planned_cases']}; recovered {counts['recovered']}/"
            f"{counts['recovery_denominator']} initial failures; "
            f"failed {counts['failed']}, missing {counts['missing']}, "
            f"cancelled {counts['cancelled']}, unresolved {counts['unresolved']}; "
            f"adapter calls {arm['attempts']['adapter_calls']}."
        )
    lines.extend(
        [
            "",
            f"Reference review: assessments {coverage['numerator']}/{coverage['denominator']}; "
            f"pending {coverage['denominator'] - coverage['numerator']}/{coverage['denominator']}; "
            f"unresolved {len(summary['unresolved_cases'])}/{coverage['numerator']} assessments; "
            f"confirmed reference passes {reference_passes['numerator']}/"
            f"{reference_passes['denominator']}; model/Judge agreement "
            f"{comparison['agreement_count']}/{comparison['denominator']}. "
            "Zero denominators mean unavailable. These are exploratory model-reference "
            "comparisons, not human accuracy or proof of teaching correctness.",
            "",
            "Cost: unknown; no provider billing observed. Local adapter latency cannot establish "
            "provider P95, throughput or MTTR. There is no measured production improvement "
            "or quality gain in these synthetic results.",
            "",
            "Delivery boundary: this artifact demonstrates the evaluation harness, schema "
            "recovery accounting and reference-review import. Owner-confirmed personal "
            "contribution: pending. " + summary["contribution_boundary"],
            "",
            f"Evidence: code revision {evaluation['provenance']['code_revision']}; "
            f"dataset hash {evaluation['provenance']['dataset_hash']}.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--cases", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    review = subparsers.add_parser("review")
    review.add_argument("--evaluation", type=Path, required=True)
    review.add_argument("--assessments", type=Path, required=True)
    review.add_argument("--output", type=Path, required=True)
    freeze = subparsers.add_parser(
        "freeze", help="Freeze explicitly supplied local evaluation inputs"
    )
    freeze.add_argument("--dataset", type=Path, required=True)
    freeze.add_argument("--config", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    freeze.add_argument("--run-id", required=True)
    freeze.add_argument("--seed", type=int, required=True)
    resume = subparsers.add_parser("resume", help="Validate frozen inputs without replaying calls")
    resume.add_argument("--manifest", type=Path, required=True)
    resume.add_argument("--dataset", type=Path, required=True)
    resume.add_argument("--config", type=Path, required=True)
    execute = subparsers.add_parser(
        "execute-generation",
        help="Explicitly execute frozen generation; may incur provider charges",
    )
    execute.add_argument("--manifest", type=Path, required=True)
    execute.add_argument("--dataset", type=Path, required=True)
    execute.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()

    if args.command in {"freeze", "resume", "execute-generation"}:
        try:
            if args.command == "freeze":
                manifest = freeze_protocol(
                    dataset_path=args.dataset,
                    config_path=args.config,
                    backend=BACKEND,
                    run_id=args.run_id,
                    seed=args.seed,
                )
                write_manifest(args.output, manifest)
            else:
                manifest = resume_manifest(
                    manifest_path=args.manifest,
                    dataset_path=args.dataset,
                    config_path=args.config,
                    backend=BACKEND,
                )
                if args.command == "execute-generation":
                    from app.database import SessionLocal

                    with SessionLocal() as session:
                        clients = verified_clients(manifest, session)
                    run_generation(args.manifest, manifest, client=clients["generation"])
                    print(json.dumps(manifest["generation_accounting"]))
                    return 0
                print(
                    json.dumps(
                        {
                            "run_id": manifest["run_id"],
                            "compatible": True,
                            "provider_calls_replayed": 0,
                        }
                    )
                )
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        return 0

    if args.command == "review":
        evaluation = json.loads(args.evaluation.read_text(encoding="utf-8"))
        packets = evaluation.get("reference_review_packets", [])
        packet_by_id = {packet["review_id"]: packet for packet in packets}
        packet_ids = set(packet_by_id)
        if len(packet_ids) != len(packets):
            parser.error("evaluation report contains duplicate reference review IDs")
        assessments = [
            json.loads(line)
            for line in args.assessments.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        try:
            validated = [ReferenceAssessment.model_validate(item) for item in assessments]
            summary = summarize_reference_reviews(assessments)
        except (ValueError, TypeError) as exc:
            parser.error(f"invalid reference assessment: {exc}")
        assessment_ids = [item.review_id for item in validated]
        if len(set(assessment_ids)) != len(assessment_ids):
            parser.error("assessment review_id values must be unique")
        if not set(assessment_ids).issubset(packet_ids):
            parser.error("assessment contains review_id absent from the evaluation packets")
        if any(
            item.rubric_version != packet_by_id[item.review_id]["rubric_version"]
            for item in validated
        ):
            parser.error("assessment rubric_version does not match its blinded review packet")
        by_id = {item.review_id: item.model_dump(mode="json") for item in validated}
        arm_index: dict[str, set[str]] = {}
        for arm_name, arm in evaluation.get("arms", {}).items():
            arm_index[arm_name] = {
                review_id
                for outcome in arm.get("outcomes", [])
                if outcome.get("final_schema_success") and isinstance(outcome.get("payload"), dict)
                for review_id in [review_packet_id(str(outcome["case_id"]), outcome["payload"])]
            }
        summary["packet_count"] = len(packet_ids)
        summary["assessment_coverage"] = {
            "numerator": len(assessment_ids),
            "denominator": len(packet_ids),
            "rate": round(len(assessment_ids) / len(packet_ids), 4) if packet_ids else None,
        }
        summary["by_arm"] = {}
        for arm_name, review_ids in arm_index.items():
            arm_assessments = [by_id[review_id] for review_id in sorted(review_ids & by_id.keys())]
            arm_summary = summarize_reference_reviews(arm_assessments)
            arm_summary["eligible_packet_count"] = len(review_ids)
            arm_summary["assessment_coverage"] = {
                "numerator": len(arm_assessments),
                "denominator": len(review_ids),
                "rate": round(len(arm_assessments) / len(review_ids), 4) if review_ids else None,
            }
            summary["by_arm"][arm_name] = arm_summary
        summary["bad_cases"] = [
            {
                "review_id": item["review_id"],
                "status": item["status"],
                "reason": item.get("disagreement_reason") or item.get("unresolved_reason"),
                "rubric_findings": {
                    dimension: result
                    for dimension, result in item["rubric"].items()
                    if result["verdict"] != "pass"
                },
            }
            for item in by_id.values()
            if item["status"] == "unresolved"
            or item.get("reference_pass") != item.get("judge_pass")
        ]
        summary["contribution_boundary"] = (
            "This evaluation records system behavior only; it does not establish individual "
            "contribution. Add owner-confirmed contribution facts before interview use."
        )
        brief = _interview_brief(evaluation, summary)
        result = {**evaluation, "reference_review": summary, "interview_brief": brief}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        args.output.with_suffix(".md").write_text(brief, encoding="utf-8")
        return 0

    frozen = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = frozen["cases"]
    templates = {
        path.stem: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted((BACKEND / "app" / "templates").glob("*.yaml"))
    }
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
    skill_files = sorted(
        path
        for path in (BACKEND / "skills").rglob("*")
        if path.is_file() and path.name in {"SKILL.md", "skill.meta.yml"}
    )
    prompt_hashes = {
        str(path.relative_to(BACKEND)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in prompt_files + skill_files
    }
    report = {
        "case_count": len(cases),
        "cases_by_template": counts,
        "provider_calls": 0,
        "arms": arms,
        "reference_review_packets": build_review_packets(arms),
        "provenance": {
            "code_revision": revision,
            "run_time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "case_hashes": [canonical_hash(case) for case in cases],
            "dataset_hash": canonical_hash(frozen),
            "model_config": "deterministic synthetic adapter; no provider/model invoked",
            "template_config_hashes": {
                key: canonical_hash(value) for key, value in templates.items()
            },
            "runtime": {"python": sys.version.split()[0]},
            "prompt_config_hashes": prompt_hashes,
        },
        "limitations": [
            "Synthetic adapter tests retry and schema-validation mechanics only; "
            "it is not a model quality benchmark.",
            "The adapter scripts 15 first-attempt failures; blind retries repeat them, "
            "while feedback retries recover them. These rates are scripted, not empirical.",
            "Latency is local validation/adapter wall time and cannot be compared "
            "with provider end-to-end latency.",
            "Cost is unknown and excluded; unknown cost is never treated as zero.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
