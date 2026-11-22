"""Names the AI invents, JSON it wraps in extra words: the two things that caused avoidable errors."""

import json

import pytest

from app import vision
from app.compliance import audit
from app.schemas import DetectedShelf, Planogram

CLOTHES = Planogram.model_validate({"rows": [
    {"row": 1, "expected": [{"product": "Shirt", "facings": 6, "aliases": ["t-shirt"]}, {"product": "Sweater", "facings": 6, "aliases": ["knitwear"]}]},
    {"row": 2, "expected": [{"product": "Jeans", "facings": 4}, {"product": "Jacket", "facings": 3}]},
]})


def seen(*rows) -> DetectedShelf:
    return DetectedShelf.model_validate({"rows": [
        {"row": r, "products": [{"name": n, "quantity": q} for n, q in prods]} for r, prods in rows]})


# ---- matching by the word that names the product ----------------------------------------------


@pytest.mark.parametrize("name, product", [
    ("brown folded sweaters", "Sweater"),
    ("white folded shirts", "Shirt"),
    ("SHIRTS (grey checked)", "Shirt"),
    ("blue denim jeans", "Jeans"),
    ("black leather jackets", "Jacket"),
])
def test_a_descriptive_name_containing_the_product_word_is_matched(name, product):
    s = seen((1, [(name, 3)])) if product in ("Shirt", "Sweater") else seen((2, [(name, 3)]))
    detection = audit(CLOTHES, s, allow_empty=True).detections[0]
    assert detection.status in ("ok", "short") and detection.matched_product == product


def test_the_clothing_wall_run_that_gave_no_score_now_scores():
    """Real case: the AI described piles as 'brown folded sweaters' without naming the planogram product."""
    s = seen((1, [("brown folded sweaters", 5), ("grey checked folded shirts", 6)]), (2, [("blue denim jeans", 4), ("denim jackets", 3)]))
    result = audit(CLOTHES, s)
    assert result.complete and result.unexpected == []
    assert [g.product for g in result.gaps] == ["Sweater"]  # 5 of 6


def test_ambiguous_or_unrelated_names_are_still_not_matched():
    only_unknown = audit(CLOTHES, seen((1, [("shirt jacket combo", 2)])), allow_empty=True)
    assert only_unknown.detections[0].status == "unexpected"  # fits both Shirt and Jacket: not guessed
    assert audit(CLOTHES, seen((1, [("red scarves", 2)])), allow_empty=True).detections[0].status == "unexpected"


def test_short_words_do_not_count():
    planogram = Planogram.model_validate({"rows": [{"row": 1, "expected": [{"product": "Hat", "facings": 3}]}]})
    assert audit(planogram, seen((1, [("that grey beanie", 2)])), allow_empty=True).detections[0].status == "unexpected"


def test_look_alike_products_with_several_words_are_still_kept_apart():
    drinks = Planogram.model_validate({"rows": [{"row": 1, "expected": [{"product": "Pepsi 500ml", "facings": 4}]}]})
    result = audit(drinks, seen((1, [("Pepsi Max 500ml", 4)])), allow_empty=True)
    assert result.detections[0].status == "unexpected"


# ---- reading the AI's reply ----------------------------------------------------------------------

SHELF = {"shelf_visible": True, "rows": [{"row": 1, "products": [{"name": "chips", "quantity": 3}]}]}


@pytest.mark.parametrize("reply", [
    json.dumps(SHELF),
    "```json\n" + json.dumps(SHELF) + "\n```",
    "Here is the result: " + json.dumps(SHELF) + " Hope that helps!",
    json.dumps(SHELF) + "\n\nNote: use {row} for the row number, e.g. {1}.",   # extra braces after the answer
    json.dumps(SHELF) + "\n" + json.dumps({"rows": []}),                        # a second object after the first
])
def test_the_first_complete_json_object_is_used_whatever_surrounds_it(reply):
    assert vision.parse_shelf_json(reply).rows[0].products[0].facings == 3


@pytest.mark.parametrize("reply", ["no json here", "[1, 2, 3]", '{"rows": [', "{}"])
def test_replies_without_a_usable_object_are_still_rejected(reply):
    with pytest.raises(ValueError):
        vision.parse_shelf_json(reply)


def test_trailing_text_no_longer_costs_a_second_ai_call(model):
    import asyncio
    from app.config import Settings

    model.queue(json.dumps(SHELF) + "\nNote: {this} is extra.")
    shelf = asyncio.run(vision.detect_shelf(b"x", "image/jpeg", Settings(_env_file=None, openrouter_api_key="k", openrouter_model="m")))
    assert len(model.calls) == 1 and shelf.rows[0].products[0].facings == 3
