from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from app.ai.client import FakeAIClient
from app.ai.dependencies import get_ai_client
from app.ai.errors import AITimeoutError, AIUnavailableError
from app.ai.finance import select_deterministic_finance_tool
from app.ai.permissions import get_or_create_ai_user_settings
from app.ai.schemas import AICompletionResponse
from app.ai.tools.finance import FinanceToolName
from app.main import app
from app.models import AIAction, ExpenseCategory, ExpenseItem, ExpenseLimit, ExpenseList
from app.timezone import msk_date_to_utc_naive

TODAY = date(2026, 9, 6)


def enable_finance(db, user) -> None:
    settings = get_or_create_ai_user_settings(db, user.id)
    settings.enabled = True
    settings.allow_finance = True
    db.commit()


def add_expense_list(db, user, title: str = "Дом") -> tuple[ExpenseList, ExpenseCategory, ExpenseCategory]:
    expense_list = ExpenseList(owner_id=user.id, title=title)
    db.add(expense_list)
    db.flush()
    food = ExpenseCategory(expense_list_id=expense_list.id, name="Еда")
    transport = ExpenseCategory(expense_list_id=expense_list.id, name="Транспорт")
    db.add_all([food, transport])
    db.flush()
    return expense_list, food, transport


def add_expense(db, category, title: str, amount: str, day: date) -> ExpenseItem:
    item = ExpenseItem(
        category_id=category.id,
        title=title,
        amount=Decimal(amount),
        created_at=msk_date_to_utc_naive(day),
    )
    db.add(item)
    return item


def ask_with_fake(client, fake: FakeAIClient, question: str):
    app.dependency_overrides[get_ai_client] = lambda: fake
    try:
        return client.post("/api/ai/finance/questions", json={"question": question})
    finally:
        app.dependency_overrides.pop(get_ai_client, None)


def answer(text: str = "Готово") -> AICompletionResponse:
    return AICompletionResponse(content=json.dumps({"answer": text}, ensure_ascii=False))


@pytest.mark.parametrize(
    ("question", "expected_tool"),
    [
        ("Сколько я потратил в августе?", FinanceToolName.SUMMARY),
        ("На что больше всего ушло?", FinanceToolName.CATEGORY_BREAKDOWN),
        ("Почему в этом месяце расходы выше?", FinanceToolName.COMPARE_PERIODS),
        ("Сравни этот месяц с прошлым", FinanceToolName.COMPARE_PERIODS),
        ("Сколько я обычно трачу на машину?", FinanceToolName.CATEGORY_BREAKDOWN),
        ("Какие категории сильнее всего выросли?", FinanceToolName.COMPARE_PERIODS),
        ("Какой прогноз до конца месяца?", FinanceToolName.FORECAST),
        ("Какие лимиты почти закончились?", FinanceToolName.BUDGET_STATUS),
        ("Что сейчас происходит с моими финансами?", FinanceToolName.SUMMARY),
        ("Какие самые крупные траты?", FinanceToolName.LARGEST_EXPENSES),
    ],
)
def test_deterministic_finance_tool_selection(question, expected_tool):
    call = select_deterministic_finance_tool(question, today=TODAY)

    assert call is not None
    assert call.tool == expected_tool
    if "августе" in question:
        assert call.arguments == {"period": "month", "year": 2026, "month": 8}


def test_llm_selector_uses_one_allowlisted_tool_and_bounded_result(client, db, make_user, login, monkeypatch):
    user = make_user("finance-selector")
    _expense_list, food, _transport = add_expense_list(db, user)
    add_expense(db, food, "Большая покупка", "500", TODAY)
    db.commit()
    enable_finance(db, user)
    monkeypatch.setattr("app.ai.finance.today_msk", lambda: TODAY)
    fake = FakeAIClient(
        responses=[
            AICompletionResponse(content='{"tool":"get_largest_expenses","arguments":{"limit":3}}'),
            answer("Крупнейшая трата — 500 ₽."),
        ]
    )
    login(user.username)

    response = ask_with_fake(client, fake, "Дай картину по деньгам")

    assert response.status_code == 200
    payload = response.json()
    assert payload["tool"] == "get_largest_expenses"
    assert payload["result"]["data"]["expenses"][0]["amount"] == "500.00"
    assert len(fake.requests) == 2
    assert "Разрешенные инструменты" in fake.requests[0].messages[0].content
    assert "Большая покупка" not in fake.requests[0].messages[0].content
    assert "Большая покупка" in fake.requests[1].messages[1].content


