"""Scoring a run against the truth, and summarising many runs. Pure code: no network, no AI.

The truth for a photo is what is REALLY on the shelf, written by a person. Scoring it with the
same compliance engine the app uses gives the "true" score, so a run can be judged on:

  - counts      : did it find the right number of units of each product in each row?
  - recall      : did it notice every product that is really there?
  - false alarms: did it invent products that are not there, or call known products unexpected?
  - score error : how far is the final percentage from the true percentage?
  - reliability : how often did it fail or give up (no score)?
  - speed       : seconds per audit, and tokens used.
"""

from __future__ import annotations

import math
from collections import defaultdict
from statistics import mean, median

from app.compliance import audit
from app.schemas import AuditResult, DetectedProduct, DetectedRow, DetectedShelf, Planogram


def build_truth(case: dict) -> AuditResult | None:
    """The true audit result for a case, or None if the case has no truth written down."""
    truth = case.get("truth")
    if truth is None:
        return None
    planogram = Planogram.model_validate(case["planogram"])
    rows: dict[int, list[DetectedProduct]] = defaultdict(list)
    for row in truth.get("rows", []):
        for item in row.get("products", []):
            rows[row["row"]].append(DetectedProduct(name=item["product"], facings=item["facings"], source="user"))
    for item in truth.get("unexpected", []):
        rows[item["row"]].append(DetectedProduct(name=item["name"], facings=item["facings"], source="user"))
    shelf = DetectedShelf(rows=[DetectedRow(row=r, products=p) for r, p in sorted(rows.items())])
    return audit(planogram, shelf, allow_empty=True)


def _counts(result: AuditResult) -> dict[tuple[int, str], int]:
    """Units found in place, per (row, planogram product)."""
    return {(c.row, item.product): item.found for c in result.comparison for item in c.items}


def score_run(result: AuditResult | None, truth: AuditResult | None) -> dict:
    """How one run compares with the truth. Empty dict if there is no truth or the run failed."""
    if truth is None or result is None:
        return {}

    truth_empty = truth.detected_units == 0
    if truth_empty:
        # The shelf really is empty: the right answer is "empty" (held for confirmation) or a 0.
        said_empty = result.incomplete_code == "empty_shelf" or (result.complete and result.detected_units == 0)
        return {"truth_empty": True, "empty_correct": bool(said_empty)}

    if not result.complete:
        return {"incomplete": True}

    truth_counts, got = _counts(truth), _counts(result)
    exact = within1 = present_total = present_found = absent_total = false_presence = 0
    abs_error = 0
    for key, want in truth_counts.items():
        have = got.get(key, 0)
        abs_error += abs(have - want)
        exact += have == want
        within1 += abs(have - want) <= 1
        if want > 0:
            present_total += 1
            present_found += have > 0
        else:
            absent_total += 1
            false_presence += have > 0

    return {
        "items": len(truth_counts),
        "exact": exact,
        "within1": within1,
        "abs_error": abs_error,
        "present": present_total,
        "present_found": present_found,
        "absent": absent_total,
        "false_presence": false_presence,
        "false_unexpected_units": max(0, result.unexpected_units - truth.unexpected_units),
        "score_abs_error": abs(result.score - truth.score) if result.score is not None and truth.score is not None else None,
    }


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def _pct(part: float, whole: float) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def summarize(runs: list[dict]) -> dict:
    """One row of numbers for a set of runs of the same configuration."""
    ok = [r for r in runs if r["ok"]]
    seconds = [r["seconds"] for r in ok]
    scored = [r["metrics"] for r in ok if r.get("metrics") and "items" in r["metrics"]]
    empties = [r["metrics"] for r in ok if r.get("metrics") and r["metrics"].get("truth_empty")]
    judged = [r for r in ok if r.get("metrics")]  # runs that had a truth to compare with
    items = sum(m["items"] for m in scored)
    score_errors = [m["score_abs_error"] for m in scored if m["score_abs_error"] is not None]

    return {
        "runs": len(runs),
        "failed_pct": _pct(len(runs) - len(ok), len(runs)),
        "incomplete_pct": _pct(sum(1 for r in ok if not r["complete"] and not (r.get("metrics") or {}).get("empty_correct")), len(ok)),
        "median_s": round(median(seconds), 1) if seconds else None,
        "p95_s": round(_percentile(seconds, 0.95), 1) if seconds else None,
        "tokens": round(mean(r["tokens"] for r in ok)) if ok else None,
        "units_seen": round(mean(r["units"] for r in ok), 1) if ok else None,
        "judged": len(judged),
        "score_mae": round(mean(score_errors), 1) if score_errors else None,
        "count_exact_pct": _pct(sum(m["exact"] for m in scored), items),
        "count_within1_pct": _pct(sum(m["within1"] for m in scored), items),
        "count_mae": round(sum(m["abs_error"] for m in scored) / items, 2) if items else None,
        "recall_pct": _pct(sum(m["present_found"] for m in scored), sum(m["present"] for m in scored)),
        "false_presence_pct": _pct(sum(m["false_presence"] for m in scored), sum(m["absent"] for m in scored)),
        "false_unexpected_units": round(mean(m["false_unexpected_units"] for m in scored), 1) if scored else None,
        "empty_correct_pct": _pct(sum(m["empty_correct"] for m in empties), len(empties)) if empties else None,
    }


_COLUMNS = [
    ("Config", None),
    ("runs", "runs"),
    ("fail %", "failed_pct"),
    ("no-score %", "incomplete_pct"),
    ("median s", "median_s"),
    ("p95 s", "p95_s"),
    ("tokens", "tokens"),
    ("units", "units_seen"),
    ("score err", "score_mae"),
    ("count exact %", "count_exact_pct"),
    ("count ±1 %", "count_within1_pct"),
    ("count err", "count_mae"),
    ("recall %", "recall_pct"),
    ("false prod %", "false_presence_pct"),
    ("empty ok %", "empty_correct_pct"),
]


def render_markdown(summaries: dict[str, dict], title: str = "ShelfSense evaluation") -> str:
    """A table with one row per configuration. Empty cells (-) mean there was nothing to measure."""
    header = "| " + " | ".join(name for name, _ in _COLUMNS) + " |"
    divider = "|" + "|".join("---" for _ in _COLUMNS) + "|"
    lines = [f"# {title}", "", header, divider]
    for name, stats in summaries.items():
        cells = [name] + [("-" if stats.get(key) is None else str(stats[key])) for _, key in _COLUMNS[1:]]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        "How to read it:",
        "- **fail %** AI errors that gave no answer. **no-score %** audits held back (uncertain, unmatched, unusable).",
        "- **median s / p95 s** seconds per audit (p95 = the slow ones). **tokens** is the average per audit.",
        "- **score err** average points between the app's percentage and the true percentage.",
        "- **count exact / ±1 / err** how close the unit counts are, per product per row. **recall** products that are really there and were found.",
        "- **false prod %** products reported that are not really there. **empty ok %** empty shelves recognised as empty.",
    ]
    return "\n".join(lines) + "\n"
