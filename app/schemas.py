"""Pydantic models for the shelf photo, the planogram and the audit result.

These double as the validation layer: if JSON doesn't fit these shapes,
parsing fails (and, for model replies, we ask the model to try again).
"""

from pydantic import BaseModel, Field, field_validator


def _clean_name(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("name must not be blank")
    return value


# ---- What the vision model sees --------------------------------------------------


class DetectedProduct(BaseModel):
    name: str
    # Facings = how many units of this product sit side by side at the front.
    facings: int = Field(ge=0)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        return _clean_name(value)


class DetectedRow(BaseModel):
    row: int = Field(ge=1)  # 1 = top shelf row
    products: list[DetectedProduct] = Field(default_factory=list)
    empty_gaps: int = Field(default=0, ge=0)  # empty spots where a product should be
    misplaced_items: list[str] = Field(default_factory=list)


class DetectedShelf(BaseModel):
    rows: list[DetectedRow]
    overall_notes: str = ""


# ---- The planogram (what the shelf SHOULD look like) -----------------------------


class ExpectedProduct(BaseModel):
    product: str
    facings: int = Field(ge=1)
    # Other names the model might use for this product, e.g. "Coke 500ml".
    aliases: list[str] = Field(default_factory=list)

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
    rows: list[PlanogramRow] = Field(min_length=1)


# ---- The audit result ------------------------------------------------------------


class GapIssue(BaseModel):
    row: int
    product: str
    expected: int
    found: int
    missing: int


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


class AuditResult(BaseModel):
    score: float  # 0-100, weighted by expected facings
    rows: list[RowResult]
    gaps: list[GapIssue]
    misplaced: list[MisplacedIssue]
    unexpected: list[UnexpectedIssue]
    actions: list[str]
