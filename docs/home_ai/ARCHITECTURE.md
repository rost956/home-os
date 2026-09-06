# Home AI: архитектура v1

## 1. Текущее устройство Home OS

- FastAPI/Jinja-приложение, синхронный SQLAlchemy и SQLite. Сессия пользователя хранит `user_id`.
- `app/main.py` содержит большинство роутов, проверок доступа, расчётов и `commit()`; `app/services/` пока используется только для backup/export.
- Конфигурация загружается один раз из env в `app/config.py`. В production работает один web-контейнер за Caddy; данные находятся в `./data`, web filesystem read-only.
- Схема создаётся через `Base.metadata.create_all()`, простые обновления существующей SQLite выполняет `ensure_runtime_schema()`. Alembic отсутствует.
- Финансовый период и прогноз уже рассчитываются детерминированно (`expense_period_bounds`, `expense_forecast_from_lists`). Расходы могут исключаться из аналитики или прогноза.
- Доступ к расходам и покупкам бывает owner/shared с `can_edit`; доходы, планировщик и меню персональные; wishlist может быть расшарен. Чат всегда между двумя участниками. Рецепты фактически читаются всеми авторизованными пользователями, изменение разрешено владельцу.
- Локальная календарная зона сейчас фиксирована в `app/timezone.py` как `Europe/Moscow`.

Следствие: не выполнять общий рефакторинг `main.py`. Перед каждым AI-доменом выносить только переиспользуемые query/calculation/command-функции в `app/services/<domain>.py`. Сервисные команды не должны сами делать `commit()`: транзакцией управляет обычный роут или механизм подтверждения AI.

## 2. Контур компонентов

Планируемый пакет:

```text
app/ai/
  config.py          # AISettings из env, независимо от основных Settings
  client.py          # async AIClient protocol и LlamaCppClient
  errors.py          # недоступность, timeout, protocol/validation errors
  schemas.py         # Pydantic-схемы ответов и typed envelopes
  dependencies.py    # получение client/orchestrator; подмена fake в тестах
  actions.py         # создание/переходы pending actions и confirmation service
  handlers.py        # server-owned allowlist доменных write handlers
  permissions.py     # единая проверка AI-настроек и domain access
  router.py          # FastAPI endpoints Home AI
  orchestrator.py    # routing, prompt assembly, tool loop, лимиты
  tools/
    registry.py      # серверный allowlist
    finance.py
    recipes.py
    menu.py
    planner.py
    wishlist.py
    chat.py
    today.py
```

Это целевая структура, а не требование создать пустые файлы заранее. Модули появляются только в своей фазе.

Поток read-запроса:

1. Backend получает текущего пользователя только из серверной сессии.
2. Дешёвый детерминированный router/fast path выбирает домен; неоднозначный запрос классифицирует LLM по короткой enum-схеме.
3. Orchestrator выдаёт только инструменты выбранного домена и ограниченный контекст.
4. Tool вызывает domain service. Tool не принимает `user_id` от модели: `ActorContext` создаётся backend и содержит user/timezone/permissions.
5. Python выполняет запросы, суммы, проценты, периоды и прогнозы. LLM получает компактный JSON результата и только объясняет его.

Поток write-запроса:

1. Детерминированный parser или LLM возвращает строго типизированный draft.
2. Backend валидирует draft, существование ссылочных ID и текущий доступ, затем сохраняет только `AIAction(status=pending)`.
3. UI показывает редактируемую карточку предложения и отдельные «Подтвердить»/«Отменить».
4. Подтверждение обычным POST повторно проверяет owner/access/expiry и вызывает серверный handler из registry.
5. Handler и переход в `confirmed` выполняются атомарно одной транзакцией. Ошибка откатывает доменные изменения, после чего action отдельно получает `failed` и безопасное описание ошибки.

LLM-клиент не получает `Session`, command service или action-confirm API. Tool layer не содержит write-tools. Поэтому модель технически не может обойти подтверждение.

