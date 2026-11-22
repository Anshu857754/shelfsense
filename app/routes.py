"""Website pages and the JSON API for stored audits.

Pages:  /  (dashboard)   /new   /audits (history)   /audits/{id}
API:    /api/audits   /api/audits/{id}   /api/audits/{id}/tasks/{task_id}
        /api/audits/{id}/corrections   /api/stats   /api/categories
"""

import csv
import io
import json
import logging
import math
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from app import compliance, pipeline
from app.categories import CATEGORIES, DEFAULT_CATEGORY
from app.config import get_settings
from app.db import get_db
from app.schemas import AuditResult, CorrectionRequest, DetectedShelf, Planogram
from app.uploads import parse_category, parse_planogram, prepare_bytes, read_prepared_image, reserve_ai_run
from app.web import STATUS_LABELS, fmt_day, score_level, templates

log = logging.getLogger("shelfsense.routes")
router = APIRouter()

PAGE_SIZE = 12
STATUS_COLORS = {
    "completed": "#4fb99b",
    "processing": "#8fc3ea",
    "needs_review": "#f58b5d",
    "failed": "#e5647d",
}


# ---- Helpers ---------------------------------------------------------------------


def render_new(
    request: Request,
    *,
    error: str | None = None,
    planogram_text: str | None = None,
    category: str = DEFAULT_CATEGORY,
    category_name: str = "",
    status_code: int = 200,
    reuse_image: str = "",
    notice: str | None = None,
):
    cat = CATEGORIES.get(category) or CATEGORIES[DEFAULT_CATEGORY]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "active": "new",
            "error": error,
            "notice": notice,
            "reuse_image": reuse_image,
            "planogram_text": planogram_text if planogram_text is not None else json.dumps(cat.sample, indent=2),
            "samples": {key: c.sample for key, c in CATEGORIES.items()},
            "selected_category": cat.key,
            "category_name": category_name,
        },
        status_code=status_code,
    )


def render_error(request: Request, title: str, message: str, status_code: int = 404):
    return templates.TemplateResponse(
        request, "error.html", {"active": "", "title": title, "message": message}, status_code=status_code
    )


def build_charts(db) -> dict:
    """Numbers for the dashboard charts (the templates draw them as plain HTML/SVG)."""
    trend = [
        {"id": r["id"], "label": fmt_day(r["created_at"]), "score": r["score"], "level": score_level(r["score"])}
        for r in db.recent_scores(12)
    ]

    counts = db.status_counts()
    total = sum(counts.values())
    legend, stops, acc = [], [], 0
    for status in ("completed", "processing", "needs_review", "failed"):
        n = counts.get(status, 0)
        legend.append({"status": status, "label": STATUS_LABELS[status], "count": n, "color": STATUS_COLORS[status]})
        if n:
            start = acc / total * 100
            acc += n
            stops.append(f"{STATUS_COLORS[status]} {start:.2f}% {acc / total * 100:.2f}%")
    donut = "conic-gradient(" + ", ".join(stops) + ")" if stops else ""

    by_category = [
        {"label": c["label"], "avg": round(c["avg_score"], 1), "audits": c["audits"], "level": score_level(c["avg_score"])}
        for c in db.category_averages()
    ]
    return {"trend": trend, "donut": donut, "legend": legend, "total": total, "by_category": by_category}


