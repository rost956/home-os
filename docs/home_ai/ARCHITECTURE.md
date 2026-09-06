# Home AI: архитектура v1

## 1. Текущее устройство Home OS

- FastAPI/Jinja-приложение, синхронный SQLAlchemy и SQLite. Сессия пользователя хранит `user_id`.
- `app/main.py` содержит большинство роутов, проверок доступа и `commit()`; переиспользуемые expense-команды и deterministic finance snapshots вынесены в `app/services/` рядом с backup/export.
- Конфигурация загружается один раз из env в `app/config.py`. В production работает один web-контейнер за Caddy; данные находятся в `./data`, web filesystem read-only.
- Схема создаётся через `Base.metadata.create_all()`, простые обновления существующей SQLite выполняет `ensure_runtime_schema()`. Alembic отсутствует.
- Финансовый период, агрегаты и прогноз рассчитываются детерминированно в `app/services/finance.py`. Расходы могут исключаться из аналитики или прогноза.
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
- Env: `AI_ENABLED` (по умолчанию false), `AI_BASE_URL`, `AI_MODEL`, `AI_CONNECT_TIMEOUT_SECONDS`, `AI_READ_TIMEOUT_SECONDS`, `AI_MAX_TOKENS`, `AI_CONTEXT_BUDGET`, `AI_MAX_CONCURRENCY` (production default 1), `AI_ENABLE_THINKING` (по умолчанию false для коротких schema-задач), при необходимости `AI_API_KEY` для локального proxy.
- `LlamaCppClient` асинхронный на `httpx.AsyncClient`; `httpx` станет явной runtime-зависимостью. Ответ всегда повторно валидируется Pydantic, даже если llama.cpp поддерживает `response_format/json_schema`.
- Ошибки connection/timeout, HTTP, пустой или невалидный JSON переводятся в типизированные ошибки. Они не падают наружу как 500 и не влияют на обычные страницы.
- Основной `/health` остаётся проверкой Home OS и БД. Отдельный авторизованный `/api/ai/health` сообщает `disabled/available/unavailable` и не участвует в healthcheck web-контейнера.
- Один process-local semaphore ограничивает inference. Текущий production использует один worker; при увеличении worker count понадобится общий limiter либо отдельная AI-очередь.
- Prompts имеют лимиты по символам/элементам, tool results пагинируются, история сокращается, тяжёлые запросы не запускаются параллельно. Простые расходы и даты обходят LLM.
- GGUF и runtime llama.cpp не входят в Git, CI и Docker build Home OS. В PHASE 6.5 выбран отдельный host systemd service на Raspberry Pi: непривилегированный `home-ai` запускает закреплённый source build `llama.cpp`, модель лежит в `/opt/home-ai/models`, а настройки — в `/etc/home-ai`. Home OS знает только OpenAI-compatible URL и alias модели.
- Linux `web` получает `host.docker.internal` через Compose `host-gateway`; `llama-server` слушает только фактический host gateway address, не `localhost`, `0.0.0.0` или LAN/public address. Host firewall разрешает порт 8081 только loopback и compose subnet/interface, `/v1` дополнительно защищён локальным API key. Caddy route и Docker port publishing отсутствуют.
- Pi 5 8 GB profile ограничен одним slot, context 4096, batch 256/ubatch 128, `MemoryHigh=5G` и `MemoryMax=6G`. Все параметры остаются изменяемыми через `/etc/home-ai/llama-server.env`, но wrapper запрещает wildcard bind, несколько slots, context больше 8192 и чрезмерный batch. Ошибка AI остаётся нефатальной для Home OS и обычного `/health`.

Первый кандидат для измерения на Pi — Qwen3.5-4B Q4_K_M, fallback — Qwen3.5-2B Q4_K_M. Репозиторий хранит только закреплённые source URL/SHA-256 и manual download helper, но не GGUF. Победитель не выбирается без одинакового smoke/benchmark на реальном Pi; замена модели не меняет tool/action contracts.

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

В PHASE 3 добавлен узкий `app/services/expenses.py`: он повторно проверяет owner/edit-share доступ к списку и принадлежность категории списку, создаёт `ExpenseItem` через `flush()` и не делает `commit()`. PHASE 6.6 заменила positional parser на гибридный production path. Backend независимо от порядка слов извлекает ровно одну сумму (включая пробелы, десятичную часть и русские обозначения рублей), `сегодня`/`вчера`/`позавчера` либо ISO-дату и разрешает относительный день через текущую `Europe/Moscow`. При отсутствии даты явно фиксируется default «сегодня»; слова «утром», «вечером» и описательные обороты датой не становятся. Несколько сумм безопасно отклоняются с просьбой вводить расходы отдельно, потому что текущий action contract представляет один расход.

Остаток фразы сохраняется как описание. Owner-scoped merchant rule и однозначные продуктовые/топливные hints остаются deterministic fast path. Только для семантической неоднозначности компактный expense-only prompt получает исходный текст как недоверенные данные, уже зафиксированные backend сумму/дату и разрешённые `{id, name}` категорий. Модель не возвращает сумму или дату, не создаёт категорию и не выполняет write; `thinking=false` задаётся на уровне этого schema-request. Чужой/выдуманный ID, ambiguity или confidence ниже 0.85 оставляет `category_id=null` в pending draft. UI требует выбрать доступную категорию до confirm. `expense.create` handler не принимает unresolved payload и повторно проверяет доступ, поэтому реальная трата по-прежнему появляется только после явного подтверждения. Merchant rule обучается только при «Запомнить выбор», причём в payload попадает распознанный merchant/rule key, а не произвольная многословная фраза.

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

