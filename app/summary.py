"""The plain-language answer for an audit.

Written by code from the numbers, not by the AI, so it always agrees with the score and can
never invent anything. Every audit gets a headline and a short summary, including the ones that
could not be scored: those say what was seen, why there is no confirmed score, and what to do.
"""

from collections import Counter

from app.schemas import ATTENTION_BELOW, AuditResult, Planogram

# A trust level of "high" needs the AI's own confidence to average at least this much.
HIGH_TRUST_CONFIDENCE = 0.7


def _pct(value: float) -> str:
    """73.7 -> '73.7%', 100.0 -> '100%'."""
    return f"{value:g}%"


def _seen(result: AuditResult, limit: int = 3) -> str:
    """What the AI saw, by the type it reported: '14 shirts, 12 knitwear and 6 other'."""
    units: Counter[str] = Counter()
    for d in result.detections:
        units[(d.category or d.name).strip().lower()] += d.facings
    top = units.most_common(limit)
    parts = [f"{n} {label}" for label, n in top]
    rest = sum(units.values()) - sum(n for _, n in top)
    if rest > 0:
        parts.append(f"{rest} other")
    if len(parts) > 1:
        return ", ".join(parts[:-1]) + " and " + parts[-1]
    return parts[0] if parts else "nothing"


def _planogram_names(planogram: Planogram, limit: int = 6) -> str:
    names: list[str] = []
    for prow in planogram.rows:
        for exp in prow.expected:
            if exp.product not in names:
                names.append(exp.product)
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def _estimate_line(result: AuditResult) -> str | None:
    if result.estimate_low is None or result.estimate_high is None:
        return None
    if result.estimate_low == result.estimate_high:
        return f"Estimated compliance: about {_pct(result.estimate_low)}. This is not confirmed."
    return (
        f"Estimated compliance: between {_pct(result.estimate_low)} and {_pct(result.estimate_high)}, "
        "depending on whether the uncertain items are right. This is not confirmed."
    )


def _trust(result: AuditResult) -> None:
    """How far the answer can be relied on, and why. Never claims more than the numbers support."""
    reasons: list[str] = []
    level = "high"

    if not result.complete:
        result.trust_level = "low"
        result.trust_reason = {
            "empty_shelf": "The AI reports an empty shelf. Confirm it to record 0%.",
            "no_match": "Your planogram does not describe what is on this shelf.",
            "no_products": "Nothing was detected in the photo.",
            "unusable_photo": "The photo could not be read well enough to audit.",
            "no_confident": "Too much of what was seen is uncertain.",
            "uncertain_share": "Too much of what was seen is uncertain.",
        }.get(result.incomplete_code or "", result.incomplete_reason or "The audit could not be completed.")
        return

    if result.detected_units == 0 and result.uncertain_units == 0:
        # Nothing was counted: the only way to get here with a score is a shelf a person confirmed empty.
        result.trust_level = "high"
        result.trust_reason = "You confirmed that the shelf is empty."
        return

    confidence = result.confidence
    total_seen = result.detected_units + result.uncertain_units
    uncertain_share = result.uncertain_units / total_seen if total_seen else 0

    if confidence is not None and confidence < 0.5:
        level = "low"
        reasons.append(f"the AI's confidence averaged only {confidence:.0%}")
    elif confidence is not None and confidence < HIGH_TRUST_CONFIDENCE:
        level = "medium"
        reasons.append(f"the AI's confidence averaged {confidence:.0%}")

    if result.uncertain_units:
        if uncertain_share > 0.2:
            level = "low"
        elif level == "high":
            level = "medium"
        reasons.append(f"{result.uncertain_units} uncertain units were not counted")

    if result.approx_count_groups:
        if level == "high":
            level = "medium"
        reasons.append(f"{result.approx_count_groups} group(s) are rough counts")

    if _has_unlisted_levels(result):
        if level == "high":
            level = "medium"
        reasons.append("your planogram has fewer rows than the shelf has levels")

    if any(w.startswith("The model noted") for w in result.warnings):
        if level == "high":
            level = "medium"
        reasons.append("the AI reported problems with the photo")

    if len(result.pass_scores) > 1:
        spread = max(result.pass_scores) - min(result.pass_scores)
        looks = len(result.pass_scores)
        if spread > 15:
            level = "low"
            reasons.append(f"{looks} looks at the photo disagreed (scores {_pct(min(result.pass_scores))} to {_pct(max(result.pass_scores))})")
        elif spread > 7:
            if level == "high":
                level = "medium"
            reasons.append(f"{looks} looks at the photo differed by {spread:.0f} points")

    verified = sum(1 for d in result.detections if d.verified)
    result.trust_level = level  # type: ignore[assignment]
    if reasons:
        text = "Because " + "; ".join(reasons) + "."
    else:
        text = "Everything counted was clearly identified."
    if len(result.pass_scores) > 1 and max(result.pass_scores) - min(result.pass_scores) <= 7:
        text += f" {len(result.pass_scores)} separate looks at the photo agreed closely."
    if verified:
        text += f" {verified} unclear group(s) were confirmed by a zoomed-in second look."
    result.trust_reason = text


