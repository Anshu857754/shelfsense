"""OpenRouter vision calls.

- describe_shelf: free-text description (Day 1)
- detect_shelf:   structured detections, validated against app.schemas, with retry

The detection prompt tells the model the retail category and the planogram, so it can say
which planogram product each group of items is. Matching free-text names with string
comparison alone does not work (the model says "Doritos bags", the planogram says
"Chips bag"), so the model is asked to resolve names itself and report how sure it is.
"""

import asyncio
import base64
import contextvars
import json
import logging
import time

import httpx
from pydantic import ValidationError

from app import imaging
from app.categories import category_label, get_category
from app.compliance import is_uncertain, pick_median_pass
from app.config import Settings, get_settings
from app.schemas import DetectedProduct, DetectedShelf, Planogram
from app.schemas import to_confidence

log = logging.getLogger("shelfsense.vision")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT_SECONDS = 90
MAX_ATTEMPTS = 3  # first try + 2 retries (for answers that are not valid JSON)
# For temporary problems reaching the service: seconds to wait before the 2nd and 3rd try.
RETRY_DELAYS = (1.0, 3.0)
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

DESCRIBE_PROMPT = (
    "This is a photo of a retail store shelf. For each shelf row (top to bottom), "
    "briefly describe the products you can see and point out any empty gaps."
)

# How much detail the model is asked for. Less detail means a shorter answer, so a faster audit.
#   full    : everything (position, attributes, box). Best for review, slowest.
#   lean    : drops position and attributes; keeps the box so the photo overlay still works.
#   minimal : also drops the box. Fastest. No overlay and no zoomed second look.
DETAIL_LEVELS = ("full", "lean", "minimal")


def _schema_example(detail: str = "full") -> str:
    """The reply format shown to the model, with only the fields this detail level asks for."""
    product: dict = {
        "name": "blue denim jeans",
        "category": "jeans",
        "quantity": 5,
        "matched_expected": "Jeans",
        "confidence": 0.9,
        "count_confidence": 0.6,
    }
    if detail == "full":
        product["position"] = "left"
        product["attributes"] = ["blue", "folded stack"]
    if detail in ("full", "lean"):
        product["box"] = {"x": 0.05, "y": 0.10, "w": 0.40, "h": 0.20}
    example = {
        "shelf_visible": True,
        "shelf_empty": False,
        "image_issues": [],
        "rows": [{"row": 1, "products": [product]}],
        "overall_notes": "short free-text remark, or an empty string",
    }
    return json.dumps(example, indent=2)


# (needed at detail level, rule text). "core" rules are always sent.
_RULE_LIST = [
    ("core", "Number rows from the top (1) downward. For wall displays and hanging racks, treat each horizontal level as a row."),
    ("core", "One entry per distinct product per row. Never merge different products into one entry, and never split identical neighbours."),
    ("core", '"quantity" is how many units of that product are visible side by side at the front. COUNT them one by one. For folded piles, count each visible folded item you can tell apart (or each visible pile if you cannot), and say how rough the number is in "count_confidence". Always give your best estimate of the number.'),
    ("core", '"matched_expected": if the entry is one of the planogram products, copy that product\'s name EXACTLY as written in the planogram. Use null if it is not a planogram product or you are not sure.'),
    ("core", '"name": the product name as printed, or, if unreadable, a short plain description such as "blue denim jeans". Keep it to 2 to 5 words, with no brackets, no explanations and no guesses in it. Never invent brand names you cannot read.'),
    ("core", '"category": your own short label for the type of product (for example "snacks", "jeans", "lipstick").'),
    ("core", '"confidence": how sure you are about WHAT the item is (its kind of product), from 0 to 1. Use 0.85 to 1 when you can clearly tell what it is, 0.6 to 0.85 when you can tell the kind of product but not the details, 0.4 to 0.6 when you are really guessing, and below 0.4 when you cannot tell. Judge this separately from the count: a pile of folded shirts that you can plainly see is "shirts" with high confidence even if you cannot count every one.'),
    ("core", '"count_confidence": how sure you are about the NUMBER in "quantity", from 0 to 1. Use 0.85 to 1 when you counted every unit one by one, 0.6 to 0.85 when it is probably within 1 or 2, and below 0.6 when it is a rough estimate (folded piles, overlapping or partly hidden items). Be honest, and do not leave a count out because it is rough.'),
    ("full", '"position": where in the row the group sits ("left", "center", "right", "far left", and so on).'),
    ("full", '"attributes": up to 4 short visual facts (colour, size, visible text, packaging).'),
    ("box", '"box": a rectangle around ALL units of that entry. x and y are its top-left corner and w and h its width and height, all as fractions from 0 to 1 of the whole image ((0,0) is the top-left of the photo, (1,1) the bottom-right). Leave "box" out if you cannot locate it.'),
    ("core", "Report only what you can actually see. The planogram is a checklist, not a list of facts: do not assume a listed product is present, and do not leave out products that are not on the planogram."),
    ("core", 'If the photo is not a shelf, or is too blurry, dark or cropped to audit, set "shelf_visible" to false, explain in "image_issues", and return "rows" as [].'),
    ("core", 'If the shelf is clearly visible but has NO products on it at all, set "shelf_empty" to true, return its rows with empty "products" lists, and say so in "overall_notes". An empty shelf is a valid answer, not a failure.'),
]


