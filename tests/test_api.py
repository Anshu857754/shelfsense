"""The stored-audit API: corrections, tasks, statistics, search and filters."""

import json

from app import db
from app.vision import OpenRouterError
from factories import item, reply, shelf_image
from test_scenarios import GROCERY, run, stocked_grocery_reply, submit

CHIPS_SHORT = reply(
    (1, [item("Potato chips", 3, "Chips bag"), item("Cornflakes", 4, "Cereal box")]),
    (2, [item("Cookies", 5, "Biscuit pack"), item("Noodle cups", 2, "Instant noodles")]),
)


def corrections(audit, **overrides):
    """Build a correction request that keeps every item as it is, then apply overrides by name."""
    rows = []
    for r in audit["working_json"]["rows"]:
        for p in r["products"]:
            rows.append({
                "row": r["row"], "name": p["name"], "quantity": p["facings"], "state": "keep",
                "edited": False, "confidence": p["confidence"], "source": p["source"],
                "matched_expected": p["matched_expected"], "category": p["category"],
            })
    for name, changes in overrides.items():
        next(i for i in rows if i["name"] == name).update(changes)
    return {"items": rows, "confirmed_empty": False}


# ---- corrections ---------------------------------------------------------------------


def test_correcting_a_quantity_reruns_the_comparison_without_the_model(client, model):
    model.queue(CHIPS_SHORT)
    audit = run(client)
    assert audit["score"] == 73.7 and len(audit["tasks"]) == 2
    calls_before = len(model.calls)

    body = corrections(audit, **{"Potato chips": {"quantity": 6, "edited": True}, "Noodle cups": {"quantity": 4, "edited": True}})
    response = client.post(f"/api/audits/{audit['id']}/corrections", json=body)

    assert response.json() == {"status": "completed", "score": 100.0, "complete": True}
    fixed = client.get(f"/api/audits/{audit['id']}").json()
    assert fixed["corrected"] is True and fixed["missing_units"] == 0 and fixed["tasks"] == []
    assert fixed["detected_json"] == audit["detected_json"]  # the model's original answer is kept
    assert fixed["working_json"] != audit["detected_json"]
    assert len(model.calls) == calls_before  # no new AI call


def test_accepting_an_uncertain_detection_completes_a_review_audit(client, model):
    model.queue(reply((1, [item("chips?", 6, "Chips bag", 0.2), item("boxes?", 4, "Cereal box", 0.3)]),
                      (2, [item("cookies?", 5, "Biscuit pack", 0.3), item("cups?", 4, "Instant noodles", 0.3)])))
    audit = run(client)
    assert audit["status"] == "needs_review" and audit["score"] is None

    body = corrections(audit)
    for entry in body["items"]:
        entry["state"] = "accept"
    done = client.post(f"/api/audits/{audit['id']}/corrections", json=body).json()
    assert done["status"] == "completed" and done["score"] == 100.0


def test_confirming_an_empty_shelf_records_a_real_zero(client, model):
    model.queue(reply((1, []), (2, [])))
    audit = run(client)
    assert audit["status"] == "needs_review"
    done = client.post(f"/api/audits/{audit['id']}/corrections", json={"items": [], "confirmed_empty": True}).json()
    assert done == {"status": "completed", "score": 0.0, "complete": True}
    final = client.get(f"/api/audits/{audit['id']}").json()
    assert final["missing_units"] == 19 and final["confirmed_empty"] is True


def test_assigning_a_detection_to_a_planogram_product(client, model):
    model.queue(reply((1, [item("mystery snack", 6, None, 0.9), item("Cornflakes", 4, "Cereal box")]),
                      (2, [item("Cookies", 5, "Biscuit pack"), item("Noodle cups", 4, "Instant noodles")])))
    audit = run(client)
    assert audit["unexpected_units"] == 6  # "mystery snack" matches nothing
    body = corrections(audit, **{"mystery snack": {"assigned_to": "Chips bag", "edited": True}})
    client.post(f"/api/audits/{audit['id']}/corrections", json=body)
    fixed = client.get(f"/api/audits/{audit['id']}").json()
    assert fixed["score"] == 100.0 and fixed["unexpected_units"] == 0


