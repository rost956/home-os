from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import MenuItem, Recipe, RecipeCookingTimer, User

from ..errors import AIResponseValidationError

MAX_RECIPE_CANDIDATES = 8


class RecipeToolName(StrEnum):
    SEARCH = "search_recipes"
    DETAILS = "get_recipe_details"
    RECENT_MENU = "get_recent_menu_context"
    RECOMMEND = "recommend_recipes"


class RecipeToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: RecipeToolName
    arguments: dict[str, Any] = Field(default_factory=dict)


class RecipeSearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str | None = Field(default=None, min_length=1, max_length=100)
    ingredient: str | None = Field(default=None, min_length=1, max_length=100)
    exclude_ingredient: str | None = Field(default=None, min_length=1, max_length=100)
    include_ingredients: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list,
        max_length=5,
    )
    exclude_ingredients: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list,
        max_length=5,
    )
    max_cook_time: int | None = Field(default=None, ge=0, le=1_440)
    max_cost: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("1_000_000"))
    min_servings: Decimal | None = Field(default=None, gt=Decimal("0"), le=Decimal("1_000"))
    tags: list[Annotated[str, Field(min_length=1, max_length=50)]] = Field(default_factory=list, max_length=5)
    limit: int = Field(default=MAX_RECIPE_CANDIDATES, ge=1, le=MAX_RECIPE_CANDIDATES)


class RecipeRecommendationArguments(RecipeSearchArguments):
    sort_by: Literal["relevance", "cost", "time", "last_cooked"] = "relevance"


class RecipeDetailsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_id: int = Field(gt=0)


class RecentMenuArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: int = Field(default=28, ge=1, le=90)
    limit: int = Field(default=8, ge=1, le=10)


class RecipeCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_id: int
    title: str = Field(max_length=150)
    ingredients_preview: str = Field(max_length=600)
    cook_time_minutes: int | None
    cost: Decimal | None
    servings: Decimal | None
    tags: list[str] = Field(max_length=20)
    is_favorite: bool
    last_cooked_at: datetime | None


class RecipeSearchData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[RecipeCandidate] = Field(max_length=MAX_RECIPE_CANDIDATES)
    total_returned: int = Field(ge=0, le=MAX_RECIPE_CANDIDATES)
    cooking_history_available: bool


class RecipeDetailsData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    found: bool
    recipe: RecipeCandidate | None = None
    ingredients: str | None = Field(default=None, max_length=20_000)
    steps: str | None = Field(default=None, max_length=20_000)
    source_url: str | None = Field(default=None, max_length=500)


class RecentMenuEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_id: int
    title: str = Field(max_length=150)
    plan_date: date
    meal_name: str = Field(max_length=80)


class RecentMenuData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[RecentMenuEntry] = Field(max_length=10)
    history_kind: Literal["planned_menu"]


class RecipeSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal[RecipeToolName.SEARCH]
    data: RecipeSearchData


class RecipeDetailsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal[RecipeToolName.DETAILS]
    data: RecipeDetailsData


class RecentMenuResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal[RecipeToolName.RECENT_MENU]
    data: RecentMenuData


class RecipeRecommendationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal[RecipeToolName.RECOMMEND]
    data: RecipeSearchData


RecipeToolResult = Annotated[
    RecipeSearchResult | RecipeDetailsResult | RecentMenuResult | RecipeRecommendationResult,
    Field(discriminator="tool"),
]


RECIPE_ARGUMENT_SCHEMAS: dict[RecipeToolName, type[BaseModel]] = {
    RecipeToolName.SEARCH: RecipeSearchArguments,
    RecipeToolName.DETAILS: RecipeDetailsArguments,
    RecipeToolName.RECENT_MENU: RecentMenuArguments,
    RecipeToolName.RECOMMEND: RecipeRecommendationArguments,
}


def recipe_tool_descriptions() -> tuple[dict[str, str], ...]:
    return (
        {"name": RecipeToolName.SEARCH, "purpose": "Search saved recipes by title, ingredients, time, cost, servings, and tags."},
        {"name": RecipeToolName.DETAILS, "purpose": "Read details for one saved recipe by its existing ID."},
        {"name": RecipeToolName.RECENT_MENU, "purpose": "Read recent planned menu entries for the current user."},
        {"name": RecipeToolName.RECOMMEND, "purpose": "Return a small deterministic shortlist of saved recipes."},
    )


