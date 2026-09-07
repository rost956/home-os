from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.ai.client import FakeAIClient
from app.ai.dependencies import get_ai_client
from app.ai.errors import AITimeoutError, AIUnavailableError
from app.ai.menu import MenuProposalError, _candidate_arguments, menu_planning_window
from app.ai.permissions import get_or_create_ai_user_settings
from app.ai.schemas import AICompletionResponse
from app.main import app
from app.models import AIAction, MenuItem, Recipe

TODAY = date(2026, 9, 6)


def enable_menu(db, user) -> None:
    settings = get_or_create_ai_user_settings(db, user.id)
    settings.enabled = True
    settings.allow_menu = True
    db.commit()


def add_recipe(
    db,
    user,
    title: str,
    ingredients: str,
    *,
    cook_time: int = 30,
    cost: str = "300",
    servings: str = "2",
    tags: str | None = None,
) -> Recipe:
    recipe = Recipe(
        owner_id=user.id,
        title=title,
        ingredients=ingredients,
        cook_time_minutes=cook_time,
        cost=Decimal(cost),
        servings=Decimal(servings),
        tags=tags,
        steps="Cook",
    )
    db.add(recipe)
    db.flush()
    return recipe


def selection(entries: list[dict[str, object]]) -> AICompletionResponse:
    return AICompletionResponse(content=json.dumps({"entries": entries}))


def ask_with_fake(client, fake: FakeAIClient, question: str):
    app.dependency_overrides[get_ai_client] = lambda: fake
    try:
        return client.post("/api/ai/menu/proposals", json={"question": question})
    finally:
        app.dependency_overrides.pop(get_ai_client, None)


def test_one_day_proposal_is_pending_and_does_not_write_menu(client, db, make_user, login, monkeypatch):
    user = make_user("menu-one-day")
    recipe = add_recipe(db, user, "Chicken dinner", "chicken, rice")
    db.commit()
    enable_menu(db, user)
    monkeypatch.setattr("app.ai.menu.today_msk", lambda: TODAY)
    login(user.username)

    response = ask_with_fake(
        client,
        FakeAIClient(
            responses=[
                selection(
                    [{"plan_date": "2026-09-07", "meal_name": "Dinner", "recipe_id": recipe.id, "rationale": "Quick"}]
                )
            ]
        ),
        "Составь меню на завтра",
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["starts_on"] == "2026-09-07"
    assert payload["entries"][0]["recipe_id"] == recipe.id
    assert payload["entries"][0]["display_title"] == "Chicken dinner"
    assert db.query(MenuItem).count() == 0
    action = db.query(AIAction).filter_by(public_id=payload["action_id"]).one()
    assert action.action_type == "menu.apply"
    assert action.status == "pending"
    assert json.loads(action.proposed_payload_json)["entries"][0]["display_title"] == "Chicken dinner"


def test_three_day_menu_window_and_negative_constraint_are_preserved():
    window = menu_planning_window("Хочу три дня домашней еды без курицы", today=TODAY)
    arguments = _candidate_arguments("Хочу три дня домашней еды без курицы")

    assert window.dates == (date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9))
    assert arguments.exclude_ingredient == "куриц"
    assert arguments.ingredient is None


def test_three_day_menu_uses_only_non_excluded_candidates_and_stays_pending(
    client, db, make_user, login, monkeypatch
):
    user = make_user("menu-three-days")
    vegetables = [add_recipe(db, user, f"Home {index}", "vegetables") for index in range(3)]
    chicken = add_recipe(db, user, "Chicken", "курица, рис")
    db.commit()
    enable_menu(db, user)
    monkeypatch.setattr("app.ai.menu.today_msk", lambda: TODAY)
    login(user.username)
    entries = [
        {
            "plan_date": (TODAY + timedelta(days=index + 1)).isoformat(),
            "meal_name": "Dinner",
            "recipe_id": vegetables[index].id,
        }
        for index in range(3)
    ]
    fake = FakeAIClient(responses=[selection(entries)])

    response = ask_with_fake(client, fake, "Хочу три дня домашней еды без курицы")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert (payload["starts_on"], payload["ends_on"]) == ("2026-09-07", "2026-09-09")
    assert chicken.id not in {entry["recipe_id"] for entry in payload["entries"]}
    assert "Chicken" not in fake.requests[0].messages[1].content
    assert '"unresolved_text":"три дня домашней еды"' in fake.requests[0].messages[1].content
    assert db.query(MenuItem).count() == 0
    assert db.query(AIAction).count() == 1


