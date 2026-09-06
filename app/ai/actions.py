from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import AIAction, User
from app.timezone import now_utc

from .action_schemas import ActionPayload, validate_action_payload
from .handlers import ActionExecutionResult, ActionHandlerRegistry
from .permissions import get_ai_user_settings, is_ai_domain_allowed
from .types import ACTION_DOMAINS, TERMINAL_ACTION_STATUSES, AIActionStatus, AIActionType

DEFAULT_ACTION_TTL = timedelta(minutes=20)
MAX_ACTION_TTL = timedelta(hours=24)
IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9_.-]{1,80}$")


class AIActionError(Exception):
    pass


class AIActionPayloadError(AIActionError):
    pass


class AIActionTransitionError(AIActionError):
    pass


class AIActionPermissionError(AIActionError):
    pass


class AIActionHandlerUnavailableError(AIActionError):
    pass


class AIActionExecutionError(AIActionError):
    pass


@dataclass(frozen=True)
class ActionConfirmation:
    action: AIAction
    executed: bool


@dataclass(frozen=True)
class ActionCancellation:
    action: AIAction
    cancelled: bool


def _serialize_payload(payload: ActionPayload) -> str:
    return json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validated_payload(action_type: AIActionType | str, payload: Mapping[str, Any] | BaseModel) -> ActionPayload:
    raw_payload = payload.model_dump(mode="python") if isinstance(payload, BaseModel) else dict(payload)
    try:
        return validate_action_payload(action_type, raw_payload)
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise AIActionPayloadError("Action payload is invalid") from exc


def create_pending_action(
    db: Session,
    *,
    owner_id: int,
    action_type: AIActionType | str,
    proposed_payload: Mapping[str, Any] | BaseModel,
    preview_text: str | None = None,
    expires_in: timedelta = DEFAULT_ACTION_TTL,
    now: datetime | None = None,
) -> AIAction:
    if db.get(User, owner_id) is None:
        raise AIActionError("Cannot create an action for an unknown user")
    if expires_in <= timedelta(0) or expires_in > MAX_ACTION_TTL:
        raise AIActionError("Action expiry must be between 1 second and 24 hours")
    clean_preview = (preview_text or "").strip() or None
    if clean_preview and len(clean_preview) > 2_000:
        raise AIActionError("Action preview is too long")
    try:
        normalized_type = AIActionType(action_type)
    except ValueError as exc:
        raise AIActionPayloadError("Unsupported action type") from exc
    validated = _validated_payload(normalized_type, proposed_payload)
    current_time = now or now_utc()
    action = AIAction(
        owner_id=owner_id,
        action_type=normalized_type.value,
        payload_version=1,
        proposed_payload_json=_serialize_payload(validated),
        preview_text=clean_preview,
        status=AIActionStatus.PENDING.value,
        expires_at=current_time + expires_in,
        created_at=current_time,
        updated_at=current_time,
    )
    db.add(action)
    db.flush()
    return action


def get_owned_action(
    db: Session,
    *,
    public_id: str,
    owner_id: int,
    now: datetime | None = None,
) -> AIAction | None:
    action = db.scalar(
        select(AIAction).where(
            AIAction.public_id == public_id,
            AIAction.owner_id == owner_id,
        )
    )
    if action:
        expire_pending_action(action, now=now)
        db.flush()
    return action


def list_owned_pending_actions(
    db: Session,
    *,
    owner_id: int,
    now: datetime | None = None,
    limit: int = 30,
) -> tuple[list[AIAction], int]:
    actions = db.scalars(
        select(AIAction)
        .where(
            AIAction.owner_id == owner_id,
            AIAction.status == AIActionStatus.PENDING.value,
        )
        .order_by(AIAction.created_at.desc())
        .limit(max(1, min(limit, 100)))
    ).all()
    expired_count = 0
    pending: list[AIAction] = []
    for action in actions:
        if expire_pending_action(action, now=now):
            expired_count += 1
        else:
            pending.append(action)
    db.flush()
    return pending, expired_count


def expire_pending_action(action: AIAction, *, now: datetime | None = None) -> bool:
    current_time = now or now_utc()
    if action.status != AIActionStatus.PENDING.value or action.expires_at > current_time:
        return False
    action.status = AIActionStatus.EXPIRED.value
    action.resolved_at = current_time
    action.updated_at = current_time
    return True


def transition_action(
    action: AIAction,
    target_status: AIActionStatus | str,
    *,
    confirmed_payload: Mapping[str, Any] | BaseModel | None = None,
    error_code: str | None = None,
    result_entity_type: str | None = None,
    result_entity_id: str | int | None = None,
    now: datetime | None = None,
) -> AIAction:
    try:
        target = AIActionStatus(target_status)
        current = AIActionStatus(action.status)
    except ValueError as exc:
        raise AIActionTransitionError("Unknown action status") from exc
    current_time = now or now_utc()
    expire_pending_action(action, now=current_time)
    current = AIActionStatus(action.status)
    if current == target:
        return action
    if current != AIActionStatus.PENDING or target not in TERMINAL_ACTION_STATUSES:
        raise AIActionTransitionError(f"Cannot transition action from {current.value} to {target.value}")

    if confirmed_payload is not None:
        if target != AIActionStatus.CONFIRMED:
            raise AIActionTransitionError("Only confirmed actions may have a confirmed payload")
        validated = _validated_payload(action.action_type, confirmed_payload)
        action.confirmed_payload_json = _serialize_payload(validated)

    if error_code is not None:
        if target != AIActionStatus.FAILED or not IDENTIFIER_PATTERN.fullmatch(error_code):
            raise AIActionTransitionError("Invalid action error code")
        action.error_code = error_code

    if result_entity_type is not None:
        if target != AIActionStatus.CONFIRMED or not IDENTIFIER_PATTERN.fullmatch(result_entity_type):
            raise AIActionTransitionError("Invalid result entity type")
        action.result_entity_type = result_entity_type
    if result_entity_id is not None:
        if target != AIActionStatus.CONFIRMED:
            raise AIActionTransitionError("Only confirmed actions may reference a result entity")
        clean_result_id = str(result_entity_id).strip()
        if not clean_result_id or len(clean_result_id) > 100:
            raise AIActionTransitionError("Invalid result entity id")
        action.result_entity_id = clean_result_id

    action.status = target.value
    action.resolved_at = current_time
    action.updated_at = current_time
    return action


