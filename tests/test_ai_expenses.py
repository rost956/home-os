from __future__ import annotations

import json
from datetime import date

from app.ai.client import FakeAIClient
from app.ai.dependencies import get_ai_client
from app.ai.expenses import normalize_merchant_key, parse_expense_text
from app.ai.permissions import get_or_create_ai_user_settings
from app.ai.schemas import AICompletionResponse
from app.main import app
from app.models import AIAction, ExpenseCategory, ExpenseItem, ExpenseList, ExpenseMerchantRule


def enable_finance(db, user) -> None:
    settings = get_or_create_ai_user_settings(db, user.id)
    settings.enabled = True
    settings.allow_finance = True
    db.commit()


def expense_setup(db, owner, *, title: str = "Дом") -> tuple[ExpenseList, ExpenseCategory, ExpenseCategory]:
    expense_list = ExpenseList(owner_id=owner.id, title=title)
    db.add(expense_list)
    db.flush()
    food = ExpenseCategory(expense_list_id=expense_list.id, name="Еда")
    transport = ExpenseCategory(expense_list_id=expense_list.id, name="Транспорт")
    db.add_all([food, transport])
    db.commit()
    return expense_list, food, transport


def latest_action(db) -> AIAction:
    return db.query(AIAction).order_by(AIAction.id.desc()).first()


def test_parser_interprets_relative_moscow_dates_without_llm():
    reference_day = date(2026, 9, 6)

    yesterday = parse_expense_text("Лента 1840 вчера", today=reference_day)
    today = parse_expense_text("Бензин 2600 сегодня", today=reference_day)
    reverse_order = parse_expense_text("5800 xteink", today=reference_day)

    assert (yesterday.title, yesterday.amount, yesterday.expense_date) == ("Лента", 1840, date(2026, 9, 5))
    assert (today.title, today.amount, today.expense_date) == ("Бензин", 2600, reference_day)
    assert (reverse_order.title, reverse_order.amount) == ("xteink", 5800)


def test_known_merchants_use_fast_path_without_llm(client, db, make_user, login):
    owner = make_user("fast-path-owner")
    expense_list, food, transport = expense_setup(db, owner)
    enable_finance(db, owner)
    fake_client = FakeAIClient()
    app.dependency_overrides[get_ai_client] = lambda: fake_client
    try:
        login(owner.username)
        food_response = client.post("/ai/expenses/draft", data={"text": "Лента 1840 вчера"}, follow_redirects=False)
        fuel_response = client.post("/ai/expenses/draft", data={"text": "Бензин 2600 сегодня"}, follow_redirects=False)
        store_response = client.post("/ai/expenses/draft", data={"text": "Пятёрочка 734"}, follow_redirects=False)
    finally:
        app.dependency_overrides.pop(get_ai_client, None)

    assert food_response.status_code == 303
    assert fuel_response.status_code == 303
    assert store_response.status_code == 303
    assert fake_client.requests == []
    payloads = [json.loads(item.proposed_payload_json) for item in db.query(AIAction).order_by(AIAction.id).all()]
    assert [item["expense_list_id"] for item in payloads] == [expense_list.id, expense_list.id, expense_list.id]
    assert [item["category_id"] for item in payloads] == [food.id, transport.id, food.id]
    assert db.query(ExpenseItem).count() == 0


def test_unknown_merchant_with_low_confidence_creates_no_action_or_category(client, db, make_user, login):
    owner = make_user("unknown-owner")
    expense_setup(db, owner)
    enable_finance(db, owner)
    fake_client = FakeAIClient(
        responses=[AICompletionResponse(content='{"category_id": 1, "confidence": 0.2, "ambiguous": true}')]
    )
    app.dependency_overrides[get_ai_client] = lambda: fake_client
    try:
        login(owner.username)
        response = client.post("/ai/expenses/draft", data={"text": "5800 xteink"})
    finally:
        app.dependency_overrides.pop(get_ai_client, None)

    assert response.status_code == 422
    assert "Выберите её вручную" in response.text
    assert len(fake_client.requests) == 1
    assert db.query(AIAction).count() == 0
    assert db.query(ExpenseCategory).count() == 2


def test_saved_merchant_rule_wins_without_llm(client, db, make_user, login):
    owner = make_user("rule-owner")
    expense_list, _food, transport = expense_setup(db, owner)
    db.add(
        ExpenseMerchantRule(
            owner_id=owner.id,
            expense_list_id=expense_list.id,
            category_id=transport.id,
            merchant_key=normalize_merchant_key("Пятёрочка"),
        )
    )
    db.commit()
    enable_finance(db, owner)
    fake_client = FakeAIClient()
    app.dependency_overrides[get_ai_client] = lambda: fake_client
    try:
        login(owner.username)
        response = client.post("/ai/expenses/draft", data={"text": "Пятёрочка 734"}, follow_redirects=False)
    finally:
        app.dependency_overrides.pop(get_ai_client, None)

    assert response.status_code == 303
    assert fake_client.requests == []
    assert json.loads(latest_action(db).proposed_payload_json)["category_id"] == transport.id


