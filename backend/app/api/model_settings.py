"""Admin-only provider endpoints and persistent template model routing."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from openai import OpenAIError
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_config_change
from app.config import settings
from app.database import get_db
from app.engine.providers import client_for_profile, encrypt_api_key
from app.errors import ProviderConfigurationError
from app.models import ModelProfile, ModelProvider, ModelRoute, QuestionTemplate, User, new_uuid
from app.security import require_permission
from app.versioning import ensure_model_profile_hash, hash_value

router = APIRouter(prefix="/api/model-settings", tags=["model-settings"])
admin = require_permission("model:manage")


class ProviderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    name: str = Field(min_length=1, max_length=64)
    base_url: str = Field(max_length=512)
    api_key: SecretStr | None = Field(default=None, max_length=8192)
    status: Literal["enabled", "disabled"] = "enabled"

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        url = urlsplit(value.strip())
        try:
            port = url.port
        except ValueError as exc:
            raise ValueError("Base URL端口不合法") from exc
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Base URL必须是无内嵌凭据、query、fragment的HTTP(S)服务地址")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("Base URL端口不合法")
        return value.strip().rstrip("/")


class ProviderOut(BaseModel):
    id: str
    name: str
    base_url: str
    status: str
    has_api_key: bool
    config_hash: str


class RouteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_id: str
    generation_profile: str | None = None
    judge_profile: str | None = None


def provider_out(provider: ModelProvider) -> ProviderOut:
    return ProviderOut(
        id=provider.id,
        name=provider.name,
        base_url=provider.base_url,
        status=provider.status,
        has_api_key=bool(provider.api_key_ciphertext),
        config_hash=provider.config_hash,
    )


@router.get("/capabilities")
def capabilities(user: User = Depends(admin)) -> dict[str, object]:
    # No secret or key fingerprints are exposed; invalid keys also fail closed.
    from app.engine.providers import _cipher

    try:
        _cipher()
        writable = True
    except ProviderConfigurationError:
        writable = False
    return {
        "credential_storage_ready": writable,
        "legacy_base_url": settings.LLM_API_BASE,
        "legacy_model": settings.LLM_MODEL_NAME,
        "protocol": "openai_compatible",
    }


@router.get("/providers", response_model=list[ProviderOut])
def providers(db: Session = Depends(get_db), user: User = Depends(admin)) -> list[ProviderOut]:
    return [provider_out(p) for p in db.query(ModelProvider).order_by(ModelProvider.name).all()]


@router.post("/providers", response_model=ProviderOut)
def save_provider(
    req: ProviderIn, db: Session = Depends(get_db), user: User = Depends(admin)
) -> ProviderOut:
    provider = db.get(ModelProvider, req.id) if req.id else None
    if req.id and provider is None:
        raise HTTPException(404, "Provider不存在")
    before = provider_out(provider).model_dump() if provider else None
    secret = req.api_key.get_secret_value().strip() if req.api_key else ""
    if provider is None and not secret:
        raise HTTPException(
            422, "新Provider必须填写API Key；不需要鉴权的本地兼容服务可填写其要求的占位值"
        )
    ciphertext = encrypt_api_key(secret) if secret else None
    if provider is None:
        provider = ModelProvider(
            api_key_ciphertext=ciphertext, name=req.name, base_url=req.base_url, status=req.status
        )
        db.add(provider)
    provider.name, provider.base_url, provider.status = req.name, req.base_url, req.status
    if ciphertext:
        provider.api_key_ciphertext = ciphertext
        provider.key_revision = new_uuid()
    if not provider.key_revision:
        provider.key_revision = new_uuid()
    provider.config_hash = hash_value(
        {
            "name": provider.name,
            "base_url": provider.base_url,
            "status": provider.status,
            "key_revision": provider.key_revision,
        }
    )
    try:
        db.flush()
        for profile in db.query(ModelProfile).filter(ModelProfile.provider_id == provider.id).all():
            profile.provider = provider.name
            profile.provider_config_hash = provider.config_hash
            ensure_model_profile_hash(profile)
        record_config_change(
            db,
            entity_type="model_provider",
            entity_id=provider.id,
            action="update" if before else "create",
            actor_id=user.id,
            tenant_id=None,
            before=before,
            after=provider_out(provider).model_dump(),
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Provider名称已存在或配置关联冲突") from exc
    return provider_out(provider)


@router.delete("/providers/{provider_id}")
def delete_provider(
    provider_id: str, db: Session = Depends(get_db), user: User = Depends(admin)
) -> dict[str, bool]:
    provider = db.get(ModelProvider, provider_id)
    if provider is None:
        raise HTTPException(404, "Provider不存在")
    if db.query(ModelProfile).filter(ModelProfile.provider_id == provider_id).first():
        raise HTTPException(409, "Provider仍被模型档案引用，请先解除绑定；也可禁用Provider")
    before = provider_out(provider).model_dump()
    db.delete(provider)
    record_config_change(
        db,
        entity_type="model_provider",
        entity_id=provider_id,
        action="delete",
        actor_id=user.id,
        tenant_id=None,
        before=before,
        after=None,
    )
    db.commit()
    return {"deleted": True}


@router.post("/providers/{provider_id}/probe")
def probe_provider(
    provider_id: str, db: Session = Depends(get_db), user: User = Depends(admin)
) -> dict[str, object]:
    provider = db.get(ModelProvider, provider_id)
    if provider is None:
        raise HTTPException(404, "Provider不存在")
    profile = ModelProfile(provider_id=provider.id, provider_record=provider)
    try:
        with client_for_profile(profile) as client:
            result = client.with_options(
                timeout=min(settings.LLM_TIMEOUT, 10.0), max_retries=0
            ).models.list()
            models = [model.id for model in result.data][:100]
    except OpenAIError as exc:
        # Do not expose provider error bodies, response headers, request URL secrets or raw keys.
        raise HTTPException(
            502, "Provider模型列表探测失败；请核对地址、鉴权和/models支持，不等同于生成能力失败"
        ) from exc
    return {"reachable": True, "models": models, "generation_verified": False}


@router.get("/routes")
def routes(db: Session = Depends(get_db), user: User = Depends(admin)) -> list[dict[str, object]]:
    overrides = {r.template_id: r for r in db.query(ModelRoute).all()}
    result = []
    for t in db.query(QuestionTemplate).order_by(QuestionTemplate.type_id).all():
        override = overrides.get(t.type_id)
        result.append(
            {
                "template_id": t.type_id,
                "name": t.name,
                "generation_profile": override.generation_profile if override else None,
                "judge_profile": override.judge_profile if override else None,
                "template_generation": (t.run_config or {}).get("model_profile"),
                "template_judge": (t.run_config or {}).get("judge_model"),
            }
        )
    return result


@router.post("/routes")
def save_route(
    req: RouteIn, db: Session = Depends(get_db), user: User = Depends(admin)
) -> dict[str, str | None]:
    template = (
        db.query(QuestionTemplate).filter(QuestionTemplate.type_id == req.template_id).first()
    )
    if template is None:
        raise HTTPException(404, "题型模板不存在")
    for name in (req.generation_profile, req.judge_profile):
        if not name:
            continue
        profile = (
            db.query(ModelProfile)
            .filter(
                ModelProfile.name == name,
                ModelProfile.status == "enabled",
                ModelProfile.tenant_id.is_(None),
            )
            .first()
        )
        if profile is None:
            raise HTTPException(422, "请选择启用的共享模型档案；不能使用不存在、禁用或私有租户档案")
        if profile.provider_id and (
            profile.provider_record is None or profile.provider_record.status != "enabled"
        ):
            raise HTTPException(422, "档案绑定的Provider不可用")
    route = db.get(ModelRoute, req.template_id)
    before = (
        {"generation_profile": route.generation_profile, "judge_profile": route.judge_profile}
        if route
        else None
    )
    if route is None:
        route = ModelRoute(template_id=req.template_id)
        db.add(route)
    route.generation_profile = req.generation_profile or None
    route.judge_profile = req.judge_profile or None
    after = {
        "template_id": route.template_id,
        "generation_profile": route.generation_profile,
        "judge_profile": route.judge_profile,
    }
    record_config_change(
        db,
        entity_type="model_route",
        entity_id=req.template_id,
        action="update" if before else "create",
        actor_id=user.id,
        tenant_id=None,
        before=before,
        after=after,
    )
    db.commit()
    return after
