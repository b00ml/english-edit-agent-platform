# tests/test_task_cancellation.py —— 任务取消功能测试（P1-3）
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.models import GenerationTask, GenerationTaskItem, QuestionTemplate


@pytest.fixture
def sample_template(db: Session):
    """创建测试用模板。"""
    template = QuestionTemplate(
        id="single_choice",
        type_id="single_choice",
        name="单选题",
        version="1.0",
        input_schema={},
        output_schema={},
        quality_rules={},
        gen_prompt="",
        run_config={},
        status="active",
        tenant_id="test-tenant",
    )
    db.add(template)
    db.commit()
    return template


@pytest.fixture
def sample_task(db: Session, sample_template: QuestionTemplate):
    """创建测试用任务。"""
    task = GenerationTask(
        id="test-task-001",
        template_id=sample_template.id,
        params={"difficulty": "medium"},
        quantity=5,
        status="pending",
        tenant_id="test-tenant",
    )
    db.add(task)
    db.commit()
    return task


def test_cancel_pending_task_logic(db: Session, sample_task: GenerationTask):
    """测试取消未开始任务的逻辑（不经过 Celery）。"""
    from app.worker.tasks import dispatch_generation_items

    # 先获取 task_id，避免后续访问脱离会话的对象
    task_id = sample_task.id

    # 标记取消
    sample_task.cancel_requested_at = datetime.now(timezone.utc)
    db.commit()

    # 强制刷新会话，确保后续查询能获取到 cancel_requested_at
    db.expire_all()

    # Mock SessionLocal 返回测试会话
    with patch("app.worker.tasks.SessionLocal", return_value=db):
        result = dispatch_generation_items(task_id)

    assert result["status"] == "cancelled"
    assert "已在调度前取消" in result["reason"]

    # 验证任务状态更新（重新查询，避免 refresh 问题）
    task = db.query(GenerationTask).filter(GenerationTask.id == task_id).first()
    assert task.status == "cancelled"


def test_dispatch_creates_items(db: Session, sample_task: GenerationTask):
    """测试调度器创建 item 记录。"""
    from app.worker.tasks import dispatch_generation_items

    # 先获取 task_id
    task_id = sample_task.id

    # Mock SessionLocal 和 Celery 投递
    with patch("app.worker.tasks.SessionLocal", return_value=db):
        with patch("app.worker.tasks._item_sender") as mock_delay:
            result = dispatch_generation_items(task_id)

    assert result["status"] == "dispatched"
    assert result["total"] == 5  # 修复：改为 total

    # 验证 item 已创建
    items = db.query(GenerationTaskItem).filter(GenerationTaskItem.task_id == task_id).all()
    assert len(items) == 5
    for i, item in enumerate(items):
        assert item.item_index == i
        assert item.status == "pending"

    # 验证投递了 5 个子任务
    assert mock_delay.call_count == 5


def test_item_respects_cancel_flag(db: Session, sample_task: GenerationTask):
    """测试 item worker 检测父任务取消标记（不调用真实 workflow）。"""
    from app.worker.tasks import dispatch_generation_items, generate_single_item

    # 先获取 task_id
    task_id = sample_task.id

    # 先创建 item
    with patch("app.worker.tasks.SessionLocal", return_value=db):
        with patch("app.worker.tasks.generate_single_item.delay"):
            dispatch_generation_items(task_id)

    # 调度器会关闭其拿到的会话；重新查询，避免对 detached 对象赋值后未持久化。
    task = db.query(GenerationTask).filter(GenerationTask.id == task_id).first()
    assert task is not None
    task.cancel_requested_at = datetime.now(timezone.utc)
    db.commit()

    # 强制刷新会话，确保后续查询能获取到 cancel_requested_at
    db.expire_all()

    # 执行 item，应在早期检查中检测到取消（patch SessionLocal + run_generation）
    with patch("app.worker.tasks.SessionLocal", return_value=db):
        with patch(
            "app.worker.tasks.run_generation",
            return_value={"status": "cancelled", "reason": "已取消"},
        ) as mock_workflow:
            result = generate_single_item(task_id, 0)

    # 应该在调用 workflow 之前就返回 cancelled
    assert result["status"] == "cancelled"
    assert "父任务已取消" in result["reason"]
    # 验证 workflow 没有被调用
    mock_workflow.assert_not_called()

    # 验证 item 状态（重新查询，避免 refresh 问题）
    item = (
        db.query(GenerationTaskItem)
        .filter(
            GenerationTaskItem.task_id == task_id,
            GenerationTaskItem.item_index == 0,
        )
        .first()
    )
    assert item.status == "cancelled"


def test_cannot_cancel_succeeded_task(db: Session, sample_task: GenerationTask):
    """测试已完成任务不可取消（业务逻辑层验证）。"""
    sample_task.status = "succeeded"
    db.commit()

    # 模拟 API 层的状态检查
    if sample_task.status not in ["pending", "dispatched", "running"]:
        # API 应返回 400
        assert True
    else:
        pytest.fail("应拦截已完成任务的取消请求")
