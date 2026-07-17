#!/usr/bin/env python3
"""
Futures Position Sizer -- pure sizing logic.

Shapiro pipeline step 4: converts a direction + entry + stop into a contract
count, given an account risk budget. PURE: no file I/O, no network, no
environment reads -- the CLI (`futures_position_sizer.py`) owns argument
parsing and gate-report file loading, and calls into this module with
already-parsed primitives.

This module NEVER sizes a position without an explicit stop, and NEVER
rounds a contract count up beyond the exact risk-budget/risk-per-contract
quotient (floor only). Two distinct failure classes exist, matched to who
supplied the offending value (the same convention contrarian-setup-gate
uses for its own untrusted-file handling):

  - A `ConfigError` (caught by the CLI, exit 2, no report written) is
    raised when the OPERATOR directly supplied a bad value: an invalid
    symbol override, a direction/stop geometry violation on the operator's
    own explicit --stop, a stop closer than one tick, or an off-tick-grid
    price on a bond-family contract (entry is ALWAYS operator-supplied,
    in both modes).
  - A `NO_TRADE` result dict (sizing_status="NO_TRADE", exit 0, a report
    IS still written) is returned when the same class of problem is
    detected on a value that came from the untrusted gate-report file
    (mode B's stop = the gate's `invalidation_level`) -- blaming the CLI
    invocation would be misleading, and it would break the fail-closed
    "always writes a report" contract every other skill in this pipeline
    follows.

Contract-spec table: see CONTRACT_SPECS below and
`references/futures-contract-specs.md` for the WebSearch-verified sourcing
of every row (official exchange contractSpecs pages only).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = "1.0"
SKILL_NAME = "futures-position-sizer"

# Bond/note family: the ONLY fractional-notation (32nds/64ths) contracts
# among the 23-symbol core table. A non-grid-aligned price on one of these
# is almost certainly a notation mistype (typing "110.16" when the
# intended price was "110'16" = 110 + 16/32 = 110.50) -- sizing it would
# silently produce wrong money math, so it is a hard, fail-closed rejection
# rather than a soft warning (unlike every other symbol, where an
# off-grid/mid-quote price is legitimate and only warns).
BOND_FAMILY = frozenset({"ZT", "ZF", "ZN", "ZB"})

# Relative (not absolute) epsilon for the contract-count floor: this nudge
# can only ever ADD BACK a contract lost to float64 representation error
# (e.g. a "true" k * risk_per_contract product that lands a few ULPs under
# k due to binary rounding) -- it can never round UP a genuinely
# non-integer quotient, because 1e-9 relative is many orders of magnitude
# smaller than any real fractional shortfall yet many orders of magnitude
# larger than float64's own representation error (~1e-15 relative) AT ANY
# SCALE. This holds by construction, not by manually walking scales (plan
# v3, review round-2 P2).
FLOOR_REL_EPSILON = 1e-9

# Same relative-epsilon construction applied to the tick-grid / minimum-
# stop-distance guards below, for the identical reason: tolerate float
# representation noise around an exact grid point or an exact one-tick
# distance, without ever masking a real off-grid price or a real
# sub-one-tick stop.
GRID_REL_EPSILON = 1e-9

RISK_PCT_WARNING_THRESHOLD = 2.0

# WEBSEARCH-VERIFIED against official exchange sources (CME Group rulebook
# chapters / contractSpecs pages, ICE, Cboe) -- see
# references/futures-contract-specs.md for the full per-row sourcing notes,
# including the two rows (ZT tick, QR identity) that needed the most
# scrutiny and the three rows (A6/D6/S6) whose minimum price increment was
# reduced by CME between 2016-2022 and is still misreported as the stale,
# pre-reduction value ($10.00/$10.00/$12.50) by many third-party aggregators.
VERIFIED_DATE = "2026-07-17"


def _spec(
    exchange_product: str,
    multiplier: float,
    tick_size: float,
    currency: str,
    exchange: str,
    source_url: str,
) -> dict[str, Any]:
    return {
        "exchange_product": exchange_product,
        "multiplier": multiplier,
        "tick_size": tick_size,
        "tick_value": multiplier * tick_size,
        "currency": currency,
        "exchange": exchange,
        "source_url": source_url,
        "verified_date": VERIFIED_DATE,
    }


CONTRACT_SPECS: dict[str, dict[str, Any]] = {
    "ES": _spec(
        "E-mini S&P 500 Index futures",
        50,
        0.25,
        "USD",
        "CME",
        "https://www.cmegroup.com/rulebook/CME/IV/350/358/358.pdf",
    ),
    "NQ": _spec(
        "E-mini Nasdaq-100 Index futures",
        20,
        0.25,
        "USD",
        "CME",
        "https://www.cmegroup.com/rulebook/CME/IV/350/359/359.pdf",
    ),
    "YM": _spec(
        "E-mini Dow Jones Industrial Average ($5) Index futures",
        5,
        1.0,
        "USD",
        "CBOT",
        "https://www.cmegroup.com/rulebook/CBOT/III/27.pdf",
    ),
    # QR = CME's E-mini Russell 2000 Index futures (current Globex ticker
    # RTY) -- NOT the discontinued ICE "Russell 2000 Mini" (legacy ticker
    # TF, $100 multiplier, delisted ~2017 when the Russell license moved
    # back to CME; using that multiplier would double the correct risk
    # figure). Confirmed live via FMP's COT data for symbol QR: every
    # recent weekly report's raw CFTC fields show
    # marketAndExchangeNames="RUSSELL E-MINI - CHICAGO MERCANTILE
    # EXCHANGE", cftcContractMarketCode="239742", contractUnits="(RUSSELL
    # 2000 INDEX X $50)" -- an actively-traded ~400K-OI contract, unlike
    # the zero-OI legacy ICE product.
    "QR": _spec(
        "E-mini Russell 2000 Index futures (Globex ticker RTY)",
        50,
        0.10,
        "USD",
        "CME",
        "https://www.cmegroup.com/rulebook/CME/III/300/393.pdf",
    ),
    # VX outright/single-leg tick is 0.05 index points ($50.00/contract).
    # A finer 0.01-point ($10.00) tick applies ONLY to the individual legs
    # and net price of SPREAD trades, and a still-finer 0.005-point tick
    # applies only to TAS Block/ECRP order types -- neither is the right
    # tick for sizing a single-leg stop-loss, which is what this table is
    # for.
    "VX": _spec(
        "Cboe Volatility Index (VIX) futures",
        1000,
        0.05,
        "USD",
        "CFE",
        "https://www.cboe.com/tradable-products/vix/vix-futures/specifications/",
    ),
    # ZT's tick is disputed among third-party aggregators; resolved here
    # ONLY against CME Group's own Rulebook Chapter 21, which states
    # verbatim: "The minimum price fluctuation shall be one-eighth of one
    # thirty-second of one point (equal to $7.8125 per contract)." That is
    # 1/8 x 1/32 = 1/256 = 0.00390625 points -- NOT 1/4-of-1/32 (that
    # fraction is ZF's). ZT's $200,000 face value (2x ZF's $100,000) is
    # what lets its coarser fraction land on the same $7.8125 dollar tick
    # as ZF's finer one -- likely the source of aggregator confusion.
    "ZT": _spec(
        "2-Year U.S. Treasury Note futures",
        2000,
        0.00390625,
        "USD",
        "CBOT",
        "https://www.cmegroup.com/rulebook/CBOT/II/21.pdf",
    ),
    # ZF: 1/4-of-1/32 = 0.0078125 points, a fraction-of-a-fraction trap.
    "ZF": _spec(
        "5-Year U.S. Treasury Note futures",
        1000,
        0.0078125,
        "USD",
        "CBOT",
        "https://www.cmegroup.com/rulebook/CBOT/II/20.pdf",
    ),
    # ZN: 1/2-of-1/32 (= 1/64) = 0.015625 points.
    "ZN": _spec(
        "10-Year U.S. Treasury Note futures",
        1000,
        0.015625,
        "USD",
        "CBOT",
        "https://www.cmegroup.com/rulebook/CBOT/II/19.pdf",
    ),
    # ZB: 1/32 = 0.03125 points.
    "ZB": _spec(
        "30-Year U.S. Treasury Bond futures",
        1000,
        0.03125,
        "USD",
        "CBOT",
        "https://www.cmegroup.com/rulebook/CBOT/V/18/18.pdf",
    ),
    "DX": _spec(
        "ICE U.S. Dollar Index futures",
        1000,
        0.005,
        "USD",
        "ICE",
        "https://www.ice.com/products/194/specs",
    ),
    "E6": _spec(
        "Euro FX futures (CME ticker 6E)",
        125_000,
        0.00005,
        "USD",
        "CME",
        "https://www.cmegroup.com/markets/fx/g10/euro-fx.contractSpecs.html",
    ),
    "J6": _spec(
        "Japanese Yen futures (CME ticker 6J)",
        12_500_000,
        0.0000005,
        "USD",
        "CME",
        "https://www.cmegroup.com/markets/fx/g10/japanese-yen.contractSpecs.html",
    ),
    "B6": _spec(
        "British Pound futures (CME ticker 6B)",
        62_500,
        0.0001,
        "USD",
        "CME",
        "https://www.cmegroup.com/markets/fx/g10/british-pound.contractSpecs.html",
    ),
    # A6/D6/S6 (v3 correction): CME reduced the outright minimum price
    # increment on these three from 0.0001 to 0.00005 between 2016 and
    # 2022 -- many generic aggregator sites and default web-search
    # snippets still quote the stale, pre-reduction tick values
    # ($10.00/$10.00/$12.50). The CME 2023 FX Product Guide's own
    # per-symbol footnotes and cmegroup.com/trading/fx/mpi.html (the MPI
    # change-history page) confirm the CURRENT values used here.
    "A6": _spec(
        "Australian Dollar futures (CME ticker 6A)",
        100_000,
        0.00005,
        "USD",
        "CME",
        "https://www.cmegroup.com/markets/fx/g10/australian-dollar.contractSpecs.html",
    ),
    # D6 = CME 6C (Canadian Dollar) -- confirmed via CME's 2023 FX Product
    # Guide (CAD/USD futures page: Product Code 6C, contract size 100,000
    # CAD, quoted USD per CAD) and the "D6" vendor-symbol convention (D
    # for "Dollar"/CAD) used by data vendors that avoid a leading digit.
    "D6": _spec(
        "Canadian Dollar futures (CME ticker 6C)",
        100_000,
        0.00005,
        "USD",
        "CME",
        "https://www.cmegroup.com/markets/fx/g10/canadian-dollar.contractSpecs.html",
    ),
    "S6": _spec(
        "Swiss Franc futures (CME ticker 6S)",
        125_000,
        0.00005,
        "USD",
        "CME",
        "https://www.cmegroup.com/markets/fx/g10/swiss-franc.contractSpecs.html",
    ),
    "GC": _spec(
        "Gold futures",
        100,
        0.10,
        "USD",
        "COMEX",
        "https://www.cmegroup.com/markets/metals/precious/gold.contractSpecs.html",
    ),
    # SI outright tick is $0.005/oz ($25.00/contract). A finer
    # $0.001/oz ($5.00) tick applies only to spread/straddle trades and
    # settlement -- not the right tick for sizing a single-leg stop.
    "SI": _spec(
        "Silver futures",
        5_000,
        0.005,
        "USD",
        "COMEX",
        "https://www.cmegroup.com/markets/metals/precious/silver.contractSpecs.html",
    ),
    "HG": _spec(
        "Copper futures",
        25_000,
        0.0005,
        "USD",
        "COMEX",
        "https://www.cmegroup.com/markets/metals/base/copper.contractSpecs.html",
    ),
    "PL": _spec(
        "Platinum futures",
        50,
        0.10,
        "USD",
        "NYMEX",
        "https://www.cmegroup.com/markets/metals/precious/platinum.contractSpecs.html",
    ),
    "CL": _spec(
        "WTI Crude Oil futures",
        1_000,
        0.01,
        "USD",
        "NYMEX",
        "https://www.cmegroup.com/markets/energy/crude-oil/light-sweet-crude.contractSpecs.html",
    ),
    # NG outright tick is $0.001/MMBtu ($10.00/contract). A finer
    # $0.00025/MMBtu ($2.50) tick applies only to Globex inter-commodity
    # spreads -- not the right tick for a single-leg stop.
    "NG": _spec(
        "Henry Hub Natural Gas futures",
        10_000,
        0.001,
        "USD",
        "NYMEX",
        "https://www.cmegroup.com/markets/energy/natural-gas/natural-gas.contractSpecs.html",
    ),
    # BT: CME's own ticker for this contract is "BTC" (5 BTC/contract),
    # distinct from CME's Micro Bitcoin contract "MBT" (0.1 BTC/contract,
    # 50x smaller notional). "BT" is this project's COT/vendor-feed alias
    # for CME's BTC -- keyed as "BT" here to match
    # cot-contrarian-detector's CORE_SYMBOLS convention.
    "BT": _spec(
        "Bitcoin futures (CME ticker BTC)",
        5,
        5.00,
        "USD",
        "CME",
        "https://www.cmegroup.com/rulebook/CME/IV/350/350.pdf",
    ),
}

MARGIN_NOTE = (
    "Exchange margin requirements are broker/time-dependent and NOT "
    "computed here; verify initial/maintenance margin with your broker."
)


class ConfigError(Exception):
    """An operator-caused configuration error: exit 2, no report written.

    Raised only for problems traceable to a value the OPERATOR supplied
    directly (CLI flags), never for a problem inside an untrusted gate
    report file -- those become a NO_TRADE result dict instead. See the
    module docstring's two-class convention.
    """

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


# --- Numeric string validators (argparse-facing) ----------------------------


def strict_positive_float(value: str, *, max_value: float | None = None) -> float:
    """Parse `value` as a finite, strictly-positive float.

    Rejects "inf"/"-inf"/"nan" (Python's `float()` accepts these as valid
    strings) and any literal that overflows to +/-inf on parse (e.g.
    "1e309" -- a syntactically ordinary-looking number). This is the
    strict validator every numeric CLI flag in this skill uses from day
    one -- position-sizer's plain `type=float` accepts "inf" and would
    silently produce an Infinity-valued JSON field (lesson from PR #249's
    ledger 10/10a/10b, explicitly called out as a trap to avoid here).
    Raises `ValueError` with a human-readable message on any rejection;
    the CLI wraps this in `argparse.ArgumentTypeError` so a bad flag is a
    parser-level usage error (exit 2), not a crash.
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{value!r} is not a valid number") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{value!r} must be finite (not inf/-inf/nan)")
    if parsed <= 0:
        raise ValueError(f"{value!r} must be greater than 0")
    if max_value is not None and parsed > max_value:
        raise ValueError(f"{value!r} must be <= {max_value}")
    return parsed


