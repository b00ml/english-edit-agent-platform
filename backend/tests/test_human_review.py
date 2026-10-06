# tests/test_human_review.py —— 灰区人工卡点（LangGraph interrupt）状态机测试（P1-1 / OPT-023）
# 以 MemorySaver + mock LLM 节点跑真实图拓扑，覆盖：灰区中断、恢复通过/驳回、
# 高分直入库、低于灰区走改版、开关关闭保持旧行为。
from types import SimpleNamespace

import pytest

from app.config import settings
from app.models import ContentItem, QualityRecord
from app.workflow import graph as wf
from app.workflow.graph import after_qc, reset_checkpointer, resume_human_review, run_generation


class _FakeQuery:
    def __init__(self, session, model):
        self._session = session
        self._model = model
        self._filters = []

    def filter(self, *args, **kwargs):
        self._filters.extend(args)
        return self

    def first(self):
        for obj in self._session.added:
            if isinstance(obj, self._model) and all(
                getattr(obj, expr.left.key) == expr.right.value for expr in self._filters
            ):
                return obj
        return None


class _FakeSession:
    """最小 DB 会话替身：ContentItem.add 后 flush 分配 id，query 返回首个匹配模型对象。"""

    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        n = sum(1 for o in self.added if isinstance(o, ContentItem))
        for o in self.added:
            if isinstance(o, ContentItem) and o.id is None:
                o.id = f"item-{n}"
        # QualityRecord.item_id 若为空则挂到首条 item（单条目测试足够）
        items = [o for o in self.added if isinstance(o, ContentItem)]
        for o in self.added:
            if isinstance(o, QualityRecord) and o.item_id is None and items:
                o.item_id = items[0].id

    def commit(self):
        self.commits += 1

    def query(self, model):
        return _FakeQuery(self, model)


def _template(human_review=None, max_revise=3):
    # 阈值 50.0 刻意不等于全局默认 QUALITY_THRESHOLD=70.0：
    # 若 GenState 键传播失效（回退默认 70），灰区 [40,50) 的用例会走改版路径而 loudly 失败
    return SimpleNamespace(
        type_id="single_choice",
        run_config={
            "max_revise": max_revise,
            "quality_threshold": 50.0,
            "human_review": human_review or {},
        },
        output_schema={"required": ["stem"]},
        gen_prompt={"system": "single_choice-system.st", "user": "single_choice-user.st"},
    )


@pytest.fixture(autouse=True)
def _memory_checkpointer(monkeypatch):
    monkeypatch.setattr(settings, "CHECKPOINTER_BACKEND", "memory")
    reset_checkpointer()
    yield
    reset_checkpointer()


@pytest.fixture()
def _wired(monkeypatch):
    """mock 掉图的 DB/LLM 依赖，脚本化 generate 与 qc 分数。"""
    session = _FakeSession()
    calls = {"generate": 0, "quality": [], "notified": []}

    monkeypatch.setattr(wf, "_load_template", lambda s, tid: _template())
    monkeypatch.setattr(wf, "get_effective_weights", lambda s, tid, **kwargs: (None, None))
    monkeypatch.setattr(wf, "build_rag_context", lambda *a, **kw: "")

    def _fake_generate(template, params, session, profile_name=None, **kw):
        calls["generate"] += 1
        return {"stem": f"题目{calls['generate']}"}

    monkeypatch.setattr(wf, "generate_with_fallback", _fake_generate)

    def _fake_qc(template, payload, model_name="", **kw):
        return calls["quality"].pop(0), {"kp_match": 80.0}

    monkeypatch.setattr(wf, "run_quality_check", _fake_qc)

    monkeypatch.setattr(
        wf,
        "notify_human_review",
        lambda db, item, score=0.0: calls["notified"].append(("review", item.id)),
    )
    monkeypatch.setattr(
        wf,
        "notify_content_rejected",
        lambda db, item, reason="": calls["notified"].append(("rejected", item.id)),
    )
    return session, calls


def _run(session, thread_id="task-x:0"):
    return run_generation(
        task_id="task-x",
        template_id="single_choice",
        params={"knowledge_point": "一般现在时"},
        session=session,
        thread_id=thread_id,
    )


