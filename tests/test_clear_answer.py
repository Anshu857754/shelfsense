"""A clear answer every time: percentage, plain summary, trust level, estimated range,
second look at unclear items, confirmation, and rebuilding a planogram from a photo."""

import html
import json
import re
import sqlite3

import pytest

from app import db, vision
from app.compliance import audit
from app.config import get_settings
from app.db import Database, _SCHEMA
from app.imaging import crop_region
from app.pipeline import planogram_from_shelf
from app.schemas import BoundingBox, DetectedShelf, Planogram
from factories import item, reply, shelf_image
from test_scenarios import CLOTHING, GROCERY, run, stocked_grocery_reply, submit

BOX = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.25}


def planogram() -> Planogram:
    return Planogram.model_validate({"rows": [
        {"row": 1, "expected": [{"product": "Coca-Cola 500ml", "facings": 6}, {"product": "Pepsi 500ml", "facings": 4}]},
        {"row": 2, "expected": [{"product": "Sprite 500ml", "facings": 5}]},
    ]})


def shelf(*rows) -> DetectedShelf:
    out = []
    for r, prods in rows:
        out.append({"row": r, "products": [{"name": p[0], "quantity": p[1], **(p[2] if len(p) > 2 else {})} for p in prods]})
    return DetectedShelf.model_validate({"rows": out})


def verify_reply(*items) -> str:
    return json.dumps({"items": list(items)})


# ---- the AI is judged on what it is, not on how well it can count -----------------------


def test_a_clearly_identified_pile_is_counted_even_when_the_number_is_rough():
    s = shelf((1, [("Coca-Cola 500ml", 6, {"confidence": 0.9, "count_confidence": 0.4}), ("Pepsi 500ml", 4, {"confidence": 0.9})]),
              (2, [("Sprite 500ml", 5, {"confidence": 0.9})]))
    result = audit(planogram(), s)
    assert result.complete and result.score == 100
    assert result.uncertain_units == 0 and result.approx_count_groups == 1
    assert any("rough counts" in w for w in result.warnings)
    assert result.trust_level == "medium" and "rough counts" in result.trust_reason


def test_not_knowing_what_something_is_still_sets_it_aside_whatever_the_count_confidence():
    s = shelf((1, [("Coca-Cola 500ml", 6, {"confidence": 0.9}), ("blob", 2, {"confidence": 0.3, "count_confidence": 0.99})]),
              (2, [("Sprite 500ml", 5, {"confidence": 0.9})]))
    assert audit(planogram(), s).uncertain_units == 2


# ---- a headline and a summary every time -------------------------------------------------


def test_a_scored_audit_gets_a_percentage_headline_and_plain_summary():
    s = shelf((1, [("Coca-Cola 500ml", 6), ("Pepsi 500ml", 2)]), (2, [("Sprite 500ml", 5)]))
    result = audit(planogram(), s)
    assert result.headline == "86.7% compliant. The shelf is in good shape."
    assert result.summary == [
        "13 of 15 expected units are in the right place.",
        "Problems found: 2 missing.",
        "Do this first: Restock 2 x Pepsi 500ml in row 1.",
    ]


def test_headline_wording_follows_the_score_band():
    perfect = audit(planogram(), shelf((1, [("Coca-Cola 500ml", 6), ("Pepsi 500ml", 4)]), (2, [("Sprite 500ml", 5)])))
    assert perfect.headline == "100% compliant. The shelf is in good shape." and perfect.summary[-1] == "No problems found."
    middling = audit(planogram(), shelf((1, [("Coca-Cola 500ml", 4), ("Pepsi 500ml", 2)]), (2, [("Sprite 500ml", 5)])))
    assert "needs attention" in middling.headline
    poor = audit(planogram(), shelf((1, [("Coca-Cola 500ml", 1)]), (2, [("Sprite 500ml", 1)])))
    assert "needs work" in poor.headline


def test_a_clean_shelf_is_trusted_and_says_why():
    result = audit(planogram(), shelf((1, [("Coca-Cola 500ml", 6, {"confidence": 0.9}), ("Pepsi 500ml", 4, {"confidence": 0.9})]),
                                      (2, [("Sprite 500ml", 5, {"confidence": 0.9})])))
    assert result.trust_level == "high" and result.trust_reason == "Everything counted was clearly identified."


