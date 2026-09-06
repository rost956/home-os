from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.ai.client import FakeAIClient
from app.ai.dependencies import get_ai_client
from app.ai.errors import AITimeoutError, AIUnavailableError
from app.ai.permissions import get_or_create_ai_user_settings
from app.ai.recipes import select_deterministic_recipe_tool
from app.ai.schemas import AICompletionResponse
from app.ai.tools.recipes import RecipeToolCall, RecipeToolName, execute_recipe_tool
from app.main import app
from app.models import AIAction, MenuItem, Recipe, RecipeCookingTimer

TODAY = date(2026, 9, 6)


def enable_recipes(db, user) -> None:
    settings = get_or_create_ai_user_settings(db, user.id)
    settings.enabled = True
    settings.allow_recipes = True
    db.commit()


def add_recipe(
    db,
    user,
    title: str,
    ingredients: str,
    *,
    cook_time: int | None = 30,
    cost: str | None = "300",
    servings: str | None = "2",
    tags: str | None = None,
    favorite: bool = False,
) -> Recipe:
    recipe = Recipe(
        owner_id=user.id,
        title=title,
        ingredients=ingredients,
        cook_time_minutes=cook_time,
        cost=Decimal(cost) if cost is not None else None,
        servings=Decimal(servings) if servings is not None else None,
        tags=tags,
        is_favorite=favorite,
        steps="Cook it",
    )
    db.add(recipe)
    db.flush()
    return recipe


def ask_with_fake(client, fake: FakeAIClient, question: str):
    app.dependency_overrides[get_ai_client] = lambda: fake
    try:
        return client.post("/api/ai/recipes/questions", json={"question": question})
    finally:
        app.dependency_overrides.pop(get_ai_client, None)


def answer(text: str = "Ready", recipe_ids: list[int] | None = None) -> AICompletionResponse:
    return AICompletionResponse(content=json.dumps({"answer": text, "recipe_ids": recipe_ids or []}))


def recipe_titles(result) -> list[str]:
    return [item.title for item in result.data.candidates]


@pytest.mark.parametrize(
    ("question", "expected_tool", "expected_arguments"),
    [
        ("Что приготовить завтра?", RecipeToolName.RECOMMEND, {"limit": 5}),
        ("Хочу что-нибудь с курицей", RecipeToolName.SEARCH, {"ingredient": "куриц"}),
        ("Что есть максимум на 40 минут?", RecipeToolName.SEARCH, {"max_cook_time": 40}),
        ("Что-нибудь недорогое", RecipeToolName.RECOMMEND, {"sort_by": "cost", "limit": 5}),
        ("Что мы давно не готовили?", RecipeToolName.RECOMMEND, {"sort_by": "last_cooked", "limit": 5}),
        ("Подбери рецепт на ужин", RecipeToolName.RECOMMEND, {"limit": 5}),
        ("Найди рецепт без рыбы", RecipeToolName.SEARCH, {"exclude_ingredient": "рыб"}),
        ("Что есть на 4 порции?", RecipeToolName.SEARCH, {"min_servings": Decimal("4")}),
    ],
)
def test_common_recipe_questions_use_deterministic_read_tools(question, expected_tool, expected_arguments):
    call = select_deterministic_recipe_tool(question)

    assert call is not None
    assert call.tool == expected_tool
    assert call.arguments == expected_arguments


