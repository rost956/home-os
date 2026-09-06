from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.models import (
    ExpenseCategory,
    ExpenseItem,
    ExpenseLimit,
    ExpenseList,
    ExpenseListShare,
    IncomeItem,
    RecurringExpense,
)
from app.services.finance import (
    build_finance_snapshot,
    expense_period_bounds,
    previous_expense_period_bounds,
)
from app.timezone import msk_date_to_utc_naive


def add_expense(
    db,
    category: ExpenseCategory,
    title: str,
    amount: str,
    day: date,
    *,
    analytics: bool = True,
    forecast: bool = True,
) -> ExpenseItem:
    item = ExpenseItem(
        category_id=category.id,
        title=title,
        amount=Decimal(amount),
        created_at=msk_date_to_utc_naive(day),
        include_in_analytics=analytics,
        include_in_forecast=forecast,
    )
    db.add(item)
    return item


def create_expense_list(db, owner, title: str = "Дом"):
    expense_list = ExpenseList(owner_id=owner.id, title=title)
    db.add(expense_list)
    db.flush()
    food = ExpenseCategory(expense_list_id=expense_list.id, name="Еда")
    transport = ExpenseCategory(expense_list_id=expense_list.id, name="Транспорт")
    empty = ExpenseCategory(expense_list_id=expense_list.id, name="Без расходов")
    db.add_all([food, transport, empty])
    db.flush()
    return expense_list, food, transport, empty


def test_empty_period_and_income_only_have_exact_totals(db, make_user):
    empty_user = make_user("empty-finance")
    empty = build_finance_snapshot(db, empty_user, today=date(2026, 4, 10))

    assert empty.income_total == Decimal("0.00")
    assert empty.expense_total == Decimal("0.00")
    assert empty.balance == Decimal("0.00")
    assert empty.savings_rate is None
    assert empty.categories == ()
    assert empty.largest_expenses == ()
    assert empty.budget.limit_total == Decimal("0.00")
    assert empty.forecast.forecast == Decimal("0.00")
    assert empty.forecast.confidence == "низкая"
    assert empty.forecast.history_days == 0

    income_user = make_user("income-only")
    db.add(
        IncomeItem(
            owner_id=income_user.id,
            title="Зарплата",
            amount=Decimal("5000.00"),
            received_at=msk_date_to_utc_naive(date(2026, 4, 5)),
        )
    )
    db.commit()
    income = build_finance_snapshot(db, income_user, today=date(2026, 4, 10))

    assert income.income_total == Decimal("5000.00")
    assert income.expense_total == Decimal("0.00")
    assert income.balance == Decimal("5000.00")
    assert income.savings_rate == Decimal("100.0")


def test_expenses_only_and_empty_categories_are_preserved(db, make_user):
    user = make_user("expense-only")
    _expense_list, food, _transport, empty_category = create_expense_list(db, user)
    add_expense(db, food, "Продукты", "125.50", date(2026, 4, 4))
    db.commit()

    snapshot = build_finance_snapshot(db, user, today=date(2026, 4, 10))
    categories = {item.name: item for item in snapshot.categories}

    assert snapshot.income_total == Decimal("0.00")
    assert snapshot.expense_total == Decimal("125.50")
    assert snapshot.balance == Decimal("-125.50")
    assert snapshot.savings_rate is None
    assert categories["Еда"].current == Decimal("125.50")
    assert categories[empty_category.name].current == Decimal("0.00")
    assert snapshot.largest_expenses[0].amount == Decimal("125.50")


