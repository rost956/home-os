# Реестр браузерной проверки

Аудит выполнен локально в Chromium против изолированной SQLite-базы `C:\Temp\recipe-budget-browser-audit`; пользовательские данные проекта не использовались. Для каждого перечисленного адреса проверены ширины 320, 360, 390, 768, 1280 и 1440 CSS px в светлой и тёмной темах; проверены HTTP-ответ, конечный URL и горизонтальное переполнение документа. Созданы отдельные тестовые учётные записи с данными и без данных.

## Экраны и состояния

| URL | Шаблон | Вход и состояние | Результат |
| --- | --- | --- | --- |
| `/` | `today.html` | Начальная ссылка; переход на сегодня | 200 после перехода |
| `/login` | `login.html` | Без входа; пустая форма и ошибка неверных данных | Исправно |
| `/register` | `register.html` | Без входа; пустая форма и ошибка несовпавших паролей | Исправно |
| `/today` | `today.html` | Главная, календарь и сводные карточки | Исправно |
| `/search` | `search.html` | Поиск без запроса | Исправно |
| `/settings` | `settings.html` | Настройки и предпросмотр пользовательской палитры | Исправно |
| `/backup` | `backup.html` | Страница резервных копий | Исправно |
| `/ai/settings` | `ai_settings.html` | Условный экран при `HOME_AI_ENABLED=true` | Исправно |
| `/recipes` | `recipes.html` | Список с рецептом | Исправно |
| `/recipes/new` | `recipe_form.html` | Пустая форма создания | Исправно |
| `/recipes/import` | `recipe_import.html` | Форма импорта | Исправно |
| `/recipes/1` | `recipe_detail.html` | Детали и действия существующего рецепта | Исправно |
| `/recipes/1/edit` | `recipe_form.html` | Заполненная форма редактирования | Исправно |
| `/menu` | `menu.html` | План меню с рецептом | Исправно |
| `/shopping` | `shopping.html` | Список покупок | Исправно |
| `/shopping/lists/1` | `shopping_list.html` | Детали списка с позицией | Исправно |
| `/expenses` | `expenses.html` | Списки и недавние расходы | Исправно |
| `/expenses/analytics` | `expense_analytics.html` | Аналитика расходов | Исправно |
| `/expenses/categories/1/analytics` | `expense_category_analytics.html` | Аналитика категории | Исправно |
| `/expenses/lists/1` | `expense_list.html` | Детали списка расходов | Исправно |
| `/expenses/categories/1` | `expense_category.html` | Детали категории | Исправно |
| `/expenses/planning` | `expense_planning.html` | Бюджет и планирование | Исправно |
| `/expenses/splits` | `expense_splits.html` | Совместные долги и пустое состояние | Исправно |
| `/income` | `income.html` | Доходы и форма добавления | Исправно |
| `/finance` | `finance.html` | Фильтр, оба графика, прогноз; есть и пустые ряды | Исправно |
| `/chats` | `chats.html` | Список чатов | Исправно |
| `/chats/1` | `chat_thread.html` | Переписка | Исправно |
| `/wishlist` | `wishlist.html` | Список желаний | Исправно |
| `/wishlist/shared/audituser` | `wishlist_shared.html` | Ссылка на общий список | Исправно |
| `/watch` | `watch.html` | Список фильмов и сериалов | Исправно |
| `/moments` | `moments.html` | Календарь, запись и карточка; проверен также вид месяца | Исправлена ширина 320 px |
| `/planner` | `planner.html` | Календарь и форма события | Исправно |
| `/vehicles` | `vehicles.html` | Список автомобилей | Исправно |
| `/vehicles/new` | `vehicle_form.html` | Форма автомобиля | Исправно |
| `/vehicles/1` | `vehicle_detail.html` | Карточка автомобиля и действия | Исправно |
| `/vehicles/1/edit` | `vehicle_form.html` | Заполненная форма автомобиля | Исправно |
| `/vehicles/1/log` | `vehicle_log.html` | Записи, фильтр и сортировка | Исправлена ширина 768 px |
| `/vehicles/1/log/new` | `vehicle_log_form.html` | Форма записи в журнал | Исправно |
| `/vehicles/1/log/print` | `vehicle_log_print.html` | Печатный вид | Исправно |
| `/vehicles/1/log/1` | `vehicle_log_detail.html` | Детали записи | Исправно |
| `/vehicles/1/log/1/edit` | `vehicle_log_form.html` | Заполненная форма записи | Исправно |
| `/vehicles/1/maintenance` | `vehicle_maintenance.html` | Список обслуживания | Исправно |
| `/vehicles/1/maintenance/new` | `vehicle_maintenance_form.html` | Форма обслуживания | Исправно |
| `/vehicles/1/maintenance/1/edit` | `vehicle_maintenance_form.html` | Заполненная форма обслуживания | Исправно |
| `/vehicles/1/fuel` | `vehicle_fuel.html` | Заправки, фильтр и сортировка | Исправно |
| `/vehicles/1/fuel/new` | `vehicle_fuel_form.html` | Форма заправки | Исправно |
| `/vehicles/1/fuel/1/edit` | `vehicle_fuel_form.html` | Заполненная форма заправки | Исправно |
| `/vehicles/trips` | `vehicle_trips.html` | Список поездок | Исправно |
| `/vehicles/trips/1` | `vehicle_trip_detail.html` | План и детали поездки | Исправно |
| `/fuel` | `fuel.html` | Карточка тестовой АЗС без наблюдений; список, карта Leaflet и поездка | Исправлена ширина 320 px; карта и маркеры проверены на 390 и 1440 px |
| `/fuel/add`, `/fuel/search` | `fuel_add.html` | Пустой поиск и пять найденных кандидатов по адресу; выбор топлива и станции | Исправлены карточки кандидатов на 320–1440 px |
| `/fuel/settings` | `fuel_settings.html` | Настройки мониторинга и уведомлений | Исправно |
| `/fuel/1` | `fuel_detail.html` | Состояния 95/98/100, история и пустой прогноз | Исправно |
| `/fuel/1/settings` | `fuel_station_settings.html` | Настройки отслеживаемых типов топлива | Исправно |
| `/files` | `file_transfers.html` | Список с пустой передачей и передачей с вложением | Исправно |
| `/files/new` | `file_transfer_form.html` | Пустая форма и загрузка PNG-вложения | Исправно |
| `/files/1` | `file_transfer_detail.html` | Пустая передача и ссылка владельца | Исправно |
| `/files/{audit_id}` | `file_transfer_detail.html` | Детали новой передачи с изображением | Исправно |
| `/share/{existing_token}` | `public_file_transfer.html` | Публичная ссылка без файлов | Исправно |
| `/share/{audit_token}` | `public_file_transfer.html` | Публичная ссылка с изображением, предпросмотром и скачиванием | Исправно |

