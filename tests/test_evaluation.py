"""The evaluation harness and the faster prompt levels. No network: the AI is scripted."""

import asyncio
import json
import re
import shutil
from pathlib import Path

import httpx
import pytest

from app import vision
from app.config import Settings
from app.schemas import Planogram
from evaluation import metrics, run_eval
from factories import item, reply, shelf_image

PLANOGRAM = {
    "rows": [
        {"row": 1, "expected": [{"product": "Chips bag", "facings": 6}, {"product": "Cereal box", "facings": 4}]},
        {"row": 2, "expected": [{"product": "Biscuit pack", "facings": 5}]},
    ]
}
TRUTH_FULL = {"rows": [
    {"row": 1, "products": [{"product": "Chips bag", "facings": 6}, {"product": "Cereal box", "facings": 4}]},
    {"row": 2, "products": [{"product": "Biscuit pack", "facings": 5}]},
]}
SETTINGS = Settings(_env_file=None, openrouter_api_key="k", openrouter_model="m")


def make_case(tmp_path: Path, name="case-a", truth=TRUTH_FULL) -> Path:
    folder = tmp_path / name
    folder.mkdir(parents=True)
    (folder / "photo.jpg").write_bytes(shelf_image())
    case = {"image": "photo.jpg", "category": "grocery", "planogram": PLANOGRAM}
    if truth is not None:
        case["truth"] = truth
    (folder / "case.json").write_text(json.dumps(case), encoding="utf-8")
    return tmp_path


def good_reply(chips=6, cereal=4, biscuit=5):
    return reply((1, [item("chips", chips, "Chips bag"), item("cereal", cereal, "Cereal box")]), (2, [item("biscuits", biscuit, "Biscuit pack")]))


# ---- metrics ------------------------------------------------------------------------------


def test_the_truth_is_scored_with_the_same_engine_as_the_app(tmp_path):
    case = {"planogram": PLANOGRAM, "truth": {"rows": [
        {"row": 1, "products": [{"product": "Chips bag", "facings": 3}, {"product": "Cereal box", "facings": 4}]},
        {"row": 2, "products": [{"product": "Biscuit pack", "facings": 5}]}]}}
    truth = metrics.build_truth(case)
    assert truth.score == 80.0 and truth.missing_units == 3  # 3 of 6 chips: 12 of 15 expected units are correct
    assert metrics.build_truth({"planogram": PLANOGRAM}) is None


def test_a_perfect_run_scores_perfectly(tmp_path):
    from app.compliance import audit
    from app.schemas import DetectedShelf
    truth = metrics.build_truth({"planogram": PLANOGRAM, "truth": TRUTH_FULL})
    shelf = DetectedShelf.model_validate({"rows": [
        {"row": 1, "products": [{"name": "Chips bag", "quantity": 6}, {"name": "Cereal box", "quantity": 4}]},
        {"row": 2, "products": [{"name": "Biscuit pack", "quantity": 5}]}]})
    got = metrics.score_run(audit(Planogram.model_validate(PLANOGRAM), shelf), truth)
    assert got["exact"] == got["items"] == 3 and got["abs_error"] == 0 and got["score_abs_error"] == 0
    assert got["present_found"] == got["present"] and got["false_presence"] == 0


def test_miscounts_and_invented_products_are_counted_as_errors():
    from app.compliance import audit
    from app.schemas import DetectedShelf
    truth = metrics.build_truth({"planogram": PLANOGRAM, "truth": {"rows": [
        {"row": 1, "products": [{"product": "Chips bag", "facings": 6}, {"product": "Cereal box", "facings": 0}]},
        {"row": 2, "products": [{"product": "Biscuit pack", "facings": 5}]}]}})
    shelf = DetectedShelf.model_validate({"rows": [
        {"row": 1, "products": [{"name": "Chips bag", "quantity": 4}, {"name": "Cereal box", "quantity": 3}]},  # chips -2, cereal invented
        {"row": 2, "products": [{"name": "Biscuit pack", "quantity": 5}]}]})
    got = metrics.score_run(audit(Planogram.model_validate(PLANOGRAM), shelf), truth)
    assert (got["exact"], got["within1"], got["abs_error"]) == (1, 1, 5)
    assert got["false_presence"] == 1 and got["absent"] == 1  # cereal is not really there
    assert got["score_abs_error"] > 0


def test_a_run_that_gave_no_score_is_marked_incomplete_not_wrong():
    from app.compliance import audit
    from app.schemas import DetectedShelf
    truth = metrics.build_truth({"planogram": PLANOGRAM, "truth": TRUTH_FULL})
    held = audit(Planogram.model_validate(PLANOGRAM), DetectedShelf(rows=[], shelf_visible=False))
    assert metrics.score_run(held, truth) == {"incomplete": True}
    assert metrics.score_run(None, truth) == {} and metrics.score_run(held, None) == {}


