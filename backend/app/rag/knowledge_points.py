"""Validated, configuration-driven knowledge taxonomy. Labels are not permissions."""

from __future__ import annotations

import hashlib
import unicodedata
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.errors import InvalidKnowledgeError

ScopeMode = Literal["exact", "ancestor", "descendant", "related", "semantic"]


def normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


class KnowledgePoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1, max_length=128)
    canonical_name: str = Field(min_length=1, max_length=128)
    aliases: list[str] = Field(default_factory=list, max_length=32)
    parent_id: str | None = None
    related: list[str] = Field(default_factory=list, max_length=32)
    subject: str = "English"
    stage: str = "general"
    language: str = "mixed"
    status: Literal["enabled", "disabled"] = "enabled"


class CatalogData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    points: list[KnowledgePoint] = Field(max_length=5000)


class KnowledgeScope(BaseModel):
    canonical_name: str | None = None
    canonical_id: str | None = None
    ids: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    mode: ScopeMode = "exact"
    matched_via: str = "none"
    catalog_hash: str = ""
    source_name: str | None = None
    section_path: list[str] = Field(default_factory=list)


class KnowledgeCatalog:
    def __init__(self, data: CatalogData, digest: str) -> None:
        self.points = {point.id: point for point in data.points}
        self.hash = digest
        self.names: dict[str, str] = {}
        if len(self.points) != len(data.points):
            raise InvalidKnowledgeError("知识点 ID 重复")
        for point in data.points:
            if point.parent_id and point.parent_id not in self.points:
                raise InvalidKnowledgeError("知识点父级不存在")
            if any(value not in self.points for value in point.related):
                raise InvalidKnowledgeError("关联知识点不存在")
            for label in [point.id, point.canonical_name, *point.aliases]:
                key = normalized(label)
                if not key or len(key) > 128:
                    raise InvalidKnowledgeError("知识点名称/别名为空或过长")
                if key in self.names and self.names[key] != point.id:
                    raise InvalidKnowledgeError("知识点别名歧义")
                self.names[key] = point.id
            seen = {point.id}
            parent = point.parent_id
            while parent:
                if parent in seen:
                    raise InvalidKnowledgeError("知识点层级存在循环")
                seen.add(parent)
                parent = self.points[parent].parent_id

    def resolve(self, value: str) -> KnowledgePoint | None:
        key = self.names.get(normalized(value))
        point = self.points.get(key) if key else None
        if point and point.status != "enabled":
            raise InvalidKnowledgeError("该知识点已禁用")
        return point

    def scope(self, value: str | None, mode: ScopeMode) -> KnowledgeScope:
        if mode not in {"exact", "ancestor", "descendant", "related", "semantic"}:
            raise InvalidKnowledgeError("未知知识点召回模式")
        if value is not None and len(value) > 128:
            raise InvalidKnowledgeError("知识点查询超过 128 字符")
        point = self.resolve(value) if value else None
        result = KnowledgeScope(mode=mode, catalog_hash=self.hash)
        if not value:
            return result
        if not point:
            result.canonical_name = value.strip()
            result.labels = [normalized(value)] if mode != "semantic" else []
            result.matched_via = "literal"
            return result
        result.canonical_name, result.canonical_id = point.canonical_name, point.id
        result.matched_via = (
            "exact" if normalized(value) == normalized(point.canonical_name) else "alias"
        )
        if mode == "semantic":
            return result
        keys = {point.id}
        if mode == "ancestor":
            parent = point.parent_id
            while parent:
                keys.add(parent)
                parent = self.points[parent].parent_id
        elif mode == "related":
            keys.update(point.related)
        elif mode == "descendant":
            changed = True
            while changed:
                before = len(keys)
                keys.update(p.id for p in self.points.values() if p.parent_id in keys)
                changed = len(keys) > before
        selected = [
            self.points[key] for key in sorted(keys) if self.points[key].status == "enabled"
        ]
        if len(selected) > settings.RAG_KNOWLEDGE_SCOPE_MAX:
            raise InvalidKnowledgeError("知识点扩展范围超过安全上限")
        result.ids = [p.id for p in selected]
        result.labels = sorted(
            {normalized(label) for p in selected for label in [p.id, p.canonical_name, *p.aliases]}
        )
        return result

    def tags(self, values: list[str]) -> tuple[list[str], list[str]]:
        if len(values) > 16:
            raise InvalidKnowledgeError("知识点标签最多 16 个")
        ids, labels = set(), set()
        for value in values:
            if not value.strip() or len(value) > 128:
                raise InvalidKnowledgeError("知识点标签为空或过长")
            point = self.resolve(value)
            if point:
                ids.add(point.id)
                labels.add(point.canonical_name)
            else:
                labels.add(value.strip())
        return sorted(ids), sorted(labels)


def load_catalog() -> KnowledgeCatalog:
    path = (
        Path(settings.RAG_KNOWLEDGE_CATALOG)
        if settings.RAG_KNOWLEDGE_CATALOG
        else Path(__file__).with_name("knowledge_points.yml")
    )
    try:
        raw = path.read_bytes()
        data = CatalogData.model_validate(yaml.safe_load(raw))
        return KnowledgeCatalog(data, hashlib.sha256(raw).hexdigest())
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise InvalidKnowledgeError("知识点目录配置无效") from exc
