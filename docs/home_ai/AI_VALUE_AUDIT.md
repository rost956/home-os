# Home AI: аудит фактической AI-ценности

Дата аудита: 2026-09-07. Проверенный commit: `6e5c69f`.

Область: уже реализованные expense NL input, finance questions/explanations,
recipe recommendations, menu proposals и AI actions/confirmation. PHASE 7 не
рассматривается. Код не изменялся.

## Методика и важное ограничение

Аудит прослеживает production control flow от пользовательского текста до LLM,
детерминированного инструмента и возможного pending action. Проверены production
prompts, Pydantic-контракты и тесты с `FakeAIClient`. Дополнительно 40 новых фраз
были пропущены через production deterministic selectors/parsers без модели и без
БД-записей.

Ни одна из 40 фраз до аудита не встречалась в tracked production prompts или
тестах (`git grep -F` для каждой фразы: совпадений нет). Фразы находятся только в
этом документе и не являются few-shot примерами production prompt.

Реальная модель в рамках аудита не запускалась. Поэтому ветка «передано LLM»
означает, что архитектура допускает semantic fallback, но не доказывает качество
Qwen на этой формулировке. Тест, в котором `FakeAIClient` заранее возвращает
правильный tool call, доказывает валидацию и orchestration, но не понимание языка.

Шкала:

- **A** — LLM выполняет существенную семантическую работу, которую трудно разумно
  заменить простой формой или parser.
- **B** — полезный hybrid: deterministic слой сохраняет корректность, а LLM
  действительно разрешает семантическую неоднозначность или делает выбор.
- **C** — основное понимание/решение уже выполнено regex/SQL/Python; LLM чаще
  классифицирует или переформатирует готовый результат.
- **D** — практически фиксированный шаблон/parser под видом AI.

## Итоговый рейтинг

| Пользовательская функция | Рейтинг | Краткий вывод |
|---|---:|---|
| Natural-language expense draft | **B** | Деньги и даты правильно закреплены backend; LLM реально может выделить merchant/description и предложить категорию, но не видит фразы без числовой суммы и не распознаёт тип операции. |
| Finance questions and explanations | **C** | Python полностью вычисляет ответ; common routing — regex, а LLM обычно превращает готовый DTO в текст. Semantic selector существует, но его качество на unseen wording не проверено реальной моделью. |
| Recipe search/recommendations | **C** | Regex выбирает tool/filters, SQL формирует и сортирует shortlist. LLM в частом пути объясняет уже выбранный список; частичный regex-match может потерять ограничения и не дать LLM исправить запрос. |
| Menu proposals | **B** | LLM делает содержательный выбор recipes/slots из shortlist, но даты и доступные кандидаты до него фиксирует узкий parser; ошибочное сужение необратимо. |
| AI actions/confirmation | **B** | Сам слой не выполняет NLP и не должен. Это корректная hybrid-граница между LLM proposal и детерминированным owner-safe write. |

Функций уровня **A** сейчас нет. Функций уровня **D** также нет: даже слабые
finance/recipe ветки имеют настоящий LLM fallback. Однако две функции уровня C
дают заметно меньше AI-ценности, чем предполагает их пользовательское название.

## 1. Natural-language expense input — B

### Что выполняется до LLM

- `parse_expense_text()` извлекает единственную числовую сумму, currency suffix,
  пробелы между тысячами, decimal часть, относительную/ISO дату, default today и
  очищенный остаток текста (`app/ai/expenses.py:139`).
- Несколько сумм, отсутствие суммы, ноль, отрицательная и чрезмерная сумма
  отклоняются до модели.
- Owner-scoped expense list/category, merchant rule и food/fuel hints выбираются
  детерминированно (`app/ai/expenses.py:285`, `app/ai/expenses.py:369`).
- Сумма и дата передаются модели как fixed facts, а не поля для повторного
  вычисления (`app/ai/expenses.py:298`, `app/ai/expenses.py:308`).

### Что реально делает LLM

