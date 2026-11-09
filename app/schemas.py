from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DraftInput(Input):
    client_org_id: int = Field(gt=0, le=2147483647, strict=True)
    comment: str = Field(default="", max_length=1000)


class DraftOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    owner_id: int
    client_org_id: int
    comment: str
    status: str
    created_at: datetime


class ItemInput(Input):
    product_id: int = Field(gt=0, le=2147483647, strict=True)
    quantity: Decimal = Field(gt=0, max_digits=12, decimal_places=3, allow_inf_nan=False)


class VersionInput(DraftInput):
    items: list[ItemInput] = Field(min_length=1, max_length=100)
    discount: Decimal = Field(default=Decimal("0"), ge=0, le=100,
                              max_digits=5, decimal_places=2, allow_inf_nan=False)
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def unique_products(self):
        if len({item.product_id for item in self.items}) != len(self.items):
            raise ValueError("Each product must occur once")
        return self


class LeadInput(Input):
    action: Literal["approve", "return"]
    comment: str = Field(default="", max_length=1000)


class DecisionInput(Input):
    action: Literal["accept", "decline"]
