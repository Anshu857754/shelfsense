"""Pydantic models for the shelf photo, the planogram, corrections and the audit result.

These double as the validation layer: if JSON doesn't fit these shapes, parsing fails
(and, for model replies, we ask the model to try again).
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

# Audits scoring below this need attention (matches the "Needs attention" colour band).
ATTENTION_BELOW = 85.0

AuditStatus = Literal["processing", "completed", "needs_review", "failed"]


def _clean_name(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("name must not be blank")
    return value


def to_confidence(value: Any) -> float | None:
    """Model-reported confidence as 0..1, or None when it gave none or nonsense.

    Never invents a value: anything unusable becomes None ("not reported").
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    if 1 < number <= 100:  # models sometimes answer in percent
        number /= 100
    if number < 0 or number > 1:
        return None
    return round(number, 3)


# ---- What the vision model sees --------------------------------------------------


class BoundingBox(BaseModel):
    """A rectangle as fractions (0 to 1) of the image width/height, from the top-left."""

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)


class DetectedProduct(BaseModel):
    """One group of identical products in one shelf row."""

    name: str
    facings: int = Field(ge=0)  # quantity: units side by side at the front
    category: str = ""  # the model's own label for the product type, e.g. "jeans"
    # The planogram product the model says this is (exact planogram name), if any.
    matched_expected: str | None = None
    # How sure the model is of WHAT this is (model-reported, 0..1; None = not reported).
    # This is what decides whether an item is counted. It is deliberately separate from the
    # count: a pile of folded shirts is clearly shirts even when the number is a rough estimate.
    confidence: float | None = None
    # How sure the model is of the NUMBER (0..1; None = not reported). Low values only add a
    # "rough count" warning; they never remove the item from the audit.
    count_confidence: float | None = None
    verified: bool = False  # re-checked and confirmed by the zoomed-in second look
    position: str = ""  # e.g. "left", "center", "right"
    attributes: list[str] = Field(default_factory=list)  # colour, size, visible text...
    box: BoundingBox | None = None
    # Who produced this entry. "user" entries come from manual corrections.
    source: Literal["model", "user"] = "model"
    assigned_to: str | None = None  # manual correction: planogram product this really is

    @model_validator(mode="before")
    @classmethod
    def accept_quantity_alias(cls, data: Any) -> Any:
        """Models are asked for "quantity"; older replies use "facings"."""
        if isinstance(data, dict) and "facings" not in data:
            for key in ("quantity", "qty", "count"):
                if key in data:
                    return {**data, "facings": data[key]}
        return data

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("confidence", "count_confidence", mode="before")
    @classmethod
    def forgiving_confidence(cls, value: Any) -> float | None:
        return to_confidence(value)

    @field_validator("category", "position", mode="before")
    @classmethod
    def text_or_empty(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("matched_expected", "assigned_to", mode="before")
    @classmethod
    def blank_means_none(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("attributes", mode="before")
    @classmethod
    def attributes_as_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        items = [value] if isinstance(value, str) else list(value) if isinstance(value, (list, tuple)) else []
        return [str(i).strip() for i in items if str(i).strip()][:6]

    @field_validator("box", mode="before")
    @classmethod
    def forgiving_box(cls, value: Any) -> BoundingBox | None:
        """A missing or malformed box must never make the whole reply invalid."""
        if value is None:
            return None
        try:
            box = BoundingBox.model_validate(value)
        except ValidationError:
            return None
        # Models often overshoot the edge slightly: clamp instead of discarding.
        return BoundingBox(
            x=box.x, y=box.y, w=min(box.w, 1 - box.x) or 0.001, h=min(box.h, 1 - box.y) or 0.001
        )


class DetectedRow(BaseModel):
    row: int = Field(ge=1)  # 1 = top shelf row
    products: list[DetectedProduct] = Field(default_factory=list)
    empty_gaps: int = Field(default=0, ge=0)  # empty spots where a product should be
    misplaced_items: list[str] = Field(default_factory=list)


class DetectedShelf(BaseModel):
    rows: list[DetectedRow]
    overall_notes: str = ""
    # False when the model says the photo can't be audited (not a shelf, too dark, blurry...).
    shelf_visible: bool = True
    # True when the shelf is clearly visible and has nothing on it (not the same as "unusable photo").
    shelf_empty: bool = False
    image_issues: list[str] = Field(default_factory=list)

    @field_validator("image_issues", mode="before")
    @classmethod
    def issues_as_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        items = [value] if isinstance(value, str) else list(value) if isinstance(value, (list, tuple)) else []
        return [str(i).strip() for i in items if str(i).strip()][:6]


# ---- The planogram (what the shelf SHOULD look like) -----------------------------


class ExpectedProduct(BaseModel):
    product: str
    facings: int = Field(ge=1)
    # Other names the model might use for this product, e.g. "Coke 500ml".
    aliases: list[str] = Field(default_factory=list)
    category: str = ""  # optional product category, e.g. "denim"

    @field_validator("product")
    @classmethod
    def product_not_blank(cls, value: str) -> str:
        return _clean_name(value)


class PlanogramRow(BaseModel):
    row: int = Field(ge=1)
    expected: list[ExpectedProduct] = Field(min_length=1)


class Planogram(BaseModel):
    store: str = ""
    shelf: str = ""
    category: str = ""
    rows: list[PlanogramRow] = Field(min_length=1)


# ---- Manual corrections ----------------------------------------------------------


class CorrectionItem(BaseModel):
    """One row of the correction table the user edits on the result page."""

    row: int = Field(ge=1, le=99)
    name: str
    quantity: int = Field(ge=0, le=999)
    # keep = leave as is, drop = remove from the count, accept = count an uncertain detection
    state: Literal["keep", "drop", "accept"] = "keep"
    edited: bool = False
    assigned_to: str | None = None
    category: str = ""
    confidence: float | None = None
    count_confidence: float | None = None
    verified: bool = False
    position: str = ""
    attributes: list[str] = Field(default_factory=list)
    matched_expected: str | None = None
    box: BoundingBox | None = None
    source: Literal["model", "user"] = "model"

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        return _clean_name(value)


class CorrectionRequest(BaseModel):
    items: list[CorrectionItem] = Field(max_length=400)
    # The user confirms the shelf really is empty, so a 0% score is a real result.
    confirmed_empty: bool = False


# ---- The audit result ------------------------------------------------------------


class GapIssue(BaseModel):
    row: int
    product: str
    expected: int
    found: int
    missing: int


class OverstockIssue(BaseModel):
    row: int
    product: str
    expected: int
    found: int
    extra: int


class MisplacedIssue(BaseModel):
    product: str
    found_in_row: int
    belongs_in_row: int
    facings: int


class UnexpectedIssue(BaseModel):
    name: str
    row: int
    facings: int


class RowResult(BaseModel):
    row: int
    expected_facings: int
    correct_facings: int
    score: float  # 0-100


class Task(BaseModel):
    """One thing a store worker should do, most urgent first."""

    kind: Literal["restock", "move", "remove", "correct", "check"]
    priority: Literal["high", "medium", "low"]
    text: str


class ComparisonItem(BaseModel):
    product: str
    expected: int
    found: int  # facings found in the right row


class ExtraItem(BaseModel):
    """Something on the shelf row that the planogram does not put there."""

    name: str
    facings: int
    kind: Literal["misplaced", "unexpected"]
    belongs_in_row: int | None = None


class RowComparison(BaseModel):
    row: int
    score: float | None = None  # None for rows the planogram doesn't have
    items: list[ComparisonItem]
    extras: list[ExtraItem]


class Detection(BaseModel):
    """One product group the model saw, with how it compares to the planogram."""

    row: int
    name: str
    facings: int
    status: Literal["ok", "short", "misplaced", "unexpected", "uncertain"]
    category: str = ""
    matched_product: str | None = None  # the planogram product it was matched to
    confidence: float | None = None
    count_confidence: float | None = None
    verified: bool = False
    position: str = ""
    attributes: list[str] = Field(default_factory=list)
    source: Literal["model", "user"] = "model"
    box: BoundingBox | None = None


class AuditResult(BaseModel):
    # None when the audit is incomplete: no honest score can be given.
    score: float | None = None  # 0-100, weighted by expected facings
    complete: bool = True
    incomplete_reason: str | None = None
    # Why the audit is incomplete: no_products, unusable_photo, no_confident, no_match, uncertain_share
    incomplete_code: str | None = None
    # A plain-language answer, written by code from the numbers (not by the AI).
    headline: str = ""
    summary: list[str] = Field(default_factory=list)
    trust_level: Literal["high", "medium", "low"] | None = None
    trust_reason: str = ""
    # For an incomplete audit that still saw something: the score would be somewhere in this
    # range, depending on whether the uncertain items turn out to be right.
    estimate_low: float | None = None
    estimate_high: float | None = None
    approx_count_groups: int = 0  # counted groups whose number is only a rough estimate
    # When the AI looked at the photo more than once: the score from each look (sorted).
    pass_scores: list[float] = Field(default_factory=list)
    rows: list[RowResult] = Field(default_factory=list)
    gaps: list[GapIssue] = Field(default_factory=list)
    overstock: list[OverstockIssue] = Field(default_factory=list)
    misplaced: list[MisplacedIssue] = Field(default_factory=list)
    unexpected: list[UnexpectedIssue] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)  # task texts, most urgent first
    tasks: list[Task] = Field(default_factory=list)
    comparison: list[RowComparison] = Field(default_factory=list)
    detections: list[Detection] = Field(default_factory=list)
    # Unit counts (confident detections only; uncertain ones are counted separately).
    detected_units: int = 0
    missing_units: int = 0
    misplaced_units: int = 0
    unexpected_units: int = 0
    uncertain_units: int = 0
    # Average of the confidences the model reported for counted items (None if none reported).
    confidence: float | None = None
    warnings: list[str] = Field(default_factory=list)
