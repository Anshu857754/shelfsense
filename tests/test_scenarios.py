"""The ten required scenarios, run end to end: HTTP request, real pipeline, real database.

Only the network call to the AI is scripted (see factories.FakeModel). Detection counts,
scores, missing/misplaced/unexpected items, tasks and history are all computed by the real code,
so these tests check that the numbers stay consistent from the model's reply to what is stored.
"""

import json

import pytest

from app import db, vision
from factories import item, reply, shelf_image

GROCERY = {
    "store": "S1", "shelf": "Snacks-A",
    "rows": [
        {"row": 1, "expected": [{"product": "Chips bag", "facings": 6, "aliases": ["snack bag"]}, {"product": "Cereal box", "facings": 4}]},
        {"row": 2, "expected": [{"product": "Biscuit pack", "facings": 5}, {"product": "Instant noodles", "facings": 4}]},
    ],
}
CLOTHING = {
    "store": "S1", "shelf": "Apparel-A",
    "rows": [
        {"row": 1, "expected": [{"product": "Shirt", "facings": 6, "aliases": ["t-shirt"]}, {"product": "Jeans", "facings": 5}]},
        {"row": 2, "expected": [{"product": "Trousers", "facings": 4}, {"product": "Dress", "facings": 3}, {"product": "Jacket", "facings": 3}]},
    ],
}
COSMETICS = {
    "store": "S1", "shelf": "Beauty-A",
    "rows": [
        {"row": 1, "expected": [{"product": "Face moisturizer", "facings": 4, "aliases": ["face cream"]}, {"product": "Lipstick", "facings": 6}]},
        {"row": 2, "expected": [{"product": "Sunscreen tube", "facings": 4, "aliases": ["sun cream"]}, {"product": "Shampoo bottle", "facings": 3}]},
    ],
}


def stocked_grocery_reply(**overrides):
    rows = {
        1: [item("Salted potato chips, red bags", 6, "Chips bag", 0.92, "snacks"), item("Cornflakes box", 4, "Cereal box", 0.88)],
        2: [item("Choco cookies", 5, "Biscuit pack", 0.90), item("Noodle cups", 4, "Instant noodles", 0.85)],
    }
    rows.update(overrides)
    return reply(*rows.items())


def submit(client, image=None, planogram=GROCERY, category="grocery", category_name="",
           filename="shelf.jpg", ctype="image/jpeg"):
    return client.post(
        "/audits",
        files={"file": (filename, image if image is not None else shelf_image(), ctype)},
        data={"planogram": json.dumps(planogram), "category": category, "category_name": category_name},
        follow_redirects=False,
    )


def run(client, **kw):
    """Submit an audit and return its stored record once the background job has finished."""
    response = submit(client, **kw)
    assert response.status_code == 303, response.text
    audit_id = response.headers["location"].rsplit("/", 1)[-1]
    return client.get(f"/api/audits/{audit_id}").json()


def result_of(audit):
    return audit["result_json"]


# ---- 1. Fully stocked grocery shelf ---------------------------------------------------


def test_1_fully_stocked_grocery_shelf(client, model):
    model.queue(stocked_grocery_reply())
    audit = run(client)

    assert audit["status"] == "completed" and audit["score"] == 100.0
    assert (audit["detected_units"], audit["missing_units"], audit["misplaced_units"], audit["unexpected_units"]) == (19, 0, 0, 0)
    assert audit["tasks"] == [] and result_of(audit)["gaps"] == []
    # Reported confidences are passed through as given (mean of .92 .88 .90 .85), not invented.
    assert audit["confidence"] == pytest.approx(0.8875, abs=0.001)

    # The photo really reached the model, and the model was told what to look for.
    content = model.calls[0][0]["content"]
    assert [part["type"] for part in content] == ["text", "image_url"]
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    prompt = model.first_prompt
    assert "Grocery and snacks" in prompt and "Chips bag" in prompt and "Instant noodles" in prompt


# ---- 2. Partially empty grocery shelf -------------------------------------------------