def strict_nonneg_int(value: str) -> int:
    """Parse `value` as a finite, non-negative integer (used for
    --max-contracts, where 0 means "no cap"). Rejects "inf"/"nan" and any
    non-integral float string ("1.5") -- fractional contract caps make no
    sense and a truncating `int()` on such a string would silently accept
    one."""
    try:
        parsed_float = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{value!r} is not a valid integer") from exc
    if not math.isfinite(parsed_float):
        raise ValueError(f"{value!r} must be finite (not inf/-inf/nan)")
    if parsed_float != int(parsed_float):
        raise ValueError(f"{value!r} must be a whole number")
    parsed_int = int(parsed_float)
    if parsed_int < 0:
        raise ValueError(f"{value!r} must be >= 0")
    return parsed_int


# --- Whole-structure non-finite scan (defense-in-depth for the JSON writer)


def contains_non_finite(value: Any) -> bool:
    """Iteratively check whether a structure contains any non-finite float
    (`inf`/`-inf`/`nan`) ANYWHERE, at any depth. Same iterative (not
    recursive) construction as contrarian-setup-gate's
    `_contains_non_finite` -- a legitimate structure can be nested deeply
    without limit and a recursive walker would raise `RecursionError`
    well before that on an entirely ordinary input. Used both by the CLI's
    hardened gate-json loader (copied verbatim from contrarian-setup-gate)
    and as a second, independent defense layer immediately before this
    module hands a result dict to the JSON writer's `allow_nan=False`.
    """
    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, float):
            if not math.isfinite(current):
                return True
        elif isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return False


