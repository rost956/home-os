#!/usr/bin/env python3
"""Manual, data-safe Qwen 2B/4B comparison for Raspberry Pi 5."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from home_ai_benchmark_contract import (
    ALLOWED_INTENTS,
    ALLOWED_TOOLS,
    ROUTING_SYSTEM_PROMPT,
    SYNTHETIC_RECIPE_IDS,
    routing_response_schema,
)
from home_ai_runtime_smoke import (
    PromptCase,
    SmokeFailure,
    _memory_metrics,
    _read_api_key,
    _stream_chat,
)


@dataclass(frozen=True)
class BenchmarkCase:
    prompt: str
    intent: str
    tool: str
    required_arguments: dict[str, Any]
    allowed_ids: frozenset[int] = frozenset()
    minimum_ids: int = 0


CASES = (
    BenchmarkCase(
        "Лента 1840 вчера",
        "expense_draft",
        "expense.create_draft",
        {"merchant": "Лента", "amount": 1840, "date_hint": "вчера"},
    ),
    BenchmarkCase(
        "Бензин 2600 сегодня",
        "expense_draft",
        "expense.create_draft",
        {"merchant": "Бензин", "amount": 2600, "date_hint": "сегодня"},
    ),
    BenchmarkCase(
        "Сколько я потратил в этом месяце?",
        "finance_question",
        "finance.summary",
        {"period": "current_month"},
    ),
    BenchmarkCase(
        "Почему расходы выросли?",
        "finance_question",
        "finance.comparison",
        {"period": "current_month"},
    ),
    BenchmarkCase(
        "Что приготовить максимум за 40 минут?",
        "recipe_query",
        "recipes.recommend",
        {"max_minutes": 40},
        SYNTHETIC_RECIPE_IDS,
        1,
    ),
    BenchmarkCase(
        "Выбери недорогой рецепт из наших",
        "recipe_query",
        "recipes.recommend",
        {"budget": "low"},
        SYNTHETIC_RECIPE_IDS,
        1,
    ),
    BenchmarkCase(
        "Составь меню на три дня без повторов",
        "menu_proposal",
        "menu.propose",
        {"days": 3, "no_repeats": True},
        SYNTHETIC_RECIPE_IDS,
        3,
    ),
)


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_contract_shape(payload: Any) -> bool:
    if not isinstance(payload, dict) or set(payload) != {"intent", "tool", "arguments", "referenced_ids"}:
        return False
    if payload["intent"] not in ALLOWED_INTENTS or payload["tool"] not in ALLOWED_TOOLS:
        return False
    arguments = payload["arguments"]
    if not isinstance(arguments, dict):
        return False
    allowed_argument_keys = {
        "merchant",
        "amount",
        "date_hint",
        "period",
        "max_minutes",
        "budget",
        "days",
        "no_repeats",
    }
    if not set(arguments).issubset(allowed_argument_keys):
        return False
    if "merchant" in arguments and (not isinstance(arguments["merchant"], str) or not arguments["merchant"]):
        return False
    if "amount" in arguments and (not _is_integer(arguments["amount"]) or arguments["amount"] < 1):
        return False
    if "date_hint" in arguments and arguments["date_hint"] not in {"сегодня", "вчера"}:
        return False
    if "period" in arguments and arguments["period"] != "current_month":
        return False
    if "max_minutes" in arguments and (
        not _is_integer(arguments["max_minutes"]) or not 1 <= arguments["max_minutes"] <= 480
    ):
        return False
    if "budget" in arguments and arguments["budget"] != "low":
        return False
    if "days" in arguments and (not _is_integer(arguments["days"]) or not 1 <= arguments["days"] <= 7):
        return False
    if "no_repeats" in arguments and not isinstance(arguments["no_repeats"], bool):
        return False
    ids = payload["referenced_ids"]
    return (
        isinstance(ids, list)
        and all(_is_integer(item) and item in SYNTHETIC_RECIPE_IDS for item in ids)
        and len(ids) == len(set(ids))
    )


def _grade(case: BenchmarkCase, content: str) -> tuple[dict[str, bool], dict[str, Any] | None]:
    scores = {
        "valid_structured_output": False,
        "correct_intent": False,
        "correct_tool": False,
        "correct_arguments": False,
        "no_invented_ids": False,
    }
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return scores, None
    if not isinstance(payload, dict):
        return scores, None
    scores["valid_structured_output"] = _valid_contract_shape(payload)
    scores["correct_intent"] = payload.get("intent") == case.intent
    scores["correct_tool"] = payload.get("tool") == case.tool

    arguments = payload.get("arguments")
    if isinstance(arguments, dict):
        scores["correct_arguments"] = arguments == case.required_arguments

    ids = payload.get("referenced_ids")
    if isinstance(ids, list) and all(isinstance(item, int) for item in ids):
        if case.allowed_ids:
            scores["no_invented_ids"] = (
                len(ids) >= case.minimum_ids
                and len(ids) == len(set(ids))
                and set(ids).issubset(case.allowed_ids)
            )
        else:
            scores["no_invented_ids"] = ids == []
    return scores, payload


def _print_case_result(
    index: int,
    case: BenchmarkCase,
    scores: dict[str, bool],
    payload: Any,
    raw_content: str,
) -> None:
    print(f"[{index}/{len(CASES)}] {case.prompt}: {scores}")
    if all(scores.values()):
        return
    expected = {"intent": case.intent, "tool": case.tool, "arguments": case.required_arguments}
    if isinstance(payload, dict):
        actual = {
            "intent": payload.get("intent"),
            "tool": payload.get("tool"),
            "arguments": payload.get("arguments"),
        }
    else:
        actual = {"intent": None, "tool": None, "arguments": None}
    print(f"  expected: {json.dumps(expected, ensure_ascii=False, sort_keys=True)}")
    print(f"  actual:   {json.dumps(actual, ensure_ascii=False, sort_keys=True)}")
    if not isinstance(payload, dict):
        print(f"  raw:      {raw_content}")


def _aggregate(report: dict[str, Any]) -> dict[str, Any]:
    cases = report.get("cases", [])
    totals = {
        key: sum(bool(case.get("scores", {}).get(key)) for case in cases)
        for key in (
            "valid_structured_output",
            "correct_intent",
            "correct_tool",
            "correct_arguments",
            "no_invented_ids",
        )
    }
    latencies = [case["metrics"]["total_seconds"] for case in cases if case.get("metrics")]
    token_rates = [
        case["metrics"]["tokens_per_second"]
        for case in cases
        if case.get("metrics", {}).get("tokens_per_second") is not None
    ]
    rss_values = [
        case["metrics"]["llama_rss_kib"]
        for case in cases
        if case.get("metrics", {}).get("llama_rss_kib") is not None
    ]
    return {
        **totals,
        "case_count": len(cases),
        "median_total_seconds": round(statistics.median(latencies), 3) if latencies else None,
        "median_tokens_per_second": round(statistics.median(token_rates), 3) if token_rates else None,
        "peak_llama_rss_kib": max(rss_values) if rss_values else None,
    }


def _compare(paths: list[Path]) -> int:
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    print("candidate\tstructured\tintent\ttool\targuments\tIDs\tmedian_s\tmedian_tok/s\tpeak_RSS_KiB")
    for report in reports:
        summary = report.get("summary") or _aggregate(report)
        count = summary["case_count"]
        print(
            f"{report.get('candidate', report.get('model'))}\t"
            f"{summary['valid_structured_output']}/{count}\t"
            f"{summary['correct_intent']}/{count}\t"
            f"{summary['correct_tool']}/{count}\t"
            f"{summary['correct_arguments']}/{count}\t"
            f"{summary['no_invented_ids']}/{count}\t"
            f"{summary['median_total_seconds']}\t"
            f"{summary['median_tokens_per_second']}\t"
            f"{summary['peak_llama_rss_kib']}"
        )
    print("No winner is selected automatically; review failed cases and Pi responsiveness before choosing.")
    return 0


def _mock_report() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for case in CASES:
        payload = {
            "intent": case.intent,
            "tool": case.tool,
            "arguments": case.required_arguments,
            "referenced_ids": sorted(case.allowed_ids)[: case.minimum_ids],
        }
        scores, parsed = _grade(case, json.dumps(payload, ensure_ascii=False))
        cases.append({"prompt": case.prompt, "response": parsed, "scores": scores, "metrics": {}})
    report: dict[str, Any] = {"candidate": "mock", "model": "mock", "cases": cases}
    report["summary"] = _aggregate(report)
    return report


def _prompt_case(index: int, case: BenchmarkCase) -> PromptCase:
    return PromptCase(
        name=f"benchmark_{index}",
        system=ROUTING_SYSTEM_PROMPT,
        user=case.prompt,
        expected={},
        allowed_ids=case.allowed_ids,
        minimum_ids=case.minimum_ids,
        response_schema=routing_response_schema(
            allowed_ids=case.allowed_ids,
            minimum_ids=case.minimum_ids,
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://172.17.0.1:8081/v1")
    parser.add_argument("--model", default="qwen3.5-4b-q4_k_m")
    parser.add_argument("--candidate", default="Qwen3.5-4B Q4_K_M")
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--service", default="home-ai-llama.service")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("REPORT_A", "REPORT_B"))
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    if args.compare:
        return _compare(args.compare)
    if args.mock:
        report = _mock_report()
    else:
        api_key = _read_api_key(args.api_key_file)
        report = {"candidate": args.candidate, "model": args.model, "cases": []}
        for index, case in enumerate(CASES, start=1):
            prompt_case = _prompt_case(index, case)
            content, metrics = _stream_chat(
                args.base_url.rstrip("/"), args.model, api_key, args.timeout, prompt_case
            )
            scores, parsed = _grade(case, content)
            report["cases"].append(
                {
                    "prompt": case.prompt,
                    "expected": {
                        "intent": case.intent,
                        "tool": case.tool,
                        "arguments": case.required_arguments,
                        "allowed_ids": sorted(case.allowed_ids),
                    },
                    "response": parsed if parsed is not None else content,
                    "scores": scores,
                    "metrics": {**metrics, **_memory_metrics(args.service)},
                }
            )
            _print_case_result(index, case, scores, parsed, content)
        report["summary"] = _aggregate(report)

    output = args.output or Path(f"home-ai-benchmark-{args.model}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    print(f"report: {output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SmokeFailure, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"BENCHMARK FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
