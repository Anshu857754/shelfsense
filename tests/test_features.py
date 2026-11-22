"""Schema parsing: boxes, confidence, quantity, attributes."""

import json

import pytest
from pydantic import ValidationError

from app import vision
from app.schemas import DetectedProduct, DetectedShelf, Planogram


def product(**kw):
    return DetectedProduct.model_validate({"name": "Cola", "facings": 2, **kw})


# ---- bounding boxes ---------------------------------------------------------------


def test_valid_box_is_kept():
    p = product(box={"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4})
    assert (p.box.x, p.box.y, p.box.w, p.box.h) == (0.1, 0.2, 0.3, 0.4)


def test_missing_box_is_fine():
    assert product().box is None


@pytest.mark.parametrize(
    "bad",
    [{"x": 2, "y": 0, "w": 1, "h": 1}, {"x": "left"}, "nope", {"x": 0.1, "y": 0.1, "w": 0, "h": 0.2}],
)
def test_malformed_box_is_dropped_not_fatal(bad):
    assert product(box=bad).box is None


def test_box_running_past_the_edge_is_clamped():
    p = product(box={"x": 0.8, "y": 0.1, "w": 0.5, "h": 0.2})
    assert round(p.box.w, 3) == 0.2


def test_reply_with_a_bad_box_still_parses():
    reply = json.dumps({"rows": [{"row": 1, "products": [{"name": "Cola", "facings": 2, "box": "garbage"}]}]})
    assert vision.parse_shelf_json(reply).rows[0].products[0].box is None


# ---- confidence: taken from the model, never invented -----------------------------


@pytest.mark.parametrize(
    "given, expected",
    [(0.85, 0.85), ("0.7", 0.7), (85, 0.85), ("85%", 0.85), (None, None), ("high", None),
     (True, None), (-0.2, None), (250, None), (float("nan"), None)],
)
def test_confidence_parsing(given, expected):
    assert product(confidence=given).confidence == expected


def test_missing_confidence_stays_missing():
    assert product().confidence is None


# ---- quantity, attributes, names ---------------------------------------------------


def test_quantity_is_accepted_as_facings():
    assert DetectedProduct.model_validate({"name": "Jeans", "quantity": 5}).facings == 5


def test_attributes_string_becomes_a_list_and_is_capped():
    assert product(attributes="red").attributes == ["red"]
    assert len(product(attributes=[str(i) for i in range(20)]).attributes) == 6


def test_blank_matched_expected_means_none():
    assert product(matched_expected="  ").matched_expected is None


def test_fractional_or_negative_quantity_is_rejected():
    for bad in (2.5, -1):
        with pytest.raises(ValidationError):
            product(facings=bad)


def test_blank_product_name_is_rejected():
    with pytest.raises(ValidationError):
        DetectedProduct(name="  ", facings=1)


def test_shelf_visible_defaults_to_true_and_issues_are_a_list():
    shelf = DetectedShelf.model_validate({"rows": [], "image_issues": "too dark"})
    assert shelf.shelf_visible is True and shelf.image_issues == ["too dark"]


def test_planogram_needs_at_least_one_row_and_product():
    with pytest.raises(ValidationError):
        Planogram.model_validate({"rows": []})
    with pytest.raises(ValidationError):
        Planogram.model_validate({"rows": [{"row": 1, "expected": []}]})
