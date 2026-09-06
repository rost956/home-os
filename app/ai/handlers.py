from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import User

from .action_schemas import ActionPayload, CreateExpenseActionPayload
from .expenses import confirm_expense_action
from .types import AIActionType


@dataclass(frozen=True)
class ActionExecutionResult:
    entity_type: str | None = None
    entity_id: str | int | None = None


ActionHandler = Callable[[Session, User, ActionPayload], ActionExecutionResult | None]


class ActionHandlerRegistry:
    """Immutable server-owned allowlist of action handlers."""

    def __init__(self, handlers: Mapping[AIActionType, ActionHandler] | None = None) -> None:
        self._handlers = dict(handlers or {})

    def get(self, action_type: AIActionType | str) -> ActionHandler | None:
        try:
            normalized_type = AIActionType(action_type)
        except ValueError:
            return None
        return self._handlers.get(normalized_type)

    def supports(self, action_type: AIActionType | str) -> bool:
        return self.get(action_type) is not None

    @property
    def supported_types(self) -> frozenset[str]:
        return frozenset(action_type.value for action_type in self._handlers)


def _create_expense_handler(db: Session, actor: User, payload: ActionPayload) -> ActionExecutionResult:
    if not isinstance(payload, CreateExpenseActionPayload):
        raise TypeError("Expense handler received an invalid payload")
    return ActionExecutionResult(entity_type="expense_item", entity_id=confirm_expense_action(db, actor, payload))


DEFAULT_ACTION_REGISTRY = ActionHandlerRegistry({AIActionType.CREATE_EXPENSE: _create_expense_handler})


def get_action_registry() -> ActionHandlerRegistry:
    return DEFAULT_ACTION_REGISTRY
