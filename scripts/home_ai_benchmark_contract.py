"""Shared, synthetic routing contract for Phase 6.5 smoke and benchmark tools.

This is deliberately separate from production Home AI contracts. Production
uses domain-specific tool enums and does not expose this cross-domain intent
schema. The benchmark never executes the selected tool.
"""

from __future__ import annotations

from typing import Any

ALLOWED_INTENTS = (
    "expense_draft",
    "finance_question",
    "recipe_query",
    "menu_proposal",
)

ALLOWED_TOOLS = (
    "expense.create_draft",
    "finance.summary",
    "finance.comparison",
    "recipes.recommend",
    "menu.propose",
)

SYNTHETIC_RECIPE_IDS = frozenset({101, 102, 103})

ROUTING_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent", "tool", "arguments", "referenced_ids"],
    "properties": {
        "intent": {"type": "string", "enum": list(ALLOWED_INTENTS)},
        "tool": {"type": "string", "enum": list(ALLOWED_TOOLS)},
        "arguments": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "merchant": {"type": "string", "minLength": 1},
                "amount": {"type": "integer", "minimum": 1},
                "date_hint": {"type": "string", "enum": ["сегодня", "вчера"]},
                "period": {"type": "string", "enum": ["current_month"]},
                "max_minutes": {"type": "integer", "minimum": 1, "maximum": 480},
                "budget": {"type": "string", "enum": ["low"]},
                "days": {"type": "integer", "minimum": 1, "maximum": 7},
                "no_repeats": {"type": "boolean"},
            },
        },
        "referenced_ids": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "integer", "enum": sorted(SYNTHETIC_RECIPE_IDS)},
        },
    },
}

ROUTING_SYSTEM_PROMPT = """
Ты выполняешь только read-only benchmark маршрутизации Home OS. Ты НЕ запускаешь
инструменты, НЕ записываешь данные, НЕ выполняешь SQL и НЕ совершаешь автономных
действий.

Верни ровно один JSON-объект и ничего больше. Обязательная точная схема:
{
  "intent": "<одно разрешенное имя intent>",
  "tool": "<одно разрешенное имя tool>",
  "arguments": {<точные аргументы выбранного tool>},
  "referenced_ids": [<только разрешенные целые ID>]
}
Дополнительные поля запрещены. Внутренние имена intent, tool, аргументов и enum
копируй ТОЧНО как написано ниже: не переводи, не перефразируй и не заменяй
синонимами.

Разрешенные intent (только эти четыре):
- expense_draft
- finance_question
- recipe_query
- menu_proposal

Разрешенные tool (только эти пять):
- expense.create_draft
- finance.summary
- finance.comparison
- recipes.recommend
- menu.propose

Точное соответствие tool -> intent и arguments:
1. expense.create_draft -> intent expense_draft.
   arguments содержит РОВНО merchant (строка), amount (целое число рублей) и
   date_hint (enum: "сегодня" или "вчера"). referenced_ids всегда [].
   Короткие записи покупки являются созданием ЧЕРНОВИКА расхода, а не финансовым
   вопросом. Обязательные примеры:
   "Лента 1840 вчера" -> expense_draft / expense.create_draft /
   {"merchant":"Лента","amount":1840,"date_hint":"вчера"}.
   "Бензин 2600 сегодня" -> expense_draft / expense.create_draft /
   {"merchant":"Бензин","amount":2600,"date_hint":"сегодня"}.
2. finance.summary -> intent finance_question.
   arguments содержит РОВНО period (единственное разрешенное значение
   "current_month"). referenced_ids всегда []. Выбирай для вопросов об итогах,
   сумме, доходах, расходах или балансе текущего периода, например
   "Сколько я потратил в этом месяце?".
3. finance.comparison -> intent finance_question.
   arguments содержит РОВНО period="current_month". referenced_ids всегда [].
   Выбирай для "почему выросли/снизились", "что изменилось" и сравнений одного
   периода с другим, например "Почему расходы выросли?". Такие вопросы НЕ
   относятся к finance.summary.
4. recipes.recommend -> intent recipe_query.
   Для ограничения времени arguments содержит РОВНО max_minutes (целое 1..480).
   Имя max_time_minutes запрещено. Для низкой стоимости arguments содержит РОВНО
   budget="low". Имена criteria и значение low_cost запрещены.
   referenced_ids содержит один или несколько уникальных ID только из 101,102,103.
5. menu.propose -> intent menu_proposal.
   arguments содержит РОВНО days (целое 1..7) и no_repeats (boolean).
   referenced_ids содержит уникальные ID только из 101,102,103; для трех дней —
   все три ID без повторов. Строка menu.propose является tool, но никогда intent.

Тестовые read-only рецепты: 101 Овощной суп (35 минут, недорогой), 102 Каша
(15 минут, недорогая), 103 Запеканка (40 минут, средняя цена). Других ID нет.
Если запрос не требует рецептов или меню, referenced_ids обязан быть [].
""".strip()


def routing_response_format() -> dict[str, Any]:
    """Return llama.cpp's schema-constrained response_format payload."""
    return {"type": "json_schema", "schema": ROUTING_RESPONSE_SCHEMA}