# --- Risk math ----------------------------------------------------------------


def geometry_ok(direction: str, entry: float, stop: float) -> bool:
    """LONG requires stop < entry; SHORT requires stop > entry. Equality
    or inversion is a violation in both directions -- there is no such
    thing as a zero-distance or wrong-side stop."""
    if direction == "LONG":
        return stop < entry
    if direction == "SHORT":
        return stop > entry
    raise ValueError(f"unknown direction {direction!r}")  # pragma: no cover - CLI enforces the enum


def _tick_ratio_and_nearest(distance: float, tick_size: float) -> tuple[float, int | None]:
    """`nearest` is `None` when `ratio` itself overflowed to a non-finite
    value (an extreme, uncapped `--entry`/`--stop` divided by a small
    `tick_size` can do this even though both inputs are individually
    finite) -- `round()` on a non-finite float raises `OverflowError`, so
    callers must check for `None` instead of calling `round()` themselves."""
    ratio = distance / tick_size
    if not math.isfinite(ratio):
        return ratio, None
    return ratio, round(ratio)


def is_on_tick_grid(price: float, tick_size: float, rel_eps: float = GRID_REL_EPSILON) -> bool:
    """True if `price` lands on the tick grid (an integer number of ticks
    from 0), within a relative-epsilon tolerance that absorbs float64
    representation noise but not a genuine off-grid price. Used both for
    the bond-family hard off-grid rejection and the soft `off_tick_grid`
    warning on every other symbol.

    Never raises: a `price`/`tick_size` combination whose ratio overflows
    to a non-finite value is treated as maximally off-grid (`False`) rather
    than crashing -- the caller's own fail-closed handling (a bond-family
    ConfigError, or a warning for every other symbol) takes it from there;
    this function's only job is never to be the uncaught-exception source."""
    ratio, nearest = _tick_ratio_and_nearest(price, tick_size)
    if nearest is None:
        return False
    if nearest == 0:
        # A price within one tick of zero is not a realistic futures
        # price for any symbol in this table, but guard the divide
        # against a zero denominator in the relative check below anyway.
        return math.isclose(ratio, 0.0, abs_tol=1e-6)
    return abs(ratio - nearest) <= rel_eps * abs(nearest)


