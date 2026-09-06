#!/usr/bin/env python3
"""Read-only held-out evaluation of the production natural-language expense path."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from home_ai_expense_cases import HELD_OUT_EXPENSE_CASES, ExpenseEvaluationCase  # noqa: E402

from app.ai.client import LlamaCppClient  # noqa: E402
from app.ai.config import get_ai_settings  # noqa: E402
from app.ai.errors import AIError  # noqa: E402
from app.ai.expenses import (  # noqa: E402
    ExpenseDraftError,
    ExpenseMultipleExpensesError,
    ExpenseTextParseError,
    normalize_merchant_key,
    parse_expense_text,
    prepare_expense_draft,
)
from app.database import SessionLocal  # noqa: E402
from app.models import AIAction, User  # noqa: E402
from app.services.expenses import writable_expense_category_choices  # noqa: E402
from app.timezone import today_msk  # noqa: E402


def _meaning_preserved(title: str, expected_terms: tuple[str, ...]) -> bool:
    actual_terms = normalize_merchant_key(title).split()
    return all(any(term in actual or actual in term for actual in actual_terms) for term in expected_terms)


def _expected_day(case: ExpenseEvaluationCase, reference_day: date) -> date:
    return date.fromisoformat(case.explicit_date) if case.explicit_date else reference_day + timedelta(days=case.date_delta)


def _error_is_safe(case: ExpenseEvaluationCase, error: ExpenseTextParseError) -> bool:
    if case.outcome == "multiple_amounts":
        return isinstance(error, ExpenseMultipleExpensesError)
    return case.outcome in {"missing_amount", "invalid_amount", "invalid_text"}


async def _run(args: argparse.Namespace) -> int:
    settings = get_ai_settings()
    if not settings.enabled:
        raise RuntimeError("AI_ENABLED must be true inside the production web container")
    client = LlamaCppClient(settings)
    reference_day = date.fromisoformat(args.reference_date) if args.reference_date else today_msk()
    cases = HELD_OUT_EXPENSE_CASES[: args.limit] if args.limit else HELD_OUT_EXPENSE_CASES
    reports: list[dict[str, Any]] = []
    deterministic_only = 0
    llm_assisted = 0
    safely_invalid_or_ambiguous = 0

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == args.username))
        if user is None:
            raise RuntimeError(f"User not found: {args.username}")
        allowed_ids = {
            item.id
            for item in writable_expense_category_choices(db, user)
            if args.expense_list_id is None or item.expense_list_id == args.expense_list_id
        }
        before_actions = db.scalar(select(func.count()).select_from(AIAction).where(AIAction.owner_id == user.id))

        for case in cases:
            started = time.perf_counter()
            report: dict[str, Any] = {"case": case.name, "input": case.text}
            deterministic = None
            try:
                deterministic = parse_expense_text(case.text, today=reference_day)
                prepared = await prepare_expense_draft(
                    db,
                    user,
                    client,
                    text=case.text,
                    expense_list_id=args.expense_list_id,
                    category_id=None,
                    today=reference_day,
                )
                parsed = prepared.parsed
                checks = {
                    "expected_valid": case.outcome == "valid",
                    "amount": case.amount == parsed.amount,
                    "date": parsed.expense_date == _expected_day(case, reference_day),
                    "date_default": parsed.date_was_defaulted == case.date_was_defaulted,
                    "meaning": _meaning_preserved(parsed.title, case.meaning_terms),
                    "category_safe": prepared.category_id is None or prepared.category_id in allowed_ids,
                }
                case_passed = all(checks.values())
                report.update(
                    {
                        "deterministic_facts": {
                            "amount": str(parsed.amount),
                            "expense_date": parsed.expense_date.isoformat(),
                            "date_source": parsed.date_source,
                            "remaining_text": parsed.remaining_text,
                        },
                        "llm_required": prepared.llm_required,
                        "llm_prompt_chars": prepared.llm_prompt_chars,
                        "parsed_draft": {
                            "title": parsed.title,
                            "merchant_key": prepared.merchant_key,
                            "category_id": prepared.category_id,
                            "category_confident": prepared.category_confident,
                        },
                        "checks": checks,
                        "passed": case_passed,
                    }
                )
                if not case_passed:
                    report["failure_reason"] = "failed checks: " + ", ".join(
                        name for name, passed_check in checks.items() if not passed_check
                    )
                llm_assisted += int(prepared.llm_required)
                deterministic_only += int(not prepared.llm_required)
                safely_invalid_or_ambiguous += int(prepared.category_id is None)
            except ExpenseTextParseError as exc:
                safe = _error_is_safe(case, exc)
                report.update(
                    {
                        "deterministic_facts": None,
                        "llm_required": False,
                        "parsed_draft": None,
                        "checks": {"rejected_safely": safe},
                        "passed": safe,
                        "failure_reason": str(exc),
                    }
                )
                safely_invalid_or_ambiguous += int(safe)
            except (ExpenseDraftError, AIError) as exc:
                report.update(
                    {
                        "deterministic_facts": (
                            {
                                "amount": str(deterministic.amount),
                                "expense_date": deterministic.expense_date.isoformat(),
                                "date_source": deterministic.date_source,
                                "remaining_text": deterministic.remaining_text,
                            }
                            if deterministic
                            else None
                        ),
                        "llm_required": isinstance(exc, AIError),
                        "parsed_draft": None,
                        "checks": {"handled_safely": True, "expected_valid": False},
                        "passed": False,
                        "failure_reason": str(exc),
                    }
                )
            report["latency_seconds"] = round(time.perf_counter() - started, 3)
            reports.append(report)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))

        after_actions = db.scalar(select(func.count()).select_from(AIAction).where(AIAction.owner_id == user.id))
        db.rollback()

    no_actions_created = before_actions == after_actions
    passed = sum(bool(report["passed"]) for report in reports)
    summary = {
        "passed": passed,
        "total": len(reports),
        "deterministic_only_cases": deterministic_only,
        "llm_assisted_cases": llm_assisted,
        "invalid_or_ambiguous_handled_safely": safely_invalid_or_ambiguous,
        "median_latency_seconds": round(statistics.median(report["latency_seconds"] for report in reports), 3),
        "no_actions_created": no_actions_created,
    }
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False, sort_keys=True))
    if args.output:
        args.output.write_text(
            json.dumps({"reference_date": reference_day.isoformat(), "cases": reports, "summary": summary}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0 if passed == len(reports) and no_actions_created else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True, help="Existing production username used for owner-scoped reads")
    parser.add_argument("--expense-list-id", type=int)
    parser.add_argument("--reference-date", help="Optional YYYY-MM-DD reference date; defaults to current Moscow date")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", type=Path)
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
