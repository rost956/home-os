from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.orm import Session

from app.models import User
from app.services.finance import (
    FinanceSnapshot,
    build_finance_snapshot,
    clamp_month_day,
    expense_period_bounds,
    expense_period_start_day,
    previous_expense_period_bounds,
)

from ..errors import AIResponseValidationError


class FinanceToolName(StrEnum):
    SUMMARY = "get_finance_summary"
    COMPARE_PERIODS = "compare_finance_periods"
    CATEGORY_BREAKDOWN = "get_category_breakdown"
    LARGEST_EXPENSES = "get_largest_expenses"
    BUDGET_STATUS = "get_budget_status"
    FORECAST = "get_finance_forecast"


class FinanceToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: FinanceToolName
    arguments: dict[str, Any] = Field(default_factory=dict)


class FinancePeriodArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: Literal["current", "previous", "month"] = "current"
    year: int | None = Field(default=None, ge=2000, le=2100)
    month: int | None = Field(default=None, ge=1, le=12)

    @model_validator(mode="after")
    def validate_named_month(self) -> FinancePeriodArguments:
        if self.period == "month" and (self.year is None or self.month is None):
            raise ValueError("year and month are required for a named month")
        if self.period != "month" and (self.year is not None or self.month is not None):
            raise ValueError("year and month are only valid with period=month")
        return self


class CategoryBreakdownArguments(FinancePeriodArguments):
    limit: int = Field(default=10, ge=1, le=10)


class LargestExpensesArguments(FinancePeriodArguments):
    limit: int = Field(default=5, ge=1, le=5)


class NoFinanceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FinancePeriod(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date
    end: date
    actual_end: date


class FinanceSummaryData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: FinancePeriod
    income: Decimal
    expenses: Decimal
    balance: Decimal
    savings_rate_percent: Decimal | None


class CategoryChangeData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(max_length=200)
    current: Decimal
    previous: Decimal
    difference: Decimal


class FinanceComparisonData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: FinancePeriod
    previous_start: date
    previous_end: date
    current_expenses: Decimal
    previous_expenses: Decimal
    difference: Decimal
    percent_change: Decimal | None
    direction: Literal["higher", "lower", "unchanged"]
    strongest_category_changes: list[CategoryChangeData] = Field(max_length=5)


class CategoryBreakdownData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: FinancePeriod
    categories: list[CategoryChangeData] = Field(max_length=10)


class LargestExpenseData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=300)
    amount: Decimal
    expense_date: date
    category: str = Field(max_length=200)
    expense_list: str = Field(max_length=200)


class LargestExpensesData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: FinancePeriod
    expenses: list[LargestExpenseData] = Field(max_length=5)


class BudgetCategoryData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(max_length=200)
    limit: Decimal
    spent: Decimal
    left: Decimal
    percent: int = Field(ge=0, le=160)
    state: Literal["ok", "almost_exhausted", "exceeded"]


class BudgetStatusData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: FinancePeriod
    limit_total: Decimal
    spent: Decimal
    left: Decimal
    percent: int = Field(ge=0, le=160)
    categories: list[BudgetCategoryData] = Field(max_length=20)


class FinanceForecastData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: FinancePeriod
    available: bool
    unavailable_reason: Literal["insufficient_history"] | None = None
    expected_expenses: Decimal | None
    expected_balance: Decimal | None
    daily_rate: Decimal | None
    confidence: Literal["низкая", "средняя", "высокая"] | None
    history_days: int = Field(ge=0)
    method: str = Field(max_length=200)


class FinanceSummaryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal[FinanceToolName.SUMMARY]
    data: FinanceSummaryData


class FinanceComparisonResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal[FinanceToolName.COMPARE_PERIODS]
    data: FinanceComparisonData


class CategoryBreakdownResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal[FinanceToolName.CATEGORY_BREAKDOWN]
    data: CategoryBreakdownData


class LargestExpensesResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal[FinanceToolName.LARGEST_EXPENSES]
    data: LargestExpensesData


class BudgetStatusResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal[FinanceToolName.BUDGET_STATUS]
    data: BudgetStatusData


class FinanceForecastResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal[FinanceToolName.FORECAST]
    data: FinanceForecastData


FinanceToolResult = Annotated[
    FinanceSummaryResult
    | FinanceComparisonResult
    | CategoryBreakdownResult
    | LargestExpensesResult
    | BudgetStatusResult
    | FinanceForecastResult,
    Field(discriminator="tool"),
]


FINANCE_ARGUMENT_SCHEMAS: dict[FinanceToolName, type[BaseModel]] = {
    FinanceToolName.SUMMARY: FinancePeriodArguments,
    FinanceToolName.COMPARE_PERIODS: FinancePeriodArguments,
    FinanceToolName.CATEGORY_BREAKDOWN: CategoryBreakdownArguments,
    FinanceToolName.LARGEST_EXPENSES: LargestExpensesArguments,
    FinanceToolName.BUDGET_STATUS: NoFinanceArguments,
    FinanceToolName.FORECAST: NoFinanceArguments,
}


def finance_tool_descriptions() -> tuple[dict[str, str], ...]:
    return (
        {"name": FinanceToolName.SUMMARY, "purpose": "Доходы, расходы и баланс за период"},
        {"name": FinanceToolName.COMPARE_PERIODS, "purpose": "Сравнение расходов и причин изменения"},
        {"name": FinanceToolName.CATEGORY_BREAKDOWN, "purpose": "Расходы и изменения по категориям"},
        {"name": FinanceToolName.LARGEST_EXPENSES, "purpose": "Несколько крупнейших отдельных расходов"},
        {"name": FinanceToolName.BUDGET_STATUS, "purpose": "Состояние месячных лимитов"},
        {"name": FinanceToolName.FORECAST, "purpose": "Прогноз расходов до конца текущего периода"},
    )


def _validated_arguments(call: FinanceToolCall) -> BaseModel:
    schema = FINANCE_ARGUMENT_SCHEMAS.get(call.tool)
    if schema is None:
        raise AIResponseValidationError("AI selected an unknown finance tool")
    try:
        return schema.model_validate(call.arguments)
    except ValidationError as exc:
        raise AIResponseValidationError("AI returned invalid finance tool arguments") from exc


def _snapshot_for_period(
    db: Session,
    user: User,
    arguments: FinancePeriodArguments,
    *,
    today: date,
) -> FinanceSnapshot:
    start_day = expense_period_start_day(user)
    current_start, _current_end = expense_period_bounds(today, start_day)
    if arguments.period == "current":
        snapshot_day = today
    elif arguments.period == "previous":
        _previous_start, snapshot_day = previous_expense_period_bounds(current_start, start_day)
    else:
        assert arguments.year is not None and arguments.month is not None
        requested_start = clamp_month_day(arguments.year, arguments.month, start_day)
        _period_start, requested_end = expense_period_bounds(requested_start, start_day)
        if requested_start > today:
            raise AIResponseValidationError("A future finance period is not available")
        snapshot_day = today if requested_start <= today <= requested_end else requested_end
    return build_finance_snapshot(db, user, today=snapshot_day)


def _period(snapshot: FinanceSnapshot) -> FinancePeriod:
    return FinancePeriod(start=snapshot.period_start, end=snapshot.period_end, actual_end=snapshot.actual_end)


def _category_change_rows(snapshot: FinanceSnapshot, limit: int) -> list[CategoryChangeData]:
    return [
        CategoryChangeData(
            category=item.name,
            current=item.current,
            previous=item.previous,
            difference=item.difference,
        )
        for item in snapshot.categories[:limit]
    ]


