from __future__ import annotations

import importlib
from datetime import date, timedelta

from sqlalchemy import create_engine, inspect

from app.ai.actions import create_pending_action
from app.ai.handlers import ActionExecutionResult, ActionHandlerRegistry, get_action_registry
from app.ai.permissions import get_or_create_ai_user_settings, is_ai_domain_allowed
from app.ai.types import AIActionStatus, AIActionType, AIDomain
from app.main import app
from app.models import AIAction, AIUserSettings, ExpenseItem, PlannerItem
from app.timezone import now_utc


def expense_payload() -> dict[str, object]:
    return {
        "expense_list_id": 10,
        "category_id": 20,
        "title": "Лента",
        "amount": "1840.00",
        "expense_date": date(2026, 9, 5),
    }


def pending_expense(db, user, *, preview_text: str = "Добавить расход 1840 ₽") -> AIAction:
    action = create_pending_action(
        db,
        owner_id=user.id,
        action_type=AIActionType.CREATE_EXPENSE,
        proposed_payload=expense_payload(),
        preview_text=preview_text,
    )
    db.commit()
    db.refresh(action)
    return action


def allow_finance(db, user) -> AIUserSettings:
    settings = get_or_create_ai_user_settings(db, user.id)
    settings.enabled = True
    settings.allow_finance = True
    db.commit()
    return settings


def install_registry(handler):
    app.dependency_overrides[get_action_registry] = lambda: ActionHandlerRegistry(
        {AIActionType.CREATE_EXPENSE: handler}
    )


def remove_registry() -> None:
    app.dependency_overrides.pop(get_action_registry, None)


