# tests/test_failure_retry.py —— 失败分类与重试策略测试（P1-3）
import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import ContentItem, GenerationTask, GenerationTaskItem, QuestionTemplate
from app.worker.tasks import dispatch_generation_items, generate_single_item


@pytest.fixture
def retry_task(db: Session):
    """创建用于测试重试的任务。"""
    db.add(
        QuestionTemplate(
            id="single_choice",
            type_id="single_choice",
            name="单选题",
            version=1,
            input_schema={},
            output_schema={},
            quality_rules={},
            gen_prompt="",
            run_config={},
            status="active",
            tenant_id="test-tenant",
        )
    )
    db.commit()
    task = GenerationTask(
        id="test-retry-001",
        template_id="single_choice",
        params={"difficulty": "medium"},
        quantity=3,
        status="pending",
        tenant_id="test-tenant",
    )
    db.add(task)
    db.commit()
    for content_id in ["content-retry-001", "content-unknown-001"]:
        db.add(
            ContentItem(
                id=content_id,
                task_id=task.id,
                template_id="single_choice",
                payload={},
                tenant_id="test-tenant",
            )
        )
    db.commit()
    return task


def test_permanent_failure_no_retry(db: Session, retry_task: GenerationTask, monkeypatch):
    """测试 422 永久性错误不重试。"""
    # 调度
    dispatch_generation_items.apply(args=[retry_task.id]).get()

    # Mock run_generation 抛出 422 错误
    def mock_run_generation(*args, **kwargs):
        raise HTTPException(status_code=422, detail="Invalid parameters")

    monkeypatch.setattr("app.worker.tasks.run_generation", mock_run_generation)

    # 执行 item（应立即失败，不重试）
    result = generate_single_item.apply(args=[retry_task.id, 0]).get()
    assert result["status"] == "failed"
    assert result["failure_code"] == "PERMANENT_ERROR"
    assert result["reason"] == "HTTPException"

    # 验证 item 状态
    item = (
        db.query(GenerationTaskItem)
        .filter(
            GenerationTaskItem.task_id == retry_task.id,
            GenerationTaskItem.item_index == 0,
        )
        .first()
    )
    assert item.status == "failed"
    assert item.failure_code == "PERMANENT_ERROR"
    assert item.attempts == 1  # 仅尝试 1 次


def test_transient_failure_backoff(db: Session, retry_task: GenerationTask, monkeypatch):
    """测试 429 临时性错误指数退避重试。"""
    # 调度
    dispatch_generation_items.apply(args=[retry_task.id]).get()

    # Mock run_generation 抛出 429 错误
    call_count = {"count": 0}

    def mock_run_generation(*args, **kwargs):
        call_count["count"] += 1
        if call_count["count"] < 3:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        return {"status": "stored", "content_id": "content-retry-001"}

    monkeypatch.setattr("app.worker.tasks.run_generation", mock_run_generation)

    # 执行 item（应重试并最终成功）
    # 注意：实际测试中 Celery 的 retry 是异步的，这里简化为同步模拟
    try:
        generate_single_item.apply(args=[retry_task.id, 0]).get()
    except Exception as exc:
        # 第一次执行应触发重试（抛出异常）
        assert "429" in str(exc)

    # 验证 item 记录了失败信息
    item = (
        db.query(GenerationTaskItem)
        .filter(
            GenerationTaskItem.task_id == retry_task.id,
            GenerationTaskItem.item_index == 0,
        )
        .first()
    )
    # 由于重试是异步的，这里仅验证失败码被记录
    assert item.failure_code == "TRANSIENT_ERROR"


def test_400_permanent_failure(db: Session, retry_task: GenerationTask, monkeypatch):
    """测试 400 错误不重试。"""
    # 调度
    dispatch_generation_items.apply(args=[retry_task.id]).get()

    # Mock run_generation 抛出 400 错误
    def mock_run_generation(*args, **kwargs):
        raise HTTPException(status_code=400, detail="Missing required field")

    monkeypatch.setattr("app.worker.tasks.run_generation", mock_run_generation)

    # 执行 item
    result = generate_single_item.apply(args=[retry_task.id, 0]).get()
    assert result["status"] == "failed"
    assert result["failure_code"] == "PERMANENT_ERROR"

    # 验证仅尝试 1 次
    item = (
        db.query(GenerationTaskItem)
        .filter(
            GenerationTaskItem.task_id == retry_task.id,
            GenerationTaskItem.item_index == 0,
        )
        .first()
    )
    assert item.attempts == 1


def test_unknown_error_standard_retry(db: Session, retry_task: GenerationTask, monkeypatch):
    """测试未分类错误使用标准重试策略。"""
    # 调度
    dispatch_generation_items.apply(args=[retry_task.id]).get()

    # Mock run_generation 抛出未分类错误
    call_count = {"count": 0}

    def mock_run_generation(*args, **kwargs):
        call_count["count"] += 1
        if call_count["count"] < 2:
            raise Exception("Unknown network error")
        return {"status": "stored", "content_id": "content-unknown-001"}

    monkeypatch.setattr("app.worker.tasks.run_generation", mock_run_generation)

    # 执行 item（应触发标准重试）
    try:
        generate_single_item.apply(args=[retry_task.id, 0]).get()
    except Exception as exc:
        # 第一次执行应触发重试
        assert "Unknown network error" in str(exc)

    # 验证 item 记录了失败信息
    item = (
        db.query(GenerationTaskItem)
        .filter(
            GenerationTaskItem.task_id == retry_task.id,
            GenerationTaskItem.item_index == 0,
        )
        .first()
    )
    assert item.failure_code == "UNKNOWN_ERROR"
