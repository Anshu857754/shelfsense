"""Compliance engine tests. Pure Python: no network, no model, no database."""

import pytest

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


def make_shelf(*rows, **extra) -> DetectedShelf:
    """rows: (row_number, [(name, qty) | (name, qty, {extra fields})])"""
    out = []
    for r, prods in rows:
        items = []
        for p in prods:
            entry = {"name": p[0], "quantity": p[1]}
            if len(p) > 2:
                entry.update(p[2])
            items.append(entry)
        out.append({"row": r, "products": items})
    return DetectedShelf.model_validate({"rows": out, **extra})


PERFECT = [(1, [("Coca-Cola 500ml", 6), ("Pepsi 500ml", 4)]), (2, [("Sprite 500ml", 5)])]


# ---- name matching ---------------------------------------------------------------


def test_normalize_handles_case_punctuation_and_units():
    assert normalize("Coca-Cola 500 ml") == "coca cola 500ml"
    assert normalize("  PEPSI   500ML ") == "pepsi 500ml"


def test_similar_spellings_score_100():
    assert similarity("Coca-Cola 500 ml", "coca cola 500ml") == 100


# ---- scoring: the formula ---------------------------------------------------------


def test_perfect_shelf_scores_100_with_no_issues():
    result = audit(make_planogram(), make_shelf(*PERFECT))
    assert result.score == 100 and result.complete
    assert result.gaps == [] and result.misplaced == [] and result.unexpected == []
    assert result.tasks == [] and result.actions == []
    assert result.detected_units == 15


def test_score_is_correct_facings_over_expected_facings():
    shelf = make_shelf((1, [("Coca-Cola 500ml", 6), ("Pepsi 500ml", 2)]), (2, [("Sprite 500ml", 5)]))
    result = audit(make_planogram(), shelf)
    assert [r.score for r in result.rows] == [80.0, 100.0]
    assert result.score == 86.7  # (80*10 + 100*5) / 15
    assert result.missing_units == 2
    gap = result.gaps[0]
    assert (gap.row, gap.product, gap.expected, gap.found, gap.missing) == (1, "Pepsi 500ml", 4, 2, 2)


def test_misplaced_item_is_found_in_the_wrong_row_and_costs_points():
    shelf = make_shelf(
        (1, [("Coca-Cola 500ml", 6), ("Pepsi 500ml", 4), ("Sprite 500ml", 1)]),
        (2, [("Sprite 500ml", 4)]),
    )
    result = audit(make_planogram(), shelf)
    m = result.misplaced[0]
    assert (m.product, m.found_in_row, m.belongs_in_row, m.facings) == ("Sprite 500ml", 1, 2, 1)
    assert [r.score for r in result.rows] == [95.0, 80.0]  # row 1 loses 5 points
    assert result.score == 90.0
    assert result.misplaced_units == 1


def test_unknown_product_is_unexpected_and_costs_points():
    shelf = make_shelf(
        (1, [("Coca-Cola 500ml", 6), ("Pepsi 500ml", 4), ("Mystery Juice 1L", 2)]),
        (2, [("Sprite 500ml", 5)]),
    )
    result = audit(make_planogram(), shelf)
    assert [u.name for u in result.unexpected] == ["Mystery Juice 1L"]
    assert result.unexpected_units == 2
    assert result.score == 96.7


def test_more_than_expected_is_overstock_without_extra_credit():
    shelf = make_shelf((1, [("Coca-Cola 500ml", 9), ("Pepsi 500ml", 4)]), (2, [("Sprite 500ml", 5)]))
    result = audit(make_planogram(), shelf)
    assert result.score == 100
    assert [(o.product, o.found, o.expected, o.extra) for o in result.overstock] == [("Coca-Cola 500ml", 9, 6, 3)]
    assert [(t.kind, t.priority) for t in result.tasks] == [("correct", "low")]
    assert "remove 3" in result.tasks[0].text


def test_item_in_a_row_missing_from_the_planogram_costs_overall_points():
    result = audit(make_planogram(), make_shelf(*PERFECT, (3, [("Coca-Cola 500ml", 2)])))
    assert result.misplaced[0].found_in_row == 3 and result.misplaced[0].belongs_in_row == 1
    assert result.score == 95.0


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


# ---- name matching: aliases, look-alikes, and the model's own planogram match -------


