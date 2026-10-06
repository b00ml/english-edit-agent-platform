"""Replay sanitized durable Trace files into the database (no model invocation)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine.observability import TraceLogSink  # noqa: E402
from app.engine.trace_recovery import replay_spool  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.limit <= 10000:
        parser.error("limit must be 1..10000")
    result = replay_spool(TraceLogSink().emit, args.limit)
    sys.stdout.write(json.dumps(result) + "\n")
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
