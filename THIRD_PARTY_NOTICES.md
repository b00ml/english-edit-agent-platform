# Third-party notices

## WeKnora RAG design adaptations

Copyright (C) 2025 Tencent. All rights reserved.

The RAG document parser, table/context handling, adaptive chunking fallback and
validation design in `backend/app/rag/` are adapted from the local WeKnora 0.8.0
source snapshot. Algorithms and boundary tests were rewritten for this project's
Python/FastAPI/SQLAlchemy architecture; no Go services, third-party OCR engines,
or third-party model weights were copied.

The upstream MIT notice is retained in `licenses/WeKnora-MIT.txt`. Individual
third-party dependencies retain their own licenses, not automatically this MIT
license. Per-file source fingerprints and adaptation scope are recorded in
`docs/现状文档/RAG P0移植来源与许可证.md`.

P1 DEF additionally adapts parent/child source lineage, ID-based RRF fusion,
optional/required JSON reranking and bounded query-enhancement designs. This is
not a wholesale or behavior-equivalent copy of the upstream Go pipeline.
See the P1 appendix in the source fingerprint notice above.