def test_uncertain_items_lower_the_trust_and_are_explained():
    result = audit(planogram(), shelf(
        (1, [("Coca-Cola 500ml", 6, {"confidence": 0.9}), ("Pepsi 500ml", 4, {"confidence": 0.9}), ("blob", 2, {"confidence": 0.3})]),
        (2, [("Sprite 500ml", 5, {"confidence": 0.9})])))
    assert result.trust_level == "medium" and "2 uncertain units were not counted" in result.trust_reason


def test_a_planogram_with_fewer_rows_than_the_shelf_is_called_out():
    """Real case: a rack with 6 levels and a planogram with 2 rows scored 25% with no explanation."""
    s = shelf((1, [("Coca-Cola 500ml", 6), ("Pepsi 500ml", 4)]), (2, [("Sprite 500ml", 5)]),
              (3, [("Coca-Cola 500ml", 6)]), (4, [("Pepsi 500ml", 4)]))
    result = audit(planogram(), s)
    assert any("shelf level(s) 3, 4" in w and "counted as in place" in w for w in result.warnings)
    assert any("fewer rows" in line for line in result.summary)
    # Row 1 and 2 are already full, so the duplicates on levels 3 and 4 are surplus (10 units), but the
    # planogram's own rows are no longer marked down for the AI's level numbering.
    assert result.score == 90.0 and result.misplaced_units == 10 and result.trust_level == "medium"
    assert "fewer rows than the shelf has levels" in result.trust_reason
    # Products found only on an unlisted level fill the row they belong to instead of counting as wrong-row.
    filled = audit(planogram(), shelf((1, [("Coca-Cola 500ml", 6)]), (3, [("Pepsi 500ml", 4), ("Sprite 500ml", 5)])))
    assert filled.misplaced_units == 0 and filled.score == 100.0


def test_an_unscored_audit_explains_itself_in_one_short_line():
    result = audit(planogram(), shelf((1, [("blue widgets", 5, {"confidence": 0.9})])))
    assert result.incomplete_code == "no_match"
    assert result.trust_reason == "Your planogram does not describe what is on this shelf."


# ---- an empty shelf is an answer, not a failure ---------------------------------------------


def test_the_ai_saying_the_shelf_is_empty_gives_a_clear_held_answer():
    result = audit(planogram(), DetectedShelf(rows=[], shelf_empty=True))
    assert not result.complete and result.incomplete_code == "empty_shelf" and result.score is None
    assert (result.estimate_low, result.estimate_high) == (0.0, 0.0)
    assert result.headline == "The shelf looks empty."
    assert "none of the 15 expected units are there: 0% compliance" in result.summary[1]
    assert any("about 0%" in line and "not confirmed" in line for line in result.summary)
    assert result.gaps == [] and result.tasks == []  # nothing is recorded until a person confirms
    confirmed = audit(planogram(), DetectedShelf(rows=[], shelf_empty=True), allow_empty=True)
    assert confirmed.complete and confirmed.score == 0 and confirmed.missing_units == 15
    assert confirmed.trust_reason == "You confirmed that the shelf is empty."


def test_empty_is_not_claimed_when_products_were_seen():
    s = shelf((1, [("Coca-Cola 500ml", 6)]))
    s.shelf_empty = True  # a contradictory reply: products listed AND "empty"
    assert audit(planogram(), s).incomplete_code != "empty_shelf"


def test_empty_shelf_end_to_end_with_one_click_confirmation(client, model):
    model.queue(json.dumps({"shelf_visible": True, "shelf_empty": True, "overall_notes": "Nothing on the shelves.",
                            "rows": [{"row": 1, "products": []}, {"row": 2, "products": []}]}))
    audit_row = run(client)
    assert audit_row["status"] == "needs_review" and audit_row["score"] is None
    page = client.get(f"/audits/{audit_row['id']}").text
    assert "The shelf looks empty." in page and "~0%" in page and 'id="confirm-empty-btn"' in page
    assert "Yes, the shelf is empty (record 0%)" in page

    done = client.post(f"/api/audits/{audit_row['id']}/corrections", json={"items": [], "confirmed_empty": True}).json()
    assert done == {"status": "completed", "score": 0.0, "complete": True}
    final = client.get(f"/api/audits/{audit_row['id']}").json()
    assert final["missing_units"] == 19 and {t["priority"] for t in final["tasks"]} == {"high"}
    assert "0% compliant." in client.get(f"/audits/{audit_row['id']}").text


