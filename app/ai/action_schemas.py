from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .types import AIActionType


class ActionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreateExpenseActionPayload(ActionPayload):
    expense_list_id: int = Field(gt=0)
    category_id: int | None = Field(default=None, gt=0)
    title: str = Field(min_length=1, max_length=150)
    amount: Decimal = Field(gt=0, le=Decimal("99999999.99"), max_digits=10, decimal_places=2)
    expense_date: date
    date_was_defaulted: bool = False
    category_confident: bool = True
    include_in_analytics: bool = True
    include_in_forecast: bool = True
    merchant_key: str | None = Field(default=None, max_length=120)
    remember_category: bool = False


class MenuActionEntry(ActionPayload):
    plan_date: date
    meal_name: str = Field(min_length=1, max_length=80)
    recipe_id: int = Field(gt=0)
    note: str | None = Field(default=None, max_length=250)
    display_title: str | None = Field(default=None, min_length=1, max_length=150)
    rationale: str | None = Field(default=None, max_length=300)


class ApplyMenuActionPayload(ActionPayload):
    entries: list[MenuActionEntry] = Field(min_length=1, max_length=28)
    conflict_item_ids: list[int] = Field(default_factory=list, max_length=28)

    @model_validator(mode="after")
    def unique_slots_and_conflicts(self) -> "ApplyMenuActionPayload":
        slots = {(entry.plan_date, entry.meal_name.casefold()) for entry in self.entries}
        if len(slots) != len(self.entries):
            raise ValueError("menu entries must use unique date and meal slots")
        if any(item_id <= 0 for item_id in self.conflict_item_ids):
            raise ValueError("conflict item ids must be positive")
        if len(set(self.conflict_item_ids)) != len(self.conflict_item_ids):
            raise ValueError("conflict item ids must be unique")
        return self


class CreatePlannerActionPayload(ActionPayload):
    title: str = Field(min_length=1, max_length=180)
    scheduled_for: date
    start_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    description: str | None = Field(default=None, max_length=10_000)
    color: str = Field(default="#2563eb", pattern=r"^#[0-9a-fA-F]{6}$")

    @model_validator(mode="after")
    def end_must_follow_start(self) -> "CreatePlannerActionPayload":
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValueError("end_time must be later than start_time")
        return self


ACTION_PAYLOAD_SCHEMAS: dict[AIActionType, type[ActionPayload]] = {
    AIActionType.CREATE_EXPENSE: CreateExpenseActionPayload,
    AIActionType.APPLY_MENU: ApplyMenuActionPayload,
    AIActionType.CREATE_PLANNER_ITEM: CreatePlannerActionPayload,
}


def validate_action_payload(action_type: AIActionType | str, payload: dict[str, Any]) -> ActionPayload:
    normalized_type = AIActionType(action_type)
    return ACTION_PAYLOAD_SCHEMAS[normalized_type].model_validate(payload)
