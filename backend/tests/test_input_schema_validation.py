"""
测试输入 Schema 校验（P1-2）
验收标准：
- single_choice 缺少选项/选项数量错误/字段类型错误在 API 422 阶段返回
- 输入校验错误不产生 Celery 任务、不消耗 LLM 调用
"""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.errors import InvalidTemplateParamsError
from app.models import QuestionTemplate, User
from app.schemas import GenerateRequest
from app.services.generation_service import GenerationService


@pytest.fixture
def mock_db():
    """模拟数据库会话。"""
    return MagicMock(spec=Session)


@pytest.fixture
def mock_user():
    """模拟当前用户。"""
    user = MagicMock(spec=User)
    user.id = 1
    user.tenant_id = "test-tenant"
    user.role = "researcher"
    return user


@pytest.fixture
def mock_celery():
    """模拟 Celery 应用。"""
    celery = MagicMock()
    celery.send_task = MagicMock()
    return celery


@pytest.fixture
def single_choice_template():
    """模拟单选题模板（带 input_schema）。"""
    template = MagicMock(spec=QuestionTemplate)
    template.type_id = "single_choice"
    template.tenant_id = None
    template.disabled = False
    template.updated_at = None
    template.input_schema = {
        "type": "object",
        "properties": {
            "topic": {"type": "string"},
            "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]},
            "options_count": {"type": "integer", "minimum": 2, "maximum": 6},
        },
        "required": ["topic", "difficulty", "options_count"],
    }
    return template


def test_valid_params_pass_validation(mock_db, mock_user, mock_celery, single_choice_template):
    """正常参数应通过校验并创建任务。"""
    # Mock 数据库查询返回模板
    mock_db.query.return_value.filter.return_value.first.return_value = single_choice_template

    # Mock TaskRepository.get_by_request_hash 返回 None（无重复任务）
    with patch("app.services.generation_service.TaskRepository") as MockRepo:
        mock_repo = MockRepo.return_value
        mock_repo.get_by_request_hash.return_value = None

        # Mock create 返回任务对象
        mock_task = MagicMock()
        mock_task.id = "task-123"
        mock_task.status = "pending"
        mock_repo.create.return_value = mock_task

        service = GenerationService(mock_db)
        req = GenerateRequest(
            template_id="single_choice",
            params={"topic": "数学", "difficulty": "easy", "options_count": 4},
            quantity=10,
        )

        with patch(
            "app.services.generation_service.relay_pending", return_value={"scanned": 0}
        ) as relay:
            response = service.create_task(req, mock_user, mock_celery)
            relay.assert_called_once()

        assert response.task_id == "task-123"
        assert response.status == "pending"
        # 验证 Celery 任务已投递
        mock_celery.send_task.assert_not_called()  # No unsafe direct-send fallback.


def test_missing_required_field_fails(mock_db, mock_user, mock_celery, single_choice_template):
    """缺少必填字段应抛出 InvalidTemplateParamsError。"""
    mock_db.query.return_value.filter.return_value.first.return_value = single_choice_template

    service = GenerationService(mock_db)
    req = GenerateRequest(
        template_id="single_choice",
        params={"topic": "数学", "difficulty": "easy"},  # 缺少 options_count
        quantity=10,
    )

    with pytest.raises(InvalidTemplateParamsError) as exc_info:
        service.create_task(req, mock_user, mock_celery)

    # 验证错误码与字段路径
    assert exc_info.value.code == "INVALID_TEMPLATE_PARAMS"
    assert (
        "options_count" in str(exc_info.value.field_path)
        or "required" in exc_info.value.validation_keyword
    )
    # 验证 Celery 任务未投递
    mock_celery.send_task.assert_not_called()


def test_invalid_enum_value_fails(mock_db, mock_user, mock_celery, single_choice_template):
    """枚举值错误应抛出 InvalidTemplateParamsError。"""
    mock_db.query.return_value.filter.return_value.first.return_value = single_choice_template

    service = GenerationService(mock_db)
    req = GenerateRequest(
        template_id="single_choice",
        params={"topic": "数学", "difficulty": "超难", "options_count": 4},  # 枚举值错误
        quantity=10,
    )

    with pytest.raises(InvalidTemplateParamsError) as exc_info:
        service.create_task(req, mock_user, mock_celery)

    assert exc_info.value.code == "INVALID_TEMPLATE_PARAMS"
    assert "enum" in exc_info.value.validation_keyword
    mock_celery.send_task.assert_not_called()


def test_invalid_type_fails(mock_db, mock_user, mock_celery, single_choice_template):
    """字段类型错误应抛出 InvalidTemplateParamsError。"""
    mock_db.query.return_value.filter.return_value.first.return_value = single_choice_template

    service = GenerationService(mock_db)
    req = GenerateRequest(
        template_id="single_choice",
        params={"topic": "数学", "difficulty": "easy", "options_count": "四"},  # 类型错误
        quantity=10,
    )

    with pytest.raises(InvalidTemplateParamsError) as exc_info:
        service.create_task(req, mock_user, mock_celery)

    assert exc_info.value.code == "INVALID_TEMPLATE_PARAMS"
    assert "type" in exc_info.value.validation_keyword
    mock_celery.send_task.assert_not_called()


def test_out_of_range_integer_fails(mock_db, mock_user, mock_celery, single_choice_template):
    """整数超出范围应抛出 InvalidTemplateParamsError。"""
    mock_db.query.return_value.filter.return_value.first.return_value = single_choice_template

    service = GenerationService(mock_db)
    req = GenerateRequest(
        template_id="single_choice",
        params={"topic": "数学", "difficulty": "easy", "options_count": 10},  # 超过 maximum=6
        quantity=10,
    )

    with pytest.raises(InvalidTemplateParamsError) as exc_info:
        service.create_task(req, mock_user, mock_celery)

    assert exc_info.value.code == "INVALID_TEMPLATE_PARAMS"
    assert "maximum" in exc_info.value.validation_keyword
    mock_celery.send_task.assert_not_called()


def test_template_without_schema_skips_validation(mock_db, mock_user, mock_celery):
    """模板无 input_schema 时跳过参数校验。"""
    template = MagicMock(spec=QuestionTemplate)
    template.type_id = "legacy_template"
    template.tenant_id = None
    template.disabled = False
    template.updated_at = None
    template.input_schema = None  # 无 schema

    mock_db.query.return_value.filter.return_value.first.return_value = template

    with patch("app.services.generation_service.TaskRepository") as MockRepo:
        mock_repo = MockRepo.return_value
        mock_repo.get_by_request_hash.return_value = None
        mock_task = MagicMock()
        mock_task.id = "task-456"
        mock_task.status = "pending"
        mock_repo.create.return_value = mock_task

        service = GenerationService(mock_db)
        req = GenerateRequest(
            template_id="legacy_template",
            params={"任意参数": "任意值"},  # 无 schema 约束，任意参数都应通过
            quantity=5,
        )

        with patch(
            "app.services.generation_service.relay_pending", return_value={"scanned": 0}
        ) as relay:
            response = service.create_task(req, mock_user, mock_celery)
            relay.assert_called_once()

        assert response.task_id == "task-456"
        mock_celery.send_task.assert_not_called()  # No unsafe direct-send fallback.
