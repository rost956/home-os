from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from app.ai.client import FakeAIClient
from app.ai.dependencies import get_ai_client
from app.ai.errors import AIUnavailableError
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


def test_unknown_merchant_with_low_confidence_creates_unresolved_pending_action(client, db, make_user, login):
    owner = make_user("unknown-owner")
    expense_setup(db, owner)
    enable_finance(db, owner)
    fake_client = FakeAIClient(
        responses=[
            AICompletionResponse(
                content=(
                    '{"title":"xteink","merchant":"xteink","category_id":1,'
                    '"category_confidence":0.2,"ambiguous":true}'
                )
            )
        ]
    )
    app.dependency_overrides[get_ai_client] = lambda: fake_client
    try:
        login(owner.username)
        response = client.post("/ai/expenses/draft", data={"text": "5800 xteink"}, follow_redirects=False)
    finally:
        app.dependency_overrides.pop(get_ai_client, None)

    assert response.status_code == 303
    assert len(fake_client.requests) == 1
    assert json.loads(latest_action(db).proposed_payload_json)["category_id"] is None
    assert db.query(AIAction).count() == 1
    assert db.query(ExpenseItem).count() == 0
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
                content=json.dumps(
                    {
                        "title": "xteink",
                        "merchant": "xteink",
                        "category_id": foreign_category.id,
                        "category_confidence": 0.99,
                        "ambiguous": False,
                    }
                )
            )
        ]
    )
    app.dependency_overrides[get_ai_client] = lambda: fake_client
    try:
        login(owner.username)
        response = client.post("/ai/expenses/draft", data={"text": "5800 xteink"}, follow_redirects=False)
    finally:
        app.dependency_overrides.pop(get_ai_client, None)

    assert response.status_code == 303
    assert json.loads(latest_action(db).proposed_payload_json)["category_id"] is None
    assert db.query(AIAction).count() == 1
    assert db.query(ExpenseItem).count() == 0


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


def test_real_world_failing_phrase_is_deterministic_and_creates_only_pending_action(
    client, db, make_user, login, monkeypatch
):
    owner = make_user("natural-failing-owner")
    expense_list, _food, transport = expense_setup(db, owner)
    enable_finance(db, owner)
    fake_client = FakeAIClient()
    app.dependency_overrides[get_ai_client] = lambda: fake_client
    monkeypatch.setattr("app.ai.expenses.today_msk", lambda: date(2026, 9, 7))
    try:
        login(owner.username)
        response = client.post(
            "/ai/expenses/draft",
            data={"text": "Бензин тест 2100 позавчера"},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_ai_client, None)

    payload = json.loads(latest_action(db).proposed_payload_json)
    assert response.status_code == 303
    assert fake_client.requests == []
    assert payload["expense_list_id"] == expense_list.id
    assert payload["category_id"] == transport.id
    assert payload["amount"] == "2100"
    assert payload["expense_date"] == "2026-09-05"
    assert payload["title"] == "Бензин тест"
    assert db.query(ExpenseItem).count() == 0


