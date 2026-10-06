from types import SimpleNamespace

from test_p0_quality_events import seed

from app.api.routes import dashboard
from app.models import QualityEvaluation
from app.workflow import graph as wf


def test_dashboard_no_quality_events_is_not_zero_or_perfect_pass_rate(db):
    result = dashboard(db=db, current_user=SimpleNamespace(role="admin"))
    kpis = {k.key: k for k in result.kpis}
    assert kpis["qc_pass_rate"].value is None
    assert kpis["qc_first_pass_rate"].value is None
    assert result.quality_pipeline["threads"] == 0


def test_dashboard_rejections_not_hidden_and_manual_records_do_not_inflate_denominator(
    db, monkeypatch
):
    state = seed(db)
    monkeypatch.setattr(wf, "get_effective_weights", lambda *args, **kwargs: (None, 80))
    monkeypatch.setattr(wf, "run_quality_check", lambda *args, **kwargs: (75, {"a": 75}))
    state.update(wf.qc_node(state, db))
    wf.reject_node(state, db)
    result = dashboard(db=db, current_user=SimpleNamespace(role="admin"))
    assert {k.key: k.value for k in result.kpis}["qc_pass_rate"] == 0
    assert result.quality_pipeline["finalized_threads"] == 1
    assert result.by_template[0]["pass_rate"] == 0
    assert db.query(QualityEvaluation).one().threshold == 80
