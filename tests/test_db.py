"""Database: persistence, search/filter, statistics, tasks."""

from app.db import Database
from app.schemas import Task

PLANOGRAM = {"store": "S1", "shelf": "Snacks-A", "rows": [{"row": 1, "expected": [{"product": "Chips", "facings": 2}]}]}


def make(db, category="grocery", label="Grocery and snacks", planogram=None, name=""):
    return db.create_audit(
        category=category, category_label=label, category_name=name,
        planogram=planogram or PLANOGRAM, image=b"jpegbytes", mime="image/jpeg",
        width=800, height=600, warnings=["a warning"],
    )


def test_create_and_get_roundtrip(tmp_path):
    db = Database(tmp_path)
    audit_id = make(db)
    audit = db.get_audit(audit_id)
    assert audit_id.startswith("SS-") and len(audit_id) == 11
    assert audit["status"] == "processing" and audit["stage"] == "queued"
    assert audit["planogram_json"] == PLANOGRAM
    assert audit["warnings_json"] == ["a warning"]
    assert db.read_image(audit_id) == b"jpegbytes"
    assert db.get_audit("SS-NOPE0000") is None


def test_audits_survive_reopening_the_database(tmp_path):
    first = Database(tmp_path)
    audit_id = make(first)
    first.update_audit(audit_id, status="completed", score=91.5, result_json={"score": 91.5})
    first.replace_tasks(audit_id, [Task(kind="restock", priority="high", text="Restock 1 x Chips in row 1")])

    reopened = Database(tmp_path)  # what a server restart does
    audit = reopened.get_audit(audit_id)
    assert audit["status"] == "completed" and audit["score"] == 91.5
    assert audit["result_json"] == {"score": 91.5}
    assert [t["text"] for t in reopened.get_tasks(audit_id)] == ["Restock 1 x Chips in row 1"]
    assert reopened.read_image(audit_id) == b"jpegbytes"


def test_stats_count_each_group(tmp_path):
    db = Database(tmp_path)
    done_good, done_low, review, failed, busy = (make(db) for _ in range(5))
    db.update_audit(done_good, status="completed", score=95.0)
    db.update_audit(done_low, status="completed", score=60.0)
    db.update_audit(review, status="needs_review")
    db.update_audit(failed, status="failed")
    stats = db.stats()
    assert stats == {"total": 5, "completed": 2, "processing": 1, "attention": 3}
    assert Database(tmp_path / "empty").stats() == {"total": 0, "completed": 0, "processing": 0, "attention": 0}


def test_list_search_and_filters(tmp_path):
    db = Database(tmp_path)
    a = make(db, "grocery", "Grocery and snacks")
    b = make(db, "clothing", "Clothing and fashion", {**PLANOGRAM, "shelf": "Denim wall"})
    c = make(db, "custom", "Pet supplies", name="Pet supplies")
    db.update_audit(a, status="completed", score=90.0)
    db.update_audit(b, status="completed", score=50.0)
    db.update_audit(c, status="failed")

    ids = lambda **kw: {r["id"] for r in db.list_audits(**kw)[0]}  # noqa: E731
    assert ids() == {a, b, c}
    assert ids(q="denim") == {b}
    assert ids(q="FASHION") == {b}  # case-insensitive, matches the category label
    assert ids(q=a.lower()[:6]) == {a}
    assert ids(q="pet") == {c}
    assert ids(status="completed") == {a, b}
    assert ids(status="failed") == {c}
    assert ids(status="attention") == {b, c}
    assert ids(category="clothing") == {b}
    assert ids(q="snacks", status="completed") == {a}
    assert ids(date_from="2999-01-01") == set()
    assert ids(date_to="2000-01-01") == set()


def test_list_is_newest_first_and_paginated(tmp_path):
    db = Database(tmp_path)
    made = [make(db) for _ in range(5)]
    rows, total = db.list_audits(limit=2, offset=0)
    assert total == 5 and [r["id"] for r in rows] == [made[4], made[3]]
    rows, _ = db.list_audits(limit=2, offset=4)
    assert [r["id"] for r in rows] == [made[0]]