Только при отсутствии rule/heuristic category модель получает исходный и
очищенный текст, fixed amount/date и allow-listed категории. Она возвращает
`title`, `merchant`, `category_id`, confidence и ambiguity. Это настоящая
семантическая задача: отделить название/описание от разговорной оболочки и
сопоставить его с пользовательской категорией. Модель не пишет в БД.

LLM не вызывается для известных rules/hints и не получает фразу, если parser не
нашёл ровно одну numeric amount. Поэтому словесные суммы, `2к`, скидка+итог и
refund semantics не могут быть разрешены моделью.

### Это только форматирование?

Нет для fallback merchant/category; да для fast path, где LLM вообще не нужна.
Простая форма способна заменить fast path, но semantic category mapping для
неизвестного merchant добавляет удобство. Рейтинг B оправдан именно fallback,
а не извлечением числа/даты.

### Реально поддерживаемая вариативность

- Сумма может находиться до, после или внутри описания.
- Поддержаны numeric values, `р/руб/₽`, пробелы тысяч и decimal separator.
- Поддержаны `сегодня`, `вчера`, `позавчера`, ISO date и omitted date.
- Filler text сохраняется и может быть интерпретирован LLM.
- Не поддержаны word-number amounts, `2к`, weekdays, «пятого числа» и явное
  различие expense/refund.

Production prompt не содержит NL-примеров, поэтому prompt overfitting здесь не
обнаружен. Набор из 34 PHASE 6.6 cases проверяет parser, но реальные LLM cases
используют заранее заданный `FakeAIClient` response
(`tests/test_ai_expenses.py:292`). Это не доказательство model generalization.

### Что происходит с unseen wording

Если ровно одна numeric amount распознана, unseen residue дойдёт до LLM, кроме
случая уверенного heuristic/rule. Если amount не распознана, запрос завершится до
LLM. Если regex ошибочно принял число за отдельную сумму, модель также не сможет
исправить результат.

### Held-out expenses

| Фраза | Наблюдаемый pre-LLM результат |
|---|---|
| «Позавчера отдал в шиномонтаже две тысячи» | Reject: словесная сумма не распознана; LLM не вызывается. |
| «Сгонял за хлебом, вышло 186,40 р» | `186.40`, today default, residue сохранён; semantic fallback возможен. |
| «За связь списали 799₽» | `799`, today default; semantic fallback возможен. |
| «Пятого числа купил корм коту за 1200» | `1200`, но дата ошибочно default today; «пятого числа» остаётся description. |
| «На рынке овощи — 1 350 вчера» | `1350`, yesterday, свободный порядок работает. |
| «В субботу кино и попкорн 980» | `980`, но weekday игнорируется и дата становится today. |
| «Саша вернул 500 за такси» | Создаётся expense candidate на `500`; refund/non-expense intent контрактом не представлен. |
| «Скидка 300, итог 1700 за продукты» | Safe reject как две суммы, хотя семантически одна может быть скидкой. |
| «Ужин обошёлся примерно в полторы тысячи» | Reject: словесная сумма не распознана. |
| «С карты ушло 2к за доставку» | Reject: shorthand amount не распознана. |

Детерминированная фиксация денег, дат, IDs и confirmation здесь используется
правильно. Проблема не в её наличии, а в том, что parser иногда закрывает модели
доступ к семантической неоднозначности.

## 2. Finance questions/explanations — C

### Что выполняется до LLM

`select_deterministic_finance_tool()` regex-ветками распознаёт forecast, limits,
comparison, largest expenses, category breakdown и summary, включая русские
названия месяцев (`app/ai/finance.py:74`). Если regex сработал, selector LLM не
вызывается.

`execute_finance_tool()` owner-scoped собирает snapshot и полностью вычисляет
income, expenses, balance, percentages, comparison, category changes, budget
states и forecast (`app/ai/tools/finance.py:292`). Это правильно: LLM не должна
считать деньги или читать БД напрямую.

### Что реально делает LLM