def _items(session):
    return [o for o in session.added if isinstance(o, ContentItem)]


def _records(session):
    return [o for o in session.added if isinstance(o, QualityRecord)]


class TestGrayZoneHumanReview:
    def test_gray_zone_interrupts_and_stores_awaiting_review(self, _wired, monkeypatch):
        session, calls = _wired
        monkeypatch.setattr(
            wf,
            "_load_template",
            lambda s, tid: _template({"enabled": True, "gray_margin": 10}),
        )
        calls["quality"].append(45.0)  # 灰区：[40, 50)

        result = _run(session)

        assert "__interrupt__" in result
        items = _items(session)
        assert len(items) == 1
        assert items[0].status == "awaiting_review"
        assert items[0].thread_id == "task-x:0"
        assert items[0].qc_score == 45.0
        # 落库时同时写 auto 质检记录并通知
        assert any(r.source == "auto" for r in _records(session))
        assert ("review", items[0].id) in calls["notified"]
        # interrupt 载荷携带质检分
        interrupt_payload = result["__interrupt__"][0].value
        assert interrupt_payload["qc_score"] == 45.0

    def test_resume_approved(self, _wired, monkeypatch):
        session, calls = _wired
        monkeypatch.setattr(
            wf,
            "_load_template",
            lambda s, tid: _template({"enabled": True, "gray_margin": 10}),
        )
        calls["quality"].append(45.0)
        result = _run(session)
        content_id = result["content_id"]

        resumed = resume_human_review(
            "task-x:0", {"approved": True, "score": 100.0, "reason": ""}, session
        )

        assert resumed["status"] == "review_passed"
        item = _items(session)[0]
        assert item.status == "passed" and item.qc_score == 100.0
        manual = [r for r in _records(session) if r.source == "manual_review"]
        assert len(manual) == 1 and manual[0].score == 100.0
        assert ("rejected", content_id) not in calls["notified"]

    def test_resume_rejected(self, _wired, monkeypatch):
        session, calls = _wired
        monkeypatch.setattr(
            wf,
            "_load_template",
            lambda s, tid: _template({"enabled": True, "gray_margin": 10}),
        )
        calls["quality"].append(45.0)
        result = _run(session)
        content_id = result["content_id"]

        resumed = resume_human_review(
            "task-x:0", {"approved": False, "score": 0.0, "reason": "题目不严谨"}, session
        )

        assert resumed["status"] == "review_rejected"
        item = _items(session)[0]
        assert item.status == "rejected"
        manual = [r for r in _records(session) if r.source == "manual_review"]
        assert len(manual) == 1 and manual[0].score == 0.0
        assert ("rejected", content_id) in calls["notified"]

    def test_above_threshold_stores_directly(self, _wired, monkeypatch):
        session, calls = _wired
        monkeypatch.setattr(
            wf,
            "_load_template",
            lambda s, tid: _template({"enabled": True, "gray_margin": 10}),
        )
        calls["quality"].append(60.0)

        result = _run(session)

        assert "__interrupt__" not in result
        assert result["status"] == "stored"
        assert _items(session)[0].status == "pending_qc"
        assert calls["notified"] == []

    def test_below_gray_zone_revises_then_stores(self, _wired, monkeypatch):
        session, calls = _wired
        monkeypatch.setattr(
            wf,
            "_load_template",
            lambda s, tid: _template({"enabled": True, "gray_margin": 10}),
        )
        calls["quality"].extend([35.0, 60.0])

        result = _run(session)

        assert "__interrupt__" not in result
        assert result["status"] == "stored"
        assert calls["generate"] == 2
        assert result["revise_count"] == 1

    def test_disabled_config_keeps_legacy_behavior(self, _wired, monkeypatch):
        session, calls = _wired
        monkeypatch.setattr(
            wf,
            "_load_template",
            lambda s, tid: _template(max_revise=2),  # 非默认 3：验证 max_revise 键传播
        )
        calls["quality"].extend([45.0, 45.0, 45.0])  # 反复不达标

        result = _run(session)

        assert "__interrupt__" not in result
        assert result["status"] == "rejected"
        assert calls["generate"] == 3  # 初始 1 次 + 改版 2 次（上限熔断，max_revise=2）
        assert calls["notified"] == []


