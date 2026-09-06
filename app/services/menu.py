from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.action_schemas import ApplyMenuActionPayload
from app.models import MenuItem, Recipe, User


class MenuCommandError(Exception):
    pass


@dataclass(frozen=True)
class MenuConflict:
    item_id: int
    plan_date: datetime
    meal_name: str
    recipe_id: int
    recipe_title: str


def _owned_recipes_for_payload(db: Session, actor: User, payload: ApplyMenuActionPayload) -> dict[int, Recipe]:
    recipe_ids = {entry.recipe_id for entry in payload.entries}
    recipes = db.scalars(select(Recipe).where(Recipe.id.in_(recipe_ids), Recipe.owner_id == actor.id)).all()
    by_id = {recipe.id: recipe for recipe in recipes}
    if len(by_id) != len(recipe_ids):
        raise MenuCommandError("One or more proposed recipes are unavailable")
    return by_id


def menu_conflicts(db: Session, actor: User, payload: ApplyMenuActionPayload) -> list[MenuConflict]:
    """Return existing entries occupying one of the proposal's date/meal slots."""
    slots = {(entry.plan_date, entry.meal_name.casefold()) for entry in payload.entries}
    dates = {entry.plan_date for entry in payload.entries}
    existing = db.scalars(
        select(MenuItem)
        .where(
            MenuItem.owner_id == actor.id,
            MenuItem.plan_date >= datetime.combine(min(dates), time.min),
            MenuItem.plan_date <= datetime.combine(max(dates), time.max),
        )
        .order_by(MenuItem.plan_date, MenuItem.meal_name, MenuItem.id)
    ).all()
    recipe_ids = {item.recipe_id for item in existing}
    recipe_titles = {
        recipe.id: recipe.title
        for recipe in db.scalars(select(Recipe).where(Recipe.id.in_(recipe_ids), Recipe.owner_id == actor.id)).all()
    }
    return [
        MenuConflict(
            item_id=item.id,
            plan_date=item.plan_date,
            meal_name=item.meal_name,
            recipe_id=item.recipe_id,
            recipe_title=recipe_titles.get(item.recipe_id, "Unavailable recipe"),
        )
        for item in existing
        if (item.plan_date.date(), item.meal_name.casefold()) in slots
    ]


def apply_menu_action(db: Session, actor: User, payload: ApplyMenuActionPayload) -> list[int]:
    """Create menu entries after confirmation; never overwrite or delete existing entries."""
    recipes = _owned_recipes_for_payload(db, actor, payload)
    conflicts = menu_conflicts(db, actor, payload)
    actual_conflict_ids = sorted(conflict.item_id for conflict in conflicts)
    if actual_conflict_ids != sorted(payload.conflict_item_ids):
        raise MenuCommandError("Menu changed after this proposal; create a new proposal")

    created: list[MenuItem] = []
    for entry in payload.entries:
        # display_title and rationale are immutable proposal/audit metadata; the handler still verifies the title.
        if entry.display_title != recipes[entry.recipe_id].title:
            raise MenuCommandError("Proposal recipe title no longer matches the saved recipe")
        item = MenuItem(
            owner_id=actor.id,
            recipe_id=entry.recipe_id,
            plan_date=datetime.combine(entry.plan_date, time.min),
            meal_name=entry.meal_name,
            note=entry.note,
        )
        db.add(item)
        created.append(item)
    db.flush()
    return [item.id for item in created]
