"""Test helpers: photo-like images, fake model replies, and a scripted stand-in for the AI.

The fake model only replaces the network call. Everything after it (prompt building, JSON
parsing, validation, matching, scoring, storage) is the real code.
"""

import io
import json
import random

from PIL import Image, ImageDraw, ImageFilter


def shelf_image(size=(900, 600), blur: float = 0, fmt: str = "JPEG", seed: int = 7, orientation=None) -> bytes:
    """A synthetic 'shelf' picture with plenty of sharp edges (so it passes the blur check)."""
    rng = random.Random(seed)
    img = Image.new("RGB", size, (240, 238, 245))
    draw = ImageDraw.Draw(img)
    for _ in range(70):
        x0, y0 = rng.randint(0, size[0] - 60), rng.randint(0, size[1] - 60)
        w, h = rng.randint(20, 80), rng.randint(30, 120)
        colour = tuple(rng.randint(30, 230) for _ in range(3))
        draw.rectangle([x0, y0, x0 + w, y0 + h], fill=colour, outline=(0, 0, 0))
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    buf = io.BytesIO()
    if fmt == "JPEG":
        kwargs = {"quality": 92}
        if orientation:
            exif = img.getexif()
            exif[0x0112] = orientation
            kwargs["exif"] = exif
        img.save(buf, "JPEG", **kwargs)
    else:
        img.save(buf, fmt)
    return buf.getvalue()


def item(name, qty, matched=None, conf=0.9, category="", box=None, position="", attributes=None):
    """One product group as the model would report it."""
    entry = {"name": name, "quantity": qty, "matched_expected": matched, "confidence": conf}
    if category:
        entry["category"] = category
    if box:
        entry["box"] = box
    if position:
        entry["position"] = position
    if attributes:
        entry["attributes"] = attributes
    if conf is None:
        entry.pop("confidence")
    return entry


def reply(*rows, notes="", visible=True, issues=None) -> str:
    """A model reply. rows: (row_number, [items])."""
    return json.dumps(
        {
            "shelf_visible": visible,
            "image_issues": issues or [],
            "rows": [{"row": r, "products": products} for r, products in rows],
            "overall_notes": notes,
        }
    )


class FakeModel:
    """Stands in for the OpenRouter call. Queue replies (strings) or exceptions to raise."""

    def __init__(self):
        self.replies: list = []
        self.calls: list[list[dict]] = []

    def queue(self, *replies):
        self.replies.extend(replies)
        return self

    async def __call__(self, messages, settings):
        self.calls.append(list(messages))
        if not self.replies:
            raise AssertionError("The fake model was called more times than it has replies for.")
        next_reply = self.replies.pop(0)
        if isinstance(next_reply, Exception):
            raise next_reply
        return next_reply

    @property
    def first_prompt(self) -> str:
        """The text part of the first message sent to the model."""
        return self.calls[0][0]["content"][0]["text"]
