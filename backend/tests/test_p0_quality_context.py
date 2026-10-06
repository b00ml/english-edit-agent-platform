from types import SimpleNamespace
from unittest.mock import Mock

from app.engine.quality import _build_judge_prompt
from app.workflow import graph as wf


def test_judge_receives_only_declared_generation_constraints():
    template = SimpleNamespace(
        quality_rules=[{"id": "match", "weight": 1.0}],
        input_schema={"properties": {"knowledge_point": {}, "difficulty": {}, "topic": {}}},
    )
    _, user = _build_judge_prompt(
        template,
        {"stem": "A question"},
        {
            "knowledge_point": "过去完成时",
            "difficulty": "难",
            "topic": "travel",
            "rag_context": "secret-rag",
            "api_key": "secret-key",
            "revise_context": "old-draft",
        },
    )
    assert "过去完成时" in user and "难" in user and "travel" in user
    assert "A question" in user
    assert "secret-rag" not in user and "secret-key" not in user and "old-draft" not in user
    assert "{{" not in user


def test_qc_node_passes_original_params(monkeypatch):
    template = SimpleNamespace(type_id="example", run_config={}, version=1, output_schema={})
    captured = {}
    monkeypatch.setattr(wf, "_load_template", lambda *args: template)
    monkeypatch.setattr(wf, "get_effective_weights", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(wf, "quality_snapshot", lambda *args, **kwargs: kwargs)

    def check(*args, **kwargs):
        captured.update(kwargs)
        return 80.0, {"match": 80.0}

    monkeypatch.setattr(wf, "run_quality_check", check)
    params = {"template_id": "example", "knowledge_point": "一般现在时", "difficulty": "易"}
    session = Mock()
    session.query.return_value.filter.return_value.first.return_value = None
    wf.qc_node({"params": params, "trace_id": "t:0", "draft": {"stem": "x"}}, session)
    assert captured["generation_params"] == params
