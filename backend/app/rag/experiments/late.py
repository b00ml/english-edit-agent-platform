"""True transformer-before-pooling prototype; no final-vector API emulation."""

from __future__ import annotations

import hashlib
import importlib
import math
import time
import uuid
from pathlib import Path
from typing import Any

from app.engine.trace import record_trace
from app.versioning import hash_value


def pool_tokens(
    text: str, artifact: dict[str, Any], ranges: list[list[tuple[int, int]]], max_tokens: int = 8192
) -> list[list[float]]:
    if (
        artifact.get("text_hash") != hash_value(text)
        or artifact.get("kind") != "contextual_token_hidden_states"
    ):
        raise ValueError("Late token artifact identity/type mismatch")
    offsets = artifact.get("offsets", [])
    states = artifact.get("states", [])
    mask = artifact.get("attention_mask", [])
    if (
        not states
        or len(states) != len(offsets)
        or len(mask) != len(states)
        or len(states) > max_tokens
    ):
        raise ValueError("Late artifact tokens incomplete/over cap")
    dim = len(states[0])
    previous = 0
    covered = bytearray(len(text))
    for bounds, values, active in zip(offsets, states, mask):
        if (
            len(bounds) != 2
            or len(values) != dim
            or not dim
            or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)
        ):
            raise ValueError("Invalid hidden states/offset shape")
        a, b = bounds
        if (
            not isinstance(a, int)
            or not isinstance(b, int)
            or not 0 <= a <= b <= len(text)
            or active not in {0, 1}
        ):
            raise ValueError("Invalid token coordinate/mask")
        if active and a != b:
            if a < previous:
                raise ValueError("Nonmonotonic token offsets")
            covered[a:b] = b"\x01" * (b - a)
            previous = b
    if any(not flag and not ch.isspace() for flag, ch in zip(covered, text)):
        raise ValueError("Token artifact is truncated/missing source characters")
    result = []
    for spans in ranges:
        if not spans or any(not 0 <= a < b <= len(text) for a, b in spans):
            raise ValueError("Invalid pooling source range")
        indexes = [
            i
            for i, ((a, b), active) in enumerate(zip(offsets, mask))
            if active and a < b and any(a < hi and b > lo for lo, hi in spans)
        ]
        if not indexes:
            raise ValueError("No usable token for chunk")
        vector = [sum(float(states[i][j]) for i in indexes) / len(indexes) for j in range(dim)]
        norm = math.sqrt(sum(x * x for x in vector))
        if not norm or not math.isfinite(norm):
            raise ValueError("Zero pooled late vector")
        result.append([x / norm for x in vector])
    return result


class LocalTokenEncoder:
    """Only explicitly supplied local model weights, no downloads/custom remote code."""

    def __init__(
        self,
        directory: Path,
        max_tokens: int = 8192,
        trace_id: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        directory = directory.resolve()
        if not directory.is_dir() or not (directory / "config.json").exists():
            raise ValueError("Explicit local transformer model directory required")
        manifest = {}
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix in {".json", ".txt", ".safetensors", ".model"}:
                resolved = path.resolve()
                if not resolved.is_relative_to(directory):
                    raise ValueError("Model symlink escapes explicit local directory")
                digest = hashlib.sha256()
                with resolved.open("rb") as source:
                    while piece := source.read(1024 * 1024):
                        digest.update(piece)
                manifest[str(path.relative_to(directory))] = digest.hexdigest()
        self.trace_id = trace_id or "rag-late-local:" + uuid.uuid4().hex
        self.tenant_id = tenant_id
        # Optional packages live in an explicit experiment environment, not the core API.
        transformers = importlib.import_module("transformers")
        self.torch = importlib.import_module("torch")
        self.tokenizer = transformers.AutoTokenizer.from_pretrained(
            str(directory), local_files_only=True, trust_remote_code=False, use_fast=True
        )
        if not self.tokenizer.is_fast:
            raise ValueError("Late chunking requires precise fast-tokenizer offsets")
        self.model = transformers.AutoModel.from_pretrained(
            str(directory), local_files_only=True, trust_remote_code=False, use_safetensors=True
        ).eval()
        self.maximum = min(
            max_tokens,
            int(getattr(self.model.config, "max_position_embeddings", max_tokens)),
            int(getattr(self.tokenizer, "model_max_length", max_tokens)),
        )
        self.model_identity: dict[str, Any] = {
            "model_and_tokenizer_manifest_hash": hash_value(manifest),
            "file_manifest": manifest,
            "config_hash": hash_value((directory / "config.json").read_text(encoding="utf-8")),
            "path": str(directory),
            "license_and_embedding_suitability": (
                "verify model card; generic hidden states " "are not quality-certified embeddings"
            ),
        }

    def encode(self, text: str) -> dict[str, Any]:
        started = time.perf_counter()
        error = None
        artifact: dict[str, Any] = {}
        try:
            artifact = self._encode(text)
            return artifact
        except Exception as exc:  # noqa: BLE001 - record local forward failure then propagate
            error = type(exc).__name__
            raise
        finally:
            record_trace(
                trace_id=self.trace_id,
                tenant_id=self.tenant_id,
                model=Path(self.model_identity["path"]).name,
                stage="rag_late_local",
                latency_ms=(time.perf_counter() - started) * 1000,
                cost=0.0,
                prompt_version=self.model_identity["model_and_tokenizer_manifest_hash"],
                input_data={"text_hash": hash_value(text), "characters": len(text)},
                output_data={
                    "error_type": error,
                    "token_count": len(artifact.get("offsets", [])),
                    "local_compute_unpriced": True,
                    "provider_bill_verified": False,
                },
                success=error is None,
                usage_reported=False,
            )

    def _encode(self, text: str) -> dict[str, Any]:
        encoded = self.tokenizer(
            text, return_tensors="pt", return_offsets_mapping=True, truncation=False
        )
        offsets = encoded.pop("offset_mapping")[0].tolist()
        if len(offsets) > self.maximum:
            raise ValueError(
                "Late input exceeds model context; no silent truncation/window emulation"
            )
        with self.torch.inference_mode():
            result = self.model(**encoded)
        states = getattr(result, "last_hidden_state", None)
        if states is None:
            raise ValueError("Model does not expose contextual token hidden states")
        return {
            "kind": "contextual_token_hidden_states",
            "text_hash": hash_value(text),
            "offsets": offsets,
            "attention_mask": encoded.get(
                "attention_mask", self.torch.ones_like(encoded["input_ids"])
            )[0].tolist(),
            "states": states[0].float().cpu().tolist(),
            "model": self.model_identity,
        }