def test_2_partially_empty_grocery_shelf(client, model):
    model.queue(reply(
        (1, [item("Potato chips", 3, "Chips bag"), item("Cornflakes", 4, "Cereal box")]),
        (2, [item("Cookies", 5, "Biscuit pack"), item("Noodle cups", 2, "Instant noodles")]),
    ))
    audit = run(client)
    result = result_of(audit)

    assert audit["status"] == "completed"
    assert audit["detected_units"] == 14 and audit["missing_units"] == 5
    assert audit["score"] == 73.7  # row 1: 7/10 = 70.0, row 2: 7/9 = 77.8, weighted by facings
    assert sorted((g["product"], g["missing"]) for g in result["gaps"]) == [("Chips bag", 3), ("Instant noodles", 2)]
    assert {t["kind"] for t in audit["tasks"]} == {"restock"}
    assert all(t["priority"] == "high" for t in audit["tasks"])  # both are half empty


# ---- 3. Incorrectly arranged grocery shelf --------------------------------------------


def test_3_incorrectly_arranged_grocery_shelf(client, model):
    model.queue(reply(
        (1, [item("Potato chips", 6, "Chips bag"), item("Cookies", 5, "Biscuit pack")]),  # cookies belong in row 2
        (2, [item("Cornflakes", 4, "Cereal box"), item("Noodle cups", 4, "Instant noodles")]),  # cereal belongs in row 1
    ))
    audit = run(client)
    result = result_of(audit)

    assert audit["status"] == "completed"
    assert sorted((m["product"], m["found_in_row"], m["belongs_in_row"]) for m in result["misplaced"]) == [
        ("Biscuit pack", 1, 2), ("Cereal box", 2, 1),
    ]
    assert audit["misplaced_units"] == 9
    assert audit["score"] == 47.6  # row 1: 6/10 - 5 = 55.0; row 2: 4/9 - 5 = 39.4
    kinds = sorted(t["kind"] for t in audit["tasks"])
    assert kinds == ["move", "move", "restock", "restock"]
    assert all(t["priority"] == "high" for t in audit["tasks"])  # each move fills an empty product


# ---- 4. Clothing shelf with shirts and jeans -------------------------------------------


def test_4_clothing_shelf_with_shirts_and_jeans(client, model):
    model.queue(reply(
        (1, [item("white cotton t-shirts", 6, "Shirt", 0.9, "shirts", attributes=["white", "folded"]),
             item("blue denim jeans", 5, "Jeans", 0.85, "jeans")]),
        (2, [item("black trousers", 4, "Trousers", 0.8, "trousers"),
             item("floral summer dresses", 3, "Dress", 0.75, "dresses"),
             item("denim jackets", 3, "Jacket", 0.7, "jackets")]),
    ))
    audit = run(client, planogram=CLOTHING, category="clothing")

    assert audit["category"] == "clothing" and audit["category_label"] == "Clothing and fashion"
    assert audit["status"] == "completed" and audit["score"] == 100.0 and audit["detected_units"] == 21
    # Detection is not limited to bottles and bags: garment types come through as reported.
    detections = result_of(audit)["detections"]
    assert {d["category"] for d in detections} == {"shirts", "jeans", "trousers", "dresses", "jackets"}
    assert next(d for d in detections if d["matched_product"] == "Shirt")["attributes"] == ["white", "folded"]
    # The model was told this is a clothing shelf and how to count garments.
    prompt = model.first_prompt
    assert "Clothing and fashion" in prompt and "jeans" in prompt and "dresses" in prompt
    assert "Shirt" in prompt and "Jacket" in prompt


def test_4b_custom_category_uses_the_users_own_name(client, model):
    model.queue(reply((1, [item("dog chew toys", 4, "Product A", 0.8)])))
    planogram = {"rows": [{"row": 1, "expected": [{"product": "Product A", "facings": 4}]}]}
    audit = run(client, planogram=planogram, category="custom", category_name="Pet supplies")
    assert audit["category_label"] == "Pet supplies" and audit["score"] == 100.0
    assert "Pet supplies" in model.first_prompt


# ---- 5. Cosmetics shelf -----------------------------------------------------------------