def test_permission_page_updates_only_current_user_and_changes_access(client, db, make_user, login):
    owner = make_user("permission-owner")
    other = make_user("permission-other")
    login(owner.username)

    page = client.get("/ai/settings")
    assert page.status_code == 200
    assert "Home AI" in page.text

    response = client.post(
        "/ai/settings",
        data={"enabled": "1", "allow_finance": "1", "allow_planner": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    db.expire_all()
    owner_settings = db.query(AIUserSettings).filter_by(user_id=owner.id).one()
    assert is_ai_domain_allowed(owner_settings, AIDomain.FINANCE) is True
    assert is_ai_domain_allowed(owner_settings, AIDomain.PLANNER) is True
    assert is_ai_domain_allowed(owner_settings, AIDomain.CHAT) is False
    assert db.query(AIUserSettings).filter_by(user_id=other.id).one_or_none() is None

    response = client.post("/ai/settings", data={}, follow_redirects=False)
    assert response.status_code == 303
    db.expire_all()
    assert is_ai_domain_allowed(db.query(AIUserSettings).filter_by(user_id=owner.id).one(), AIDomain.FINANCE) is False


def test_permission_update_rejects_cross_origin_request(client, db, make_user, login):
    owner = make_user("origin-owner")
    login(owner.username)

    response = client.post(
        "/ai/settings",
        data={"enabled": "1", "allow_finance": "1"},
        headers={"origin": "https://evil.example.test"},
    )

    assert response.status_code == 403
    assert db.query(AIUserSettings).filter_by(user_id=owner.id).one_or_none() is None


def test_pending_confirmation_card_is_owner_scoped_and_escaped(client, db, make_user, login):
    owner = make_user("card-owner")
    stranger = make_user("card-stranger")
    own_action = pending_expense(db, owner, preview_text='<script>alert("own")</script>')
    other_action = pending_expense(db, stranger, preview_text="Private action")
    login(owner.username)

    response = client.get("/ai/settings")

    assert response.status_code == 200
    assert own_action.public_id in response.text
    assert other_action.public_id not in response.text
    assert '<script>alert("own")</script>' not in response.text
    assert "&lt;script&gt;" in response.text
    assert "disabled" in response.text


def test_invalid_expense_reference_fails_without_writing_domain_data(client, db, make_user, login):
    owner = make_user("no-handler-owner")
    allow_finance(db, owner)
    action = pending_expense(db, owner)
    login(owner.username)

    response = client.post(f"/ai/actions/{action.public_id}/confirm", follow_redirects=False)

    assert response.status_code == 500
    db.expire_all()
    assert db.get(AIAction, action.id).status == AIActionStatus.FAILED.value
    assert db.query(ExpenseItem).count() == 0


def test_permission_is_rechecked_before_handler_execution(client, db, make_user, login):
    owner = make_user("denied-owner")
    action = pending_expense(db, owner)
    calls: list[int] = []

    def handler(_db, actor, _payload):
        calls.append(actor.id)
        return ActionExecutionResult()

    install_registry(handler)
    try:
        login(owner.username)
        response = client.post(f"/ai/actions/{action.public_id}/confirm", follow_redirects=False)
    finally:
        remove_registry()

    assert response.status_code == 403
    assert calls == []
    db.expire_all()
    assert db.get(AIAction, action.id).status == AIActionStatus.PENDING.value


def test_confirm_is_idempotent_and_executes_backend_handler_once(client, db, make_user, login):
    owner = make_user("confirm-owner")
    action = pending_expense(db, owner)
    calls: list[int] = []

    def handler(_db, actor, _payload):
        calls.append(actor.id)
        return ActionExecutionResult(entity_type="test_receipt", entity_id="receipt-1")

    install_registry(handler)
    try:
        login(owner.username)
        permission_response = client.post(
            "/ai/settings",
            data={"enabled": "1", "allow_finance": "1"},
            follow_redirects=False,
        )
        first = client.post(f"/ai/actions/{action.public_id}/confirm", follow_redirects=False)
        second = client.post(f"/ai/actions/{action.public_id}/confirm", follow_redirects=False)
    finally:
        remove_registry()

    assert permission_response.status_code == 303
    assert first.status_code == 303
    assert second.status_code == 303
    assert calls == [owner.id]
    db.expire_all()
    stored = db.get(AIAction, action.id)
    assert stored.status == AIActionStatus.CONFIRMED.value
    assert stored.result_entity_type == "test_receipt"
    assert stored.result_entity_id == "receipt-1"
    assert stored.claim_token is None


def test_invalid_stored_payload_is_rejected_without_running_handler(client, db, make_user, login):
    owner = make_user("invalid-payload-owner")
    allow_finance(db, owner)
    action = pending_expense(db, owner)
    action.proposed_payload_json = '{"amount":"not-a-number"}'
    db.commit()
    calls: list[int] = []

    def handler(_db, actor, _payload):
        calls.append(actor.id)
        return ActionExecutionResult()

    install_registry(handler)
    try:
        login(owner.username)
        response = client.post(f"/ai/actions/{action.public_id}/confirm", follow_redirects=False)
    finally:
        remove_registry()

    assert response.status_code == 409
    assert calls == []
    db.expire_all()
    assert db.get(AIAction, action.id).status == AIActionStatus.PENDING.value


def test_foreign_user_cannot_confirm_or_cancel_action(client, db, make_user, login):
    owner = make_user("ownership-owner")
    stranger = make_user("ownership-stranger")
    action = pending_expense(db, owner)
    login(stranger.username)

    confirm = client.post(f"/ai/actions/{action.public_id}/confirm", follow_redirects=False)
    cancel = client.post(f"/ai/actions/{action.public_id}/cancel", follow_redirects=False)

    assert confirm.status_code == 404
    assert cancel.status_code == 404
    db.expire_all()
    assert db.get(AIAction, action.id).status == AIActionStatus.PENDING.value


def test_cancel_is_idempotent_and_cancelled_action_cannot_be_confirmed(client, db, make_user, login):
    owner = make_user("cancel-owner")
    allow_finance(db, owner)
    action = pending_expense(db, owner)
    calls: list[int] = []

    def handler(_db, actor, _payload):
        calls.append(actor.id)
        return ActionExecutionResult()

    install_registry(handler)
    try:
        login(owner.username)
        first = client.post(f"/ai/actions/{action.public_id}/cancel", follow_redirects=False)
        second = client.post(f"/ai/actions/{action.public_id}/cancel", follow_redirects=False)
        confirm = client.post(f"/ai/actions/{action.public_id}/confirm", follow_redirects=False)
    finally:
        remove_registry()

    assert first.status_code == 303
    assert second.status_code == 303
    assert confirm.status_code == 409
    assert calls == []
    db.expire_all()
    assert db.get(AIAction, action.id).status == AIActionStatus.CANCELLED.value


def test_expired_action_is_persisted_and_cannot_be_confirmed(client, db, make_user, login):
    owner = make_user("expired-owner")
    allow_finance(db, owner)
    action = pending_expense(db, owner)
    action.expires_at = now_utc() - timedelta(seconds=1)
    db.commit()
    login(owner.username)

    response = client.post(f"/ai/actions/{action.public_id}/confirm", follow_redirects=False)

    assert response.status_code == 409
    db.expire_all()
    assert db.get(AIAction, action.id).status == AIActionStatus.EXPIRED.value


def test_claimed_action_cannot_be_cancelled(client, db, make_user, login):
    owner = make_user("claimed-owner")
    action = pending_expense(db, owner)
    action.claim_token = "server-owned-claim"
    action.claimed_at = now_utc()
    db.commit()
    login(owner.username)

    response = client.post(f"/ai/actions/{action.public_id}/cancel", follow_redirects=False)

    assert response.status_code == 409
    db.expire_all()
    stored = db.get(AIAction, action.id)
    assert stored.status == AIActionStatus.PENDING.value
    assert stored.claim_token == "server-owned-claim"


def test_handler_failure_rolls_back_domain_write_and_marks_action_failed(client, db, make_user, login):
    owner = make_user("rollback-owner")
    allow_finance(db, owner)
    action = pending_expense(db, owner)

    def failing_handler(handler_db, actor, _payload):
        handler_db.add(
            PlannerItem(
                owner_id=actor.id,
                title="Must be rolled back",
                scheduled_for=date(2026, 9, 7),
            )
        )
        handler_db.flush()
        raise RuntimeError("simulated write failure")

    install_registry(failing_handler)
    try:
        login(owner.username)
        response = client.post(f"/ai/actions/{action.public_id}/confirm", follow_redirects=False)
    finally:
        remove_registry()

    assert response.status_code == 500
    db.expire_all()
    assert db.query(PlannerItem).filter_by(owner_id=owner.id).count() == 0
    stored = db.get(AIAction, action.id)
    assert stored.status == AIActionStatus.FAILED.value
    assert stored.error_code == "execution_failed"
    assert stored.claim_token is None


def test_runtime_migration_adds_claim_columns_without_losing_actions(tmp_path, monkeypatch):
    legacy_engine = create_engine(f"sqlite:///{(tmp_path / 'legacy-ai.db').as_posix()}")
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                theme VARCHAR(20) NOT NULL DEFAULT 'light',
                expense_period_start_day INTEGER NOT NULL DEFAULT 1,
                last_seen_at DATETIME
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE ai_actions (
                id INTEGER PRIMARY KEY,
                public_id VARCHAR(36) NOT NULL,
                owner_id INTEGER NOT NULL,
                action_type VARCHAR(80) NOT NULL,
                payload_version INTEGER NOT NULL,
                proposed_payload_json TEXT NOT NULL,
                confirmed_payload_json TEXT,
                preview_text TEXT,
                status VARCHAR(20) NOT NULL,
                expires_at DATETIME NOT NULL,
                resolved_at DATETIME,
                error_code VARCHAR(80),
                result_entity_type VARCHAR(80),
                result_entity_id VARCHAR(100),
                source_conversation_id VARCHAR(36),
                source_message_id VARCHAR(36),
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO ai_actions (
                id, public_id, owner_id, action_type, payload_version,
                proposed_payload_json, status, expires_at, created_at, updated_at
            ) VALUES (
                1, 'legacy-action', 1, 'expense.create', 1,
                '{}', 'pending', '2026-09-07 12:00:00',
                '2026-09-06 12:00:00', '2026-09-06 12:00:00'
            )
            """
        )

    main_module = importlib.import_module("app.main")
    monkeypatch.setattr(main_module, "engine", legacy_engine)
    main_module.ensure_runtime_schema()

    columns = {column["name"] for column in inspect(legacy_engine).get_columns("ai_actions")}
    assert {"claim_token", "claimed_at"} <= columns
    with legacy_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT public_id FROM ai_actions WHERE id = 1").scalar_one() == "legacy-action"
    legacy_engine.dispose()