def stop_distance_ticks(entry: float, stop: float, tick_size: float) -> float:
    """Exact (unrounded) tick count between entry and stop. Callers round
    this for display; the unrounded value is what the minimum-distance
    guard evaluates against."""
    return abs(entry - stop) / tick_size


def meets_min_stop_distance(
    entry: float, stop: float, tick_size: float, rel_eps: float = GRID_REL_EPSILON
) -> bool:
    """True if the entry/stop distance is at least one tick. Relative-
    epsilon nudge (same construction as the floor algorithm below): a
    distance that is a hair under exactly one tick due to float noise
    still passes; a genuinely sub-one-tick distance still fails."""
    ticks = stop_distance_ticks(entry, stop, tick_size)
    return ticks * (1 + rel_eps) >= 1.0


def _format_ticks(ticks: float) -> int | float:
    """Render an exact whole-tick count as an int (matches the output
    contract's worked example: `stop_distance_ticks: 81`, not `81.0`);
    otherwise round to 2 decimals (off-grid symbols only warn, they are
    never forced onto the grid, so a fractional tick count is legitimate
    and reported at 2dp per plan).

    Never raises: `round(ticks)` (no ndigits -> converts to int) raises
    `OverflowError` on a non-finite `ticks` -- an extreme, uncapped
    `--entry`/`--stop` divided by a small `tick_size` can produce one even
    though both inputs are individually finite. `round(ticks, 2)` (WITH
    ndigits -> stays a float) never raises for a non-finite input, so the
    non-finite branch uses that form. The resulting non-finite value is
    always transient here: `size_futures_position`'s own
    `math.isfinite(risk_per_contract)` guard raises `ConfigError` before
    this result dict is ever written out, since the same extreme inputs
    that overflow the tick ratio also overflow the risk-per-contract
    product."""
    if not math.isfinite(ticks):
        return round(ticks, 2)
    nearest = round(ticks)
    if abs(ticks - nearest) < 1e-6:
        return int(nearest)
    return round(ticks, 2)


