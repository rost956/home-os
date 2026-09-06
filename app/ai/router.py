from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import AIAction, User
from app.services.expenses import (
    ExpenseCommandError,
    require_writable_expense_category,
    writable_expense_category_choices,
    writable_expense_lists,
)
from app.web import templates

from .actions import (
    AIActionExecutionError,
    AIActionHandlerUnavailableError,
    AIActionPayloadError,
    AIActionPermissionError,
    AIActionTransitionError,
    cancel_action,
    confirm_action,
    create_pending_action,
    get_owned_action,
    list_owned_pending_actions,
    mark_action_failed_after_rollback,
)
from .client import AIClient
from .config import AISettings, get_ai_settings
from .dependencies import get_ai_client
from .errors import AIError
from .expenses import (
    ExpenseDraftAmbiguityError,
    ExpenseDraftError,
    ExpenseDraftLowConfidenceError,
    ExpenseTextParseError,
    build_expense_draft_payload,
    corrected_expense_payload,
    expense_action_category_choices,
    expense_payload_from_action,
    find_merchant_rule_category,
    heuristic_category_id,
    parse_expense_text,
    resolve_expense_list,
    select_category_with_llm,
)
from .handlers import ActionHandlerRegistry, get_action_registry
from .permissions import (
    DOMAIN_PERMISSION_FIELDS,
    get_ai_user_settings,
    get_or_create_ai_user_settings,
    is_ai_domain_allowed,
)
from .schemas import AIHealthResponse
from .types import AIActionStatus, AIActionType, AIDomain

router = APIRouter(tags=["home-ai"])
logger = logging.getLogger("home_ai")

PERMISSION_OPTIONS = (
    (AIDomain.GENERAL, "Общий чат", "Обычные вопросы без доступа к домашним данным"),
    (AIDomain.FINANCE, "Финансы", "Расходы, доходы, лимиты и прогнозы"),
    (AIDomain.RECIPES, "Рецепты", "Поиск и рекомендации по сохранённым рецептам"),
    (AIDomain.MENU, "Меню", "Предложения меню и подготовка изменений"),
    (AIDomain.PLANNER, "Планировщик", "Чтение планов и подготовка событий"),
    (AIDomain.WISHLIST, "Хотелки", "Сопоставление желаний с финансовыми данными"),
    (AIDomain.CHAT, "Чаты", "Локальный поиск с согласием участников"),
    (AIDomain.TODAY, "Сегодня", "Персональная сводка домашних дел"),
)
ACTION_TYPE_LABELS = {
    AIActionType.CREATE_EXPENSE.value: "Добавление расхода",
    AIActionType.APPLY_MENU.value: "Применение меню",
    AIActionType.CREATE_PLANNER_ITEM.value: "Добавление в планировщик",
}


def _redirect_settings(notice: str) -> RedirectResponse:
    return RedirectResponse(f"/ai/settings?notice={quote(notice)}", status_code=status.HTTP_303_SEE_OTHER)


def _owned_action_or_404(db: Session, public_id: str, user: User) -> AIAction:
    action = get_owned_action(db, public_id=public_id, owner_id=user.id)
    if action is None:
        raise HTTPException(status_code=404, detail="AI action not found")
    return action


def _record_failed_action(db: Session, public_id: str, owner_id: int) -> None:
    db.rollback()
    try:
        mark_action_failed_after_rollback(db, public_id=public_id, owner_id=owner_id)
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Could not record failed AI action public_id=%s owner_id=%s", public_id, owner_id)


