# Home AI: статус

## Текущая фаза

**PHASE 3 — завершена 2026-09-06.** Реализованы natural-language drafts расходов, merchant/category rules, fast path и подтверждаемое создание расхода. PHASE 4A не начиналась.

Исходная точка: `main` / `a791e8b` (`Prepare Home OS for production`). Рабочее дерево до фазы было чистым.

## Что готово

- Описаны фактические границы FastAPI/Jinja, SQLAlchemy/SQLite, session auth, Docker/Caddy и runtime schema updates.
- Зафиксированы async llama.cpp client, graceful degradation и отдельный AI health, не влияющий на `/health`.
- Спроектированы allow-listed read tools, server-owned actor context и детерминированные расчёты.
- Спроектированы `AIUserSettings`, `AIAction`, merchant rules, chat consent и general AI history.
- Зафиксирован обязательный pending/confirm/audit flow без прямой записи LLM.
- План разбит на небольшие фазы; рискованные фазы 2, 4 и 9 разделены на A/B.
- Добавлены env-настройки Home AI, выключенные по умолчанию, async OpenAI-compatible llama.cpp client и `FakeAIClient`.
- Добавлен авторизованный non-critical endpoint `GET /api/ai/health`; основной `/health` не изменён.
- Добавлены таблицы `ai_user_settings` и `ai_actions`; существующие таблицы и данные не изменяются.
- Добавлены типизированные contracts `expense.create`, `menu.apply`, `planner.create`, owner-scoped lookup, expiry и идемпотентные terminal transitions.
- Persistence services делают `flush()`, но не `commit()`, оставляя транзакцию будущему confirmation flow.
- Permission UI сохраняет только настройки текущего пользователя; master toggle и разрешение конкретного домена повторно проверяются backend при подтверждении.
- Confirmation получает action только в owner scope, атомарно захватывает pending-запись через `claim_token` и выполняет handler вместе с terminal transition в одной транзакции; cancel использует условный update и не может перезаписать уже захваченное действие.
- Повторное подтверждение уже выполненного action не вызывает handler второй раз; cancel также идемпотентен.
- Ошибка handler откатывает доменные изменения, после чего action отдельной транзакцией получает `failed` и безопасный error code.
- Production handler registry пока пуст: LLM и preview flow не могут напрямую создать расход, меню или planner item до соответствующей доменной фазы.
- Runtime migration добавляет `claim_token`/`claimed_at` в ранее созданную `ai_actions`, сохраняя существующие записи.
- Добавлена таблица `expense_merchant_rules`: правило всегда scoped по владельцу, списку трат и реально существующей категории; новые категории Home AI не создаёт.
- Добавлен узкий expense command service без собственного `commit()`: он проверяет owner/edit-share доступ и категорию выбранного списка как при draft, так и при confirm.
- Простые строки расходов разбираются локально: одна сумма, merchant, `сегодня`/`вчера` в `Europe/Moscow`; известные merchant rules и однозначные продуктовые/топливные hints обходят LLM.
- Неоднозначный fallback получает только ограниченный список категорий выбранного writable list. Низкая confidence, ambiguity или чужой category ID не создают pending action.
- `expense.create` стал единственным production handler: он создаёт `ExpenseItem` только после confirm, а исправленная пользователем категория может создать или обновить правило лишь с явным «Запомнить выбор».

## Изменённые файлы

- `docs/home_ai/ARCHITECTURE.md`
- `docs/home_ai/STATUS.md`
- `app/ai/__init__.py`
- `app/ai/config.py`
- `app/ai/errors.py`
- `app/ai/schemas.py`
- `app/ai/client.py`
- `app/ai/dependencies.py`
- `app/ai/router.py`
- `app/ai/types.py`
- `app/ai/action_schemas.py`
- `app/ai/actions.py`
- `app/ai/handlers.py`
- `app/ai/permissions.py`
- `app/ai/expenses.py`
- `app/templates/ai_settings.html`
- `app/templates/_ai_action_card.html`
- `app/templates/base.html`
- `app/static/style.css`
- `app/services/expenses.py`
- `tests/test_ai_foundation.py`
- `tests/test_ai_actions.py`
- `tests/test_ai_confirmation.py`
- `tests/test_ai_expenses.py`
- `app/models.py`
- `app/main.py`, `requirements.txt`, `requirements-dev.txt`, `docker-compose.yml`, `.env.example`