def audit_view(audit: dict) -> dict:
    """Everything the audit page needs, prepared once."""
    db = get_db()
    result = AuditResult.model_validate(audit["result_json"]) if audit["result_json"] else None
    working = DetectedShelf.model_validate(audit["working_json"]) if audit["working_json"] else None
    planogram = Planogram.model_validate(audit["planogram_json"])
    threshold = get_settings().confidence_threshold

    if result is not None and not result.headline and working is not None:
        # An audit saved before summaries existed: score it again from its stored detections.
        result = compliance.audit(
            planogram, working, confidence_threshold=threshold, allow_empty=audit["confirmed_empty"]
        )

    items = []
    if working:
        for row in working.rows:
            for product in row.products:
                item = product.model_dump(mode="json")
                item["row"] = row.row
                item["quantity"] = product.facings
                item["uncertain"] = compliance.is_uncertain(product, threshold)
                items.append(item)

    expected_names = []
    for prow in planogram.rows:
        for exp in prow.expected:
            if exp.product not in expected_names:
                expected_names.append(exp.product)

    stage_index = pipeline.STAGES.index(audit["stage"]) if audit["stage"] in pipeline.STAGES else 0
    step_labels = [
        "Photo received",
        "Reading the shelf with AI",
        "Second look at unclear items",
        "Scoring against the planogram",
    ]
    steps = [
        {"label": label, "state": "done" if stage_index > i else ("active" if stage_index == i else "")}
        for i, label in enumerate(step_labels)
    ]

    tasks = db.get_tasks(audit["id"])
    warnings = list(audit["warnings_json"] or []) + (result.warnings if result else [])
    return {
        "audit": audit,
        "result": result.model_dump(mode="json") if result else None,
        "planogram": planogram.model_dump(mode="json"),
        "items": items,
        "expected_names": expected_names,
        "tasks": tasks,
        "tasks_done": sum(1 for t in tasks if t["done"]),
        "steps": steps,
        "warnings": warnings,
        "level": score_level(audit["score"]),
        "threshold_pct": int(round(threshold * 100)),
    }


# ---- Pages -----------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request):
    db = get_db()
    recent, _ = db.list_audits(limit=6)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"active": "dashboard", "stats": db.stats(), "recent": recent, "charts": build_charts(db)},
    )


@router.get("/new", response_class=HTMLResponse, include_in_schema=False)
async def new_audit(request: Request, source: str = Query("", alias="from")):
    """The new-audit form. With ?from=<audit id> the planogram is built from what the AI saw
    in that audit, and its photo is reused, so a planogram that didn't fit can be fixed in one step."""
    if not source:
        return render_new(request)
    audit = get_db().get_audit(source)
    if audit is None:
        return render_error(request, "Audit not found", f"There is no audit with the ID {source}.")
    detected = audit["working_json"] or audit["detected_json"]
    if not detected:
        return render_new(request, error="That audit has no detections to build a planogram from.")
    plan = pipeline.planogram_from_shelf(DetectedShelf.model_validate(detected), audit["store"], audit["shelf"])
    if not plan["rows"]:
        return render_new(request, error="The AI did not see any products in that photo, so there is nothing to build a planogram from.")
    return render_new(
        request,
        planogram_text=json.dumps(plan, indent=2),
        category=audit["category"],
        category_name=audit["category_name"],
        reuse_image=source,
        notice=(
            f"This planogram was built from what the AI saw in audit {source}. It describes the shelf as it is, "
            "not as it should be: edit product names, quantities and rows, then run the audit. "
            "The photo from that audit is reused."
        ),
    )


@router.get("/audits", response_class=HTMLResponse, include_in_schema=False)
async def history(
    request: Request,
    q: str = "",
    status: str = "",
    category: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
):
    db = get_db()
    page = max(page, 1)
    rows, total = db.list_audits(
        q=q, status=status, category=category, date_from=date_from, date_to=date_to,
        limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE,
    )
    filters = {"q": q, "status": status, "category": category, "date_from": date_from, "date_to": date_to}
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "active": "history",
            "stats": db.stats(),
            "audits": rows,
            "total": total,
            "page": page,
            "pages": max(math.ceil(total / PAGE_SIZE), 1),
            "filters": filters,
            "filter_query": urlencode({k: v for k, v in filters.items() if v}),
            "filtered": any(filters.values()),
        },
    )


@router.get("/audits/{audit_id}", response_class=HTMLResponse, include_in_schema=False)
async def audit_page(request: Request, audit_id: str):
    audit = get_db().get_audit(audit_id)
    if audit is None:
        return render_error(request, "Audit not found", f"There is no audit with the ID {audit_id}.")
    return templates.TemplateResponse(request, "result.html", {"active": "history", **audit_view(audit)})


