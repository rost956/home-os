#!/usr/bin/env python3
"""Manual Raspberry Pi smoke checks for the host llama-server.

This script is intentionally not part of normal CI. ``--mock`` exercises its
parsers and assertions without contacting a model or systemd.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from home_ai_benchmark_contract import (
    ROUTING_RESPONSE_SCHEMA,
    ROUTING_SYSTEM_PROMPT,
    SYNTHETIC_RECIPE_IDS,
    routing_response_format,
)


class SmokeFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class PromptCase:
    name: str
    system: str
    user: str
    expected: dict[str, Any] | None = None
    allowed_ids: frozenset[int] = frozenset()
    minimum_ids: int = 0
    response_schema: dict[str, Any] | None = None


STATUS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "count"],
    "properties": {
        "status": {"type": "string", "enum": ["ok"]},
        "count": {"type": "integer", "enum": [2]},
    },
}


CASES = (
    PromptCase(
        name="basic_russian",
        system="Отвечай по-русски, кратко и без Markdown.",
        user="Назови один безопасный способ сэкономить время при готовке.",
    ),
    PromptCase(
        name="structured_json",
        system='Верни только JSON: {"status":"ok","count":2}.',
        user="Сформируй тестовый структурированный ответ.",
        expected={"status": "ok", "count": 2},
        response_schema=STATUS_RESPONSE_SCHEMA,
    ),
    PromptCase(
        name="expense_parse_read_only",
        system=ROUTING_SYSTEM_PROMPT,
        user="Лента 1840 вчера",
        expected={
            "intent": "expense_draft",
            "tool": "expense.create_draft",
            "arguments": {"merchant": "Лента", "amount": 1840, "date_hint": "вчера"},
            "referenced_ids": [],
        },
        response_schema=ROUTING_RESPONSE_SCHEMA,
    ),
    PromptCase(
        name="finance_read_only",
        system=ROUTING_SYSTEM_PROMPT,
        user="Сколько я потратил в этом месяце?",
        expected={
            "intent": "finance_question",
            "tool": "finance.summary",
            "arguments": {"period": "current_month"},
            "referenced_ids": [],
        },
        response_schema=ROUTING_RESPONSE_SCHEMA,
    ),
    PromptCase(
        name="recipe_read_only",
        system=ROUTING_SYSTEM_PROMPT,
        user="Выбери рецепт максимум за 40 минут",
        expected={
            "intent": "recipe_query",
            "tool": "recipes.recommend",
            "arguments": {"max_minutes": 40},
        },
        allowed_ids=frozenset({101, 102, 103}),
        minimum_ids=1,
        response_schema=ROUTING_RESPONSE_SCHEMA,
    ),
    PromptCase(
        name="menu_proposal_no_write",
        system=ROUTING_SYSTEM_PROMPT,
        user=(
            "Составь меню на один день без повторов из доступных рецептов. "
            "Выбери существующий рецепт, но не подтверждай, не применяй и не записывай меню."
        ),
        expected={
            "intent": "menu_proposal",
            "tool": "menu.propose",
            "arguments": {"days": 1, "no_repeats": True},
        },
        allowed_ids=SYNTHETIC_RECIPE_IDS,
        minimum_ids=1,
        response_schema=ROUTING_RESPONSE_SCHEMA,
    ),
)


def _read_api_key(path: Path | None) -> str | None:
    if path is None:
        return os.getenv("AI_API_KEY", "").strip() or None
    keys = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    keys = [line for line in keys if line and not line.startswith("#")]
    if not keys:
        raise SmokeFailure(f"no API key in {path}")
    return keys[0]


def _headers(api_key: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _request_json(
    method: str,
    url: str,
    api_key: str | None,
    timeout: float,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, headers=_headers(api_key), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise SmokeFailure(f"{method} {url} failed: {exc}") from exc
    if not isinstance(result, dict):
        raise SmokeFailure(f"{method} {url} returned a non-object JSON value")
    return result


def _chat_payload(model: str, case: PromptCase) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": case.system},
            {"role": "user", "content": case.user},
        ],
        "temperature": 0.0,
        "max_tokens": 256,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if case.response_schema is not None:
        payload["response_format"] = (
            routing_response_format()
            if case.response_schema is ROUTING_RESPONSE_SCHEMA
            else {"type": "json_schema", "schema": case.response_schema}
        )
    elif case.expected is not None:
        payload["response_format"] = {"type": "json_object"}
    return payload


def _stream_chat(
    base_url: str,
    model: str,
    api_key: str | None,
    timeout: float,
    case: PromptCase,
) -> tuple[str, dict[str, Any]]:
    payload = _chat_payload(model, case)

    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers=_headers(api_key),
        method="POST",
    )
    started = time.perf_counter()
    first_token_seconds: float | None = None
    pieces: list[str] = []
    usage: dict[str, Any] = {}
    timings: dict[str, Any] = {}
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                event = json.loads(data)
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                if isinstance(event.get("timings"), dict):
                    timings = event["timings"]
                choices = event.get("choices") or []
                if not choices:
                    continue
                content = choices[0].get("delta", {}).get("content")
                if isinstance(content, str) and content:
                    if first_token_seconds is None:
                        first_token_seconds = time.perf_counter() - started
                    pieces.append(content)
    except (OSError, urllib.error.HTTPError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeFailure(f"chat case {case.name} failed: {exc}") from exc

    total_seconds = time.perf_counter() - started
    content = "".join(pieces).strip()
    if not content:
        raise SmokeFailure(f"chat case {case.name} returned empty content")
    metrics = {
        "first_token_seconds": first_token_seconds,
        "total_seconds": round(total_seconds, 3),
        "prompt_tokens": usage.get("prompt_tokens", timings.get("prompt_n")),
        "generated_tokens": usage.get("completion_tokens", timings.get("predicted_n")),
        "tokens_per_second": timings.get("predicted_per_second"),
    }
    return content, metrics


def _assert_case(case: PromptCase, content: str) -> dict[str, Any] | None:
    if case.expected is None:
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise SmokeFailure(f"{case.name} did not return valid JSON: {content[:200]}") from exc
    if not isinstance(parsed, dict):
        raise SmokeFailure(f"{case.name} returned JSON that is not an object: actual={content}")
    expected_keys = set(case.expected)
    if case.allowed_ids:
        expected_keys.add("referenced_ids")
    if set(parsed) != expected_keys:
        raise SmokeFailure(
            f"{case.name}: response keys differ; "
            f"expected={json.dumps(case.expected, ensure_ascii=False, sort_keys=True)}; "
            f"actual={json.dumps(parsed, ensure_ascii=False, sort_keys=True)}"
        )
    for key, expected_value in case.expected.items():
        if parsed.get(key) != expected_value:
            raise SmokeFailure(
                f"{case.name}: contract mismatch; "
                f"expected={json.dumps(case.expected, ensure_ascii=False, sort_keys=True)}; "
                f"actual={json.dumps(parsed, ensure_ascii=False, sort_keys=True)}"
            )
    if case.allowed_ids:
        recipe_ids = parsed.get("referenced_ids")
        if (
            not isinstance(recipe_ids, list)
            or len(recipe_ids) < case.minimum_ids
            or len(recipe_ids) != len(set(recipe_ids))
            or any(not isinstance(item, int) or item not in case.allowed_ids for item in recipe_ids)
        ):
            raise SmokeFailure(
                f"{case.name}: invalid referenced_ids; expected subset of {sorted(case.allowed_ids)} "
                f"with at least {case.minimum_ids} item(s); "
                f"actual={json.dumps(parsed, ensure_ascii=False, sort_keys=True)}"
            )
    return parsed


def _memory_metrics(service: str) -> dict[str, int | None]:
    result: dict[str, int | None] = {"llama_rss_kib": None, "system_available_kib": None}
    try:
        pid_text = subprocess.check_output(
            ["systemctl", "show", "--property", "MainPID", "--value", service],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        pid = int(pid_text)
        if pid > 0:
            status_lines = Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines()
            rss_line = next(line for line in status_lines if line.startswith("VmRSS:"))
            result["llama_rss_kib"] = int(rss_line.split()[1])
    except (FileNotFoundError, ValueError, subprocess.CalledProcessError, StopIteration):
        pass
    try:
        memory_lines = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
        available_line = next(line for line in memory_lines if line.startswith("MemAvailable:"))
        result["system_available_kib"] = int(available_line.split()[1])
    except (FileNotFoundError, ValueError, StopIteration):
        pass
    return result


def _check_systemd(service: str) -> None:
    try:
        subprocess.run(["systemctl", "is-active", "--quiet", service], check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise SmokeFailure(f"systemd service is not active: {service}") from exc


def _check_container(compose_dir: Path) -> None:
    probe = """
