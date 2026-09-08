from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    ExpenseCategory,
    ExpenseLimit,
    ExpenseList,
    ExpenseListShare,
    IncomeItem,
    RecurringExpense,
    User,
)
from app.timezone import msk_date, today_msk

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class CashflowSummary:
    date_from: date
    date_to: date
    income_total: Decimal
    expense_total: Decimal
    balance: Decimal
    savings_rate: Decimal | None
    income_by_day: dict[date, Decimal]
    expense_by_day: dict[date, Decimal]


@dataclass(frozen=True)
class CategoryBreakdown:
    name: str
    current: Decimal
    previous: Decimal
    difference: Decimal


@dataclass(frozen=True)
class LargestExpense:
    id: int
    title: str
    amount: Decimal
    expense_date: date
    category: str
    expense_list: str


@dataclass(frozen=True)
class BudgetCategoryStatus:
    category: str
    limit: Decimal
    spent: Decimal
    left: Decimal
    percent: int


@dataclass(frozen=True)
class BudgetStatus:
    limit_total: Decimal
    spent: Decimal
    left: Decimal
    percent: int
    categories: tuple[BudgetCategoryStatus, ...]


@dataclass(frozen=True)
class RecurringPaymentSnapshot:
    id: int
    title: str
    amount: Decimal
    day_of_month: int
    category: str
    expense_list_id: int


@dataclass(frozen=True)
class ForecastSnapshot:
    daily_rate: Decimal
    forecast: Decimal
    confidence: str
    history_days: int
    daily_forecast: dict[date, Decimal]
    recurring_by_day: dict[date, Decimal]
    method: str

    def as_legacy_dict(self) -> dict[str, Any]:
        return {
            "daily_rate": self.daily_rate,
            "forecast": self.forecast,
            "confidence": self.confidence,
            "days": self.history_days,
            "daily_forecast": self.daily_forecast,
            "recurring_by_day": self.recurring_by_day,
            "method": self.method,
        }


@dataclass(frozen=True)
class PeriodComparison:
    previous_start: date
    previous_end: date
    comparable_end: date
    current_total: Decimal
    previous_total: Decimal
    difference: Decimal
    percent_change: Decimal | None


@dataclass(frozen=True)
class FinanceSnapshot:
    period_start: date
    period_end: date
    actual_end: date
    income_total: Decimal
    period_income_total: Decimal
    expense_total: Decimal
    period_expense_total: Decimal
    balance: Decimal
    savings_rate: Decimal | None
    previous_period_expense_total: Decimal
    period_difference: Decimal
    comparison: PeriodComparison
    categories: tuple[CategoryBreakdown, ...]
    largest_expenses: tuple[LargestExpense, ...]
    budget: BudgetStatus
    recurring_payments: tuple[RecurringPaymentSnapshot, ...]
    forecast: ForecastSnapshot
    forecast_balance: Decimal


def clamp_month_day(year: int, month: int, day: int) -> date:
    safe_day = max(1, min(31, int(day or 1)))
    return date(year, month, min(safe_day, calendar.monthrange(year, month)[1]))


def shifted_month(year: int, month: int, months: int) -> tuple[int, int]:
    month_index = (month - 1) + months
    return year + month_index // 12, month_index % 12 + 1


def expense_period_start_day(user: User | None) -> int:
    value = getattr(user, "expense_period_start_day", 1) or 1
    return max(1, min(31, int(value)))


def financial_period_bounds(day: date, start_day: int) -> tuple[date, date]:
    """Return the user's current financial period for a reference date."""
    current_start = clamp_month_day(day.year, day.month, start_day)
    if day < current_start:
        year, month = shifted_month(day.year, day.month, -1)
        current_start = clamp_month_day(year, month, start_day)
    next_year, next_month = shifted_month(current_start.year, current_start.month, 1)
    next_start = clamp_month_day(next_year, next_month, start_day)
    return current_start, next_start - timedelta(days=1)


def expense_period_bounds(day: date, start_day: int) -> tuple[date, date]:
    """Backward-compatible name for financial_period_bounds."""
    return financial_period_bounds(day, start_day)


def previous_expense_period_bounds(period_start: date, start_day: int) -> tuple[date, date]:
    year, month = shifted_month(period_start.year, period_start.month, -1)
    return clamp_month_day(year, month, start_day), period_start - timedelta(days=1)


