"""Compares what the model saw with the planogram and computes a compliance score.

All scoring is plain Python, no LLM, so results are deterministic and explainable:
  - gap:        a product has fewer facings than the planogram expects
  - misplaced:  a product was found in a row it does not belong to
  - unexpected: a product that matches nothing in the planogram
"""

import re
from collections import defaultdict
from dataclasses import dataclass

from rapidfuzz import fuzz

from app.schemas import (
    AuditResult,
    DetectedShelf,
    GapIssue,
    MisplacedIssue,
    Planogram,
    RowResult,
    UnexpectedIssue,
)

# Two names count as the same product at or above this similarity (0-100).
# Kept high on purpose so "Pepsi 500ml" is not confused with "Pepsi Max 500ml".
# For brand nicknames ("Coke"), add aliases in the planogram instead of lowering this.
MATCH_THRESHOLD = 88

# Points taken off a row's score for each misplaced or unexpected item found in it.
MISPLACED_PENALTY = 5.0

_UNIT_SPACE = re.compile(r"(\d)\s+(ml|cl|l|g|kg|oz)\b")


def normalize(name: str) -> str:
    """Lowercase, drop punctuation, and glue units to numbers: 'Coca-Cola 500 ml' -> 'coca cola 500ml'."""
    text = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    text = re.sub(r"\s+", " ", text).strip()
    return _UNIT_SPACE.sub(r"\1\2", text)


def similarity(a: str, b: str) -> float:
    """0-100 similarity between two product names, ignoring word order and punctuation."""
    return fuzz.token_sort_ratio(normalize(a), normalize(b))


@dataclass
class _Expected:
    row: int
    product: str
    names: list[str]  # product name plus aliases


def _find_match(
    name: str, candidates: list[_Expected], detected_row: int, threshold: float
) -> _Expected | None:
    """Best planogram entry for a detected name, or None. Ties prefer the same row."""
    best_key, best = None, None
    for cand in candidates:
        score = max(similarity(name, n) for n in cand.names)
        if score >= threshold:
            key = (score, cand.row == detected_row)
            if best_key is None or key > best_key:
                best_key, best = key, cand
    return best


def audit(
    planogram: Planogram, detected: DetectedShelf, threshold: float = MATCH_THRESHOLD
) -> AuditResult:
    candidates = [
        _Expected(row=prow.row, product=exp.product, names=[exp.product, *exp.aliases])
        for prow in planogram.rows
        for exp in prow.expected
    ]

    present: dict[tuple[int, str], int] = defaultdict(int)  # (row, product) -> facings in place
    misplaced_acc: dict[tuple[str, int, int], int] = defaultdict(int)  # (product, found, belongs)
    unexpected_acc: dict[tuple[str, int], int] = defaultdict(int)  # (name, row)

    for drow in detected.rows:
        for dp in drow.products:
            if dp.facings == 0:
                continue
            match = _find_match(dp.name, candidates, drow.row, threshold)
            if match is None:
                unexpected_acc[(dp.name, drow.row)] += dp.facings
            elif match.row == drow.row:
                present[(match.row, match.product)] += dp.facings
            else:
                misplaced_acc[(match.product, drow.row, match.row)] += dp.facings

    misplaced = [
        MisplacedIssue(product=p, found_in_row=f, belongs_in_row=b, facings=n)
        for (p, f, b), n in misplaced_acc.items()
    ]
    unexpected = [
        UnexpectedIssue(name=name, row=row, facings=n) for (name, row), n in unexpected_acc.items()
    ]

    gaps: list[GapIssue] = []
    row_results: list[RowResult] = []
    for prow in sorted(planogram.rows, key=lambda r: r.row):
        expected_total = correct_total = 0
        for exp in prow.expected:
            found = present[(prow.row, exp.product)]
            expected_total += exp.facings
            correct_total += min(found, exp.facings)  # extra units don't earn extra credit
            if found < exp.facings:
                gaps.append(
                    GapIssue(
                        row=prow.row,
                        product=exp.product,
                        expected=exp.facings,
                        found=found,
                        missing=exp.facings - found,
                    )
                )
        issues_here = sum(1 for m in misplaced if m.found_in_row == prow.row) + sum(
            1 for u in unexpected if u.row == prow.row
        )
        score = max(0.0, correct_total / expected_total * 100 - MISPLACED_PENALTY * issues_here)
        row_results.append(
            RowResult(
                row=prow.row,
                expected_facings=expected_total,
                correct_facings=correct_total,
                score=round(score, 1),
            )
        )

    # Overall score: row scores weighted by how many facings each row should have.
    total_expected = sum(r.expected_facings for r in row_results)
    weighted = sum(r.score * r.expected_facings for r in row_results) / total_expected
    # Items found in rows the planogram doesn't have still cost points.
    planogram_rows = {r.row for r in planogram.rows}
    stray = sum(1 for m in misplaced if m.found_in_row not in planogram_rows) + sum(
        1 for u in unexpected if u.row not in planogram_rows
    )
    overall = max(0.0, weighted - MISPLACED_PENALTY * stray)

    actions = (
        [f"Restock {g.missing} x {g.product} in row {g.row}" for g in gaps]
        + [
            f"Move {m.facings} x {m.product} from row {m.found_in_row} to row {m.belongs_in_row}"
            for m in misplaced
        ]
        + [f"Check '{u.name}' in row {u.row}: it is not in the planogram" for u in unexpected]
    )

    return AuditResult(
        score=round(overall, 1),
        rows=row_results,
        gaps=gaps,
        misplaced=misplaced,
        unexpected=unexpected,
        actions=actions,
    )