def execute_finance_tool(
    db: Session,
    user: User,
    call: FinanceToolCall,
    *,
    today: date,
) -> FinanceToolResult:
    """Execute one server-owned read tool for the authenticated actor."""
    arguments = _validated_arguments(call)

    if call.tool == FinanceToolName.BUDGET_STATUS:
        snapshot = build_finance_snapshot(db, user, today=today)
        categories = []
        for item in sorted(snapshot.budget.categories, key=lambda row: row.percent, reverse=True)[:20]:
            state = "exceeded" if item.percent >= 100 else "almost_exhausted" if item.percent >= 80 else "ok"
            categories.append(
                BudgetCategoryData(
                    category=item.category,
                    limit=item.limit,
                    spent=item.spent,
                    left=item.left,
                    percent=item.percent,
                    state=state,
                )
            )
        return BudgetStatusResult(
            tool=FinanceToolName.BUDGET_STATUS,
            data=BudgetStatusData(
                period=_period(snapshot),
                limit_total=snapshot.budget.limit_total,
                spent=snapshot.budget.spent,
                left=snapshot.budget.left,
                percent=snapshot.budget.percent,
                categories=categories,
            ),
        )

    if call.tool == FinanceToolName.FORECAST:
        snapshot = build_finance_snapshot(db, user, today=today)
        has_scheduled_payment = any(amount > 0 for amount in snapshot.forecast.recurring_by_day.values())
        available = snapshot.forecast.history_days > 0 or has_scheduled_payment
        return FinanceForecastResult(
            tool=FinanceToolName.FORECAST,
            data=FinanceForecastData(
                period=_period(snapshot),
                available=available,
                unavailable_reason=None if available else "insufficient_history",
                expected_expenses=snapshot.forecast.forecast if available else None,
                expected_balance=snapshot.forecast_balance if available else None,
                daily_rate=snapshot.forecast.daily_rate if available else None,
                confidence=snapshot.forecast.confidence if available else None,
                history_days=snapshot.forecast.history_days,
                method=snapshot.forecast.method,
            ),
        )

    if not isinstance(arguments, FinancePeriodArguments):
        raise AIResponseValidationError("Finance tool arguments do not match the selected tool")
    snapshot = _snapshot_for_period(db, user, arguments, today=today)

    if call.tool == FinanceToolName.SUMMARY:
        return FinanceSummaryResult(
            tool=FinanceToolName.SUMMARY,
            data=FinanceSummaryData(
                period=_period(snapshot),
                income=snapshot.income_total,
                expenses=snapshot.expense_total,
                balance=snapshot.balance,
                savings_rate_percent=snapshot.savings_rate,
            ),
        )
    if call.tool == FinanceToolName.COMPARE_PERIODS:
        difference = snapshot.comparison.difference
        direction = "higher" if difference > 0 else "lower" if difference < 0 else "unchanged"
        return FinanceComparisonResult(
            tool=FinanceToolName.COMPARE_PERIODS,
            data=FinanceComparisonData(
                period=_period(snapshot),
                previous_start=snapshot.comparison.previous_start,
                previous_end=snapshot.comparison.comparable_end,
                current_expenses=snapshot.comparison.current_total,
                previous_expenses=snapshot.comparison.previous_total,
                difference=difference,
                percent_change=snapshot.comparison.percent_change,
                direction=direction,
                strongest_category_changes=_category_change_rows(snapshot, 5),
            ),
        )
    if call.tool == FinanceToolName.CATEGORY_BREAKDOWN:
        assert isinstance(arguments, CategoryBreakdownArguments)
        return CategoryBreakdownResult(
            tool=FinanceToolName.CATEGORY_BREAKDOWN,
            data=CategoryBreakdownData(
                period=_period(snapshot),
                categories=_category_change_rows(snapshot, arguments.limit),
            ),
        )
    if call.tool == FinanceToolName.LARGEST_EXPENSES:
        assert isinstance(arguments, LargestExpensesArguments)
        return LargestExpensesResult(
            tool=FinanceToolName.LARGEST_EXPENSES,
            data=LargestExpensesData(
                period=_period(snapshot),
                expenses=[
                    LargestExpenseData(
                        title=item.title,
                        amount=item.amount,
                        expense_date=item.expense_date,
                        category=item.category,
                        expense_list=item.expense_list,
                    )
                    for item in snapshot.largest_expenses[: arguments.limit]
                ],
            ),
        )
    raise AIResponseValidationError("AI selected an unknown finance tool")