## 3. Локальный inference

- Backend: `llama-server`, OpenAI-compatible `POST /v1/chat/completions`.
- Env: `AI_ENABLED` (по умолчанию false), `AI_BASE_URL`, `AI_MODEL`, `AI_CONNECT_TIMEOUT_SECONDS`, `AI_READ_TIMEOUT_SECONDS`, `AI_MAX_TOKENS`, `AI_CONTEXT_BUDGET`, `AI_MAX_CONCURRENCY` (production default 1), при необходимости `AI_API_KEY` для локального proxy.
- `LlamaCppClient` асинхронный на `httpx.AsyncClient`; `httpx` станет явной runtime-зависимостью. Ответ всегда повторно валидируется Pydantic, даже если llama.cpp поддерживает `response_format/json_schema`.
- Ошибки connection/timeout, HTTP, пустой или невалидный JSON переводятся в типизированные ошибки. Они не падают наружу как 500 и не влияют на обычные страницы.
- Основной `/health` остаётся проверкой Home OS и БД. Отдельный авторизованный `/api/ai/health` сообщает `disabled/available/unavailable` и не участвует в healthcheck web-контейнера.
- Один process-local semaphore ограничивает inference. Текущий production использует один worker; при увеличении worker count понадобится общий limiter либо отдельная AI-очередь.
- Prompts имеют лимиты по символам/элементам, tool results пагинируются, история сокращается, тяжёлые запросы не запускаются параллельно. Простые расходы и даты обходят LLM.
- GGUF и runtime llama.cpp не входят в Git, CI и Docker build Home OS. На первом шаге llama-server управляется отдельно, а Home OS знает только URL. Для production предпочтителен systemd/отдельный контейнер на том же Raspberry Pi; доступ ограничивается host/docker network и firewall. Добавлять llama.cpp в compose можно лишь после отдельной проверки ARM64 и памяти.

Предполагаемая небольшая Qwen GGUF не зашивается в код. Замена модели не меняет tool/action contracts.

## 4. Схемы данных и транзакции

Новые таблицы следует добавлять отдельными SQLAlchemy models; `create_all()` безопасно создаст их в существующей БД без обнуления данных. Перед production-фазами сохраняется действующий backup/rollback-процесс.

### AIUserSettings

- `user_id` unique FK;
- `ai_enabled`;
- разрешения по доменам, особенно чтение finance/wishlist/chat;
- timestamps.

Default deny для чувствительных доменов. Отсутствующая запись эквивалентна отключённым разрешениям.

В PHASE 2A принят более строгий вариант: `enabled` и все domain permissions (`general`, `finance`, `recipes`, `menu`, `planner`, `wishlist`, `chat`, `today`) по умолчанию false. Persistence services выполняют только `flush()` и никогда не управляют `commit()`.

### AIAction

- непрогнозируемый public id, `owner_id`, `action_type` из server enum;
- immutable `proposed_payload_json`, optional `confirmed_payload_json`, preview;
- `status`: `pending`, `confirmed`, `cancelled`, `expired`, `failed`;
- `created_at`, `expires_at`, terminal timestamp, error code, optional result entity type/id;
- optional source conversation/message id.

Payload проходит action-specific Pydantic-схему и имеет версию. Подтверждение идемпотентно: terminal action не исполняется повторно. Изменённый пользователем payload сохраняется отдельно, чтобы audit trail не терял исходное предложение.

Начальный набор typed contracts: `expense.create`, `menu.apply`, `planner.create`. Pending action живёт 20 минут по умолчанию, допустимый максимум — 24 часа. Другие action types нельзя сохранить до добавления отдельной серверной схемы.

