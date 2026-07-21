#!/usr/bin/env python3
"""Fetch FXMacroData release-calendar events."""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import sys
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode

FXMACRODATA_BASE_URL = "https://api.fxmacrodata.com/v1"
VALID_MARKET_TIERS = frozenset({1, 2, 3})


class _NonFiniteJSONValue(ValueError):
    """Raised when the JSON decoder encounters NaN or Infinity."""


def _normalize_currency(value: str) -> str:
    """Return a safe three-letter currency code without echoing bad input."""
    normalized = value.strip().lower()
    if len(normalized) != 3 or not normalized.isascii() or not normalized.isalpha():
        raise RuntimeError("currency must be a 3-letter ASCII code")
    return normalized


def _tier_rank(value: Any) -> int | None:
    """Return an API-contract market tier, or None for invalid input."""
    if isinstance(value, int) and not isinstance(value, bool) and value in VALID_MARKET_TIERS:
        return value
    return None


def _reject_non_finite_constant(value: str) -> None:
    """Reject the non-standard JSON constants NaN and Infinity."""
    raise _NonFiniteJSONValue(value)


def _validate_finite_json(value: Any) -> None:
    """Recursively reject numbers that decoded to NaN or Infinity."""
    if isinstance(value, float) and not math.isfinite(value):
        raise RuntimeError("FXMacroData API returned invalid response: non-finite number")
    if isinstance(value, dict):
        for item in value.values():
            _validate_finite_json(item)
    elif isinstance(value, list):
        for item in value:
            _validate_finite_json(item)


def _redact(text: str) -> str:
    """Redact an api_key query-param value from a string, as a backup.

    Primary defense against key leakage is never referencing the built
    request URL or HTTPError.url in error messages; this is a backup for
    any string that might still carry the key.
    """
    if "api_key=" not in text:
        return text
    prefix, _, rest = text.partition("api_key=")
    _, _, suffix = rest.partition("&")
    return f"{prefix}api_key=***{('&' + suffix) if suffix else ''}"


def fetch_calendar(currency: str, limit: int, min_tier: int | None) -> dict[str, Any]:
    normalized_currency = _normalize_currency(currency)
    if min_tier is not None and _tier_rank(min_tier) is None:
        raise RuntimeError("min_tier must be one of 1, 2, or 3")
    limit_count = max(1, min(int(limit), 100))
    params = {"limit": str(limit_count)}
    api_key = os.getenv("FXMACRODATA_API_KEY")
    if api_key:
        params["api_key"] = api_key

    try:
        url = f"{FXMACRODATA_BASE_URL}/calendar/{normalized_currency}?{urlencode(params)}"
        request = urllib.request.Request(
            url, headers={"User-Agent": "claude-trading-skills-fxmacrodata/1.0"}
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response, parse_constant=_reject_non_finite_constant)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"FXMacroData API request failed for currency={normalized_currency}: HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"FXMacroData API request failed for currency={normalized_currency}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise RuntimeError(
            f"FXMacroData API request timed out for currency={normalized_currency}"
        ) from exc
    except _NonFiniteJSONValue as exc:
        raise RuntimeError("FXMacroData API returned invalid response: non-finite number") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"FXMacroData API returned invalid JSON for currency={normalized_currency}"
        ) from exc
    except (http.client.InvalidURL, ValueError) as exc:
        raise RuntimeError(
            f"FXMacroData API request could not be built for currency={normalized_currency}"
        ) from exc

    _validate_finite_json(payload)
    if not isinstance(payload, dict):
        raise RuntimeError(
            "FXMacroData API returned invalid response: top-level JSON must be an object"
        )
    if "data" not in payload:
        raise RuntimeError("FXMacroData API returned invalid response: missing required data field")

    data = payload["data"]
    if not isinstance(data, list):
        raise RuntimeError("FXMacroData API returned invalid response: data field must be an array")

    events: list[dict[str, Any]] = []
    for index, event in enumerate(data):
        if not isinstance(event, dict):
            raise RuntimeError(
                f"FXMacroData API returned invalid response: data[{index}] must be an object"
            )
        tier = _tier_rank(event.get("market_tier"))
        if tier is None:
            raise RuntimeError(
                f"FXMacroData API returned invalid response: invalid market_tier at data[{index}]"
            )
        if min_tier is None or tier <= min_tier:
            events.append(event)

    events = events[:limit_count]
    return {
        "currency": payload.get("currency", normalized_currency.upper()),
        "timezone": payload.get("timezone"),
        "data_quality": payload.get("data_quality"),
        "events": events,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--currency", default="usd")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--min-tier", type=int, choices=(1, 2, 3), default=1)
    args = parser.parse_args()

    try:
        result = fetch_calendar(args.currency, args.limit, args.min_tier)
    except RuntimeError as exc:
        print(_redact(f"Error: {exc}"), file=sys.stderr)
        sys.exit(1)

    try:
        serialized = json.dumps(result, indent=2, allow_nan=False)
    except (TypeError, ValueError):
        print("Error: result could not be serialized as strict JSON", file=sys.stderr)
        sys.exit(1)

    print(serialized)
    sys.exit(0)


if __name__ == "__main__":
    main()