def test_incomplete_period_comparison_limits_recurring_and_drivers(db, make_user):
    user = make_user("complete-snapshot")
    user.expense_period_start_day = 15
    expense_list, food, transport, empty_category = create_expense_list(db, user)

    add_expense(db, food, "Текущая еда 1", "100", date(2026, 2, 16))
    add_expense(db, food, "Текущая еда 2", "200", date(2026, 3, 1))
    add_expense(db, transport, "Такси", "300", date(2026, 3, 3))
    add_expense(db, food, "Будущая трата", "50", date(2026, 3, 10))
    add_expense(db, food, "Исключено", "999", date(2026, 3, 2), analytics=False)
    add_expense(db, food, "Прошлая еда", "80", date(2026, 1, 20))
    add_expense(db, transport, "Прошлый транспорт", "120", date(2026, 2, 1))
    add_expense(db, food, "Поздняя прошлая", "400", date(2026, 2, 10))
    db.add_all(
        [
            IncomeItem(
                owner_id=user.id,
                title="Доход",
                amount=Decimal("1000"),
                received_at=msk_date_to_utc_naive(date(2026, 2, 20)),
            ),
            IncomeItem(
                owner_id=user.id,
                title="Будущий доход",
                amount=Decimal("500"),
                received_at=msk_date_to_utc_naive(date(2026, 3, 10)),
            ),
            ExpenseLimit(owner_id=user.id, category_name="Еда", monthly_limit=Decimal("500")),
            ExpenseLimit(owner_id=user.id, category_name="Транспорт", monthly_limit=Decimal("400")),
            RecurringExpense(
                owner_id=user.id,
                expense_list_id=expense_list.id,
                category_name="Дом",
                title="Интернет",
                amount=Decimal("100"),
                day_of_month=10,
            ),
            RecurringExpense(
                owner_id=user.id,
                expense_list_id=expense_list.id,
                category_name="Дом",
                title="Выключенный",
                amount=Decimal("999"),
                day_of_month=11,
                is_active=False,
            ),
        ]
    )
    db.commit()

    snapshot = build_finance_snapshot(db, user, today=date(2026, 3, 5))
    categories = {item.name: item for item in snapshot.categories}

    assert (snapshot.period_start, snapshot.period_end, snapshot.actual_end) == (
        date(2026, 2, 15),
        date(2026, 3, 14),
        date(2026, 3, 5),
    )
    assert snapshot.income_total == Decimal("1000.00")
    assert snapshot.period_income_total == Decimal("1500.00")
    assert snapshot.expense_total == Decimal("600.00")
    assert snapshot.period_expense_total == Decimal("650.00")
    assert snapshot.balance == Decimal("400.00")
    assert snapshot.savings_rate == Decimal("40.0")
    assert snapshot.previous_period_expense_total == Decimal("600.00")
    assert snapshot.period_difference == Decimal("50.00")

    assert snapshot.comparison.previous_start == date(2026, 1, 15)
    assert snapshot.comparison.previous_end == date(2026, 2, 14)
    assert snapshot.comparison.comparable_end == date(2026, 2, 2)
    assert snapshot.comparison.current_total == Decimal("600.00")
    assert snapshot.comparison.previous_total == Decimal("200.00")
    assert snapshot.comparison.difference == Decimal("400.00")
    assert snapshot.comparison.percent_change == Decimal("200.0")

    assert categories["Еда"].current == Decimal("300.00")
    assert categories["Еда"].previous == Decimal("80.00")
    assert categories["Еда"].difference == Decimal("220.00")
    assert categories["Транспорт"].difference == Decimal("180.00")
    assert categories[empty_category.name].current == Decimal("0.00")
    assert [item.amount for item in snapshot.largest_expenses] == [
        Decimal("300.00"),
        Decimal("200.00"),
        Decimal("100.00"),
    ]

    assert snapshot.budget.limit_total == Decimal("900.00")
    assert snapshot.budget.spent == Decimal("650.00")
    assert snapshot.budget.left == Decimal("250.00")
    assert snapshot.budget.percent == 72
    assert len(snapshot.recurring_payments) == 1
    assert snapshot.recurring_payments[0].title == "Интернет"
    assert snapshot.forecast.recurring_by_day[date(2026, 3, 10)] == Decimal("100.00")


def test_recurring_forecast_without_history_has_exact_values(db, make_user):
    user = make_user("recurring-forecast")
    expense_list, _food, _transport, _empty = create_expense_list(db, user)
    db.add(
        RecurringExpense(
            owner_id=user.id,
            expense_list_id=expense_list.id,
            category_name="Дом",
            title="Аренда",
            amount=Decimal("300"),
            day_of_month=15,
        )
    )
    db.commit()

    snapshot = build_finance_snapshot(db, user, today=date(2026, 1, 10))

    assert snapshot.forecast.history_days == 0
    assert snapshot.forecast.confidence == "низкая"
    assert {day: amount for day, amount in snapshot.forecast.recurring_by_day.items() if amount} == {
        date(2026, 1, 15): Decimal("300.00")
    }
    assert snapshot.forecast.forecast == Decimal("300.00")
    assert snapshot.forecast.daily_rate == Decimal("14.29")