def test_recipe_search_filters_are_deterministic_and_owner_scoped(db, make_user):
    alice = make_user("recipe-filter-alice")
    bob = make_user("recipe-filter-bob")
    add_recipe(db, alice, "Chicken pasta", "chicken, pasta", cook_time=35, cost="380", servings="4", tags="dinner, quick")
    add_recipe(db, alice, "Fish soup", "fish, potato", cook_time=50, cost="250", servings="3", tags="soup")
    add_recipe(db, alice, "Slow stew", "beef", cook_time=120, cost="900", servings="6", tags="dinner")
    add_recipe(db, bob, "Bob secret chicken", "chicken", cook_time=10, cost="10", servings="10", tags="quick")
    db.commit()

    by_title = execute_recipe_tool(db, alice, RecipeToolCall(tool=RecipeToolName.SEARCH, arguments={"query": "pasta"}), today=TODAY)
    assert recipe_titles(by_title) == ["Chicken pasta"]

    by_ingredient = execute_recipe_tool(db, alice, RecipeToolCall(tool=RecipeToolName.SEARCH, arguments={"ingredient": "chicken"}), today=TODAY)
    assert recipe_titles(by_ingredient) == ["Chicken pasta"]

    fast = execute_recipe_tool(db, alice, RecipeToolCall(tool=RecipeToolName.SEARCH, arguments={"max_cook_time": 40}), today=TODAY)
    assert recipe_titles(fast) == ["Chicken pasta"]

    affordable = execute_recipe_tool(db, alice, RecipeToolCall(tool=RecipeToolName.SEARCH, arguments={"max_cost": "300"}), today=TODAY)
    assert recipe_titles(affordable) == ["Fish soup"]

    portions = execute_recipe_tool(db, alice, RecipeToolCall(tool=RecipeToolName.SEARCH, arguments={"min_servings": "4"}), today=TODAY)
    assert set(recipe_titles(portions)) == {"Chicken pasta", "Slow stew"}

    tagged = execute_recipe_tool(db, alice, RecipeToolCall(tool=RecipeToolName.SEARCH, arguments={"tags": ["quick"]}), today=TODAY)
    assert recipe_titles(tagged) == ["Chicken pasta"]

    no_fish = execute_recipe_tool(db, alice, RecipeToolCall(tool=RecipeToolName.SEARCH, arguments={"exclude_ingredient": "fish"}), today=TODAY)
    assert "Fish soup" not in recipe_titles(no_fish)
    assert "Bob secret chicken" not in recipe_titles(no_fish)


def test_recipe_empty_result_details_and_recent_menu_are_read_only(db, make_user):
    user = make_user("recipe-empty")
    recipe = add_recipe(db, user, "Pasta", "tomato, pasta")
    db.add(MenuItem(owner_id=user.id, recipe_id=recipe.id, plan_date=datetime(2026, 9, 4), meal_name="Dinner"))
    db.commit()
    initial_recipes = db.query(Recipe).count()
    initial_actions = db.query(AIAction).count()

    empty = execute_recipe_tool(db, user, RecipeToolCall(tool=RecipeToolName.SEARCH, arguments={"ingredient": "mango"}), today=TODAY)
    assert empty.data.total_returned == 0
    assert empty.data.candidates == []

    missing = execute_recipe_tool(db, user, RecipeToolCall(tool=RecipeToolName.DETAILS, arguments={"recipe_id": 99999}), today=TODAY)
    assert missing.data.found is False
    assert missing.data.recipe is None

    menu = execute_recipe_tool(db, user, RecipeToolCall(tool=RecipeToolName.RECENT_MENU, arguments={}), today=TODAY)
    assert [(entry.title, entry.plan_date.isoformat()) for entry in menu.data.entries] == [("Pasta", "2026-09-04")]
    assert menu.data.history_kind == "planned_menu"
    assert db.query(Recipe).count() == initial_recipes
    assert db.query(AIAction).count() == initial_actions


def test_recipe_shortlist_is_limited_and_only_real_ids_reach_model(client, db, make_user, login):
    user = make_user("recipe-shortlist")
    recipes = [add_recipe(db, user, f"Recipe {index}", "vegetables") for index in range(12)]
    db.commit()
    enable_recipes(db, user)
    login(user.username)
    fake = FakeAIClient(responses=[answer("Choose one of the returned recipes.")])

    response = ask_with_fake(client, fake, "What should I cook tomorrow?")

    assert response.status_code == 200
    payload = response.json()
    candidates = payload["result"]["data"]["candidates"]
    assert len(candidates) == 5
    assert {candidate["recipe_id"] for candidate in candidates}.issubset({recipe.id for recipe in recipes})
    assert len(fake.requests) == 1
    explanation_context = fake.requests[0].messages[1].content
    assert "Recipe 0" not in explanation_context
    assert "Recipe 11" in explanation_context