В PHASE 4A добавлен общий `FinanceSnapshot`. Он собирается только backend-сервисом и содержит фактические доходы/расходы, баланс, категории и причины изменения, предыдущий полный и сопоставимый периоды, крупнейшие расходы, лимиты, регулярные платежи и существующий прогноз. Расходы читаются из собственных и явно расшаренных списков пользователя; доходы, лимиты и регулярные платежи остаются owner-only. Границы периода задаются пользовательским `expense_period_start_day`, календарные даты интерпретируются через `Europe/Moscow`. Snapshot не делает записей и не вызывает LLM; PHASE 4B читает его структурированный результат.

В PHASE 4B добавлен отдельный finance-only flow `POST /api/ai/finance/questions`. Частые формулировки маршрутизируются локально, неоднозначные — одним коротким schema-validated вызовом модели. Затем выполняется ровно один инструмент из server-owned allowlist: summary, comparison, category breakdown, largest expenses, budget status или forecast. Инструменты не принимают `user_id`, получают пользователя из backend-сессии и возвращают Pydantic-схемы поверх `FinanceSnapshot`. В модель передаются только ограниченные агрегаты (не более пяти отдельных крупнейших расходов), после чего второй вызов лишь объясняет готовый результат. General orchestrator, write-tools и прямой SQL в этом контуре отсутствуют.

### Recipes и menu

- В PHASE 5 добавлен отдельный owner-only read contour `POST /api/ai/recipes/questions`: это намеренно приватнее устаревшего UI read-path. SQL-фильтры по названию, ингредиентам, времени, стоимости, порциям и тегам выполняются до модели; shortlist ограничен восемью реальными `Recipe.id`.
- Разрешены только `search_recipes`, `get_recipe_details`, `get_recent_menu_context` и `recommend_recipes`. Tool не принимает `user_id`, не делает записи и не создаёт recipes/menu actions. Детали чужого или отсутствующего ID возвращаются как `found=false` без утечки данных; recipe IDs из LLM explanation валидируются как подмножество фактического tool result.
- История cooking timer используется как единственное свидетельство приготовления. `MenuItem` возвращается отдельно как контекст запланированного меню и не выдаётся за факт готовки.
- В PHASE 6 menu proposal получает только owner-scoped shortlist до восьми реальных Recipe ID после детерминированных time/cost/servings/tag/ingredient filters. Недавние `MenuItem` исключаются лишь при явном запросе без повторов; отсутствие истории не интерпретируется как факт готовки.
- Модель может подготовить только структурированные `{plan_date, meal_name, recipe_id, display_title, rationale}` в пределах запрошенного периода. Backend валидирует ID как подмножество shortlist, даты и уникальность date/meal slots; многодневный запрос должен содержать все дни периода. Ни один `MenuItem` не создаётся до pending `menu.apply` action и явного confirm.
- Перед confirm menu handler повторно проверяет owner-only рецепты и набор занятых slots. Известные конфликты показываются в карточке; существующие `MenuItem` никогда не удаляются или не перезаписываются, а подтверждение добавляет новое блюдо рядом. Изменившийся после draft набор конфликтов безопасно отклоняет action. Handler и status transition остаются одной транзакцией, повторный confirm идемпотентен.

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
6.6. **Natural expense follow-up**: свободный порядок слов, deterministic facts, компактная semantic интерпретация и pending draft с явной неопределённостью.
7. **Planner**: read + natural-language pending action.
8. **Wishlist + finance**: deterministic affordability scenario и объяснение.
9. **Chat retrieval** разбить:
   - 9A: consent model/UI и FTS5/fallback index;
   - 9B: permission-bound retrieval tool и injection tests.
10. **General chat**: bounded domain router/orchestrator, personal conversation UI.
11. **Today/in-page UX**: on-demand summary и контекстные точки входа.
12. **Hardening**: end-to-end fake tests, security/performance review, повторная production Pi-проверка и failure drills.

Инфраструктурная **PHASE 6.5** выполнена между Menu и Planner и не меняет этот
порядок доменных фаз: host runtime/systemd, model download helper, закрытый
host-gateway доступ, smoke/benchmark и Pi runbook готовы; production установка и
выбор 4B/2B остаются явными ручными операциями.

Read-only real-model проверка PHASE 6.6 запускается внутри production web-контейнера и использует тот же `prepare_expense_draft`, что HTTP endpoint, но не вызывает `create_pending_action` и завершает DB session rollback:

```bash
sudo docker compose exec -T web python -B scripts/home_ai_expense_evaluation.py \
  --username "USERNAME" \
  --expense-list-id EXPENSE_LIST_ID
```

Набор из 34 held-out фраз находится только в diagnostic/test module и не включён в production prompt.

Дробление фаз 2, 4 и 9 уменьшает размер изменений и отдельно проверяет наиболее рискованные persistence, finance и privacy boundaries.

## 9. Принятые ограничения v1

- Нет облачных LLM, embeddings/vector DB, web browsing, URL fetching, shell/code execution, анализа вложений и автоматической записи.
- Нет фоновой генерации без действия пользователя, autonomous agents и нескольких одновременных inference.
- Модель не источник истины для прав, ID, денег, дат или успешности операции.
- Исправление не связанных с Home AI проблем остаётся вне этих фаз, если оно не блокирует конкретный AI contract.