def test_weekday_forecast_reuses_history_and_forecast_flag(db, make_user):
    user = make_user("weekday-forecast")
    _expense_list, food, _transport, _empty = create_expense_list(db, user)
    add_expense(db, food, "Included", "100", date(2026, 4, 3))
    add_expense(db, food, "Actual only", "50", date(2026, 4, 4), forecast=False)
    db.commit()

    snapshot = build_finance_snapshot(db, user, today=date(2026, 4, 10))

    assert snapshot.expense_total == Decimal("150.00")
    assert snapshot.forecast.history_days == 8
    assert snapshot.forecast.daily_forecast[date(2026, 4, 17)] == Decimal("40.00")
    assert snapshot.forecast.daily_forecast[date(2026, 4, 24)] == Decimal("40.00")
    assert snapshot.forecast.forecast == Decimal("180.00")
    assert snapshot.forecast.daily_rate == Decimal("4.00")


def test_year_boundary_and_user_scope_include_only_shared_expenses(db, make_user):
    owner = make_user("finance-owner")
    viewer = make_user("finance-viewer")
    owner.expense_period_start_day = 15
    shared_list, shared_category, _transport, _empty = create_expense_list(db, owner, "Общий")
    private_list, private_category, _transport_private, _empty_private = create_expense_list(db, owner, "Личный")
    db.add(ExpenseListShare(expense_list_id=shared_list.id, user_id=viewer.id, can_edit=False))
    add_expense(db, shared_category, "Общая трата", "100", date(2026, 1, 5))
    add_expense(db, private_category, "Личная трата", "200", date(2026, 1, 6))
    db.add_all(
        [
            IncomeItem(
                owner_id=owner.id,
                title="Доход владельца",
                amount=Decimal("900"),
                received_at=msk_date_to_utc_naive(date(2026, 1, 4)),
            ),
            ExpenseLimit(owner_id=owner.id, category_name="Еда", monthly_limit=Decimal("1000")),
            RecurringExpense(
                owner_id=owner.id,
                expense_list_id=shared_list.id,
                category_name="Дом",
                title="Платёж владельца",
                amount=Decimal("50"),
                day_of_month=12,
            ),
        ]
    )
    db.commit()

    assert expense_period_bounds(date(2026, 1, 10), 15) == (date(2025, 12, 15), date(2026, 1, 14))
    assert previous_expense_period_bounds(date(2025, 12, 15), 15) == (
        date(2025, 11, 15),
        date(2025, 12, 14),
    )
    owner_snapshot = build_finance_snapshot(db, owner, today=date(2026, 1, 10))
    viewer_snapshot = build_finance_snapshot(db, viewer, today=date(2026, 1, 10))

    assert owner_snapshot.expense_total == Decimal("300.00")
    assert owner_snapshot.income_total == Decimal("900.00")
    assert len(owner_snapshot.recurring_payments) == 1
    assert viewer_snapshot.expense_total == Decimal("100.00")
    assert viewer_snapshot.income_total == Decimal("0.00")
    assert viewer_snapshot.budget.limit_total == Decimal("0.00")
    assert viewer_snapshot.recurring_payments == ()
    assert viewer_snapshot.comparison.comparable_end == date(2025, 12, 10)
    assert private_list.id != shared_list.id


def test_existing_finance_pages_render_snapshot_values(client, db, make_user, login, monkeypatch):
    user = make_user("finance-pages")
    _expense_list, food, _transport, _empty = create_expense_list(db, user)
    add_expense(db, food, "Products", "125.50", date(2026, 4, 4))
    db.add(
        IncomeItem(
            owner_id=user.id,
            title="Salary",
            amount=Decimal("500.00"),
            received_at=msk_date_to_utc_naive(date(2026, 4, 3)),
        )
    )
    db.commit()
    monkeypatch.setattr("app.main.msk_today", lambda: date(2026, 4, 10))
    login(user.username)

    finance = client.get("/finance?from_date=2026-04-01&to_date=2026-04-10")
    analytics = client.get("/expenses/analytics")

    assert finance.status_code == 200
    assert "+500 ₽" in finance.text
    assert "-125,5 ₽" in finance.text
    assert "+374,5 ₽" in finance.text
    assert analytics.status_code == 200
    assert "125,5 ₽" in analytics.text
