"""Local MinerU 4.x V1 client. No cloud fallback, SDK, model weights or paid calls."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time
from typing import Any, Callable, Literal
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.config import settings
from app.errors import DocumentParseError


class OcrParseError(DocumentParseError):
    """Stage-specific, redacted failure; partial jobs never become successful documents."""

    def __init__(self, stage: str, message: str, *, job_id: str | None = None) -> None:
        self.stage, self.job_id = stage, job_id
        super().__init__(f"OCR {stage}: {message}")


class Element(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str
    content: str | list[Element] = ""
    index: int | None = Field(default=None, ge=0)
    bbox: list[float] | None = None
    level: int | None = Field(default=None, ge=1, le=6)

    @field_validator("bbox")
    @classmethod
    def valid_bbox(cls, value: list[float] | None) -> list[float] | None:
        if value is not None and (
            len(value) != 4
            or any(not 0 <= item <= 1 for item in value)
            or value[0] > value[2]
            or value[1] > value[3]
        ):
            raise ValueError("MinerU 2.0 bbox must be normalized page coordinates")
        return value


class NativePage(BaseModel):
    model_config = ConfigDict(extra="allow")
    page_idx: int = Field(ge=0)
    blocks: list[Element]


class Producer(BaseModel):
    name: Literal["mineru"]
    version: str = Field(pattern=r"^4\.\d+\.\d+(?:[+.-].*)?$")


class Metadata(BaseModel):
    model_config = ConfigDict(extra="allow")
    producer: Producer


class NativeDocument(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    schema_name: Literal["docvortex.middle"] = Field(alias="schema")
    schema_version: Literal["2.0"]
    metadata: Metadata
    pages: list[NativePage] = Field(min_length=1, max_length=1)
    is_full_document: bool = True

    @field_validator("pages")
    @classmethod
    def one_page(cls, value: list[NativePage]) -> list[NativePage]:
        if value[0].page_idx != 0:
            raise ValueError("A one-page submission must return page_idx=0")
        indexes = [block.index for block in value[0].blocks if block.index is not None]
        if len(indexes) != len(set(indexes)):
            raise ValueError("Duplicate native block indices")
        if len(value[0].blocks) > 10000:
            raise ValueError("Too many OCR blocks")
        return value


def validate_native(value: Any) -> NativeDocument:
    try:
        result = NativeDocument.model_validate(value)
    except ValidationError as exc:
        raise OcrParseError(
            "schema", "不兼容的 MinerU 结构化结果（需要 4.x / middle 2.0）"
        ) from exc
    if not result.is_full_document:
        raise OcrParseError("schema", "单页结果不完整")
    return result


class NativeWindowDocument(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    schema_name: Literal["docvortex.middle"] = Field(alias="schema")
    schema_version: Literal["2.0"]
    metadata: Metadata
    pages: list[NativePage] = Field(min_length=2, max_length=3)
    is_full_document: bool = True

    @field_validator("pages")
    @classmethod
    def ordered(cls, pages: list[NativePage]) -> list[NativePage]:
        if [page.page_idx for page in pages] != list(range(len(pages))):
            raise ValueError("Window must return every consecutive local page")
        if any(len(page.blocks) > 10000 for page in pages):
            raise ValueError("Window page block limit exceeded")
        for page in pages:
            indexes = [block.index for block in page.blocks if block.index is not None]
            if len(indexes) != len(set(indexes)):
                raise ValueError("Duplicate window block indices")
        return pages


def validate_window(value: Any, expected_pages: int) -> NativeWindowDocument:
    try:
        result = NativeWindowDocument.model_validate(value)
    except ValidationError as exc:
        raise OcrParseError("schema", "多页结果不完整/页映射不兼容") from exc
    if not result.is_full_document or len(result.pages) != expected_pages:
        raise OcrParseError("schema", "多页结果页数/完整性不匹配")
    return result


def local_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        parsed.port  # validate malformed ports before making any request
    except ValueError as exc:
        raise OcrParseError("config", "OCR 端点格式无效") from exc
    host = parsed.hostname or ""
    local = host in {"localhost", "host.docker.internal"}
    try:
        address = ipaddress.ip_address(host)
        local = local or address.is_loopback or address.is_private
    except ValueError:
        pass
    if (
        not local
        or parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise OcrParseError("config", "OCR 仅允许显式配置本机/私网端点，不使用云服务或 URL 凭据")
    return value.rstrip("/")


class MinerUClient:
    """Bounded page or explicit 2/3-page window; local V1 protocol, same-origin resources."""

    def __init__(
        self,
        *,
        url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        if url is None and settings.RAG_OCR_ENGINE != "mineru":
            raise OcrParseError("config", "本地 OCR 未开启；请配置 RAG_OCR_ENGINE=mineru")
        self.url = local_url(url if url is not None else settings.RAG_OCR_URL)
        self.timeout = timeout if timeout is not None else settings.RAG_OCR_PAGE_TIMEOUT
        self.transport = transport
        self.cancel_check = cancel_check

    def server_version(self) -> str:
        headers = (
            {"Authorization": f"Bearer {settings.RAG_OCR_API_KEY}"}
            if settings.RAG_OCR_API_KEY
            else {}
        )
        try:
            with httpx.Client(
                transport=self.transport, trust_env=False, follow_redirects=False
            ) as client:
                response = client.get(self.url + "/v1/health", headers=headers, timeout=10)
                if response.status_code != 200 or len(response.content) > 65536:
                    raise OcrParseError("health", "OCR 健康检查失败")
                version = Producer(name="mineru", version=response.json()["version"]).version
                return version
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise OcrParseError("health", "OCR 健康检查不可用或版本不兼容") from exc

    def parse_page(self, content: bytes, filename: str = "page.pdf") -> NativeDocument:
        return validate_native(self._parse(content, filename, 1))

    def parse_window(
        self, content: bytes, expected_pages: int, filename: str = "window.pdf"
    ) -> NativeWindowDocument:
        if expected_pages not in {2, 3}:
            raise OcrParseError("input", "复核窗口必须为连续2或3页")
        return validate_window(self._parse(content, filename, expected_pages), expected_pages)

    def _parse(self, content: bytes, filename: str, expected_pages: int) -> Any:
        if not content or len(content) > expected_pages * 10 * 1024 * 1024:
            raise OcrParseError("input", "复核输入为空或超过逐页大小上限")
        deadline = time.monotonic() + self.timeout
        headers = (
            {"Authorization": f"Bearer {settings.RAG_OCR_API_KEY}"}
            if settings.RAG_OCR_API_KEY
            else {}
        )
        job_id = None
        cancel_required = False
        stage = "health"
        with httpx.Client(
            transport=self.transport, headers=headers, trust_env=False, follow_redirects=False
        ) as client:

            def request(method: str, path: str, **kwargs: Any) -> Any:
                nonlocal cancel_required
                if self.cancel_check is not None and self.cancel_check():
                    cancel_required = True
                    raise OcrParseError("cancelled", "后台任务已取消", job_id=job_id)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OcrParseError(stage, "单页处理超过总时限", job_id=job_id)
                target = urljoin(self.url + "/", path)
                if urlsplit(target)[:2] != urlsplit(self.url)[:2]:
                    raise OcrParseError(stage, "上游返回跨源资源地址，已拒绝", job_id=job_id)
                with client.stream(method, target, timeout=remaining, **kwargs) as response:
                    if not 200 <= response.status_code < 300:
                        raise OcrParseError(
                            stage, f"本地服务 HTTP {response.status_code}", job_id=job_id
                        )
                    body = bytearray()
                    for piece in response.iter_bytes():
                        if time.monotonic() >= deadline:
                            raise OcrParseError(stage, "读取超过单页总时限", job_id=job_id)
                        body.extend(piece)
                        if len(body) > settings.RAG_OCR_MAX_RESULT_BYTES:
                            raise OcrParseError(stage, "本地服务响应超过上限", job_id=job_id)
                if not body:
                    return {}
                loaded = json.loads(body)
                if not isinstance(loaded, dict):
                    raise OcrParseError(stage, "本地服务响应不是 JSON 对象", job_id=job_id)
                return loaded

            def identifier(value: Any) -> str:
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
                    raise OcrParseError(stage, "本地服务资源 ID 无效", job_id=job_id)
                return value

            try:
                request("GET", self.url + "/v1/health")
                stage = "upload"
                upload = request(
                    "POST",
                    self.url + "/v1/uploads",
                    json={
                        "filename": filename,
                        "bytes": len(content),
                        "mime_type": "application/pdf",
                        "purpose": "parse",
                        "sha256sum": hashlib.sha256(content).hexdigest(),
                    },
                )
                if upload.get("status") != "completed":
                    upload_id = identifier(upload.get("id"))
                    upload_url = upload.get("upload_url")
                    if not isinstance(upload_url, str):
                        raise OcrParseError(stage, "缺少上传地址")
                    request(
                        "PUT",
                        upload_url,
                        content=content,
                        headers={"Content-Type": "application/pdf"},
                    )
                    upload = request("POST", self.url + f"/v1/uploads/{upload_id}/complete")
                file_id = identifier(upload["file"]["id"])
                stage = "submit"
                job = request(
                    "POST",
                    self.url + "/v1/parse/jobs",
                    json={
                        "files": [{"source": {"type": "file_id", "file_id": file_id}}],
                        "tier": "basic",
                        "ocr_mode": "ocr",
                        "output_formats": ["middle_json"],
                    },
                )
                job_id = identifier(job.get("job_id"))
                stage = "poll"
                while job.get("status") in {"queued", "running"}:
                    time.sleep(min(0.25, max(0, deadline - time.monotonic())))
                    job = request("GET", self.url + f"/v1/parse/jobs/{job_id}")
                if job.get("status") != "completed":
                    raise OcrParseError(
                        stage, "本地任务失败/取消/部分成功，未生成可入库结果", job_id=job_id
                    )
                files = job.get("files")
                if not isinstance(files, list) or len(files) != 1:
                    raise OcrParseError(stage, "本地任务文件数不匹配", job_id=job_id)
                output_id = identifier(files[0]["output_files"]["middle_json"]["file_id"])
                stage = "download"
                return request("GET", self.url + f"/v1/files/{output_id}/content")
            except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError) as exc:
                cancel_required = isinstance(exc, httpx.TimeoutException)
                raise OcrParseError(
                    stage, f"本地服务不可用或响应无效（{type(exc).__name__}）", job_id=job_id
                ) from exc
            finally:
                if job_id is not None and (
                    cancel_required or (stage == "poll" and time.monotonic() >= deadline)
                ):
                    try:
                        client.delete(self.url + f"/v1/parse/jobs/{job_id}", timeout=2)
                    except httpx.HTTPError:
                        pass  # best-effort cancellation; never changes the original timeout failure
