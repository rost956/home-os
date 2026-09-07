from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Domain = Literal["expense", "finance", "recipe", "menu"]


@dataclass(frozen=True)
class SemanticEvaluationCase:
    name: str
    domain: Domain
    text: str
    expected_tool: str | None = None
    expected_days: int | None = None
    expected_recipe_id: int | None = None
    expected_date: str | None = None
    expected_categories: tuple[str, ...] = ()
    expected_excludes: tuple[str, ...] = ()
    expected_includes: tuple[str, ...] = ()
    expected_max_time: int | None = None
    expected_max_cost: str | None = None
    expected_min_servings: str | None = None
    expected_sort: str | None = None
    expected_entries: int | None = None
    safe_rejection: bool = False


# This is diagnostic data from AI_VALUE_AUDIT.md, never imported by production prompts.
HELD_OUT_SEMANTIC_CASES = (
    SemanticEvaluationCase("expense_word_amount", "expense", "Позавчера отдал в шиномонтаже две тысячи", safe_rejection=True),
    SemanticEvaluationCase("expense_colloquial_bread", "expense", "Сгонял за хлебом, вышло 186,40 р", expected_categories=("Еда",)),
    SemanticEvaluationCase("expense_mobile", "expense", "За связь списали 799₽", expected_categories=("Связь",)),
    SemanticEvaluationCase("expense_day_ordinal", "expense", "Пятого числа купил корм коту за 1200", expected_date="2026-09-05", expected_categories=("Еда", "Дом")),
    SemanticEvaluationCase("expense_market", "expense", "На рынке овощи — 1 350 вчера", expected_date="2026-09-06", expected_categories=("Еда",)),
    SemanticEvaluationCase("expense_weekday", "expense", "В субботу кино и попкорн 980", expected_date="2026-09-05", expected_categories=("Развлечения",)),
    SemanticEvaluationCase("expense_refund", "expense", "Саша вернул 500 за такси", safe_rejection=True),
    SemanticEvaluationCase("expense_discount_total", "expense", "Скидка 300, итог 1700 за продукты", safe_rejection=True),
    SemanticEvaluationCase("expense_approx_word_amount", "expense", "Ужин обошёлся примерно в полторы тысячи", safe_rejection=True),
    SemanticEvaluationCase("expense_short_amount", "expense", "С карты ушло 2к за доставку", safe_rejection=True),
    SemanticEvaluationCase("finance_leak", "finance", "Куда опять утекли деньги за этот период?", "get_category_breakdown"),
    SemanticEvaluationCase("finance_balance", "finance", "Я в плюсе сейчас или уже нет?", "get_finance_summary"),
    SemanticEvaluationCase("finance_jump", "finance", "Откуда такой скачок трат?", "compare_finance_periods"),
    SemanticEvaluationCase("finance_salary_forecast", "finance", "Хватит ли мне денег до зарплаты?", "get_finance_forecast"),
    SemanticEvaluationCase("finance_top_three", "finance", "Покажи три самых дорогих покупки за июль", "get_largest_expenses"),
    SemanticEvaluationCase("finance_budget_article", "finance", "В какой статье бюджета перебор?", "get_budget_status"),
    SemanticEvaluationCase("finance_food_limits", "finance", "Сколько осталось по ограничениям на еду?", "get_budget_status"),
    SemanticEvaluationCase("finance_spring", "finance", "Стало ли жить дороже по сравнению с весной?", "compare_finance_periods"),
    SemanticEvaluationCase("finance_salary_spend", "finance", "Что съело зарплату в прошлом месяце?", "get_category_breakdown"),
    SemanticEvaluationCase("finance_march_breakdown", "finance", "Разложи расходы за март 2025 по направлениям", "get_category_breakdown"),
    SemanticEvaluationCase("recipe_spicy_dairy", "recipe", "Хочется чего-то острого и без молочки минут на двадцать", expected_excludes=("молочк",), expected_max_time=20),
    SemanticEvaluationCase("recipe_lentil", "recipe", "Есть что-нибудь постное из нута?", expected_includes=("нут",)),
    SemanticEvaluationCase("recipe_four_fast", "recipe", "Накорми четверых быстро и без рыбы", expected_excludes=("рыб",), expected_min_servings="4", expected_sort="time"),
    SemanticEvaluationCase("recipe_breakfast", "recipe", "Что у нас есть для завтрака?"),
    SemanticEvaluationCase("recipe_id", "recipe", "Покажи рецепт номер 12", "get_recipe_details", expected_recipe_id=12),
    SemanticEvaluationCase("recipe_rarest", "recipe", "Дай то, что я готовил реже всего", expected_sort="last_cooked"),
    SemanticEvaluationCase("recipe_cost_time", "recipe", "Уложимся в 500 рублей и полчаса?", expected_max_time=30, expected_max_cost="500"),
    SemanticEvaluationCase("recipe_vegan_six", "recipe", "Нужно веганское на шестерых", expected_min_servings="6"),
    SemanticEvaluationCase("recipe_potato_mushroom", "recipe", "Что можно приготовить из картошки и грибов?", expected_includes=("картошк", "гриб")),
    SemanticEvaluationCase("recipe_soup_no_onion", "recipe", "Найди суп без лука, до 45 минут", "search_recipes", expected_excludes=("лук",), expected_max_time=45),
    SemanticEvaluationCase("menu_three_dinners", "menu", "Распиши ужины на три дня", expected_days=3),
    SemanticEvaluationCase("menu_weekend", "menu", "Сделай план еды на выходные", expected_days=2),
    SemanticEvaluationCase("menu_weekdays", "menu", "На понедельник и среду поставь что-нибудь лёгкое", expected_days=2),
    SemanticEvaluationCase("menu_week_dairy", "menu", "Неделя без молочки, готовка до 25 минут", expected_days=7, expected_excludes=("молочк",), expected_max_time=25),
    SemanticEvaluationCase("menu_until_friday", "menu", "Меню до пятницы, блюда не дороже 400 руб", expected_max_cost="400"),
    SemanticEvaluationCase("menu_multiple_slots", "menu", "Два завтрака и два ужина на завтра", expected_days=1, expected_entries=4),
    SemanticEvaluationCase("menu_three_home_no_chicken", "menu", "Хочу три дня домашней еды без курицы", expected_days=3, expected_excludes=("куриц",)),
    SemanticEvaluationCase("menu_no_recent", "menu", "Не повторяй то, что ели на прошлой неделе"),
    SemanticEvaluationCase("menu_light_vegetables", "menu", "Побольше овощей и поменьше тяжёлой еды на завтра", expected_days=1),
    SemanticEvaluationCase("menu_child_date", "menu", "Для ребёнка без острого на 12 сентября", expected_days=1, expected_date="2026-09-12", expected_excludes=("остр",)),
)