def test_alias_matches_a_nickname():
    result = audit(make_planogram(), make_shelf((1, [("COKE 500 ML", 6), ("Pepsi 500ml", 4)]), (2, [("Sprite 500ml", 5)])))
    assert result.score == 100 and result.unexpected == []


def test_pepsi_max_is_not_confused_with_pepsi():
    shelf = make_shelf((1, [("Coca-Cola 500ml", 6), ("Pepsi Max 500ml", 4)]), (2, [("Sprite 500ml", 5)]))
    result = audit(make_planogram(), shelf)
    assert [u.name for u in result.unexpected] == ["Pepsi Max 500ml"]
    assert any(g.product == "Pepsi 500ml" and g.missing == 4 for g in result.gaps)


def test_model_named_planogram_product_resolves_generic_descriptions():
    """The bug that gave 0 detected / 12 missing: the model says 'Doritos bags', the planogram
    says 'Chips bag'. Plain string matching cannot link them; the model's own match can."""
    planogram = Planogram.model_validate(
        {"rows": [{"row": 1, "expected": [{"product": "Chips bag", "facings": 6}, {"product": "Glass bottle", "facings": 4}]}]}
    )
    shelf = make_shelf(
        (1, [
            ("Doritos bags (top display)", 6, {"matched_expected": "Chips bag", "confidence": 0.9}),
            ("Tall green bottles", 4, {"matched_expected": "Glass bottle", "confidence": 0.8}),
        ])
    )
    result = audit(planogram, shelf)
    assert result.score == 100 and result.missing_units == 0 and result.unexpected == []
    # Without the model's match, the same names are unrecognisable: that is why the old
    # pipeline scored such a shelf 0%.
    no_hint = make_shelf((1, [("Doritos bags (top display)", 6), ("Tall green bottles", 4)]))
    assert audit(planogram, no_hint).complete is False


def test_matched_expected_that_is_not_in_the_planogram_falls_back_to_the_name():
    shelf = make_shelf((1, [("Coca-Cola 500ml", 6, {"matched_expected": "Something Else"}), ("Pepsi 500ml", 4)]), (2, [("Sprite 500ml", 5)]))
    assert audit(make_planogram(), shelf).score == 100


def test_manual_assignment_beats_the_name():
    shelf = make_shelf(
        (1, [("Coca-Cola 500ml", 6), ("red can", 4, {"assigned_to": "Pepsi 500ml", "source": "user"})]),
        (2, [("Sprite 500ml", 5)]),
    )
    result = audit(make_planogram(), shelf)
    assert result.score == 100 and result.unexpected == []


# ---- confidence: uncertain detections are kept apart -------------------------------


def test_low_confidence_detections_are_not_counted():
    shelf = make_shelf(
        (1, [("Coca-Cola 500ml", 6, {"confidence": 0.9}), ("Pepsi 500ml", 4, {"confidence": 0.3})]),
        (2, [("Sprite 500ml", 5)]),
    )
    result = audit(make_planogram(), shelf)
    assert result.uncertain_units == 4
    assert result.detected_units == 11  # the uncertain 4 are not included
    assert any(g.product == "Pepsi 500ml" and g.found == 0 for g in result.gaps)
    statuses = {(d.name, d.status) for d in result.detections}
    assert ("Pepsi 500ml", "uncertain") in statuses
    assert any("too uncertain" in w for w in result.warnings)


def test_unrated_detections_are_counted_and_confidence_is_not_made_up():
    result = audit(make_planogram(), make_shelf(*PERFECT))  # no confidences reported
    assert result.confidence is None
    assert result.detected_units == 15


def test_reported_confidence_is_averaged():
    shelf = make_shelf((1, [("Coca-Cola 500ml", 6, {"confidence": 0.9}), ("Pepsi 500ml", 4, {"confidence": 0.7})]), (2, [("Sprite 500ml", 5, {"confidence": 0.8})]))
    assert audit(make_planogram(), shelf).confidence == 0.8


def test_user_accepted_entries_count_even_with_low_confidence():
    shelf = make_shelf((1, [("Coca-Cola 500ml", 6), ("Pepsi 500ml", 4, {"confidence": 0.2, "source": "user"})]), (2, [("Sprite 500ml", 5)]))
    result = audit(make_planogram(), shelf)
    assert result.uncertain_units == 0 and result.score == 100


