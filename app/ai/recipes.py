from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.models import User
from app.timezone import today_msk

from .client import AIClient
from .errors import AIResponseValidationError
from .schemas import AICompletionRequest
from .tools.recipes import (
    RecipeToolCall,
    RecipeToolName,
    RecipeToolResult,
    execute_recipe_tool,
    recipe_tool_descriptions,
)


class RecipeQuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=3, max_length=500)


class RecipeExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    answer: str = Field(min_length=1, max_length=2_000)
    recipe_ids: list[int] = Field(default_factory=list, max_length=8)


class RecipeQuestionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    tool: RecipeToolName
    result: RecipeToolResult
    answer: str
    recipe_ids: list[int] = Field(default_factory=list, max_length=8)


def recipe_search_arguments_from_text(question: str) -> dict[str, object]:
    normalized = question.casefold().replace("ё", "е")
    arguments: dict[str, object] = {}

    ingredient_match = re.search(r"\bс\s+([а-яa-z][а-яa-z -]{1,40})", normalized)
    if ingredient_match and any(word in normalized for word in ("хочу", "блюд", "рецепт", "что-нибудь")):
        arguments["ingredient"] = ingredient_match.group(1).strip().split()[0]
    if "куриц" in normalized:
        arguments["ingredient"] = "куриц"
    if "рыб" in normalized and re.search(r"\bбез\b", normalized):
        arguments["exclude_ingredient"] = "рыб"

    time_match = re.search(r"(?:до|максимум|не\s+больше)?\s*(\d{1,4})\s*мин", normalized)
    if time_match:
        arguments["max_cook_time"] = int(time_match.group(1))
    hours_match = re.search(r"(?:до|максимум|не\s+больше)?\s*(\d{1,2})\s*час", normalized)
    if hours_match and "max_cook_time" not in arguments:
        arguments["max_cook_time"] = int(hours_match.group(1)) * 60
    if "max_cook_time" not in arguments and re.search(r"не\s+больше\s+час", normalized):
        arguments["max_cook_time"] = 60
    cost_match = re.search(r"(?:до|максимум|не\s+дороже)\s*(\d{1,6}(?:[.,]\d{1,2})?)\s*(?:₽|руб)", normalized)
    if cost_match:
        arguments["max_cost"] = Decimal(cost_match.group(1).replace(",", "."))
    servings_match = re.search(r"\bна\s+(\d{1,3}(?:[.,]\d{1,2})?)\s+порц", normalized)
    if servings_match:
        arguments["min_servings"] = Decimal(servings_match.group(1).replace(",", "."))
    tag_match = re.search(r"\bтег(?:ом)?\s+([а-яa-z0-9_-]{1,50})", normalized)
    if tag_match:
        arguments["tags"] = [tag_match.group(1)]
    return arguments


def select_deterministic_recipe_tool(question: str) -> RecipeToolCall | None:
    """Route common recipe questions without exposing the catalogue to a model."""
    normalized = question.casefold().replace("ё", "е")
    arguments = recipe_search_arguments_from_text(question)

    if re.search(r"\bнедавно\s+в\s+меню\b|\bпоследн\w*\s+меню\b", normalized):
        return RecipeToolCall(tool=RecipeToolName.RECENT_MENU, arguments={})
    if re.search(r"\bдавно\s+не\s+готов", normalized):
        return RecipeToolCall(
            tool=RecipeToolName.RECOMMEND,
            arguments={**arguments, "sort_by": "last_cooked", "limit": 5},
        )
    if "недорог" in normalized or "дешев" in normalized:
        return RecipeToolCall(
            tool=RecipeToolName.RECOMMEND,
            arguments={**arguments, "sort_by": "cost", "limit": 5},
        )
    if re.search(r"\b(завтра|ужин|что\s+приготовить|подбери|посоветуй|tomorrow|dinner|what\s+should\s+i\s+cook|recommend)\b", normalized):
        return RecipeToolCall(tool=RecipeToolName.RECOMMEND, arguments={**arguments, "limit": 5})
    if arguments:
        return RecipeToolCall(tool=RecipeToolName.SEARCH, arguments=arguments)
    search_match = re.match(r"\s*(?:найди|ищи|покажи)\s+(.{2,100})", question, flags=re.IGNORECASE)
    if search_match:
        return RecipeToolCall(tool=RecipeToolName.SEARCH, arguments={"query": search_match.group(1).strip()})
    return None


def _selection_request(question: str) -> AICompletionRequest:
    tools_json = json.dumps(recipe_tool_descriptions(), ensure_ascii=False, separators=(",", ":"))
    return AICompletionRequest(
        messages=[
            {
                "role": "system",
                "content": (
                    "You route only Home OS recipe read tools. Select exactly one allow-listed read-only tool. "
                    "Never emit SQL, user_id, create/update/delete actions, menu actions, or invented recipe data. "
                    "For get_recipe_details, recipe_id must be an integer supplied by the user; otherwise choose search or recommend. "
                    'Return strict JSON: {"tool":"<name>","arguments":{}}. '
                    f"Allowed tools: {tools_json}"
                ),
            },
            {"role": "user", "content": question},
        ],
        max_tokens=180,
        temperature=0.0,
        output_mode="json",
    )


def _explanation_request(question: str, result: RecipeToolResult) -> AICompletionRequest:
    result_json = result.model_dump_json()
    return AICompletionRequest(
        messages=[
            {
                "role": "system",
                "content": (
                    "Explain only the supplied Home OS recipe tool result. Every recipe title and recipe_id you mention "
                    "must be present in that result. Put every recipe you recommend into recipe_ids. Do not invent recipes, "
                    "ingredients, cooking history, prices, or menu entries. "
                    "If cooking_history_available is false, say there is no recorded cooking-timer history instead of claiming "
                    "that a recipe was not cooked. Recent menu entries are plans, not proof of cooking. Do not propose writes. "
                    'Return strict JSON: {"answer":"...","recipe_ids":[]}.'
                ),
            },
            {"role": "user", "content": f"Question: {question}\nRead-only result: {result_json}"},
        ],
        max_tokens=320,
        temperature=0.1,
        output_mode="json",
    )


def _result_recipe_ids(result: RecipeToolResult) -> set[int]:
    if result.tool in {RecipeToolName.SEARCH, RecipeToolName.RECOMMEND}:
        return {candidate.recipe_id for candidate in result.data.candidates}
    if result.tool == RecipeToolName.DETAILS:
        return {result.data.recipe.recipe_id} if result.data.found and result.data.recipe else set()
    return {entry.recipe_id for entry in result.data.entries}


async def answer_recipe_question(
    db: Session,
    user: User,
    client: AIClient,
    question: str,
    *,
    today: date | None = None,
) -> RecipeQuestionResponse:
    """Run one bounded read-only recipe tool, then let the model explain its result."""
    call = select_deterministic_recipe_tool(question)
    if call is None:
        call = await client.complete_json(_selection_request(question), RecipeToolCall)
    result = execute_recipe_tool(db, user, call, today=today or today_msk())
    explanation = await client.complete_json(_explanation_request(question, result), RecipeExplanation)
    available_recipe_ids = _result_recipe_ids(result)
    if not set(explanation.recipe_ids).issubset(available_recipe_ids):
        raise AIResponseValidationError("AI explanation referenced a recipe outside the tool result")
    return RecipeQuestionResponse(
        question=question,
        tool=call.tool,
        result=result,
        answer=explanation.answer,
        recipe_ids=explanation.recipe_ids,
    )
