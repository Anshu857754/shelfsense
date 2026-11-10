"""Pydantic models for what the vision model reports about a shelf.

These double as the validation layer: if the model's JSON doesn't fit these
shapes, parsing fails and we ask the model to try again.
"""

from pydantic import BaseModel, Field, field_validator


class DetectedProduct(BaseModel):
    name: str
    # Facings = how many units of this product sit side by side at the front.
    facings: int = Field(ge=0)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("product name must not be blank")
        return value


class DetectedRow(BaseModel):
    row: int = Field(ge=1)  # 1 = top shelf row
    products: list[DetectedProduct] = Field(default_factory=list)
    empty_gaps: int = Field(default=0, ge=0)  # empty spots where a product should be
    misplaced_items: list[str] = Field(default_factory=list)


class DetectedShelf(BaseModel):
    rows: list[DetectedRow]
    overall_notes: str = ""