def test_5_cosmetics_shelf(client, model):
    model.queue(reply(
        (1, [item("face cream", 4, None, 0.8, "skincare"),  # no planogram match from the model: the alias covers it
             item("matte lipsticks", 6, "Lipstick", 0.9, "makeup")]),
        (2, [item("SPF 50 sunscreen tube", 4, "Sunscreen tube", 0.85, "sun care"),
             item("shampoo bottles", 2, "Shampoo bottle", 0.7, "haircare")]),
    ))
    audit = run(client, planogram=COSMETICS, category="cosmetics")

    assert audit["status"] == "completed" and audit["detected_units"] == 16
    assert audit["score"] == 94.1  # only one shampoo bottle is missing
    assert [(g["product"], g["missing"]) for g in result_of(audit)["gaps"]] == [("Shampoo bottle", 1)]
    assert "Cosmetics and skincare" in model.first_prompt


# ---- 6. Shelf containing unexpected products --------------------------------------------


def test_6_shelf_with_unexpected_products(client, model):
    model.queue(reply(
        (1, [item("Potato chips", 6, "Chips bag"), item("Cornflakes", 4, "Cereal box"), item("Mystery gadget", 2, None, 0.8)]),
        (2, [item("Cookies", 5, "Biscuit pack"), item("Noodle cups", 4, "Instant noodles")]),
    ))
    audit = run(client)
    result = result_of(audit)

    assert audit["status"] == "completed" and audit["unexpected_units"] == 2
    assert [(u["name"], u["row"], u["facings"]) for u in result["unexpected"]] == [("Mystery gadget", 1, 2)]
    assert audit["score"] == 97.4  # the other 19 facings are right; one 5-point penalty in row 1
    assert [(t["kind"], t["text"]) for t in audit["tasks"]] == [
        ("remove", "Remove 'Mystery gadget' from row 1: it is not in the planogram")
    ]


# ---- 7. Blurry or unsupported image -------------------------------------------------------


def test_7a_blurry_photo_is_audited_but_flagged(client, model):
    model.queue(stocked_grocery_reply())
    audit = run(client, image=shelf_image(blur=7))
    assert audit["status"] == "completed"
    assert any("blurry" in w for w in audit["warnings_json"])


@pytest.mark.parametrize("fmt", ["GIF", "BMP"])
def test_7b_unsupported_image_formats_are_refused(client, model, fmt):
    response = client.post("/detect", files={"file": ("shelf.jpg", shelf_image(fmt=fmt), "image/jpeg")})
    assert response.status_code == 400 and "not supported" in response.json()["detail"]
    assert model.calls == []  # nothing was sent to the AI