def confirm_action(
    db: Session,
    *,
    action: AIAction,
    actor: User,
    registry: ActionHandlerRegistry,
    confirmed_payload: Mapping[str, Any] | BaseModel | None = None,
    now: datetime | None = None,
) -> ActionConfirmation:
    if action.owner_id != actor.id:
        raise AIActionPermissionError("Action does not belong to this user")
    current_time = now or now_utc()
    expire_pending_action(action, now=current_time)
    if action.status == AIActionStatus.CONFIRMED.value:
        return ActionConfirmation(action=action, executed=False)
    if action.status == AIActionStatus.EXPIRED.value:
        raise AIActionTransitionError("Action has expired")
    if action.status != AIActionStatus.PENDING.value:
        raise AIActionTransitionError(f"Cannot confirm action with status {action.status}")

    try:
        action_type = AIActionType(action.action_type)
    except ValueError as exc:
        raise AIActionHandlerUnavailableError("Unsupported action type") from exc
    domain = ACTION_DOMAINS[action_type]
    user_settings = get_ai_user_settings(db, actor.id)
    if not is_ai_domain_allowed(user_settings, domain):
        raise AIActionPermissionError("AI permission is disabled for this action")
    handler = registry.get(action_type)
    if handler is None:
        raise AIActionHandlerUnavailableError("No backend handler is registered for this action")

    if confirmed_payload is None:
        try:
            raw_payload = json.loads(action.proposed_payload_json)
        except (TypeError, ValueError) as exc:
            raise AIActionPayloadError("Stored action payload is invalid") from exc
        validated_payload = _validated_payload(action_type, raw_payload)
    else:
        validated_payload = _validated_payload(action_type, confirmed_payload)

    claim_token = str(uuid.uuid4())
    claimed = db.execute(
        update(AIAction)
        .where(
            AIAction.id == action.id,
            AIAction.owner_id == actor.id,
            AIAction.status == AIActionStatus.PENDING.value,
            AIAction.claim_token.is_(None),
            AIAction.expires_at > current_time,
        )
        .values(claim_token=claim_token, claimed_at=current_time, updated_at=current_time)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        db.refresh(action)
        if action.status == AIActionStatus.CONFIRMED.value:
            return ActionConfirmation(action=action, executed=False)
        raise AIActionTransitionError("Action is already being processed or is no longer pending")
    db.refresh(action)

    try:
        result = handler(db, actor, validated_payload) or ActionExecutionResult()
        if not isinstance(result, ActionExecutionResult):
            raise TypeError("Action handler returned an invalid result")
        transition_action(
            action,
            AIActionStatus.CONFIRMED,
            confirmed_payload=confirmed_payload,
            result_entity_type=result.entity_type,
            result_entity_id=result.entity_id,
            now=current_time,
        )
    except Exception as exc:
        raise AIActionExecutionError("Backend action handler failed") from exc

    action.claim_token = None
    return ActionConfirmation(action=action, executed=True)


def cancel_action(
    db: Session,
    *,
    action: AIAction,
    actor: User,
    now: datetime | None = None,
) -> ActionCancellation:
    if action.owner_id != actor.id:
        raise AIActionPermissionError("Action does not belong to this user")
    current_time = now or now_utc()
    expire_pending_action(action, now=current_time)
    if action.status == AIActionStatus.CANCELLED.value:
        return ActionCancellation(action=action, cancelled=False)
    if action.status == AIActionStatus.EXPIRED.value:
        raise AIActionTransitionError("Action has expired")
    if action.status != AIActionStatus.PENDING.value:
        raise AIActionTransitionError(f"Cannot cancel action with status {action.status}")

    cancelled = db.execute(
        update(AIAction)
        .where(
            AIAction.id == action.id,
            AIAction.owner_id == actor.id,
            AIAction.status == AIActionStatus.PENDING.value,
            AIAction.claim_token.is_(None),
            AIAction.expires_at > current_time,
        )
        .values(
            status=AIActionStatus.CANCELLED.value,
            resolved_at=current_time,
            updated_at=current_time,
        )
        .execution_options(synchronize_session=False)
    )
    db.refresh(action)
    if cancelled.rowcount != 1:
        if action.status == AIActionStatus.CANCELLED.value:
            return ActionCancellation(action=action, cancelled=False)
        raise AIActionTransitionError("Action is already being processed or is no longer pending")
    return ActionCancellation(action=action, cancelled=True)


def mark_action_failed_after_rollback(
    db: Session,
    *,
    public_id: str,
    owner_id: int,
    error_code: str = "execution_failed",
    now: datetime | None = None,
) -> AIAction | None:
    action = get_owned_action(db, public_id=public_id, owner_id=owner_id, now=now)
    if not action or action.status != AIActionStatus.PENDING.value:
        return action
    transition_action(action, AIActionStatus.FAILED, error_code=error_code, now=now)
    action.claim_token = None
    return action
