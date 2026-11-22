"""Runs an audit end to end: detect, score, save.

run_audit() is written to be used as a background job: it never raises. Whatever goes wrong is
stored on the audit (status "failed" plus a readable message) and written to the log, so the
page can show it instead of a blank screen or a misleading score.
"""

import logging
import time

from app import compliance, vision
from app.config import get_settings
from app.db import get_db
from app.schemas import (
    AuditResult,
    CorrectionRequest,
    DetectedProduct,
    DetectedRow,
    DetectedShelf,
    Planogram,
)

log = logging.getLogger("shelfsense.pipeline")

# Order of the stages an audit moves through. The page shows the real current stage.
# "verifying" (the zoomed-in second look) is skipped when nothing is uncertain.
STAGES = ["queued", "detecting", "verifying", "scoring", "done"]


def finalize(
    audit_id: str,
    planogram: Planogram,
    shelf: DetectedShelf,
    *,
    confirmed_empty: bool = False,
    corrected: bool = False,
    pass_scores: list[float] | None = None,
) -> AuditResult:
    """Score the detections, then store the result and a fresh task list."""
    db = get_db()
    result = compliance.audit(
        planogram,
        shelf,
        confidence_threshold=get_settings().confidence_threshold,
        allow_empty=confirmed_empty,
        pass_scores=pass_scores,
    )
    fields = dict(
        status="completed" if result.complete else "needs_review",
        stage="done",
        working_json=shelf.model_dump(mode="json"),
        result_json=result.model_dump(mode="json"),
        score=result.score,
        detected_units=result.detected_units,
        missing_units=result.missing_units,
        misplaced_units=result.misplaced_units,
        unexpected_units=result.unexpected_units,
        uncertain_units=result.uncertain_units,
        confidence=result.confidence,
        error=None if result.complete else result.incomplete_reason,
        confirmed_empty=int(confirmed_empty),
        confirmed_at=None,  # the result changed, so an earlier "confirmed by you" no longer applies
    )
    if corrected:
        fields["corrected"] = 1
    db.update_audit(audit_id, **fields)
    db.replace_tasks(audit_id, result.tasks)
    log.info(
        "audit %s %s: score=%s detected=%s missing=%s misplaced=%s unexpected=%s uncertain=%s",
        audit_id, fields["status"], result.score, result.detected_units, result.missing_units,
        result.misplaced_units, result.unexpected_units, result.uncertain_units,
    )
    return result


def _fail(audit_id: str, message: str) -> None:
    get_db().update_audit(audit_id, status="failed", stage="done", error=message, score=None)


async def run_audit(audit_id: str) -> None:
    """Background job: detect products in the stored photo, score them, save everything."""
    db = get_db()
    audit = db.get_audit(audit_id)
    if audit is None or audit["status"] != "processing":
        return

    # Counts how many times this audit has been started, so one that keeps crashing the server
    # is eventually given up on instead of being resumed forever.
    db.update_audit(audit_id, attempts=audit["attempts"] + 1)

    settings = get_settings()
    usage = vision.track_usage()  # counts this audit's AI calls, time and tokens
    started = time.monotonic()
    detect_seconds = verify_seconds = 0.0
    try:
        planogram = Planogram.model_validate(audit["planogram_json"])
        image = db.read_image(audit_id)
        if image is None:
            _fail(audit_id, "The saved photo for this audit is missing. Please upload it again.")
            return

        db.update_audit(audit_id, stage="detecting", model=settings.openrouter_model)
        shelf, pass_scores = await vision.detect_best_of(
            image,
            audit["image_mime"] or "image/jpeg",
            settings,
            passes=settings.detect_passes,
            planogram=planogram,
            category=audit["category"],
            custom_category=audit["category_name"],
        )
        detect_seconds = time.monotonic() - started
        # Keep the AI's first answer exactly as it came, before any second look changes things.
        db.update_audit(audit_id, detected_json=shelf.model_dump(mode="json"))

        if settings.verify_uncertain and vision.verification_candidates(shelf, settings.confidence_threshold):
            db.update_audit(audit_id, stage="verifying")
            verify_started = time.monotonic()
            try:
                await vision.verify_detections(
                    image, shelf, settings, planogram, settings.confidence_threshold
                )
            except Exception as exc:  # the second look is a bonus: never let it sink the audit
                log.warning("audit %s: second look failed, continuing without it: %s", audit_id, exc)
            verify_seconds = time.monotonic() - verify_started

        db.update_audit(audit_id, stage="scoring")
        finalize(audit_id, planogram, shelf, pass_scores=pass_scores)
        db.update_audit(
            audit_id,
            usage_json={
                **usage,
                "detect_seconds": round(detect_seconds, 1),
                "verify_seconds": round(verify_seconds, 1),
                "total_seconds": round(time.monotonic() - started, 1),
                "detail": settings.detail_level,
            },
        )
    except vision.MissingAPIKeyError as exc:
        log.error("audit %s: %s", audit_id, exc)
        _fail(audit_id, str(exc))
    except vision.InvalidModelOutputError as exc:
        log.error("audit %s: %s", audit_id, exc)
        _fail(
            audit_id,
            "The AI model's answer could not be understood, even after retrying. "
            "Please try again, or use a different model in your settings.",
        )
    except vision.VisionError as exc:
        log.error("audit %s: detection failed: %s", audit_id, exc)
        _fail(audit_id, f"The AI service could not analyse this photo: {exc}")
    except Exception:  # last resort: the audit must never stay "processing" forever
        log.exception("audit %s: unexpected error", audit_id)
        _fail(audit_id, "Something went wrong while analysing this photo. Please try again.")
    finally:
        log.info("audit %s finished in %.1fs", audit_id, time.monotonic() - started)


