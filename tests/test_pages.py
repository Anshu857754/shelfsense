"""Website pages: every audit state, form errors, history filters, safety of model-supplied text."""

import html
import json
import re

from app import db, vision
from factories import item, reply, shelf_image
from test_scenarios import GROCERY, run, stocked_grocery_reply, submit

BOX = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.25}


def page(client, audit_id):
    response = client.get(f"/audits/{audit_id}")
    assert response.status_code == 200
    return response.text


# ---- dashboard -------------------------------------------------------------------------


def test_dashboard_empty_state(client):
    text = client.get("/").text
    assert "No audits yet" in text and "Run your first audit" in text
    assert "Total audits" in text and "Needs attention" in text


def test_dashboard_with_audits_shows_stats_charts_and_recent_list(client, model):
    model.queue(stocked_grocery_reply(), vision.OpenRouterError("down"))
    good = run(client)
    failed = run(client)
    text = client.get("/").text
    assert good["id"] in text and failed["id"] in text
    assert "Compliance trend" in text and "Audit status" in text and "Average by category" in text
    assert "conic-gradient" in text  # the status donut
    assert "Grocery and snacks" in text and "100.0%" in text


# ---- new audit page ----------------------------------------------------------------------


def test_new_audit_page_has_every_input_mode_and_category(client):
    text = client.get("/new").text
    for needle in ('id="dropzone"', 'id="tab-camera"', 'id="cam-start"', 'id="cam-capture"', 'id="cam-switch"',
                   'id="cam-stop"', 'id="cam-retake"', 'id="cam-confirm"', 'id="planogram"', 'id="audit-btn"',
                   'name="category"', 'name="category_name"', 'id="samples-data"', "Audit a shelf"):
        assert needle in text
    for label in ("Grocery and snacks", "Beverages", "Clothing and fashion", "Cosmetics and skincare",
                  "Electronics", "Pharmacy and personal care", "Household products", "Custom category"):
        assert label in text
    assert "Jeans" in text  # the clothing sample planogram is embedded for the sample button


# ---- creating an audit: form errors ------------------------------------------------------


def test_bad_submissions_come_back_as_form_errors_and_create_nothing(client, model):
    bad_planogram = client.post(
        "/audits", files={"file": ("s.jpg", shelf_image(), "image/jpeg")},
        data={"planogram": "{oops", "category": "grocery"},
    )
    assert bad_planogram.status_code == 400
    assert "Invalid planogram" in bad_planogram.text and "{oops" in bad_planogram.text  # what you typed is kept

    gif = submit(client, image=shelf_image(fmt="GIF"))
    assert gif.status_code == 400 and "not supported" in gif.text

    text_file = submit(client, image=b"hello", filename="notes.txt", ctype="text/plain")
    assert text_file.status_code == 400 and "Only JPEG, PNG or WEBP" in text_file.text

    unknown = submit(client, category="spaceships")
    assert unknown.status_code == 400 and "Unknown retail category" in unknown.text

    assert client.get("/api/stats").json()["total"] == 0
    assert model.calls == []


# ---- audit page: each state --------------------------------------------------------------


def test_completed_audit_page_has_every_section(client, model):
    model.queue(reply(
        (1, [item("Potato chips", 3, "Chips bag", 0.9, box=BOX), item("Cornflakes", 4, "Cereal box", 0.9, box={**BOX, "x": 0.5})]),
        (2, [item("Cookies", 5, "Biscuit pack", 0.9), item("Noodle cups", 4, "Instant noodles", 0.4, box={**BOX, "y": 0.6})]),
    ))
    audit = run(client)
    text = page(client, audit["id"])

    for needle in ("% compliant.", "How sure are we?", "Confirm this result", "Trust:", "Do this first:",
                   "Shelf view", "To-do list", "Planogram vs shelf", "Review and correct detections",
                   "Units detected", "Units missing", "Units misplaced", "Unexpected units", "Uncertain (not counted)",
                   "AI confidence", "Restock 3 x Chips bag in row 1", f"/audits/{audit['id']}/image",
                   "Original AI output", "Missing products"):
        assert needle in text
    assert f"{audit['score']}%" in text
    assert 'class="task high' in text  # chips are half empty
    assert 'class="bbox short"' in text and 'class="bbox ok"' in text and 'class="bbox uncertain"' in text
    assert "Save corrections and recompute" in text and 'id="confirm-empty"' in text


def test_audit_page_without_boxes_says_so(client, model):
    model.queue(stocked_grocery_reply())
    text = page(client, run(client)["id"])
    assert "did not return box positions" in text and "bbox-label" not in text


def test_uncertain_items_are_marked_in_the_correction_table(client, model):
    model.queue(reply((1, [item("chips?", 6, "Chips bag", 0.2), item("Cornflakes", 4, "Cereal box", 0.9)]),
                      (2, [item("Cookies", 5, "Biscuit pack", 0.9), item("Noodles", 4, "Instant noodles", 0.9)])))
    text = page(client, run(client)["id"])
    assert "Uncertain" in text and "less than 50% sure" in text
    assert re.search(r'<tr class="uncertain"', text)