def test_menu_weekend_weekdays_and_explicit_date_windows():
    weekend = menu_planning_window("Сделай план еды на выходные", today=TODAY)
    weekdays = menu_planning_window("На понедельник и среду поставь лёгкое", today=TODAY)
    explicit = menu_planning_window("Для ребёнка на 12 сентября", today=TODAY)

    assert weekend.dates == (date(2026, 9, 12), date(2026, 9, 13))
    assert weekdays.dates == (date(2026, 9, 7), date(2026, 9, 9))
    assert explicit.dates == (date(2026, 9, 12),)


@pytest.mark.parametrize("question", ["Меню на 15 дней", "Меню на 31 февраля", "Меню на 12 сентября 2025"])
def test_invalid_or_past_menu_window_is_rejected_instead_of_defaulting_to_one_day(question: str):
    with pytest.raises(MenuProposalError):
        menu_planning_window(question, today=TODAY)


def test_menu_previous_week_no_repeat_phrase_excludes_recent_recipe(client, db, make_user, login, monkeypatch):
    user = make_user("menu-natural-repeat")
    recent = add_recipe(db, user, "Recent", "beans")
    available = add_recipe(db, user, "Available", "rice")
    db.add(MenuItem(owner_id=user.id, recipe_id=recent.id, plan_date=datetime(2026, 9, 3), meal_name="Dinner"))
    db.commit()
    enable_menu(db, user)
    monkeypatch.setattr("app.ai.menu.today_msk", lambda: TODAY)
    login(user.username)
    fake = FakeAIClient(
        responses=[selection([{"plan_date": "2026-09-07", "meal_name": "Dinner", "recipe_id": available.id}])]
    )

    response = ask_with_fake(client, fake, "Не повторяй то, что ели на прошлой неделе")

    assert response.status_code == 200, response.text
    assert recent.id not in {entry["recipe_id"] for entry in response.json()["entries"]}
    assert "Recent" not in fake.requests[0].messages[1].content


def test_week_proposal_uses_only_shortlisted_recipes_and_excludes_recent_menu(client, db, make_user, login, monkeypatch):
    user = make_user("menu-week")
    recent = add_recipe(db, user, "Recent", "pasta")
    candidates = [add_recipe(db, user, f"Recipe {index}", "vegetables") for index in range(7)]
    db.add(MenuItem(owner_id=user.id, recipe_id=recent.id, plan_date=datetime(2026, 8, 31), meal_name="Dinner"))
    db.commit()
    enable_menu(db, user)
    monkeypatch.setattr("app.ai.menu.today_msk", lambda: TODAY)
    login(user.username)
    entries = [
        {"plan_date": (date(2026, 9, 7) + timedelta(days=index)).isoformat(), "meal_name": "Dinner", "recipe_id": candidates[index].id}
        for index in range(7)
    ]
    fake = FakeAIClient(responses=[selection(entries)])

    response = ask_with_fake(client, fake, "Составь меню на следующую неделю без повторов последних двух недель")

    assert response.status_code == 200
    proposal = response.json()
    assert proposal["starts_on"] == "2026-09-07"
    assert proposal["ends_on"] == "2026-09-13"
    assert [entry["recipe_id"] for entry in proposal["entries"]] == [recipe.id for recipe in candidates]
    assert recent.id not in {entry["recipe_id"] for entry in proposal["entries"]}
    assert "Recent" not in fake.requests[0].messages[1].content
    assert db.query(MenuItem).count() == 1


def test_week_proposal_rejects_missing_day(client, db, make_user, login, monkeypatch):
    user = make_user("menu-week-missing-day")
    recipe = add_recipe(db, user, "Recipe", "vegetables")
    db.commit()
    enable_menu(db, user)
    monkeypatch.setattr("app.ai.menu.today_msk", lambda: TODAY)
    login(user.username)

    response = ask_with_fake(
        client,
        FakeAIClient(responses=[selection([{"plan_date": "2026-09-07", "meal_name": "Dinner", "recipe_id": recipe.id}])]),
        "Составь меню на следующую неделю",
    )

    assert response.status_code == 502
    assert db.query(AIAction).count() == 0
    assert db.query(MenuItem).count() == 0