def _validated_arguments(call: RecipeToolCall) -> BaseModel:
    schema = RECIPE_ARGUMENT_SCHEMAS.get(call.tool)
    if schema is None:
        raise AIResponseValidationError("AI selected an unknown recipe tool")
    try:
        return schema.model_validate(call.arguments)
    except ValidationError as exc:
        raise AIResponseValidationError("AI returned invalid recipe tool arguments") from exc


def _recipe_tags(raw_tags: str | None) -> list[str]:
    return [tag.strip() for tag in (raw_tags or "").replace(";", ",").split(",") if tag.strip()][:20]


def _ingredients_preview(ingredients: str) -> str:
    normalized = " ".join(ingredients.split())
    return normalized[:600]


def _last_cooked_by_recipe(db: Session, user_id: int, recipe_ids: list[int]) -> tuple[dict[int, datetime], bool]:
    if not recipe_ids:
        return {}, False
    rows = db.execute(
        select(RecipeCookingTimer.recipe_id, func.max(RecipeCookingTimer.stopped_at))
        .where(
            RecipeCookingTimer.owner_id == user_id,
            RecipeCookingTimer.recipe_id.in_(recipe_ids),
            RecipeCookingTimer.stopped_at.is_not(None),
        )
        .group_by(RecipeCookingTimer.recipe_id)
    ).all()
    cooked = {recipe_id: stopped_at for recipe_id, stopped_at in rows if stopped_at is not None}
    return cooked, bool(cooked)


def _candidate(recipe: Recipe, last_cooked_at: datetime | None) -> RecipeCandidate:
    return RecipeCandidate(
        recipe_id=recipe.id,
        title=recipe.title,
        ingredients_preview=_ingredients_preview(recipe.ingredients),
        cook_time_minutes=recipe.cook_time_minutes,
        cost=recipe.cost,
        servings=recipe.servings,
        tags=_recipe_tags(recipe.tags),
        is_favorite=recipe.is_favorite,
        last_cooked_at=last_cooked_at,
    )


def _recipe_query(user: User, arguments: RecipeSearchArguments):
    statement = select(Recipe).where(Recipe.owner_id == user.id)
    if arguments.query:
        pattern = f"%{arguments.query}%"
        statement = statement.where(
            or_(Recipe.title.ilike(pattern), Recipe.ingredients.ilike(pattern), Recipe.tags.ilike(pattern))
        )
    if arguments.ingredient:
        pattern = f"%{arguments.ingredient}%"
        statement = statement.where(
            or_(Recipe.title.ilike(pattern), Recipe.ingredients.ilike(pattern), Recipe.tags.ilike(pattern))
        )
    for ingredient in arguments.include_ingredients:
        pattern = f"%{ingredient}%"
        statement = statement.where(
            or_(Recipe.title.ilike(pattern), Recipe.ingredients.ilike(pattern), Recipe.tags.ilike(pattern))
        )
    if arguments.exclude_ingredient:
        pattern = f"%{arguments.exclude_ingredient}%"
        statement = statement.where(~Recipe.ingredients.ilike(pattern))
    for ingredient in arguments.exclude_ingredients:
        pattern = f"%{ingredient}%"
        statement = statement.where(~Recipe.ingredients.ilike(pattern))
    if arguments.max_cook_time is not None:
        statement = statement.where(Recipe.cook_time_minutes.is_not(None), Recipe.cook_time_minutes <= arguments.max_cook_time)
    if arguments.max_cost is not None:
        statement = statement.where(Recipe.cost.is_not(None), Recipe.cost <= arguments.max_cost)
    if arguments.min_servings is not None:
        statement = statement.where(Recipe.servings.is_not(None), Recipe.servings >= arguments.min_servings)
    for tag in arguments.tags:
        statement = statement.where(Recipe.tags.ilike(f"%{tag}%"))
    return statement