def _rules(detail: str = "full") -> str:
    keep = {"core"}
    if detail == "full":
        keep |= {"full"}
    if detail in ("full", "lean"):
        keep |= {"box"}
    chosen = [text for need, text in _RULE_LIST if need in keep]
    return "Rules:\n" + "\n".join(f"{n}. {text}" for n, text in enumerate(chosen, start=1))

def build_detect_prompt(
    planogram: Planogram | None = None,
    category: str | None = None,
    custom_category: str = "",
    detail: str = "full",
) -> str:
    """The instructions sent with the photo. Category and planogram make it specific."""
    if detail not in DETAIL_LEVELS:
        detail = "full"
    cat = get_category(category)
    label = category_label(category, custom_category)

    parts = ["You are a retail shelf auditor."]
    if category:
        parts.append(
            f"The photo shows a store shelf, display or rack in the category: {label}. {cat.hint} "
            f"For this category, a quantity counts {cat.unit}."
        )
    else:
        parts.append("The photo shows a store shelf, display or rack.")

    if planogram is not None:
        lines = []
        for prow in sorted(planogram.rows, key=lambda r: r.row):
            for exp in prow.expected:
                extra = f" (also called: {', '.join(exp.aliases)})" if exp.aliases else ""
                kind = f" [{exp.category}]" if exp.category else ""
                lines.append(f"  Row {prow.row}: {exp.product}{kind} x{exp.facings}{extra}")
        parts.append(
            "PLANOGRAM (what SHOULD be on the shelf; row 1 is the top):\n" + "\n".join(lines)
        )

    parts.append(
        "Reply with ONLY one JSON object. No markdown, no code fences, no text before or after.\n\n"
        "Schema:\n" + _schema_example(detail)
    )
    parts.append(_rules(detail))
    return "\n\n".join(parts)


# Kept for callers that want the generic, planogram-free prompt.
DETECT_PROMPT = build_detect_prompt()


class VisionError(Exception):
    """Base class for errors raised while calling the vision model."""


class MissingAPIKeyError(VisionError):
    """OPENROUTER_API_KEY (or the model name) is not configured."""


class OpenRouterError(VisionError):
    """OpenRouter was unreachable or returned an unusable response."""


class InvalidModelOutputError(VisionError):
    """The model never produced JSON that fits the schema, even after retries."""


_usage: contextvars.ContextVar[dict | None] = contextvars.ContextVar("shelfsense_usage", default=None)


