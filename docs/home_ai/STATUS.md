# Home AI: статус

## Текущая фаза

**PHASE 6.6 — завершена 2026-09-07 как follow-up natural-language expenses.** Production expense path принимает свободный порядок слов, извлекает деньги и даты backend-кодом, использует компактную semantic LLM-схему только при необходимости и сохраняет исключительно подтверждаемый pending draft. Добавлен read-only held-out evaluator из 34 фраз для реального Raspberry Pi. Следующая доменная фаза остаётся только PHASE 7 (Planner); она не начата.

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
- Production handler registry содержит только server-owned handlers для `expense.create` и `menu.apply`; LLM не получает доступ к ним, к `Session` или к прямой записи в БД.
- Runtime migration добавляет `claim_token`/`claimed_at` в ранее созданную `ai_actions`, сохраняя существующие записи.
- Добавлена таблица `expense_merchant_rules`: правило всегда scoped по владельцу, списку трат и реально существующей категории; новые категории Home AI не создаёт.
- Добавлен узкий expense command service без собственного `commit()`: он проверяет owner/edit-share доступ и категорию выбранного списка как при draft, так и при confirm.
- Natural expense pre-parser независимо от порядка слов извлекает одну сумму, валютный суффикс/разделитель тысяч, `сегодня`/`вчера`/`позавчера` или ISO-дату. Отсутствующая дата явно означает сегодняшний день в `Europe/Moscow`; время суток остаётся описанием.
- Несколько сумм не объединяются: текущая single-action архитектура возвращает просьбу ввести расходы отдельно. Отсутствующая, нулевая, отрицательная или чрезмерная сумма не дополняется догадкой модели.
- Известные merchant rules и однозначные продуктовые/топливные hints обходят LLM. Неоднозначный fallback получает фиксированные backend-факты и только категории выбранного writable list через компактный expense-only contract с `thinking=false`.
- Низкая confidence, ambiguity или чужой category ID создают pending draft с unresolved category; UI показывает неопределённость и требует owner-scoped выбор до confirm. До подтверждения `ExpenseItem` отсутствует.
- `expense.create` стал единственным production handler: он создаёт `ExpenseItem` только после confirm, а исправленная пользователем категория может создать или обновить правило лишь с явным «Запомнить выбор».
- Добавлен read-only `FinanceSnapshot`: доходы, расходы, баланс, накопления, категории, крупнейшие расходы, лимиты, регулярные платежи, прогноз и сравнение с предыдущим сопоставимым периодом рассчитываются Python-кодом без LLM.
- Сохранена финансовая модель доступа: собственные и расшаренные списки участвуют в расходах, а доходы, лимиты и регулярные платежи выбираются только для текущего пользователя. Опциональный фильтр списка применяется после server-side проверки доступных списков.
- Обычные страницы `/finance` и `/expenses/analytics` используют тот же service layer; существующие формулы периода, прогноза и отображаемые template contracts сохранены.
- Добавлены шесть Pydantic-валидированных read-only finance tools: summary, period comparison, category breakdown, largest expenses, budget status и forecast. Все числовые значения берутся из `FinanceSnapshot`; инструменты не принимают `user_id` и не выполняют записи.
- Частые финансовые вопросы выбирают один tool локально; неоднозначный вопрос использует короткий finance-only LLM selector. Модель затем получает только ограниченный результат выбранного tool и формирует краткое объяснение без пересчёта сумм.
- Endpoint `POST /api/ai/finance/questions` требует активного master toggle и finance permission. Timeout, disabled/unavailable backend, invalid JSON, неизвестные/write tools и невалидные arguments возвращают безопасные HTTP-ошибки без изменения БД.
- Endpoint `POST /api/ai/recipes/questions` требует recipes permission и использует только owner-scoped `Recipe` records. SQL-фильтры выполняются до модели, shortlist ограничен восемью фактическими Recipe ID; `get_recipe_details` не раскрывает чужой или отсутствующий ID, а IDs из ответа модели валидируются по tool result. Menu context явно отмечен как planned menu, а cooking history берётся только из остановленных cooking timers.
- Добавлен `POST /api/ai/menu/proposals` и одноимённая форма Home AI. Backend сначала применяет фильтры рецептов и исключает реально запланированные недавние `MenuItem`; LLM получает не более восьми owner-scoped кандидатов и может вернуть только их ID и даты запрошенного периода.
- Предложение сохраняется только как versioned pending `menu.apply` action: до confirm `MenuItem` не создаётся. На подтверждении handler ещё раз проверяет владельца рецептов и точный набор конфликтов, добавляет новые элементы транзакционно, не удаляет и не перезаписывает существующее меню. Повторный confirm идемпотентен.
- Карточка pending action показывает блюда, даты, приём пищи и текущие коллизии. Конфликтные записи не скрываются: подтверждение явно добавляет новое блюдо рядом, а изменение состава конфликтов после draft безопасно завершает action без записи.
- PHASE 6.5 закрепила `llama.cpp v0.4.0` source build под ARM64 и отдельного непривилегированного пользователя `home-ai`; installer сохраняет модели и существующий runtime env, не трогает Home OS data и не запускает сервис без явного действия оператора.
- Добавлены systemd unit и validated launcher: runtime/model/working directory задаются через `/etc/home-ai/llama-server.env`, API key хранится отдельным файлом, Web UI/slots отключены, включён `Restart=on-failure`, journal logging, один inference slot и лимиты 5/6 GiB.
- Compose `web` получает Linux `host-gateway`, но llama-server не публикуется портом Docker или через Caddy. Runbook требует bind только к фактическому gateway address и firewall allow только для compose subnet/interface.
- Существующий AI client получил один общий переключатель `AI_ENABLE_THINKING` (production default false) и передаёт его через поддерживаемый llama.cpp `chat_template_kwargs`, чтобы Qwen3.5 не расходовала короткий Pi context на reasoning перед schema-ответом.
- Model helper использует `.partial`, предварительную проверку места, Content-Length, SHA-256 и atomic no-clobber rename. Зафиксированы кандидаты Qwen3.5-4B Q4_K_M (первый) и Qwen3.5-2B Q4_K_M (fallback), без GGUF/mmproj в Git или CI.
- Manual smoke проверяет systemd, health, models/chat, container reachability, русский ответ, structured JSON и безопасные expense/finance/recipe/menu сценарии с latency/token/RSS/RAM metrics. Отдельный benchmark прогоняет семь одинаковых prompts для 2B/4B и не объявляет победителя без реального запуска на Pi.
- PHASE 6.6 содержит 34 held-out natural expense фразы отдельно от production prompt и read-only evaluator, вызывающий фактический production preparation path без создания action/expense.

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
- `app/ai/finance.py`
- `app/ai/menu.py`
- `app/ai/recipes.py`
- `app/ai/tools/__init__.py`
- `app/ai/tools/finance.py`
- `app/ai/tools/recipes.py`
- `app/templates/ai_settings.html`
- `app/templates/_ai_action_card.html`
- `app/templates/ai_settings.html`
- `app/templates/base.html`
- `app/static/style.css`
- `app/services/expenses.py`
- `app/services/finance.py`
- `app/services/menu.py`
- `tests/test_ai_foundation.py`
- `tests/test_ai_actions.py`
- `tests/test_ai_confirmation.py`
- `tests/test_ai_expenses.py`
- `tests/test_finance_snapshot.py`
- `tests/test_ai_finance.py`
- `tests/test_ai_recipes.py`
- `tests/test_ai_menu.py`
- `ops/home-ai/llama-server.service`
- `ops/home-ai/llama-server.env.example`
- `scripts/install_llama_cpp.sh`
- `scripts/download_home_ai_model.sh`
- `scripts/run_llama_server.sh`
- `scripts/wait_llama_server.sh`
- `scripts/home_ai_runtime_smoke.py`
- `scripts/home_ai_benchmark.py`
- `docs/home_ai/RASPBERRY_PI_RUNTIME.md`
- `tests/test_home_ai_runtime_assets.py`
- `.gitignore`, `.dockerignore`, `README.md`
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
- PHASE 4A targeted lint: `ruff check app/services/finance.py app/main.py tests/test_finance_snapshot.py tests/test_expenses.py tests/test_ai_expenses.py` — успешно.
- PHASE 4A targeted tests: `pytest tests/test_finance_snapshot.py tests/test_expenses.py tests/test_ai_expenses.py -q` — `19 passed`.
- Числовыми проверками покрыты пустой период, только доходы, только расходы, нулевые категории, пользовательский период и переход года, текущий неполный/предыдущий сопоставимый период, лимиты, регулярные платежи, прогноз без истории, исключённые из аналитики расходы и изоляция owner/shared данных. Дополнительно обе существующие финансовые страницы проверяются через HTTP с ожидаемыми суммами.
- PHASE 4B targeted lint: `ruff check app/ai app/services/finance.py tests/test_ai_finance.py tests/test_finance_snapshot.py` — успешно.
- PHASE 4B targeted tests: `pytest tests/test_ai_foundation.py tests/test_ai_actions.py tests/test_ai_confirmation.py tests/test_ai_expenses.py tests/test_ai_finance.py tests/test_finance_snapshot.py -q` — `60 passed`.
- Покрыты выбор каждого finance tool, конкретный месяц, пустой snapshot, сравнение периодов, категории, крупнейшие расходы, недоступный forecast, почти исчерпанный и превышенный лимит, finance permission, два владельца, отсутствие утечки и записей, invalid JSON, неизвестный/write tool, подмена `user_id`, timeout, unavailable и disabled backend.
- PHASE 5 targeted lint: `ruff check app/ai/recipes.py app/ai/tools/recipes.py app/ai/router.py tests/test_ai_recipes.py` — успешно.
- PHASE 5 targeted tests: `pytest tests/test_ai_recipes.py -q` — `19 passed`.
- Покрыты title/ingredient/time/cost/servings/tags filters, пустой результат, owner isolation, отсутствующий/чужой Recipe ID, ограниченный shortlist, recent menu context, cooking timer history, invalid JSON, неизвестный/write tool, подмена `user_id`, timeout, unavailable, disabled backend и отсутствие записей.
- PHASE 6 targeted lint: `ruff check app/ai/menu.py app/ai/router.py app/services/menu.py tests/test_ai_menu.py` — успешно.
- PHASE 6 targeted tests: `pytest tests/test_ai_menu.py tests/test_ai_confirmation.py -q` — `27 passed`.
- Покрыты меню на один день и неделю, полный период, только существующие/короткие owner-scoped Recipe ID, исключение последних двух недель, time/cost/servings/ingredient filters, пустой shortlist без вызова модели, конфликт существующего MenuItem, форма и preview card, permission, чужие/несуществующие IDs, invalid JSON, timeout, unavailable/disabled backend, cancel, confirm, double confirm и отсутствие записи до confirm.
- Реальные LLM/GGUF не запускались и в автоматические тесты входить не будут.
- PHASE 6.5 targeted tests: `pytest tests/test_home_ai_runtime_assets.py -q` — `7 passed`; проверены env/systemd/Compose templates, отсутствие GGUF, pinned source policy и mock modes smoke/benchmark без сети или модели.
- PHASE 6.6 Home AI regression: parser/expense/action/client/finance/recipes/menu/diagnostic tests — `159 passed`; полный `pytest -q` — `198 passed, 1 skipped` из-за ограничения Windows symlink. Held-out набор содержит 34 фразы и не входит в production prompt; CI использует только deterministic parser и `FakeAIClient`.
- Финальная локальная проверка: `ruff check .` — успешно; `pytest -q` — `132 passed, 1 skipped` (Windows symlink restriction); `bash -n` четырёх новых shell scripts, `docker compose config --quiet` и `git diff --check` — успешно. Реальный inference остаётся только ручной Pi-проверкой.