def test_embedded_sample_planograms_keep_their_natural_key_order(client):
    """The page compares the text box with these samples to decide whether you have edited it.
    Alphabetised keys made them never match, so choosing a category never swapped the sample."""
    page = client.get("/new").text
    samples = json.loads(re.search(r'<script type="application/json" id="samples-data">(.*?)</script>', page, re.S).group(1))
    assert list(samples["clothing"])[:3] == ["store", "shelf", "rows"]
    assert list(samples["clothing"]["rows"][0]) == ["row", "expected"]
    box = re.search(r"<textarea[^>]*>(.*?)</textarea>", page, re.S).group(1)
    assert json.loads(html.unescape(box)) == samples["grocery"]  # the box shows the default category's sample


# ---- scripts can never be stale -------------------------------------------------------------


def test_script_and_style_links_change_when_the_files_change(client):
    text = client.get("/new").text
    assert re.search(r'/static/app\.js\?v=\d+', text) and re.search(r'/static/style\.css\?v=\d+', text)
    assert client.get("/static/app.js?v=123").status_code == 200  # the version is only a cache key
    assert 'planoMismatch' in client.get("/static/app.js").text and 'id="plano-mismatch"' in text


# ---- an incomplete audit still gives an answer -------------------------------------------


def test_mostly_uncertain_audit_gives_an_estimated_range_not_a_blank():
    s = shelf((1, [("Coca-Cola 500ml", 2, {"confidence": 0.9}),
                   ("brown bottles", 6, {"confidence": 0.3, "matched_expected": "Pepsi 500ml"})]))
    result = audit(planogram(), s)
    assert not result.complete and result.score is None and result.incomplete_code == "uncertain_share"
    assert (result.estimate_low, result.estimate_high) == (13.3, 40.0)  # only-sure vs everything-accepted
    assert result.headline == "Not enough is confirmed to give one score."
    assert any("between 13.3% and 40%" in line and "not confirmed" in line for line in result.summary)
    assert result.trust_level == "low"


def test_when_nothing_would_match_even_if_accepted_the_planogram_is_the_problem():
    """The real clothing audit: shirts and sweaters at low confidence against a grocery planogram."""
    s = shelf((1, [("brown folded sweaters", 12, {"confidence": 0.4, "category": "knitwear"}),
                   ("blue folded shirts", 14, {"confidence": 0.4, "category": "shirts"})]),
              (2, [("red folded shirts", 11, {"confidence": 0.4, "category": "shirts"})]))
    result = audit(planogram(), s)
    assert result.incomplete_code == "no_match" and result.estimate_low is None
    assert result.headline == "None of the items match your planogram."
    assert result.summary[0] == "The AI saw 37 units: 25 shirts and 12 knitwear."
    assert result.summary[1].startswith("Your planogram lists: Coca-Cola 500ml, Pepsi 500ml, Sprite 500ml.")
    assert "use what the AI saw as your planogram" in result.summary[2]


def test_an_empty_or_unusable_photo_says_what_to_do():
    empty = audit(planogram(), DetectedShelf(rows=[]))
    assert empty.headline == "No products were detected." and "confirm that" in empty.summary[0]
    unusable = audit(planogram(), DetectedShelf(rows=[], shelf_visible=False, image_issues=["too dark"]))
    assert unusable.headline == "The photo could not be audited." and "too dark" in unusable.summary[0]


# ---- end to end, through the pages ---------------------------------------------------------