def test_a_truly_empty_shelf_is_judged_on_saying_so():
    from app.compliance import audit
    from app.schemas import DetectedShelf
    empty_truth = metrics.build_truth({"planogram": PLANOGRAM, "truth": {"rows": [
        {"row": 1, "products": [{"product": "Chips bag", "facings": 0}]}]}})
    said_empty = audit(Planogram.model_validate(PLANOGRAM), DetectedShelf(rows=[], shelf_empty=True))
    saw_things = audit(Planogram.model_validate(PLANOGRAM), DetectedShelf.model_validate(
        {"rows": [{"row": 1, "products": [{"name": "Chips bag", "quantity": 3}]}]}))
    assert metrics.score_run(said_empty, empty_truth) == {"truth_empty": True, "empty_correct": True}
    assert metrics.score_run(saw_things, empty_truth)["empty_correct"] is False


def test_summary_numbers():
    runs = [
        {"ok": True, "complete": True, "seconds": 10.0, "tokens": 1000, "units": 15,
         "metrics": {"items": 4, "exact": 3, "within1": 4, "abs_error": 1, "present": 3, "present_found": 3,
                     "absent": 1, "false_presence": 0, "false_unexpected_units": 0, "score_abs_error": 2.0}},
        {"ok": True, "complete": True, "seconds": 20.0, "tokens": 2000, "units": 17,
         "metrics": {"items": 4, "exact": 1, "within1": 3, "abs_error": 5, "present": 3, "present_found": 2,
                     "absent": 1, "false_presence": 1, "false_unexpected_units": 2, "score_abs_error": 6.0}},
        {"ok": False, "complete": False, "seconds": 3.0, "tokens": 0, "units": 0, "metrics": None},
        {"ok": True, "complete": False, "seconds": 12.0, "tokens": 1500, "units": 9, "metrics": {"incomplete": True}},
    ]
    s = metrics.summarize(runs)
    assert s["runs"] == 4 and s["failed_pct"] == 25.0 and s["incomplete_pct"] == 33.3
    assert s["median_s"] == 12.0 and s["p95_s"] == 20.0 and s["tokens"] == 1500
    assert (s["count_exact_pct"], s["count_within1_pct"], s["count_mae"]) == (50.0, 87.5, 0.75)
    assert s["recall_pct"] == 83.3 and s["false_presence_pct"] == 50.0
    assert s["score_mae"] == 4.0 and s["false_unexpected_units"] == 1.0


def test_report_table_lists_every_configuration():
    text = metrics.render_markdown({"fast": metrics.summarize([]), "slow": metrics.summarize([])})
    assert "| Config |" in text and "| fast |" in text and "| slow |" in text and "How to read it" in text


# ---- running cases -------------------------------------------------------------------------


def test_cases_load_and_templates_are_skipped(tmp_path):
    make_case(tmp_path, "real-one")
    shutil.copytree(Path("evaluation/cases/_template"), tmp_path / "_template")
    assert [c["id"] for c in run_eval.load_cases(tmp_path)] == ["real-one"]
    (tmp_path / "real-one" / "photo.jpg").unlink()
    with pytest.raises(FileNotFoundError):
        run_eval.load_cases(tmp_path)


def test_files_saved_by_windows_editors_with_a_bom_still_load(tmp_path):
    make_case(tmp_path)
    case_file = tmp_path / "case-a" / "case.json"
    case_file.write_bytes(b"\xef\xbb\xbf" + case_file.read_bytes())  # what Notepad and PowerShell can add
    assert [c["id"] for c in run_eval.load_cases(tmp_path)] == ["case-a"]
    configs = tmp_path / "c.json"
    configs.write_bytes(b"\xef\xbb\xbf" + json.dumps([{"name": "a", "model": "m"}]).encode())
    assert run_eval.load_configs(configs)[0]["name"] == "a"


def test_duplicate_configuration_names_are_rejected(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps([{"name": "a", "model": "m"}, {"name": "a", "model": "n"}]))
    with pytest.raises(ValueError):
        run_eval.load_configs(path)
    path.write_text(json.dumps([{"name": "a", "model": "m"}]))
    assert run_eval.load_configs(path)[0]["detail"] == "full"  # defaults are filled in