- При отсутствии regex match выбирает один из шести read-only tools
  (`app/ai/finance.py:106`).
- После выполнения tool всегда пишет краткое объяснение готового результата
  (`app/ai/finance.py:129`, `app/ai/finance.py:153`).

### Почему рейтинг C

Для всех common/tested вопросов код уже выбрал смысловой route и вычислил полный
ответ. LLM возвращает единственное поле `answer`; фактическая согласованность
текста с result программно не проверяется. Тот же полезный ответ можно вывести
детерминированным template по typed result. Здесь LLM в основном даёт тон и
связный текст, а не новую финансовую insight.

Настоящая AI-ценность есть только в fallback routing unseen wording. Но тест
`test_llm_selector_uses_one_allowlisted_tool_and_bounded_result`
(`tests/test_ai_finance.py:88`) заранее задаёт tool call и не измеряет, выберет ли
реальная модель правильный tool. Поэтому эта ценность архитектурно возможна, но
эмпирически не подтверждена production-path held-out eval.

### Pattern dependence и unseen wording

Тестовые common phrases почти точно покрывают regex stems: `почему`, `сравни`,
`прогноз`, `лимит`, `категории`, `потратил`, `крупные`. Тесты не влияют на
runtime, но демонстрируют узкую grammar. Unseen wording без этих stems попадёт в
LLM. Хуже partial false positive: если regex выбрал tool, LLM-selector уже не
может исправить route или period semantics.

### Held-out finance

| Фраза | Наблюдаемый route до LLM |
|---|---|
| «Куда опять утекли деньги за этот период?» | Нет match → semantic selector LLM. |
| «Я в плюсе сейчас или уже нет?» | Нет match → LLM, хотя нужен summary/balance. |
| «Откуда такой скачок трат?» | Нет match → LLM, хотя нужен comparison. |
| «Хватит ли мне денег до зарплаты?» | Нет match → LLM; ни один tool не моделирует дату зарплаты. |
| «Покажи три самых дорогих покупки за июль» | Нет match → LLM; `три` и month должен извлечь selector. |
| «В какой статье бюджета перебор?» | Нет match → LLM; возможно category или budget status. |
| «Сколько осталось по ограничениям на еду?» | Нет match → LLM; tool не поддерживает category argument для limit. |
| «Стало ли жить дороже по сравнению с весной?» | Regex выбирает current comparison; «весной» игнорируется. |
| «Что съело зарплату в прошлом месяце?» | Нет match → LLM; previous period должен извлечь selector. |
| «Разложи расходы за март 2025 по направлениям» | Regex ошибочно выбирает summary, хотя формулировка просит category breakdown. |

## 3. Recipe recommendations — C

### Что выполняется до LLM

`recipe_search_arguments_from_text()` regex-логикой извлекает некоторые
ingredients, только fish exclusion, numeric minutes/hours, bounded cost,
servings и one tag (`app/ai/recipes.py:49`).

`select_deterministic_recipe_tool()` выбирает search/recommend/recent menu и
sort order (`app/ai/recipes.py:81`). SQL затем фильтрует owner recipes и сортирует
их по favorites/update, cost, time или last cooked
(`app/ai/tools/recipes.py:211`, `app/ai/tools/recipes.py:239`). Shortlist уже
определён до explanation LLM.

### Что реально делает LLM

- Для текста без deterministic route может выбрать read tool
  (`app/ai/recipes.py:108`). В отличие от finance prompt, selector перечисляет
  purposes, но не объясняет точные argument names/enums полного schema.
- После tool result объясняет shortlist и выбирает упоминаемые IDs
  (`app/ai/recipes.py:130`, `app/ai/recipes.py:161`). IDs проверяются как subset,
  что правильно.

### Почему рейтинг C

В common path «рекомендация» — deterministic SQL ordering плюс LLM prose. Модель
не видит весь каталог и не может вернуть рецепт, исключённый ранним parser/filter.
Особенно важно: любой непустой partial `arguments` немедленно выбирает SEARCH.
Нераспознанные constraints теряются, а selector LLM не вызывается. Explanation
видит вопрос, но уже не может повторно запросить каталог.

