"""Looking at a photo several times and using the middle result."""

import asyncio

import pytest

from app import vision
from app.compliance import audit, pick_median_pass
from app.config import Settings, get_settings
from app.schemas import DetectedShelf, Planogram
from evaluation import run_eval
from factories import item, reply, shelf_image
from test_evaluation import PLANOGRAM, SETTINGS, make_case
from test_scenarios import GROCERY, run

PLAN = Planogram.model_validate(GROCERY)


def shelf_with(chips=6, cereal=4, biscuit=5, noodles=4) -> DetectedShelf:
    return DetectedShelf.model_validate({"rows": [
        {"row": 1, "products": [{"name": "Chips bag", "quantity": chips}, {"name": "Cereal box", "quantity": cereal}]},
        {"row": 2, "products": [{"name": "Biscuit pack", "quantity": biscuit}, {"name": "Instant noodles", "quantity": noodles}]},
    ]})


def grocery_reply(chips=6, cereal=4, biscuit=5, noodles=4) -> str:
    return reply(
        (1, [item("chips", chips, "Chips bag"), item("cereal", cereal, "Cereal box")]),
        (2, [item("biscuits", biscuit, "Biscuit pack"), item("noodles", noodles, "Instant noodles")]),
    )


# ---- choosing the middle look ----------------------------------------------------------------


def test_the_middle_scoring_look_is_chosen():
    shelves = [shelf_with(), shelf_with(chips=3), shelf_with(chips=0)]  # 100, 84.2, 68.4
    index, scores = pick_median_pass(PLAN, shelves)
    assert index == 1 and scores == [68.4, 84.2, 100.0]


def test_the_order_of_the_looks_does_not_matter():
    shelves = [shelf_with(chips=0), shelf_with(), shelf_with(chips=3)]
    index, scores = pick_median_pass(PLAN, shelves)
    assert index == 2 and scores == [68.4, 84.2, 100.0]


def test_an_even_number_of_looks_takes_the_lower_middle():
    index, scores = pick_median_pass(PLAN, [shelf_with(), shelf_with(chips=0)])
    assert index == 1 and scores == [68.4, 100.0]  # the cautious one of the two


def test_looks_that_cannot_be_scored_are_ignored():
    unusable = DetectedShelf(rows=[], shelf_visible=False)
    index, scores = pick_median_pass(PLAN, [unusable, shelf_with(chips=3), unusable])
    assert index == 1 and scores == [84.2]
    assert pick_median_pass(PLAN, [unusable, unusable]) == (0, [])


# ---- trust follows agreement ------------------------------------------------------------------


@pytest.mark.parametrize(
    "scores, level, fragment",
    [
        ([84.0, 85.0, 86.0], "high", "agreed closely"),
        ([80.0, 84.0, 89.0], "medium", "differed by 9 points"),
        ([60.0, 84.0, 100.0], "low", "disagreed (scores 60% to 100%)"),
    ],
)
def test_disagreement_between_looks_lowers_the_trust(scores, level, fragment):
    result = audit(PLAN, shelf_with(chips=3), pass_scores=scores)
    assert result.pass_scores == scores
    assert result.trust_level == level and fragment in result.trust_reason
    assert any(f"looked at the photo {len(scores)} times" in line for line in result.summary)


def test_a_single_look_says_nothing_about_agreement():
    result = audit(PLAN, shelf_with(chips=3), pass_scores=[84.2])
    assert result.pass_scores == [] and "looks" not in result.trust_reason
    assert not any("times" in line for line in result.summary)


# ---- the parallel looks ---------------------------------------------------------------------------


def go(model_settings=SETTINGS, passes=3, planogram=PLAN):
    return asyncio.run(vision.detect_best_of(b"img", "image/jpeg", model_settings, passes=passes, planogram=planogram, category="grocery"))


def test_three_looks_make_three_calls_and_keep_the_middle(model):
    model.queue(grocery_reply(), grocery_reply(chips=3), grocery_reply(chips=0))
    shelf, scores = go()
    assert len(model.calls) == 3 and scores == [68.4, 84.2, 100.0]
    assert shelf.rows[0].products[0].facings == 3  # the 84.2 look


def test_one_look_is_the_old_behaviour(model):
    model.queue(grocery_reply())
    shelf, scores = go(passes=1)
    assert len(model.calls) == 1 and scores == [] and shelf.rows[0].products[0].facings == 6


def test_a_failed_look_is_skipped_when_another_worked(model):
    model.queue(vision.OpenRouterError("busy"), grocery_reply(chips=3), grocery_reply())
    shelf, scores = go()
    assert scores == [84.2, 100.0] and len(model.calls) == 3


def test_if_every_look_fails_the_error_is_reported(model):
    model.queue(*[vision.OpenRouterError("OpenRouter returned status 503")] * 3)
    with pytest.raises(vision.OpenRouterError, match="503"):
        go()


# ---- end to end -------------------------------------------------------------------------------------


def test_an_audit_with_three_looks_stores_and_shows_the_agreement(client, model, monkeypatch):
    monkeypatch.setattr(get_settings(), "detect_passes", 3)
    model.queue(grocery_reply(), grocery_reply(chips=3), grocery_reply(chips=0))
    audit_row = run(client)

    assert len(model.calls) == 3 and audit_row["status"] == "completed"
    assert audit_row["score"] == 84.2  # the middle look, not the best or the worst
    result = audit_row["result_json"]
    assert result["pass_scores"] == [68.4, 84.2, 100.0] and result["trust_level"] == "low"
    page = client.get(f"/audits/{audit_row['id']}").text
    assert "looked at the photo 3 times" in page and "disagreed (scores 68.4% to 100%)" in page


def test_the_default_is_a_single_look():
    assert Settings(_env_file=None).detect_passes == 1


def test_the_evaluation_tool_can_compare_one_look_with_three(tmp_path, model):
    cases = run_eval.load_cases(make_case(tmp_path))
    configs = [{**run_eval.DEFAULT_CONFIG, "name": "single", "model": "m", "verify": False},
               {**run_eval.DEFAULT_CONFIG, "name": "triple", "model": "m", "verify": False, "passes": 3}]
    one = reply((1, [item("chips", 6, "Chips bag"), item("cereal", 4, "Cereal box")]), (2, [item("biscuits", 5, "Biscuit pack")]))
    model.queue(one, one, one, one)
    rows = asyncio.run(run_eval.evaluate(cases, configs, base=SETTINGS, progress=lambda _: None))
    assert len(model.calls) == 4 and {r["config"] for r in rows} == {"single", "triple"}
    assert all(r["score"] == 100.0 for r in rows)