def test_dropping_a_wrong_detection(client, model):
    model.queue(reply((1, [item("Potato chips", 6, "Chips bag"), item("Cornflakes", 4, "Cereal box"), item("a shadow", 2, None, 0.8)]),
                      (2, [item("Cookies", 5, "Biscuit pack"), item("Noodle cups", 4, "Instant noodles")])))
    audit = run(client)
    assert audit["unexpected_units"] == 2
    client.post(f"/api/audits/{audit['id']}/corrections", json=corrections(audit, **{"a shadow": {"state": "drop"}}))
    assert client.get(f"/api/audits/{audit['id']}").json()["unexpected_units"] == 0


def test_correction_errors(client, model):
    assert client.post("/api/audits/SS-MISSING0/corrections", json={"items": []}).status_code == 404
    model.queue(stocked_grocery_reply())
    audit = run(client)
    bad = corrections(audit)
    bad["items"][0]["quantity"] = -5
    assert client.post(f"/api/audits/{audit['id']}/corrections", json=bad).status_code == 422
    stuck = db.get_db().create_audit(category="grocery", category_label="x", category_name="", planogram=GROCERY,
                                     image=b"x", mime="image/jpeg", width=1, height=1, warnings=[])
    assert client.post(f"/api/audits/{stuck}/corrections", json={"items": []}).status_code == 409


# ---- tasks ---------------------------------------------------------------------------


def test_task_completion_is_saved_and_survives_corrections(client, model):
    model.queue(CHIPS_SHORT)
    audit = run(client)
    restock_chips = next(t for t in audit["tasks"] if "Chips bag" in t["text"])
    other = next(t for t in audit["tasks"] if "Instant noodles" in t["text"])

    progress = client.post(f"/api/audits/{audit['id']}/tasks/{restock_chips['id']}", json={"done": True}).json()
    assert progress == {"total": 2, "done": 1}
    assert [t["done"] for t in client.get(f"/api/audits/{audit['id']}").json()["tasks"]] == [True, False]  # a refresh

    # Fixing the noodles only: the chips task is unchanged, so it stays ticked; the noodles task disappears.
    client.post(f"/api/audits/{audit['id']}/corrections", json=corrections(audit, **{"Noodle cups": {"quantity": 4, "edited": True}}))
    tasks = client.get(f"/api/audits/{audit['id']}").json()["tasks"]
    assert [(t["text"], t["done"]) for t in tasks] == [(restock_chips["text"], True)]
    assert other["text"] not in [t["text"] for t in tasks]

    assert client.post(f"/api/audits/{audit['id']}/tasks/{restock_chips['id']}", json={"done": False}).json() == {"total": 1, "done": 0}
    assert client.post(f"/api/audits/{audit['id']}/tasks/99999", json={"done": True}).status_code == 404


# ---- statistics, history, search ------------------------------------------------------


def test_stats_and_history_filters(client, model):
    model.queue(stocked_grocery_reply(), CHIPS_SHORT, OpenRouterError("down"))
    good = run(client)  # 100%
    low = run(client, planogram={**GROCERY, "shelf": "Cheap aisle"})  # 73.7%
    failed = run(client, category="beverages")

    assert client.get("/api/stats").json() == {"total": 3, "completed": 2, "processing": 0, "attention": 2}

    listing = lambda **q: {a["id"] for a in client.get("/api/audits", params=q).json()["audits"]}  # noqa: E731
    assert listing() == {good["id"], low["id"], failed["id"]}
    assert listing(status="completed") == {good["id"], low["id"]}
    assert listing(status="attention") == {low["id"], failed["id"]}
    assert listing(status="failed") == {failed["id"]}
    assert listing(category="beverages") == {failed["id"]}
    assert listing(q="cheap") == {low["id"]}
    assert listing(q=good["id"]) == {good["id"]}
    page = client.get("/api/audits", params={"page_size": 2, "page": 2}).json()
    assert page["total"] == 3 and len(page["audits"]) == 1


def test_unknown_audit_is_a_404(client):
    assert client.get("/api/audits/SS-MISSING0").status_code == 404
    assert client.get("/audits/SS-MISSING0/image").status_code == 404


def test_categories_endpoint_lists_all_supported_categories(client):
    keys = {c["key"] for c in client.get("/api/categories").json()}
    assert keys == {"grocery", "beverages", "clothing", "cosmetics", "electronics", "pharmacy", "household", "custom"}