В PHASE 2B подтверждение получает короткоживущий UUID `claim_token` условным SQL `UPDATE`, применимым только к принадлежащему пользователю, неистёкшему `pending` action без активного claim. Cancel также использует условный update только для незахваченного pending action. Handler выбирается исключительно из server-owned registry, а пользовательский master toggle и разрешение домена проверяются повторно непосредственно перед claim. Handler и переход в `confirmed` коммитятся одной транзакцией; при ошибке транзакция откатывается, после чего action отдельно помечается `failed`. Production registry остаётся пустым до доменных фаз, поэтому ни LLM, ни клиентский payload пока не способны выполнить доменную запись.

### ExpenseMerchantRule

- `owner_id`, `expense_list_id`, нормализованный merchant key, `category_id`, timestamps/use count;
- unique `(owner_id, expense_list_id, merchant_key)`.

Правило применяется только к доступному на запись списку и существующей в нём категории. Исправленная категория записывается как правило только при явной опции «Запомнить выбор». Удалённая/недоступная категория делает правило невалидным; автоматического создания категории нет.

В PHASE 3 добавлен узкий `app/services/expenses.py`: он повторно проверяет owner/edit-share доступ к списку и принадлежность категории списку, создаёт `ExpenseItem` через `flush()` и не делает `commit()`. AI-парсер детерминированно извлекает одну сумму, merchant и `сегодня`/`вчера` в `Europe/Moscow`; известное правило merchant→category и небольшой набор однозначных merchant hints обходятся без inference. Только если fast path не выбрал категорию, LLM получает текст и ограниченный server-built список `{id, name}` категорий выбранного writable list. Ответ с чужим ID, `ambiguous=true` или confidence ниже 0.85 не создаёт action. Даже удачный fallback создаёт исключительно `AIAction(pending)`, а реальная трата появляется только через server-owned `expense.create` handler после confirm.

### Chat permissions и AI history

- Разрешение чтения чата хранится per-user/per-thread. Retrieval разрешён, только если согласие активно у обоих участников потока.
- В фазе чатов создаётся локальный SQLite FTS5 индекс с фильтрацией по разрешённым thread IDs; при отсутствии FTS5 используется ограниченный `LIKE`, без vector DB.
- General AI conversations/messages персональны. Не хранить копии retrieved chat context и полные internal prompts; action audit хранит только структурированные данные, необходимые для проверки.

## 5. Доменная политика

### Expenses и finance

- Fast path: нормализация merchant -> rule -> простой parser суммы/дат `сегодня/вчера` -> draft. LLM вызывается только при неоднозначности.
- Список расхода обязателен. Если нет единственного очевидного writable list, карточка требует выбора пользователя.
- LLM может предложить только ID категории из bounded списка выбранного writable list.
- Finance tools используют доступные списки и действующие флаги `include_in_analytics/include_in_forecast`; доходы только владельца.
- Все денежные значения возвращаются backend как `Decimal`/готовые агрегаты. Ответ о покупке показывает исходные суммы и сценарий, но не даёт гарантий.

### Recipes и menu

- Сначала SQL-фильтры по времени/стоимости/ингредиентам, затем ранжирование небольшого набора.
- Результат содержит реальные Recipe IDs. Доступ повторяет явно зафиксированную продуктовую политику существующего UI; несогласованности owner/global read необходимо закрыть тестами до AI tools.
- Menu proposal ссылается только на существующие Recipe IDs и учитывает недавние MenuItem. Применение недели является одним versioned pending action и выполняется транзакционно.

### Planner, wishlist, Today

- Planner draft использует `Europe/Moscow` через существующий timezone module; распознанные дата/время всегда видны до подтверждения.
- Wishlist tool читает только собственный или явно расшаренный список согласно текущим helper-проверкам. Финансовый сценарий рассчитывает backend.
- Today AI summary строится поверх детерминированного snapshot существующей страницы. Генерация on-demand/с коротким cache; отсутствие AI оставляет обычную страницу полностью рабочей.

### Chats

- Текст БД является недоверенными данными и помещается в отделённый context block с инструкцией не выполнять содержащиеся в нём команды.
- Сначала permission check и retrieval, затем только несколько релевантных фрагментов. Никаких attachments, URL fetch или полной истории в v1.