Fake tests хорошо доказывают owner isolation, bounded shortlist и no invented ID,
но не semantic selection на unseen Russian wording. Поэтому это полезный
read/search API с AI-обёрткой, а не доказанно сильный recommendation engine.

### Held-out recipes

| Фраза | Наблюдаемый route до LLM |
|---|---|
| «Хочется чего-то острого и без молочки минут на двадцать» | Нет numeric minutes/known exclusion → LLM. |
| «Есть что-нибудь постное из нута?» | `из` и dietary constraint не распознаны → LLM. |
| «Накорми четверых быстро и без рыбы» | SEARCH только `exclude_ingredient=рыб`; servings/speed потеряны. |
| «Что у нас есть для завтрака?» | Нет route keyword → LLM. |
| «Покажи рецепт номер 12» | Ошибочно SEARCH по строке `рецепт номер 12`, не DETAILS; LLM bypass. |
| «Дай то, что я готовил реже всего» | Нет exact `давно не готов` → LLM. |
| «Уложимся в 500 рублей и полчаса?» | Cost/time grammar не совпала → LLM. |
| «Нужно веганское на шестерых» | Dietary и word-number servings не распознаны → LLM. |
| «Что можно приготовить из картошки и грибов?» | `что можно приготовить`/`из` не совпадают с grammar → LLM. |
| «Найди суп без лука, до 45 минут» | SEARCH только `max_cook_time=45`; query `суп` и exclusion `лук` потеряны. |

## 4. Menu proposals — B

### Что выполняется до LLM

- `menu_planning_window()` распознаёт только next week, until Friday, tomorrow,
  иначе молча выбирает tomorrow (`app/ai/menu.py:97`).
- `_recent_exclusion_days()` требует форму `без повтор...` и ограниченный набор
  числовых expressions (`app/ai/menu.py:111`).
- Recipe constraints переиспользуют узкий recipe parser; SQL строит shortlist до
  модели (`app/ai/menu.py:145`, `app/ai/menu.py:156`).

### Что реально делает LLM

Модель получает исходный вопрос, allowed dates и до восьми recipe candidates с
title/ingredient preview/time/cost/servings/tags. Она выбирает recipe per date,
meal slot, note и rationale (`app/ai/menu.py:174`). Это содержательная
combinatorial/semantic работа, особенно для описательных preferences внутри уже
доступного shortlist.

Backend затем проверяет каждый ID, date, duplicate slot и полноту всех dates
(`app/ai/menu.py:217`). Создаётся только pending action
(`app/ai/menu.py:259`). Это правильный hybrid.

### Почему не A

LLM не может исправить ошибочную duration/date interpretation или вернуть рецепт,
который ранний parser исключил. Вопрос «на три дня» превращается в one-day
allowed_dates; явная дата и weekdays также теряются. Более опасен пример «без
курицы»: общий recipe parser выставляет положительный `ingredient=куриц`, то есть
shortlist инвертирует пользовательское ограничение до LLM.

### Held-out menu

| Фраза | Наблюдаемый pre-LLM контекст |
|---|---|
| «Распиши ужины на три дня» | Allowed dates: только tomorrow. |
| «Сделай план еды на выходные» | Только tomorrow; weekend не распознан. |
| «На понедельник и среду поставь что-нибудь лёгкое» | Только tomorrow; оба weekdays потеряны. |
| «Неделя без молочки, готовка до 25 минут» | Только tomorrow; time=25, dairy exclusion потерян. |
| «Меню до пятницы, блюда не дороже 400 руб» | Корректное окно до Friday и max_cost=400 — хороший hybrid case. |
| «Два завтрака и два ужина на завтра» | Date верна; LLM может предложить несколько meal slots, но count не структурирован. |
| «Хочу три дня домашней еды без курицы» | Только tomorrow и ошибочный positive `ingredient=куриц`. |
| «Не повторяй то, что ели на прошлой неделе» | Recent exclusion=0 из-за отсутствия exact `без повтор`. |
| «Побольше овощей и поменьше тяжёлой еды на завтра» | Date верна, filters пусты; LLM может выбирать семантически только среди top-8. |
| «Для ребёнка без острого на 12 сентября» | Explicit date и exclusion потеряны; allowed date остаётся tomorrow. |

