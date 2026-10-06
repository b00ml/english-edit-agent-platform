"""Admin-configured endpoints. No silent credential fallback for a bound provider."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from openai import OpenAI
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import ProviderConfigurationError
from app.models import ModelProfile, ModelRoute


def _cipher() -> Fernet:
    try:
        if not settings.PROVIDER_SECRET_KEY:
            raise ValueError("missing dedicated key")
        return Fernet(settings.PROVIDER_SECRET_KEY.encode("ascii"))
    except (ValueError, UnicodeError) as exc:
        raise ProviderConfigurationError(
            "Provider凭据加密密钥未配置或无效；请配置独立PROVIDER_SECRET_KEY"
        ) from exc


def encrypt_api_key(value: str) -> str:
    return _cipher().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_api_key(value: str) -> str:
    try:
        return _cipher().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise ProviderConfigurationError(
            "Provider凭据无法解密；请恢复原加密密钥或重新设置API Key"
        ) from exc


def client_for_profile(profile: ModelProfile) -> OpenAI:
    provider = profile.provider_record
    if provider is None or provider.status != "enabled":
        raise ProviderConfigurationError("模型绑定的Provider不存在或已禁用")
    api_key = decrypt_api_key(provider.api_key_ciphertext)
    return OpenAI(base_url=provider.base_url, api_key=api_key, timeout=settings.LLM_TIMEOUT)


def route_for_template(session: Session, template_id: str) -> ModelRoute | None:
    return session.get(ModelRoute, template_id)


def judge_profile_for_template(
    session: Session, template_id: str, tenant_id: str | None
) -> ModelProfile | None:
    route = route_for_template(session, template_id)
    if route is None or not route.judge_profile:
        return None
    profile = session.scalar(
        select(ModelProfile).where(
            ModelProfile.name == route.judge_profile,
            (ModelProfile.tenant_id == tenant_id) | ModelProfile.tenant_id.is_(None),
        )
    )
    if profile is None or profile.status != "enabled":
        raise ProviderConfigurationError("配置的Judge档案不存在或已禁用，不能静默回退")
    return profile


def profile_identity(profile: ModelProfile | None) -> dict[str, str | None]:
    if profile is None:
        return {"binding": "environment"}
    return {
        "name": profile.name,
        "model_name": profile.model_name,
        "provider_id": profile.provider_id,
        "provider_config_hash": profile.provider_config_hash,
        "model_hash": profile.model_hash,
    }
