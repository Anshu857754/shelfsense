"""OpenRouter vision calls.

- describe_shelf: free-text description (Day 1)
- detect_shelf:   strict JSON validated against app.schemas, with retry (Day 2)
"""

import base64
import json

import httpx
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.schemas import DetectedShelf

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT_SECONDS = 60
MAX_ATTEMPTS = 3  # first try + 2 retries

DESCRIBE_PROMPT = (
    "This is a photo of a retail store shelf. For each shelf row (top to bottom), "
    "briefly describe the products you can see and point out any empty gaps."
)

DETECT_PROMPT = """You are auditing a photo of a retail store shelf.

Reply with ONLY a JSON object. No markdown, no code fences, no text before or after.

Schema:
{
  "rows": [
    {
      "row": 1,
      "products": [{"name": "Coca-Cola 500ml", "facings": 6}],
      "empty_gaps": 2,
      "misplaced_items": ["Pepsi bottle among the Coca-Cola"]
    }
  ],
  "overall_notes": "short free-text remark, or an empty string"
}

Rules:
- Number rows from the top shelf row (1) downward.
- "facings" is the number of units of that product standing side by side at the front.
  COUNT them one by one. Never merge neighbouring identical items into one.
- Each distinct product appears once per row, with its total count in that row.
- "empty_gaps" is the number of empty spots on that row where a product is missing.
- "misplaced_items" lists items that look out of place in that row (empty list if none).
- Use the product name as printed on the label. If unreadable, use a short description
  such as "red soda can".
- If you are unsure about a count, give your best estimate. Never leave a number blank.
- All numbers must be whole numbers, 0 or greater."""


class VisionError(Exception):
    """Base class for errors raised while calling the vision model."""


class MissingAPIKeyError(VisionError):
    """OPENROUTER_API_KEY (or the model name) is not configured."""


class OpenRouterError(VisionError):
    """OpenRouter was unreachable or returned an unusable response."""


class InvalidModelOutputError(VisionError):
    """The model never produced JSON that fits the schema, even after retries."""


def _image_message(prompt: str, image_bytes: bytes, mime_type: str) -> dict:
    """Build a user message holding text plus the image as a base64 data URL."""
    data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
    }


async def _call_openrouter(messages: list[dict], settings: Settings) -> str:
    """POST a chat conversation to OpenRouter and return the reply text."""
    if not settings.openrouter_api_key:
        raise MissingAPIKeyError("OPENROUTER_API_KEY is not set. Add it to your .env file.")
    if not settings.openrouter_model:
        raise MissingAPIKeyError("OPENROUTER_MODEL is not set. Add it to your .env file.")

    payload = {"model": settings.openrouter_model, "messages": messages}
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


async def describe_shelf(
    image_bytes: bytes, mime_type: str, settings: Settings | None = None
) -> str:
    """Send the image to the configured vision model and return its text reply."""
    settings = settings or get_settings()
    messages = [_image_message(DESCRIBE_PROMPT, image_bytes, mime_type)]
    return await _call_openrouter(messages, settings)


def parse_shelf_json(text: str) -> DetectedShelf:
    """Turn the model's reply into a validated DetectedShelf.

    Raises ValueError (pydantic's ValidationError is a subclass) if the reply
    has no JSON or the JSON doesn't fit the schema.
    """
    # Models sometimes wrap JSON in ```json fences or add a sentence around it,
    # so take everything from the first '{' to the last '}'.
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("No JSON object found in the reply.")
    data = json.loads(text[start : end + 1])
    return DetectedShelf.model_validate(data)


def _summarize_error(exc: ValueError) -> str:
    """Short, model-readable description of what was wrong with a reply."""
    if isinstance(exc, ValidationError):
        parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]]
        return "; ".join(parts)
    return str(exc)[:300]


async def detect_shelf(
    image_bytes: bytes,
    mime_type: str,
    settings: Settings | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> DetectedShelf:
    """Ask the model for structured shelf data, retrying when its JSON is invalid."""
    settings = settings or get_settings()
    messages = [_image_message(DETECT_PROMPT, image_bytes, mime_type)]
    last_error = ""

    for _ in range(max_attempts):
        reply = await _call_openrouter(messages, settings)
        try:
            return parse_shelf_json(reply)
        except ValueError as exc:
            last_error = _summarize_error(exc)
            # Show the model its own bad reply plus what was wrong, then ask again.
            messages.append({"role": "assistant", "content": reply})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"That reply was not valid: {last_error}. "
                        "Reply again with ONLY the JSON object matching the schema."
                    ),
                }
            )

    raise InvalidModelOutputError(
        f"Model did not return valid shelf JSON after {max_attempts} attempts: {last_error}"
    )