## 6. Security invariants

- Нет raw SQL, shell, файловых путей, URL fetch и произвольных tool/action names от модели.
- Tool/action registries задаются Python enum/mapping. Аргументы валидируются Pydantic и дополнительно проверяются domain service.
- Ownership/share permission проверяется при чтении, создании draft и повторно при confirm; ID пользователя от клиента/LLM игнорируется.
- Сохранённые тексты, tool output и retrieved messages никогда не становятся system/developer instructions.
- Ответ v1 отображается как plain text. При будущем Markdown потребуется строгая sanitization; Jinja autoescape сохраняется.
- В логи не попадают prompt, сообщения чатов, токены, ключи или полный payload расходов. Допустимы request id, duration, model, domain, error code и token counts.
- Время жизни pending action ограничено. Cancel/expire/confirm доступны только владельцу. Confirmation endpoint защищён текущей same-origin политикой; при появлении публичного API потребуется отдельный CSRF/token design.
- AI backend не публикуется через Caddy и не получает доступ к `data/`, SQLite или uploads.

## 7. Тестовая стратегия

- `AIClient` protocol и `FakeAIClient` со сценариями success/timeout/unavailable/invalid payload; dependency override вместо реальной модели.
- Unit: config validation, schema parsing, router selection, prompt limits, merchant normalization/rules, status machine, expiry/idempotency.
- Integration через существующий `TestClient` и временную SQLite: owner/share permissions, double confirm, rollback failure, category ID allowlist, planner timezone, menu Recipe IDs, chat consent обоих участников.
- Security: prompt injection remains data, неизвестный tool/action отклоняется, user-supplied `user_id` не влияет, write невозможен через read tool.
- CI не скачивает GGUF и не обращается к сети/llama-server. Реальный inference проверяется только отдельным manual smoke на Raspberry Pi.

## 8. Этапы реализации

0. **Audit & design**: этот документ и `STATUS.md`.
1. **Foundation**: config, async client protocol/llama client, fake, базовые schemas/errors, AI-only health и tests. Без models/tools/UI.
2. **Actions & permissions** разбить для контроля риска:
   - 2A: models/settings/status machine/action service и migrations/tests;
   - 2B: confirmation/cancel endpoints, минимальная карточка и permission UI/tests.
3. **Natural-language expenses**: точечный expense service, merchant rules, fast path, category proposal и confirmed create.
4. **Finance intelligence** разбить:
   - 4A: вынести и покрыть тестами deterministic finance snapshots;
   - 4B: read tools, вопросы, объяснение сравнений/прогноза.
5. **Recipes**: read search/recommendations по реальным IDs.
6. **Menu**: proposal и подтверждаемое применение.
7. **Planner**: read + natural-language pending action.
8. **Wishlist + finance**: deterministic affordability scenario и объяснение.
9. **Chat retrieval** разбить:
   - 9A: consent model/UI и FTS5/fallback index;
   - 9B: permission-bound retrieval tool и injection tests.
10. **General chat**: bounded domain router/orchestrator, personal conversation UI.
11. **Today/in-page UX**: on-demand summary и контекстные точки входа.
12. **Hardening**: end-to-end fake tests, security/performance review, Pi llama-server runbook, failure drills.

Дробление фаз 2, 4 и 9 уменьшает размер изменений и отдельно проверяет наиболее рискованные persistence, finance и privacy boundaries.

## 9. Принятые ограничения v1

- Нет облачных LLM, embeddings/vector DB, web browsing, URL fetching, shell/code execution, анализа вложений и автоматической записи.
- Нет фоновой генерации без действия пользователя, autonomous agents и нескольких одновременных inference.
- Модель не источник истины для прав, ID, денег, дат или успешности операции.
- Исправление не связанных с Home AI проблем остаётся вне этих фаз, если оно не блокирует конкретный AI contract.
