#!/usr/bin/env python3
"""Keep one REG.RU A record aligned with the server public IPv4 address."""

from __future__ import annotations

import ipaddress
import json
import os
import sys
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE = "https://api.reg.ru/api/regru2"
API_METHODS = frozenset({"zone/add_alias", "zone/get_resource_records", "zone/remove_record"})
IP_SOURCES = (
    "https://api.ipify.org",
    "https://ifconfig.co/ip",
    "https://icanhazip.com",
)


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not configured")
    return value


def public_ipv4() -> str:
    errors: list[str] = []
    for source in IP_SOURCES:
        try:
            # The source is selected only from the fixed HTTPS tuple above.
            with urlopen(source, timeout=12) as response:  # nosec B310
                address = response.read().decode("utf-8").strip()
            parsed = ipaddress.ip_address(address)
            if parsed.version == 4 and not parsed.is_private:
                return str(parsed)
            errors.append(f"{source}: not a public IPv4 address")
        except (URLError, ValueError, OSError) as exc:
            errors.append(f"{source}: {exc}")
    raise RuntimeError("Could not determine the public IPv4 address: " + "; ".join(errors))


def api_call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    if method not in API_METHODS:
        raise RuntimeError(f"Unsupported REG.RU API method: {method}")
    body = urlencode({"input_format": "json", "input_data": json.dumps(payload)}).encode("utf-8")
    request = Request(f"{API_BASE}/{method}", data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        # Both the API base and method are constrained above.
        with urlopen(request, timeout=20) as response:  # nosec B310
            result = json.loads(response.read().decode("utf-8"))
    except (URLError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"REG.RU API request {method} failed: {exc}") from exc
    if result.get("result") != "success":
        raise RuntimeError(f"REG.RU API request {method} failed: {result}")
    return result


def domain_result(result: dict[str, Any]) -> dict[str, Any]:
    domains = result.get("answer", {}).get("domains", [])
    if not domains or domains[0].get("result") != "success":
        raise RuntimeError(f"REG.RU did not accept the DNS operation: {result}")
    return domains[0]


def main() -> int:
    username = require_env("REG_RU_USERNAME")
    password = require_env("REG_RU_PASSWORD")
    domain = require_env("REG_RU_DOMAIN").lower()
    subdomain = require_env("REG_RU_SUBDOMAIN").lower()
    if not domain or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for char in domain):
        raise RuntimeError("REG_RU_DOMAIN is invalid")
    if subdomain != "@" and any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-*" for char in subdomain):
        raise RuntimeError("REG_RU_SUBDOMAIN is invalid")

    auth = {"username": username, "password": password, "domains": [{"dname": domain}]}
    records_result = api_call("zone/get_resource_records", auth)
    records = domain_result(records_result).get("rrs", [])
    current_addresses = sorted({
        str(record.get("content", "")).strip()
        for record in records
        if record.get("subname") == subdomain and record.get("rectype") == "A"
    })
    address = public_ipv4()

    if current_addresses == [address]:
        print(f"REG.RU DDNS: {domain}/{subdomain} is already {address}")
        return 0

    if address not in current_addresses:
        add_payload = {**auth, "subdomain": subdomain, "ipaddr": address}
        domain_result(api_call("zone/add_alias", add_payload))
    for old_address in current_addresses:
        if old_address == address:
            continue
        remove_payload = {
            **auth,
            "subdomain": subdomain,
            "record_type": "A",
            "content": old_address,
        }
        domain_result(api_call("zone/remove_record", remove_payload))
    old_display = ", ".join(current_addresses) if current_addresses else "none"
    print(f"REG.RU DDNS: {domain}/{subdomain} changed from {old_display} to {address}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"REG.RU DDNS error: {exc}", file=sys.stderr)
        raise SystemExit(1)