def _has_unlisted_levels(result: AuditResult) -> bool:
    """True when the AI saw shelf levels the planogram has no row for."""
    return any("has no row for" in w for w in result.warnings) or any(c.score is None for c in result.comparison)


def describe(result: AuditResult, planogram: Planogram, confidence_threshold: float) -> None:
    """Fill in headline, summary and trust level on the result (in place)."""
    _trust(result)
    lines: list[str] = []

    if result.complete and result.score is not None:
        score = result.score
        verdict = (
            "The shelf is in good shape." if score >= ATTENTION_BELOW
            else "The shelf needs attention." if score >= 60
            else "The shelf needs work."
        )
        result.headline = f"{_pct(score)} compliant. {verdict}"

        expected = sum(r.expected_facings for r in result.rows)
        correct = sum(r.correct_facings for r in result.rows)
        lines.append(f"{correct} of {expected} expected units are in the right place.")

        problems = []
        if result.missing_units:
            problems.append(f"{result.missing_units} missing")
        if result.misplaced_units:
            problems.append(f"{result.misplaced_units} in the wrong row")
        if result.unexpected_units:
            problems.append(f"{result.unexpected_units} not in the planogram")
        extra = sum(o.extra for o in result.overstock)
        if extra:
            problems.append(f"{extra} above the planogram quantity")
        lines.append("Problems found: " + ", ".join(problems) + "." if problems else "No problems found.")

        if result.tasks:
            lines.append(f"Do this first: {result.tasks[0].text}.")
        if result.uncertain_units:
            lines.append(f"{result.uncertain_units} uncertain units were not counted. Review them below.")
        if _has_unlisted_levels(result):
            lines.append(
                "Your planogram has fewer rows than the shelf has levels. Known products on the extra "
                "levels are counted as in place; add rows if you want those levels checked too."
            )
        if result.approx_count_groups:
            lines.append("Some counts are rough estimates (folded or overlapping items).")
        if len(result.pass_scores) > 1:
            lines.append(
                f"The AI looked at the photo {len(result.pass_scores)} times and this is the middle result "
                f"(scores ranged from {_pct(min(result.pass_scores))} to {_pct(max(result.pass_scores))})."
            )

    else:
        code = result.incomplete_code
        estimate = _estimate_line(result)
        if code == "empty_shelf":
            expected = sum(exp.facings for prow in planogram.rows for exp in prow.expected)
            result.headline = "The shelf looks empty."
            lines.append("The AI sees a clearly visible shelf with no products on it.")
            lines.append(f"If that is right, none of the {expected} expected units are there: 0% compliance.")
            if estimate:
                lines.append(estimate)
            lines.append("Confirm that the shelf is empty to record 0% and get a restock list, or upload a different photo.")
        elif code == "no_products":
            result.headline = "No products were detected."
            lines.append(result.incomplete_reason or "")
        elif code == "unusable_photo":
            result.headline = "The photo could not be audited."
            lines.append(result.incomplete_reason or "")
        elif code == "no_match":
            result.headline = "None of the items match your planogram."
            lines.append(f"The AI saw {result.detected_units + result.uncertain_units} units: {_seen(result)}.")
            lines.append(f"Your planogram lists: {_planogram_names(planogram)}.")
            lines.append(
                "Rename the products or add aliases, correct the detections below, "
                "or use what the AI saw as your planogram."
            )
        elif code == "no_confident":
            result.headline = "The AI is not sure enough to give a score."
            lines.append(f"It saw about {result.uncertain_units} units ({_seen(result)}) but was unsure what they are.")
            if estimate:
                lines.append(estimate)
            lines.append("Accept the uncertain items below if they look right, or try a sharper photo.")
        elif code == "uncertain_share":
            result.headline = "Not enough is confirmed to give one score."
            lines.append(result.incomplete_reason or "")
            if estimate:
                lines.append(estimate)
        else:
            result.headline = "The audit is incomplete."
            lines.append(result.incomplete_reason or "")

    result.summary = [line for line in lines if line]