class TestAfterQcRouting:
    """边界路由单元测试（threshold=50 刻意≠全局默认 70, gray_margin=10）。"""

    HR = {"enabled": True, "gray_margin": 10}

    def test_pass_to_store(self):
        assert (
            after_qc({"qc_score": 50, "quality_threshold": 50.0, "human_review_config": self.HR})
            == "store"
        )

    def test_gray_zone_upper_bound(self):
        assert (
            after_qc({"qc_score": 49.9, "quality_threshold": 50.0, "human_review_config": self.HR})
            == "submit_review"
        )

    def test_gray_zone_lower_bound_inclusive(self):
        assert (
            after_qc({"qc_score": 40, "quality_threshold": 50.0, "human_review_config": self.HR})
            == "submit_review"
        )

    def test_below_gray_zone_revises(self):
        assert (
            after_qc(
                {
                    "qc_score": 39.9,
                    "quality_threshold": 50.0,
                    "human_review_config": self.HR,
                    "revise_count": 0,
                    "max_revise": 3,
                }
            )
            == "revise"
        )

    def test_disabled_config_skips_review(self):
        assert (
            after_qc(
                {
                    "qc_score": 45,
                    "quality_threshold": 50.0,
                    "human_review_config": {},
                    "revise_count": 0,
                    "max_revise": 3,
                }
            )
            == "revise"
        )

    def test_revise_budget_exhausted_rejects(self):
        assert (
            after_qc(
                {
                    "qc_score": 39.9,
                    "quality_threshold": 50.0,
                    "human_review_config": self.HR,
                    "revise_count": 3,
                    "max_revise": 3,
                }
            )
            == "reject"
        )


# ---------------------------------------------------------------------------
# OPT-023：改版链路双生成 Bug 修复回归 —— revise 节点只准备上下文，不调用生成
# ---------------------------------------------------------------------------
class TestReviseNodeFix:
    def test_revise_node_prepares_context_without_generating(self, monkeypatch):
        def _must_not_call(*args, **kwargs):
            raise AssertionError("revise 节点不应直接调用 generate_with_fallback")

        monkeypatch.setattr(wf, "generate_with_fallback", _must_not_call)
        state = {
            "params": {"template_id": "single_choice"},
            "draft": {"stem": "旧稿"},
            "qc_score": 55.0,
            "dimension_scores": {"kp_match": 60.0},
            "revise_count": 0,
        }
        out = wf.revise_node(state, _FakeSession())
        assert out["revise_count"] == 1
        assert out["status"] == "revised"
        ctx = out["params"]["revise_context"]
        assert ctx["previous_draft"] == {"stem": "旧稿"}
        assert ctx["qc_score"] == 55.0
        assert ctx["dimension_scores"] == {"kp_match": 60.0}

    def test_generate_node_injects_revise_instruction(self, monkeypatch):
        # generate 节点把 revise_context 渲染进 params["revise_instruction"]，由引擎消费
        captured = {}

        def _fake_generate(template, params, session, profile_name=None, **kw):
            captured["params"] = params
            return {"stem": "新稿"}

        monkeypatch.setattr(wf, "generate_with_fallback", _fake_generate)
        monkeypatch.setattr(
            wf,
            "_load_template",
            lambda s, tid: _template({"enabled": False}),
        )
        monkeypatch.setattr(wf, "get_effective_weights", lambda s, tid, **kwargs: (None, None))
        state = {
            "task_id": "t",
            "trace_id": "th",
            "quality_threshold": 50.0,
            "params": {
                "template_id": "single_choice",
                "revise_context": {
                    "previous_draft": {"stem": "旧稿"},
                    "qc_score": 35.0,
                    "dimension_scores": {"kp_match": 60.0},
                },
            },
        }
        out = wf.generate_node(state, _FakeSession())
        assert out["draft"] == {"stem": "新稿"}
        inst = captured["params"]["revise_instruction"]
        assert "35" in inst and "50" in inst and "旧稿" in inst and "改进版" in inst
