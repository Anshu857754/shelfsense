"""A restart must not lose audits that were running."""

import time

import pytest
from fastapi.testclient import TestClient

from app import db, vision
from app.main import app
from factories import shelf_image
from test_scenarios import GROCERY, stocked_grocery_reply


def interrupted_audit(**fields) -> str:
    """An audit as a stopped server leaves it: saved photo and planogram, status 'processing'."""
    database = db.get_db()
    audit_id = database.create_audit(
        category="grocery", category_label="Grocery and snacks", category_name="", planogram=GROCERY,
        image=shelf_image(), mime="image/jpeg", width=900, height=600, warnings=[],
    )
    if fields:
        database.update_audit(audit_id, **fields)
    return audit_id


def wait_until_settled(audit_id: str, seconds: float = 8.0) -> dict:
    deadline = time.time() + seconds
    while time.time() < deadline:
        audit = db.get_db().get_audit(audit_id)
        if audit["status"] != "processing":
            return audit
        time.sleep(0.05)
    raise AssertionError("the audit was still processing")


def test_an_audit_interrupted_by_a_restart_finishes_after_the_restart(model):
    audit_id = interrupted_audit(stage="detecting")
    model.queue(stocked_grocery_reply())

    with TestClient(app):  # starting the app runs the start-up recovery
        audit = wait_until_settled(audit_id)

    assert audit["status"] == "completed" and audit["score"] == 100.0 and audit["error"] is None
    assert audit["attempts"] == 1 and len(model.calls) == 1


def test_resuming_does_not_use_up_the_daily_allowance(model, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "max_audits_per_day", 1)
    audit_id = interrupted_audit()
    model.queue(stocked_grocery_reply())
    with TestClient(app):
        wait_until_settled(audit_id)
    assert db.get_db().runs_today() == 0  # the original request already counted


def test_an_audit_that_is_too_old_is_not_resumed(model):
    audit_id = interrupted_audit(created_at="2020-01-01T00:00:00+00:00")
    with TestClient(app):
        audit = wait_until_settled(audit_id)
    assert audit["status"] == "failed" and "too old" in audit["error"] and model.calls == []


def test_an_audit_that_keeps_getting_interrupted_is_eventually_stopped(model):
    audit_id = interrupted_audit(attempts=3)
    with TestClient(app):
        audit = wait_until_settled(audit_id)
    assert audit["status"] == "failed" and "kept being interrupted" in audit["error"] and model.calls == []


def test_each_restart_counts_one_attempt_until_the_limit(model):
    audit_id = interrupted_audit()
    # The AI service is down during every start, so the audit keeps failing and being retried by hand.
    for expected_attempts in (1, 2):
        model.queue(vision.OpenRouterError("down"))
        with TestClient(app):
            audit = wait_until_settled(audit_id)
        assert audit["status"] == "failed"
        db.get_db().update_audit(audit_id, status="processing", stage="queued")  # as if it was interrupted again
        assert db.get_db().get_audit(audit_id)["attempts"] == expected_attempts


def test_a_person_asking_to_retry_starts_with_a_clean_slate(client, model):
    audit_id = interrupted_audit(attempts=3, status="failed", stage="done", error="x")
    model.queue(stocked_grocery_reply())
    assert client.post(f"/audits/{audit_id}/retry", follow_redirects=False).status_code == 303
    audit = db.get_db().get_audit(audit_id)
    assert audit["status"] == "completed" and audit["attempts"] == 1  # reset to 0, then this run counted


def test_finished_audits_are_untouched_by_a_restart(model):
    audit_id = interrupted_audit(status="completed", stage="done", score=88.0)
    with TestClient(app):
        pass
    assert db.get_db().get_audit(audit_id)["score"] == 88.0 and model.calls == []
