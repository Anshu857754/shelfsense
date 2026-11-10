"""ShelfSense API and website.

API:  /health, /describe, /detect, /score, /audit   (try them at /docs)
Site: /  (upload a photo + planogram)  ->  /audit-ui  (result page)
"""

import json
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ValidationError

from app import compliance, vision
from app.config import get_settings
from app.schemas import AuditResult, DetectedShelf, Planogram

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Pre-filled on the upload page so you can see the format. Edit it to match your shelf.
SAMPLE_PLANOGRAM = json.dumps(
    {
        "store": "Store-101",
        "shelf": "Snacks-A",
        "rows": [
            {
                "row": 1,
                "expected": [
                    {"product": "Chips bag", "facings": 6, "aliases": ["snack bag", "tortilla chips bag"]}
                ],
            },
            {
                "row": 2,
                "expected": [
                    {"product": "Glass bottle", "facings": 4},
                    {"product": "Jar", "facings": 2, "aliases": ["jar or can"]},
                ],
            },
        ],
    },
    indent=2,
)

app = FastAPI(title="ShelfSense")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ---- Helpers ---------------------------------------------------------------------


def detect_image_type(data: bytes) -> str | None:
    """Identify the real image type from the file's first bytes.

    The Content-Type header is sent by the client and can be wrong or faked,
    so we check the file signature too.
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def read_valid_image(file: UploadFile) -> tuple[bytes, str]:
    """Validate an upload (type, size, real content) and return (bytes, mime type)."""
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(400, "Only JPEG, PNG or WEBP images are allowed.")

    # Read one byte past the limit so we can tell "too big" without loading a huge file.
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "Image is larger than 5 MB.")

    mime_type = detect_image_type(data)
    if mime_type is None:
        raise HTTPException(400, "File content is not a valid JPEG, PNG or WEBP image.")
    return data, mime_type


def parse_planogram(text: str) -> Planogram:
    """Parse planogram JSON text, turning problems into a readable 400 error."""
    try:
        return Planogram.model_validate_json(text)
    except ValidationError as exc:
        parts = [
            f"{'.'.join(str(p) for p in e['loc']) or 'planogram'}: {e['msg']}"
            for e in exc.errors()[:5]
        ]
        raise HTTPException(400, "Invalid planogram: " + "; ".join(parts)) from exc


async def detect_or_raise(data: bytes, mime_type: str) -> DetectedShelf:
    """Run vision detection and map failures to HTTP errors."""
    try:
        return await vision.detect_shelf(data, mime_type)
    except vision.MissingAPIKeyError as exc:
        # Server configuration problem, not the caller's fault.
        raise HTTPException(500, str(exc)) from exc
    except vision.VisionError as exc:
        # Covers OpenRouter failures and replies that stayed invalid after retries.
        raise HTTPException(502, str(exc)) from exc


async def audit_upload(
    file: UploadFile, planogram_text: str
) -> tuple[DetectedShelf, AuditResult]:
    planogram = parse_planogram(planogram_text)
    data, mime_type = await read_valid_image(file)
    shelf = await detect_or_raise(data, mime_type)
    return shelf, compliance.audit(planogram, shelf)


# ---- API -------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/describe")
async def describe(file: UploadFile = File(...)) -> dict[str, str]:
    data, mime_type = await read_valid_image(file)

    try:
        description = await vision.describe_shelf(data, mime_type)
    except vision.MissingAPIKeyError as exc:
        raise HTTPException(500, str(exc)) from exc
    except vision.VisionError as exc:
        raise HTTPException(502, str(exc)) from exc

    return {"model": get_settings().openrouter_model, "description": description}


@app.post("/detect")
async def detect(file: UploadFile = File(...)) -> dict:
    """Return the shelf as validated, structured JSON (rows, products, counts, gaps)."""
    data, mime_type = await read_valid_image(file)
    shelf = await detect_or_raise(data, mime_type)
    return {"model": get_settings().openrouter_model, "shelf": shelf.model_dump()}


class ScoreRequest(BaseModel):
    planogram: Planogram
    detected: DetectedShelf


@app.post("/score", response_model=AuditResult)
async def score(body: ScoreRequest) -> AuditResult:
    """Score hand-written detection data against a planogram. No image, no model call, free."""
    return compliance.audit(body.planogram, body.detected)


@app.post("/audit")
async def audit_endpoint(
    file: UploadFile = File(...), planogram: str = Form(...)
) -> dict:
    """Photo + planogram JSON in; detected shelf + compliance result out."""
    shelf, result = await audit_upload(file, planogram)
    return {
        "model": get_settings().openrouter_model,
        "detected": shelf.model_dump(),
        "audit": result.model_dump(),
    }


# ---- Website ---------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home(request: Request):
    return templates.TemplateResponse(
        request, "index.html", {"planogram_text": SAMPLE_PLANOGRAM, "error": None}
    )


@app.post("/audit-ui", response_class=HTMLResponse, include_in_schema=False)
async def audit_ui(
    request: Request, file: UploadFile = File(...), planogram: str = Form(...)
):
    try:
        shelf, result = await audit_upload(file, planogram)
    except HTTPException as exc:
        # Show the problem on the form instead of a bare JSON error.
        return templates.TemplateResponse(
            request,
            "index.html",
            {"planogram_text": planogram, "error": exc.detail},
            status_code=exc.status_code,
        )
    return templates.TemplateResponse(
        request,
        "result.html",
        {
            "model": get_settings().openrouter_model,
            "audit": result.model_dump(),
            "detected": shelf.model_dump(),
            "detected_json": json.dumps(shelf.model_dump(), indent=2),
        },
    )