def _render_ai_settings(
    request: Request,
    *,
    user: User,
    db: Session,
    runtime_settings: AISettings,
    registry: ActionHandlerRegistry,
    error: str | None = None,
):
    pending_actions, expired_count = list_owned_pending_actions(db, owner_id=user.id)
    if expired_count:
        db.commit()
    action_categories = {}
    action_payloads = {}
    for action in pending_actions:
        if action.action_type != AIActionType.CREATE_EXPENSE.value:
            continue
        action_categories[action.public_id] = expense_action_category_choices(db, user, action)
        try:
            action_payloads[action.public_id] = expense_payload_from_action(action)
        except ExpenseDraftError:
            continue
    return templates.TemplateResponse(
        request,
        "ai_settings.html",
        {
            "user": user,
            "error": error,
            "ai_settings": get_ai_user_settings(db, user.id),
            "backend_ai_enabled": runtime_settings.enabled,
            "permission_options": PERMISSION_OPTIONS,
            "permission_fields": DOMAIN_PERMISSION_FIELDS,
            "pending_actions": pending_actions,
            "confirmable_types": registry.supported_types,
            "action_type_labels": ACTION_TYPE_LABELS,
            "writable_expense_lists": writable_expense_lists(db, user),
            "expense_category_choices": writable_expense_category_choices(db, user),
            "expense_action_categories": action_categories,
            "action_payloads": action_payloads,
        },
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT if error else status.HTTP_200_OK,
    )


@router.get("/api/ai/health", response_model=AIHealthResponse)
async def ai_health(
    _user: User = Depends(get_current_user),
    settings: AISettings = Depends(get_ai_settings),
    client: AIClient = Depends(get_ai_client),
) -> AIHealthResponse:
    availability = await client.probe()
    return AIHealthResponse(
        state=availability.state,
        model=settings.model if availability.state != "disabled" else None,
    )


@router.get("/ai/settings")
def ai_settings_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    runtime_settings: AISettings = Depends(get_ai_settings),
    registry: ActionHandlerRegistry = Depends(get_action_registry),
):
    return _render_ai_settings(
        request,
        user=user,
        db=db,
        runtime_settings=runtime_settings,
        registry=registry,
    )