def format_period_range(period_start: date, period_end: date) -> str:
    if period_start.year == period_end.year:
        return f"{period_start.strftime('%d.%m')}–{period_end.strftime('%d.%m.%Y')}"
    return f"{period_start.strftime('%d.%m.%Y')}–{period_end.strftime('%d.%m.%Y')}"


def accessible_expense_lists(db: Session, user: User) -> list[ExpenseList]:
    owned = db.scalars(
        select(ExpenseList)
        .options(
            selectinload(ExpenseList.owner),
            selectinload(ExpenseList.categories).selectinload(ExpenseCategory.items),
            selectinload(ExpenseList.shares),
        )
        .where(ExpenseList.owner_id == user.id)
        .order_by(ExpenseList.title)
    ).all()
    shared = db.scalars(
        select(ExpenseList)
        .join(ExpenseListShare)
        .options(
            selectinload(ExpenseList.owner),
            selectinload(ExpenseList.categories).selectinload(ExpenseCategory.items),
        )
        .where(ExpenseListShare.user_id == user.id)
        .order_by(ExpenseList.title)
    ).all()
    return list(owned) + list(shared)


def active_recurring_for_lists(
    db: Session,
    user: User,
    expense_lists: list[ExpenseList],
) -> list[RecurringExpense]:
    list_ids = [expense_list.id for expense_list in expense_lists]
    if not list_ids:
        return []
    return db.scalars(
        select(RecurringExpense).where(
            RecurringExpense.owner_id == user.id,
            RecurringExpense.is_active.is_(True),
            RecurringExpense.expense_list_id.in_(list_ids),
        )
    ).all()