import json, os, urllib.request
base = os.environ.get('AI_BASE_URL', '').rstrip('/')
assert base, 'AI_BASE_URL is empty in web container'
headers = {'Accept': 'application/json'}
key = os.environ.get('AI_API_KEY', '').strip()
if key:
    headers['Authorization'] = 'Bearer ' + key
request = urllib.request.Request(base + '/models', headers=headers)
with urllib.request.urlopen(request, timeout=10) as response:
    payload = json.load(response)
assert isinstance(payload.get('data'), list) and payload['data'], 'no loaded model'
print(payload['data'][0]['id'])
""".strip()
    try:
        subprocess.run(
            ["docker", "compose", "exec", "-T", "web", "python", "-c", probe],
            cwd=compose_dir,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise SmokeFailure("web container cannot reach AI_BASE_URL/models") from exc


def _mock_run() -> int:
    for case in CASES:
        if case.expected is None:
            _assert_case(case, "Короткий тестовый ответ.")
            continue
        payload = dict(case.expected)
        if case.allowed_ids:
            payload["referenced_ids"] = sorted(case.allowed_ids)[: case.minimum_ids]
        _assert_case(case, json.dumps(payload, ensure_ascii=False))
    print(f"mock smoke passed: {len(CASES)} cases; no network or model used")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://172.17.0.1:8081/v1")
    parser.add_argument("--model", default="qwen3.5-4b-q4_k_m")
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--service", default="home-ai-llama.service")
    parser.add_argument("--compose-dir", type=Path, default=Path("/opt/recipe_budget_service"))
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--skip-systemd", action="store_true")
    parser.add_argument("--skip-container", action="store_true")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    if args.mock:
        return _mock_run()
    if args.timeout <= 0:
        raise SmokeFailure("--timeout must be positive")

    base_url = args.base_url.rstrip("/")
    api_key = _read_api_key(args.api_key_file)
    if not args.skip_systemd:
        _check_systemd(args.service)

    health = _request_json("GET", f"{base_url}/health", None, args.timeout)
    if health.get("status") != "ok":
        raise SmokeFailure(f"llama-server is not ready: {health}")
    models = _request_json("GET", f"{base_url}/models", api_key, args.timeout)
    model_ids = [item.get("id") for item in models.get("data", []) if isinstance(item, dict)]
    if args.model not in model_ids:
        raise SmokeFailure(f"configured model alias {args.model!r} not in /models: {model_ids}")

    if not args.skip_container:
        _check_container(args.compose_dir.resolve())

    report: dict[str, Any] = {
        "base_url": base_url,
        "model": args.model,
        "service": args.service,
        "cases": [],
    }
    for case in CASES:
        content, metrics = _stream_chat(base_url, args.model, api_key, args.timeout, case)
        parsed = _assert_case(case, content)
        case_report = {
            "name": case.name,
            "ok": True,
            "metrics": {**metrics, **_memory_metrics(args.service)},
            "response": parsed if parsed is not None else content,
        }
        report["cases"].append(case_report)
        print(
            f"{case.name}: OK; first={metrics['first_token_seconds']}s; "
            f"total={metrics['total_seconds']}s; tok/s={metrics['tokens_per_second']}"
        )

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"report: {args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeFailure as exc:
        print(f"SMOKE FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