## Тесты

- Targeted lint: `ruff check app/ai app/main.py tests/test_ai_foundation.py` — успешно.
- Targeted tests: `pytest tests/test_ai_foundation.py -q` — `5 passed`.
- Покрыты disabled config/health, valid и invalid structured responses, timeout, unavailable backend, privacy health endpoint и `FakeAIClient`.
- PHASE 2A targeted lint: `ruff check app/ai app/models.py app/main.py tests/test_ai_foundation.py tests/test_ai_actions.py` — успешно.
- PHASE 2A targeted tests: `pytest tests/test_ai_foundation.py tests/test_ai_actions.py -q` — `12 passed`.
- Покрыты default-deny settings, отсутствие implicit commit, строгие payload schemas, owner isolation, expiry, terminal idempotency, audit payload и создание отсутствующих AI-таблиц без потери пользователя.
- PHASE 2B targeted lint: `ruff check app/ai app/models.py app/main.py tests/test_ai_foundation.py tests/test_ai_actions.py tests/test_ai_confirmation.py` — успешно.
- PHASE 2B targeted tests: `pytest tests/test_ai_foundation.py tests/test_ai_actions.py tests/test_ai_confirmation.py -q` — `25 passed`.
- Покрыты permission UI, same-origin rejection, owner-only pending UI/confirm/cancel, HTML escaping, пустой production registry, backend permission recheck, double confirm/cancel, запрет cancel для claimed action, expiry, invalid payload, rollback failure и upgrade старой AI-схемы без потери action.
- PHASE 3 targeted lint: `ruff check app/ai app/services/expenses.py app/models.py tests/test_ai_foundation.py tests/test_ai_actions.py tests/test_ai_confirmation.py tests/test_ai_expenses.py` — успешно.
- PHASE 3 targeted tests: `pytest tests/test_ai_foundation.py tests/test_ai_actions.py tests/test_ai_confirmation.py tests/test_ai_expenses.py -q` — `33 passed`.
- Покрыты `Лента 1840 вчера`, `Бензин 2600 сегодня`, `Пятёрочка 734`, `5800 xteink`, fast path без LLM, low confidence, ограничение LLM только категориями владельца, сохранённое правило, создание и обновление merchant rule, ручная корректировка категории, idempotent confirm и запрет чужой категории.
- Реальные LLM/GGUF не запускались и в автоматические тесты входить не будут.

## Известные риски и вопросы

- `app/main.py` всё ещё объединяет routes/business logic/transactions. Нужны только небольшие domain extractions по мере подключения AI.
- Фактическая политика чтения рецептов шире owner-only, а меню персонально. Перед recipe tools требуется закрепить ожидаемое поведение тестами.
- Chat AI требует нового согласия обоих участников; текущая проверка участия в чате сама по себе недостаточна.
- Текущий timezone — `Europe/Moscow`, не per-user. V1 использует его как источник истины.
- `llama-server` на Raspberry Pi и конкретный Qwen GGUF ещё не выбраны/не проверены. Модель не должна попадать в Git или CI.
- Production CD run `33914549000` завершился failure после запуска контейнеров и попытки rollback (`tar: stdout: write error`). Это существующая операционная проблема вне PHASE 0; перед будущим AI production deploy её нужно устранить и проверить состояние сервера.
- Production зарегистрировал только `expense.create`; menu и planner handlers по-прежнему отсутствуют до своих доменных фаз.
- Атомарный claim покрыт интеграционными последовательными запросами; отдельный конкурентный stress test на production SQLite остаётся частью финального hardening.

## Точная следующая фаза

**PHASE 4A — Deterministic finance snapshots, и только она:**

1. Вынести и покрыть targeted-тестами deterministic snapshots расходов, доходов, лимитов и прогноза без LLM.
2. Зафиксировать owner/shared access semantics финансовых данных в service contracts.
3. Не добавлять finance read tools, чат, prompts или новые write flows: это остаётся PHASE 4B.