def expense_forecast_from_lists(
    expense_lists: list[ExpenseList],
    period_start: date,
    period_end: date,
    recurring: list[RecurringExpense] | None = None,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Existing weekday/category forecast, made deterministic for callers and tests."""
    current_day = today or today_msk()
    daily: dict[date, Decimal] = defaultdict(lambda: ZERO)
    category_daily: dict[str, dict[date, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    for expense_list in expense_lists:
        for category in expense_list.categories:
            for item in category.items:
                if not item.include_in_analytics or not item.include_in_forecast:
                    continue
                item_day = msk_date(item.created_at)
                if item_day is None or item_day < current_day - timedelta(days=364):
                    continue
                daily[item_day] += item.amount
                category_daily[category.name][item_day] += item.amount

    recurring = recurring or []
    recurring_by_day: dict[date, Decimal] = defaultdict(lambda: ZERO)
    future_start = max(current_day + timedelta(days=1), period_start)
    for recurring_item in recurring:
        cursor = date(future_start.year, future_start.month, 1)
        while cursor <= period_end:
            due_date = clamp_month_day(cursor.year, cursor.month, recurring_item.day_of_month)
            if future_start <= due_date <= period_end:
                recurring_by_day[due_date] += recurring_item.amount
            next_year, next_month = shifted_month(cursor.year, cursor.month, 1)
            cursor = date(next_year, next_month, 1)

    if daily:
        first_day = min(daily)
        history_days = (current_day - first_day).days + 1
    else:
        history_days = 0

    def robust_weekday_rate(values: list[Decimal]) -> Decimal:
        if not values:
            return ZERO
        positive = sorted(value for value in values if value > 0)
        cap = positive[min(len(positive) - 1, max(0, int(len(positive) * 0.9) - 1))] if positive else ZERO
        weighted_total = ZERO
        weights = ZERO
        for index, value in enumerate(values):
            weight = Decimal("1") + Decimal("2") * Decimal(index + 1) / Decimal(len(values))
            weighted_total += min(value, cap) * weight
            weights += weight
        return (weighted_total / weights).quantize(Decimal("0.01")) if weights else ZERO

    weekday_rates: dict[str, dict[int, Decimal]] = {}
    if history_days:
        for category_name, amounts in category_daily.items():
            category_first_day = min(amounts)
            category_history_days = (current_day - category_first_day).days + 1
            category_dates = [category_first_day + timedelta(days=offset) for offset in range(category_history_days)]
            weekday_rates[category_name] = {
                weekday: robust_weekday_rate([amounts[day] for day in category_dates if day.weekday() == weekday])
                for weekday in range(7)
            }

    period_days = max(1, (period_end - period_start).days + 1)
    recurring_daily_share = sum((item.amount for item in recurring), ZERO) / Decimal(period_days)
    daily_forecast: dict[date, Decimal] = {}
    for offset in range(max(0, (period_end - future_start).days + 1)):
        forecast_day = future_start + timedelta(days=offset)
        baseline = sum((rates[forecast_day.weekday()] for rates in weekday_rates.values()), ZERO)
        baseline = max(ZERO, baseline - recurring_daily_share)
        daily_forecast[forecast_day] = (baseline + recurring_by_day[forecast_day]).quantize(Decimal("0.01"))

    actual = sum(
        (daily[day] for day in daily if period_start <= day <= min(current_day, period_end)),
        ZERO,
    )
    future_total = sum(daily_forecast.values(), ZERO)
    forecast = (actual + future_total).quantize(Decimal("0.01"))
    daily_rate = (future_total / Decimal(len(daily_forecast))).quantize(Decimal("0.01")) if daily_forecast else ZERO
    confidence = "низкая" if history_days < 21 else "средняя" if history_days < 75 else "высокая"
    return {
        "daily_rate": daily_rate,
        "forecast": forecast,
        "confidence": confidence,
        "days": history_days,
        "daily_forecast": daily_forecast,
        "recurring_by_day": recurring_by_day,
        "method": "категории, дни недели и регулярные платежи",
    }


def summarize_cashflow(
    expense_lists: list[ExpenseList],
    incomes: list[IncomeItem],
    *,
    date_from: date,
    date_to: date,
    today: date,
) -> CashflowSummary:
    income_by_day: dict[date, Decimal] = defaultdict(lambda: ZERO)
    expense_by_day: dict[date, Decimal] = defaultdict(lambda: ZERO)
    for item in incomes:
        item_day = msk_date(item.received_at)
        if item_day is not None and date_from <= item_day <= min(date_to, today):
            income_by_day[item_day] += item.amount
    for expense_list in expense_lists:
        for category in expense_list.categories:
            for item in category.items:
                item_day = msk_date(item.created_at)
                if item.include_in_analytics and item_day is not None and date_from <= item_day <= date_to:
                    expense_by_day[item_day] += item.amount
    income_total = sum(income_by_day.values(), ZERO)
    expense_total = sum(expense_by_day.values(), ZERO)
    balance = income_total - expense_total
    savings_rate = (balance / income_total * Decimal("100")).quantize(Decimal("0.1")) if income_total else None
    return CashflowSummary(
        date_from=date_from,
        date_to=date_to,
        income_total=income_total,
        expense_total=expense_total,
        balance=balance,
        savings_rate=savings_rate,
        income_by_day=dict(income_by_day),
        expense_by_day=dict(expense_by_day),
    )


def _budget_status(limits: list[ExpenseLimit], spent_by_category: dict[str, Decimal]) -> BudgetStatus:
    rows: list[BudgetCategoryStatus] = []
    for limit in limits:
        spent = spent_by_category.get(limit.category_name.strip().casefold(), ZERO)
        percent = int((spent / limit.monthly_limit) * 100) if limit.monthly_limit else 0
        rows.append(
            BudgetCategoryStatus(
                category=limit.category_name,
                limit=limit.monthly_limit,
                spent=spent,
                left=limit.monthly_limit - spent,
                percent=min(percent, 160),
            )
        )
    limit_total = sum((item.limit for item in rows), ZERO)
    spent = sum((item.spent for item in rows), ZERO)
    percent = int((spent / limit_total) * 100) if limit_total else 0
    return BudgetStatus(
        limit_total=limit_total,
        spent=spent,
        left=limit_total - spent,
        percent=min(percent, 160),
        categories=tuple(rows),
    )


def build_finance_snapshot(
    db: Session,
    user: User,
    *,
    today: date | None = None,
    expense_list_ids: set[int] | None = None,
    largest_limit: int = 7,
) -> FinanceSnapshot:
    current_day = today or today_msk()
    start_day = expense_period_start_day(user)
    period_start, period_end = financial_period_bounds(current_day, start_day)
    actual_end = min(current_day, period_end)
    previous_start, previous_end = previous_expense_period_bounds(period_start, start_day)
    elapsed_days = max(1, (actual_end - period_start).days + 1)
    comparable_end = min(previous_end, previous_start + timedelta(days=elapsed_days - 1))
    lists = accessible_expense_lists(db, user)
    if expense_list_ids is not None:
        lists = [item for item in lists if item.id in expense_list_ids]
    incomes = db.scalars(select(IncomeItem).where(IncomeItem.owner_id == user.id)).all()
    limits = db.scalars(
        select(ExpenseLimit).where(ExpenseLimit.owner_id == user.id).order_by(ExpenseLimit.category_name)
    ).all()
    recurring = active_recurring_for_lists(db, user, lists)

    category_names: dict[str, str] = {}
    current_by_category: dict[str, Decimal] = defaultdict(lambda: ZERO)
    previous_by_category: dict[str, Decimal] = defaultdict(lambda: ZERO)
    period_by_category: dict[str, Decimal] = defaultdict(lambda: ZERO)
    expense_total = ZERO
    period_expense_total = ZERO
    previous_period_total = ZERO
    comparison_previous_total = ZERO
    largest: list[LargestExpense] = []
    for expense_list in lists:
        for category in expense_list.categories:
            category_key = category.name.strip().casefold()
            category_names.setdefault(category_key, category.name)
            for item in category.items:
                if not item.include_in_analytics:
                    continue
                item_day = msk_date(item.created_at)
                if item_day is None:
                    continue
                if period_start <= item_day <= period_end:
                    period_expense_total += item.amount
                    period_by_category[category_key] += item.amount
                if period_start <= item_day <= actual_end:
                    expense_total += item.amount
                    current_by_category[category_key] += item.amount
                    largest.append(
                        LargestExpense(
                            id=item.id,
                            title=item.title,
                            amount=item.amount,
                            expense_date=item_day,
                            category=category.name,
                            expense_list=expense_list.title,
                        )
                    )
                if previous_start <= item_day <= previous_end:
                    previous_period_total += item.amount
                if previous_start <= item_day <= comparable_end:
                    comparison_previous_total += item.amount
                    previous_by_category[category_key] += item.amount

    income_total = ZERO
    period_income_total = ZERO
    for item in incomes:
        item_day = msk_date(item.received_at)
        if item_day is None:
            continue
        if period_start <= item_day <= period_end:
            period_income_total += item.amount
        if period_start <= item_day <= actual_end:
            income_total += item.amount
    balance = income_total - expense_total
    savings_rate = (balance / income_total * Decimal("100")).quantize(Decimal("0.1")) if income_total else None

    category_rows = tuple(
        CategoryBreakdown(
            name=category_names[key],
            current=current_by_category[key],
            previous=previous_by_category[key],
            difference=current_by_category[key] - previous_by_category[key],
        )
        for key in sorted(
            category_names,
            key=lambda item: (current_by_category[item] - previous_by_category[item], current_by_category[item]),
            reverse=True,
        )
    )
    largest.sort(key=lambda item: (item.amount, item.expense_date, item.id), reverse=True)
    forecast_data = expense_forecast_from_lists(
        lists,
        period_start,
        period_end,
        recurring,
        today=current_day,
    )
    forecast = ForecastSnapshot(
        daily_rate=forecast_data["daily_rate"],
        forecast=forecast_data["forecast"],
        confidence=forecast_data["confidence"],
        history_days=forecast_data["days"],
        daily_forecast=dict(forecast_data["daily_forecast"]),
        recurring_by_day=dict(forecast_data["recurring_by_day"]),
        method=forecast_data["method"],
    )
    comparison_difference = expense_total - comparison_previous_total
    comparison_percent = (
        (comparison_difference / comparison_previous_total * Decimal("100")).quantize(Decimal("0.1"))
        if comparison_previous_total
        else None
    )
    return FinanceSnapshot(
        period_start=period_start,
        period_end=period_end,
        actual_end=actual_end,
        income_total=income_total,
        period_income_total=period_income_total,
        expense_total=expense_total,
        period_expense_total=period_expense_total,
        balance=balance,
        savings_rate=savings_rate,
        previous_period_expense_total=previous_period_total,
        period_difference=period_expense_total - previous_period_total,
        comparison=PeriodComparison(
            previous_start=previous_start,
            previous_end=previous_end,
            comparable_end=comparable_end,
            current_total=expense_total,
            previous_total=comparison_previous_total,
            difference=comparison_difference,
            percent_change=comparison_percent,
        ),
        categories=category_rows,
        largest_expenses=tuple(largest[: max(0, largest_limit)]),
        budget=_budget_status(limits, period_by_category),
        recurring_payments=tuple(
            RecurringPaymentSnapshot(
                id=item.id,
                title=item.title,
                amount=item.amount,
                day_of_month=item.day_of_month,
                category=item.category_name,
                expense_list_id=item.expense_list_id,
            )
            for item in sorted(recurring, key=lambda entry: (entry.day_of_month, entry.id))
        ),
        forecast=forecast,
        forecast_balance=period_income_total - forecast.forecast,
    )
