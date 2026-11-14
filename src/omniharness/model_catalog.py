"""User-initiated compatible model discovery and conservative routing advice.

Only explicitly registered endpoints and credentials are used. The catalog does
not establish model capability, availability or billing price.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import select

from .models import ModelConfig, Provider, now
from .providers import _safe_error, validate_config
from .services import ServiceError, require, runtime_config, snapshot_model

ModelId = Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:/-]+$")]
_STAGES = ("think", "plan", "dispatch", "execute", "review")
_MAX_CATALOG_BYTES = 2_000_000
_MAX_CATALOG_MODELS = 5000


class CatalogImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_ids: list[ModelId] = Field(min_length=1, max_length=100)


StageName = Literal["think", "plan", "dispatch", "execute", "review"]
Score = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
Price = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=8, allow_inf_nan=False)]


class UserAssessment(BaseModel):
    """Explicit user estimates; never silently promoted to verified facts."""
    model_config = ConfigDict(extra="forbid")
    model_config_id: str = Field(min_length=1, max_length=36)
    stage_scores: dict[StageName, Score] = Field(default_factory=dict)
    input_price_per_million: Price
    output_price_per_million: Price
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    source: str = Field(min_length=1, max_length=200)


class StageRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minimum_score: Score
    expected_input_tokens: int = Field(ge=0, le=10_000_000)
    expected_output_tokens: int = Field(ge=0, le=10_000_000)

    @model_validator(mode="after")
    def nonzero_usage(self):
        if not (self.expected_input_tokens or self.expected_output_tokens):
            raise ValueError("At least one token estimate must be nonzero")
        return self


class RecommendationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_model_config_id: str = Field(min_length=1, max_length=36)
    candidate_model_config_ids: list[str] = Field(default_factory=list, max_length=100)
    assessments: list[UserAssessment] = Field(default_factory=list, max_length=100)
    stage_requirements: dict[StageName, StageRequirement] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_assessments(self):
        ids = [entry.model_config_id for entry in self.assessments]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate model assessments")
        if self.candidate_model_config_ids and not set(ids).issubset(self.candidate_model_config_ids):
            raise ValueError("Assessments must refer to candidate models")
        return self


async def fetch_catalog(config):
    """Read only /models at the configured origin; never follow redirects."""
    if config.adapter_type not in {"openai-compatible", "openai"}:
        raise ServiceError("CATALOG_UNSUPPORTED", "该适配器暂不支持读取模型目录。", 422)
    validate_config(config)
    url = (config.base_url or "https://api.openai.com/v1").rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    try:
        # Ignore process proxy settings only for explicit loopback endpoints;
        # remote providers still use the user's configured environment proxy.
        local = urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"}
        async with httpx.AsyncClient(follow_redirects=False, timeout=min(config.timeout_seconds, 30),
                                     trust_env=not local) as client:
            async with client.stream("GET", url, headers=headers) as response:
                if 300 <= response.status_code < 400:
                    raise ServiceError("CATALOG_REDIRECT", "模型目录接口重定向；为保护密钥已停止请求。", 422)
                response.raise_for_status()
                chunks = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_CATALOG_BYTES:
                        raise ServiceError("CATALOG_TOO_LARGE", "模型目录响应过大。", 422)
                    chunks.append(chunk)
        data = json.loads(b"".join(chunks))
    except ServiceError:
        raise
    except (ValueError, TypeError, UnicodeError):
        raise ServiceError("CATALOG_INVALID", "模型目录响应格式无效。", 422) from None
    except Exception as exc:
        error = _safe_error(exc)
        raise ServiceError(error.code, error.public_message, 422) from None
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ServiceError("CATALOG_INVALID", "模型目录响应格式无效。", 422)
    if len(data["data"]) > _MAX_CATALOG_MODELS:
        raise ServiceError("CATALOG_TOO_LARGE", "模型目录条目过多。", 422)
    ids = []
    seen = set()
    for row in data["data"]:
        identity = row.get("id") if isinstance(row, dict) else None
        if (not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,200}", identity)
                or (config.api_key and config.api_key in identity) or identity in seen):
            continue
        ids.append(identity)
        seen.add(identity)
    return ids


def create_catalog_router(factory, secrets):
    """Mount with existing session authentication before the SPA catch-all."""
    router = APIRouter(prefix="/api/v1")

    def source(identity):
        with factory() as db:
            snapshot = snapshot_model(db, identity)
        return snapshot, runtime_config(snapshot, secrets)

    @router.get("/model-configs/{identity}/catalog")
    async def catalog(identity: str):
        snapshot, config = source(identity)
        ids = await fetch_catalog(config)
        # Do not return remote arbitrary metadata: it is not trustworthy and
        # might include credential-like values. Only the safe model ID survives.
        return {"model_config_id": snapshot["id"], "fetched_at": now().isoformat(),
                "models": [{"id": item, "owned_by": None, "capabilities": None, "price": None} for item in ids]}

    @router.post("/model-configs/{identity}/catalog/import")
    async def import_catalog(identity: str, body: CatalogImport):
        snapshot, config = source(identity)
        available = set(await fetch_catalog(config))
        selected = list(dict.fromkeys(body.model_ids))
        if any(item not in available for item in selected):
            raise ServiceError("CATALOG_ENTRY_MISSING", "所选模型不在当前目录中，请刷新后重试。", 409)
        results = []
        with factory.begin() as db:
            # Serialize imports from the same source on PostgreSQL and reject
            # changed settings rather than copying stale credentials.
            source_model = db.scalar(select(ModelConfig).where(ModelConfig.id == identity).with_for_update())
            if source_model is None:
                raise ServiceError("NOT_FOUND", "模型配置不存在。", 404)
            provider = db.scalar(select(Provider).where(Provider.id == source_model.provider_id).with_for_update())
            if (not provider.enabled or source_model.version != snapshot["version"]
                    or provider.adapter_type != snapshot["adapter_type"] or provider.base_url != snapshot["base_url"]
                    or provider.encrypted_api_key != snapshot["encrypted_api_key"]):
                raise ServiceError("CONFIG_CHANGED", "读取目录期间模型配置已改变，请重试。", 409)
            for name in selected:
                existing = db.scalar(select(ModelConfig).join(Provider).where(
                    Provider.adapter_type == provider.adapter_type,
                    Provider.base_url == provider.base_url,
                    Provider.encrypted_api_key == provider.encrypted_api_key,
                    ModelConfig.model_name == name,
                ).order_by(ModelConfig.created_at).limit(1))
                if existing:
                    results.append({"model_id": name, "status": "existing", "model_config_id": existing.id})
                    continue
                # The current schema couples enable/edit operations to Provider;
                # independent encrypted copies avoid editing one model changing
                # or disabling its siblings. They are not automatically synced.
                clone = Provider(adapter_type=provider.adapter_type, base_url=provider.base_url,
                                 encrypted_api_key=provider.encrypted_api_key, enabled=True, version=1)
                db.add(clone)
                db.flush()
                model = ModelConfig(provider_id=clone.id, name=name[:120], model_name=name,
                                    parameters={}, timeout_seconds=source_model.timeout_seconds,
                                    max_output_tokens=source_model.max_output_tokens, capabilities={})
                db.add(model)
                db.flush()
                results.append({"model_id": name, "status": "created", "model_config_id": model.id})
        return {"results": results}

    @router.post("/model-routing/recommend")
    def recommend(body: RecommendationRequest):
        with factory() as db:
            default = require(db, ModelConfig, body.default_model_config_id)
            provider = require(db, Provider, default.provider_id)
            if not provider.enabled:
                raise ServiceError("MODEL_DISABLED", "默认模型已停用。", 422)
            candidates = (set(body.candidate_model_config_ids) | {body.default_model_config_id}
                          | {entry.model_config_id for entry in body.assessments})
            for identity in candidates:
                candidate = require(db, ModelConfig, identity)
                if not require(db, Provider, candidate.provider_id).enabled:
                    raise ServiceError("MODEL_DISABLED", "候选模型已停用。", 422)
            results = []
            for stage in _STAGES:
                requirement = body.stage_requirements.get(stage)
                eligible = [a for a in body.assessments if requirement is not None
                            and stage in a.stage_scores and a.stage_scores[stage] >= requirement.minimum_score]
                currencies = {a.currency for a in eligible}
                rated_ids = {a.model_config_id for a in body.assessments if stage in a.stage_scores}
                evidence = {"qualified_model_config_ids": [a.model_config_id for a in eligible],
                            "unassessed_model_config_ids": sorted(candidates - rated_ids),
                            "comparison_currency_conflict": len(currencies) > 1}
                # Do not compare across currencies or present a single candidate
                # as a ranking; assumptions and ratings are user estimates.
                if len(eligible) >= 2 and len(currencies) == 1:
                    def estimated_cost(a):
                        return (a.input_price_per_million * requirement.expected_input_tokens
                                + a.output_price_per_million * requirement.expected_output_tokens) / Decimal(1_000_000)

                    choice = min(eligible, key=lambda a: (estimated_cost(a), a.model_config_id))
                    results.append({
                        "stage": stage, "model_config_id": choice.model_config_id, "evidence": evidence,
                        "reason": "依据用户估计的阶段评分、价格和用量，在达到门槛的候选中选择预计费用较低者。",
                        "capabilities": {"stage_score": choice.stage_scores[stage],
                                         "minimum_score": requirement.minimum_score,
                                         "source": "user_estimate"},
                        "price": {"estimated_cost": str(estimated_cost(choice)), "currency": choice.currency,
                                  "input_price_per_million": str(choice.input_price_per_million),
                                  "output_price_per_million": str(choice.output_price_per_million),
                                  "expected_input_tokens": requirement.expected_input_tokens,
                                  "expected_output_tokens": requirement.expected_output_tokens,
                                  "source": choice.source, "basis": "user_estimate"},
                    })
                else:
                    results.append({"stage": stage, "model_config_id": default.id, "evidence": evidence,
                                    "reason": "缺少同币种可比较价格、评分或阶段门槛，暂继承默认模型，可手动调整。",
                                    "capabilities": default.capabilities or None, "price": None})
        return {"recommendations": results,
                "limitations": ["模型目录不证明模型可调用、能力或价格。评分、单价与用量均为用户估计。",
                                "推荐仅为草案，不保证最优或节省费用，费用不是供应商账单。"]}

    return router