def compute_contracts(
    risk_budget_usd: float, risk_per_contract_usd: float, max_contracts: int | None = None
) -> int:
    """`floor(q * (1 + FLOOR_REL_EPSILON))` where `q = risk_budget /
    risk_per_contract`, then capped by `max_contracts` (None = uncapped).
    NEVER rounds up beyond the exact quotient -- a true 2.5 stays 2; the
    epsilon only recovers a contract lost to float representation error
    at an exact k * risk_per_contract boundary. See module docstring and
    plan v3 (review round-2 P2) for the relative-vs-absolute rationale."""
    q = risk_budget_usd / risk_per_contract_usd
    contracts = math.floor(q * (1 + FLOOR_REL_EPSILON))
    if max_contracts is not None and max_contracts > 0:
        contracts = min(contracts, max_contracts)
    return contracts


def requires_fx_rate(currency: str) -> bool:
    """True when `currency` (a contract's quote currency) is not USD, and
    an explicit --fx-rate is therefore mandatory rather than defaulted."""
    return currency.strip().upper() != "USD"


# --- Contract-spec resolution ------------------------------------------------


def resolve_spec(
    symbol: str,
    *,
    specs: dict[str, dict[str, Any]] = CONTRACT_SPECS,
    multiplier: float | None = None,
    tick_size: float | None = None,
    contract_currency: str | None = None,
) -> dict[str, Any]:
    """Resolve `symbol` to a contract-spec dict, either from the verified
    `specs` table or from a fully-specified operator override for a
    symbol NOT in the table.

    Two fail-closed rules, both deliberately strict (documented judgment
    call, flagged for PR review like the margin-note interpretation):

    1. A KNOWN symbol may never be silently overridden or silently left
       as-is when override flags are also given -- an override alongside
       a known symbol is treated as a config error rather than either
       guessing which one the operator meant or silently ignoring flags
       they explicitly typed.
    2. An UNKNOWN symbol requires ALL THREE override flags together
       (--multiplier, --tick-size, --contract-currency) -- no partial
       override with an implicit default, since a silently-defaulted
       currency or multiplier on an unlisted contract is exactly the kind
       of silent-wrong-money-math this skill exists to prevent.
    """
    symbol = symbol.strip().upper()
    override_given = any(v is not None for v in (multiplier, tick_size, contract_currency))

    if symbol in specs:
        if override_given:
            raise ConfigError(
                f"{symbol} already has a verified contract spec in the table; "
                "remove --multiplier/--tick-size/--contract-currency, or use "
                "--list-specs to inspect the existing row",
                reason="known_symbol_override_conflict",
            )
        spec = dict(specs[symbol])
        spec["cot_symbol"] = symbol
        return spec

    missing = [
        name
        for name, val in (
            ("--multiplier", multiplier),
            ("--tick-size", tick_size),
            ("--contract-currency", contract_currency),
        )
        if val is None
    ]
    if missing:
        raise ConfigError(
            f"{symbol} is not in the verified spec table; provide all of "
            f"--multiplier, --tick-size, and --contract-currency together "
            f"(missing: {', '.join(missing)})",
            reason="unknown_symbol_incomplete_override",
        )

    currency = contract_currency.strip().upper()
    tick_value = multiplier * tick_size
    return {
        "cot_symbol": symbol,
        "exchange_product": None,
        "multiplier": multiplier,
        "tick_size": tick_size,
        "tick_value": tick_value,
        "currency": currency,
        "exchange": None,
        "source_url": None,
        "verified_date": None,
    }


def _spec_audit_dict(spec: dict[str, Any]) -> dict[str, Any]:
    exchange = spec.get("exchange")
    return {
        "multiplier": spec["multiplier"],
        "tick_size": spec["tick_size"],
        "tick_value": spec["tick_value"],
        "currency": spec["currency"],
        "source": exchange.lower() if isinstance(exchange, str) else None,
        "verified": spec.get("verified_date"),
    }


