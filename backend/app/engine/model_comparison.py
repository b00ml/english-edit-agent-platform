"""Bounded explicit A/B execution; no fallback, publication or manufactured human labels."""

from __future__ import annotations

import uuid
from typing import Any

import jsonschema
from fastapi import HTTPException
from openai import OpenAIError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.engine.fewshot import select_fewshot
from app.engine.quality import run_quality_check
from app.engine.structured_output import generate_structured
from app.errors import ModelRoutingError, PlatformError
from app.models import ModelProfile, QuestionTemplate, TraceLog
from app.rag.retriever import build_rag_context
from app.versioning import hash_value, model_profile_hash


class ComparisonCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(min_length=1, max_length=128)
    template_id: str
    params: dict[str, Any]  # Template input_schema boundary.


def _profile(session: Session, name: str) -> ModelProfile:
    profile = (
        session.query(ModelProfile)
        .filter(
            ModelProfile.name == name,
            ModelProfile.tenant_id.is_(None),
            ModelProfile.status == "enabled",
        )
        .first()
    )
    if profile is None:
        raise ModelRoutingError("比较档案不存在、未启用或不是共享档案；不能自动代选")
    if profile.provider_id and (
        profile.provider_record is None or profile.provider_record.status != "enabled"
    ):
        raise ModelRoutingError("比较档案的Provider不可用")
    return profile


def endpoint_identity(profile: ModelProfile, *, judge: bool = False) -> tuple[str, str]:
    from app.config import settings

    return (
        (
            profile.provider_record.base_url
            if profile.provider_id and profile.provider_record
            else (
                (settings.JUDGE_API_BASE or settings.LLM_API_BASE)
                if judge
                else settings.LLM_API_BASE
            )
        ).rstrip("/"),
        profile.model_name,
    )


def comparison_plan(session: Session, a: str, b: str, judge: str | None = None) -> dict[str, Any]:
    first, second = _profile(session, a), _profile(session, b)
    identities = [endpoint_identity(first), endpoint_identity(second)]
    same = identities[0] == identities[1]
    reviewer = _profile(session, judge) if judge else None
    return {
        "profiles": [
            {
                "name": p.name,
                "model": p.model_name,
                "provider_id": p.provider_id,
                "profile_hash": model_profile_hash(p),
            }
            for p in (first, second)
        ],
        "same_effective_model": same,
        "ready": not same,
        "judge_profile": reviewer.name if reviewer else None,
        "judge_matches_candidate": bool(
            reviewer and endpoint_identity(reviewer, judge=True) in identities
        ),
        "human_labels": "pending",
        "source": "configured_profiles_only",
        "note": (
            "Different endpoint/model identifiers do not prove "
            "different model families or expert quality."
        ),
    }


def compare_models(
    session: Session, cases: list[ComparisonCase], a: str, b: str, *, judge: str | None = None
) -> dict[str, Any]:
    plan = comparison_plan(session, a, b, judge)
    if not plan["ready"]:
        raise ModelRoutingError(
            "两个档案指向相同实际端点/模型；请先在模型设置配置真实不同候选，不执行重复收费比较"
        )
    if not cases or len(cases) > 10 or len({c.case_id for c in cases}) != len(cases):
        raise ValueError("比较需要1～10条唯一case_id，不自动扩批或重试失败批次")
    run_id = "compare:" + uuid.uuid4().hex
    profiles = {name: _profile(session, name) for name in (a, b)}
    judge_profile = _profile(session, judge) if judge else None
    results = []
    for offset, case in enumerate(cases):
        template = (
            session.query(QuestionTemplate)
            .filter(
                QuestionTemplate.type_id == case.template_id,
                QuestionTemplate.status == "enabled",
                QuestionTemplate.tenant_id.is_(None),
            )
            .first()
        )
        if template is None:
            raise ValueError("比较题型不存在、禁用或不是共享题型")
        params = {
            k: v
            for k, v in case.params.items()
            if k
            not in {
                "tenant_id",
                "rag_context",
                "fewshot_context",
                "fewshot_provenance",
                "revise_instruction",
                "revise_context",
            }
        }
        params["quantity"] = 1
        jsonschema.validate(params, template.input_schema)
        params["tenant_id"] = None
        rag_cfg = (template.run_config or {}).get("rag", {})
        provenance: dict[str, Any] = {}
        context = build_rag_context(
            session,
            query=str(params.get("knowledge_point") or ""),
            knowledge_point=str(params.get("knowledge_point") or ""),
            tenant_id=None,
            trace_id=run_id + ":source:" + str(offset),
            template_id=template.type_id,
            mode="required" if rag_cfg.get("require_human_verification") else rag_cfg.get("mode"),
            scope_mode=rag_cfg.get("scope_mode"),
            context_mode=rag_cfg.get("context_mode"),
            provenance=provenance,
        )
        if context:
            params["rag_context"] = context
        examples = select_fewshot(session, template, params)
        if examples["context"]:
            params["fewshot_context"] = examples["context"]
        params["fewshot_provenance"] = {k: v for k, v in examples.items() if k != "context"}
        frozen_hash = hash_value(params)
        order = (a, b) if offset % 2 == 0 else (b, a)
        for name in order:
            trace_id = run_id + ":" + str(offset) + ":" + ("A" if name == a else "B")
            result: dict[str, Any] = {
                "case_id": case.case_id,
                "profile": name,
                "trace_id": trace_id,
                "input_hash": frozen_hash,
                "template_version": template.version,
                "template_hash": template.template_hash,
                "rag_provenance": provenance,
                "human_status": "pending",
                "published": False,
            }
            try:
                payload = generate_structured(
                    template,
                    params,
                    profiles[name],
                    trace_id=trace_id,
                    template_id=template.type_id,
                )
                result.update(status="generated", payload=payload, payload_hash=hash_value(payload))
                if judge_profile:
                    score, dims = run_quality_check(
                        template,
                        payload,
                        model_profile=judge_profile,
                        model_name=judge_profile.model_name,
                        trace_id=trace_id,
                        template_id=template.type_id,
                        generation_params=params,
                    )
                    result.update(auto_score=score, dimension_scores=dims)
            except (PlatformError, HTTPException, OpenAIError) as exc:
                result.update(
                    status="failed",
                    error_code=exc.code if isinstance(exc, PlatformError) else type(exc).__name__,
                )
            session.expire_all()
            calls = session.query(TraceLog).filter(TraceLog.trace_id == trace_id).all()
            result.update(
                estimated_cost=sum(float(c.cost or 0) for c in calls),
                latency_ms=sum(float(c.latency_ms or 0) for c in calls),
                call_attempts=len(calls),
                usage_fully_reported=bool(calls) and all(c.usage_reported for c in calls),
                prompt_tokens=sum(c.prompt_tokens or 0 for c in calls),
                completion_tokens=sum(c.completion_tokens or 0 for c in calls),
            )
            results.append(result)
    all_calls = session.query(TraceLog).filter(TraceLog.trace_id.like(run_id + ":%")).all()
    return {
        "run_id": run_id,
        "plan": plan,
        "case_count": len(cases),
        "total_run_estimated_cost": sum(float(c.cost or 0) for c in all_calls),
        "shared_source_estimated_cost": sum(
            float(c.cost or 0) for c in all_calls if ":source:" in c.trace_id
        ),
        "results": results,
        "human_quality_verdict": None,
        "estimated_cost_not_provider_bill": True,
        "no_content_published": True,
    }
