# tests/integration/test_pipeline.py —— 端到端集成测试（P1-4 / OPT-025）
# 真 Postgres + FastAPI TestClient + 进程内 Celery 任务 + 脚本化 LLM：
#   1. 生成→质检→入库全链路（高分入库路径，含 TraceLog 埋点与统计接口）；
#   2. 重复提交去重 409；
#   3. 灰区 interrupt 人工卡点：任务 awaiting → review API 恢复图 → 终态。
# 口径：先脚本化 LLM 响应，再提交任务并同步执行（inline_celery）。
import uuid
from pathlib import Path

import pytest
import yaml

from app.database import SessionLocal
from app.models import (
    AppNotification,
    ContentItem,
    GenerationTask,
    QualityRecord,
    QuestionTemplate,
    TraceLog,
)
from app.template_loader import _validate_template
from tests.integration.conftest import GEN_OK, JUDGE_GRAY, JUDGE_HIGH  # noqa: F401

pytestmark = pytest.mark.integration

_PARAMS = {"knowledge_point": "一般现在时", "difficulty": "易", "quantity": 1}


@pytest.fixture(autouse=True)
def indexed_reference_sources(pg_app_sessions):
    # Built-in templates now require sources. Provide test-owned real PG indexes,
    # using the existing no-cloud embedding fixture, not disabling production gates.
    from app.rag.indexer import index_document

    with pg_app_sessions() as session:
        for point, body in [
            ("一般现在时", "A third-person singular subject uses goes. He goes to school daily."),
            ("被动语态", "The passive voice uses be plus a past participle. The door is opened."),
        ]:
            index_document(session, "教材", "Pipeline source " + point, body, knowledge_point=point)


