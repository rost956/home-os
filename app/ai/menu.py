from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.action_schemas import ApplyMenuActionPayload, MenuActionEntry
from app.ai.actions import create_pending_action
from app.ai.recipes import analyze_recipe_text, recipe_search_arguments_from_text
from app.ai.russian_dates import parse_bounded_cardinal, parse_future_russian_day_expression
from app.ai.tools.recipes import (
    RecipeCandidate,
    RecipeRecommendationArguments,
    RecipeToolCall,
    RecipeToolName,
    execute_recipe_tool,
)
from app.models import MenuItem, Recipe, User
from app.services.menu import MenuConflict, menu_conflicts
from app.timezone import today_msk

from .client import AIClient
from .errors import AIResponseValidationError
from .schemas import AICompletionRequest
from .types import AIActionType

MAX_MENU_DAYS = 14
MAX_MENU_CANDIDATES = 8


class MenuProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=3, max_length=500)


class MenuProposalEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    plan_date: date
    meal_name: str = Field(default="Ужин", min_length=1, max_length=80)
    recipe_id: int = Field(gt=0)
    note: str | None = Field(default=None, max_length=250)
    rationale: str | None = Field(default=None, max_length=300)


class MenuProposalSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[MenuProposalEntry] = Field(min_length=1, max_length=MAX_MENU_DAYS)


class MenuConflictData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: int
    plan_date: date
    meal_name: str = Field(max_length=80)
    recipe_id: int
    recipe_title: str = Field(max_length=150)


class MenuProposalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str
    starts_on: date
    ends_on: date
    entries: list[MenuActionEntry] = Field(max_length=MAX_MENU_DAYS)
    conflicts: list[MenuConflictData] = Field(max_length=MAX_MENU_DAYS)
    candidate_count: int = Field(ge=0, le=MAX_MENU_CANDIDATES)


class MenuProposalError(Exception):
    pass


@dataclass(frozen=True)
class MenuPlanningWindow:
    starts_on: date
    ends_on: date
    requested_dates: tuple[date, ...] | None = None

    @property
    def dates(self) -> tuple[date, ...]:
        if self.requested_dates is not None:
            return self.requested_dates
        return tuple(self.starts_on + timedelta(days=offset) for offset in range((self.ends_on - self.starts_on).days + 1))


def _next_weekday(after: date, weekday: int) -> date:
    offset = (weekday - after.weekday()) % 7
    return after + timedelta(days=offset or 7)


def menu_planning_window(question: str, *, today: date) -> MenuPlanningWindow:
    normalized = question.casefold().replace("ё", "е")
    starts_on = today + timedelta(days=1)
    duration_match = re.search(
        r"\b(?P<count>\d{1,2}|один|одна|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|"
        r"одиннадцать|двенадцать|тринадцать|четырнадцать)\s+дн(?:я|ей)?\b",
        normalized,
    )
    if duration_match:
        duration = parse_bounded_cardinal(duration_match.group("count"), maximum=MAX_MENU_DAYS)
        if duration is None:
            raise MenuProposalError(f"Menu period must contain between 1 and {MAX_MENU_DAYS} days")
        return MenuPlanningWindow(starts_on=starts_on, ends_on=starts_on + timedelta(days=duration - 1))
    if re.search(r"\bследующ\w*\s+недел", normalized):
        starts_on = _next_weekday(today, 0)
        return MenuPlanningWindow(starts_on=starts_on, ends_on=starts_on + timedelta(days=6))
    if re.search(r"\b(?:на\s+)?выходн", normalized):
        saturday = _next_weekday(today, 5)
        dates = (saturday, saturday + timedelta(days=1))
        return MenuPlanningWindow(starts_on=dates[0], ends_on=dates[-1], requested_dates=dates)
    if re.search(r"\bдо\s+пятниц", normalized):
        ends_on = _next_weekday(today, 4)
        return MenuPlanningWindow(starts_on=starts_on, ends_on=ends_on)
    try:
        explicit_date = parse_future_russian_day_expression(question, today=today)
    except ValueError as exc:
        raise MenuProposalError("Menu date is invalid") from exc
    if explicit_date is not None:
        if explicit_date.value <= today:
            raise MenuProposalError("Menu date must be in the future")
        return MenuPlanningWindow(starts_on=explicit_date.value, ends_on=explicit_date.value)
    weekday_patterns = (
        (0, r"\bпонедельник\w*\b"),
        (1, r"\bвторник\w*\b"),
        (2, r"\bсред(?:а|у|ы|е)\b"),
        (3, r"\bчетверг\w*\b"),
        (4, r"\bпятниц\w*\b"),
        (5, r"\bсуббот\w*\b"),
        (6, r"\bвоскресень\w*\b"),
    )
    weekdays = [weekday for weekday, pattern in weekday_patterns if re.search(pattern, normalized)]
    if weekdays:
        if len(weekdays) > 1:
            week_starts_on = _next_weekday(today, 0)
            dates = tuple(week_starts_on + timedelta(days=weekday) for weekday in sorted(set(weekdays)))
        else:
            dates = (_next_weekday(today, weekdays[0]),)
        return MenuPlanningWindow(starts_on=dates[0], ends_on=dates[-1], requested_dates=dates)
    if re.search(r"\bнедел(?:я|ю|и)\b", normalized):
        return MenuPlanningWindow(starts_on=starts_on, ends_on=starts_on + timedelta(days=6))
    if "завтра" in normalized or "tomorrow" in normalized:
        return MenuPlanningWindow(starts_on=starts_on, ends_on=starts_on)
    return MenuPlanningWindow(starts_on=starts_on, ends_on=starts_on)