`base.html` проверен как общий каркас на каждом маршруте. API, загрузки и выгрузки файлов с не-HTML ответами не включены в перечень экранов.

## Дефекты и снимки

| Дефект до исправления | Изменение | До | После |
| --- | --- | --- | --- |
| Месячный экран моментов на 320 px расширял документ на 5 px из-за строки заголовка и кнопки | На ширине до 360 px кнопка занимает отдельную строку | [moments-320-before.png](evidence/moments-320-before.png) | [moments-320.png](evidence/moments-320.png) |
| Панель АЗС на 320 px расширяла документ и сдавливала переключатель «Список / Карта / Поездка» | На ширине до 360 px фильтр и переключатель расположены друг под другом | [fuel-320-before.png](evidence/fuel-320-before.png) | [fuel-320.png](evidence/fuel-320.png) |
| Фильтр журнала автомобиля задавал пять минимальных колонок по 130 px и давал ширину документа 858 px на планшете 768 px | На ширине до 900 px фильтр использует три гибкие колонки | [vehicle-log-768-before.png](evidence/vehicle-log-768-before.png) | [vehicles-1-log-768.png](evidence/vehicles-1-log-768.png) |
| Даты серий в легенде нового графика не переносились на 320 px | Легенда переносит строки и даты внутри карточки | [finance-320-legend-before.png](evidence/finance-320-legend-before.png) | [finance-320.png](evidence/finance-320.png) |
| При тратах 1 000 000 000 ₽ подписи оси Y обрезались слева, а SVG на телефоне сжимал шаг между днями | Левое поле зависит от длины суммы; график сохраняет расчётную ширину и прокручивается внутри карточки | [finance-large-320-before.png](evidence/finance-large-320-before.png) | [finance-large-320.png](evidence/finance-large-320.png) |
| После поиска АЗС чекбоксы топлива занимали почти всю ширину карточки, а подписи были на отдельных строках | Выбор топлива оформлен группой с компактными чекбоксами и целью касания 44 px | [fuel-add-candidates-390-before.png](evidence/fuel-add-candidates-390-before.png) | [fuel-add-candidates-390.png](evidence/fuel-add-candidates-390.png) |
| При размере текста 200% на 320–390 px длинные кнопки, формы и карточки расширяли документ на многих экранах | Узкие сетки переходят в одну колонку; кнопки, заголовки и поля переносятся внутри карточек | [меню до](evidence/menu-text-200-320-before.png), [настройки до](evidence/settings-text-200-320-before.png) | [меню после](evidence/menu-text-200-320.png), [настройки после](evidence/settings-text-200-320.png) |
| На `/planner` при 768 px и размере текста 200% календарь и панель выходили за экран | Панель переносится, календарь занимает одну колонку | [planner-text-200-768-before.png](evidence/planner-text-200-768-before.png) | [planner-text-200-768.png](evidence/planner-text-200-768.png) |

