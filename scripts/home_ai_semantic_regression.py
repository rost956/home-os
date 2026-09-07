#!/usr/bin/env python3
"""Opt-in real-model regression for the 40 held-out Home AI semantic cases.

The runner builds an in-memory synthetic Home OS database. It never opens the
production database and never confirms a pending action.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.ai.client import AIClient, LlamaCppClient  # noqa: E402
from app.ai.config import get_ai_settings  # noqa: E402
from app.ai.errors import AIError  # noqa: E402
from app.ai.expenses import ExpenseDraftError, parse_expense_text, prepare_expense_draft  # noqa: E402
from app.ai.finance import analyze_finance_route, answer_finance_question  # noqa: E402
from app.ai.menu import (  # noqa: E402
    MenuProposalError,
    _candidate_arguments,
    create_menu_proposal,
    menu_planning_window,
)
from app.ai.recipes import analyze_recipe_text, answer_recipe_question, select_deterministic_recipe_tool  # noqa: E402
from app.ai.schemas import (  # noqa: E402
    AIAvailability,
    AICompletionRequest,
    AICompletionResponse,
    parse_structured_response,
)
from app.database import Base  # noqa: E402
from app.models import (  # noqa: E402
    AIAction,
    ExpenseCategory,
    ExpenseItem,
    ExpenseLimit,
    ExpenseList,
    IncomeItem,
    MenuItem,
    Recipe,
    User,
)
from app.timezone import msk_date_to_utc_naive  # noqa: E402
from scripts.home_ai_semantic_cases import HELD_OUT_SEMANTIC_CASES, SemanticEvaluationCase  # noqa: E402

SchemaT = TypeVar("SchemaT", bound=BaseModel)
REFERENCE_DAY = date(2026, 9, 7)


@dataclass
class ModelTrace:
    schema: str
    prompt_chars: int
    latency_seconds: float
    raw_output: str | None
    parsed_output: dict[str, Any] | None
    error: str | None


class TracingAIClient:
    def __init__(self, wrapped: LlamaCppClient) -> None:
        self.wrapped = wrapped
        self.traces: list[ModelTrace] = []
        self.attempts = 0

    async def complete(self, request: AICompletionRequest) -> AICompletionResponse:
        return await self.wrapped.complete(request)

    async def complete_json(self, request: AICompletionRequest, schema: type[SchemaT]) -> SchemaT:
        started = time.perf_counter()
        self.attempts += 1
        response: AICompletionResponse | None = None
        try:
            response = await self.wrapped.complete(request.model_copy(update={"output_mode": "json"}))
            parsed = parse_structured_response(response.content, schema)
        except AIError as exc:
            self.traces.append(
                ModelTrace(
                    schema=schema.__name__,
                    prompt_chars=sum(len(message.content) for message in request.messages),
                    latency_seconds=round(time.perf_counter() - started, 3),
                    raw_output=response.content if response else None,
                    parsed_output=None,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            raise
        self.traces.append(
            ModelTrace(
                schema=schema.__name__,
                prompt_chars=sum(len(message.content) for message in request.messages),
                latency_seconds=round(time.perf_counter() - started, 3),
                raw_output=response.content,
                parsed_output=parsed.model_dump(mode="json"),
                error=None,
            )
        )
        return parsed

    async def probe(self) -> AIAvailability:
        return await self.wrapped.probe()


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _seed_synthetic_db(db: Session) -> User:
    user = User(username="semantic-eval", password_hash="not-used")
    db.add(user)
    db.flush()
    expense_list = ExpenseList(owner_id=user.id, title="Синтетический бюджет")
    db.add(expense_list)
    db.flush()
    categories = [
        ExpenseCategory(expense_list_id=expense_list.id, name=name)
        for name in ("Еда", "Транспорт", "Дом", "Связь", "Развлечения", "Подарки")
    ]
    db.add_all(categories)
    db.flush()
    for category, title, amount, day in (
        (categories[0], "Продукты", "1200", date(2025, 3, 5)),
        (categories[1], "Такси", "700", date(2025, 3, 12)),
        (categories[0], "Продукты", "900", date(2026, 8, 10)),
        (categories[1], "Бензин", "1800", date(2026, 9, 3)),
        (categories[0], "Рынок", "1100", date(2026, 9, 5)),
    ):
        db.add(ExpenseItem(category_id=category.id, title=title, amount=Decimal(amount), created_at=msk_date_to_utc_naive(day)))
    db.add(IncomeItem(owner_id=user.id, title="Зарплата", amount=Decimal("10000"), received_at=msk_date_to_utc_naive(date(2026, 9, 1))))
    db.add_all(
        [
            ExpenseLimit(owner_id=user.id, category_name="Еда", monthly_limit=Decimal("3000")),
            ExpenseLimit(owner_id=user.id, category_name="Транспорт", monthly_limit=Decimal("2000")),
        ]
    )
    recipes = [
        Recipe(id=12, owner_id=user.id, title="Суп без лука", ingredients="картофель, морковь, зелень", cook_time_minutes=35, cost=Decimal("300"), servings=Decimal("4"), tags="суп, домашнее", steps="Сварить."),
        Recipe(owner_id=user.id, title="Овощное рагу", ingredients="картошка, грибы, овощи", cook_time_minutes=25, cost=Decimal("350"), servings=Decimal("4"), tags="веганское, домашнее", steps="Потушить."),
        Recipe(owner_id=user.id, title="Нут с овощами", ingredients="нут, овощи", cook_time_minutes=20, cost=Decimal("250"), servings=Decimal("6"), tags="постное, веганское", steps="Смешать."),
        Recipe(owner_id=user.id, title="Острый завтрак", ingredients="яйцо, овощи, перец", cook_time_minutes=15, cost=Decimal("200"), servings=Decimal("2"), tags="завтрак, острое", steps="Приготовить."),
        Recipe(owner_id=user.id, title="Куриный ужин", ingredients="курица, рис", cook_time_minutes=30, cost=Decimal("400"), servings=Decimal("4"), tags="ужин", steps="Приготовить."),
        Recipe(owner_id=user.id, title="Рыбный ужин", ingredients="рыба, картофель", cook_time_minutes=25, cost=Decimal("450"), servings=Decimal("4"), tags="ужин", steps="Приготовить."),
    ]
    db.add_all(recipes)
    db.flush()
    db.add(MenuItem(owner_id=user.id, recipe_id=recipes[1].id, plan_date=datetime(2026, 9, 1), meal_name="Ужин"))
    db.commit()
    return user


def _preprocess(case: SemanticEvaluationCase) -> dict[str, Any]:
    if case.domain == "expense":
        parsed = parse_expense_text(case.text, today=REFERENCE_DAY)
        return {
            "amount": str(parsed.amount),
            "expense_date": parsed.expense_date.isoformat(),
            "date_source": parsed.date_source,
            "remaining_text": parsed.remaining_text,
        }
    if case.domain == "finance":
        analysis = analyze_finance_route(case.text, today=REFERENCE_DAY)
        return {
            "deterministic_call": analysis.deterministic_call.model_dump(mode="json") if analysis.deterministic_call else None,
            "fixed_period_arguments": analysis.fixed_period_arguments,
            "matched_intents": [str(item) for item in analysis.matched_intents],
            "unresolved_text": analysis.unresolved_text,
            "needs_semantic_resolution": analysis.needs_semantic_resolution,
        }
    if case.domain == "recipe":
        return asdict(analyze_recipe_text(case.text))
    window = menu_planning_window(case.text, today=REFERENCE_DAY)
    return {
        "allowed_dates": [item.isoformat() for item in window.dates],
        "candidate_arguments": _candidate_arguments(case.text).model_dump(mode="json"),
        "unresolved_text": analyze_recipe_text(case.text).unresolved_text,
    }


def _recipe_call_after_semantic_pass(case: SemanticEvaluationCase, client: AIClient) -> dict[str, Any] | None:
    analysis = analyze_recipe_text(case.text)
    selector_trace = next(
        (trace for trace in getattr(client, "traces", []) if trace.schema == "RecipeToolCall"),
        None,
    )
    if selector_trace is not None and selector_trace.parsed_output is not None:
        selected = selector_trace.parsed_output
        return {
            "tool": selected["tool"],
            "arguments": {**selected.get("arguments", {}), **analysis.arguments},
        }
    deterministic = select_deterministic_recipe_tool(case.text)
    return deterministic.model_dump(mode="json") if deterministic else None


def _constraint_checks(arguments: dict[str, Any], case: SemanticEvaluationCase) -> dict[str, bool]:
    includes = [arguments.get("ingredient"), *arguments.get("include_ingredients", [])]
    excludes = [arguments.get("exclude_ingredient"), *arguments.get("exclude_ingredients", [])]
    checks: dict[str, bool] = {}
    if case.expected_includes:
        checks["includes"] = all(expected in includes for expected in case.expected_includes)
    if case.expected_excludes:
        checks["excludes"] = all(expected in excludes for expected in case.expected_excludes)
    if case.expected_max_time is not None:
        checks["max_time"] = arguments.get("max_cook_time") == case.expected_max_time
    if case.expected_max_cost is not None:
        checks["max_cost"] = str(arguments.get("max_cost")) in {
            case.expected_max_cost,
            f"{case.expected_max_cost}.0",
            f"{case.expected_max_cost}.00",
        }
    if case.expected_min_servings is not None:
        checks["min_servings"] = str(arguments.get("min_servings")) in {
            case.expected_min_servings,
            f"{case.expected_min_servings}.0",
            f"{case.expected_min_servings}.00",
        }
    if case.expected_sort is not None:
        checks["sort"] = arguments.get("sort_by") == case.expected_sort
    return checks


async def _execute_case(
    db: Session,
    user: User,
    client: AIClient,
    case: SemanticEvaluationCase,
) -> tuple[dict[str, Any], bool]:
    if case.domain == "expense":
        prepared = await prepare_expense_draft(
            db,
            user,
            client,
            text=case.text,
            expense_list_id=None,
            category_id=None,
            today=REFERENCE_DAY,
        )
        category_name = db.scalar(
            select(ExpenseCategory.name).where(ExpenseCategory.id == prepared.category_id)
        ) if prepared.category_id is not None else None
        final = {
            "amount": str(prepared.parsed.amount),
            "expense_date": prepared.parsed.expense_date.isoformat(),
            "title": prepared.parsed.title,
            "category_id": prepared.category_id,
            "category_name": category_name,
            "category_confident": prepared.category_confident,
        }
        passed = case.expected_date is None or final["expense_date"] == case.expected_date
        if case.expected_categories:
            passed = passed and category_name in case.expected_categories
        return final, passed
    if case.domain == "finance":
        response = await answer_finance_question(db, user, client, case.text, today=REFERENCE_DAY)
        final = {"tool": str(response.tool), "result": response.result.model_dump(mode="json")}
        return final, case.expected_tool is None or final["tool"] == case.expected_tool
    if case.domain == "recipe":
        response = await answer_recipe_question(db, user, client, case.text, today=REFERENCE_DAY)
        selected_call = _recipe_call_after_semantic_pass(case, client)
        argument_checks = _constraint_checks(selected_call["arguments"], case) if selected_call else {}
        final = {
            "tool": str(response.tool),
            "selected_call": selected_call,
            "recipe_ids": response.recipe_ids,
            "result": response.result.model_dump(mode="json"),
            "constraint_checks": argument_checks,
        }
        passed = (case.expected_tool is None or final["tool"] == case.expected_tool) and all(argument_checks.values())
        if case.expected_recipe_id is not None:
            passed = passed and response.result.data.found and response.result.data.recipe.recipe_id == case.expected_recipe_id
        return final, passed
    candidate_arguments = _candidate_arguments(case.text).model_dump(mode="json")
    constraint_checks = _constraint_checks(candidate_arguments, case)
    response = await create_menu_proposal(db, user, client, case.text, today=REFERENCE_DAY)
    final = {
        "allowed_days": (response.ends_on - response.starts_on).days + 1,
        "starts_on": response.starts_on.isoformat(),
        "ends_on": response.ends_on.isoformat(),
        "candidate_arguments": candidate_arguments,
        "constraint_checks": constraint_checks,
        "entries": [entry.model_dump(mode="json") for entry in response.entries],
    }
    actual_dates = {entry.plan_date for entry in response.entries}
    passed = (case.expected_days is None or len(actual_dates) == case.expected_days) and all(constraint_checks.values())
    if case.expected_entries is not None:
        passed = passed and len(response.entries) == case.expected_entries
    if case.expected_date is not None:
        passed = passed and actual_dates == {date.fromisoformat(case.expected_date)}
    return final, passed


async def _run_real(args: argparse.Namespace) -> int:
    settings = get_ai_settings()
    if not settings.enabled:
        raise RuntimeError("AI_ENABLED must be true; run this inside the configured production web container")
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    trace_client = TracingAIClient(LlamaCppClient(settings))
    reports: list[dict[str, Any]] = []
    with Session(engine) as db:
        user = _seed_synthetic_db(db)
        for case in HELD_OUT_SEMANTIC_CASES:
            trace_client.traces.clear()
            trace_client.attempts = 0
            started = time.perf_counter()
            report: dict[str, Any] = {"case": case.name, "domain": case.domain, "input": case.text}
            try:
                report["preprocessing"] = _preprocess(case)
                final, passed = await _execute_case(db, user, trace_client, case)
                report.update({"final_interpretation": final, "passed": passed})
            except (ExpenseDraftError, AIError, MenuProposalError, SQLAlchemyError, ValueError) as exc:
                safe = case.safe_rejection and isinstance(exc, ExpenseDraftError)
                report.update(
                    {
                        "preprocessing": report.get("preprocessing"),
                        "final_interpretation": {"safe_rejection": safe},
                        "passed": safe,
                        "error_reason": f"{type(exc).__name__}: {exc}",
                    }
                )
            report["llm_called"] = trace_client.attempts > 0
            report["llm_structured_output"] = [_jsonable(asdict(item)) for item in trace_client.traces]
            report["latency_seconds"] = round(time.perf_counter() - started, 3)
            reports.append(report)
            print(json.dumps(report, ensure_ascii=False, default=str))
            db.rollback()

        remaining_actions = db.scalar(select(func.count()).select_from(AIAction))
    passed_count = sum(bool(report["passed"]) for report in reports)
    summary = {
        "passed": passed_count,
        "total": len(reports),
        "median_latency_seconds": round(statistics.median(item["latency_seconds"] for item in reports), 3),
        "synthetic_database": True,
        "pending_actions_after_run": remaining_actions,
    }
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False))
    if args.output:
        args.output.write_text(json.dumps({"cases": reports, "summary": summary}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 0 if passed_count == len(reports) and remaining_actions == 0 else 1


def _run_mock() -> int:
    critical = {
        "Хочу три дня домашней еды без курицы",
        "Разложи расходы за март 2025 по направлениям",
        "Покажи рецепт номер 12",
        "Пятого числа купил корм коту за 1200",
        "Саша вернул 500 за такси",
    }
    preprocessable = 0
    safe_rejections = 0
    for case in HELD_OUT_SEMANTIC_CASES:
        try:
            _preprocess(case)
            preprocessable += 1
        except ExpenseDraftError:
            safe_rejections += int(case.safe_rejection)
    valid = len(HELD_OUT_SEMANTIC_CASES) == 40 and critical.issubset({case.text for case in HELD_OUT_SEMANTIC_CASES})
    print(
        "MOCK SUMMARY "
        + json.dumps(
            {
                "cases": len(HELD_OUT_SEMANTIC_CASES),
                "preprocessable": preprocessable,
                "expected_safe_rejections": safe_rejections,
                "real_model_called": False,
                "synthetic_database": True,
                "valid": valid,
            },
            ensure_ascii=False,
        )
    )
    return 0 if valid else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="Validate cases/preprocessing without DB or model calls")
    parser.add_argument("--output", type=Path, help="Optional JSON result path")
    args = parser.parse_args()
    return _run_mock() if args.mock else asyncio.run(_run_real(args))


if __name__ == "__main__":
    raise SystemExit(main())