def _bond_off_grid_message(symbol: str, field_name: str, price: float, tick_size: float) -> str:
    return (
        f"{symbol} {field_name} {price} is not on the {tick_size}-point tick grid. "
        f"Bond/note futures quote in 32nds/64ths-of-a-point notation shown with an "
        f"apostrophe -- e.g. 110'16 means 110 + 16/32 = 110.50 -- enter DECIMAL "
        f"points, not the raw digits after the apostrophe (110.16 is NOT 110'16)."
    )


# --- Top-level sizing orchestration ------------------------------------------


def _base_result(
    *,
    symbol: str,
    direction: str,
    entry: float,
    stop: float,
    spec: dict[str, Any],
    account_size: float,
    fx_rate: float,
    as_of: str,
    gate_block: dict[str, Any] | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "direction": direction,
        "sizing_status": None,
        "no_trade_reason": None,
        "entry": entry,
        "stop": stop,
        "stop_distance_points": None,
        "stop_distance_ticks": None,
        "contract_spec": _spec_audit_dict(spec),
        "risk_per_contract_usd": None,
        "risk_budget_usd": None,
        "contracts": 0,
        "total_risk_usd": None,
        "risk_pct_of_account": None,
        "max_contracts_cap_applied": False,
        "fx_rate_used": fx_rate,
        "margin_note": MARGIN_NOTE,
        "warnings": [],
        "run_context": {
            "symbol": symbol,
            "as_of": as_of,
            "schema_version": SCHEMA_VERSION,
            "skill": SKILL_NAME,
        },
    }
    if gate_block is not None:
        result["gate"] = gate_block
    return result