## Известные риски и вопросы

- `app/main.py` всё ещё объединяет routes/business logic/transactions. Нужны только небольшие domain extractions по мере подключения AI.
- Фактическая политика чтения рецептов шире owner-only, а меню персонально. Перед recipe tools требуется закрепить ожидаемое поведение тестами.
- Chat AI требует нового согласия обоих участников; текущая проверка участия в чате сама по себе недостаточна.
- Текущий timezone — `Europe/Moscow`, не per-user. V1 использует его как источник истины.
- Runtime architecture и два кандидата подготовлены, но `llama-server`/GGUF ещё не запускались на production Raspberry Pi. Финальный выбор 4B против 2B делается только по manual benchmark; модель не должна попадать в Git или CI.
- Ошибка production rollback с директорией `Caddyfile` и `tar: stdout: write error` исправлена в `73ddebf`; исправление ещё должно пройти обычный production deployment после merge.
- Production зарегистрировал `expense.create` и `menu.apply`; planner handler отсутствует до PHASE 7.
- Атомарный claim покрыт интеграционными последовательными запросами; отдельный конкурентный stress test на production SQLite остаётся частью финального hardening.

## Точная следующая фаза

**PHASE 7 — Planner, и только она:**

1. Добавить owner-scoped read contour планировщика и natural-language pending action.
2. Использовать существующий confirmation flow и `Europe/Moscow` для даты/времени.
3. Не начинать wishlist или общий multi-domain orchestrator.
