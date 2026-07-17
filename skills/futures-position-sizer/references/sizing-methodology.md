# Futures Position Sizing Methodology

## Why a Separate Skill from `position-sizer`

`position-sizer` computes a SHARE count for long equity trades: `shares = floor(dollar_risk / (entry - stop))`, where one share's dollar risk is simply its own price move. Futures contracts are different in a way that makes that formula silently wrong if reused as-is: every contract has a **multiplier** that converts a one-point price move into a dollar amount, and that multiplier varies by roughly two orders of magnitude across symbols in the same table -- a 0.25-point move is $12.50 on ES (multiplier 50) but $5.00 on NQ (multiplier 20) and $31.25 on ZB (multiplier 1000, tick 1/32). Feeding a futures entry/stop into the equity sizer would compute a "share" count using the raw point difference as if it were a dollar difference, understating or overstating real risk by 20-1000x depending on the symbol. This skill exists specifically to apply the correct multiplier, tick size, and (for FX/international contracts) currency conversion before any risk arithmetic happens.

## Core Formula

```
stop_distance       = |entry - stop|                          (price points)
risk_per_contract   = stop_distance * multiplier * fx_rate     (USD)
risk_budget         = account_size * risk_pct / 100            (USD)
contracts            = floor(risk_budget / risk_per_contract)   (never rounds up)
total_risk           = contracts * risk_per_contract            (USD, when SIZED)
```

`fx_rate` converts a non-USD-quoted contract's risk into USD; it is `1.0` for every symbol in the verified core table (all 23 are USD-quoted -- see `futures-contract-specs.md`) and is required, with no default, for any operator-supplied override symbol quoted in another currency.

## The Floor Algorithm (Relative Epsilon, Pinned)

```python
q = risk_budget / risk_per_contract
contracts = math.floor(q * (1 + 1e-9))
```

Floating-point division can land a "true" exact multiple (`risk_budget == k * risk_per_contract` for some integer k) a few ULPs *under* `k` due to binary representation error -- a naive `math.floor(q)` would then silently return `k - 1` instead of `k`. The fix is a small **relative** epsilon nudge applied before flooring, not an absolute one: `1e-9` relative is many orders of magnitude smaller than any real fractional shortfall (a genuinely non-integer quotient, e.g. `q = 2.5`, still floors to `2`) yet many orders of magnitude larger than float64's own representation error (~1e-15 relative) **at every scale** -- from a single-digit contract count up through `q` in the hundreds of thousands. This holds by construction, not by manually verifying it at each scale a test happens to check.

The same relative-epsilon construction is reused for the tick-grid check (`is_on_tick_grid`) and the minimum-stop-distance guard (`meets_min_stop_distance`) -- both need to tolerate float noise around an exact boundary without ever masking a real violation.

**Minimum-stop-distance guard.** If the entry/stop distance is less than one tick, sizing is refused rather than silently computing an ultra-high, effectively meaningless contract count from a near-zero `risk_per_contract`. This closes the one regime where any epsilon design gets shaky: a stop distance of a few ULPs would make `risk_per_contract` itself vanishingly small, and even a tiny risk budget would imply an enormous (and nonsensical) contract count.

## Two Fail-Closed Classes: ConfigError vs. NO_TRADE

Every validation failure in this skill resolves to one of two outcomes, and which one depends on **who supplied the offending value** -- not on which rule was violated:

