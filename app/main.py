"""ShelfSense API. Day 2: health, raw description, and structured detection."""

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import RedirectResponse

from app import vision
from app.config import get_settings

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}

app = FastAPI(title="ShelfSense")


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


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    # Opening the bare address sends you to the interactive docs page.
    return RedirectResponse(url="/docs")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/describe")
async def describe(file: UploadFile = File(...)) -> dict[str, str]:
    data, mime_type = await read_valid_image(file)

    try:
        description = await vision.describe_shelf(data, mime_type)
    except vision.MissingAPIKeyError as exc:
        # Server configuration problem, not the caller's fault.
        raise HTTPException(500, str(exc)) from exc
    except vision.VisionError as exc:
        raise HTTPException(502, str(exc)) from exc

    return {"model": get_settings().openrouter_model, "description": description}


@app.post("/detect")
async def detect(file: UploadFile = File(...)) -> dict:
    """Return the shelf as validated, structured JSON (rows, products, counts, gaps)."""
    data, mime_type = await read_valid_image(file)

    try:
        shelf = await vision.detect_shelf(data, mime_type)
    except vision.MissingAPIKeyError as exc:
        raise HTTPException(500, str(exc)) from exc
    except vision.VisionError as exc:
        # Covers OpenRouter failures and replies that stayed invalid after retries.
        raise HTTPException(502, str(exc)) from exc

    return {"model": get_settings().openrouter_model, "shelf": shelf.model_dump()}