def track_usage() -> dict:
    """Start counting this job's AI calls: how many, how long, and how many tokens.

    Returns the dictionary that is filled in as calls happen, so the caller can store it.
    """
    stats = {"calls": 0, "seconds": 0.0, "prompt_tokens": 0, "completion_tokens": 0}
    _usage.set(stats)
    return stats


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

    started = time.monotonic()
    response = None
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        # A brief network hiccup or a busy service should not cost a whole audit: try a few times.
        for attempt, delay in enumerate((0.0, *RETRY_DELAYS)):
            if delay:
                await asyncio.sleep(delay)
            last_try = attempt == len(RETRY_DELAYS)
            try:
                response = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                log.warning("OpenRouter request failed (%s), try %s of %s", type(exc).__name__, attempt + 1, len(RETRY_DELAYS) + 1)
                if last_try:
                    log.error("OpenRouter request failed: %s", type(exc).__name__)
                    raise OpenRouterError(f"Could not reach OpenRouter: {type(exc).__name__}") from exc
                continue
            if response.status_code in RETRYABLE_STATUS and not last_try:
                log.warning("OpenRouter answered %s, trying again", response.status_code)
                continue
            break

    log.info(
        "OpenRouter %s answered %s in %.1fs",
        settings.openrouter_model, response.status_code, time.monotonic() - started,
    )
    if response.status_code != 200:
        # Keep the message short so we never dump a huge or sensitive body.
        log.error("OpenRouter error %s: %s", response.status_code, response.text[:300])
        raise OpenRouterError(
            f"OpenRouter returned status {response.status_code}: {response.text[:200]}"
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise OpenRouterError("OpenRouter returned a non-JSON response.") from exc

    stats = _usage.get()
    if stats is not None:  # a job asked for call statistics (see track_usage)
        used = body.get("usage") or {}
        stats["calls"] += 1
        stats["seconds"] = round(stats["seconds"] + (time.monotonic() - started), 2)
        stats["prompt_tokens"] += int(used.get("prompt_tokens") or 0)
        stats["completion_tokens"] += int(used.get("completion_tokens") or 0)

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
    return DetectedShelf.model_validate(_first_json_object(text))


def _first_json_object(text: str) -> dict:
    """The first complete JSON object in a reply, ignoring anything before or after it.

    Models sometimes wrap JSON in ```json fences, add a sentence around it, or add a note with
    braces after it. Reading just the first complete object avoids rejecting a good answer (and
    paying for a second AI call) because of the extra words.
    """
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in the reply.")
    data, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(data, dict):
        raise ValueError("The reply did not contain a JSON object.")
    return data


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
    planogram: Planogram | None = None,
    category: str | None = None,
    custom_category: str = "",
    detail: str | None = None,
) -> DetectedShelf:
    """Ask the model for structured shelf data, retrying when its JSON is invalid.

    detail is "full", "lean" or "minimal" (see DETAIL_LEVELS); the default comes from settings.
    """
    settings = settings or get_settings()
    prompt = build_detect_prompt(planogram, category, custom_category, detail or settings.detail_level)
    messages = [_image_message(prompt, image_bytes, mime_type)]
    last_error = ""

    for attempt in range(1, max_attempts + 1):
        reply = await _call_openrouter(messages, settings)
        try:
            shelf = parse_shelf_json(reply)
        except ValueError as exc:
            last_error = _summarize_error(exc)
            log.warning("attempt %s/%s: reply was not valid shelf JSON: %s", attempt, max_attempts, last_error)
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
            continue
        log.info(
            "detected %s product groups in %s rows (attempt %s)",
            sum(len(r.products) for r in shelf.rows), len(shelf.rows), attempt,
        )
        return shelf

    raise InvalidModelOutputError(
        f"Model did not return valid shelf JSON after {max_attempts} attempts: {last_error}"
    )


async def detect_best_of(
    image_bytes: bytes,
    mime_type: str,
    settings: Settings,
    passes: int = 1,
    planogram: Planogram | None = None,
    category: str | None = None,
    custom_category: str = "",
    detail: str | None = None,
) -> tuple[DetectedShelf, list[float]]:
    """Look at the photo `passes` times at once and keep the look with the middle score.

    Counts on crowded shelves vary from look to look, so one lucky or unlucky look can move the
    percentage by tens of points. The middle of several looks is steadier, and how far apart the
    looks are tells how much to trust the answer. Returns (the chosen look, every look's score
    sorted; empty when there was only one look). A look that fails is skipped as long as another
    one worked.
    """
    kwargs = dict(planogram=planogram, category=category, custom_category=custom_category, detail=detail)
    if passes <= 1:
        return await detect_shelf(image_bytes, mime_type, settings, **kwargs), []

    outcomes = await asyncio.gather(
        *(detect_shelf(image_bytes, mime_type, settings, **kwargs) for _ in range(passes)),
        return_exceptions=True,
    )
    for outcome in outcomes:
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
    shelves = [o for o in outcomes if isinstance(o, DetectedShelf)]
    if not shelves:
        raise next(o for o in outcomes if isinstance(o, BaseException))  # all failed: report why
    if len(shelves) < passes:
        log.warning("%s of %s looks failed; continuing with the rest", passes - len(shelves), passes)
    if planogram is None:
        return shelves[0], []
    index, scores = pick_median_pass(planogram, shelves, confidence_threshold=settings.confidence_threshold)
    log.info("looked %s times, scores %s, using look %s", len(shelves), scores, index + 1)
    return shelves[index], scores


# ---- Second look: zoom in on what the first pass was unsure about --------------------

