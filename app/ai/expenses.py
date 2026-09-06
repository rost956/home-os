from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AIAction, ExpenseMerchantRule, User
from app.services.expenses import (
    ExpenseCategoryChoice,
    ExpenseCategoryUnavailableError,
    ExpenseCommandError,
    ExpenseCreateCommand,
    create_expense,
    require_writable_expense_category,
    require_writable_expense_list,
    writable_expense_category_choices,
    writable_expense_lists,
)
from app.timezone import today_msk

from .action_schemas import CreateExpenseActionPayload, validate_action_payload
from .client import AIClient
from .schemas import AICompletionRequest, AIMessage, ExpenseCategorySelection

MAX_EXPENSE_TEXT_LENGTH = 500
LLM_CATEGORY_CONFIDENCE = Decimal("0.85")
AMOUNT_PATTERN = re.compile(r"(?<![\w])(?P<amount>\d{1,8}(?:[.,]\d{1,2})?)(?![\w])")
WHITESPACE_PATTERN = re.compile(r"\s+")
DATE_WORDS = {
    "сегодня": 0,
    "today": 0,
    "вчера": -1,
    "yesterday": -1,
}
FOOD_MERCHANTS = frozenset({"лента", "пятерочка", "пятёрочка", "пятерка", "пятёрка", "magnit", "магнит"})
FUEL_MERCHANTS = frozenset({"бензин", "азс", "gas", "fuel"})
FOOD_CATEGORY_NAMES = frozenset({"еда", "продукты", "продукт", "grocery", "groceries"})
FUEL_CATEGORY_NAMES = frozenset({"бензин", "топливо", "авто", "транспорт", "машина"})


class ExpenseDraftError(Exception):
    pass


class ExpenseTextParseError(ExpenseDraftError):
    pass


class ExpenseDraftAmbiguityError(ExpenseDraftError):
    pass


class ExpenseDraftLowConfidenceError(ExpenseDraftError):
    pass


@dataclass(frozen=True)
class ParsedExpenseText:
    title: str
    merchant_key: str
    amount: Decimal
    expense_date: date


def normalize_merchant_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = "".join(character if character.isalnum() else " " for character in normalized)
    return WHITESPACE_PATTERN.sub(" ", normalized).strip()[:120]


def parse_expense_text(raw_text: str, *, today: date | None = None) -> ParsedExpenseText:
    clean_text = WHITESPACE_PATTERN.sub(" ", raw_text.strip())
    if not clean_text or len(clean_text) > MAX_EXPENSE_TEXT_LENGTH:
        raise ExpenseTextParseError("Enter an expense up to 500 characters")
    amounts = list(AMOUNT_PATTERN.finditer(clean_text))
    if len(amounts) != 1:
        raise ExpenseTextParseError("Specify exactly one expense amount")
    try:
        amount = Decimal(amounts[0].group("amount").replace(",", "."))
    except (InvalidOperation, ValueError) as exc:
        raise ExpenseTextParseError("Expense amount is invalid") from exc
    if amount <= 0 or amount > Decimal("99999999.99"):
        raise ExpenseTextParseError("Expense amount is out of range")

    selected_date = today or today_msk()
    title_parts: list[str] = []
    for token in clean_text[: amounts[0].start()].split() + clean_text[amounts[0].end() :].split():
        relative_day = DATE_WORDS.get(token.casefold().strip(".,!?:;"))
        if relative_day is not None:
            selected_date += timedelta(days=relative_day)
        else:
            title_parts.append(token)
    title = " ".join(title_parts).strip(" -—,.;:")
    if not title or len(title) > 150:
        raise ExpenseTextParseError("Expense title is invalid")
    merchant_key = normalize_merchant_key(title)
    if not merchant_key:
        raise ExpenseTextParseError("Expense merchant is invalid")
    return ParsedExpenseText(title=title, merchant_key=merchant_key, amount=amount, expense_date=selected_date)


def resolve_expense_list(
    db: Session,
    actor: User,
    *,
    expense_list_id: int | None,
    category_id: int | None,
) -> int:
    if expense_list_id is not None:
        require_writable_expense_list(db, actor, expense_list_id)
        return expense_list_id
    if category_id is not None:
        choices = writable_expense_category_choices(db, actor)
        selected = next((item for item in choices if item.id == category_id), None)
        if selected is None:
            raise ExpenseDraftAmbiguityError("Choose a category available to you")
        return selected.expense_list_id
    lists = writable_expense_lists(db, actor)
    if len(lists) != 1:
        raise ExpenseDraftAmbiguityError("Choose the expense list")
    return lists[0].id


def find_merchant_rule_category(
    db: Session,
    actor: User,
    *,
    expense_list_id: int,
    merchant_key: str,
) -> int | None:
    rule = db.scalar(
        select(ExpenseMerchantRule).where(
            ExpenseMerchantRule.owner_id == actor.id,
            ExpenseMerchantRule.expense_list_id == expense_list_id,
            ExpenseMerchantRule.merchant_key == merchant_key,
        )
    )
    if rule is None:
        return None
    try:
        require_writable_expense_category(
            db,
            actor,
            expense_list_id=expense_list_id,
            category_id=rule.category_id,
        )
    except (ExpenseCommandError, ExpenseCategoryUnavailableError):
        return None
    return rule.category_id