@router.get("/audits/{audit_id}/image", include_in_schema=False)
async def audit_image(audit_id: str):
    path = get_db().image_path(audit_id)
    if path is None:
        raise HTTPException(404, "No photo is stored for this audit.")
    return FileResponse(path, media_type="image/jpeg")


@router.post("/audits", response_class=HTMLResponse, include_in_schema=False)
async def create_audit(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile | None = File(None),
    planogram: str = Form(...),
    category: str = Form(DEFAULT_CATEGORY),
    category_name: str = Form(""),
    reuse_image: str = Form(""),
):
    try:
        plan = parse_planogram(planogram)
        key, label, custom = parse_category(category, category_name)
        if file is not None and getattr(file, "filename", ""):
            image = await read_prepared_image(file)
        elif reuse_image:
            stored = get_db().read_image(reuse_image)
            if stored is None:
                raise HTTPException(400, "The photo from the earlier audit is no longer available. Please upload it again.")
            image = await prepare_bytes(stored)
        else:
            raise HTTPException(400, "Please add a shelf photo.")
        reserve_ai_run()
    except HTTPException as exc:
        # Show the problem on the form instead of a bare JSON error.
        return render_new(
            request, error=exc.detail, planogram_text=planogram,
            category=category if category in CATEGORIES else DEFAULT_CATEGORY,
            category_name=category_name, status_code=exc.status_code,
            reuse_image=reuse_image if get_db().image_path(reuse_image) else "",
        )

    audit_id = get_db().create_audit(
        category=key, category_label=label, category_name=custom,
        planogram=plan.model_dump(mode="json"), image=image.data, mime=image.mime,
        width=image.width, height=image.height, warnings=image.warnings,
    )
    background_tasks.add_task(pipeline.run_audit, audit_id)
    return RedirectResponse(f"/audits/{audit_id}", status_code=303)


@router.post("/audits/{audit_id}/retry", include_in_schema=False)
async def retry_audit(audit_id: str, background_tasks: BackgroundTasks):
    audit = get_db().get_audit(audit_id)
    if audit is None or audit["status"] == "processing":
        raise HTTPException(409, "This audit cannot be retried right now.")
    reserve_ai_run()  # a re-run uses the AI again, so it counts toward the daily limit
    if not pipeline.reset_for_retry(audit_id):
        raise HTTPException(409, "This audit cannot be retried right now.")
    background_tasks.add_task(pipeline.run_audit, audit_id)
    return RedirectResponse(f"/audits/{audit_id}", status_code=303)