def size_futures_position(
    *,
    symbol: str,
    direction: str,
    entry: float,
    stop: float,
    stop_source: str,
    spec: dict[str, Any],
    account_size: float,
    risk_pct: float,
    max_contracts: int | None,
    fx_rate: float,
    as_of: str,
    gate_block: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the full sizing result (plan §2 output contract) from
    already-validated, already-normalized primitives.

    `stop_source` is `"operator"` (mode A: --stop is a direct CLI flag) or
    `"gate"` (mode B: --stop came from the gate report's
    `invalidation_level`) -- it selects which of the two fail-closed
    classes (ConfigError vs NO_TRADE) a geometry/off-grid/too-close
    violation resolves to. See module docstring.

    Raises `ConfigError` for every operator-caused violation. Returns a
    result dict (`sizing_status` "SIZED" or "NO_TRADE") otherwise --
    NEVER raises for a gate-caused violation or for the ordinary
    risk_below_one_contract outcome, both of which are legitimate,
    fail-closed answers, not errors.
    """
    result = _base_result(
        symbol=symbol,
        direction=direction,
        entry=entry,
        stop=stop,
        spec=spec,
        account_size=account_size,
        fx_rate=fx_rate,
        as_of=as_of,
        gate_block=gate_block,
    )
    warnings: list[str] = result["warnings"]

    def _no_trade(reason: str) -> dict[str, Any]:
        result["sizing_status"] = "NO_TRADE"
        result["no_trade_reason"] = reason
        return result

    # --- Geometry ---
    if not geometry_ok(direction, entry, stop):
        if stop_source == "operator":
            comparator = "<" if direction == "LONG" else ">"
            raise ConfigError(
                f"{direction} requires stop {comparator} entry (entry={entry}, stop={stop})",
                reason="direction_stop_mismatch",
            )
        return _no_trade("entry_on_wrong_side_of_stop")

    tick_size = spec["tick_size"]
    is_bond = symbol in BOND_FAMILY

    # --- Off-grid handling: bond family is a hard, mode-aware rejection;
    # every other symbol only warns. Entry is ALWAYS operator-supplied in
    # both modes, so an off-grid entry is always a ConfigError.
    if is_bond:
        if not is_on_tick_grid(entry, tick_size):
            raise ConfigError(
                _bond_off_grid_message(symbol, "entry", entry, tick_size),
                reason="entry_off_tick_grid",
            )
        if not is_on_tick_grid(stop, tick_size):
            if stop_source == "operator":
                raise ConfigError(
                    _bond_off_grid_message(symbol, "stop", stop, tick_size),
                    reason="stop_off_tick_grid",
                )
            return _no_trade("gate_stop_off_tick_grid")
    else:
        if not is_on_tick_grid(entry, tick_size):
            warnings.append("off_tick_grid_entry")
        if not is_on_tick_grid(stop, tick_size):
            warnings.append("off_tick_grid_stop")

    # --- Minimum stop distance (closes the ULP-degenerate regime any
    # epsilon design gets shaky in, on independent grounds from the floor
    # algorithm's own epsilon).
    if not meets_min_stop_distance(entry, stop, tick_size):
        if stop_source == "operator":
            raise ConfigError(
                f"stop distance ({round(abs(entry - stop), 8)}) is less than one "
                f"tick ({tick_size}) for {symbol}",
                reason="stop_too_close",
            )
        return _no_trade("gate_stop_too_close")

    # --- Risk math (price-space, exact; only rounded for display) ---
    distance = abs(entry - stop)
    ticks_exact = stop_distance_ticks(entry, stop, tick_size)
    result["stop_distance_points"] = round(distance, 8)
    result["stop_distance_ticks"] = _format_ticks(ticks_exact)

    # Defense-in-depth against float64 overflow: every operand here (entry,
    # stop, multiplier, fx_rate, account_size, risk_pct) already passed its
    # own argparse-level max_value cap, but the PRODUCT of several
    # individually-valid large values can still overflow to inf (e.g. an
    # extreme --account-size times a double-digit --risk-pct). An
    # uncaught inf would otherwise reach math.floor() inside
    # compute_contracts() (raises OverflowError) or the JSON writer's
    # allow_nan=False (raises ValueError) -- both uncaught crashes, exit 1,
    # violating the two-class exit contract (2 config / 0 fail-closed
    # report -- never 1). Both risk_per_contract and risk_budget are
    # entirely operator-supplied in both modes (never gate-sourced), so a
    # ConfigError (exit 2, no report) is the correct class here, same as
    # resolve_spec's and requires_fx_rate's own operator-side errors.
    risk_per_contract = distance * spec["multiplier"] * fx_rate
    if not math.isfinite(risk_per_contract):
        raise ConfigError(
            f"computed risk_per_contract is not finite (stop_distance={distance}, "
            f"multiplier={spec['multiplier']}, fx_rate={fx_rate}) -- one of "
            "--multiplier/--tick-size/--fx-rate (or an unknown-symbol override) "
            "is unreasonably large",
            reason="risk_per_contract_overflow",
        )
    result["risk_per_contract_usd"] = round(risk_per_contract, 2)

    risk_budget = account_size * risk_pct / 100.0
    if not math.isfinite(risk_budget):
        raise ConfigError(
            f"computed risk_budget is not finite (account_size={account_size}, "
            f"risk_pct={risk_pct}) -- --account-size is unreasonably large",
            reason="risk_budget_overflow",
        )
    result["risk_budget_usd"] = round(risk_budget, 2)

    if risk_pct > RISK_PCT_WARNING_THRESHOLD:
        warnings.append("risk_pct_above_2")

    cap = max_contracts if max_contracts and max_contracts > 0 else None
    pre_cap_contracts = compute_contracts(risk_budget, risk_per_contract, None)
    contracts = min(pre_cap_contracts, cap) if cap is not None else pre_cap_contracts
    result["contracts"] = contracts
    result["max_contracts_cap_applied"] = cap is not None and contracts < pre_cap_contracts

    if contracts <= 0:
        return _no_trade("risk_below_one_contract")

    total_risk = contracts * risk_per_contract
    result["total_risk_usd"] = round(total_risk, 2)
    result["risk_pct_of_account"] = round(total_risk / account_size * 100.0, 2)
    result["sizing_status"] = "SIZED"
    return result


def build_gate_failure_result(
    *,
    symbol: str,
    entry: float,
    reason: str,
    as_of: str,
    report_path: str,
    setup_status: str | None = None,
    gate_confidence: str | None = None,
    warnings: tuple[str, ...] = (),
) -> dict[str, Any]:
    """A minimal NO_TRADE result for a gate report that never yielded a
    usable stop -- unreadable/malformed/not-ready/symbol-mismatched. No
    risk math has run (there is no stop to run it against), so every
    downstream-computed field stays null, matching the output contract's
    "only entry is known" case."""
    return {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "direction": None,
        "sizing_status": "NO_TRADE",
        "no_trade_reason": reason,
        "entry": entry,
        "stop": None,
        "stop_distance_points": None,
        "stop_distance_ticks": None,
        "contract_spec": None,
        "risk_per_contract_usd": None,
        "risk_budget_usd": None,
        "contracts": 0,
        "total_risk_usd": None,
        "risk_pct_of_account": None,
        "max_contracts_cap_applied": False,
        "fx_rate_used": None,
        "margin_note": MARGIN_NOTE,
        "gate": {
            "report_path": report_path,
            "setup_status": setup_status,
            "gate_confidence": gate_confidence,
            "warnings": list(warnings),
        },
        "warnings": [],
        "run_context": {
            "symbol": symbol,
            "as_of": as_of,
            "schema_version": SCHEMA_VERSION,
            "skill": SKILL_NAME,
        },
    }


# --- Gate report normalization -----------------------------------------------


def _is_supported_schema(value: Any) -> bool:
    """Accept schema major version "1" (e.g. "1.0", "1.4"); reject
    anything else, including missing/non-string/other-major values (fail
    closed). Same idiom as contrarian-setup-gate's own `_is_supported_schema`
    (gate_logic.py) -- reimplemented here, not imported, since skills in
    this repo are self-contained (no cross-skill Python dependency)."""
    if not isinstance(value, str) or not value:
        return False
    return value.split(".", 1)[0] == "1"


def _valid_finite_positive(value: Any) -> float | None:
    """A usable `invalidation_level`: a finite, positive, non-bool number.
    `isinstance(True, int)` is `True` in Python, so bool must be excluded
    explicitly or a boolean would silently pass as 0.0/1.0. Same guard
    contrarian-setup-gate's `_valid_stop_reference` applies to the
    upstream price-action report's own stop_reference field."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return float(value)


@dataclass(frozen=True)
class GateNormalized:
    """One gate-report's normalized, fail-closed usability.

    `usable=True` only for a schema-supported, symbol-matched
    `READY_FOR_PLAN` report with a valid `direction` and
    `invalidation_level` -- exactly the shape `size_futures_position`
    needs for mode B. Every other case is `usable=False` with a named
    `reason`, always resolving to a NO_TRADE result (never a ConfigError
    -- the gate file is an untrusted input in every case here, by
    definition of mode B)."""

    usable: bool
    reason: str | None
    symbol: str | None = None
    direction: str | None = None
    invalidation_level: float | None = None
    setup_status: str | None = None
    gate_confidence: str | None = None
    warnings: tuple[str, ...] = ()


def _extract_confidence_and_warnings(
    raw_data: dict[str, Any],
) -> tuple[str | None, tuple[str, ...]]:
    gate_confidence = raw_data.get("gate_confidence")
    if not isinstance(gate_confidence, str):
        gate_confidence = None
    raw_warnings = raw_data.get("warnings")
    warnings = (
        tuple(w for w in raw_warnings if isinstance(w, str))
        if isinstance(raw_warnings, list)
        else ()
    )
    return gate_confidence, warnings


def normalize_gate_report(
    raw_data: Any, load_error: str | None, *, symbol: str | None
) -> GateNormalized:
    """Normalize a contrarian-setup-gate JSON report for use as this
    skill's mode-B stop/direction source.

    `load_error` is one of the CLI's three hardened loader tags
    ("unreadable" / "parse_error" / "non_finite", copied verbatim from
    contrarian-setup-gate's `load_json_file`) or `None` when the file
    parsed cleanly. Everything below `load_error is None` is the NEW
    gate-shape ("malformed") checker this skill adds, in the same idiom
    as contrarian-setup-gate's own `normalize_*` functions -- detecting a
    wrong-shaped GATE-REPORT schema (setup_status/direction/
    invalidation_level types), not a wrong-shaped upstream input.

    `symbol`, when given, must match the report's own `symbol` field
    (case-insensitively); when omitted, the report's symbol is used as-is
    (plan §2: "if --symbol omitted, taken from the gate report").
    """
    if load_error == "unreadable":
        return GateNormalized(False, "gate_json_unreadable")
    if load_error == "parse_error":
        return GateNormalized(False, "gate_json_parse_error")
    if load_error == "non_finite":
        return GateNormalized(False, "gate_json_non_finite")
    if not isinstance(raw_data, dict):
        return GateNormalized(False, "gate_json_malformed")

    if not _is_supported_schema(raw_data.get("schema_version")):
        return GateNormalized(False, "gate_json_schema_unsupported")

    report_symbol = raw_data.get("symbol")
    if not isinstance(report_symbol, str) or not report_symbol:
        return GateNormalized(False, "gate_json_malformed")
    report_symbol = report_symbol.strip().upper()
    if symbol is not None and report_symbol != symbol.strip().upper():
        return GateNormalized(False, "gate_symbol_mismatch", symbol=report_symbol)

    setup_status = raw_data.get("setup_status")
    if not isinstance(setup_status, str) or not setup_status:
        return GateNormalized(False, "gate_json_malformed", symbol=report_symbol)

    if setup_status != "READY_FOR_PLAN":
        gate_confidence, warnings = _extract_confidence_and_warnings(raw_data)
        return GateNormalized(
            False,
            "gate_not_ready",
            symbol=report_symbol,
            setup_status=setup_status,
            gate_confidence=gate_confidence,
            warnings=warnings,
        )

    direction = raw_data.get("direction")
    if direction not in ("LONG", "SHORT"):
        return GateNormalized(
            False, "gate_json_invalid_direction", symbol=report_symbol, setup_status=setup_status
        )

    invalidation_level = _valid_finite_positive(raw_data.get("invalidation_level"))
    if invalidation_level is None:
        return GateNormalized(
            False,
            "gate_json_invalid_invalidation_level",
            symbol=report_symbol,
            direction=direction,
            setup_status=setup_status,
        )

    gate_confidence, warnings = _extract_confidence_and_warnings(raw_data)
    return GateNormalized(
        True,
        None,
        symbol=report_symbol,
        direction=direction,
        invalidation_level=invalidation_level,
        setup_status=setup_status,
        gate_confidence=gate_confidence,
        warnings=warnings,
    )
