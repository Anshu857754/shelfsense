"""OpenRouter vision call. Day 1: send a shelf photo, get a text description back."""

import base64

import httpx

from app.config import Settings, get_settings

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT_SECONDS = 60

PROMPT = (
    "This is a photo of a retail store shelf. For each shelf row (top to bottom), "
    "briefly describe the products you can see and point out any empty gaps."
)


class VisionError(Exception):
    """Base class for errors raised while calling the vision model."""


class MissingAPIKeyError(VisionError):
    """OPENROUTER_API_KEY (or the model name) is not configured."""


class OpenRouterError(VisionError):
    """OpenRouter was unreachable or returned an unusable response."""


async def describe_shelf(
    image_bytes: bytes, mime_type: str, settings: Settings | None = None
) -> str:
    """Send the image to the configured vision model and return its text reply."""
    settings = settings or get_settings()
    if not settings.openrouter_api_key:
        raise MissingAPIKeyError("OPENROUTER_API_KEY is not set. Add it to your .env file.")
    if not settings.openrouter_model:
        raise MissingAPIKeyError("OPENROUTER_MODEL is not set. Add it to your .env file.")

    # OpenRouter accepts images as base64 data URLs inside the message content.
    data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    payload = {
        "model": settings.openrouter_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"}

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(OPENROUTER_URL, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise OpenRouterError(f"Could not reach OpenRouter: {type(exc).__name__}") from exc

    if response.status_code != 200:
        # Keep the message short so we never dump a huge or sensitive body.
        raise OpenRouterError(
            f"OpenRouter returned status {response.status_code}: {response.text[:200]}"
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise OpenRouterError("OpenRouter returned a non-JSON response.") from exc

    choices = body.get("choices") or []
    if not choices:
        raise OpenRouterError("OpenRouter returned no choices.")

    text = (choices[0].get("message") or {}).get("content")
    if not text:
        raise OpenRouterError("OpenRouter returned an empty reply.")
    return text
