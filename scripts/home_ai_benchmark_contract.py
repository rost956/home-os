"""Shared, synthetic routing contract for Phase 6.5 smoke and benchmark tools.

This is deliberately separate from production Home AI contracts. Production
uses domain-specific tool enums and does not expose this cross-domain intent
schema. The benchmark never executes the selected tool.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
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
DIAGNOSTIC_SEED = 6500
DIAGNOSTIC_MAX_TOKENS = 256


@dataclass(frozen=True)
class StructuredDiagnosticCase:
    name: str
    prompt: str
    intent: str
    tool: str
    required_arguments: dict[str, Any]
    allowed_ids: frozenset[int] = frozenset()
    minimum_ids: int = 0

    @property
    def expected(self) -> dict[str, Any]:
        expected: dict[str, Any] = {
            "intent": self.intent,
            "tool": self.tool,
            "arguments": self.required_arguments,
        }
        if not self.allowed_ids:
            expected["referenced_ids"] = []
        return expected


EXPENSE_DRAFT_CASE = StructuredDiagnosticCase(
    "expense_parse_read_only",
    "Лента 1840 вчера",
    "expense_draft",
    "expense.create_draft",
    {"merchant": "Лента", "amount": 1840, "date_hint": "вчера"},
)
EXPENSE_DRAFT_TODAY_CASE = StructuredDiagnosticCase(
    "expense_parse_today_read_only",
    "Бензин 2600 сегодня",
    "expense_draft",
    "expense.create_draft",
    {"merchant": "Бензин", "amount": 2600, "date_hint": "сегодня"},
)
FINANCE_SUMMARY_CASE = StructuredDiagnosticCase(
    "finance_read_only",
    "Сколько я потратил в этом месяце?",
    "finance_question",
    "finance.summary",
    {"period": "current_month"},
)
FINANCE_COMPARISON_CASE = StructuredDiagnosticCase(
    "finance_comparison_read_only",
    "Почему расходы выросли?",
    "finance_question",
    "finance.comparison",
    {"period": "current_month"},
)
RECIPE_RECOMMENDATION_CASE = StructuredDiagnosticCase(
    "recipe_read_only",
    "Что приготовить максимум за 40 минут?",
    "recipe_query",
    "recipes.recommend",
    {"max_minutes": 40},
    SYNTHETIC_RECIPE_IDS,
    1,
)
LOW_COST_RECIPE_CASE = StructuredDiagnosticCase(
    "low_cost_recipe_read_only",
    "Выбери недорогой рецепт из наших",
    "recipe_query",
    "recipes.recommend",
    {"budget": "low"},
    SYNTHETIC_RECIPE_IDS,
    1,
)
MENU_PROPOSAL_CASE = StructuredDiagnosticCase(
    "menu_proposal_no_write",
    "Составь меню на три дня без повторов",
    "menu_proposal",
    "menu.propose",
    {"days": 3, "no_repeats": True},
    SYNTHETIC_RECIPE_IDS,
    3,
)

BENCHMARK_CASES = (
    EXPENSE_DRAFT_CASE,
    EXPENSE_DRAFT_TODAY_CASE,
    FINANCE_SUMMARY_CASE,
    FINANCE_COMPARISON_CASE,
    RECIPE_RECOMMENDATION_CASE,
    LOW_COST_RECIPE_CASE,
    MENU_PROPOSAL_CASE,
)

SMOKE_ROUTING_CASES = (
    EXPENSE_DRAFT_CASE,
    FINANCE_SUMMARY_CASE,
    RECIPE_RECOMMENDATION_CASE,
    MENU_PROPOSAL_CASE,
)

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

Общее точное правило выбора существующих рецептов для recipes.recommend и
menu.propose: доступны только ID 101, 102 и 103, перечисленные ниже. Когда запрос
требует выбрать рецепт или составить меню, referenced_ids содержит хотя бы один
из этих существующих ID. Пустой referenced_ids НЕДОПУСТИМ. Каждый ID копируй из
списка доступных рецептов; никогда не придумывай ID.

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
   budget="low". Имена criteria и значение low_cost запрещены. Для referenced_ids
   строго выполняй общее правило выбора существующих рецептов выше.
5. menu.propose -> intent menu_proposal.
   arguments содержит РОВНО days (целое 1..7) и no_repeats (boolean).
   Для referenced_ids строго выполняй общее правило выбора существующих рецептов
   выше; для трех дней выбери все три существующих ID без повторов. Строка
   menu.propose является tool, но никогда intent.

Тестовые read-only рецепты: 101 Овощной суп (35 минут, недорогой), 102 Каша
(15 минут, недорогая), 103 Запеканка (40 минут, средняя цена). Других ID нет.
Если запрос не требует рецептов или меню, referenced_ids обязан быть [].
""".strip()


def routing_response_schema(
    *,
    allowed_ids: frozenset[int] = frozenset(),
    minimum_ids: int = 0,
) -> dict[str, Any]:
    """Return an exact per-case schema derived from the shared routing contract."""
    if minimum_ids < 0 or minimum_ids > len(allowed_ids):
        raise ValueError("minimum_ids must fit within allowed_ids")
    if minimum_ids and not allowed_ids:
        raise ValueError("minimum_ids requires allowed_ids")

    schema = deepcopy(ROUTING_RESPONSE_SCHEMA)
    referenced_ids = schema["properties"]["referenced_ids"]
    referenced_ids["items"]["enum"] = sorted(allowed_ids or SYNTHETIC_RECIPE_IDS)
    if allowed_ids:
        referenced_ids["minItems"] = minimum_ids
        referenced_ids["maxItems"] = len(allowed_ids)
    else:
        referenced_ids["maxItems"] = 0
    return schema


def routing_response_format(schema: dict[str, Any] = ROUTING_RESPONSE_SCHEMA) -> dict[str, Any]:
    """Return llama.cpp's schema-constrained response_format payload."""
    return {"type": "json_schema", "schema": schema}


def structured_diagnostic_request(model: str, case: StructuredDiagnosticCase) -> dict[str, Any]:
    """Build the canonical read-only routing request used by smoke and benchmark."""
    schema = routing_response_schema(
        allowed_ids=case.allowed_ids,
        minimum_ids=case.minimum_ids,
    )
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": ROUTING_SYSTEM_PROMPT},
            {"role": "user", "content": case.prompt},
        ],
        "temperature": 0.0,
        "seed": DIAGNOSTIC_SEED,
        "cache_prompt": False,
        "max_tokens": DIAGNOSTIC_MAX_TOKENS,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": routing_response_format(schema),
    }