Крупность графика, перенос дат и нулевые серии дополнительно проверены на [finance-390.png](evidence/finance-390.png), [finance-1440.png](evidence/finance-1440.png), [finance-dark-390.png](evidence/finance-dark-390.png) и [finance-rotation-text-200.png](evidence/finance-rotation-text-200.png). Пользовательский цвет `#9f1239` выбран в настройках, после чего цвет фокуса графика сверён с палитрой; снимки: [settings-custom-palette-390.png](evidence/settings-custom-palette-390.png) и [finance-custom-palette-390.png](evidence/finance-custom-palette-390.png). Публичное вложение и его открытый предпросмотр зафиксированы на [public-file-attachment-390.png](evidence/public-file-attachment-390.png) и [public-file-preview-open-390.png](evidence/public-file-preview-open-390.png); печать проверена на [vehicle-log-print.png](evidence/vehicle-log-print.png). Ошибки форм входа и регистрации зафиксированы на [login-validation-error.png](evidence/login-validation-error.png) и [register-validation-error.png](evidence/register-validation-error.png). Остальные ключевые экраны показаны в [fuel-390.png](evidence/fuel-390.png), [recipes-390.png](evidence/recipes-390.png), [vehicles-1-390.png](evidence/vehicles-1-390.png), [login-anonymous-390.png](evidence/login-anonymous-390.png) и [register-anonymous-390.png](evidence/register-anonymous-390.png).

## Действия и результаты