def test_clothing_photo_with_a_grocery_planogram_explains_the_mismatch(client, model):
    model.queue(reply(
        (1, [item("brown folded sweaters", 12, None, 0.4, "knitwear"), item("blue folded shirts", 14, None, 0.4, "shirts")]),
        (2, [item("red folded shirts", 11, None, 0.4, "shirts"), item("navy folded knitwear", 10, None, 0.4, "knitwear")]),
    ))
    audit_row = run(client)  # the grocery planogram, like the real run
    assert audit_row["status"] == "needs_review" and audit_row["score"] is None
    text = client.get(f"/audits/{audit_row['id']}").text
    assert "None of the items match your planogram." in text
    assert "The AI saw 47 units" in text and "Chips bag" in text
    assert "Use what the AI saw as my planogram" in text and f"/new?from={audit_row['id']}" in text


def test_clothing_audit_with_the_right_planogram_gets_a_percentage_and_summary(client, model):
    model.queue(reply(
        (1, [item("folded white shirts", 6, "Shirt", 0.85, "shirts"), item("blue denim jeans", 4, "Jeans", 0.9, "jeans")]),
        (2, [item("navy trousers", 4, "Trousers", 0.8), item("summer dresses", 3, "Dress", 0.8), item("jackets", 3, "Jacket", 0.8)]),
    ))
    audit_row = run(client, planogram=CLOTHING, category="clothing")
    text = client.get(f"/audits/{audit_row['id']}").text
    # Row 1: 10 of 11 (jeans 4 of 5) = 90.9; row 2: 10 of 10 = 100; weighted by facings = 95.2.
    assert audit_row["status"] == "completed" and audit_row["score"] == 95.2
    assert "95.2% compliant." in text and "20 of 21 expected units" in text
    assert "Problems found: 1 missing." in text and "How sure are we?" in text


def test_old_audits_without_a_summary_are_rescored_when_opened(client, model):
    model.queue(stocked_grocery_reply())
    audit_row = run(client)
    old = {**audit_row["result_json"], "headline": "", "summary": [], "trust_level": None, "trust_reason": ""}
    db.get_db().update_audit(audit_row["id"], result_json=old)
    assert "100% compliant." in client.get(f"/audits/{audit_row['id']}").text


# ---- confirming a result ---------------------------------------------------------------------


def test_a_result_can_be_confirmed_and_unconfirmed(client, model):
    model.queue(stocked_grocery_reply())
    audit_row = run(client)
    assert 'id="confirmed-chip" hidden' in client.get(f"/audits/{audit_row['id']}").text

    stamp = client.post(f"/api/audits/{audit_row['id']}/confirm", json={"confirmed": True}).json()["confirmed_at"]
    assert stamp and client.get(f"/api/audits/{audit_row['id']}").json()["confirmed_at"] == stamp
    page = client.get(f"/audits/{audit_row['id']}").text
    assert 'id="confirmed-chip" hidden' not in page and "Undo confirmation" in page

    assert client.post(f"/api/audits/{audit_row['id']}/confirm", json={"confirmed": False}).json() == {"confirmed_at": None}


def test_changing_the_result_removes_the_confirmation(client, model):
    model.queue(stocked_grocery_reply())
    audit_row = run(client)
    client.post(f"/api/audits/{audit_row['id']}/confirm", json={})
    rows = [{"row": r["row"], "name": p["name"], "quantity": p["facings"], "state": "keep", "edited": False,
             "confidence": p["confidence"], "source": p["source"], "matched_expected": p["matched_expected"]}
            for r in audit_row["working_json"]["rows"] for p in r["products"]]
    client.post(f"/api/audits/{audit_row['id']}/corrections", json={"items": rows})
    assert client.get(f"/api/audits/{audit_row['id']}").json()["confirmed_at"] is None


def test_only_a_scored_audit_can_be_confirmed(client, model):
    model.queue(reply((1, []), (2, [])))
    review = run(client)
    assert client.post(f"/api/audits/{review['id']}/confirm", json={}).status_code == 409
    assert client.post("/api/audits/SS-MISSING0/confirm", json={}).status_code == 404


# ---- the zoomed-in second look ---------------------------------------------------------------

DETECT_WITH_UNSURE_GROUP = reply(
    (1, [item("Potato chips", 6, "Chips bag", 0.9), item("grey stacks", 3, None, 0.4, box=BOX)]),
    (2, [item("Cookies", 5, "Biscuit pack", 0.9), item("Noodle cups", 4, "Instant noodles", 0.9)]),
)