def test_model_cannot_reveal_nonexistent_or_other_owner_recipe(client, db, make_user, login):
    alice = make_user("recipe-owner-alice")
    bob = make_user("recipe-owner-bob")
    secret = add_recipe(db, bob, "Bob secret", "secret ingredient")
    db.commit()
    enable_recipes(db, alice)
    login(alice.username)
    fake = FakeAIClient(
        responses=[
            AICompletionResponse(content=json.dumps({"tool": "get_recipe_details", "arguments": {"recipe_id": secret.id}})),
            answer("There is no accessible saved recipe with that ID."),
        ]
    )

    response = ask_with_fake(client, fake, "Tell me something unusual")

    assert response.status_code == 200
    assert response.json()["result"]["data"] == {"found": False, "recipe": None, "ingredients": None, "steps": None, "source_url": None}
    assert "Bob secret" not in fake.requests[1].messages[1].content


def test_model_cannot_recommend_a_recipe_id_outside_the_tool_result(client, db, make_user, login):
    user = make_user("recipe-invented-id")
    add_recipe(db, user, "Saved recipe", "beans")
    db.commit()
    enable_recipes(db, user)
    login(user.username)

    response = ask_with_fake(client, FakeAIClient(responses=[answer("Try this one.", [99999])]), "What should I cook tomorrow?")

    assert response.status_code == 502


@pytest.mark.parametrize(
    "selector_response",
    [
        "not-json",
        '{"tool":"delete_recipe","arguments":{}}',
        '{"tool":"recipe.create","arguments":{}}',
        '{"tool":"search_recipes","arguments":{"user_id":999}}',
    ],
)
def test_invalid_unknown_and_write_recipe_tools_are_rejected(client, db, make_user, login, selector_response):
    user = make_user(f"recipe-invalid-{abs(hash(selector_response))}")
    add_recipe(db, user, "Existing", "beans")
    db.commit()
    enable_recipes(db, user)
    initial_recipes = db.query(Recipe).count()
    initial_actions = db.query(AIAction).count()
    login(user.username)

    response = ask_with_fake(client, FakeAIClient(responses=[AICompletionResponse(content=selector_response)]), "Tell me something unusual")

    assert response.status_code == 502
    assert db.query(Recipe).count() == initial_recipes
    assert db.query(AIAction).count() == initial_actions


def test_recipe_permission_timeout_unavailable_and_disabled_are_safe(client, db, make_user, login):
    user = make_user("recipe-errors")
    fake = FakeAIClient()
    login(user.username)
    denied = ask_with_fake(client, fake, "Tell me something unusual")
    assert denied.status_code == 403
    assert fake.requests == []

    enable_recipes(db, user)
    timeout = ask_with_fake(client, FakeAIClient(responses=[AITimeoutError("slow")]), "Tell me something unusual")
    unavailable = ask_with_fake(client, FakeAIClient(responses=[AIUnavailableError("offline")]), "Tell me something unusual")
    disabled = client.post("/api/ai/recipes/questions", json={"question": "Tell me something unusual"})

    assert timeout.status_code == 504
    assert unavailable.status_code == 503
    assert disabled.status_code == 503


def test_cooking_timer_history_is_used_without_claiming_menu_is_cooking_history(db, make_user):
    user = make_user("recipe-history")
    older = add_recipe(db, user, "Older", "rice")
    newer = add_recipe(db, user, "Newer", "pasta")
    db.add_all(
        [
            RecipeCookingTimer(owner_id=user.id, recipe_id=older.id, stopped_at=datetime(2026, 8, 1), is_running=False),
            RecipeCookingTimer(owner_id=user.id, recipe_id=newer.id, stopped_at=datetime(2026, 9, 1), is_running=False),
        ]
    )
    db.commit()

    result = execute_recipe_tool(
        db,
        user,
        RecipeToolCall(tool=RecipeToolName.RECOMMEND, arguments={"sort_by": "last_cooked"}),
        today=TODAY,
    )

    assert result.data.cooking_history_available is True
    assert recipe_titles(result) == ["Older", "Newer"]