- Новый график проверен мышью, фокусом клавиатуры и касанием. Для отсутствующего дня tooltip сообщает «нет точки в текущем периоде», а не подставляет 0 ₽. Доступная таблица раскрывает все дни.
- Диапазон верхнего фильтра оставлен произвольным; даты и точки нового графика остаются связанными с текущим и предыдущим финансовыми периодами.
- На форме передачи создано тестовое изображение. В публичной странице предпросмотр открывает модальное окно, Escape его закрывает. Отдельно проверено пустое состояние передачи.
- Журнал печати открыт в режиме браузерной печати. На финансовом экране проверены тёмная тема, ширина 320 px, портретная/альбомная ориентация и увеличение корневого размера шрифта до 200%.
- При повторной проверке графика с тратой 1 000 000 000 ₽ в Chromium на 320, 360, 390, 768, 1280 и 1440 px подпись верхней отметки оси Y видна; на узких ширинах прокручивается только график. Снимок ПК: [finance-large-1440.png](evidence/finance-large-1440.png).
- При увеличении текста до 200% все 60 аутентифицированных адресов и состояний повторно открыты на 320, 390, 768 и 1440 px: 240 сочетаний без горизонтального переполнения. Числовой результат для каждого маршрута: [text-zoom-results.csv](evidence/text-zoom-results.csv).
- На отдельной пустой учётной записи проверены 29 доступных без записей экранов на 390 и 1440 px: 58 открытий с кодом 200 без переполнения. Таблица: [empty-user-results.csv](evidence/empty-user-results.csv); примеры: [рецепты](evidence/empty-recipes-390.png), [финансы](evidence/empty-finance-390.png), [бензин](evidence/empty-fuel-390.png).
- Для 56 аутентифицированных адресов в браузере подставлен длинный заголовок и текст карточки, затем проверены 320 и 1440 px: 112 сочетаний без переполнения. Исходные данные приложения при этой проверке не менялись. Таблица: [long-content-results.csv](evidence/long-content-results.csv).
- Поиск АЗС выполнен по тестовому адресу; найдены пять кандидатов. Их карточки проверены в обеих темах на 320, 360, 390, 768, 1280 и 1440 px без переполнения. Первый кандидат выбран с АИ-95 и АИ-100, без АИ-98; сохранённая подписка повторяет этот выбор. Дополнительные снимки: [320 px](evidence/fuel-add-candidates-320.png), [1440 px](evidence/fuel-add-candidates-1440.png).
- Карта Leaflet проверена с настоящими тайлами OpenStreetMap на 390 и 1440 px: обе тестовые АЗС видны, на телефоне нажатие маркера открывает нижнюю карточку, на ПК — всплывающую карточку. Снимки: [карта 390 px](evidence/fuel-map-leaflet-390.png), [маркер 390 px](evidence/fuel-map-leaflet-marker-390.png), [карта 1440 px](evidence/fuel-map-leaflet-1440.png), [маркер 1440 px](evidence/fuel-map-leaflet-marker-1440.png).
- На 15 экранах создания проверена отправка пустой формы: обязательные поля блокируют отправку браузерной валидацией. Дополнительно проверены [результаты поиска](evidence/search-results-390.png), [открытая форма момента](evidence/moments-add-open-390.png), переключение режимов «Карта»/«Поездка» и произвольный диапазон `/finance`. Результаты действий: [action-review.csv](evidence/action-review.csv).
- В браузере применены фильтры и сортировки аналитики расходов, категории, автомобильного журнала, заправок, поездок, файлов и `/finance`; также переключены месяцы моментов и планировщика и выбран АИ-95 на `/fuel`. Все десять переходов завершились без переполнения: [filter-review.csv](evidence/filter-review.csv).
- На всех 58 аутентифицированных адресах выполнен дополнительный проход доступных ссылок, раскрывающихся блоков и кнопок интерфейса: 32 перехода, 14 раскрытий и 11 действий кнопок без ошибок JavaScript. Подробности по каждому адресу: [generic-interactions.csv](evidence/generic-interactions.csv).
- Итоговый браузерный проход: 58 аутентифицированных маршрутов/состояний, включая добавленные передачу и публичное вложение, плюс вход и регистрация без сессии; 744 состояния ширины/темы; ошибок ответа и переполнения страницы — 0. После входа адреса `/login` и `/register` перенаправляли на `/recipes`; их формы проверены отдельно без сессии.

CSV с ответом, конечным URL, заголовком, шириной, темой и переполнением: [viewport-results.csv](evidence/viewport-results.csv). Сводка интерактивных проверок: [interaction-results.txt](evidence/interaction-results.txt).

## Границы подтверждённой проверки

Автоматический проход по 744 сочетаниям проверяет открытие маршрута и переполнение документа; 240 сочетаний дополнительно проверены при размере текста 200%. Дополнительный проход снял 116 обзорных снимков 58 аутентифицированных адресов на 390 и 1440 px без ошибок JavaScript; действия перечислены выше, в `interaction-results.txt`, `action-review.csv`, `filter-review.csv` и `generic-interactions.csv`. Пустые состояния, длинное содержимое, ошибки входа и регистрации, обязательные поля 15 форм, публичные ссылки и действия ключевых экранов проверены отдельно. Полный перебор всех сочетаний значений форм и размеров наборов данных не выполнялся.

Сохранена исходная карта Leaflet. Переключение списка и карты, загрузка тайлов и открытие маркера проверены в настоящем браузере на мобильной и настольной ширинах.