def test_mostly_uncertain_shelf_is_held_for_review_not_scored():
    shelf = make_shelf((1, [
        ("Coca-Cola 500ml", 2, {"confidence": 0.9}),
        ("brown bottles", 6, {"confidence": 0.3, "matched_expected": "Pepsi 500ml"}),
    ]))
    result = audit(make_planogram(), shelf)
    assert result.complete is False and result.score is None
    assert "too uncertain" in result.incomplete_reason
    assert result.gaps == [] and result.tasks == []  # Pepsi is not declared "missing": it may be in the pile
    got = {d.name: d for d in result.detections}
    assert got["Coca-Cola 500ml"].status == "short"  # counted, but only 2 of the 6 expected
    assert got["brown bottles"].status == "uncertain" and got["brown bottles"].matched_product == "Pepsi 500ml"  # a suggestion


def test_accepting_the_uncertain_items_completes_the_audit():
    shelf = make_shelf((1, [
        ("Coca-Cola 500ml", 6, {"confidence": 0.9}),
        ("brown bottles", 4, {"confidence": 0.3, "matched_expected": "Pepsi 500ml", "source": "user"}),
    ]), (2, [("Sprite 500ml", 5)]))
    result = audit(make_planogram(), shelf)
    assert result.complete and result.score == 100 and result.uncertain_units == 0


def test_a_small_uncertain_share_does_not_block_the_score():
    shelf = make_shelf((1, [("Coca-Cola 500ml", 6), ("Pepsi 500ml", 4, {"confidence": 0.3})]), (2, [("Sprite 500ml", 5)]))
    result = audit(make_planogram(), shelf)  # 4 of 15 units uncertain = 27%
    assert result.complete and result.uncertain_units == 4


# ---- detection failure is not a score ----------------------------------------------


def test_no_products_detected_is_incomplete_not_zero_percent():
    result = audit(make_planogram(), DetectedShelf(rows=[]))
    assert result.complete is False and result.score is None
    assert result.gaps == [] and result.tasks == []  # nothing is claimed "missing"
    assert "No products were detected" in result.incomplete_reason


def test_only_uncertain_detections_is_incomplete():
    shelf = make_shelf((1, [("Coca-Cola 500ml", 6, {"confidence": 0.2})]))
    result = audit(make_planogram(), shelf)
    assert result.complete is False and result.score is None
    assert result.uncertain_units == 6
    assert "enough confidence" in result.incomplete_reason


def test_unusable_photo_is_incomplete_with_the_models_reasons():
    shelf = DetectedShelf(rows=[], shelf_visible=False, image_issues=["too dark", "not a shelf"])
    result = audit(make_planogram(), shelf)
    assert result.complete is False and result.score is None
    assert "too dark" in result.incomplete_reason
    assert any("too dark" in w for w in result.warnings)


def test_nothing_matching_the_planogram_is_incomplete():
    shelf = make_shelf((1, [("Blue widgets", 5), ("Green gadgets", 3)]))
    result = audit(make_planogram(), shelf)
    assert result.complete is False and result.score is None
    assert "none match a planogram product" in result.incomplete_reason
    assert result.detections  # the user can still see (and correct) what was found


def test_confirmed_empty_shelf_is_a_real_zero():
    result = audit(make_planogram(), DetectedShelf(rows=[]), allow_empty=True)
    assert result.complete and result.score == 0
    assert sum(g.missing for g in result.gaps) == 15
    assert result.missing_units == 15


# ---- to-do list --------------------------------------------------------------------


def test_empty_product_is_high_priority_and_listed_first():
    shelf = make_shelf((1, [("Coca-Cola 500ml", 5)]), (2, [("Sprite 500ml", 5)]))
    tasks = audit(make_planogram(), shelf).tasks
    assert [(t.kind, t.priority) for t in tasks] == [("restock", "high"), ("restock", "low")]
    assert "Pepsi 500ml" in tasks[0].text and "Coca-Cola 500ml" in tasks[1].text


def test_gap_priority_levels():
    shelf = make_shelf((1, [("Coca-Cola 500ml", 4), ("Pepsi 500ml", 2)]), (2, [("Sprite 500ml", 5)]))
    by_text = {t.text: t.priority for t in audit(make_planogram(), shelf).tasks}
    assert by_text["Restock 2 x Coca-Cola 500ml in row 1"] == "medium"  # a third missing
    assert by_text["Restock 2 x Pepsi 500ml in row 1"] == "high"  # half missing