def _recent_exclusion_days(question: str) -> int:
    normalized = question.casefold().replace("ё", "е")
    match = re.search(r"последн\w*\s+(\d{1,2})\s+(?:дн|недел)", normalized)
    if not re.search(r"без\s+повтор|не\s+повтор", normalized):
        return 0
    if match:
        count = int(match.group(1))
        return count * 7 if "недел" in match.group(0) else count
    if re.search(r"последн\w*\s+двух\s+недел", normalized):
        return 14
    if re.search(r"последн\w*\s+двух\s+дн", normalized):
        return 2
    if re.search(r"прошл\w*\s+недел", normalized):
        return 7
    return 0


def _recent_recipe_ids(db: Session, user: User, *, before: date, days: int) -> set[int]:
    if days <= 0:
        return set()
    start = datetime.combine(before - timedelta(days=days), time.min)
    end = datetime.combine(before, time.min)
    return set(
        db.scalars(
            select(MenuItem.recipe_id)
            .join(Recipe, Recipe.id == MenuItem.recipe_id)
            .where(
                MenuItem.owner_id == user.id,
                Recipe.owner_id == user.id,
                MenuItem.plan_date >= start,
                MenuItem.plan_date < end,
            )
        ).all()
    )


def _candidate_arguments(question: str) -> RecipeRecommendationArguments:
    arguments = recipe_search_arguments_from_text(question)
    arguments["limit"] = MAX_MENU_CANDIDATES
    normalized = question.casefold().replace("ё", "е")
    if "недорог" in normalized or "дешев" in normalized:
        arguments["sort_by"] = "cost"
    elif "быстр" in normalized or "час" in normalized or "мин" in normalized:
        arguments["sort_by"] = "time"
    return RecipeRecommendationArguments.model_validate(arguments)


def _candidate_shortlist(db: Session, user: User, question: str, window: MenuPlanningWindow) -> list[RecipeCandidate]:
    arguments = _candidate_arguments(question)
    result = execute_recipe_tool(
        db,
        user,
        RecipeToolCall(tool=RecipeToolName.RECOMMEND, arguments=arguments.model_dump(mode="json")),
        today=window.starts_on,
    )
    candidates = list(result.data.candidates)
    excluded = _recent_recipe_ids(
        db,
        user,
        before=window.starts_on,
        days=_recent_exclusion_days(question),
    )
    return [candidate for candidate in candidates if candidate.recipe_id not in excluded][:MAX_MENU_CANDIDATES]


