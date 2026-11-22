"""Compares what the model saw with the planogram and computes a compliance score.

All scoring is plain Python, no LLM, so results are deterministic and explainable:

  compliance = correctly matched expected facings / total expected facings x 100

  - gap:        a product has fewer facings than the planogram expects
  - overstock:  a product has more facings than the planogram expects
  - misplaced:  a product was found in a row it does not belong to
  - unexpected: a product that matches nothing in the planogram
  - uncertain:  a detection below the confidence threshold: kept apart, never counted

If detection failed (nothing usable was found) the result is *incomplete*: there is no score
and no list of "missing" products, because that would be misleading, not a measurement.
"""

import re
from collections import defaultdict
from dataclasses import dataclass

from rapidfuzz import fuzz

from app import summary
from app.schemas import (
    AuditResult,
    ComparisonItem,
    DetectedProduct,
    DetectedShelf,
    Detection,
    ExtraItem,
    GapIssue,
    MisplacedIssue,
    OverstockIssue,
    Planogram,
    RowComparison,
    RowResult,
    Task,
    UnexpectedIssue,
)

# Two names count as the same product at or above this similarity (0-100).
# Kept high on purpose so "Pepsi 500ml" is not confused with "Pepsi Max 500ml".
# For nicknames ("Coke"), add aliases in the planogram instead of lowering this.
MATCH_THRESHOLD = 88

# Detections the model is less sure of than this (0 to 1) are treated as uncertain.
DEFAULT_CONFIDENCE_THRESHOLD = 0.5

# If more than this share of everything the model saw is uncertain, a score would be
# misleading (the "missing" products may be sitting in the uncertain pile), so the audit is
# held for review instead.
UNCERTAIN_SHARE_LIMIT = 0.3

# A counted group whose count_confidence is below this is flagged as a rough count.
APPROX_COUNT_BELOW = 0.6

# Points taken off a row's score for each misplaced or unexpected item found in it.
MISPLACED_PENALTY = 5.0

_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
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


def _stem(word: str) -> str:
    """A rough singular form, enough to treat 'sweaters' and 'Sweater' as the same word."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("sses", "shes", "ches", "xes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _words(text: str) -> set[str]:
    return {_stem(w) for w in normalize(text).split()}


def _keyword_match(name: str, candidates: list[_Expected], detected_row: int) -> _Expected | None:
    """Last resort: a one-word planogram name (Shirt, Jeans, Dress) appearing as a whole word.

    'brown folded sweaters' contains the word of the planogram product 'Sweater', so it is one,
    even if the AI did not say so. Only single words of 4+ letters count, and only when exactly one
    planogram product fits, so 'Pepsi Max 500ml' is still not mistaken for 'Pepsi 500ml'.
    """
    detected_words = _words(name)
    hits = []
    for cand in candidates:
        for planogram_name in cand.names:
            words = _words(planogram_name)
            if len(words) == 1:
                (word,) = words
                if len(word) >= 4 and word in detected_words:
                    hits.append(cand)
                    break
    if len({c.product for c in hits}) != 1:  # nothing fits, or it is ambiguous
        return None
    return next((c for c in hits if c.row == detected_row), hits[0])


def _find_match(
    name: str, candidates: list[_Expected], detected_row: int, threshold: float
) -> _Expected | None:
    """Best planogram entry for a name, or None. Ties prefer the same row."""
    best_key, best = None, None
    for cand in candidates:
        score = max(similarity(name, n) for n in cand.names)
        if score >= threshold:
            key = (score, cand.row == detected_row)
            if best_key is None or key > best_key:
                best_key, best = key, cand
    return best or _keyword_match(name, candidates, detected_row)


def _resolve(
    dp: DetectedProduct, candidates: list[_Expected], row: int, threshold: float
) -> _Expected | None:
    """Which planogram product is this detection?

    Order of trust: a manual assignment by the user, then the planogram product the model
    named, then plain name similarity.
    """
    for hint in (dp.assigned_to, dp.matched_expected):
        if hint:
            match = _find_match(hint, candidates, row, threshold)
            if match:
                return match
    return _find_match(dp.name, candidates, row, threshold)


def _is_uncertain(dp: DetectedProduct, confidence_threshold: float) -> bool:
    """Low-confidence model guesses are kept apart. The user's own entries are never uncertain."""
    return (
        dp.source != "user"
        and not dp.assigned_to
        and dp.confidence is not None
        and dp.confidence < confidence_threshold
    )