## 5. AI actions/confirmation — B как enabling infrastructure

Этот слой намеренно не выполняет natural-language interpretation. Он валидирует
typed payload и TTL при создании pending action (`app/ai/actions.py:81`), повторно
проверяет owner, permission, status и server-owned handler, атомарно claim'ит
action и делает terminal transition (`app/ai/actions.py:228`). Cancel также
owner-scoped и условный (`app/ai/actions.py:308`). Expense/menu payload имеют
отдельные строгие схемы (`app/ai/action_schemas.py:16`,
`app/ai/action_schemas.py:39`).

Здесь отсутствие LLM — достоинство, не недостаток. Простая форма действительно
способна выполнить confirm/cancel, но infrastructure обеспечивает безопасную
границу для семантически сформированного proposal. Тесты реально доказывают
owner isolation, idempotency, expiry, permission recheck и rollback
(`tests/test_ai_confirmation.py:118`–`287`).

Для этого слоя held-out NL phrases неприменимы: он не принимает свободный текст.
Unseen action type/payload отклоняется schema/registry, что является правильным
поведением.

## Inputs, работающие только из-за examples/patterns

Тесты не влияют на runtime, поэтому буквально «работает потому, что пример есть в
тесте» — неверная причинность. Production prompts также почти не содержат NL
few-shot examples. Реальная зависимость другая:

- finance common path зависит от regex stems, почти один-в-один представленных в
  parameterized tests (`tests/test_ai_finance.py:64`);
- recipe common path зависит от точных `с курицей`, `без рыбы`, numeric `минут`,
  `на N порций`, `недорого`, `давно не готовили`
  (`tests/test_ai_recipes.py:73`);
- menu dates зависят от `завтра`, `следующая неделя`, `до пятницы`, а repeat
  exclusion — от `без повтор...` (`tests/test_ai_menu.py:65`, `97`, `143`);
- expense production prompt не содержит examples; PHASE 6.6 parser grammar
  определяет, попадёт ли текст к LLM.

Иными словами, риск — не prompt memorization, а deterministic grammar coverage и
ранний short-circuit.

## Топ-5 случаев, где AI добавляет мало или ничего

1. **Finance common summary:** tool и все суммы уже известны; LLM нужен только для
   одного текстового `answer`, который мог бы построить template.
2. **Finance comparison/forecast explanation:** direction, difference, strongest
   categories, forecast и confidence уже рассчитаны Python; factual value модели
   в основном стилистическая.
3. **Recipe common recommendation:** shortlist и ordering (`cost/time/last_cooked`)
   уже определены regex+SQL; LLM пересказывает до пяти строк.
4. **Recipe partial-match path:** «Найди суп без лука, до 45 минут» запускает поиск
   только по времени. LLM explanation не может вернуть потерянные recipes.
5. **Menu unsupported date wording:** для «на три дня» модель получает ровно одну
   allowed date. Здесь AI не может выполнить заявленный запрос независимо от
   качества модели.

## Топ-5 правильно построенных hybrid cases

1. **Expense:** backend фиксирует Decimal/date; LLM разрешает merchant/category;
   пользователь подтверждает pending draft.
2. **Finance fallback:** unseen wording может быть отображён LLM на один bounded
   read tool, после чего все значения рассчитывает Python.
3. **Menu selection:** LLM выбирает только реальные recipes для server-owned dates,
   а backend валидирует ID/date/slots и сохраняет pending action.
4. **Recipe fallback:** LLM не получает Session и выбирает только read tool;
   explanation IDs проверяются как subset фактического result.