def test_llm_receives_fixed_facts_and_can_only_prepare_an_allowed_category(client, db, make_user, login, monkeypatch):
    owner = make_user("natural-llm-owner")
    _expense_list, food, _transport = expense_setup(db, owner)
    enable_finance(db, owner)
    fake_client = FakeAIClient(
        responses=[
            AICompletionResponse(
                content=json.dumps(
                    {
                        "title": "Цветы в Север",
                        "merchant": "Север",
                        "category_id": food.id,
                        "category_confidence": 0.91,
                        "ambiguous": False,
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )
    app.dependency_overrides[get_ai_client] = lambda: fake_client
    monkeypatch.setattr("app.ai.expenses.today_msk", lambda: date(2026, 9, 7))
    try:
        login(owner.username)
        response = client.post(
            "/ai/expenses/draft",
            data={"text": "в Север взял цветы за 1750 вчера вечером"},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_ai_client, None)

    payload = json.loads(latest_action(db).proposed_payload_json)
    prompt = "\n".join(message.content for message in fake_client.requests[0].messages)
    assert response.status_code == 303
    assert payload["amount"] == "1750"
    assert payload["expense_date"] == "2026-09-06"
    assert payload["category_id"] == food.id
    assert payload["merchant_key"] == "север"
    assert '"amount":"1750"' in prompt
    assert '"expense_date":"2026-09-06"' in prompt
    assert "fixed server facts" in prompt
    assert len(prompt) < 2_000
    assert db.query(ExpenseItem).count() == 0


def test_unresolved_category_requires_manual_choice_before_confirm(client, db, make_user, login):
    owner = make_user("natural-unresolved-owner")
    _expense_list, food, _transport = expense_setup(db, owner)
    enable_finance(db, owner)
    fake_client = FakeAIClient(
        responses=[
            AICompletionResponse(
                content=(
                    '{"title":"непонятная покупка","merchant":null,"category_id":null,'
                    '"category_confidence":0.1,"ambiguous":true}'
                )
            )
        ]
    )
    app.dependency_overrides[get_ai_client] = lambda: fake_client
    try:
        login(owner.username)
        draft_response = client.post(
            "/ai/expenses/draft",
            data={"text": "непонятная покупка 777"},
            follow_redirects=False,
        )
        action = latest_action(db)
        draft_page = client.get("/ai/settings")
        rejected_confirm = client.post(f"/ai/actions/{action.public_id}/confirm", follow_redirects=False)
        db.expire_all()
        pending_status = db.get(AIAction, action.id).status
        accepted_confirm = client.post(
            f"/ai/actions/{action.public_id}/confirm",
            data={"category_id": str(food.id)},
            follow_redirects=False,
        )
        repeated_confirm = client.post(f"/ai/actions/{action.public_id}/confirm", follow_redirects=False)
    finally:
        app.dependency_overrides.pop(get_ai_client, None)

    assert draft_response.status_code == 303
    assert "Не определена — выберите ниже" in draft_page.text
    assert "сегодня по умолчанию" in draft_page.text
    assert 'name="category_id" required' in draft_page.text
    assert "Опишите трату обычными словами" in draft_page.text
    assert rejected_confirm.status_code == 409
    assert pending_status == "pending"
    assert accepted_confirm.status_code == 303
    assert repeated_confirm.status_code == 303
    assert db.query(ExpenseItem).one().amount == Decimal("777")
    assert db.query(ExpenseItem).count() == 1


def test_multiple_expenses_are_not_merged_or_persisted(client, db, make_user, login):
    owner = make_user("natural-multiple-owner")
    expense_setup(db, owner)
    enable_finance(db, owner)
    login(owner.username)

    response = client.post("/ai/expenses/draft", data={"text": "кофе 250 и такси 617"})

    assert response.status_code == 422
    assert "Введите каждый расход отдельно" in response.text
    assert db.query(AIAction).count() == 0
    assert db.query(ExpenseItem).count() == 0


def test_saved_merchant_rule_matches_inside_natural_phrase_without_llm(client, db, make_user, login):
    owner = make_user("natural-rule-owner")
    expense_list, _food, transport = expense_setup(db, owner)
    db.add(
        ExpenseMerchantRule(
            owner_id=owner.id,
            expense_list_id=expense_list.id,
            category_id=transport.id,
            merchant_key="север сервис",
        )
    )
    db.commit()
    enable_finance(db, owner)
    fake_client = FakeAIClient()
    app.dependency_overrides[get_ai_client] = lambda: fake_client
    try:
        login(owner.username)
        response = client.post(
            "/ai/expenses/draft",
            data={"text": "вчера в Север Сервис починили кран за 3600"},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_ai_client, None)

    payload = json.loads(latest_action(db).proposed_payload_json)
    assert response.status_code == 303
    assert fake_client.requests == []
    assert payload["category_id"] == transport.id
    assert payload["merchant_key"] == "север сервис"


def test_short_unknown_expense_phrases_reach_bounded_semantic_model(client, db, make_user, login):
    owner = make_user("short-expense-semantics")
    _expense_list, food, _transport = expense_setup(db, owner)
    enable_finance(db, owner)
    texts = ("шава 350", "озон 900", "кофе 250", "цветы Ане 1800", "закинул на телефон 500")
    fake = FakeAIClient(
        responses=[
            AICompletionResponse(
                content=json.dumps(
                    {
                        "title": text.rsplit(" ", 1)[0],
                        "merchant": None,
                        "category_id": food.id,
                        "category_confidence": 0.9,
                        "ambiguous": False,
                    },
                    ensure_ascii=False,
                )
            )
            for text in texts
        ]
    )
    app.dependency_overrides[get_ai_client] = lambda: fake
    try:
        login(owner.username)
        responses = [client.post("/ai/expenses/draft", data={"text": text}, follow_redirects=False) for text in texts]
    finally:
        app.dependency_overrides.pop(get_ai_client, None)

    assert all(response.status_code == 303 for response in responses)
    assert len(fake.requests) == len(texts)
    assert all(request.enable_thinking is False for request in fake.requests)
    assert db.query(ExpenseItem).count() == 0
    assert db.query(AIAction).count() == len(texts)


def test_unsupported_refund_and_ai_unavailable_create_no_expense_action(client, db, make_user, login):
    owner = make_user("expense-safe-fallback")
    expense_setup(db, owner)
    enable_finance(db, owner)
    fake = FakeAIClient(responses=[AIUnavailableError("offline")])
    app.dependency_overrides[get_ai_client] = lambda: fake
    try:
        login(owner.username)
        refund = client.post("/ai/expenses/draft", data={"text": "Саша вернул 500 за такси"})
        unavailable = client.post("/ai/expenses/draft", data={"text": "озон 900"})
    finally:
        app.dependency_overrides.pop(get_ai_client, None)

    assert refund.status_code == 422
    assert "Возвраты и компенсации" in refund.text
    assert unavailable.status_code == 422
    assert db.query(AIAction).count() == 0
    assert db.query(ExpenseItem).count() == 0
