import pytest
from pydantic import ValidationError

from app.schemas import DetectedProduct, DetectedRow, DetectedShelf


def test_valid_shelf_parses_and_fills_defaults():
    shelf = DetectedShelf.model_validate(
        {"rows": [{"row": 1, "products": [{"name": " Coca-Cola 500ml ", "facings": 6}]}]}
    )
    row = shelf.rows[0]
    assert row.products[0].name == "Coca-Cola 500ml"  # whitespace stripped
    assert row.empty_gaps == 0
    assert row.misplaced_items == []
    assert shelf.overall_notes == ""


def test_numeric_strings_are_accepted():
    product = DetectedProduct.model_validate({"name": "Sprite", "facings": "4"})
    assert product.facings == 4


def test_negative_facings_rejected():
    with pytest.raises(ValidationError):
        DetectedProduct(name="Sprite", facings=-1)


def test_fractional_facings_rejected():
    with pytest.raises(ValidationError):
        DetectedProduct.model_validate({"name": "Sprite", "facings": 2.5})


def test_blank_product_name_rejected():
    with pytest.raises(ValidationError):
        DetectedProduct(name="   ", facings=1)


def test_row_number_must_start_at_one():
    with pytest.raises(ValidationError):
        DetectedRow(row=0)


def test_negative_gaps_rejected():
    with pytest.raises(ValidationError):
        DetectedRow(row=1, empty_gaps=-2)


def test_rows_are_required():
    with pytest.raises(ValidationError):
        DetectedShelf.model_validate({"overall_notes": "no rows key"})
