from __future__ import annotations

import json
import re
from dataclasses import dataclass
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


@dataclass(frozen=True)
class RecipeParseCoverage:
    arguments: dict[str, object]
    explicit_recipe_id: int | None
    unresolved_text: str
    needs_semantic_resolution: bool


_CONSTRAINT_BOUNDARY = (
    r"(?=\s+без\b|\s+(?:до|максимум|не\s+больше|не\s+дороже)\b|"
    r"\s+на\s+\d|\s+(?:минут\w*|час\w*)\b|[,;.!?]|$)"
)


def _split_ingredients(raw_value: str) -> list[str]:
    values = re.split(r"\s+(?:и|или)\s+|\s*,\s*", raw_value.strip())
    normalized: list[str] = []
    for value in values:
        ingredient = value.strip(" -")
        if not ingredient:
            continue
        words = ingredient.split()
        if len(words) == 1:
            word = words[0]
            if word.endswith(("ого", "его")) and len(word) > 6:
                word = word[:-3]
            elif word.endswith("ицей") and len(word) > 6:
                word = word[:-2]
            elif word.endswith("ов") and len(word) > 4:
                word = word[:-2]
            elif word.endswith(("ом", "ем")) and len(word) > 5:
                word = word[:-2]
            elif word.endswith(("а", "я", "ы", "и")) and len(word) > 3:
                word = word[:-1]
            ingredient = word
        normalized.append(ingredient)
    return normalized[:5]


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    characters = list(text)
    for start, end in spans:
        characters[start:end] = " " * (end - start)
    remaining = " ".join("".join(characters).split()).strip(" ,.;:!?-")
    remaining = re.sub(
        r"\b(?:хочу|найди|ищи|покажи|дай|подбери|посоветуй|рецепт|рецепты|блюдо|блюда|"
        r"что-нибудь|что\s+есть|что\s+мы|что\s+приготовить|на\s+ужин|ужин|завтра|максимум|до|на|"
        r"what\s+should\s+i\s+cook|tomorrow|dinner|recommend)\b",
        " ",
        remaining,
        flags=re.IGNORECASE,
    )
    return " ".join(remaining.split()).strip(" ,.;:!?-")


def analyze_recipe_text(question: str) -> RecipeParseCoverage:
    normalized = question.casefold().replace("ё", "е")
    arguments: dict[str, object] = {}
    spans: list[tuple[int, int]] = []

    explicit_id_match = re.search(r"\bрецепт(?:а|у|ом)?(?:\s+номер)?\s*#?\s*(\d+)\b", normalized)
    explicit_recipe_id = int(explicit_id_match.group(1)) if explicit_id_match else None
    if explicit_id_match:
        spans.append(explicit_id_match.span())

    exclusion_matches = list(
        re.finditer(rf"\bбез\s+(?P<values>[а-яa-z][а-яa-z -]{{0,80}}?){_CONSTRAINT_BOUNDARY}", normalized)
    )
    exclusions: list[str] = []
    for match in exclusion_matches:
        values = _split_ingredients(match.group("values"))
        if values and not values[0].startswith("повтор"):
            exclusions.extend(values)
        spans.append(match.span())
    exclusions = list(dict.fromkeys(exclusions))[:5]
    if len(exclusions) == 1:
        arguments["exclude_ingredient"] = exclusions[0]
    elif exclusions:
        arguments["exclude_ingredients"] = exclusions

    inclusion_match = re.search(
        rf"\b(?:с|из)\s+(?P<values>[а-яa-z][а-яa-z -]{{0,80}}?){_CONSTRAINT_BOUNDARY}",
        normalized,
    )
    if inclusion_match:
        inclusions = _split_ingredients(inclusion_match.group("values"))
        if inclusions and inclusions[0].startswith(("наш", "сохраненн")):
            inclusions = []
        if len(inclusions) == 1:
            arguments["ingredient"] = inclusions[0]
        elif inclusions:
            arguments["include_ingredients"] = inclusions
        spans.append(inclusion_match.span())

    time_match = re.search(r"(?:до|максимум|не\s+больше)?\s*(\d{1,4})\s*мин\w*", normalized)
    if time_match:
        arguments["max_cook_time"] = int(time_match.group(1))
        spans.append(time_match.span())
    hours_match = re.search(r"(?:до|максимум|не\s+больше)?\s*(\d{1,2})\s*час\w*", normalized)
    if hours_match and "max_cook_time" not in arguments:
        arguments["max_cook_time"] = int(hours_match.group(1)) * 60
        spans.append(hours_match.span())
    if "max_cook_time" not in arguments and re.search(r"не\s+больше\s+час", normalized):
        arguments["max_cook_time"] = 60
        match = re.search(r"не\s+больше\s+час", normalized)
        assert match is not None
        spans.append(match.span())
    cost_match = re.search(r"(?:до|максимум|не\s+дороже)\s*(\d{1,6}(?:[.,]\d{1,2})?)\s*(?:₽|руб)", normalized)
    if cost_match:
        arguments["max_cost"] = Decimal(cost_match.group(1).replace(",", "."))
        spans.append(cost_match.span())
    servings_match = re.search(r"\bна\s+(\d{1,3}(?:[.,]\d{1,2})?)\s+порц", normalized)
    if servings_match:
        arguments["min_servings"] = Decimal(servings_match.group(1).replace(",", "."))
        spans.append(servings_match.span())
    tag_match = re.search(r"\bтег(?:ом)?\s+([а-яa-z0-9_-]{1,50})", normalized)
    if tag_match:
        arguments["tags"] = [tag_match.group(1)]
        spans.append(tag_match.span())

    for understood_pattern in (
        r"\b(?:недорог|дешев)\w*\b",
        r"\bдавно\s+не\s+готов\w*\b",
        r"\bнедавно\s+в\s+меню\b",
        r"\bпоследн\w*\s+меню\b",
    ):
        understood_match = re.search(understood_pattern, normalized)
        if understood_match:
            spans.append(understood_match.span())

    search_match = re.match(
        rf"\s*(?:найди|ищи)\s+(?:рецепт\s+)?(?P<query>(?!без\b)[^,;]{{2,100}}?){_CONSTRAINT_BOUNDARY}",
        normalized,
    )
    if search_match and explicit_recipe_id is None:
        query = search_match.group("query").strip()
        if query and query not in {"рецепт", "рецепты"}:
            arguments["query"] = query
            spans.append(search_match.span())

    unresolved_text = _remove_spans(normalized, spans)
    semantic_words = [word for word in re.findall(r"[а-яa-z]+", unresolved_text) if len(word) > 2]
    return RecipeParseCoverage(
        arguments=arguments,
        explicit_recipe_id=explicit_recipe_id,
        unresolved_text=unresolved_text,
        needs_semantic_resolution=explicit_recipe_id is None and bool(semantic_words),
    )


