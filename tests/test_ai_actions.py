from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import inspect

from app.ai.actions import (
    AIActionPayloadError,
    AIActionTransitionError,
    create_pending_action,
    expire_pending_action,
    get_owned_action,
    transition_action,
)
from app.ai.permissions import get_or_create_ai_user_settings, is_ai_domain_allowed
from app.ai.types import AIActionStatus, AIActionType, AIDomain
from app.database import Base, engine
from app.main import schema_change_required
from app.models import AIAction, AIUserSettings, User


def expense_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "expense_list_id": 10,
        "category_id": 20,
        "title": "Лента",
        "amount": "1840.00",
        "expense_date": date(2026, 9, 5),
    }
    payload.update(overrides)
    return payload


def test_ai_user_settings_default_deny_and_no_implicit_commit(db, make_user):
    user = make_user("settings-owner")
    settings = get_or_create_ai_user_settings(db, user.id)

    assert settings.enabled is False
    assert get_or_create_ai_user_settings(db, user.id) is settings
    assert is_ai_domain_allowed(settings, AIDomain.FINANCE) is False
    assert is_ai_domain_allowed(None, AIDomain.FINANCE) is False
    settings.enabled = True
    settings.allow_finance = True
    assert is_ai_domain_allowed(settings, AIDomain.FINANCE) is True

    db.rollback()
    assert db.query(AIUserSettings).filter_by(user_id=user.id).one_or_none() is None


def test_create_pending_action_validates_and_preserves_proposed_payload(db, make_user):
    user = make_user("action-owner")
    now = datetime(2026, 9, 6, 12, 0)
    action = create_pending_action(
        db,
        owner_id=user.id,
        action_type=AIActionType.CREATE_EXPENSE,
        proposed_payload=expense_payload(),
        preview_text="Добавить расход 1840 ₽",
        now=now,
    )

    proposed = json.loads(action.proposed_payload_json)
    assert action.public_id and action.id
    assert action.status == AIActionStatus.PENDING.value
    assert action.expires_at == now + timedelta(minutes=20)
    assert proposed["category_id"] == 20
    assert proposed["amount"] == "1840.00"
    assert action.confirmed_payload_json is None

    with pytest.raises(AIActionPayloadError):
        create_pending_action(
            db,
            owner_id=user.id,
            action_type=AIActionType.CREATE_EXPENSE,
            proposed_payload=expense_payload(category_id=-1),
        )
    with pytest.raises(AIActionPayloadError):
        create_pending_action(
            db,
            owner_id=user.id,
            action_type="unknown.action",
            proposed_payload={},
        )


def test_v1_action_payload_schemas_are_strict(db, make_user):
    user = make_user("schema-owner")
    menu = create_pending_action(
        db,
        owner_id=user.id,
        action_type=AIActionType.APPLY_MENU,
        proposed_payload={
            "entries": [
                {
                    "plan_date": "2026-09-07",
                    "meal_name": "Ужин",
                    "recipe_id": 7,
                }
            ]
        },
    )
    planner = create_pending_action(
        db,
        owner_id=user.id,
        action_type=AIActionType.CREATE_PLANNER_ITEM,
        proposed_payload={
            "title": "Купить корм",
            "scheduled_for": "2026-09-07",
            "start_time": "18:00",
        },
    )

    assert menu.action_type == "menu.apply"
    assert planner.action_type == "planner.create"
    with pytest.raises(AIActionPayloadError):
        create_pending_action(
            db,
            owner_id=user.id,
            action_type=AIActionType.CREATE_PLANNER_ITEM,
            proposed_payload={
                "title": "Некорректное время",
                "scheduled_for": "2026-09-07",
                "start_time": "18:00",
                "end_time": "17:00",
            },
        )


def test_action_lookup_is_owner_scoped(db, make_user):
    owner = make_user("owner")
    stranger = make_user("stranger")
    action = create_pending_action(
        db,
        owner_id=owner.id,
        action_type=AIActionType.CREATE_EXPENSE,
        proposed_payload=expense_payload(),
    )

    assert get_owned_action(db, public_id=action.public_id, owner_id=owner.id) is action
    assert get_owned_action(db, public_id=action.public_id, owner_id=stranger.id) is None


def test_action_expiry_and_terminal_transitions_are_idempotent(db, make_user):
    user = make_user("transition-owner")
    now = datetime(2026, 9, 6, 12, 0)
    expired = create_pending_action(
        db,
        owner_id=user.id,
        action_type=AIActionType.CREATE_EXPENSE,
        proposed_payload=expense_payload(),
        expires_in=timedelta(seconds=5),
        now=now,
    )
    assert expire_pending_action(expired, now=now + timedelta(seconds=6)) is True
    first_resolved_at = expired.resolved_at
    assert expire_pending_action(expired, now=now + timedelta(seconds=7)) is False
    assert expired.resolved_at == first_resolved_at
    with pytest.raises(AIActionTransitionError):
        transition_action(expired, AIActionStatus.CONFIRMED, now=now + timedelta(seconds=8))

    cancelled = create_pending_action(
        db,
        owner_id=user.id,
        action_type=AIActionType.CREATE_EXPENSE,
        proposed_payload=expense_payload(),
        now=now,
    )
    transition_action(cancelled, AIActionStatus.CANCELLED, now=now + timedelta(seconds=1))
    first_resolved_at = cancelled.resolved_at
    transition_action(cancelled, AIActionStatus.CANCELLED, now=now + timedelta(seconds=2))
    assert cancelled.resolved_at == first_resolved_at


def test_confirmed_payload_is_separate_from_immutable_proposal(db, make_user):
    user = make_user("confirm-owner")
    action = create_pending_action(
        db,
        owner_id=user.id,
        action_type=AIActionType.CREATE_EXPENSE,
        proposed_payload=expense_payload(category_id=20),
    )
    original = action.proposed_payload_json

    transition_action(
        action,
        AIActionStatus.CONFIRMED,
        confirmed_payload=expense_payload(category_id=21),
        result_entity_type="expense_item",
        result_entity_id=42,
    )
    transition_action(action, AIActionStatus.CONFIRMED)

    assert action.proposed_payload_json == original
    assert json.loads(action.confirmed_payload_json)["category_id"] == 21
    assert action.result_entity_id == "42"


def test_schema_check_detects_and_recreates_missing_ai_tables(db, make_user):
    user = make_user("migration-owner")
    user_id = user.id
    db.close()
    AIAction.__table__.drop(bind=engine)
    AIUserSettings.__table__.drop(bind=engine)

    assert schema_change_required() is True
    Base.metadata.create_all(bind=engine)

    tables = set(inspect(engine).get_table_names())
    assert {"ai_actions", "ai_user_settings"} <= tables
    assert schema_change_required() is False
    with engine.connect() as connection:
        assert connection.execute(User.__table__.select().where(User.id == user_id)).first() is not None