MAX_VERIFY_CROPS = 6


def verification_candidates(
    shelf: DetectedShelf, confidence_threshold: float, limit: int = MAX_VERIFY_CROPS
) -> list[tuple[int, DetectedProduct]]:
    """Uncertain detections that have a box, biggest first: the ones worth a closer look."""
    found = [
        (row.row, product)
        for row in shelf.rows
        for product in row.products
        if product.facings > 0 and product.box is not None and is_uncertain(product, confidence_threshold)
    ]
    found.sort(key=lambda item: item[1].facings, reverse=True)
    return found[:limit]


def _verify_prompt(count: int, planogram: Planogram | None) -> str:
    names = ""
    if planogram is not None:
        listed = sorted({exp.product for prow in planogram.rows for exp in prow.expected})
        names = "The planogram products are: " + ", ".join(listed) + "."
    return (
        f"You are double-checking parts of a retail shelf photo. Below are {count} zoomed-in crops. "
        "Each was cut from the shelf photo around one group of products that an earlier look was not sure about.\n\n"
        "For EACH crop, look closely and reply with ONLY one JSON object (no markdown, no code fences):\n"
        '{"items": [{"crop": 1, "name": "white folded shirts", "quantity": 4, "matched_expected": null, '
        '"confidence": 0.9, "count_confidence": 0.7, "attributes": ["white", "folded"]}]}\n\n'
        "Rules:\n"
        '- "crop" is the crop number; give one entry per crop.\n'
        '- "name": a short plain description of what you now see.\n'
        '- "quantity": how many units of it you can count in the crop. Use 0 if the crop does not show it.\n'
        f'- "matched_expected": the exact planogram product name if it is one of them, otherwise null. {names}\n'
        '- "confidence" is how sure you are of WHAT it is and "count_confidence" how sure you are of the number '
        "(0 to 1, honest). A closer view often lets you be more sure, but never claim certainty you do not have.\n"
        '- "attributes": up to 4 short visual facts.'
    )


async def verify_detections(
    image_bytes: bytes,
    shelf: DetectedShelf,
    settings: Settings | None = None,
    planogram: Planogram | None = None,
    confidence_threshold: float = 0.5,
) -> int:
    """Re-check uncertain detections on zoomed-in crops. Updates the shelf in place.

    An item is only upgraded when the second look is itself confident (at or above the threshold);
    otherwise it stays uncertain. Returns how many items were confirmed. Raises VisionError or
    ValueError if the model fails, so the caller decides whether that matters.
    """
    settings = settings or get_settings()
    candidates = verification_candidates(shelf, confidence_threshold)
    if not candidates:
        return 0

    content: list[dict] = [{"type": "text", "text": _verify_prompt(len(candidates), planogram)}]
    for number, (row, product) in enumerate(candidates, start=1):
        crop = imaging.crop_region(image_bytes, product.box)
        data_url = f"data:image/jpeg;base64,{base64.b64encode(crop).decode('ascii')}"
        content.append({"type": "text", "text": f"Crop {number} (shelf row {row}). Earlier described as: {product.name}"})
        content.append({"type": "image_url", "image_url": {"url": data_url}})

    reply = await _call_openrouter([{"role": "user", "content": content}], settings)
    items = _first_json_object(reply).get("items") or []

    confirmed = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        number = item.get("crop")
        if not isinstance(number, int) or not 1 <= number <= len(candidates):
            continue
        _, product = candidates[number - 1]
        new_confidence = to_confidence(item.get("confidence"))
        quantity = item.get("quantity")
        if new_confidence is None or new_confidence < confidence_threshold:
            continue  # the closer look is not sure either: it stays uncertain
        if not isinstance(quantity, (int, float)) or isinstance(quantity, bool) or quantity < 1:
            continue  # the crop did not show this product

        name = str(item.get("name") or "").strip()
        if name:
            product.name = name
        product.facings = int(round(quantity))
        matched = str(item.get("matched_expected") or "").strip()
        if matched:
            product.matched_expected = matched
        product.confidence = new_confidence
        product.count_confidence = to_confidence(item.get("count_confidence"))
        attributes = item.get("attributes")
        if isinstance(attributes, list) and attributes:
            product.attributes = [str(a).strip() for a in attributes if str(a).strip()][:6]
        product.verified = True
        confirmed += 1

    log.info("second look: %s of %s uncertain groups confirmed", confirmed, len(candidates))
    return confirmed