def recipe_search_arguments_from_text(question: str) -> dict[str, object]:
    return analyze_recipe_text(question).arguments


def select_deterministic_recipe_tool(question: str) -> RecipeToolCall | None:
    """Route common recipe questions without exposing the catalogue to a model."""
    normalized = question.casefold().replace("ё", "е")
    analysis = analyze_recipe_text(question)
    arguments = analysis.arguments

    if analysis.explicit_recipe_id is not None:
        return RecipeToolCall(
            tool=RecipeToolName.DETAILS,
            arguments={"recipe_id": analysis.explicit_recipe_id},
        )

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


def _selection_request(question: str, analysis: RecipeParseCoverage) -> AICompletionRequest:
    tools_json = json.dumps(recipe_tool_descriptions(), ensure_ascii=False, separators=(",", ":"))
    semantic_context = json.dumps(
        {
            "original_question": question,
            "fixed_arguments": analysis.arguments,
            "unresolved_text": analysis.unresolved_text,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return AICompletionRequest(
        messages=[
            {
                "role": "system",
                "content": (
                    "You route only Home OS recipe read tools. Select exactly one allow-listed read-only tool. "
                    "Never emit SQL, user_id, create/update/delete actions, menu actions, or invented recipe data. "
                    "For get_recipe_details, recipe_id must be an integer supplied by the user; otherwise choose search or recommend. "
                    "For search_recipes and recommend_recipes, exact allowed arguments are query, ingredient, "
                    "exclude_ingredient, include_ingredients, exclude_ingredients, max_cook_time, max_cost, "
                    "min_servings, tags, limit; recommend_recipes also allows sort_by=relevance|cost|time|last_cooked. "
                    "ingredient/exclude_ingredient are strings; include_ingredients/exclude_ingredients and tags are arrays; "
                    "max_cook_time is minutes; max_cost, min_servings and limit are numbers. Preserve all fixed_arguments "
                    "exactly and interpret unresolved_text without reversing include/exclude meaning. "
                    'Return strict JSON: {"tool":"<name>","arguments":{}}. '
                    f"Allowed tools: {tools_json}"
                ),
            },
            {"role": "user", "content": semantic_context},
        ],
        max_tokens=180,
        temperature=0.0,
        output_mode="json",
        enable_thinking=False,
    )


def _merge_recipe_call(selection: RecipeToolCall, analysis: RecipeParseCoverage) -> RecipeToolCall:
    if selection.tool not in {RecipeToolName.SEARCH, RecipeToolName.RECOMMEND}:
        if analysis.arguments:
            raise AIResponseValidationError("AI discarded deterministic recipe constraints")
        return selection
    return selection.model_copy(update={"arguments": {**selection.arguments, **analysis.arguments}})


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
    analysis = analyze_recipe_text(question)
    call = select_deterministic_recipe_tool(question)
    if call is None or analysis.needs_semantic_resolution:
        selection = await client.complete_json(_selection_request(question, analysis), RecipeToolCall)
        call = _merge_recipe_call(selection, analysis)
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