def test_a_full_evaluation_run_end_to_end(tmp_path, model):
    cases = run_eval.load_cases(make_case(tmp_path))
    configs = [{**run_eval.DEFAULT_CONFIG, "name": "accurate", "model": "m1", "verify": False},
               {**run_eval.DEFAULT_CONFIG, "name": "sloppy", "model": "m2", "detail": "lean", "verify": False}]
    model.queue(good_reply(), good_reply(chips=3, biscuit=2))  # run order: accurate, then sloppy
    lines = []
    rows = asyncio.run(run_eval.evaluate(cases, configs, repeats=1, base=SETTINGS, progress=lines.append))

    by = {r["config"]: r for r in rows}
    assert by["accurate"]["score"] == by["accurate"]["true_score"] == 100.0
    assert by["sloppy"]["score"] < 100 and by["sloppy"]["metrics"]["abs_error"] == 6
    assert len(lines) == 2 and "accurate" in lines[0]

    text = run_eval.report(rows, configs)
    assert "| accurate |" in text and "| sloppy |" in text and "## Every run" in text


def test_failures_are_recorded_not_raised(tmp_path, model):
    cases = run_eval.load_cases(make_case(tmp_path))
    model.queue(vision.OpenRouterError("OpenRouter returned status 503"))
    rows = asyncio.run(run_eval.evaluate(cases, [{**run_eval.DEFAULT_CONFIG, "name": "x", "model": "m"}], base=SETTINGS, progress=lambda _: None))
    assert rows[0]["ok"] is False and "503" in rows[0]["error"]
    assert metrics.summarize(rows)["failed_pct"] == 100.0


def test_the_models_are_really_switched_per_configuration(tmp_path, monkeypatch):
    seen = []

    async def spy(messages, settings):
        seen.append((settings.openrouter_model, settings.detail_level))
        return good_reply()

    monkeypatch.setattr(vision, "_call_openrouter", spy)
    cases = run_eval.load_cases(make_case(tmp_path, truth=None))
    configs = [{**run_eval.DEFAULT_CONFIG, "name": "a", "model": "vendor/model-a", "detail": "minimal", "verify": False}]
    rows = asyncio.run(run_eval.evaluate(cases, configs, base=SETTINGS, progress=lambda _: None))
    assert seen == [("vendor/model-a", "minimal")] and rows[0]["true_score"] is None and rows[0]["metrics"] == {}


# ---- faster prompt levels -------------------------------------------------------------------


def test_prompt_detail_levels_ask_for_less_and_stay_well_formed():
    plan = Planogram.model_validate(PLANOGRAM)
    full = vision.build_detect_prompt(plan, "grocery", detail="full")
    lean = vision.build_detect_prompt(plan, "grocery", detail="lean")
    minimal = vision.build_detect_prompt(plan, "grocery", detail="minimal")

    assert '"position"' in full and '"attributes"' in full and '"box"' in full
    assert '"position"' not in lean and '"attributes"' not in lean and '"box"' in lean
    assert '"position"' not in minimal and '"attributes"' not in minimal and '"box"' not in minimal
    assert len(minimal) < len(lean) < len(full)
    for text in (full, lean, minimal):
        assert '"count_confidence"' in text and "WHAT the item is" in text
        assert "empty_gaps" not in text and "misplaced_items" not in text  # fields the scoring never used
        numbers = [int(n) for n in re.findall(r"^(\d+)\. ", text, re.M)]
        assert numbers == list(range(1, len(numbers) + 1))  # rules are numbered 1..n without gaps
        json.loads(re.search(r"Schema:\n(\{.*?\n\})\n\nRules", text, re.S).group(1))  # the example is valid JSON
    assert vision.build_detect_prompt(plan, "grocery", detail="nonsense") == full  # unknown level -> full


# ---- timing and tokens ----------------------------------------------------------------------


def test_ai_calls_are_counted_with_time_and_tokens(monkeypatch):
    handler = lambda request: httpx.Response(200, json={  # noqa: E731
        "choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 1200, "completion_tokens": 300}})
    real = httpx.AsyncClient
    monkeypatch.setattr(vision.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    async def two_calls():
        stats = vision.track_usage()
        await vision._call_openrouter([{"role": "user", "content": "a"}], SETTINGS)
        await vision._call_openrouter([{"role": "user", "content": "b"}], SETTINGS)
        return stats

    stats = asyncio.run(two_calls())
    assert (stats["calls"], stats["prompt_tokens"], stats["completion_tokens"]) == (2, 2400, 600) and stats["seconds"] >= 0


def test_an_audit_records_how_long_it_took(client, model):
    from test_scenarios import run, stocked_grocery_reply
    model.queue(stocked_grocery_reply())
    audit_row = run(client)
    usage = audit_row["usage_json"]
    assert usage["total_seconds"] >= 0 and usage["detail"] == "full" and "calls" in usage
    assert "Analysed in" in client.get(f"/audits/{audit_row['id']}").text