@router.post("/ai/settings")
def ai_settings_update(
    enabled: str | None = Form(None),
    allow_general: str | None = Form(None),
    allow_finance: str | None = Form(None),
    allow_recipes: str | None = Form(None),
    allow_menu: str | None = Form(None),
    allow_planner: str | None = Form(None),
    allow_wishlist: str | None = Form(None),
    allow_chat: str | None = Form(None),
    allow_today: str | None = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_settings = get_or_create_ai_user_settings(db, user.id)
    user_settings.enabled = enabled == "1"
    submitted_permissions = {
        "allow_general": allow_general,
        "allow_finance": allow_finance,
        "allow_recipes": allow_recipes,
        "allow_menu": allow_menu,
        "allow_planner": allow_planner,
        "allow_wishlist": allow_wishlist,
        "allow_chat": allow_chat,
        "allow_today": allow_today,
    }
    for field_name, value in submitted_permissions.items():
        setattr(user_settings, field_name, value == "1")
    db.commit()
    return _redirect_settings("Настройки Home AI сохранены")


@router.post("/ai/expenses/draft")
async def ai_expense_draft(
    request: Request,
    text: str = Form(...),
    expense_list_id: int | None = Form(None),
    category_id: int | None = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    runtime_settings: AISettings = Depends(get_ai_settings),
    registry: ActionHandlerRegistry = Depends(get_action_registry),
    client: AIClient = Depends(get_ai_client),
):
    if not is_ai_domain_allowed(get_ai_user_settings(db, user.id), AIDomain.FINANCE):
        raise HTTPException(status_code=403, detail="AI permission is disabled for expenses")
    try:
        parsed = parse_expense_text(text)
        selected_list_id = resolve_expense_list(
            db,
            user,
            expense_list_id=expense_list_id,
            category_id=category_id,
        )
        categories = [
            item
            for item in writable_expense_category_choices(db, user)
            if item.expense_list_id == selected_list_id
        ]
        if category_id is not None:
            if not any(item.id == category_id for item in categories):
                raise ExpenseDraftAmbiguityError("Choose a category from the selected expense list")
            selected_category_id = category_id
        else:
            selected_category_id = find_merchant_rule_category(
                db,
                user,
                expense_list_id=selected_list_id,
                merchant_key=parsed.merchant_key,
            ) or heuristic_category_id(parsed, categories)
            if selected_category_id is None:
                selected_category_id = await select_category_with_llm(client, text=text, categories=categories)
            if selected_category_id is None:
                raise ExpenseDraftLowConfidenceError("Не удалось уверенно выбрать категорию. Выберите её вручную.")
        payload = build_expense_draft_payload(
            parsed,
            expense_list_id=selected_list_id,
            category_id=selected_category_id,
        )
        action = create_pending_action(
            db,
            owner_id=user.id,
            action_type=AIActionType.CREATE_EXPENSE,
            proposed_payload=payload,
            preview_text=(
                f"{payload.title} — {payload.amount} ₽, {payload.expense_date.isoformat()}. "
                "Проверьте категорию перед подтверждением."
            ),
        )
        db.commit()
    except (ExpenseTextParseError, ExpenseDraftAmbiguityError, ExpenseDraftLowConfidenceError, ExpenseCommandError) as exc:
        db.rollback()
        return _render_ai_settings(
            request,
            user=user,
            db=db,
            runtime_settings=runtime_settings,
            registry=registry,
            error=str(exc),
        )
    except AIError:
        db.rollback()
        return _render_ai_settings(
            request,
            user=user,
            db=db,
            runtime_settings=runtime_settings,
            registry=registry,
            error="Не удалось уверенно выбрать категорию. Выберите её вручную.",
        )
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Could not create AI expense draft owner_id=%s", user.id)
        return _render_ai_settings(
            request,
            user=user,
            db=db,
            runtime_settings=runtime_settings,
            registry=registry,
            error="Не удалось подготовить расход. Попробуйте ещё раз.",
        )
    return _redirect_settings(f"Расход подготовлен: {action.public_id}")


@router.post("/ai/actions/{public_id}/confirm")
def ai_action_confirm(
    public_id: str,
    category_id: int | None = Form(None),
    remember_category: str | None = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    registry: ActionHandlerRegistry = Depends(get_action_registry),
):
    action = _owned_action_or_404(db, public_id, user)
    if action.status == AIActionStatus.EXPIRED.value:
        db.commit()
        raise HTTPException(status_code=409, detail="AI action has expired")
    try:
        confirmed_payload = None
        if action.action_type == AIActionType.CREATE_EXPENSE.value:
            confirmed_payload = corrected_expense_payload(
                action,
                category_id=category_id,
                remember_category=remember_category == "1",
            )
            if confirmed_payload is not None:
                require_writable_expense_category(
                    db,
                    user,
                    expense_list_id=confirmed_payload.expense_list_id,
                    category_id=confirmed_payload.category_id,
                )
        confirmation = confirm_action(
            db,
            action=action,
            actor=user,
            registry=registry,
            confirmed_payload=confirmed_payload,
        )
    except AIActionPermissionError as exc:
        db.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (AIActionHandlerUnavailableError, AIActionPayloadError, ExpenseCommandError, ExpenseDraftError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AIActionTransitionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OperationalError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="AI action is being processed") from exc
    except AIActionExecutionError as exc:
        _record_failed_action(db, public_id, user.id)
        logger.warning("AI action handler failed public_id=%s owner_id=%s", public_id, user.id)
        raise HTTPException(status_code=500, detail="AI action failed safely; no changes were applied") from exc

    try:
        db.commit()
    except SQLAlchemyError as exc:
        _record_failed_action(db, public_id, user.id)
        raise HTTPException(status_code=500, detail="AI action failed safely; no changes were applied") from exc
    if confirmation.executed:
        return _redirect_settings("Действие подтверждено")
    return _redirect_settings("Действие уже было подтверждено")


@router.post("/ai/actions/{public_id}/cancel")
def ai_action_cancel(
    public_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    action = _owned_action_or_404(db, public_id, user)
    if action.status == AIActionStatus.EXPIRED.value:
        db.commit()
        raise HTTPException(status_code=409, detail="AI action has expired")
    try:
        cancel_action(db, action=action, actor=user)
        db.commit()
    except (AIActionPermissionError, AIActionTransitionError, OperationalError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _redirect_settings("Действие отменено")