def test_second_look_confirms_an_unclear_group(client, model):
    model.queue(DETECT_WITH_UNSURE_GROUP, verify_reply(
        {"crop": 1, "name": "boxes of cereal", "quantity": 4, "matched_expected": "Cereal box",
         "confidence": 0.9, "count_confidence": 0.8, "attributes": ["white box"]}))
    audit_row = run(client)

    assert len(model.calls) == 2
    second = model.calls[1][0]["content"]
    assert len([p for p in second if p["type"] == "image_url"]) == 1  # one zoomed crop
    assert "Cereal box" in second[0]["text"] and "grey stacks" in second[1]["text"]

    assert audit_row["status"] == "completed" and audit_row["score"] == 100.0 and audit_row["uncertain_units"] == 0
    original = next(p for r in audit_row["detected_json"]["rows"] for p in r["products"] if p["name"] == "grey stacks")
    assert original["confidence"] == 0.4 and original["verified"] is False  # the first answer is kept as it came
    working = next(p for r in audit_row["working_json"]["rows"] for p in r["products"] if p["name"] == "boxes of cereal")
    assert working["verified"] is True and working["facings"] == 4 and working["matched_expected"] == "Cereal box"
    page = client.get(f"/audits/{audit_row['id']}").text
    assert "Second look confirmed" in page and "confirmed by a zoomed-in second look" in page


def test_second_look_that_is_still_unsure_changes_nothing(client, model):
    model.queue(DETECT_WITH_UNSURE_GROUP, verify_reply(
        {"crop": 1, "name": "something", "quantity": 4, "matched_expected": "Cereal box", "confidence": 0.3}))
    audit_row = run(client)
    assert audit_row["uncertain_units"] == 3 and audit_row["status"] == "completed"
    assert any("Cereal box" in t["text"] for t in audit_row["tasks"])  # still reported missing, not invented


@pytest.mark.parametrize("second", ["not json at all", vision.OpenRouterError("down"), verify_reply({"crop": 9, "confidence": 0.9, "quantity": 2})])
def test_a_failed_or_useless_second_look_never_breaks_the_audit(client, model, second):
    model.queue(DETECT_WITH_UNSURE_GROUP, second)
    audit_row = run(client)
    assert audit_row["status"] == "completed" and audit_row["uncertain_units"] == 3 and len(model.calls) == 2


def test_no_second_look_without_boxes_or_when_switched_off(client, model, monkeypatch):
    model.queue(reply((1, [item("Potato chips", 6, "Chips bag", 0.9), item("grey stacks", 3, None, 0.4)]),
                      (2, [item("Cookies", 5, "Biscuit pack", 0.9), item("Noodle cups", 4, "Instant noodles", 0.9)])))
    run(client)
    assert len(model.calls) == 1  # nothing to crop: no box

    model.queue(DETECT_WITH_UNSURE_GROUP)
    monkeypatch.setattr(get_settings(), "verify_uncertain", False)
    run(client)
    assert len(model.calls) == 2  # one more detect call, and still no second look


def test_crops_are_enlarged_so_small_details_become_visible():
    data = shelf_image()
    crop = crop_region(data, BoundingBox(x=0.1, y=0.1, w=0.1, h=0.1))
    assert crop[:2] == b"\xff\xd8"
    from PIL import Image
    import io
    width, height = Image.open(io.BytesIO(crop)).size
    assert 600 <= max(width, height) <= 700  # a ~200 px area was enlarged to about 640

    tiny = Image.open(io.BytesIO(crop_region(data, BoundingBox(x=0.5, y=0.5, w=0.001, h=0.001)))).size
    assert max(tiny) <= 4 * 108 + 4  # a very small area is enlarged at most 4 times, not blown up endlessly

    from types import SimpleNamespace
    off_photo = SimpleNamespace(x=2.0, y=2.0, w=0.1, h=0.1)  # a box outside the photo
    assert Image.open(io.BytesIO(crop_region(data, off_photo))).size == (900, 600)  # falls back to the whole photo


# ---- the prompt asks for calibrated confidence ----------------------------------------------