is_uncertain = _is_uncertain  # public name, used by the pages to flag items in the editor


def _gap_priority(gap: GapIssue) -> str:
    """Empty or mostly empty facings are urgent: customers can't buy what isn't there."""
    ratio = gap.missing / gap.expected
    if gap.found == 0 or ratio >= 0.5:
        return "high"
    if ratio >= 0.25:
        return "medium"
    return "low"


def _incomplete(
    reason: str,
    detections: list[Detection],
    uncertain_units: int,
    warnings: list[str],
    detected_units: int = 0,
    code: str | None = None,
) -> AuditResult:
    return AuditResult(
        score=None,
        complete=False,
        incomplete_reason=reason,
        incomplete_code=code,
        detections=detections,
        detected_units=detected_units,
        uncertain_units=uncertain_units,
        warnings=warnings,
    )


def pick_median_pass(
    planogram: Planogram,
    shelves: list[DetectedShelf],
    threshold: float = MATCH_THRESHOLD,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> tuple[int, list[float]]:
    """Of several looks at the same photo, pick the one with the middle score.

    Returns (index of the chosen look, every look's score sorted). The middle one is robust: a
    single wild over- or under-count cannot become the answer. Looks that could not be scored
    are ignored; if none could, the first look is used.
    """
    scored = []
    for index, shelf in enumerate(shelves):
        result = _score(planogram, shelf, threshold, confidence_threshold, allow_empty=False)
        if result.complete and result.score is not None:
            scored.append((result.score, index))
    if not scored:
        return 0, []
    scored.sort()
    return scored[(len(scored) - 1) // 2][1], [score for score, _ in scored]


def audit(
    planogram: Planogram,
    detected: DetectedShelf,
    threshold: float = MATCH_THRESHOLD,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    allow_empty: bool = False,
    pass_scores: list[float] | None = None,
) -> AuditResult:
    """Score a shelf and write the plain-language answer.

    allow_empty=True means the user confirmed the shelf really is empty.

    When no honest score is possible but the AI did see something, the result also carries an
    *estimate*: the range the score would fall in, depending on whether the uncertain items
    turn out to be right. The user then gets an answer with an honest "not confirmed" label,
    instead of a blank screen.
    """
    result = _score(planogram, detected, threshold, confidence_threshold, allow_empty)

    if not result.complete and result.incomplete_code == "empty_shelf":
        # If the shelf really is empty, every expected unit is missing: that is a definite 0%.
        empty_score = _score(planogram, detected, threshold, confidence_threshold, allow_empty=True).score
        result.estimate_low = result.estimate_high = empty_score

    if not result.complete and result.incomplete_code in ("no_confident", "uncertain_share"):
        # Lowest case: only what the AI was sure about counts. Highest case: every uncertain
        # item turns out to be right (the user accepts them all).
        low = _score(planogram, detected, threshold, confidence_threshold, allow_empty=True)
        accepted = detected.model_copy(deep=True)
        for row in accepted.rows:
            for product in row.products:
                product.source = "user"
        high = _score(planogram, accepted, threshold, confidence_threshold, allow_empty=True)
        matched_if_accepted = sum(d.facings for d in high.detections if d.status in ("ok", "short", "misplaced"))
        if matched_if_accepted == 0:
            # Even if every uncertain item were right, none would match the planogram: the
            # planogram does not describe this shelf. That is the useful thing to say.
            total = result.detected_units + result.uncertain_units
            result.incomplete_code = "no_match"
            result.incomplete_reason = (
                f"{total} item(s) were detected, but none match a planogram product. "
                "Check the retail category and the product names or aliases in the planogram, "
                "or correct the detections below."
            )
        elif low.score is not None and high.score is not None:
            result.estimate_low = min(low.score, high.score)
            result.estimate_high = max(low.score, high.score)

    if result.complete and pass_scores and len(pass_scores) > 1:
        result.pass_scores = list(pass_scores)
    summary.describe(result, planogram, confidence_threshold)
    return result


def _score(
    planogram: Planogram,
    detected: DetectedShelf,
    threshold: float,
    confidence_threshold: float,
    allow_empty: bool,
) -> AuditResult:
    candidates = [
        _Expected(row=prow.row, product=exp.product, names=[exp.product, *exp.aliases])
        for prow in planogram.rows
        for exp in prow.expected
    ]
    expected_by_key: dict[tuple[int, str], int] = defaultdict(int)
    for prow in planogram.rows:
        for exp in prow.expected:
            expected_by_key[(prow.row, exp.product)] += exp.facings

    warnings: list[str] = []
    if detected.image_issues:
        warnings.append("The model noted: " + "; ".join(detected.image_issues) + ".")

    # ---- Split detections into counted (confident) and uncertain ----
    confident: list[tuple[int, DetectedProduct]] = []
    uncertain: list[tuple[int, DetectedProduct]] = []
    for drow in detected.rows:
        for dp in drow.products:
            if dp.facings == 0:
                continue
            (uncertain if _is_uncertain(dp, confidence_threshold) else confident).append((drow.row, dp))
    uncertain_units = sum(dp.facings for _, dp in uncertain)

    def as_detection(row: int, dp: DetectedProduct, status: str, matched: str | None = None) -> Detection:
        return Detection(
            row=row, name=dp.name, facings=dp.facings, status=status, category=dp.category,
            matched_product=matched, confidence=dp.confidence, count_confidence=dp.count_confidence,
            verified=dp.verified, position=dp.position,
            attributes=dp.attributes, source=dp.source, box=dp.box,
        )

    # For uncertain items the model's own planogram match is shown as a suggestion only.
    uncertain_detections = [as_detection(r, dp, "uncertain", dp.matched_expected) for r, dp in uncertain]
    if uncertain:
        warnings.append(
            f"{uncertain_units} unit(s) in {len(uncertain)} detection(s) were too uncertain to count. "
            "Review them and accept the ones that are right."
        )

    # ---- Cases where no honest score can be given ----
    if not detected.shelf_visible and not allow_empty:
        why = "; ".join(detected.image_issues) or "it does not look like a shelf, or is too unclear"
        return _incomplete(
            f"The photo could not be audited: {why}. Try a sharper, well-lit, front-on photo.",
            uncertain_detections, uncertain_units, warnings, code="unusable_photo",
        )
    if detected.shelf_empty and not confident and not uncertain and not allow_empty:
        # The AI positively says the shelf is empty. That is an answer (0%), but a surprising one,
        # so it is held until a person confirms it.
        return _incomplete(
            "The AI says the shelf is empty. If that is right, confirm it to record 0% compliance.",
            [], 0, warnings, code="empty_shelf",
        )
    if not confident and not allow_empty:
        if uncertain:
            reason = (
                f"Possible products were seen, but none with enough confidence "
                f"(below {confidence_threshold:.0%}). Review them below, or try a clearer photo."
            )
        else:
            reason = (
                "No products were detected in the photo. If the shelf really is empty, confirm that "
                "below; otherwise try a clearer, front-on photo."
            )
        return _incomplete(
            reason, uncertain_detections, uncertain_units, warnings,
            code="no_confident" if uncertain else "no_products",
        )

    # ---- Match every counted detection to the planogram ----
    present: dict[tuple[int, str], int] = defaultdict(int)  # (row, product) -> facings in place
    misplaced_acc: dict[tuple[str, int, int], int] = defaultdict(int)  # (product, found, belongs)
    unexpected_acc: dict[tuple[str, int], int] = defaultdict(int)  # (name, row)
    # (row, detection, kind, planogram key if in place, matched product name)
    seen: list[tuple[int, DetectedProduct, str, tuple[int, str] | None, str | None]] = []

    listed_rows = {prow.row for prow in planogram.rows}
    # Levels the planogram lists are judged first. A level it has no row for cannot be called
    # "the wrong row" (the AI's level numbers are only a guess there), so what is found on such
    # a level fills whatever the listed rows still lack; any surplus is reported as misplaced.
    order = sorted(range(len(confident)), key=lambda i: confident[i][0] not in listed_rows)
    outcome: dict[int, tuple[int, DetectedProduct, str, tuple[int, str] | None, str | None]] = {}
    for i in order:
        row, dp = confident[i]
        match = _resolve(dp, candidates, row, threshold)
        if match is None:
            unexpected_acc[(dp.name, row)] += dp.facings
            outcome[i] = (row, dp, "unexpected", None, None)
            continue
        key = (match.row, match.product)
        if match.row == row or (row not in listed_rows and present[key] + dp.facings <= expected_by_key[key]):
            present[key] += dp.facings
            outcome[i] = (row, dp, "in_place", key, match.product)
        else:
            misplaced_acc[(match.product, row, match.row)] += dp.facings
            outcome[i] = (row, dp, "misplaced", None, match.product)
    seen.extend(outcome[i] for i in range(len(confident)))

    matched_units = sum(dp.facings for _, dp, kind, _, _ in seen if kind != "unexpected")
    seen_units = sum(dp.facings for _, dp, _, _, _ in seen)

    detections = [
        as_detection(
            row, dp,
            ("ok" if present[key] >= expected_by_key[key] else "short") if kind == "in_place" else kind,
            matched,
        )
        for row, dp, kind, key, matched in seen
    ] + uncertain_detections

    if matched_units == 0 and seen_units > 0 and not allow_empty:
        return _incomplete(
            f"{seen_units} item(s) were detected, but none match a planogram product. "
            "Check the retail category and the product names or aliases in the planogram, "
            "or correct the detections below.",
            detections, uncertain_units, warnings, seen_units, code="no_match",
        )

    total_seen = seen_units + uncertain_units
    if uncertain_units and not allow_empty and uncertain_units / total_seen > UNCERTAIN_SHARE_LIMIT:
        return _incomplete(
            f"{uncertain_units} of {total_seen} detected units were too uncertain to count "
            f"(below {confidence_threshold:.0%} confidence), so a score would be misleading. "
            "Accept or correct them below, or try a larger, sharper photo.",
            detections, uncertain_units, warnings, seen_units, code="uncertain_share",
        )

    # Items on shelf levels the planogram has no row for can only count as misplaced. That is
    # usually a planogram that has fewer rows than the shelf has levels, so say so.
    unlisted_rows = sorted({row for row, _ in confident} - {prow.row for prow in planogram.rows})
    if unlisted_rows:
        warnings.append(
            f"The AI found items on shelf level(s) {', '.join(map(str, unlisted_rows))}, which your planogram "
            "has no row for. Known products there are counted as in place where the planogram still "
            "lacks them (their level is not checked). Add rows to the planogram to check those levels too."
        )

    misplaced = [
        MisplacedIssue(product=p, found_in_row=f, belongs_in_row=b, facings=n)
        for (p, f, b), n in misplaced_acc.items()
    ]
    unexpected = [
        UnexpectedIssue(name=name, row=row, facings=n) for (name, row), n in unexpected_acc.items()
    ]

    gaps: list[GapIssue] = []
    overstock: list[OverstockIssue] = []
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
                        row=prow.row, product=exp.product, expected=exp.facings,
                        found=found, missing=exp.facings - found,
                    )
                )
            elif found > exp.facings:
                overstock.append(
                    OverstockIssue(
                        row=prow.row, product=exp.product, expected=exp.facings,
                        found=found, extra=found - exp.facings,
                    )
                )
        issues_here = sum(1 for m in misplaced if m.found_in_row == prow.row) + sum(
            1 for u in unexpected if u.row == prow.row
        )
        score = max(0.0, correct_total / expected_total * 100 - MISPLACED_PENALTY * issues_here)
        row_results.append(
            RowResult(
                row=prow.row, expected_facings=expected_total,
                correct_facings=correct_total, score=round(score, 1),
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

    # ---- To-do list, most urgent first ----
    gap_keys = {(g.row, g.product) for g in gaps}
    tasks = [
        Task(kind="restock", priority=_gap_priority(g), text=f"Restock {g.missing} x {g.product} in row {g.row}")
        for g in gaps
    ]
    for m in misplaced:
        # Moving an item back is urgent when it also fills a hole in its own row.
        fills_gap = (m.belongs_in_row, m.product) in gap_keys
        tasks.append(
            Task(
                kind="move",
                priority="high" if fills_gap else "medium",
                text=f"Move {m.facings} x {m.product} from row {m.found_in_row} to row {m.belongs_in_row}",
            )
        )
    tasks += [
        Task(
            kind="correct",
            priority="low",
            text=f"Correct quantity of {o.product} in row {o.row}: {o.found} found, {o.expected} expected (remove {o.extra})",
        )
        for o in overstock
    ]
    tasks += [
        Task(kind="remove", priority="low", text=f"Remove '{u.name}' from row {u.row}: it is not in the planogram")
        for u in unexpected
    ]
    tasks.sort(key=lambda t: _PRIORITY_RANK[t.priority])  # stable: keeps order within a level

    # ---- Planogram vs shelf, row by row ----
    score_by_row = {r.row: r.score for r in row_results}
    stray_rows = {m.found_in_row for m in misplaced} | {u.row for u in unexpected}
    comparison = []
    for row in sorted(planogram_rows | stray_rows):
        items = [
            ComparisonItem(product=exp.product, expected=exp.facings, found=present[(row, exp.product)])
            for prow in planogram.rows
            if prow.row == row
            for exp in prow.expected
        ]
        extras = [
            ExtraItem(name=m.product, facings=m.facings, kind="misplaced", belongs_in_row=m.belongs_in_row)
            for m in misplaced
            if m.found_in_row == row
        ] + [
            ExtraItem(name=u.name, facings=u.facings, kind="unexpected")
            for u in unexpected
            if u.row == row
        ]
        comparison.append(RowComparison(row=row, score=score_by_row.get(row), items=items, extras=extras))

    # Average of what the model itself reported. Not invented: None when it reported nothing.
    reported = [dp.confidence for _, dp in confident if dp.confidence is not None]

    # Counted groups whose NUMBER is only a rough estimate (folded piles, overlapping items).
    approx_groups = sum(
        1 for _, dp in confident
        if dp.count_confidence is not None and dp.count_confidence < APPROX_COUNT_BELOW
    )
    if approx_groups:
        warnings.append(
            f"{approx_groups} group(s) are rough counts (for example folded piles or overlapping items), "
            "so the totals can be off by a few units."
        )

    return AuditResult(
        score=round(overall, 1),
        complete=True,
        approx_count_groups=approx_groups,
        rows=row_results,
        gaps=gaps,
        overstock=overstock,
        misplaced=misplaced,
        unexpected=unexpected,
        actions=[t.text for t in tasks],
        tasks=tasks,
        comparison=comparison,
        detections=detections,
        detected_units=seen_units,
        missing_units=sum(g.missing for g in gaps),
        misplaced_units=sum(m.facings for m in misplaced),
        unexpected_units=sum(u.facings for u in unexpected),
        uncertain_units=uncertain_units,
        confidence=round(sum(reported) / len(reported), 3) if reported else None,
        warnings=warnings,
    )