def _safe_cell(value) -> str:
    """Stop a spreadsheet from running a product name that starts with = + - or @ as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def audit_csv(audit: dict, tasks: list[dict]) -> str:
    """A plain report of a scored audit: summary, planogram vs shelf, and the to-do list."""
    result = AuditResult.model_validate(audit["result_json"])
    buffer = io.StringIO()
    out = csv.writer(buffer)

    def write(*cells):
        out.writerow([_safe_cell(c) for c in cells])

    write("section", "row", "product", "expected", "found", "status", "note")
    write("summary", "", "Audit", "", "", audit["id"], audit["created_at"])
    write("summary", "", "Category", "", "", audit["category_name"] or audit["category_label"], f"{audit['store']} {audit['shelf']}".strip())
    write("summary", "", "Compliance score", "", "", f"{result.score}%", result.headline)
    write("summary", "", "Trust", "", "", result.trust_level or "", result.trust_reason)
    write("summary", "", "Units detected / missing / misplaced / unexpected", "", "",
          f"{result.detected_units} / {result.missing_units} / {result.misplaced_units} / {result.unexpected_units}", "")
    for c in result.comparison:
        for item in c.items:
            status = "in place" if item.found == item.expected else ("over" if item.found > item.expected else ("missing" if item.found == 0 else "short"))
            write("comparison", c.row, item.product, item.expected, item.found, status, "")
        for extra in c.extras:
            note = f"belongs in row {extra.belongs_in_row}" if extra.kind == "misplaced" else "not in the planogram"
            write("extra", c.row, extra.name, "", extra.facings, extra.kind, note)
    for task in tasks:
        write("task", "", task["text"], "", "", task["priority"], "done" if task["done"] else "open")
    return buffer.getvalue()


@router.get("/audits/{audit_id}/export.csv", include_in_schema=False)
async def export_csv(audit_id: str):
    db = get_db()
    audit = db.get_audit(audit_id)
    if audit is None:
        raise HTTPException(404, "Audit not found.")
    if audit["status"] != "completed":
        raise HTTPException(409, "Only an audit with a confirmed score can be exported.")
    body = "﻿" + audit_csv(audit, db.get_tasks(audit_id))  # the BOM makes Excel read it as UTF-8
    return Response(
        body, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{audit_id}.csv"'},
    )


def _delete(audit_id: str) -> None:
    db = get_db()
    audit = db.get_audit(audit_id)
    if audit is None:
        raise HTTPException(404, "Audit not found.")
    if audit["status"] == "processing":
        raise HTTPException(409, "This audit is still processing. Wait for it to finish first.")
    db.delete_audit(audit_id)


@router.post("/audits/{audit_id}/delete", include_in_schema=False)
async def delete_audit_page(audit_id: str):
    _delete(audit_id)
    return RedirectResponse("/audits", status_code=303)


@router.delete("/api/audits/{audit_id}")
async def api_delete_audit(audit_id: str) -> dict:
    _delete(audit_id)
    return {"deleted": True}


# ---- JSON API --------------------------------------------------------------------


class TaskUpdate(BaseModel):
    done: bool


@router.get("/api/categories")
async def api_categories() -> list[dict]:
    return [{"key": c.key, "label": c.label, "unit": c.unit, "sample": c.sample} for c in CATEGORIES.values()]


@router.get("/api/stats")
async def api_stats() -> dict:
    return get_db().stats()


@router.get("/api/audits")
async def api_list_audits(
    q: str = "", status: str = "", category: str = "",
    date_from: str = "", date_to: str = "", page: int = 1, page_size: int = 20,
) -> dict:
    page, page_size = max(page, 1), min(max(page_size, 1), 100)
    rows, total = get_db().list_audits(
        q=q, status=status, category=category, date_from=date_from, date_to=date_to,
        limit=page_size, offset=(page - 1) * page_size,
    )
    return {"total": total, "page": page, "page_size": page_size, "audits": rows}


@router.get("/api/audits/{audit_id}")
async def api_get_audit(audit_id: str) -> dict:
    db = get_db()
    audit = db.get_audit(audit_id)
    if audit is None:
        raise HTTPException(404, "Audit not found.")
    audit.pop("image_path", None)  # a server path is not for clients
    audit["tasks"] = db.get_tasks(audit_id)
    return audit


@router.post("/api/audits/{audit_id}/tasks/{task_id}")
async def api_update_task(audit_id: str, task_id: int, body: TaskUpdate) -> dict:
    progress = get_db().set_task_done(audit_id, task_id, body.done)
    if progress is None:
        raise HTTPException(404, "Task not found.")
    return progress


class ConfirmUpdate(BaseModel):
    confirmed: bool = True


@router.post("/api/audits/{audit_id}/confirm")
async def api_confirm(audit_id: str, body: ConfirmUpdate) -> dict:
    """Mark a finished result as checked and accepted by a person (or undo that)."""
    audit = get_db().get_audit(audit_id)
    if audit is None:
        raise HTTPException(404, "Audit not found.")
    if audit["status"] != "completed":
        raise HTTPException(409, "Only a completed audit with a score can be confirmed.")
    return {"confirmed_at": get_db().set_confirmed(audit_id, body.confirmed)}


@router.post("/api/audits/{audit_id}/corrections")
async def api_corrections(audit_id: str, body: CorrectionRequest) -> dict:
    audit = get_db().get_audit(audit_id)
    if audit is None:
        raise HTTPException(404, "Audit not found.")
    if audit["status"] == "processing":
        raise HTTPException(409, "This audit is still processing.")
    result = pipeline.apply_corrections(audit_id, body)
    updated = get_db().get_audit(audit_id)
    return {
        "status": updated["status"],
        "score": result.score if result else None,
        "complete": bool(result and result.complete),
    }
