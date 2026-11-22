"""ShelfSense API and website.

Website:    /  (dashboard)  /new  /audits  /audits/{id}        (see app/routes.py)
Audit API:  /api/audits ...                                     (see app/routes.py)
Direct API: /health  /describe  /detect  /score  /audit         (stateless, kept for compatibility)
"""

import asyncio
import base64
import binascii
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import compliance, pipeline, routes, vision
from app.categories import DEFAULT_CATEGORY
from app.config import get_settings
from app.db import get_db
from app.schemas import AuditResult, DetectedShelf, Planogram
from app.uploads import (  # noqa: F401  (re-exported: other code imports these from app.main)
    ALLOWED_TYPES,
    MAX_UPLOAD_BYTES,
    parse_category,
    parse_planogram,
    read_prepared_image,
    reserve_ai_run,
)
from app.web import BASE_DIR

log = logging.getLogger("shelfsense")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=getattr(logging, get_settings().log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # A restart (a crash, a redeploy, or saving a file under --reload) can interrupt audits that
    # were running. Everything they need is saved, so run them again instead of failing them.
    resume, gave_up = get_db().recover_interrupted()
    if gave_up:
        log.warning("gave up on %s interrupted audit(s) that were too old or kept failing", gave_up)
    running: set[asyncio.Task] = set()
    for audit_id in resume:
        log.warning("resuming audit %s that was interrupted by a restart", audit_id)
        task = asyncio.create_task(pipeline.run_audit(audit_id))
        running.add(task)  # keep a reference so the task is not garbage collected mid-run
        task.add_done_callback(running.discard)
    yield


app = FastAPI(title="ShelfSense", lifespan=lifespan)


def _password_ok(header: str, password: str) -> bool:
    """True if an Authorization header carries the right password (any username)."""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        supplied = base64.b64decode(token).decode("utf-8").partition(":")[2]
    except (binascii.Error, UnicodeDecodeError):
        return False
    return secrets.compare_digest(supplied.encode(), password.encode())


@app.middleware("http")
async def require_password(request: Request, call_next):
    """If ACCESS_PASSWORD is set, nothing but the health check is reachable without it."""
    password = get_settings().access_password
    if password and request.url.path != "/health":
        if not _password_ok(request.headers.get("authorization", ""), password):
            return Response(
                "A password is required.", status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="ShelfSense", charset="UTF-8"'},
            )
    return await call_next(request)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(routes.router)


# ---- Direct API (no database; same contracts as before) --------------------------


async def detect_or_raise(
    data: bytes,
    mime_type: str,
    planogram: Planogram | None = None,
    category: str | None = None,
    custom_category: str = "",
) -> DetectedShelf:
    """Run vision detection and map failures to HTTP errors."""
    try:
        return await vision.detect_shelf(
            data, mime_type, planogram=planogram, category=category, custom_category=custom_category
        )
    except vision.MissingAPIKeyError as exc:
        # Server configuration problem, not the caller's fault.
        raise HTTPException(500, str(exc)) from exc
    except vision.VisionError as exc:
        # Covers OpenRouter failures and replies that stayed invalid after retries.
        raise HTTPException(502, str(exc)) from exc


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/describe")
async def describe(file: UploadFile = File(...)) -> dict[str, str]:
    image = await read_prepared_image(file)
    reserve_ai_run()
    try:
        description = await vision.describe_shelf(image.data, image.mime)
    except vision.MissingAPIKeyError as exc:
        raise HTTPException(500, str(exc)) from exc
    except vision.VisionError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"model": get_settings().openrouter_model, "description": description}


@app.post("/detect")
async def detect(file: UploadFile = File(...)) -> dict:
    """Return the shelf as validated, structured JSON (rows, products, counts, confidence...)."""
    image = await read_prepared_image(file)
    reserve_ai_run()
    shelf = await detect_or_raise(image.data, image.mime)
    return {"model": get_settings().openrouter_model, "shelf": shelf.model_dump()}


class ScoreRequest(BaseModel):
    planogram: Planogram
    detected: DetectedShelf
    confirmed_empty: bool = False


@app.post("/score", response_model=AuditResult)
async def score(body: ScoreRequest) -> AuditResult:
    """Score hand-written detection data against a planogram. No image, no model call, free."""
    return compliance.audit(
        body.planogram,
        body.detected,
        confidence_threshold=get_settings().confidence_threshold,
        allow_empty=body.confirmed_empty,
    )


@app.post("/audit")
async def audit_endpoint(
    file: UploadFile = File(...),
    planogram: str = Form(...),
    category: str = Form(DEFAULT_CATEGORY),
    category_name: str = Form(""),
) -> dict:
    """Photo + planogram JSON in; detected shelf + compliance result out (not stored)."""
    plan = parse_planogram(planogram)
    key, _, custom = parse_category(category, category_name)
    image = await read_prepared_image(file)
    reserve_ai_run()
    shelf = await detect_or_raise(image.data, image.mime, plan, key, custom)
    result = compliance.audit(
        plan, shelf, confidence_threshold=get_settings().confidence_threshold
    )
    return {
        "model": get_settings().openrouter_model,
        "detected": shelf.model_dump(),
        "audit": result.model_dump(),
    }