@pytest.mark.parametrize(
    ("question", "recipe_kwargs"),
    [
        ("Не больше часа готовки", {"cook_time": 90}),
        ("Недорого до 200 руб", {"cost": "250"}),
        ("На 4 порции", {"servings": "2"}),
        ("Без рыбы", {"ingredients": "рыба, картофель"}),
    ],
)
def test_menu_deterministic_filters_produce_empty_shortlist(client, db, make_user, login, monkeypatch, question, recipe_kwargs):
    user = make_user(f"menu-empty-{abs(hash(question))}")
    values = {"ingredients": "beans", **recipe_kwargs}
    add_recipe(db, user, "Unavailable", **values)
    db.commit()
    enable_menu(db, user)
    monkeypatch.setattr("app.ai.menu.today_msk", lambda: TODAY)
    login(user.username)
    fake = FakeAIClient()

    response = ask_with_fake(client, fake, question)

    assert response.status_code == 422
    assert fake.requests == []
    assert db.query(AIAction).count() == 0
    assert db.query(MenuItem).count() == 0


def test_conflict_is_exposed_and_confirm_adds_without_overwriting(client, db, make_user, login, monkeypatch):
    user = make_user("menu-conflict")
    current = add_recipe(db, user, "Current", "beans")
    proposed = add_recipe(db, user, "Proposed", "rice")
    existing = MenuItem(owner_id=user.id, recipe_id=current.id, plan_date=datetime(2026, 9, 7), meal_name="Dinner")
    db.add(existing)
    db.commit()
    enable_menu(db, user)
    monkeypatch.setattr("app.ai.menu.today_msk", lambda: TODAY)
    login(user.username)

    proposal_response = ask_with_fake(
        client,
        FakeAIClient(responses=[selection([{"plan_date": "2026-09-07", "meal_name": "Dinner", "recipe_id": proposed.id}])]),
        "Выбери что-то из наших рецептов на завтра",
    )

    assert proposal_response.status_code == 200
    proposal = proposal_response.json()
    assert proposal["conflicts"] == [
        {
            "item_id": existing.id,
            "plan_date": "2026-09-07",
            "meal_name": "Dinner",
            "recipe_id": current.id,
            "recipe_title": "Current",
        }
    ]
    assert db.query(MenuItem).count() == 1

    confirmed = client.post(f"/ai/actions/{proposal['action_id']}/confirm", follow_redirects=False)

    assert confirmed.status_code == 303
    assert [(item.recipe_id, item.meal_name) for item in db.query(MenuItem).order_by(MenuItem.id)] == [
        (current.id, "Dinner"),
        (proposed.id, "Dinner"),
    ]


def test_menu_form_and_confirmation_card_show_entries_and_conflicts(client, db, make_user, login, monkeypatch):
    user = make_user("menu-form")
    current = add_recipe(db, user, "Current", "beans")
    proposed = add_recipe(db, user, "Proposed", "rice")
    db.add(MenuItem(owner_id=user.id, recipe_id=current.id, plan_date=datetime(2026, 9, 7), meal_name="Dinner"))
    db.commit()
    enable_menu(db, user)
    monkeypatch.setattr("app.ai.menu.today_msk", lambda: TODAY)
    login(user.username)
    fake = FakeAIClient(
        responses=[selection([{"plan_date": "2026-09-07", "meal_name": "Dinner", "recipe_id": proposed.id}])]
    )
    app.dependency_overrides[get_ai_client] = lambda: fake
    try:
        prepared = client.post(
            "/ai/menu/proposals",
            data={"question": "Выбери что-то из наших рецептов на завтра"},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_ai_client, None)

    assert prepared.status_code == 303
    page = client.get("/ai/settings")
    assert page.status_code == 200
    assert "Составить меню" in page.text
    assert "Proposed" in page.text
    assert "Current" in page.text
    assert "В эти слоты уже добавлены блюда" in page.text
    assert db.query(MenuItem).count() == 1