def test_finance_permission_blocks_request_before_model_call(client, db, make_user, login):
    user = make_user("finance-permission-denied")
    fake = FakeAIClient()
    login(user.username)

    response = ask_with_fake(client, fake, "Сколько я потратил?")

    assert response.status_code == 403
    assert fake.requests == []


def test_summary_is_owner_scoped_does_not_write_or_leak(client, db, make_user, login, monkeypatch):
    alice = make_user("finance-alice")
    bob = make_user("finance-bob")
    _alice_list, alice_food, _alice_transport = add_expense_list(db, alice, "Алиса")
    _bob_list, bob_food, _bob_transport = add_expense_list(db, bob, "Боб")
    add_expense(db, alice_food, "Покупка Алисы", "100", TODAY)
    add_expense(db, bob_food, "Секрет Боба", "9999", TODAY)
    db.commit()
    enable_finance(db, alice)
    enable_finance(db, bob)
    monkeypatch.setattr("app.ai.finance.today_msk", lambda: TODAY)
    initial_expenses = db.query(ExpenseItem).count()
    initial_actions = db.query(AIAction).count()

    login(alice.username)
    alice_fake = FakeAIClient(responses=[answer("Алиса потратила 100 ₽.")])
    alice_response = ask_with_fake(client, alice_fake, "Что сейчас происходит с моими финансами?")

    assert alice_response.status_code == 200
    assert alice_response.json()["result"]["data"]["expenses"] == "100.00"
    assert "9999" not in alice_fake.requests[0].messages[1].content
    assert "Секрет Боба" not in alice_fake.requests[0].messages[1].content

    login(bob.username)
    bob_response = ask_with_fake(client, FakeAIClient(responses=[answer()]), "Сколько я потратил?")
    assert bob_response.status_code == 200
    assert bob_response.json()["result"]["data"]["expenses"] == "9999.00"
    assert db.query(ExpenseItem).count() == initial_expenses
    assert db.query(AIAction).count() == initial_actions


def test_empty_summary_and_unavailable_forecast(client, db, make_user, login, monkeypatch):
    user = make_user("empty-ai-finance")
    enable_finance(db, user)
    monkeypatch.setattr("app.ai.finance.today_msk", lambda: TODAY)
    fake = FakeAIClient(responses=[answer("Операций нет."), answer("Для прогноза недостаточно истории.")])
    login(user.username)

    summary = ask_with_fake(client, fake, "Сколько я потратил?")
    forecast = ask_with_fake(client, fake, "Какой прогноз до конца месяца?")

    assert summary.status_code == 200
    summary_data = summary.json()["result"]["data"]
    assert summary_data["income"] == "0.00"
    assert summary_data["expenses"] == "0.00"
    assert summary_data["balance"] == "0.00"
    assert summary_data["savings_rate_percent"] is None
    assert forecast.status_code == 200
    forecast_data = forecast.json()["result"]["data"]
    assert forecast_data["available"] is False
    assert forecast_data["unavailable_reason"] == "insufficient_history"
    assert forecast_data["expected_expenses"] is None


