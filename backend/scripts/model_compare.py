"""Plan-only by default. Explicit --execute runs configured candidates and records real usage."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import SessionLocal  # noqa: E402 - project root bootstrap
from app.engine.model_comparison import (  # noqa: E402
    ComparisonCase,
    compare_models,
    comparison_plan,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-a", required=True)
    parser.add_argument("--profile-b", required=True)
    parser.add_argument("--judge-profile")
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--execute", action="store_true", help="明确允许实际embedding/chat调用；请使用受限输出路径"
    )
    args = parser.parse_args()
    with SessionLocal() as session:
        if args.execute:
            if not args.cases:
                parser.error("--execute需要--cases")
            values = json.loads(args.cases.read_text(encoding="utf-8"))
            report = compare_models(
                session,
                [ComparisonCase.model_validate(x) for x in values],
                args.profile_a,
                args.profile_b,
                judge=args.judge_profile,
            )
        else:
            report = comparison_plan(session, args.profile_a, args.profile_b, args.judge_profile)
            report["executed"] = False
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