| Violation | Operator-supplied value (mode A `--stop`, or entry in either mode) | Gate-supplied value (mode B `--stop` = gate's `invalidation_level`) |
|---|---|---|
| Geometry (LONG stop >= entry, or SHORT stop <= entry) | `ConfigError` "direction_stop_mismatch" -- exit 2, no report | `NO_TRADE` "entry_on_wrong_side_of_stop" -- exit 0, report written |
| Stop closer than one tick | `ConfigError` "stop_too_close" -- exit 2 | `NO_TRADE` "gate_stop_too_close" -- exit 0 |
| Bond-family (ZT/ZF/ZN/ZB) off tick grid | `ConfigError` "entry_off_tick_grid" / "stop_off_tick_grid" -- exit 2 | `NO_TRADE` "gate_stop_off_tick_grid" -- exit 0 (entry is always operator-supplied, so an off-grid ENTRY is always a ConfigError even in mode B) |

This mirrors `contrarian-setup-gate`'s own convention for untrusted-file handling: a CLI usage mistake is the operator's problem (loud failure, no report, exit 2); a problem discovered inside an untrusted input FILE is never allowed to crash the tool -- it always produces a report naming exactly why sizing was refused (exit 0). Blaming the CLI invocation for a bad value that actually came from the gate's JSON file would be misleading, and would break the "every run either sizes or explains why not" contract every skill in this pipeline follows.

`risk_below_one_contract` (the risk budget can't afford even one contract at this stop distance) is **not** in this two-class table -- it is always `NO_TRADE`, in both modes, because it isn't anyone's mistake. It is the correct, expected output of risk-based sizing when the numbers simply don't support a position; widening the stop, raising `--risk-pct` (up to the 10% ceiling), or accepting no trade are all legitimate operator responses.

## Bond/Note Off-Grid Guard: Why It's Hard, Not Soft

ZT (2-Year), ZF (5-Year), ZN (10-Year), and ZB (30-Year) Treasury futures are the only fractional-notation family among the 23 core symbols -- they quote in 32nds (or, for ZN, 64ths; for ZF, quarter-32nds) of a point, conventionally written with an apostrophe: `110'16` means `110 + 16/32 = 110.50`. Every other symbol in the table quotes in plain decimal points.

The trap: an operator who mentally reads `110'16` and types `110.16` into `--entry` has entered a price that is **not on the tick grid at all** (`110.16 / 0.03125` is nowhere near an integer), and would have the sizer compute a stop distance using a price roughly 34 cents away from the intended one -- a small-looking but real, silent, wrong-money-math error. Every other symbol's off-grid price is legitimate (a mid-quote, an odd fill price) and only produces a warning; the bond family's off-grid price is treated as almost certainly a notation mistake and is a hard, fail-closed rejection instead, with a message that spells out the 32nds-to-decimal conversion.

## Worked Examples

### ES, LONG, explicit mode

```
entry = 5000.25, stop = 4980.00, multiplier = 50, tick_size = 0.25
stop_distance = 20.25 points = 81 ticks
risk_per_contract = 20.25 * 50 = $1,012.50
account_size = 100,000, risk_pct = 2.0% -> risk_budget = $2,000.00
contracts = floor(2000.00 / 1012.50) = floor(1.975...) = 1
total_risk = 1 * 1012.50 = $1,012.50 (1.01% of account)
```

At `risk_pct = 1.0%` instead, `risk_budget = $1,000.00 < risk_per_contract`, so `contracts = 0` -- `sizing_status: NO_TRADE`, `no_trade_reason: risk_below_one_contract`. The risk math (stop distance, risk per contract, risk budget) is still reported in full; only the trade itself is refused.

### B6, SHORT, gate handoff

A `contrarian-setup-gate` report for B6 reaches `READY_FOR_PLAN` with `direction: SHORT` and `invalidation_level: 1.3450` (the gate's stop reference). The operator supplies only the entry:

```bash
python3 skills/futures-position-sizer/scripts/futures_position_sizer.py \
  --gate-json reports/contrarian_setup_gate_B6_2026-07-15.json \
  --entry 1.3400 --account-size 100000 --risk-pct 1.0 \
  --output-dir reports/ --format both
```

B6 (British Pound, contract size GBP 62,500) is USD-quoted, so no `--fx-rate` is needed. `direction` and `stop` come entirely from the gate file; if `1.3400` (SHORT entry) were on the wrong side of `1.3450` (stop) -- i.e. the entry were above the stop for a SHORT -- the result would be `NO_TRADE` with reason `entry_on_wrong_side_of_stop`, exit 0, not a crash.

## Risk-Percentage Guardrails

`--risk-pct` accepts `(0, 10]`; values above `10` are an argparse-level usage error (no override). A value above `2.0` produces the `risk_pct_above_2` warning (never a rejection) -- consistent with `position-sizer`'s own 1-2% guideline for a single trade's risk. Futures leverage means the SAME percentage risk moves faster in dollar terms than an unlevered equity position of the same nominal size; this warning is a deliberately low bar.

## Margin Is Never Computed

The `margin_note` field is always the same static reminder text, never a computed number. Exchange initial/maintenance margin requirements are broker-specific and change with volatility regimes, sometimes intraday during stress -- a computed "margin estimate" would either be wrong the moment it's stale, or would require live broker data this skill deliberately does not fetch (fully offline, no API keys, no network). The issue that motivated this skill (#242) asked for a "margin estimate note"; this skill reads that as "a note ABOUT margin" (honest, never-stale) rather than "a computed estimate presented as a note" (which would rot). This interpretation was flagged explicitly for review rather than decided silently -- see the PR description.
