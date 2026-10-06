"""Shared durable filesystem spool. Caller-owned relative keys, immutable attempt artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from app.config import settings
from app.errors import DocumentParseError


class OcrStorage:
    def __init__(self) -> None:
        self.root = Path(settings.RAG_OCR_STORAGE_DIR).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if not key or Path(key).is_absolute() or not candidate.is_relative_to(self.root):
            raise DocumentParseError("OCR 文件键超出存储范围")
        return candidate

    def save_input(self, job_id: str, stream: BinaryIO) -> tuple[str, str, int]:
        uuid.UUID(job_id)
        key = f"jobs/{job_id}/source.pdf"
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(uuid.uuid4().hex + ".tmp")
        digest, size = hashlib.sha256(), 0
        try:
            with temp.open("xb") as output:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.RAG_OCR_MAX_INPUT_BYTES:
                        raise DocumentParseError("OCR 文件超过输入大小上限")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if not size:
                raise DocumentParseError("OCR 输入文件为空")
            os.replace(temp, target)
        finally:
            # Remove only this operation's generated temporary file, never an input/archive tree.
            if temp.exists():
                temp.unlink()
        return key, digest.hexdigest(), size

    def write_json(self, key: str, value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        if len(encoded) > settings.RAG_OCR_MAX_PREVIEW_BYTES:
            raise DocumentParseError("OCR 预览超过输出大小上限")
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(uuid.uuid4().hex + ".tmp")
        try:
            with temp.open("xb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink()
        return hashlib.sha256(encoded).hexdigest()

    def read_json(self, key: str, expected_hash: str | None = None) -> Any:
        file = self.path(key)
        if file.stat().st_size > settings.RAG_OCR_MAX_PREVIEW_BYTES:
            raise DocumentParseError("OCR 结果超过输出大小上限")
        raw = file.read_bytes()
        if expected_hash is not None and hashlib.sha256(raw).hexdigest() != expected_hash:
            raise DocumentParseError("OCR 结果校验失败，不能使用损坏的检查点")
        return json.loads(raw)

    def input_bytes(self, key: str, expected_hash: str) -> bytes:
        with self.path(key).open("rb") as source:
            value = source.read(settings.RAG_OCR_MAX_INPUT_BYTES + 1)
        if (
            len(value) > settings.RAG_OCR_MAX_INPUT_BYTES
            or hashlib.sha256(value).hexdigest() != expected_hash
        ):
            raise DocumentParseError("OCR 输入快照大小/hash不一致")
        return value

    def cache_key(self, tenant: str | None, source_hash: str, page: int, version: str) -> str:
        # Tenant null != tenant string "null"; no cross-tenant course material reuse.
        scope = hashlib.sha256(json.dumps(tenant).encode()).hexdigest()
        code_hash = hashlib.sha256()
        for relative in ("adapter.py", "pipeline.py", "../tables.py"):
            code_hash.update((Path(__file__).parent / relative).read_bytes())
        profile = {
            "adapter_code_hash": code_hash.hexdigest(),
            "source_hash": source_hash,
            "page": page,
            "version": version,
            "revision": settings.RAG_OCR_CACHE_REVISION,
            "url": settings.RAG_OCR_URL,
            "min_text": settings.RAG_PDF_MIN_TEXT_CHARS,
            "image_ratio": settings.RAG_PDF_SCAN_IMAGE_RATIO,
        }
        digest = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
        return f"cache/{scope}/{digest}.json"
