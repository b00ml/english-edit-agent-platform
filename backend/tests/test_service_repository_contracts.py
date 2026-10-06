"""分层 Service/Repository 的无外部服务契约测试。

这些测试使用项目共享的 SQLite session，重点覆盖查询组合、状态转换和探针错误语义。
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.domain.status import CONTENT_PASSED, CONTENT_PENDING_QC
from app.errors import ContentStateConflictError
from app.health import (
    _probe,
    check_alembic,
    check_checkpointer,
    check_database,
    check_redis,
    dependency_report,
    readiness_report,
)
from app.models import ContentItem, User
from app.repositories.base import BaseRepository
from app.repositories.content_repository import ContentRepository
from app.repositories.knowledge_repository import KnowledgeRepository
from app.repositories.quality_repository import (
    QualityCalibrationRepository,
    QualityRecordRepository,
)
from app.repositories.sample_repository import SampleRepository
from app.repositories.task_repository import TaskRepository
from app.repositories.trace_repository import TraceRepository
from app.repositories.user_repository import UserRepository
from app.schemas import UserCreateIn, UserUpdateIn
from app.services.auth_service import AuthService
from app.services.content_service import ContentService


class _CommitDB:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1

    def refresh(self, instance):
        return instance


def _user(
    user_id: str = "user-1",
    username: str = "reviewer",
    role: str = "reviewer",
    status: str = "active",
    tenant_id: str = "tenant-1",
) -> User:
    now = datetime.now(timezone.utc)
    return User(
        id=user_id,
        username=username,
        password_hash="hashed",
        display_name="Reviewer",
        role=role,
        status=status,
        tenant_id=tenant_id,
        created_at=now,
        updated_at=now,
    )


def test_auth_service_contracts(monkeypatch):
    db = _CommitDB()
    service = AuthService(db)
    active = _user()

    class Users:
        duplicate = False

        def get_by_username(self, username):
            return active if username == active.username else None

        def exists_username(self, username):
            return self.duplicate

        def create(self, **kwargs):
            return _user(username=kwargs["username"], role=kwargs["role"])

        def get_by_id(self, user_id):
            return active if user_id == active.id else None

        def update(self, instance, **kwargs):
            for key, value in kwargs.items():
                setattr(instance, key, value)

        def list_by_tenant(self, tenant_id, skip, limit):
            return [active]

        def count_by_tenant(self, tenant_id):
            return 1

        def list_all(self, skip, limit):
            return [active]

        def count(self):
            return 1

    service.user_repo = Users()
    monkeypatch.setattr(
        "app.services.auth_service.verify_password", lambda password, hashed: password == "ok"
    )
    monkeypatch.setattr("app.services.auth_service.create_access_token", lambda user: "jwt")
    monkeypatch.setattr("app.services.auth_service.hash_password", lambda password: "new-hash")

    assert service.login("reviewer", "ok").access_token == "jwt"
    with pytest.raises(HTTPException, match="用户名或密码错误"):
        service.login("reviewer", "bad")
    active.status = "disabled"
    with pytest.raises(HTTPException, match="账号已被禁用"):
        service.login("reviewer", "ok")
    active.status = "active"

    created = service.create_user(
        UserCreateIn(username="new-user", password="secret1", role="viewer"),
        operator_id=active.id,
        tenant_id="tenant-2",
    )
    assert created.username == "new-user"
    service.user_repo.duplicate = True
    with pytest.raises(HTTPException, match="用户名已存在"):
        service.create_user(UserCreateIn(username="new-user", password="secret1"), active.id)
    service.user_repo.duplicate = False

    updated = service.update_user(active.id, UserUpdateIn(display_name="New", password="secret2"))
    assert updated.display_name == "New"
    with pytest.raises(HTTPException, match="用户不存在"):
        service.update_user("missing", UserUpdateIn(display_name="x"))
    assert service.get_user(active.id) is active
    assert service.list_users("tenant-1")[1] == 1
    assert service.list_users()[1] == 1
    assert service.disable_user(active.id).status == "disabled"
    assert service.enable_user(active.id).status == "active"
    with pytest.raises(HTTPException, match="用户不存在"):
        service.disable_user("missing")
    with pytest.raises(HTTPException, match="用户不存在"):
        service.enable_user("missing")


def test_content_service_contracts(db):
    from app.errors import TenantScopeDeniedError
    from app.models import GenerationTask, QuestionTemplate

    template = QuestionTemplate(
        type_id="fixture",
        name="Fixture",
        version=1,
        input_schema={},
        output_schema={},
        quality_rules=[],
        gen_prompt={},
        run_config={},
    )
    db.add(template)
    db.flush()
    task = GenerationTask(
        template_id=template.type_id,
        quantity=1,
        params={},
        status="succeeded",
        tenant_id="tenant-1",
    )
    db.add(task)
    db.flush()
    item = ContentItem(
        task_id=task.id,
        template_id=template.type_id,
        payload={},
        status=CONTENT_PENDING_QC,
        tenant_id="tenant-1",
    )
    db.add(item)
    db.commit()
    service = ContentService(db)
    viewer = _user(role="viewer")
    researcher = _user(role="researcher")
    with pytest.raises(HTTPException):
        service.get_content(item.id, viewer)
    assert service.get_content(item.id, researcher).id == item.id
    with pytest.raises(TenantScopeDeniedError):
        service.get_content(item.id, _user(tenant_id="other"))
    with pytest.raises(HTTPException):
        service.get_content("missing", researcher)
    assert service.list_contents(researcher).total == 1
    with pytest.raises(ContentStateConflictError):
        service.publish_content(item.id, researcher)
    service.update_content_status(item.id, CONTENT_PASSED)
    assert service.publish_content(item.id, researcher).status == "published"
    assert service.get_content(item.id, viewer).id == item.id
    assert service.count_by_task(task.id) == 1


def test_repository_query_contracts(db):
    """每个 Repository 的过滤、分页和统计入口都应可执行。"""
    content = ContentRepository(db)
    content.list_by_task("missing")
    content.list_by_status("passed")
    content.list_by_tenant("tenant")
    content.list_published_by_tenant("tenant")
    content.count_by_status("passed")
    content.count_by_status("passed", "tenant")
    content.count_by_tenant("tenant")
    content.count_by_tenant_and_status("tenant", "passed")
    content.list_by_template("single_choice")
    content.count_status_summary("tenant")

    tasks = TaskRepository(db)
    tasks.get_by_request_hash("missing")
    tasks.list_by_user("missing")
    tasks.list_by_tenant("tenant")
    tasks.list_by_status("pending")
    tasks.count_by_user("missing")
    tasks.count_by_tenant("tenant")
    tasks.get_recent_running()
    tasks.count_by_status("tenant")

    users = UserRepository(db)
    users.get_by_username("missing")
    users.list_by_role("viewer")
    users.list_by_tenant("tenant")
    users.count_by_tenant("tenant")
    users.exists_username("missing")

    knowledge = KnowledgeRepository(db)
    knowledge.list_by_source_type("教材")
    knowledge.list_by_knowledge_point("时态")
    knowledge.list_by_tenant("tenant")
    knowledge.count_by_source_type("教材")
    knowledge.count_by_tenant("tenant")

    quality = QualityRecordRepository(db)
    quality.get_by_item("missing")
    quality.list_by_source("auto")
    quality.list_by_tenant("tenant")
    quality.list_manual_rejections()
    quality.count_by_source("auto")

    calibrations = QualityCalibrationRepository(db)
    calibrations.get_latest_by_template("single_choice")
    calibrations.list_by_template("single_choice")
    calibrations.list_by_tenant("tenant")
    calibrations.count_by_template("single_choice")

    samples = SampleRepository(db)
    samples.get_by_item("missing")
    samples.list_by_template("single_choice")
    samples.list_by_purpose("fewshot")
    samples.list_by_source("manual")
    samples.list_by_knowledge_point("时态")
    samples.list_by_tenant("tenant")
    samples.list_fewshot_samples("single_choice")
    samples.list_fewshot_samples("single_choice", "时态")
    samples.count_by_template("single_choice")
    samples.count_by_purpose("fewshot")
    samples.count_by_tenant("tenant")

    traces = TraceRepository(db)
    traces.get_by_trace_id("missing")
    traces.list_by_task("missing")
    traces.list_by_item("missing")
    traces.list_by_template("single_choice")
    traces.list_by_stage("generate")
    traces.list_by_tenant("tenant")
    traces.get_cost_by_task("missing")
    now = datetime.now(timezone.utc)
    traces.get_cost_by_template("single_choice", now, now)
    traces.get_cost_by_stage("missing", "generate")
    traces.get_success_rate_by_template("single_choice")
    traces.count_by_tenant("tenant")
    traces.sum_cost("tenant")
    traces.sum_cost("tenant", now)
    traces.aggregate_by_model("tenant")
    traces.aggregate_by_model("tenant", now)
    traces.aggregate_by_date("tenant")
    traces.aggregate_by_date("tenant", now)
    traces.structured_output_stats("tenant")


def test_base_repository_crud_contract(db):
    repo = BaseRepository(User, db)
    assert repo.get_by_id("missing") is None
    assert repo.list_all() == []
    assert repo.count() == 0
    user = repo.create(
        username="repo-user",
        password_hash="hash",
        display_name="Repo User",
        role="viewer",
        status="active",
    )
    repo.update(user, display_name="Updated", ignored="value")
    repo.commit()
    repo.refresh(user)
    assert user.display_name == "Updated"
    repo.delete(user)
    repo.commit()
    assert repo.count() == 0


def test_health_probe_reports_success_and_failure():
    assert _probe("ok", lambda: "ready")["status"] == "healthy"
    failed = _probe("broken", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert failed["status"] == "unavailable"
    assert failed["detail"] == "RuntimeError"


def test_health_database_and_alembic_probes(monkeypatch):
    class Row:
        def __getitem__(self, index):
            return "v1"

    class Session:
        def execute(self, statement):
            return SimpleNamespace(first=lambda: Row())

        def close(self):
            pass

    monkeypatch.setattr("app.health.SessionLocal", lambda: Session())
    assert check_database()["status"] == "healthy"
    monkeypatch.setattr("app.main._verify_migrations", lambda session: "verified-head")
    assert check_alembic()["status"] == "healthy"


def test_health_redis_and_checkpointer_branches(monkeypatch):
    class Redis:
        def ping(self):
            return True

        def close(self):
            pass

    monkeypatch.setattr("redis.from_url", lambda *args, **kwargs: Redis())
    assert check_redis()["status"] == "healthy"

    from app.config import settings

    monkeypatch.setattr(settings, "CHECKPOINTER_BACKEND", "memory")
    assert check_checkpointer()["status"] == "degraded"
    monkeypatch.setattr(settings, "CHECKPOINTER_BACKEND", "postgres")
    monkeypatch.setattr("app.workflow.graph._get_checkpointer", lambda: object())
    assert check_checkpointer()["status"] == "healthy"


def test_health_report_and_readiness_failure(monkeypatch):
    checks = [
        {"name": "database", "status": "unavailable", "latency_ms": 1, "detail": "down"},
        {"name": "alembic", "status": "healthy", "latency_ms": 1, "detail": "v1"},
        {"name": "redis", "status": "degraded", "latency_ms": 1, "detail": "slow"},
        {"name": "checkpointer", "status": "unavailable", "latency_ms": 1, "detail": "down"},
    ]
    monkeypatch.setattr("app.health.check_database", lambda: checks[0])
    monkeypatch.setattr("app.health.check_alembic", lambda: checks[1])
    monkeypatch.setattr("app.health.check_redis", lambda: checks[2])
    monkeypatch.setattr("app.health.check_checkpointer", lambda: checks[3])
    report = dependency_report()
    assert report["status"] == "unavailable"
    ready = readiness_report()
    assert ready["ready"] is False
    assert set(ready["blocking"]) == {"database", "redis", "checkpointer"}
