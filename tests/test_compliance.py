"""Compliance engine tests. Pure Python: no network, no model."""

from app.compliance import audit, normalize, similarity
from app.schemas import DetectedShelf, Planogram


def make_planogram() -> Planogram:
    return Planogram.model_validate(
        {
            "rows": [
                {
                    "row": 1,
                    "expected": [
                        {"product": "Coca-Cola 500ml", "facings": 6, "aliases": ["Coke 500ml"]},
                        {"product": "Pepsi 500ml", "facings": 4},
                    ],
                },
                {"row": 2, "expected": [{"product": "Sprite 500ml", "facings": 5}]},
            ]
        }
    )


def make_shelf(*rows) -> DetectedShelf:
    """rows: (row_number, [(name, facings), ...])"""
    return DetectedShelf.model_validate(
        {
            "rows": [
                {"row": r, "products": [{"name": n, "facings": f} for n, f in prods]}
                for r, prods in rows
            ]
        }
    )


PERFECT = [
    (1, [("Coca-Cola 500ml", 6), ("Pepsi 500ml", 4)]),
    (2, [("Sprite 500ml", 5)]),
]


# ---- name matching ---------------------------------------------------------------


def test_normalize_handles_case_punctuation_and_units():
    assert normalize("Coca-Cola 500 ml") == "coca cola 500ml"
    assert normalize("  PEPSI   500ML ") == "pepsi 500ml"


def test_similar_spellings_score_100():
    assert similarity("Coca-Cola 500 ml", "coca cola 500ml") == 100


# ---- scoring ---------------------------------------------------------------------


def test_perfect_shelf_scores_100_with_no_issues():
    result = audit(make_planogram(), make_shelf(*PERFECT))
    assert result.score == 100
    assert result.gaps == [] and result.misplaced == [] and result.unexpected == []
    assert result.actions == []


def test_missing_units_are_reported_as_gaps():
    shelf = make_shelf((1, [("Coca-Cola 500ml", 6), ("Pepsi 500ml", 2)]), (2, [("Sprite 500ml", 5)]))
    result = audit(make_planogram(), shelf)
    assert [r.score for r in result.rows] == [80.0, 100.0]
    assert result.score == 86.7  # (80*10 + 100*5) / 15
    assert len(result.gaps) == 1
    gap = result.gaps[0]
    assert (gap.row, gap.product, gap.expected, gap.found, gap.missing) == (
        1, "Pepsi 500ml", 4, 2, 2
    )
    assert "Restock 2 x Pepsi 500ml in row 1" in result.actions


def test_item_in_wrong_row_is_misplaced_and_costs_points():
    shelf = make_shelf(
        (1, [("Coca-Cola 500ml", 6), ("Pepsi 500ml", 4), ("Sprite 500ml", 1)]),
        (2, [("Sprite 500ml", 4)]),
    )
    result = audit(make_planogram(), shelf)
    assert len(result.misplaced) == 1
    m = result.misplaced[0]
    assert (m.product, m.found_in_row, m.belongs_in_row, m.facings) == ("Sprite 500ml", 1, 2, 1)
    # Row 1: 100 - 5 penalty = 95. Row 2: 4 of 5 = 80. Overall (95*10 + 80*5) / 15 = 90.
    assert [r.score for r in result.rows] == [95.0, 80.0]
    assert result.score == 90.0
    assert result.gaps[0].product == "Sprite 500ml" and result.gaps[0].missing == 1
    assert "Move 1 x Sprite 500ml from row 1 to row 2" in result.actions


def test_unknown_product_is_reported_as_unexpected():
    shelf = make_shelf(
        (1, [("Coca-Cola 500ml", 6), ("Pepsi 500ml", 4), ("Mystery Juice 1L", 2)]),
        (2, [("Sprite 500ml", 5)]),
    )
    result = audit(make_planogram(), shelf)
    assert len(result.unexpected) == 1
    assert result.unexpected[0].name == "Mystery Juice 1L"
    assert result.score == 96.7  # row 1 = 95, row 2 = 100


def test_alias_matches_a_nickname():
    shelf = make_shelf((1, [("COKE 500 ML", 6), ("Pepsi 500ml", 4)]), (2, [("Sprite 500ml", 5)]))
    result = audit(make_planogram(), shelf)
    assert result.score == 100
    assert result.unexpected == []


def test_pepsi_max_is_not_confused_with_pepsi():
    shelf = make_shelf(
        (1, [("Coca-Cola 500ml", 6), ("Pepsi Max 500ml", 4)]), (2, [("Sprite 500ml", 5)])
    )
    result = audit(make_planogram(), shelf)
    assert [u.name for u in result.unexpected] == ["Pepsi Max 500ml"]
    assert any(g.product == "Pepsi 500ml" and g.missing == 4 for g in result.gaps)


def test_extra_units_earn_no_extra_credit():
    shelf = make_shelf((1, [("Coca-Cola 500ml", 9), ("Pepsi 500ml", 4)]), (2, [("Sprite 500ml", 5)]))
    result = audit(make_planogram(), shelf)
    assert result.rows[0].correct_facings == 10
    assert result.score == 100


def test_empty_detection_scores_zero_with_gaps_for_everything():
    result = audit(make_planogram(), DetectedShelf(rows=[]))
    assert result.score == 0
    assert len(result.gaps) == 3
    assert sum(g.missing for g in result.gaps) == 15


def test_zero_facing_products_are_ignored():
    shelf = make_shelf((1, [("Coca-Cola 500ml", 6), ("Pepsi 500ml", 4), ("Fanta", 0)]), (2, [("Sprite 500ml", 5)]))
    result = audit(make_planogram(), shelf)
    assert result.unexpected == []
    assert result.score == 100


def test_item_in_a_row_missing_from_the_planogram_costs_overall_points():
    shelf = make_shelf(*PERFECT, (3, [("Coca-Cola 500ml", 2)]))
    result = audit(make_planogram(), shelf)
    assert result.misplaced[0].found_in_row == 3
    assert result.misplaced[0].belongs_in_row == 1
    assert result.score == 95.0  # 100 minus one 5-point penalty


def test_same_product_in_two_rows_prefers_the_row_it_was_seen_in():
    planogram = Planogram.model_validate(
        {
            "rows": [
                {"row": 1, "expected": [{"product": "Water 1L", "facings": 3}]},
                {"row": 2, "expected": [{"product": "Water 1L", "facings": 3}]},
            ]
        }
    )
    result = audit(planogram, make_shelf((2, [("Water 1L", 3)])))
    assert result.misplaced == []
    assert [(g.row, g.missing) for g in result.gaps] == [(1, 3)]
