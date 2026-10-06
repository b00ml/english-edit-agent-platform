import pytest
from test_p0_quality_events import seed

from app.models import QualityEvaluation, QualityRecord
from app.workflow import graph as wf


@pytest.mark.parametrize(
    "threshold,score,gray,expected",
    [
        (80, 75, False, "reject"),
        (80, 75, True, "submit_review"),
        (60, 65, False, "store"),
        (0, 0, False, "store"),
    ],
)
def test_effective_threshold_controls_route_snapshot_and_event(
    db, monkeypatch, threshold, score, gray, expected
):
    state = seed(db)
    state.update(max_revise=0, human_review_config={"enabled": gray, "gray_margin": 10})
    monkeypatch.setattr(
        wf, "get_effective_weights", lambda *args, **kwargs: ({"a": 1.0}, threshold)
    )
    monkeypatch.setattr(wf, "run_quality_check", lambda *args, **kwargs: (score, {"a": score}))
    state.update(wf.qc_node(state, db))
    assert state["quality_threshold"] == threshold
    assert state["quality_config_snapshot"]["threshold"] == threshold
    assert db.query(QualityEvaluation).one().threshold == threshold
    assert wf.after_qc(state) == expected
    # Replay must reuse the frozen threshold even if the calibration changes.
    monkeypatch.setattr(wf, "get_effective_weights", lambda *args, **kwargs: (None, 95))
    assert wf.qc_node(state, db)["quality_threshold"] == threshold
    if expected == "store":
        wf.store_node(state, db)
    elif expected == "reject":
        wf.reject_node(state, db)
    else:
        monkeypatch.setattr(wf, "notify_human_review", lambda *args: None)
        wf.submit_review_node(state, db)
    assert db.query(QualityRecord).one().config_snapshot["threshold"] == threshold


def test_manual_review_preserves_auto_effective_snapshot(db, monkeypatch):
    from types import SimpleNamespace

    from app.models import ContentItem
    from app.schemas import QualityReviewRequest
    from app.services.quality_service import QualityService

    state = seed(db)
    monkeypatch.setattr(wf, "get_effective_weights", lambda *args, **kwargs: ({"a": 1}, 60))
    monkeypatch.setattr(wf, "run_quality_check", lambda *args, **kwargs: (65, {"a": 65}))
    state.update(wf.qc_node(state, db))
    wf.store_node(state, db)
    item = db.query(ContentItem).one()
    record = QualityService(db).review_content(
        item.id,
        QualityReviewRequest(**{"pass": True, "reason": ""}),
        SimpleNamespace(id="human", tenant_id=None, role="reviewer"),
    )
    assert record.config_snapshot["threshold"] == 60
    assert record.config_snapshot["weights"] == {"a": 1}