5. **Confirmation:** любая модельная рекомендация отделена от записи owner check,
   permission recheck, atomic claim, idempotency и cancel.

## Рекомендации по убыванию влияния

1. **Не считать FakeAI semantic test.** Добавить read-only real-model held-out
   evaluation именно production request path для finance, recipes и menu, с
   expected semantic route/constraints, а не только JSON validity. Фразы хранить
   отдельно от prompts.
2. **Устранить partial deterministic short-circuit.** Parser должен возвращать
   extracted facts плюс coverage/unconsumed constraints. Если распознана лишь
   часть сложного запроса, bounded LLM дополняет недостающие поля, не меняя уже
   надёжные facts.
3. **Исправить menu intent extraction до PHASE 7:** structured duration/date set
   для `N дней`, weekends, weekdays и explicit dates; arbitrary ingredient
   negation; затем прежняя deterministic validation allowed dates/IDs.
4. **Укрепить recipe selector contract:** перечислить точные argument names/enums,
   поддержать несколько ingredients/exclusions и не позволять одному найденному
   числу подавлять остальные constraints.
5. **Сделать finance route confidence-aware:** сложные/unconsumed period/category
   формулировки должны идти в selector, а не в первый regex match. Tool contract
   должен явно отвечать, какие category-specific вопросы невозможны.
6. **Определить expense ambiguity policy** для refunds, скидка+итог, word-number,
   shorthand amount и colloquial dates. Не угадывать; возвращать clarification
   state, когда transaction type/amount/date не доказаны.
7. **Оценить отказ от LLM explanation там, где она не добавляет content.** Для
   простых finance summary/empty result и recipe list использовать быстрый typed
   renderer; оставлять LLM для «почему», trade-offs и multi-constraint synthesis.
8. **В PHASE 12 валидировать не только IDs, но и claims ответа:** проверять, что
   finance/recipe prose не приписывает result отсутствующие причины/ingredients.

## Что исправить до PHASE 7

- Пункты 1–3: production-path semantic held-out eval, partial-match policy и menu
  date/negation extraction. Иначе PHASE 7 рискует повторить тот же шаблон:
  узкий date parser фиксирует неверный контекст до LLM.
- Критические части пункта 4: exact recipe selector schema и корректное `без X`,
  потому что menu напрямую переиспользует этот parser.
- Пункт 5 для очевидных false positives вроде «по направлениям» → summary.

Это не требует ослаблять deterministic calculations, DB access, ownership, ID
validation или confirmation. Меняется только граница semantic interpretation.

## Что может ждать PHASE 10/12

- Общий conversational router, follow-up context и cross-domain dialogue —
  PHASE 10.
- Выбор между template и LLM explanation ради latency/cost — PHASE 12 hardening.
- Более широкие slang/word-number/date vocabularies после clarification policy —
  PHASE 12.
- Автоматическая factual consistency проверка explanation prose и adversarial
  semantic suite — PHASE 12.
- UX-улучшения rationale/style, если typed result уже корректен, также могут ждать
  PHASE 10/12.

Главный итог: safety architecture в целом правильная и не «слишком
детерминированная». Недостаток AI-ценности возникает там, где узкий parser
преждевременно объявляет семантику полностью понятой и тем самым лишает LLM
возможности обработать оставшуюся естественно-языковую часть.

## PHASE 6.6 remediation — 2026-09-08

Исходное наблюдение сохранено выше как baseline. Исправление не заменяет
детерминированные вычисления моделью: оно меняет только границу между надёжно
извлечёнными фактами и ещё не понятым текстом.