def test_llm_cannot_propose_a_category_outside_the_users_list(client, db, make_user, login):
    owner = make_user("llm-owner")
    stranger = make_user("llm-stranger")
    expense_setup(db, owner)
    _foreign_list, foreign_category, _other = expense_setup(db, stranger, title="Чужой")
    enable_finance(db, owner)
    fake_client = FakeAIClient(
        responses=[
            AICompletionResponse(
                content=json.dumps({"category_id": foreign_category.id, "confidence": 0.99, "ambiguous": False})
            )
        ]
    )
    app.dependency_overrides[get_ai_client] = lambda: fake_client
    try:
        login(owner.username)
        response = client.post("/ai/expenses/draft", data={"text": "5800 xteink"})
    finally:
        app.dependency_overrides.pop(get_ai_client, None)

    assert response.status_code == 422
    assert db.query(AIAction).count() == 0


def test_corrected_category_updates_existing_merchant_rule(client, db, make_user, login):
    owner = make_user("rule-update-owner")
    expense_list, food, transport = expense_setup(db, owner)
    db.add(
        ExpenseMerchantRule(
            owner_id=owner.id,
            expense_list_id=expense_list.id,
            category_id=food.id,
            merchant_key=normalize_merchant_key("Лента"),
        )
    )
    db.commit()
    enable_finance(db, owner)
    login(owner.username)

    draft = client.post("/ai/expenses/draft", data={"text": "Лента 100"}, follow_redirects=False)
    action = latest_action(db)
    confirm = client.post(
        f"/ai/actions/{action.public_id}/confirm",
        data={"category_id": str(transport.id), "remember_category": "1"},
        follow_redirects=False,
    )

    assert draft.status_code == 303
    assert confirm.status_code == 303
    rules = db.query(ExpenseMerchantRule).filter_by(owner_id=owner.id, expense_list_id=expense_list.id).all()
    assert len(rules) == 1
    assert rules[0].category_id == transport.id
    assert rules[0].use_count == 1


def test_user_correction_creates_rule_and_confirm_is_idempotent(client, db, make_user, login):
    owner = make_user("correction-owner")
    expense_list, food, transport = expense_setup(db, owner)
    enable_finance(db, owner)
    login(owner.username)

    draft = client.post("/ai/expenses/draft", data={"text": "Лента 1840 вчера"}, follow_redirects=False)
    action = latest_action(db)
    assert draft.status_code == 303
    assert json.loads(action.proposed_payload_json)["category_id"] == food.id
    assert db.query(ExpenseItem).count() == 0

    first_confirm = client.post(
        f"/ai/actions/{action.public_id}/confirm",
        data={"category_id": str(transport.id), "remember_category": "1"},
        follow_redirects=False,
    )
    second_confirm = client.post(
        f"/ai/actions/{action.public_id}/confirm",
        data={"category_id": str(transport.id), "remember_category": "1"},
        follow_redirects=False,
    )

    assert first_confirm.status_code == 303
    assert second_confirm.status_code == 303
    item = db.query(ExpenseItem).one()
    assert item.category_id == transport.id
    assert db.query(ExpenseItem).count() == 1
    db.expire_all()
    action = db.get(AIAction, action.id)
    assert json.loads(action.confirmed_payload_json)["category_id"] == transport.id
    rule = db.query(ExpenseMerchantRule).filter_by(owner_id=owner.id, expense_list_id=expense_list.id).one()
    assert rule.category_id == transport.id

    next_draft = client.post("/ai/expenses/draft", data={"text": "Лента 200"}, follow_redirects=False)
    assert next_draft.status_code == 303
    assert json.loads(latest_action(db).proposed_payload_json)["category_id"] == transport.id


def test_foreign_category_cannot_be_used_for_draft_or_confirm(client, db, make_user, login):
    owner = make_user("category-owner")
    stranger = make_user("category-stranger")
    expense_list, food, _transport = expense_setup(db, owner)
    _foreign_list, foreign_category, _other = expense_setup(db, stranger, title="Чужой")
    enable_finance(db, owner)
    login(owner.username)

    invalid_draft = client.post(
        "/ai/expenses/draft",
        data={"text": "Лента 100", "expense_list_id": str(expense_list.id), "category_id": str(foreign_category.id)},
    )
    valid_draft = client.post("/ai/expenses/draft", data={"text": "Лента 100"}, follow_redirects=False)
    action = latest_action(db)
    invalid_confirm = client.post(
        f"/ai/actions/{action.public_id}/confirm",
        data={"category_id": str(foreign_category.id)},
        follow_redirects=False,
    )

    assert invalid_draft.status_code == 422
    assert valid_draft.status_code == 303
    assert json.loads(action.proposed_payload_json)["category_id"] == food.id
    assert invalid_confirm.status_code == 409
    assert db.query(ExpenseItem).count() == 0
    db.expire_all()
    assert db.get(AIAction, action.id).status == "pending"