def test_move_is_high_when_it_fills_a_gap_in_its_own_row():
    shelf = make_shelf((1, [("Coca-Cola 500ml", 6), ("Pepsi 500ml", 4), ("Sprite 500ml", 1)]), (2, [("Sprite 500ml", 4)]))
    tasks = audit(make_planogram(), shelf).tasks
    assert (tasks[0].kind, tasks[0].priority) == ("move", "high")
    assert (tasks[1].kind, tasks[1].priority) == ("restock", "low")


def test_move_is_medium_when_nothing_is_missing():
    shelf = make_shelf((1, [("Coca-Cola 500ml", 6), ("Pepsi 500ml", 4), ("Sprite 500ml", 1)]), (2, [("Sprite 500ml", 5)]))
    assert [(t.kind, t.priority) for t in audit(make_planogram(), shelf).tasks] == [("move", "medium")]


def test_unexpected_item_becomes_a_remove_task():
    shelf = make_shelf((1, [("Coca-Cola 500ml", 6), ("Pepsi 500ml", 4), ("Mystery Juice", 2)]), (2, [("Sprite 500ml", 5)]))
    result = audit(make_planogram(), shelf)
    assert [(t.kind, t.priority) for t in result.tasks] == [("remove", "low")]
    assert result.tasks[0].text == "Remove 'Mystery Juice' from row 1: it is not in the planogram"
    assert result.actions == [t.text for t in result.tasks]


# ---- planogram vs shelf, and detections --------------------------------------------


def test_comparison_lists_found_units_and_extras_per_row():
    shelf = make_shelf(
        (1, [("Coca-Cola 500ml", 6), ("Pepsi 500ml", 2), ("Sprite 500ml", 1)]),
        (2, [("Sprite 500ml", 4)]),
        (3, [("Coca-Cola 500ml", 2)]),
    )
    cmp = {c.row: c for c in audit(make_planogram(), shelf).comparison}
    assert [(i.product, i.expected, i.found) for i in cmp[1].items] == [("Coca-Cola 500ml", 6, 6), ("Pepsi 500ml", 4, 2)]
    assert [(x.name, x.kind, x.belongs_in_row) for x in cmp[1].extras] == [("Sprite 500ml", "misplaced", 2)]
    assert cmp[2].items[0].found == 4 and cmp[2].score is not None
    assert cmp[3].items == [] and cmp[3].score is None and cmp[3].extras[0].belongs_in_row == 1


def test_detection_statuses_boxes_and_matched_product():
    box = {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}
    shelf = make_shelf(
        (1, [("Coca-Cola 500ml", 6, {"box": box}), ("Pepsi 500ml", 2), ("Sprite 500ml", 1), ("Mystery Juice", 2)]),
        (2, [("Sprite 500ml", 5)]),
    )
    got = {(d.row, d.name): d for d in audit(make_planogram(), shelf).detections}
    assert got[(1, "Coca-Cola 500ml")].status == "ok" and got[(1, "Coca-Cola 500ml")].box.w == 0.2
    assert got[(1, "Pepsi 500ml")].status == "short"
    assert got[(1, "Sprite 500ml")].status == "misplaced" and got[(1, "Sprite 500ml")].matched_product == "Sprite 500ml"
    assert got[(1, "Mystery Juice")].status == "unexpected"
    assert got[(2, "Sprite 500ml")].status == "ok"


def test_zero_quantity_entries_are_ignored():
    shelf = make_shelf((1, [("Coca-Cola 500ml", 6), ("Pepsi 500ml", 4), ("Fanta", 0)]), (2, [("Sprite 500ml", 5)]))
    result = audit(make_planogram(), shelf)
    assert result.unexpected == [] and result.score == 100


@pytest.mark.parametrize("threshold", [0.0, 1.0])
def test_confidence_threshold_extremes(threshold):
    shelf = make_shelf((1, [("Coca-Cola 500ml", 6, {"confidence": 0.99}), ("Pepsi 500ml", 4, {"confidence": 0.99})]), (2, [("Sprite 500ml", 5, {"confidence": 0.99})]))
    result = audit(make_planogram(), shelf, confidence_threshold=threshold)
    # 0.0 counts everything; 1.0 makes every reported confidence "below the bar".
    assert result.complete == (threshold == 0.0)