def test_the_prompt_separates_what_it_is_from_how_many():
    prompt = vision.build_detect_prompt(Planogram.model_validate(GROCERY), "grocery")
    assert '"count_confidence"' in prompt and "WHAT the item is" in prompt
    assert "folded shirts that you can plainly see" in prompt
    assert "Chips bag" in prompt and "Grocery and snacks" in prompt


# ---- rebuild a planogram from what the AI saw ----------------------------------------------------


def test_planogram_is_built_from_detections():
    s = shelf((1, [("blue folded shirts", 6, {"category": "shirts"}), ("blue folded shirts", 2), ("brown knitwear", 3)]),
              (2, [("empty", 0), ("red jeans", 4)]))
    plan = planogram_from_shelf(s, "Store-1", "Wall A")
    assert plan == {"store": "Store-1", "shelf": "Wall A", "rows": [
        {"row": 1, "expected": [{"product": "blue folded shirts", "facings": 8, "category": "shirts"},
                                {"product": "brown knitwear", "facings": 3}]},
        {"row": 2, "expected": [{"product": "red jeans", "facings": 4}]},
    ]}
    Planogram.model_validate(plan)  # and it is a valid planogram


def test_new_audit_page_can_start_from_a_previous_photo(client, model):
    model.queue(reply((1, [item("brown folded sweaters", 12, None, 0.9, "knitwear"), item("blue folded shirts", 14, None, 0.9, "shirts")])))
    audit_row = run(client)
    assert audit_row["status"] == "needs_review"  # nothing matches the grocery planogram

    text = client.get("/new", params={"from": audit_row["id"]}).text
    assert "built from what the AI saw" in text and "blue folded shirts" in text
    assert f'name="reuse_image" id="reuse-image" value="{audit_row["id"]}"' in text

    # Run it again with the new planogram and the same photo: no upload needed.
    plan = json.loads(re.search(r'<textarea[^>]*>(.*?)</textarea>', text, re.S).group(1).replace("&#34;", '"'))
    model.queue(reply((1, [item("brown folded sweaters", 12, "brown folded sweaters", 0.9), item("blue folded shirts", 14, "blue folded shirts", 0.9)])))
    response = client.post("/audits", data={"planogram": json.dumps(plan), "category": "clothing", "reuse_image": audit_row["id"]},
                           follow_redirects=False)
    assert response.status_code == 303
    again = client.get(f"/api/audits/{response.headers['location'].rsplit('/', 1)[-1]}").json()
    assert again["status"] == "completed" and again["score"] == 100.0
    assert (again["image_w"], again["image_h"]) == (audit_row["image_w"], audit_row["image_h"])


def test_new_audit_from_a_missing_audit_or_without_a_photo_is_a_clear_error(client):
    assert client.get("/new", params={"from": "SS-MISSING0"}).status_code == 404
    form = {"planogram": json.dumps(GROCERY), "category": "grocery"}
    assert "Please add a shelf photo" in client.post("/audits", data=form).text
    assert "no longer available" in client.post("/audits", data={**form, "reuse_image": "SS-MISSING0"}).text


# ---- an older database is upgraded without losing audits ------------------------------------------


def test_old_database_is_upgraded_in_place(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    # The schema before those columns existed.
    old_schema = re.sub(r",\n    (?:confirmed_at|usage_json)\s+TEXT|,\n    attempts\s+INTEGER NOT NULL DEFAULT 0", "", _SCHEMA)
    assert not any(name in old_schema for name in ("confirmed_at", "usage_json", "attempts"))
    conn = sqlite3.connect(tmp_path / "shelfsense.db")
    conn.executescript(old_schema)
    conn.execute("INSERT INTO audits (id, created_at, updated_at, status, category, planogram_json) "
                 "VALUES ('SS-OLD00001', '2026-11-10T08:00:00+00:00', '2026-11-10T08:00:00+00:00', 'completed', 'grocery', '{}')")
    conn.commit()
    conn.close()

    upgraded = Database(tmp_path)
    audit_row = upgraded.get_audit("SS-OLD00001")
    assert audit_row is not None and audit_row["confirmed_at"] is None
    assert upgraded.set_confirmed("SS-OLD00001", True) is not None
    Database(tmp_path)  # opening it again is harmless
