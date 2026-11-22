"""Photo handling: validate, fix rotation, shrink, and measure sharpness.

Every photo (uploaded or captured by the camera) goes through prepare_image() before it is
stored and sent to the vision model, so both input modes share one pipeline.
"""

import io
import logging
from dataclasses import dataclass, field

from PIL import Image, ImageFilter, ImageOps, ImageStat, UnidentifiedImageError

log = logging.getLogger("shelfsense.imaging")

SUPPORTED_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
MIN_SIDE = 400  # below this many pixels on the short side the photo is flagged as low-res
# Below this sharpness ratio (see sharpness_score) a photo is flagged as soft. Measured on real
# photos: sharp ones score about 4 to 13, copies blurred with radius 1.5 or more score 1.0 to 1.2.
BLUR_THRESHOLD = 1.8


class ImageError(ValueError):
    """The file is not an image we can audit. The message is safe to show to the user."""


@dataclass
class PreparedImage:
    data: bytes  # re-encoded JPEG
    mime: str
    width: int
    height: int
    original_width: int
    original_height: int
    sharpness: float
    warnings: list[str] = field(default_factory=list)


def _edge_variance(gray: Image.Image) -> float:
    return float(ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).var[0])


def sharpness_score(img: Image.Image) -> float:
    """How much fine detail the photo has, as a ratio (1.0 = none, higher = sharper).

    Compares edge strength before and after blurring the photo a little more. A sharp photo
    loses a lot of edge detail when blurred; a photo that is already blurry loses almost none.
    Computed on a fixed-size copy so it doesn't depend on the photo's resolution.
    """
    gray = ImageOps.grayscale(img)
    gray.thumbnail((640, 640))
    after = _edge_variance(gray.filter(ImageFilter.GaussianBlur(1.5)))
    if after < 1e-6:  # nothing but flat colour
        return 1.0
    return _edge_variance(gray) / after


def crop_region(data: bytes, box, pad: float = 0.06, min_long_side: int = 640) -> bytes:
    """Cut out one part of a photo (a box from the model) and enlarge it so details are visible.

    box has x, y, w, h as fractions of the photo. A little padding is added around it, and small
    crops are enlarged so the model gets more pixels to look at than it had in the whole photo.
    """
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        width, height = img.size
        left = max(0, int((box.x - pad) * width))
        top = max(0, int((box.y - pad) * height))
        right = min(width, int((box.x + box.w + pad) * width))
        bottom = min(height, int((box.y + box.h + pad) * height))
        if right - left < 8 or bottom - top < 8:  # degenerate box: fall back to the whole photo
            left, top, right, bottom = 0, 0, width, height
        crop = img.crop((left, top, right, bottom))
    long_side = max(crop.size)
    if long_side < min_long_side:
        factor = min(4.0, min_long_side / long_side)
        crop = crop.resize((int(crop.width * factor), int(crop.height * factor)), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    crop.save(out, "JPEG", quality=90)
    return out.getvalue()


def prepare_image(data: bytes, max_side: int = 1600) -> PreparedImage:
    """Validate and normalise a photo. Raises ImageError for anything unusable."""
    if not data:
        raise ImageError("The file is empty.")

    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ImageError(
            "This file could not be read as an image. Please use a JPEG, PNG or WEBP photo."
        ) from exc
    if fmt not in SUPPORTED_FORMATS:
        raise ImageError(f"{fmt or 'This'} images are not supported. Please use JPEG, PNG or WEBP.")

    try:
        img = Image.open(io.BytesIO(data))
        img.load()  # a truncated or corrupt file fails here, not later
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ImageError("The image file is damaged or incomplete. Please try another photo.") from exc

    original_width, original_height = img.size
    img = ImageOps.exif_transpose(img)  # phones store rotation in metadata

    if "A" in img.getbands():  # transparency: flatten onto white
        flat = Image.new("RGB", img.size, "white")
        flat.paste(img, mask=img.getchannel("A"))
        img = flat
    elif img.mode != "RGB":
        img = img.convert("RGB")

    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

    width, height = img.size
    sharpness = sharpness_score(img)

    warnings: list[str] = []
    if min(width, height) < MIN_SIDE:
        warnings.append(
            f"Low resolution ({width}x{height} px). Counts are more reliable on larger photos."
        )
    if sharpness < BLUR_THRESHOLD:
        warnings.append(
            "This photo looks soft or blurry, so counts and positions may be unreliable. "
            "A sharper, front-on photo will give better results."
        )

    out = io.BytesIO()
    img.save(out, "JPEG", quality=88, optimize=True)
    log.info(
        "prepared image %sx%s (from %sx%s, %s) -> %s bytes, sharpness=%.2f",
        width, height, original_width, original_height, fmt, out.tell(), sharpness,
    )
    return PreparedImage(
        data=out.getvalue(),
        mime="image/jpeg",
        width=width,
        height=height,
        original_width=original_width,
        original_height=original_height,
        sharpness=round(sharpness, 2),
        warnings=warnings,
    )
