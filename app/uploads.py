"""Shared helpers for receiving a photo and a planogram from a request."""

import asyncio

from fastapi import HTTPException, UploadFile
from pydantic import ValidationError

from app.categories import CATEGORIES, CUSTOM_CATEGORY, DEFAULT_CATEGORY, category_label
from app.config import get_settings
from app.imaging import ImageError, PreparedImage, prepare_image
from app.schemas import Planogram

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}


async def read_upload(file: UploadFile) -> bytes:
    """Read an uploaded photo, enforcing type and size limits."""
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(400, "Only JPEG, PNG or WEBP images are allowed.")
    # Read one byte past the limit so we can tell "too big" without loading a huge file.
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "Image is larger than 5 MB.")
    return data


async def prepare_bytes(data: bytes) -> PreparedImage:
    """Fix rotation, shrink, and measure sharpness. Unusable images become a 400 error."""
    try:
        return await asyncio.to_thread(prepare_image, data, get_settings().max_image_side)
    except ImageError as exc:
        raise HTTPException(400, str(exc)) from exc


async def read_prepared_image(file: UploadFile) -> PreparedImage:
    """Validate the upload, fix its rotation, shrink it, and measure its sharpness."""
    return await prepare_bytes(await read_upload(file))


def reserve_ai_run() -> None:
    """Count one AI run against today's limit, or refuse with a clear message when it is used up.

    Called just before an audit would use the AI, so mistakes (a bad file, a bad planogram) never
    use up the allowance. With no limit configured it still counts, which is harmless.
    """
    from app.db import get_db  # imported here: db.py is loaded after config in app start-up

    limit = get_settings().max_audits_per_day
    database = get_db()
    if limit and database.runs_today() >= limit:
        raise HTTPException(
            429, f"The daily limit of {limit} audits has been reached. Please try again tomorrow."
        )
    database.bump_runs()


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


def parse_category(key: str, custom_name: str = "") -> tuple[str, str, str]:
    """Validate the chosen retail category. Returns (key, label, custom_name)."""
    key = (key or DEFAULT_CATEGORY).strip()
    if key not in CATEGORIES:
        raise HTTPException(400, f"Unknown retail category: {key!r}.")
    custom_name = custom_name.strip()[:60] if key == CUSTOM_CATEGORY else ""
    return key, category_label(key, custom_name), custom_name
