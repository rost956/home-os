from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AIUserSettings, User

from .types import AIDomain

DOMAIN_PERMISSION_FIELDS: dict[AIDomain, str] = {
    AIDomain.GENERAL: "allow_general",
    AIDomain.FINANCE: "allow_finance",
    AIDomain.RECIPES: "allow_recipes",
    AIDomain.MENU: "allow_menu",
    AIDomain.PLANNER: "allow_planner",
    AIDomain.WISHLIST: "allow_wishlist",
    AIDomain.CHAT: "allow_chat",
    AIDomain.TODAY: "allow_today",
}


def get_ai_user_settings(db: Session, user_id: int) -> AIUserSettings | None:
    return db.scalar(select(AIUserSettings).where(AIUserSettings.user_id == user_id))


def get_or_create_ai_user_settings(db: Session, user_id: int) -> AIUserSettings:
    current = get_ai_user_settings(db, user_id)
    if current:
        return current
    if db.get(User, user_id) is None:
        raise ValueError("Cannot create AI settings for an unknown user")
    current = AIUserSettings(user_id=user_id)
    db.add(current)
    db.flush()
    return current


def is_ai_domain_allowed(settings: AIUserSettings | None, domain: AIDomain | str) -> bool:
    if settings is None or not settings.enabled:
        return False
    normalized_domain = AIDomain(domain)
    return bool(getattr(settings, DOMAIN_PERMISSION_FIELDS[normalized_domain]))