def _search_data(db: Session, user: User, arguments: RecipeSearchArguments, *, sort_by: str = "relevance") -> RecipeSearchData:
    statement = _recipe_query(user, arguments)
    if sort_by == "cost":
        statement = statement.order_by(Recipe.cost.is_(None), Recipe.cost.asc(), Recipe.title.asc())
    elif sort_by == "time":
        statement = statement.order_by(Recipe.cook_time_minutes.is_(None), Recipe.cook_time_minutes.asc(), Recipe.title.asc())
    elif sort_by == "last_cooked":
        last_cooked = (
            select(func.max(RecipeCookingTimer.stopped_at))
            .where(RecipeCookingTimer.owner_id == user.id, RecipeCookingTimer.recipe_id == Recipe.id)
            .correlate(Recipe)
            .scalar_subquery()
        )
        statement = statement.order_by(last_cooked.is_(None), last_cooked.asc(), Recipe.title.asc())
    else:
        statement = statement.order_by(Recipe.is_favorite.desc(), Recipe.updated_at.desc(), Recipe.id.desc())
    recipes = list(db.scalars(statement.limit(arguments.limit)).all())
    cooked_by_recipe, has_cooking_history = _last_cooked_by_recipe(db, user.id, [recipe.id for recipe in recipes])
    candidates = [_candidate(recipe, cooked_by_recipe.get(recipe.id)) for recipe in recipes]
    return RecipeSearchData(
        candidates=candidates,
        total_returned=len(candidates),
        cooking_history_available=has_cooking_history,
    )


def execute_recipe_tool(db: Session, user: User, call: RecipeToolCall, *, today: date) -> RecipeToolResult:
    """Run one owner-scoped, read-only recipe tool for the authenticated user."""
    arguments = _validated_arguments(call)

    if call.tool == RecipeToolName.SEARCH:
        assert isinstance(arguments, RecipeSearchArguments)
        return RecipeSearchResult(tool=RecipeToolName.SEARCH, data=_search_data(db, user, arguments))

    if call.tool == RecipeToolName.RECOMMEND:
        assert isinstance(arguments, RecipeRecommendationArguments)
        return RecipeRecommendationResult(
            tool=RecipeToolName.RECOMMEND,
            data=_search_data(db, user, arguments, sort_by=arguments.sort_by),
        )

    if call.tool == RecipeToolName.DETAILS:
        assert isinstance(arguments, RecipeDetailsArguments)
        recipe = db.scalar(
            select(Recipe).where(Recipe.id == arguments.recipe_id, Recipe.owner_id == user.id)
        )
        if recipe is None:
            return RecipeDetailsResult(tool=RecipeToolName.DETAILS, data=RecipeDetailsData(found=False))
        cooked_by_recipe, _has_history = _last_cooked_by_recipe(db, user.id, [recipe.id])
        return RecipeDetailsResult(
            tool=RecipeToolName.DETAILS,
            data=RecipeDetailsData(
                found=True,
                recipe=_candidate(recipe, cooked_by_recipe.get(recipe.id)),
                ingredients=recipe.ingredients,
                steps=recipe.steps,
                source_url=recipe.source_url,
            ),
        )

    if call.tool == RecipeToolName.RECENT_MENU:
        assert isinstance(arguments, RecentMenuArguments)
        start = datetime.combine(today - timedelta(days=arguments.days - 1), time.min)
        end = datetime.combine(today, time.max)
        rows = db.execute(
            select(MenuItem, Recipe)
            .join(Recipe, Recipe.id == MenuItem.recipe_id)
            .where(
                MenuItem.owner_id == user.id,
                Recipe.owner_id == user.id,
                MenuItem.plan_date >= start,
                MenuItem.plan_date <= end,
            )
            .order_by(MenuItem.plan_date.desc(), MenuItem.id.desc())
            .limit(arguments.limit)
        ).all()
        return RecentMenuResult(
            tool=RecipeToolName.RECENT_MENU,
            data=RecentMenuData(
                entries=[
                    RecentMenuEntry(
                        recipe_id=recipe.id,
                        title=recipe.title,
                        plan_date=menu_item.plan_date.date(),
                        meal_name=menu_item.meal_name,
                    )
                    for menu_item, recipe in rows
                ],
                history_kind="planned_menu",
            ),
        )

    raise AIResponseValidationError("AI selected an unsupported recipe tool")