def _submit_and_run(
    client, auth_headers, gen_client, judge_client, template_id="single_choice", params=None
):
    """脚本化一条生成 + 一条质检响应后提交任务并同步执行，返回 task_id。"""
    gen_client._contents.append(GEN_OK)
    judge_client._contents.append(JUDGE_HIGH)
    resp = client.post(
        "/api/generate",
        json={"template_id": template_id, "params": params or _PARAMS, "quantity": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["task_id"]


def _make_hr_template() -> str:
    """插入开启灰区人工卡点的单选模板（配置驱动，不改代码）。"""
    base = yaml.safe_load(
        (Path(__file__).parents[2] / "app" / "templates" / "single_choice.yaml").read_text(
            encoding="utf-8"
        )
    )
    base["type_id"] = f"single_choice_hr_{uuid.uuid4().hex[:16]}"
    base["name"] = "单选（灰区转人工）"
    base["run_config"]["human_review"] = {"enabled": True, "gray_margin": 10}
    assert not _validate_template(base)
    session = SessionLocal()
    try:
        session.add(
            QuestionTemplate(
                type_id=base["type_id"],
                name=base["name"],
                version=1,
                input_schema=base["input_schema"],
                output_schema=base["output_schema"],
                quality_rules=base["quality_rules"],
                gen_prompt={
                    "system": "prompts/single_choice-system.st",
                    "user": "prompts/single_choice-user.st",
                },
                run_config=base["run_config"],
            )
        )
        session.commit()
    finally:
        session.close()

    return base["type_id"]


class TestFullPipeline:
    def test_generate_qc_store(self, client, auth_headers, inline_celery, mock_llm):
        gen_client, judge_client = mock_llm
        task_id = _submit_and_run(client, auth_headers, gen_client, judge_client)

        # 任务同步执行后：状态成功、进度 100%
        resp = client.get(f"/api/tasks/{task_id}", headers=auth_headers)
        assert resp.status_code == 200
        if resp.json()["status"] != "succeeded":
            from app.models import GenerationTaskItem

            with SessionLocal() as debug:
                reasons = [
                    (row.failure_code, row.failure_reason)
                    for row in debug.query(GenerationTaskItem).filter_by(task_id=task_id)
                ]
            pytest.fail(f"Task did not succeed: {reasons}")
        assert resp.json()["progress"] == 1.0

        # 内容条目入库（pending_qc）且绑定 auto 质检记录
        resp = client.get("/api/contents", headers=auth_headers)
        items = [c for c in resp.json()["items"] if c["task_id"] == task_id]
        assert len(items) == 1
        item = items[0]
        assert item["status"] == "pending_qc"
        assert item["payload"]["answer"] == "B"
        assert item["qc_score"] is not None and item["qc_score"] >= 70

        # TraceLog 埋点：生成/质检各一行，attempt=1、success=True
        session = SessionLocal()
        try:
            traces = session.query(TraceLog).filter(TraceLog.task_id == task_id).all()
            assert {"generate", "qc"} <= {t.stage for t in traces}
            assert all(
                t.attempt == 1 and t.success for t in traces if t.stage in {"generate", "qc"}
            )
            records = session.query(QualityRecord).filter(QualityRecord.item_id == item["id"]).all()
            assert len(records) == 1 and records[0].source == "auto"
        finally:
            session.close()

        # 结构化符合率统计接口可用（P0-2）
        resp = client.get("/api/traces/structured-stats", headers=auth_headers)
        assert resp.status_code == 200
        stats = resp.json()
        assert stats["total_generations"] >= 1
        assert stats["first_pass_rate"] == 1.0

    def test_dashboard_exposes_new_kpis(self, client, auth_headers, inline_celery, mock_llm):
        gen_client, judge_client = mock_llm
        _submit_and_run(client, auth_headers, gen_client, judge_client)

        resp = client.get("/api/dashboard", headers=auth_headers)
        assert resp.status_code == 200
        kpi_keys = {k["key"] for k in resp.json()["kpis"]}
        assert {
            "structured_first_pass_rate",
            "structured_avg_attempts",
            "structured_failure_rate",
            "revise_trigger_rate",
        } <= kpi_keys

    def test_dedup_409_on_active_task(self, client, auth_headers, inline_celery, mock_llm):
        gen_client, judge_client = mock_llm
        task_id = _submit_and_run(client, auth_headers, gen_client, judge_client)

        # 任务已完成（不在去重窗口），手工置回 running 模拟执行中
        session = SessionLocal()
        try:
            task = session.query(GenerationTask).filter(GenerationTask.id == task_id).first()
            task.status = "running"
            session.commit()
        finally:
            session.close()

        resp = client.post(
            "/api/generate",
            json={"template_id": "single_choice", "params": _PARAMS, "quantity": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["existing_task_id"] == task_id


class TestGrayZoneHumanReview:
    def test_interrupt_and_resume(self, client, auth_headers, inline_celery, mock_llm):
        """灰区：任务 awaiting → 条目 awaiting_review → review API 恢复图 → 终态。"""
        gen_client, judge_client = mock_llm
        template_id = _make_hr_template()

        gen_client._contents.append(GEN_OK)
        judge_client._contents.append(JUDGE_GRAY)  # 65 分：落在 [60, 70) 灰区

        resp = client.post(
            "/api/generate",
            json={
                "template_id": template_id,
                "params": _PARAMS,
                "quantity": 1,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        task_id = resp.json()["task_id"]

        # Review waiting is active; progress counts finalized items only.
        resp = client.get(f"/api/tasks/{task_id}", headers=auth_headers)
        assert resp.json()["status"] == "awaiting_review"
        assert resp.json()["progress"] == 0.0

        # 条目 awaiting_review，thread_id 已绑定
        session = SessionLocal()
        try:
            item = session.query(ContentItem).filter(ContentItem.task_id == task_id).first()
            assert item is not None
            assert item.status == "awaiting_review"
            assert item.thread_id == f"{task_id}:0"
            item_id = item.id
        finally:
            session.close()

        # 人工通过：review API 凭 thread_id 恢复图
        resp = client.post(
            f"/api/quality/{item_id}/review",
            json={"pass": True, "reason": "", "reference_verified": True},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["source"] == "manual_review"

        session = SessionLocal()
        try:
            item = session.query(ContentItem).filter(ContentItem.id == item_id).first()
            assert item.status == "passed"
            manual = (
                session.query(QualityRecord)
                .filter(QualityRecord.item_id == item_id)
                .filter(QualityRecord.source == "manual_review")
                .all()
            )
            assert len(manual) == 1 and manual[0].score == 100.0
        finally:
            session.close()

    def test_reject_path_notifies(self, client, auth_headers, inline_celery, mock_llm):
        """灰区驳回：终态 rejected + 站内通知。"""
        gen_client, judge_client = mock_llm
        template_id = _make_hr_template()

        gen_client._contents.append(GEN_OK)
        judge_client._contents.append(JUDGE_GRAY)

        resp = client.post(
            "/api/generate",
            json={
                "template_id": template_id,
                "params": {"knowledge_point": "被动语态", "difficulty": "中", "quantity": 1},
                "quantity": 1,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        task_id = resp.json()["task_id"]

        session = SessionLocal()
        try:
            item = session.query(ContentItem).filter(ContentItem.task_id == task_id).first()
            item_id = item.id
        finally:
            session.close()

        resp = client.post(
            f"/api/quality/{item_id}/review",
            json={"pass": False, "reason": "干扰项质量不佳"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        session = SessionLocal()
        try:
            item = session.query(ContentItem).filter(ContentItem.id == item_id).first()
            assert item.status == "rejected"
            notes = (
                session.query(AppNotification).filter(AppNotification.related_id == item_id).all()
            )
            types = {n.type for n in notes}
            assert "content_rejected" in types
        finally:
            session.close()
