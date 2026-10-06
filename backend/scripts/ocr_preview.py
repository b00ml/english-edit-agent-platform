"""Explicit local OCR -> blocks -> child/parent preview. No DB writes or embeddings.

Run from backend using the project Python, not the heavyweight OCR interpreter.
Configure RAG_OCR_ENGINE=mineru and RAG_OCR_URL pointing at a local basic V1 server.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Match the project's standalone maintenance scripts, without requiring package installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.errors import DocumentParseError, InvalidKnowledgeError  # noqa: E402
from app.rag.ocr.pipeline import parse_pdf_with_ocr  # noqa: E402
from app.rag.preview import preview_document  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--pages", help="Comma-separated, one-based ordered physical PDF pages")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.input.suffix.lower() != ".pdf":
        parser.error("--input must be a PDF")
    if args.input.resolve() == args.output.resolve():
        parser.error("Output must not overwrite the source PDF")
    try:
        pages = [int(value) for value in args.pages.split(",")] if args.pages else None
        with args.input.open("rb") as source:
            content = source.read(settings.RAG_OCR_MAX_INPUT_BYTES + 1)
        document = parse_pdf_with_ocr(args.input.name, content, page_numbers=pages)
        result = preview_document(document)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except (DocumentParseError, InvalidKnowledgeError, OSError, ValueError) as exc:
        print(f"OCR preview failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "selected_pages": document["stats"]["selected_pages"],
                "chunks": len(result["chunks"]),
                "indexable": result["indexable"],
                "warnings": len(document["warnings"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