def test_comparison_categories_and_budget_have_exact_python_values(client, db, make_user, login, monkeypatch):
    user = make_user("finance-values")
    _expense_list, food, transport = add_expense_list(db, user)
    add_expense(db, food, "Сейчас еда", "900", date(2026, 9, 2))
    add_expense(db, transport, "Сейчас транспорт", "1200", date(2026, 9, 3))
    add_expense(db, food, "Раньше еда", "300", date(2026, 8, 2))
    db.add_all(
        [
            ExpenseLimit(owner_id=user.id, category_name="Еда", monthly_limit=Decimal("1000")),
            ExpenseLimit(owner_id=user.id, category_name="Транспорт", monthly_limit=Decimal("1000")),
        ]
    )
    db.commit()
    enable_finance(db, user)
    monkeypatch.setattr("app.ai.finance.today_msk", lambda: TODAY)
    fake = FakeAIClient(responses=[answer(), answer(), answer(), answer(), answer()])
    login(user.username)

    comparison = ask_with_fake(client, fake, "Сравни этот месяц с прошлым")
    categories = ask_with_fake(client, fake, "На что больше всего ушло?")
    budget = ask_with_fake(client, fake, "Какие лимиты почти закончились?")
    august = ask_with_fake(client, fake, "Сколько я потратил в августе?")
    forecast = ask_with_fake(client, fake, "Какой прогноз до конца месяца?")

    comparison_data = comparison.json()["result"]["data"]
    assert comparison.status_code == 200
    assert comparison_data["current_expenses"] == "2100.00"
    assert comparison_data["previous_expenses"] == "300.00"
    assert comparison_data["difference"] == "1800.00"
    assert comparison_data["percent_change"] == "600.0"
    assert comparison_data["direction"] == "higher"
    assert categories.status_code == 200
    assert [item["category"] for item in categories.json()["result"]["data"]["categories"][:2]] == [
        "Транспорт",
        "Еда",
    ]
    assert budget.status_code == 200
    budget_rows = {item["category"]: item for item in budget.json()["result"]["data"]["categories"]}
    assert budget_rows["Еда"]["percent"] == 90
    assert budget_rows["Еда"]["state"] == "almost_exhausted"
    assert budget_rows["Транспорт"]["percent"] == 120
    assert budget_rows["Транспорт"]["state"] == "exceeded"
    assert august.status_code == 200
    august_data = august.json()["result"]["data"]
    assert august_data["period"] == {"start": "2026-08-01", "end": "2026-08-31", "actual_end": "2026-08-31"}
    assert august_data["expenses"] == "300.00"
    assert forecast.status_code == 200
    forecast_data = forecast.json()["result"]["data"]
    assert forecast_data["available"] is True
    assert forecast_data["expected_expenses"] == "6774.11"
    assert forecast_data["daily_rate"] == "194.75"
    assert forecast_data["confidence"] == "средняя"


@pytest.mark.parametrize(
    "selector_response",
    [
        "not-json",
        '{"tool":"drop_database","arguments":{}}',
        '{"tool":"expense.create","arguments":{}}',
        '{"tool":"get_finance_summary","arguments":{"user_id":999}}',
    ],
)
def test_invalid_unknown_write_and_user_id_tool_calls_are_rejected(
    client,
    db,
    make_user,
    login,
    monkeypatch,
    selector_response,
):
    user = make_user(f"invalid-finance-{abs(hash(selector_response))}")
    _expense_list, food, _transport = add_expense_list(db, user)
    add_expense(db, food, "Существующая трата", "10", TODAY)
    db.commit()
    enable_finance(db, user)
    monkeypatch.setattr("app.ai.finance.today_msk", lambda: TODAY)
    fake = FakeAIClient(responses=[AICompletionResponse(content=selector_response)])
    initial_expenses = db.query(ExpenseItem).count()
    login(user.username)

    response = ask_with_fake(client, fake, "Дай картину по деньгам")

    assert response.status_code == 502
    assert db.query(ExpenseItem).count() == initial_expenses
    assert db.query(AIAction).count() == 0


def test_timeout_unavailable_and_disabled_backend_are_safe(client, db, make_user, login, monkeypatch):
    user = make_user("finance-backend-errors")
    enable_finance(db, user)
    monkeypatch.setattr("app.ai.finance.today_msk", lambda: TODAY)
    login(user.username)

    timeout = ask_with_fake(client, FakeAIClient(responses=[AITimeoutError("slow")]), "Сколько я потратил?")
    unavailable = ask_with_fake(
        client,
        FakeAIClient(responses=[AIUnavailableError("offline")]),
        "Сколько я потратил?",
    )
    disabled = client.post("/api/ai/finance/questions", json={"question": "Сколько я потратил?"})

    assert timeout.status_code == 504
    assert unavailable.status_code == 503
    assert disabled.status_code == 503