def heuristic_category_id(parsed: ParsedExpenseText, categories: list[ExpenseCategoryChoice]) -> int | None:
    merchant_terms = set(parsed.merchant_key.split())
    expected_names: set[str] = set()
    if merchant_terms & FOOD_MERCHANTS:
        expected_names.update(FOOD_CATEGORY_NAMES)
    if merchant_terms & FUEL_MERCHANTS:
        expected_names.update(FUEL_CATEGORY_NAMES)
    direct = [item.id for item in categories if normalize_merchant_key(item.name) in merchant_terms]
    hinted = [item.id for item in categories if normalize_merchant_key(item.name) in expected_names]
    candidates = list(dict.fromkeys(direct + hinted))
    return candidates[0] if len(candidates) == 1 else None


async def select_category_with_llm(
    client: AIClient,
    *,
    text: str,
    categories: list[ExpenseCategoryChoice],
) -> int | None:
    if not categories:
        return None
    category_text = json.dumps(
        [{"id": item.id, "name": item.name} for item in categories],
        ensure_ascii=False,
        separators=(",", ",:"),
    )
    request = AICompletionRequest(
        messages=[
            AIMessage(
                role="system",
                content=(
                    "Select a category for one household expense. Return JSON only. "
                    "Use only an id from the supplied categories. Set ambiguous=true or category_id=null "
                    "when the text does not support a confident choice."
                ),
            ),
            AIMessage(role="user", content=f"Expense text: {text}\nAllowed categories: {category_text}"),
        ],
        max_tokens=120,
        temperature=0.0,
        output_mode="json",
    )
    selection = await client.complete_json(request, ExpenseCategorySelection)
    allowed_ids = {item.id for item in categories}
    if (
        selection.category_id not in allowed_ids
        or selection.ambiguous
        or Decimal(str(selection.confidence)) < LLM_CATEGORY_CONFIDENCE
    ):
        return None
    return selection.category_id


def build_expense_draft_payload(
    parsed: ParsedExpenseText,
    *,
    expense_list_id: int,
    category_id: int,
) -> CreateExpenseActionPayload:
    return CreateExpenseActionPayload(
        expense_list_id=expense_list_id,
        category_id=category_id,
        title=parsed.title,
        amount=parsed.amount,
        expense_date=parsed.expense_date,
        merchant_key=parsed.merchant_key,
    )


def expense_payload_from_action(action: AIAction) -> CreateExpenseActionPayload:
    try:
        raw_payload = json.loads(action.proposed_payload_json)
        payload = validate_action_payload(action.action_type, raw_payload)
    except (TypeError, ValueError) as exc:
        raise ExpenseDraftError("Stored expense proposal is invalid") from exc
    if not isinstance(payload, CreateExpenseActionPayload):
        raise ExpenseDraftError("Action is not an expense proposal")
    return payload


def corrected_expense_payload(
    action: AIAction,
    *,
    category_id: int | None,
    remember_category: bool,
) -> CreateExpenseActionPayload | None:
    if category_id is None and not remember_category:
        return None
    payload = expense_payload_from_action(action)
    return payload.model_copy(
        update={
            "category_id": category_id if category_id is not None else payload.category_id,
            "remember_category": remember_category,
        }
    )


def expense_action_category_choices(db: Session, actor: User, action: AIAction) -> list[ExpenseCategoryChoice]:
    try:
        payload = expense_payload_from_action(action)
    except ExpenseDraftError:
        return []
    return [
        item
        for item in writable_expense_category_choices(db, actor)
        if item.expense_list_id == payload.expense_list_id
    ]


def upsert_merchant_rule(
    db: Session,
    actor: User,
    *,
    expense_list_id: int,
    category_id: int,
    merchant_key: str,
) -> None:
    normalized_key = normalize_merchant_key(merchant_key)
    if not normalized_key:
        return
    require_writable_expense_category(
        db,
        actor,
        expense_list_id=expense_list_id,
        category_id=category_id,
    )
    rule = db.scalar(
        select(ExpenseMerchantRule).where(
            ExpenseMerchantRule.owner_id == actor.id,
            ExpenseMerchantRule.expense_list_id == expense_list_id,
            ExpenseMerchantRule.merchant_key == normalized_key,
        )
    )
    if rule is None:
        rule = ExpenseMerchantRule(
            owner_id=actor.id,
            expense_list_id=expense_list_id,
            category_id=category_id,
            merchant_key=normalized_key,
            use_count=1,
        )
        db.add(rule)
    else:
        rule.category_id = category_id
        rule.use_count += 1
    db.flush()


def confirm_expense_action(db: Session, actor: User, payload: CreateExpenseActionPayload) -> int:
    item = create_expense(
        db,
        actor,
        ExpenseCreateCommand(
            expense_list_id=payload.expense_list_id,
            category_id=payload.category_id,
            title=payload.title,
            amount=payload.amount,
            expense_date=payload.expense_date,
            include_in_analytics=payload.include_in_analytics,
            include_in_forecast=payload.include_in_forecast,
        ),
    )
    if payload.remember_category and payload.merchant_key:
        upsert_merchant_rule(
            db,
            actor,
            expense_list_id=payload.expense_list_id,
            category_id=payload.category_id,
            merchant_key=payload.merchant_key,
        )
    return item.id