def test_tasks_keep_their_done_state_when_the_task_list_is_rebuilt(tmp_path):
    db = Database(tmp_path)
    audit_id = make(db)
    one = Task(kind="restock", priority="high", text="Restock 2 x Chips in row 1")
    two = Task(kind="move", priority="medium", text="Move 1 x Chips from row 2 to row 1")
    db.replace_tasks(audit_id, [one, two])
    first_id = db.get_tasks(audit_id)[0]["id"]
    assert db.set_task_done(audit_id, first_id, True) == {"total": 2, "done": 1}

    three = Task(kind="remove", priority="low", text="Remove 'X' from row 1: it is not in the planogram")
    db.replace_tasks(audit_id, [one, three])  # `two` disappeared, `three` is new
    tasks = {t["text"]: t["done"] for t in db.get_tasks(audit_id)}
    assert tasks == {one.text: True, three.text: False}
    assert db.get_tasks(audit_id)[0]["id"] == first_id  # unchanged task keeps its id


def test_tasks_are_listed_most_urgent_first(tmp_path):
    db = Database(tmp_path)
    audit_id = make(db)
    db.replace_tasks(audit_id, [
        Task(kind="remove", priority="low", text="low one"),
        Task(kind="restock", priority="high", text="high one"),
        Task(kind="move", priority="medium", text="medium one"),
    ])
    assert [t["priority"] for t in db.get_tasks(audit_id)] == ["high", "medium", "low"]


def test_task_updates_are_scoped_to_their_audit(tmp_path):
    db = Database(tmp_path)
    a, b = make(db), make(db)
    db.replace_tasks(a, [Task(kind="restock", priority="low", text="t")])
    task_id = db.get_tasks(a)[0]["id"]
    assert db.set_task_done(b, task_id, True) is None
    assert db.set_task_done(a, task_id, True) == {"total": 1, "done": 1}
    assert db.set_task_done(a, task_id, False) == {"total": 1, "done": 0}


def test_recent_interrupted_audits_are_queued_to_run_again(tmp_path):
    db = Database(tmp_path)
    stuck, finished = make(db), make(db)
    db.update_audit(stuck, stage="detecting")
    db.update_audit(finished, status="completed", score=100.0)
    resume, gave_up = db.recover_interrupted()
    assert resume == [stuck] and gave_up == 0
    assert db.get_audit(stuck)["status"] == "processing" and db.get_audit(stuck)["stage"] == "queued"
    assert db.get_audit(finished)["status"] == "completed"  # finished audits are left alone


def test_old_or_repeatedly_interrupted_audits_are_given_up_on(tmp_path):
    db = Database(tmp_path)
    old, looping, fresh = make(db), make(db), make(db)
    db.update_audit(old, created_at="2020-01-01T00:00:00+00:00")
    db.update_audit(looping, attempts=3)
    resume, gave_up = db.recover_interrupted()
    assert resume == [fresh] and gave_up == 2
    assert "too old" in db.get_audit(old)["error"] and db.get_audit(old)["status"] == "failed"
    assert "kept being interrupted" in db.get_audit(looping)["error"]


def test_chart_queries(tmp_path):
    db = Database(tmp_path)
    for cat, label, score in [("grocery", "Grocery and snacks", 80.0), ("grocery", "Grocery and snacks", 100.0), ("clothing", "Clothing and fashion", 50.0)]:
        audit_id = make(db, cat, label)
        db.update_audit(audit_id, status="completed", score=score)
    averages = {c["category"]: (c["avg_score"], c["audits"]) for c in db.category_averages()}
    assert averages == {"grocery": (90.0, 2), "clothing": (50.0, 1)}
    assert [r["score"] for r in db.recent_scores()] == [80.0, 100.0, 50.0]  # oldest first
    assert db.status_counts() == {"completed": 3}
