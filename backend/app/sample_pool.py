# app/sample_pool.py —— 高质量样本沉淀服务（数据回流 / J2）
# 将人工通过/已发布的高质量内容沉淀为 few-shot / 微调语料样本，
# 支持按题型/知识点检索与 JSONL 导出（供后续微调或 few-shot 消费）。
from typing import Optional

from sqlalchemy.orm import Session

from app.models import ContentItem, GenerationTask, SamplePool, User
from app.tenancy import scope_query

# 高质量判定：人工质检通过或已发布
ELIGIBLE_STATUSES = ("passed", "published")


def is_eligible(item: ContentItem) -> bool:
    """判定内容条目是否为高质量样本（人工通过/已发布）。"""
    return item.status in ELIGIBLE_STATUSES


def _resolve_knowledge_point(db: Session, item: ContentItem) -> Optional[str]:
    """从生成任务参数中提取知识点标签，供样本检索。"""
    task = db.query(GenerationTask).filter(GenerationTask.id == item.task_id).first()
    if task and isinstance(task.params, dict):
        kp = task.params.get("knowledge_point")
        if kp:
            return str(kp)
    return None


def pool_item(
    db: Session,
    item: ContentItem,
    source: str = "manual",
    purpose: str = "sft",
) -> SamplePool:
    """将内容条目沉淀为回流样本（幂等：已沉淀则直接返回）。"""
    existing = db.query(SamplePool).filter(SamplePool.item_id == item.id).first()
    if existing:
        return existing
    sample = SamplePool(
        item_id=item.id,
        template_id=item.template_id,
        source=source,
        purpose=purpose,
        knowledge_point=_resolve_knowledge_point(db, item),
        payload=item.payload,
        tenant_id=item.tenant_id,
        meta={"qc_score": item.qc_score, "status": item.status},
    )
    db.add(sample)
    db.commit()
    db.refresh(sample)
    return sample


def sync_eligible(
    db: Session,
    source: str = "auto",
    purpose: str = "sft",
    user: User | None = None,
) -> int:
    """自动将全部高质量内容（人工通过/已发布）沉淀为样本，返回新增数。"""
    items = (
        scope_query(db.query(ContentItem), ContentItem, user)
        .filter(ContentItem.status.in_(ELIGIBLE_STATUSES))
        .all()
    )
    added = 0
    for item in items:
        exists = db.query(SamplePool).filter(SamplePool.item_id == item.id).first()
        if not exists:
            pool_item(db, item, source=source, purpose=purpose)
            added += 1
    return added


def list_samples(
    db: Session,
    template_id: Optional[str] = None,
    knowledge_point: Optional[str] = None,
    purpose: Optional[str] = None,
    source: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    user: User | None = None,
) -> tuple[list[SamplePool], int]:
    """检索回流样本（按题型/知识点/用途/来源过滤，分页）。"""
    query = scope_query(db.query(SamplePool), SamplePool, user)
    if template_id:
        query = query.filter(SamplePool.template_id == template_id)
    if knowledge_point:
        query = query.filter(SamplePool.knowledge_point == knowledge_point)
    if purpose:
        query = query.filter(SamplePool.purpose == purpose)
    if source:
        query = query.filter(SamplePool.source == source)
    total = query.count()
    rows = (
        query.order_by(SamplePool.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return rows, total


def remove_sample(db: Session, sample_id: str) -> bool:
    """从回流样本库中移除一条样本。"""
    sample = db.get(SamplePool, sample_id)
    if sample is None:
        return False
    db.delete(sample)
    db.commit()
    return True