def reset_for_retry(audit_id: str) -> bool:
    """Put a failed or incomplete audit back in the queue. Returns False if it can't be retried."""
    db = get_db()
    audit = db.get_audit(audit_id)
    if audit is None or audit["status"] == "processing":
        return False
    db.update_audit(
        audit_id,
        status="processing",
        stage="queued",
        error=None,
        score=None,
        result_json=None,
        detected_json=None,
        working_json=None,
        confirmed_empty=0,
        corrected=0,
        confirmed_at=None,
        attempts=0,  # a person asked for this run, so it starts with a clean slate
    )
    db.replace_tasks(audit_id, [])
    return True


def planogram_from_shelf(shelf: DetectedShelf, store: str = "", shelf_name: str = "") -> dict:
    """A planogram built from what the AI saw, for when you have no planogram or yours doesn't fit.

    One entry per product name per row, with the number the AI counted. Review it before using it:
    it describes the shelf as it is, not as it should be.
    """
    rows = []
    for row in sorted(shelf.rows, key=lambda r: r.row):
        grouped: dict[str, dict] = {}
        for product in row.products:
            if product.facings < 1:
                continue
            key = product.name.strip().lower()
            entry = grouped.setdefault(key, {"product": product.name.strip(), "facings": 0})
            entry["facings"] += product.facings
            if product.category and "category" not in entry:
                entry["category"] = product.category
        if grouped:
            rows.append({"row": row.row, "expected": list(grouped.values())})
    return {"store": store, "shelf": shelf_name, "rows": rows}


def build_corrected_shelf(original: DetectedShelf, request: CorrectionRequest) -> DetectedShelf:
    """Turn the user's edited table back into detections.

    The model's original output is never modified (it stays in detected_json). User-touched
    entries are marked source="user", so they always count, whatever the model's confidence was.
    """
    rows: dict[int, list[DetectedProduct]] = {}
    for item in request.items:
        if item.state == "drop":
            continue
        by_user = item.state == "accept" or item.edited or item.source == "user"
        product = DetectedProduct(
            name=item.name,
            facings=item.quantity,
            category=item.category,
            matched_expected=item.matched_expected,
            # The model's confidence described its own guess, not a number the user retyped.
            confidence=None if (item.edited and item.state != "accept") else item.confidence,
            count_confidence=None if item.edited else item.count_confidence,
            verified=item.verified,
            position=item.position,
            attributes=item.attributes,
            box=item.box,
            source="user" if by_user else "model",
            assigned_to=item.assigned_to,
        )
        rows.setdefault(item.row, []).append(product)

    return DetectedShelf(
        rows=[DetectedRow(row=r, products=rows[r]) for r in sorted(rows)],
        overall_notes=original.overall_notes,
        shelf_visible=True,  # a person has looked at the shelf and corrected it
        image_issues=original.image_issues,
    )


def apply_corrections(audit_id: str, request: CorrectionRequest) -> AuditResult | None:
    """Re-run the comparison with the user's corrections. No AI call, so it is instant and free."""
    db = get_db()
    audit = db.get_audit(audit_id)
    if audit is None or audit["status"] == "processing":
        return None
    planogram = Planogram.model_validate(audit["planogram_json"])
    original = DetectedShelf.model_validate(
        audit["working_json"] or audit["detected_json"] or {"rows": []}
    )
    shelf = build_corrected_shelf(original, request)
    return finalize(
        audit_id, planogram, shelf, confirmed_empty=request.confirmed_empty, corrected=True
    )