| Исходная проблема | Исправление | Автоматический after-result |
|---|---|---|
| Partial recipe parse отбрасывал оставшиеся условия | `RecipeParseCoverage` хранит fixed arguments, explicit ID, unresolved text и `needs_semantic_resolution`; selector получает оба слоя, а backend повторно накладывает fixed arguments | `Найди суп без лука, до 45 минут` → search с `query=суп`, `exclude_ingredient=лук`, `max_cook_time=45` |
| `без X` было узким и могло стать positive ingredient | Общий include/exclude разбор, plural filters и SQL-применение каждого фильтра; nullable tags больше не превращают `NOT (... OR NULL)` в пустой результат | `Хочу три дня домашней еды без курицы` → только exclusion `куриц`, positive ingredient отсутствует |
| Recipe number превращался в текстовый search | `рецепт [номер] N` → deterministic `get_recipe_details`; сам tool остаётся owner-scoped и возвращает `found=false` для чужого/несуществующего ID | `Покажи рецепт номер 12` → details для доступного ID 12 |
| Generic finance wording побеждало более точный intent | Bounded scoring/precedence; category/breakdown и comparison имеют более высокий вес, month/year сохраняются как fixed facts и при LLM fallback | `Разложи расходы за март 2025 по направлениям` → category breakdown, March 2025 |
| Bare Russian day-of-month молча становился today | Добавлены numeric/ordinal day-of-month, явный Russian month/year и прошедший weekday; будущий bare day и неизвестная date-like phrase требуют уточнения | `Пятого числа ...` при reference date 2026-09-07 → 2026-09-05, `date_was_defaulted=false` |
| Refund мог стать обычным расходом | Refund/reimbursement markers отклоняются до semantic category selection | `Саша вернул 500 за такси` → safe unsupported result, AIAction/ExpenseItem не создаются |
| Menu duration/date parser передавал модели неверное окно | До shortlist детерминированно строятся 1–14 дней, weekend, grouped weekdays и explicit month date | `три дня` → три server-owned allowed dates |

Safety-инварианты не менялись: суммы и даты расходов, финансовые вычисления,
доступ к ID, owner/permission validation, allow-listed tools, pending actions,
confirmation и фактические записи остаются детерминированными. LLM не получает
Session, raw SQL или write tool.

### 40 held-out cases и способ проверки

Ровно 40 исходных фраз из этого аудита зафиксированы в
`scripts/home_ai_semantic_cases.py`, но не добавлены в production prompts.
`scripts/home_ai_semantic_regression.py` создаёт только synthetic in-memory
SQLite data и вызывает production preparation/orchestration functions. Для
каждого case он печатает input/domain, preprocessing, факт LLM call, raw и
parsed structured output, final validated interpretation, pass/fail, latency и
error. Menu proposal создаётся лишь в эфемерной транзакции и сразу откатывается;
confirmation отсутствует.

Mock/preprocessing verification: 40 cases обнаружены, 35 проходят deterministic
preprocessing, 5 ожидаемо и безопасно отклоняются из-за неподдерживаемой формы
суммы, нескольких сумм или refund semantics. Реальный Qwen/llama.cpp прогон в
этой Windows-среде не выполнялся; поэтому здесь намеренно нет выдуманного
pass-rate. Его результат следует сохранить отдельным JSON после запуска на Pi.

Команда внутри production web container:

```bash
docker compose exec web python scripts/home_ai_semantic_regression.py \
  --output /tmp/home_ai_semantic_regression.json
```

### Оставшиеся ограничения

- Суммы словами (`две тысячи`, `полторы тысячи`) и shorthand `2к` пока безопасно
  отклоняются; guessing суммы не добавлялся.
- Точный произвольный seasonal range (`по сравнению с весной`) не выражается
  текущим finance tool contract: bounded comparison route доступен, но точное
  season-vs-period сравнение отложено.
- Recipe ingredient matching остаётся bounded substring search с лёгкой
  нормализацией русских окончаний, а не морфологическим/векторным поиском.
- Количество нескольких meal slots на одной дате интерпретирует LLM; backend
  валидирует ID/date/duplicate slot, но не имеет отдельного count contract.
- Refund/reimbursement — явно unsupported intent до отдельного продуктового
  решения; Phase 6.6 только предотвращает ошибочную запись обычного расхода.