def test_menu_confirm_cancel_and_repeat_confirm_are_safe(client, db, make_user, login, monkeypatch):
    user = make_user("menu-confirm")
    recipe = add_recipe(db, user, "Recipe", "beans")
    db.commit()
    enable_menu(db, user)
    monkeypatch.setattr("app.ai.menu.today_msk", lambda: TODAY)
    login(user.username)
    proposal = ask_with_fake(
        client,
        FakeAIClient(responses=[selection([{"plan_date": "2026-09-07", "meal_name": "Dinner", "recipe_id": recipe.id}])]),
        "Составь меню на завтра",
    ).json()

    cancelled = client.post(f"/ai/actions/{proposal['action_id']}/cancel", follow_redirects=False)
    assert cancelled.status_code == 303
    assert db.query(MenuItem).count() == 0
    assert client.post(f"/ai/actions/{proposal['action_id']}/confirm", follow_redirects=False).status_code == 409

    confirmed_proposal = ask_with_fake(
        client,
        FakeAIClient(responses=[selection([{"plan_date": "2026-09-07", "meal_name": "Lunch", "recipe_id": recipe.id}])]),
        "Составь меню на завтра",
    ).json()
    first = client.post(f"/ai/actions/{confirmed_proposal['action_id']}/confirm", follow_redirects=False)
    second = client.post(f"/ai/actions/{confirmed_proposal['action_id']}/confirm", follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 303
    assert db.query(MenuItem).count() == 1


def test_menu_rejects_foreign_or_invalid_recipe_ids_without_creating_action(client, db, make_user, login, monkeypatch):
    owner = make_user("menu-owner")
    stranger = make_user("menu-stranger")
    own_recipe = add_recipe(db, owner, "Own", "beans")
    foreign_recipe = add_recipe(db, stranger, "Foreign", "beans")
    db.commit()
    enable_menu(db, owner)
    monkeypatch.setattr("app.ai.menu.today_msk", lambda: TODAY)
    login(owner.username)

    foreign = ask_with_fake(
        client,
        FakeAIClient(responses=[selection([{"plan_date": "2026-09-07", "meal_name": "Dinner", "recipe_id": foreign_recipe.id}])]),
        "Составь меню на завтра",
    )
    invalid = ask_with_fake(
        client,
        FakeAIClient(responses=[selection([{"plan_date": "2026-09-07", "meal_name": "Dinner", "recipe_id": own_recipe.id + foreign_recipe.id + 999}])]),
        "Составь меню на завтра",
    )

    assert foreign.status_code == 502
    assert invalid.status_code == 502
    assert db.query(AIAction).count() == 0
    assert db.query(MenuItem).count() == 0


@pytest.mark.parametrize("response", ["not-json", '{"entries":[]}'])
def test_menu_invalid_json_is_safe(client, db, make_user, login, monkeypatch, response):
    user = make_user(f"menu-invalid-{abs(hash(response))}")
    add_recipe(db, user, "Recipe", "beans")
    db.commit()
    enable_menu(db, user)
    monkeypatch.setattr("app.ai.menu.today_msk", lambda: TODAY)
    login(user.username)

    result = ask_with_fake(client, FakeAIClient(responses=[AICompletionResponse(content=response)]), "Составь меню на завтра")

    assert result.status_code == 502
    assert db.query(AIAction).count() == 0


def test_menu_permission_timeout_unavailable_and_disabled_are_safe(client, db, make_user, login, monkeypatch):
    user = make_user("menu-errors")
    add_recipe(db, user, "Recipe", "beans")
    db.commit()
    monkeypatch.setattr("app.ai.menu.today_msk", lambda: TODAY)
    login(user.username)
    fake = FakeAIClient()
    denied = ask_with_fake(client, fake, "Составь меню на завтра")
    assert denied.status_code == 403
    assert fake.requests == []

    enable_menu(db, user)
    timeout = ask_with_fake(client, FakeAIClient(responses=[AITimeoutError("slow")]), "Составь меню на завтра")
    unavailable = ask_with_fake(client, FakeAIClient(responses=[AIUnavailableError("offline")]), "Составь меню на завтра")
    disabled = client.post("/api/ai/menu/proposals", json={"question": "Составь меню на завтра"})

    assert timeout.status_code == 504
    assert unavailable.status_code == 503
    assert disabled.status_code == 503
