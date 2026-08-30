#!/usr/bin/env python3
"""Normalize annual consensus rows into contract-3.5 discovery metrics.

A current forward P/E is computed only from a dated FY1 estimate.  The helper
never substitutes FY2/FY3 merely because FY1 is missing.  It records estimate
breadth, range dispersion, period continuity, growth horizons, and source IDs so
the deterministic broad screen can reject FRMI-like outer-year artefacts.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from skill_version import SKILL_VERSION, runtime_metadata
except ModuleNotFoundError:  # pragma: no cover
    import importlib.util as _importlib_util

    _path = Path(__file__).with_name("skill_version.py")
    _spec = _importlib_util.spec_from_file_location("skill_version", _path)
    if _spec is None or _spec.loader is None:
        raise
    _module = _importlib_util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)
    runtime_metadata = _module.runtime_metadata
    SKILL_VERSION = _module.SKILL_VERSION


class NormalizeError(ValueError):
    """Raised for malformed estimate inputs."""


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if math.isfinite(result) else None
    if isinstance(value, str):
        try:
            result = float(value.replace(",", "").strip())
        except ValueError:
            return None
        return result if math.isfinite(result) else None
    return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number.is_integer() else None


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    return None


def _parse_date(value: Any, label: str) -> datetime:
    text = _text(value)
    if not text:
        raise NormalizeError(f"{label} is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NormalizeError(f"{label} is not ISO-8601: {text!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _load_rows(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    text = path.read_text(encoding="utf-8")
    if suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    value = json.loads(text)
    if isinstance(value, list):
        return [dict(row) for row in value if isinstance(row, Mapping)]
    if isinstance(value, Mapping) and isinstance(value.get("rows"), list):
        return [dict(row) for row in value["rows"] if isinstance(row, Mapping)]
    raise NormalizeError(f"unsupported row container in {path}")


def _symbol(row: Mapping[str, Any]) -> str:
    return (_text(row.get("symbol")) or _text(row.get("ticker")) or "UNKNOWN").upper()


def _period_end(row: Mapping[str, Any]) -> datetime | None:
    for key in ("date", "period_end", "fiscal_period_end", "fiscalDateEnding"):
        text = _text(row.get(key))
        if not text:
            continue
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def _pick(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _number(row.get(key))
        if value is not None:
            return value
    return None


def _period_record(row: Mapping[str, Any]) -> dict[str, Any] | None:
    end = _period_end(row)
    if end is None:
        return None
    fiscal_year = _text(row.get("fiscalYear")) or _text(row.get("fiscal_year")) or str(end.year)
    fiscal_year = fiscal_year.upper().removeprefix("FY")
    return {
        "fiscal_year": fiscal_year,
        "period": f"FY{fiscal_year}",
        "period_end": end.isoformat(),
        "eps_avg": _pick(row, "epsAvg", "eps_avg", "eps", "estimated_eps"),
        "eps_low": _pick(row, "epsLow", "eps_low"),
        "eps_high": _pick(row, "epsHigh", "eps_high"),
        "revenue_avg": _pick(row, "revenueAvg", "revenue_avg", "revenue", "estimated_revenue"),
        "eps_analyst_count": _integer(row.get("numAnalystsEps"))
        or _integer(row.get("num_analysts_eps")),
        "revenue_analyst_count": _integer(row.get("numAnalystsRevenue"))
        or _integer(row.get("num_analysts_revenue")),
    }


def _dispersion_pct(avg: float | None, low: float | None, high: float | None) -> float | None:
    if avg is None or low is None or high is None or avg == 0:
        return None
    return abs(high - low) / abs(avg) * 100.0


def _cagr(start: float | None, end: float | None, years: float) -> float | None:
    if start is None or end is None or start <= 0 or end <= 0 or years <= 0:
        return None
    return ((end / start) ** (1.0 / years) - 1.0) * 100.0


def normalize_symbol(
    symbol: str,
    estimate_rows: Sequence[Mapping[str, Any]],
    listing: Mapping[str, Any],
    *,
    analysis_as_of: datetime,
    estimate_as_of: datetime,
    source_ids: Sequence[str],
    minimum_analysts: int,
    max_dispersion_pct: float,
    max_fy1_horizon_days: int,
    forward_pe_tolerance_pct: float,
) -> dict[str, Any]:
    price = _pick(listing, "price", "last")
    periods = [record for row in estimate_rows if (record := _period_record(row)) is not None]
    periods.sort(key=lambda record: str(record["period_end"]))
    future = [
        record
        for record in periods
        if _parse_date(record["period_end"], "period_end") > analysis_as_of
    ]

    reasons: list[str] = []
    operating_stage = (_text(listing.get("operating_stage")) or "").lower().replace(" ", "_")
    if _bool(listing.get("is_pre_operating")) is True or operating_stage in {
        "pre_operating",
        "pre-revenue",
        "pre_revenue",
        "development_stage",
    }:
        reasons.append("pre_operating_company")

    fy1 = future[0] if future else None
    fy1_horizon_days: int | None = None
    if fy1 is None:
        reasons.append("fy1_estimate_unavailable")
    else:
        fy1_horizon_days = (_parse_date(fy1["period_end"], "fy1.period_end") - analysis_as_of).days
        if fy1_horizon_days <= 0 or fy1_horizon_days > max_fy1_horizon_days:
            reasons.append("invalid_fy1_horizon")
        eps = _number(fy1.get("eps_avg"))
        low = _number(fy1.get("eps_low"))
        high = _number(fy1.get("eps_high"))
        if eps is None or eps <= 0:
            reasons.append("non_positive_fy1_eps")
        if low is not None and high is not None and low <= 0 <= high:
            reasons.append("fy1_eps_range_crosses_zero")
        dispersion = _dispersion_pct(eps, low, high)
        if dispersion is not None and dispersion > max_dispersion_pct:
            reasons.append("fy1_estimate_dispersion_excessive")
        analysts = _integer(fy1.get("eps_analyst_count"))
        if analysts is None or analysts < minimum_analysts:
            reasons.append("estimate_breadth_below_discovery_minimum")

    series_contiguous = True
    for left, right in zip(future, future[1:]):
        delta_days = (
            _parse_date(right["period_end"], "period_end")
            - _parse_date(left["period_end"], "period_end")
        ).days
        if not 300 <= delta_days <= 430:
            series_contiguous = False
            break
    if len(future) >= 2 and not series_contiguous:
        reasons.append("annual_estimate_series_not_contiguous")

    forward_eps = _number(fy1.get("eps_avg")) if fy1 else None
    forward_pe = (
        price / forward_eps
        if price is not None and price > 0 and forward_eps is not None and forward_eps > 0
        else None
    )
    if price is None or price <= 0:
        reasons.append("price_unavailable_or_non_positive")
    supplied_pe = _pick(listing, "forward_pe", "fy1_pe", "ntm_pe")
    if supplied_pe is not None and forward_pe is not None:
        mismatch = abs(supplied_pe - forward_pe) / max(abs(forward_pe), 1e-9) * 100.0
        if mismatch > forward_pe_tolerance_pct:
            reasons.append("forward_pe_reconciliation_failed")

    growth_end = future[2] if len(future) >= 3 else (future[1] if len(future) >= 2 else None)
    growth_years = 0.0
    if fy1 and growth_end:
        growth_years = (
            _parse_date(growth_end["period_end"], "growth_end.period_end")
            - _parse_date(fy1["period_end"], "fy1.period_end")
        ).days / 365.25
    eps_growth = _cagr(
        forward_eps, _number(growth_end.get("eps_avg")) if growth_end else None, growth_years
    )
    revenue_growth = _cagr(
        _number(fy1.get("revenue_avg")) if fy1 else None,
        _number(growth_end.get("revenue_avg")) if growth_end else None,
        growth_years,
    )

    status = "valid" if not reasons else "unavailable"
    canonical_forward_eps = forward_eps if status == "valid" else None
    canonical_forward_pe = forward_pe if status == "valid" else None
    canonical_fy1 = fy1 if status == "valid" else None

    result = dict(listing)
    result.update(
        {
            "symbol": symbol,
            "normalized_estimates_version": SKILL_VERSION,
            "enrichment_attempted": True,
            # The enrichment step is resolved whether it produced a usable FY1
            # metric or exhausted the available evidence with explicit reasons.
            "enrichment_resolved": True,
            "forward_metric_basis": "fy1" if canonical_fy1 else None,
            "forward_pe_period": "FY1" if canonical_fy1 else None,
            "forward_metric_period": "FY1" if canonical_fy1 else None,
            "forward_fiscal_year": canonical_fy1.get("fiscal_year") if canonical_fy1 else None,
            "forward_period_end": canonical_fy1.get("period_end") if canonical_fy1 else None,
            "forward_metric_period_end": canonical_fy1.get("period_end") if canonical_fy1 else None,
            "forward_estimate_as_of": estimate_as_of.isoformat(),
            "forward_estimate_source_ids": list(source_ids),
            "forward_metric_source_ids": list(source_ids),
            "forward_metric_origin": "computed_from_price_and_fy1_eps" if canonical_fy1 else None,
            "fy1_horizon_days": fy1_horizon_days,
            "forward_eps": canonical_forward_eps,
            "fy1_eps": canonical_forward_eps,
            "forward_eps_low": _number(canonical_fy1.get("eps_low")) if canonical_fy1 else None,
            "forward_eps_high": _number(canonical_fy1.get("eps_high")) if canonical_fy1 else None,
            "forward_estimate_dispersion_pct": _dispersion_pct(
                canonical_forward_eps,
                _number(canonical_fy1.get("eps_low")) if canonical_fy1 else None,
                _number(canonical_fy1.get("eps_high")) if canonical_fy1 else None,
            ),
            "forward_pe": round(canonical_forward_pe, 6)
            if canonical_forward_pe is not None
            else None,
            "fy1_pe": round(canonical_forward_pe, 6) if canonical_forward_pe is not None else None,
            "analyst_count": _integer(canonical_fy1.get("eps_analyst_count"))
            if canonical_fy1
            else None,
            "forward_eps_analyst_count": _integer(canonical_fy1.get("eps_analyst_count"))
            if canonical_fy1
            else None,
            "fy1_analyst_count": _integer(canonical_fy1.get("eps_analyst_count"))
            if canonical_fy1
            else None,
            "eps_growth_pct": round(eps_growth, 6)
            if eps_growth is not None and status == "valid"
            else None,
            "revenue_growth_pct": round(revenue_growth, 6)
            if revenue_growth is not None and status == "valid"
            else None,
            "growth_horizon_start_period": "FY1" if canonical_fy1 else None,
            "growth_horizon_start_period_end": canonical_fy1.get("period_end")
            if canonical_fy1
            else None,
            "growth_horizon_end_period": growth_end.get("period")
            if growth_end and status == "valid"
            else None,
            "growth_horizon_end_period_end": growth_end.get("period_end")
            if growth_end and status == "valid"
            else None,
            "growth_horizon_years": round(growth_years, 6)
            if growth_years > 0 and status == "valid"
            else None,
            "growth_estimate_source_ids": list(source_ids),
            "estimate_series_contiguous": series_contiguous,
            "estimate_periods": future[:4],
            "estimate_normalization_status": status,
            "estimate_normalization_reasons": sorted(set(reasons)),
            # Preserve the raw candidate for diagnostics without allowing it to
            # masquerade as the current FY1/NTM valuation in downstream screens.
            "raw_forward_candidate": {
                "fiscal_year": fy1.get("fiscal_year") if fy1 else None,
                "period_end": fy1.get("period_end") if fy1 else None,
                "eps": forward_eps,
                "eps_low": _number(fy1.get("eps_low")) if fy1 else None,
                "eps_high": _number(fy1.get("eps_high")) if fy1 else None,
                "computed_pe": round(forward_pe, 6) if forward_pe is not None else None,
                "analyst_count": _integer(fy1.get("eps_analyst_count")) if fy1 else None,
            },
        }
    )
    if reasons:
        result.update(
            {
                "enrichment_exhausted": True,
                "enrichment_exhaustion_reason": "; ".join(sorted(set(reasons))),
                "enrichment_source_ids": list(source_ids),
            }
        )
    else:
        result.update(
            {
                "enrichment_exhausted": False,
                "enrichment_exhaustion_reason": None,
                "enrichment_source_ids": list(source_ids),
            }
        )
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize raw annual estimates into current FY1/NTM discovery rows."
    )
    parser.add_argument("--estimates", type=Path, required=True)
    parser.add_argument("--listing-input", type=Path, required=True)
    parser.add_argument("--analysis-as-of", required=True)
    parser.add_argument("--estimate-as-of", required=True, help="Estimate retrieval/data timestamp")
    parser.add_argument("--source-id", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-analysts", type=int, default=2)
    parser.add_argument("--max-dispersion-pct", type=float, default=100.0)
    parser.add_argument("--max-fy1-horizon-days", type=int, default=430)
    parser.add_argument("--forward-pe-tolerance-pct", type=float, default=3.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    if "--version" in raw_argv:
        print(json.dumps(runtime_metadata(), sort_keys=True))
        return 0
    args = parse_args(raw_argv)
    try:
        analysis_as_of = _parse_date(args.analysis_as_of, "analysis_as_of")
        estimate_as_of = _parse_date(args.estimate_as_of, "estimate_as_of")
        estimate_rows = _load_rows(args.estimates)
        listing_rows = _load_rows(args.listing_input)
        listing_index = {_symbol(row): row for row in listing_rows}
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in estimate_rows:
            grouped[_symbol(row)].append(dict(row))
        symbols = sorted(set(listing_index) | set(grouped))
        output_rows = [
            normalize_symbol(
                symbol,
                grouped.get(symbol, []),
                listing_index.get(symbol, {"symbol": symbol}),
                analysis_as_of=analysis_as_of,
                estimate_as_of=estimate_as_of,
                source_ids=args.source_id,
                minimum_analysts=args.minimum_analysts,
                max_dispersion_pct=args.max_dispersion_pct,
                max_fy1_horizon_days=args.max_fy1_horizon_days,
                forward_pe_tolerance_pct=args.forward_pe_tolerance_pct,
            )
            for symbol in symbols
        ]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            "".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                for row in output_rows
            ),
            encoding="utf-8",
        )
    except (OSError, ValueError, json.JSONDecodeError, NormalizeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"wrote: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