def test_processing_audit_page_shows_live_progress(client):
    audit_id = db.get_db().create_audit(category="grocery", category_label="Grocery and snacks", category_name="",
                                        planogram=GROCERY, image=shelf_image(), mime="image/jpeg",
                                        width=900, height=600, warnings=[])
    text = page(client, audit_id)
    assert 'id="processing"' in text and "Auditing your shelf" in text and "Processing" in text
    assert 'http-equiv="refresh"' in text  # works even without JavaScript
    assert "Compliance score" not in text


def test_failed_audit_page_explains_and_offers_retry(client, model):
    model.queue(vision.OpenRouterError("OpenRouter returned status 503"))
    audit = run(client)
    text = page(client, audit["id"])
    assert "could not be completed" in text and "503" in text
    assert "No compliance score was recorded" in text and "Try again" in text
    assert f'action="/audits/{audit["id"]}/retry"' in text
    assert "Compliance score" not in text and "Units missing" not in text


def test_needs_review_page_shows_no_score_and_offers_correction(client, model):
    model.queue(reply((1, []), (2, []), notes="Looks empty."))
    audit = run(client)
    text = page(client, audit["id"])
    assert "Incomplete audit" in text and "no score" in text and "No products were detected" in text
    assert "This shelf really is empty" in text and "Add an item the AI missed" in text
    assert "To-do list" not in text and "Restock" not in text  # nothing is claimed missing


def test_image_warnings_are_shown(client, model):
    model.queue(stocked_grocery_reply())
    text = page(client, run(client, image=shelf_image(blur=7))["id"])
    assert "looks soft or blurry" in text


def test_audit_page_survives_refresh_and_restart(client, model, tmp_path):
    model.queue(stocked_grocery_reply())
    audit = run(client)
    first = page(client, audit["id"])
    assert page(client, audit["id"]) == first  # a refresh shows the same thing
    db.configure(tmp_path / "data")  # a restart
    assert "100.0%" in page(client, audit["id"])
    assert client.get(f"/audits/{audit['id']}/image").headers["content-type"] == "image/jpeg"


def test_unknown_audit_page_is_a_friendly_404(client):
    response = client.get("/audits/SS-MISSING0")
    assert response.status_code == 404 and "Audit not found" in response.text


# ---- history ------------------------------------------------------------------------------


def test_history_search_filters_and_empty_states(client, model):
    model.queue(stocked_grocery_reply(), vision.OpenRouterError("down"))
    good = run(client, planogram={**GROCERY, "shelf": "Cheap aisle"})
    failed = run(client, category="beverages")

    everything = client.get("/audits").text
    assert good["id"] in everything and failed["id"] in everything and "2 audits" in everything

    only_failed = client.get("/audits", params={"status": "failed"}).text
    assert failed["id"] in only_failed and good["id"] not in only_failed and "match your filters" in only_failed

    searched = client.get("/audits", params={"q": "cheap"}).text
    assert good["id"] in searched and failed["id"] not in searched

    nothing = client.get("/audits", params={"q": "zzz-no-such-thing"}).text
    assert "No audits match" in nothing and "Clear filters" in nothing

    assert "No audits yet" in _empty_history(client)


def _empty_history(client):
    db.configure(db.get_db().directory / "other")  # a fresh, empty database
    return client.get("/audits").text


def test_history_is_paginated(client):
    database = db.get_db()
    ids = []
    for _ in range(14):
        audit_id = database.create_audit(category="grocery", category_label="Grocery and snacks", category_name="",
                                         planogram=GROCERY, image=b"x", mime="image/jpeg", width=1, height=1, warnings=[])
        database.update_audit(audit_id, status="completed", score=90.0)
        ids.append(audit_id)
    first = client.get("/audits").text
    second = client.get("/audits", params={"page": 2}).text
    assert "Page 1 of 2" in first and "Next" in first and ids[-1] in first and ids[0] not in first
    assert "Page 2 of 2" in second and "Previous" in second and ids[0] in second


# ---- safety and assets ---------------------------------------------------------------------


def test_text_from_the_model_is_escaped_not_executed(client, model):
    nasty = """<script>alert(1)</script> O'Brien "quoted" <b>bold</b>"""
    model.queue(reply(
        (1, [item(nasty, 6, "Chips bag", 0.9), item("Cornflakes", 4, "Cereal box")]),
        (2, [item("Cookies", 5, "Biscuit pack"), item("Noodle cups", 4, "Instant noodles")]),
    ))
    audit = run(client)
    text = page(client, audit["id"])
    assert "<script>alert(1)</script>" not in text and "<b>bold</b>" not in text
    # The data attribute that feeds the correction table must still parse as JSON.
    raw = re.search(r"data-item='([^']*alert[^']*)'", text).group(1)
    assert json.loads(html.unescape(raw))["name"] == nasty


def test_scripts_and_styles_are_served(client):
    app_js = client.get("/static/app.js")
    assert app_js.status_code == 200
    for needle in ("getUserMedia", "enumerateDevices", "DataTransfer", "requestSubmit", "NotAllowedError", "facingMode"):
        assert needle in app_js.text
    result_js = client.get("/static/result.js")
    assert result_js.status_code == 200 and "/corrections" in result_js.text and "/tasks/" in result_js.text
    assert "conic-gradient" in client.get("/static/style.css").text
