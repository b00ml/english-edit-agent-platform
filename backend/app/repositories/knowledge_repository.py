"""
知识库 Repository
职责：KnowledgeChunk 数据访问、向量检索
"""

from typing import List

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from app.models import KnowledgeChunk
from app.rag.retrieval.models import leaf_predicate
from app.repositories.base import BaseRepository


class KnowledgeRepository(BaseRepository[KnowledgeChunk]):
    """知识库数据访问层。"""

    def __init__(self, db: Session):
        super().__init__(KnowledgeChunk, db)

    def list_by_source_type(
        self, source_type: str, skip: int = 0, limit: int = 50
    ) -> List[KnowledgeChunk]:
        """按资料类型查询（教材/课标/真题）。"""
        return (
            self.db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.source_type == source_type)
            .order_by(desc(KnowledgeChunk.created_at))
            .offset(skip)
            .limit(limit)
            .all()
        )

    def list_by_knowledge_point(
        self, knowledge_point: str, skip: int = 0, limit: int = 50
    ) -> List[KnowledgeChunk]:
        """按知识点查询。"""
        return (
            self.db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.knowledge_point == knowledge_point)
            .order_by(desc(KnowledgeChunk.created_at))
            .offset(skip)
            .limit(limit)
            .all()
        )

    def list_by_tenant(
        self, tenant_id: str, skip: int = 0, limit: int = 50
    ) -> List[KnowledgeChunk]:
        """按租户查询知识库。"""
        return (
            self.db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.tenant_id == tenant_id)
            .order_by(desc(KnowledgeChunk.created_at))
            .offset(skip)
            .limit(limit)
            .all()
        )

    def search_by_embedding(
        self, embedding: List[float], limit: int = 5, threshold: float = 0.7
    ) -> List[KnowledgeChunk]:
        """向量相似度检索（余弦相似度 > threshold）。

        pgvector 余弦距离 = 1 - 余弦相似度；父上下文无向量，不参与召回。
        """
        if not -1 <= threshold <= 1:
            raise ValueError("余弦相似度阈值必须位于 [-1,1]")
        return (
            self.db.query(KnowledgeChunk)
            .filter(leaf_predicate(), KnowledgeChunk.embedding.is_not(None))
            .filter(KnowledgeChunk.embedding.cosine_distance(embedding) <= 1 - threshold)
            .order_by(KnowledgeChunk.embedding.cosine_distance(embedding))
            .limit(limit)
            .all()
        )

    def search_by_knowledge_point_and_embedding(
        self, knowledge_point: str, embedding: List[float], limit: int = 5
    ) -> List[KnowledgeChunk]:
        """按精确知识点过滤后进行叶子块向量检索。"""
        return (
            self.db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.knowledge_point == knowledge_point)
            .filter(leaf_predicate(), KnowledgeChunk.embedding.is_not(None))
            .order_by(KnowledgeChunk.embedding.cosine_distance(embedding))
            .limit(limit)
            .all()
        )

    def count_by_source_type(self, source_type: str) -> int:
        """统计指定资料类型的分块数。"""
        return (
            self.db.query(func.count(KnowledgeChunk.id))
            .filter(KnowledgeChunk.source_type == source_type)
            .scalar()
        )

    def count_by_tenant(self, tenant_id: str) -> int:
        """统计租户的知识库分块数。"""
        return (
            self.db.query(func.count(KnowledgeChunk.id))
            .filter(KnowledgeChunk.tenant_id == tenant_id)
            .scalar()
        )
