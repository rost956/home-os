from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import ExpenseCategory, ExpenseItem, ExpenseList, ExpenseListShare, User
from app.timezone import msk_date_to_utc_naive


class ExpenseCommandError(Exception):
    """A safe domain error for an expense command."""


class ExpenseAccessDeniedError(ExpenseCommandError):
    pass


class ExpenseCategoryUnavailableError(ExpenseCommandError):
    pass


@dataclass(frozen=True)
class ExpenseCategoryChoice:
    id: int
    name: str
    expense_list_id: int
    expense_list_title: str


@dataclass(frozen=True)
class ExpenseCreateCommand:
    expense_list_id: int
    category_id: int
    title: str
    amount: Decimal
    expense_date: date
    include_in_analytics: bool = True
    include_in_forecast: bool = True


def writable_expense_lists(db: Session, actor: User) -> list[ExpenseList]:
    """Return only lists on which the actor may create an expense."""
    lists = db.scalars(
        select(ExpenseList)
        .outerjoin(ExpenseListShare)
        .options(selectinload(ExpenseList.categories))
        .where(
            or_(
                ExpenseList.owner_id == actor.id,
                (ExpenseListShare.user_id == actor.id) & ExpenseListShare.can_edit.is_(True),
            )
        )
        .order_by(ExpenseList.title, ExpenseList.id)
    ).unique().all()
    return lists


def require_writable_expense_list(db: Session, actor: User, expense_list_id: int) -> ExpenseList:
    expense_list = db.scalar(
        select(ExpenseList)
        .options(selectinload(ExpenseList.categories), selectinload(ExpenseList.shares))
        .where(ExpenseList.id == expense_list_id)
    )
    if expense_list is None:
        raise ExpenseAccessDeniedError("Expense list is unavailable")
    if expense_list.owner_id == actor.id:
        return expense_list
    share = next((item for item in expense_list.shares if item.user_id == actor.id), None)
    if share is None or not share.can_edit:
        raise ExpenseAccessDeniedError("Expense list is unavailable")
    return expense_list


def require_writable_expense_category(
    db: Session,
    actor: User,
    *,
    expense_list_id: int,
    category_id: int,
) -> tuple[ExpenseList, ExpenseCategory]:
    expense_list = require_writable_expense_list(db, actor, expense_list_id)
    category = next((item for item in expense_list.categories if item.id == category_id), None)
    if category is None:
        raise ExpenseCategoryUnavailableError("Expense category is unavailable")
    return expense_list, category


def writable_expense_category_choices(db: Session, actor: User) -> list[ExpenseCategoryChoice]:
    choices: list[ExpenseCategoryChoice] = []
    for expense_list in writable_expense_lists(db, actor):
        choices.extend(
            ExpenseCategoryChoice(
                id=category.id,
                name=category.name,
                expense_list_id=expense_list.id,
                expense_list_title=expense_list.title,
            )
            for category in expense_list.categories
        )
    return choices


def create_expense(db: Session, actor: User, command: ExpenseCreateCommand) -> ExpenseItem:
    """Create and flush an expense after authoritative access validation.

    The caller owns the transaction and must decide whether to commit or roll back.
    """
    _, category = require_writable_expense_category(
        db,
        actor,
        expense_list_id=command.expense_list_id,
        category_id=command.category_id,
    )
    item = ExpenseItem(
        category_id=category.id,
        title=command.title,
        amount=command.amount,
        created_at=msk_date_to_utc_naive(command.expense_date),
        include_in_analytics=command.include_in_analytics,
        include_in_forecast=command.include_in_forecast,
    )
    db.add(item)
    db.flush()
    return item