def test_7c_files_that_are_not_images_are_refused(client, model):
    wrong_type = client.post("/detect", files={"file": ("notes.txt", b"hello", "text/plain")})
    fake_png = client.post("/detect", files={"file": ("fake.png", b"not really a png", "image/png")})
    full = shelf_image()
    cut_off = client.post("/detect", files={"file": ("cut.jpg", full[: len(full) // 2], "image/jpeg")})
    assert [r.status_code for r in (wrong_type, fake_png, cut_off)] == [400, 400, 400]
    assert "damaged" in cut_off.json()["detail"]


# ---- 8. Camera-captured image -------------------------------------------------------------


def test_8_camera_captured_photo_uses_the_same_pipeline(client, model):
    # A phone held upright: portrait JPEG, named the way the camera page names its snapshots.
    model.queue(stocked_grocery_reply())
    audit = run(client, image=shelf_image(size=(720, 1280)), filename="camera-20261110-120000.jpg")
    assert audit["status"] == "completed" and audit["score"] == 100.0
    assert (audit["image_w"], audit["image_h"]) == (720, 1280)
    assert db.get_db().read_image(audit["id"]) is not None


# ---- 9. Failed AI detection ---------------------------------------------------------------


def test_9a_service_error_is_a_failed_audit_not_a_zero_score(client, model):
    model.queue(vision.OpenRouterError("OpenRouter returned status 503"))
    audit = run(client)
    assert audit["status"] == "failed" and audit["score"] is None
    assert "could not analyse" in audit["error"] and "503" in audit["error"]
    assert audit["tasks"] == [] and audit["result_json"] is None  # no fake "everything is missing"

    # Retrying with a working model completes the same audit.
    model.queue(stocked_grocery_reply())
    assert client.post(f"/audits/{audit['id']}/retry", follow_redirects=False).status_code == 303
    again = client.get(f"/api/audits/{audit['id']}").json()
    assert again["status"] == "completed" and again["score"] == 100.0 and again["error"] is None


def test_9b_unparseable_answers_fail_after_retries(client, model):
    model.queue("not json", "{still not", "nope")
    audit = run(client)
    assert audit["status"] == "failed" and len(model.calls) == vision.MAX_ATTEMPTS
    assert "could not be understood" in audit["error"] and audit["score"] is None


def test_9c_missing_api_key_is_reported_clearly(client, model):
    model.queue(vision.MissingAPIKeyError("OPENROUTER_API_KEY is not set. Add it to your .env file."))
    audit = run(client)
    assert audit["status"] == "failed" and "OPENROUTER_API_KEY" in audit["error"]


def test_9d_unexpected_crash_never_leaves_an_audit_processing(client, model):
    model.queue(RuntimeError("boom"))
    audit = run(client)
    assert audit["status"] == "failed" and "Something went wrong" in audit["error"]


def test_9e_nothing_detected_needs_review_instead_of_zero_percent(client, model):
    model.queue(reply((1, []), (2, []), notes="The shelf is empty."))
    audit = run(client)
    assert audit["status"] == "needs_review" and audit["score"] is None
    assert "No products were detected" in audit["error"]
    assert audit["missing_units"] == 0 and audit["tasks"] == []


def test_9f_unusable_photo_reported_by_the_model_needs_review(client, model):
    model.queue(reply(visible=False, issues=["photo is too dark"]))
    audit = run(client)
    assert audit["status"] == "needs_review" and audit["score"] is None
    assert "too dark" in audit["error"]


def test_9g_only_uncertain_detections_need_review(client, model):
    model.queue(reply((1, [item("something red", 6, "Chips bag", 0.2)]), (2, [item("boxes", 4, None, 0.3)])))
    audit = run(client)
    assert audit["status"] == "needs_review" and audit["score"] is None
    assert audit["uncertain_units"] == 10 and audit["detected_units"] == 0


def test_9i_a_mostly_uncertain_reply_is_not_published_as_a_score(client, model):
    """Modelled on a real run: a small, crowded rack photo where the AI honestly reported
    confidence between 0.3 and 0.5 for nearly everything. That used to publish '50%' with
    products declared missing while they sat in the uncertain pile."""
    model.queue(reply(
        (1, [item("chips bags (brand not legible)", 6, "Chips bag", 0.5)]),
        (2, [item("assorted snack bags", 4, None, 0.4), item("white boxed packs", 3, None, 0.35)]),
        (3, [item("blue and red chips bags", 5, None, 0.4), item("glass bottles", 3, "Chips bag", 0.4)]),
        (4, [item("bottles and jars", 5, None, 0.3)]),
    ))
    audit = run(client)
    assert audit["status"] == "needs_review" and audit["score"] is None
    assert audit["uncertain_units"] == 20 and audit["detected_units"] == 6
    assert "too uncertain" in audit["error"]
    assert audit["tasks"] == [] and audit["missing_units"] == 0


def test_9h_detections_that_match_nothing_need_review(client, model):
    model.queue(reply((1, [item("blue widgets", 5, None, 0.9), item("green gadgets", 3, None, 0.9)])))
    audit = run(client)
    assert audit["status"] == "needs_review" and "none match a planogram product" in audit["error"]


# ---- 10. Refreshing after a completed audit -----------------------------------------------


def test_10_completed_audit_survives_refresh_and_restart(client, model, tmp_path):
    model.queue(stocked_grocery_reply())
    audit = run(client)
    audit_id = audit["id"]

    again = client.get(f"/api/audits/{audit_id}").json()  # a page refresh
    assert again["status"] == "completed" and again["score"] == 100.0

    db.configure(tmp_path / "data")  # a server restart: a brand new connection to the same files
    restarted = client.get(f"/api/audits/{audit_id}").json()
    assert restarted["score"] == 100.0 and restarted["result_json"] == audit["result_json"]
    assert client.get(f"/audits/{audit_id}/image").status_code == 200
    listed = client.get("/api/audits").json()
    assert listed["total"] == 1 and listed["audits"][0]["id"] == audit_id
