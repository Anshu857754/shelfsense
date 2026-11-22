"""Template setup shared by the page routes."""

from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.categories import CATEGORIES
from app.schemas import ATTENTION_BELOW

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

STATUS_LABELS = {
    "processing": "Processing",
    "completed": "Completed",
    "needs_review": "Needs review",
    "failed": "Failed",
}


def score_level(score: float | None) -> str:
    """Colour band for a compliance score."""
    if score is None:
        return "none"
    return "good" if score >= ATTENTION_BELOW else ("ok" if score >= 60 else "bad")


def fmt_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%d %b %Y, %H:%M UTC")
    except ValueError:
        return value


def fmt_day(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%d %b")
    except ValueError:
        return value


def pct(value: float | None, digits: int = 0) -> str:
    """0..1 as a percentage, or 'n/a' when there is no value."""
    return "n/a" if value is None else f"{value * 100:.{digits}f}%"


def asset_version(name: str) -> str:
    """A number that changes whenever a static file changes.

    Added to script and style links (?v=...) so a browser never keeps running an old copy of
    the page's JavaScript after an update. That is what let a stale script skip the
    category-to-sample swap and caused a planogram/category mismatch.
    """
    try:
        return str(int((BASE_DIR / "static" / name).stat().st_mtime))
    except OSError:
        return "0"


templates.env.globals["asset_version"] = asset_version
# Keep JSON keys in their natural order. By default the engine sorts them alphabetically, which
# turned the sample planograms into "rows, shelf, store" and broke comparing them with the text box.
templates.env.policies["json.dumps_kwargs"] = {"sort_keys": False}
templates.env.filters["fmt_time"] = fmt_time
templates.env.filters["fmt_day"] = fmt_day
templates.env.filters["pct"] = pct
templates.env.filters["status_label"] = lambda s: STATUS_LABELS.get(s, s)
templates.env.globals["score_level"] = score_level
templates.env.globals["categories"] = list(CATEGORIES.values())
templates.env.globals["attention_below"] = ATTENTION_BELOW