def _selection_request(question: str, window: MenuPlanningWindow, candidates: list[RecipeCandidate]) -> AICompletionRequest:
    candidate_payload = [
        {
            "recipe_id": candidate.recipe_id,
            "title": candidate.title,
            "cook_time_minutes": candidate.cook_time_minutes,
            "cost": str(candidate.cost) if candidate.cost is not None else None,
            "servings": str(candidate.servings) if candidate.servings is not None else None,
            "tags": candidate.tags,
        }
        for candidate in candidates
    ]
    return AICompletionRequest(
        messages=[
            {
                "role": "system",
                "content": (
                    "Create a menu proposal only. Do not create, edit, delete, or apply menu items. "
                    "Use only recipe_id values from the supplied shortlist. Return one entry for each selected date, "
                    "with plan_date, meal_name, recipe_id, optional note, and optional rationale. "
                    "preprocessing.fixed_candidate_filters are already enforced server-side; use unresolved_text only "
                    "to choose among the shortlist and never reverse an exclusion into a preference. "
                    "Do not invent recipes, IDs, prices, history, or unavailable dates. "
                    'Return strict JSON: {"entries":[{"plan_date":"YYYY-MM-DD","meal_name":"Dinner","recipe_id":1,"note":null,"rationale":null}]}.'
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": question,
                        "preprocessing": {
                            "fixed_candidate_filters": analyze_recipe_text(question).arguments,
                            "unresolved_text": analyze_recipe_text(question).unresolved_text,
                        },
                        "allowed_dates": [item.isoformat() for item in window.dates],
                        "shortlist": candidate_payload,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ),
            },
        ],
        max_tokens=600,
        temperature=0.1,
        output_mode="json",
        enable_thinking=False,
    )


def _validated_entries(selection: MenuProposalSelection, candidates: list[RecipeCandidate], window: MenuPlanningWindow) -> list[MenuActionEntry]:
    candidate_titles = {candidate.recipe_id: candidate.title for candidate in candidates}
    allowed_dates = set(window.dates)
    slots: set[tuple[date, str]] = set()
    entries: list[MenuActionEntry] = []
    for entry in selection.entries:
        if entry.recipe_id not in candidate_titles:
            raise AIResponseValidationError("AI selected a recipe outside the shortlist")
        if entry.plan_date not in allowed_dates:
            raise AIResponseValidationError("AI selected a date outside the requested period")
        slot = (entry.plan_date, entry.meal_name.casefold())
        if slot in slots:
            raise AIResponseValidationError("AI selected duplicate menu slots")
        slots.add(slot)
        entries.append(
            MenuActionEntry(
                plan_date=entry.plan_date,
                meal_name=entry.meal_name,
                recipe_id=entry.recipe_id,
                note=entry.note,
                display_title=candidate_titles[entry.recipe_id],
                rationale=entry.rationale,
            )
        )
    if {entry.plan_date for entry in entries} != allowed_dates:
        raise AIResponseValidationError("AI did not propose a menu entry for every requested day")
    return entries


def _conflict_data(conflicts: list[MenuConflict]) -> list[MenuConflictData]:
    return [
        MenuConflictData(
            item_id=conflict.item_id,
            plan_date=conflict.plan_date.date(),
            meal_name=conflict.meal_name,
            recipe_id=conflict.recipe_id,
            recipe_title=conflict.recipe_title,
        )
        for conflict in conflicts
    ]


async def create_menu_proposal(
    db: Session,
    user: User,
    client: AIClient,
    question: str,
    *,
    today: date | None = None,
) -> MenuProposalResponse:
    """Create a pending, owner-scoped menu action; this function never writes MenuItem rows."""
    window = menu_planning_window(question, today=today or today_msk())
    candidates = _candidate_shortlist(db, user, question, window)
    if not candidates:
        raise MenuProposalError("No saved recipes match the requested menu constraints")
    selection = await client.complete_json(_selection_request(question, window, candidates), MenuProposalSelection)
    entries = _validated_entries(selection, candidates, window)
    payload = ApplyMenuActionPayload(entries=entries)
    conflicts = menu_conflicts(db, user, payload)
    payload = payload.model_copy(update={"conflict_item_ids": [conflict.item_id for conflict in conflicts]})
    preview = f"Menu proposal: {len(entries)} item(s), {window.starts_on.isoformat()} to {window.ends_on.isoformat()}."
    if conflicts:
        preview += " Existing menu entries in the same slots will be kept; confirmation adds the proposed entries alongside them."
    action = create_pending_action(
        db,
        owner_id=user.id,
        action_type=AIActionType.APPLY_MENU,
        proposed_payload=payload,
        preview_text=preview,
    )
    return MenuProposalResponse(
        action_id=action.public_id,
        starts_on=window.starts_on,
        ends_on=window.ends_on,
        entries=payload.entries,
        conflicts=_conflict_data(conflicts),
        candidate_count=len(candidates),
    )
