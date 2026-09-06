from enum import StrEnum


class AIDomain(StrEnum):
    GENERAL = "general"
    FINANCE = "finance"
    RECIPES = "recipes"
    MENU = "menu"
    PLANNER = "planner"
    WISHLIST = "wishlist"
    CHAT = "chat"
    TODAY = "today"


class AIActionType(StrEnum):
    CREATE_EXPENSE = "expense.create"
    APPLY_MENU = "menu.apply"
    CREATE_PLANNER_ITEM = "planner.create"


class AIActionStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


TERMINAL_ACTION_STATUSES = frozenset(
    {
        AIActionStatus.CONFIRMED,
        AIActionStatus.CANCELLED,
        AIActionStatus.EXPIRED,
        AIActionStatus.FAILED,
    }
)


ACTION_DOMAINS: dict[AIActionType, AIDomain] = {
    AIActionType.CREATE_EXPENSE: AIDomain.FINANCE,
    AIActionType.APPLY_MENU: AIDomain.MENU,
    AIActionType.CREATE_PLANNER_ITEM: AIDomain.PLANNER,
}
