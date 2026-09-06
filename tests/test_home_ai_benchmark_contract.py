from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from home_ai_benchmark import CASES, _grade, _print_case_result  # noqa: E402
from home_ai_benchmark_contract import (  # noqa: E402
    ALLOWED_INTENTS,
    ALLOWED_TOOLS,
    ROUTING_RESPONSE_SCHEMA,
    ROUTING_SYSTEM_PROMPT,
    routing_response_format,
)
from home_ai_runtime_smoke import (  # noqa: E402
    CASES as SMOKE_CASES,
)
from home_ai_runtime_smoke import (  # noqa: E402
    SmokeFailure,
    _assert_case,
    _chat_payload,
)


def _response(case_index: int, **overrides: object) -> str:
    case = CASES[case_index]
    payload: dict[str, object] = {
        "intent": case.intent,
        "tool": case.tool,
        "arguments": case.required_arguments,
        "referenced_ids": sorted(case.allowed_ids)[: case.minimum_ids],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def test_shared_contract_enumerates_exact_names_schema_and_routing_examples():
    assert ALLOWED_INTENTS == ("expense_draft", "finance_question", "recipe_query", "menu_proposal")
    assert ALLOWED_TOOLS == (
        "expense.create_draft",
        "finance.summary",
        "finance.comparison",
        "recipes.recommend",
        "menu.propose",
    )
    assert ROUTING_RESPONSE_SCHEMA["required"] == ["intent", "tool", "arguments", "referenced_ids"]
    assert ROUTING_RESPONSE_SCHEMA["additionalProperties"] is False
    assert routing_response_format() == {"type": "json_schema", "schema": ROUTING_RESPONSE_SCHEMA}

    for internal_name in (*ALLOWED_INTENTS, *ALLOWED_TOOLS):
        assert internal_name in ROUTING_SYSTEM_PROMPT
    for exact_argument in (
        "merchant",
        "amount",
        "date_hint",
        "period",
        "max_minutes",
        "budget",
        "days",
        "no_repeats",
    ):
        assert exact_argument in ROUTING_SYSTEM_PROMPT
    assert "Лента 1840 вчера" in ROUTING_SYSTEM_PROMPT
    assert "Бензин 2600 сегодня" in ROUTING_SYSTEM_PROMPT
    assert "Почему расходы выросли?" in ROUTING_SYSTEM_PROMPT
    assert "не перефразируй" in ROUTING_SYSTEM_PROMPT
    assert "max_time_minutes запрещено" in ROUTING_SYSTEM_PROMPT
    assert "criteria" in ROUTING_SYSTEM_PROMPT and "low_cost" in ROUTING_SYSTEM_PROMPT


def test_benchmark_grades_intent_tool_and_arguments_independently_and_exactly():
    success_scores, _ = _grade(CASES[2], _response(2))
    assert all(success_scores.values())

    wrong_intent_scores, _ = _grade(CASES[2], _response(2, intent="recipe_query"))
    assert wrong_intent_scores["correct_intent"] is False
    assert wrong_intent_scores["correct_tool"] is True
    assert wrong_intent_scores["correct_arguments"] is True

    wrong_tool_scores, _ = _grade(CASES[3], _response(3, tool="finance.summary"))
    assert wrong_tool_scores["correct_intent"] is True
    assert wrong_tool_scores["correct_tool"] is False
    assert wrong_tool_scores["correct_arguments"] is True

    wrong_arguments_scores, _ = _grade(
        CASES[4],
        _response(4, arguments={"max_time_minutes": 40}),
    )
    assert wrong_arguments_scores["correct_intent"] is True
    assert wrong_arguments_scores["correct_tool"] is True
    assert wrong_arguments_scores["correct_arguments"] is False
    assert wrong_arguments_scores["valid_structured_output"] is False


def test_benchmark_rejects_ids_for_non_recipe_routes_and_unknown_recipe_ids():
    finance_scores, _ = _grade(CASES[2], _response(2, referenced_ids=[101]))
    assert finance_scores["valid_structured_output"] is True
    assert finance_scores["no_invented_ids"] is False

    recipe_scores, _ = _grade(CASES[4], _response(4, referenced_ids=[999]))
    assert recipe_scores["valid_structured_output"] is False
    assert recipe_scores["no_invented_ids"] is False


def test_failed_benchmark_case_prints_expected_and_actual_contract(capsys: pytest.CaptureFixture[str]):
    case = CASES[0]
    raw = _response(
        0,
        intent="finance_question",
        tool="finance.comparison",
        arguments={"period": "current_month"},
    )
    scores, payload = _grade(case, raw)

    _print_case_result(1, case, scores, payload, raw)

    output = capsys.readouterr().out
    assert "expected:" in output and "actual:" in output
    assert '"intent": "expense_draft"' in output
    assert '"tool": "expense.create_draft"' in output
    assert '"intent": "finance_question"' in output
    assert '"tool": "finance.comparison"' in output
    assert '"merchant": "Лента"' in output
    assert '"period": "current_month"' in output


def test_smoke_schema_cases_disable_thinking_and_use_schema_constraints():
    for case in SMOKE_CASES:
        if case.expected is None:
            continue
        payload = _chat_payload("test-model", case)
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        assert payload["response_format"]["type"] == "json_schema"

    expense_case = next(case for case in SMOKE_CASES if case.name == "expense_parse_read_only")
    assert expense_case.system == ROUTING_SYSTEM_PROMPT
    assert expense_case.expected == {
        "intent": "expense_draft",
        "tool": "expense.create_draft",
        "arguments": {"merchant": "Лента", "amount": 1840, "date_hint": "вчера"},
        "referenced_ids": [],
    }


def test_smoke_failure_includes_actual_json():
    expense_case = next(case for case in SMOKE_CASES if case.name == "expense_parse_read_only")
    actual = {
        "intent": "finance_question",
        "tool": "finance.summary",
        "arguments": {"period": "current_month"},
        "referenced_ids": [],
    }

    with pytest.raises(SmokeFailure) as error:
        _assert_case(expense_case, json.dumps(actual, ensure_ascii=False))

    message = str(error.value)
    assert "actual=" in message
    assert "finance.summary" in message
    assert "expense.create_draft" in message
