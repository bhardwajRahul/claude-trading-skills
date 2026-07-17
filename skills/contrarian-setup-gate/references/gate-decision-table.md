# Contrarian Setup Gate -- Decision Table

## Purpose

This gate combines three normalized states -- crowding (C), news failure (N), and price action (P) -- into one `setup_status` via an explicit precedence rule set. Each of N and P is one of five states: `CONFIRMED`, `NOT_CONFIRMED`, `INSUFFICIENT`, `PENDING` (the report file was not provided), or `INVALID` (a report was provided but is unusable). C shares the same five states, except it is never `PENDING` -- the detector report is always required.

The rules are evaluated **in order**; the first rule that matches decides the status. This document is the authoritative spec the test suite (`scripts/tests/test_gate_logic.py`) checks against.

## Precedence Rules

1. **Crowding first, exclusively.**
   - C is `INVALID` or `INSUFFICIENT` -> `INSUFFICIENT_EVIDENCE`.
   - C is `NOT_CONFIRMED` (classification `NEUTRAL`) -> `REJECTED`, **regardless of N or P's state**. A corrupted, stale, or mismatched downstream file must never soften crowding's own definitive "not crowded" conclusion. N/P are still echoed in the `inputs` audit block, but never change this status.
2. (C is `CONFIRMED` from here.) Any provided N or P that is `INVALID` -> `INSUFFICIENT_EVIDENCE`, naming every invalid step.
3. Any N or P that is `NOT_CONFIRMED` -> `REJECTED`, naming every rejecting step.
4. Any provided N or P that is `INSUFFICIENT` -> `INSUFFICIENT_EVIDENCE`.
5. **Out-of-order use.** If P is `CONFIRMED` while N is still `PENDING`, the pipeline order (crowding -> news -> price) was skipped. The gate accepts this but caps the status at `CROWDED`, with warning `out_of_order_price_action` and `news_failure` listed as pending. (A `NOT_CONFIRMED` P still REJECTs via rule 3 above -- that rule runs before this one is ever reached.)
6. C `CONFIRMED`, N and P both `PENDING` -> `CROWDED`.
7. C + N `CONFIRMED`, P `PENDING` -> `WATCHING_PRICE`.
8. C + N + P all `CONFIRMED` -> `READY_FOR_PLAN` (direction, `gate_confidence`, `entry_trigger`, and `invalidation_level` are populated).

## Full State Table

C has 5 reachable labels: `CROWDED_LONG` and `CROWDED_SHORT` (both `CONFIRMED`, differing only in fade direction), `NOT_CONFIRMED`, `INSUFFICIENT`, `INVALID`. N and P each have 5 reachable labels: `CONFIRMED`, `NOT_CONFIRMED`, `INSUFFICIENT`, `PENDING`, `INVALID`. All 125 combinations are exhaustively unit-tested; the table below collapses them by rule.

| C state | N / P states | Result |
|---|---|---|
| `INVALID` or `INSUFFICIENT` | any | `INSUFFICIENT_EVIDENCE` |
| `NOT_CONFIRMED` | any | `REJECTED` |
| `CONFIRMED` | any N or P `INVALID` | `INSUFFICIENT_EVIDENCE` |
| `CONFIRMED` | any N or P `NOT_CONFIRMED` (and none `INVALID`) | `REJECTED` |
| `CONFIRMED` | any N or P `INSUFFICIENT` (and none `INVALID`/`NOT_CONFIRMED`) | `INSUFFICIENT_EVIDENCE` |
| `CONFIRMED` | N `PENDING`, P `CONFIRMED` | `CROWDED` (+ `out_of_order_price_action` warning) |
| `CONFIRMED` | N `PENDING`, P `PENDING` | `CROWDED` |
| `CONFIRMED` | N `CONFIRMED`, P `PENDING` | `WATCHING_PRICE` |
| `CONFIRMED` | N `CONFIRMED`, P `CONFIRMED` | `READY_FOR_PLAN` |

## Named Precedence Pins

These specific combinations are individually pinned in the test suite because they resolve an ambiguity a naive implementation could get wrong:

- **C=`NOT_CONFIRMED` + N=`INVALID` (unreadable)** -> `REJECTED`. Crowding's own conclusion is never softened by a corrupted downstream file.
- **C=`NOT_CONFIRMED` + N=`INVALID` (symbol_mismatch)** -> `REJECTED`. A consistency-check failure is a PER-INPUT `INVALID`, never a global override -- it cannot soften crowding's own conclusion either.
- **C=`NOT_CONFIRMED` + P=`NOT_CONFIRMED`** -> `REJECTED`, with crowding named as the rejector (not price action).
- **C=`INVALID` + N=`NOT_CONFIRMED`** -> `INSUFFICIENT_EVIDENCE` (rule 1 runs before N is even inspected).
- **C=`INSUFFICIENT` + N=`NOT_CONFIRMED`** -> `INSUFFICIENT_EVIDENCE` (same).
- **C=`CONFIRMED` + N=`INVALID` + P=`NOT_CONFIRMED`** -> `INSUFFICIENT_EVIDENCE` (rule 2 runs before rule 3).
- **C=`CONFIRMED` + N=`NOT_CONFIRMED` + P=`INSUFFICIENT`** -> `REJECTED` (rule 3 runs before rule 4).
- **Out-of-order P without N, P `CONFIRMED`** -> `CROWDED` + warning.
- **Out-of-order P without N, P `NOT_CONFIRMED`** -> `REJECTED` (rule 3 still applies -- out-of-order capping only ever applies to the CONFIRMED/PENDING combinations left after rules 1-4 have run).

## Cross-Input Consistency (PER-INPUT INVALID)

A consistency failure marks the **exhibiting input** `INVALID` with a named reason and flows into the precedence rules above exactly like a loader failure -- it is never a global override.

- `symbol` in a provided news or price-action report that does not match `--symbol` -> that input `INVALID` (`news_symbol_mismatch` / `price_action_symbol_mismatch`). For the detector, `--symbol` simply not being found in `markets[]` is the existing `INSUFFICIENT` path (`detector_missing_symbol`), not a mismatch.
- `direction` in a provided news or price-action report that does not equal the detector's `classification` -> that input `INVALID` (`news_direction_mismatch` / `price_action_direction_mismatch`). Evaluated **only** when the detector row itself is usable (crowding state `CONFIRMED`) -- an unusable detector has no classification to compare against. A `direction` value of `null` is never treated as a mismatch: it is the report's own upstream fail-closed exit (e.g. NRF's `no_direction_provided`), so it normalizes to `INSUFFICIENT` with that upstream `verdict_reason` when present, or `<input>_malformed` when it is not -- comparing `null` against the detector's classification would otherwise always be unequal and misreport a legitimate upstream insufficiency as a mismatch.
- A missing `direction` key in a provided news or price-action report -> `<input>_malformed` (a required key, checked alongside `symbol`/`verdict`/`confidence`).
- `schema_version` outside major version `1` -> that input `INVALID` (`<input>_schema_unsupported`). Read locations differ per input: the detector and news reports carry `schema_version` at the **top level**; the price-action report carries it **only** at `run_context.schema_version` -- there is no top-level key on that report, so reading the top level there would silently disable the check.
- Duplicate `symbol` rows in the detector's `markets[]` -> the **first** match wins (the same `next()` pattern the detector itself uses internally).

## Reason-Token Glossary

### Crowding (cot-contrarian-detector)

| Reason | State | Meaning |
|---|---|---|
| `detector_unreadable` | INVALID | File missing, unreadable, or not valid UTF-8 |
| `detector_parse_error` | INVALID | File read but is not valid JSON |
| `detector_malformed` | INVALID | Valid JSON but the top level is not an object |
| `detector_schema_unsupported` | INVALID | `schema_version` major is not `1` |
| `detector_missing_symbol` | INSUFFICIENT | Symbol absent from `markets[]`, or present in `skipped[]` |
| `detector_missing_data_date` | INVALID | `run_context.data_date` missing or empty |
| `detector_invalid_data_date` | INVALID | `data_date` not a string, or unparsable |
| `detector_future_data_date` | INVALID | `data_date` is after `--as-of` |
| `detector_json_stale` | INVALID | `data_date` age exceeds `--max-detector-age-days` |
| `detector_unknown_classification` | INVALID | `classification` is not one of `CROWDED_LONG` / `CROWDED_SHORT` / `NEUTRAL` |
| `detector_not_crowded` | NOT_CONFIRMED | `classification` is `NEUTRAL` -- measurably not crowded, an explicit negative |

### News Failure (news-reaction-failure-analyzer) and Price Action (technical-analyst)

Both reports share the same reason-token shape, prefixed `news_` / `price_action_` respectively:

| Reason (news / price_action) | State | Meaning |
|---|---|---|
| `_unreadable` | INVALID | File missing, unreadable, or not valid UTF-8 |
| `_parse_error` | INVALID | File read but is not valid JSON |
| `_malformed` | INVALID | Top level is not an object, or `symbol`/`direction`/`verdict`/`confidence` is missing |
| `_symbol_mismatch` | INVALID | Report's `symbol` does not equal `--symbol` |
| `_schema_unsupported` | INVALID | `schema_version` major is not `1` |
| `_direction_mismatch` | INVALID | Report's `direction` is non-null and does not equal the detector's `classification` (checked only when crowding is usable). A `null` `direction` is never a mismatch -- see "Cross-Input Consistency" above |
| `_missing_as_of` | INVALID | `run_context.as_of` missing or empty |
| `_invalid_as_of` | INVALID | `as_of` not a string, or unparsable |
| `_future_as_of` | INVALID | `as_of` is after `--as-of` |
| `_json_stale` | INVALID | `as_of` age exceeds `--max-report-age-days` |
| `_unknown_verdict` | INVALID | `verdict` is not one of the three known values for that report type |
| (upstream `verdict_reason`) | NOT_CONFIRMED / INSUFFICIENT | The upstream report's own `verdict_reason` is carried through unchanged (e.g. `no_reversal_evidence`, `no_usable_events`) |

`price_action_missing_stop_reference` (INVALID, price-action only) fires when `verdict` is `CONFIRMED` but neither `handoff.price_action.stop_reference` nor the top-level `swing_levels.stop_reference` is a usable number -- a `READY_FOR_PLAN` without an invalidation level is not actionable, so this fails closed rather than emitting a null stop.

## Warnings (Never Change the Status)

- `price_action_confidence_medium` / `news_confidence_medium` -- fires only at `READY_FOR_PLAN` when that input's confidence is `MEDIUM` (single-signal weakness).
- `<input>_near_stale` -- fires when an input's age is within 2 days of its configured max age (and not already over it, which would instead be `INVALID`/`_json_stale`).
- `detector_data_date_divergence` -- the crowding market row's own `data_date` differs from `run_context.data_date` (the run's vintage, which is what staleness is evaluated against).
- `out_of_order_price_action` -- see rule 5 above.

## Worked Example: Real B6 REJECTED Case

Regenerated live against `cot-contrarian-detector` on 2026-07-12 (`--symbols B6,BT,D6 --as-of 2026-07-12`): B6 (British Pound) came back `CROWDED_SHORT` with `cot_index_3y=7.2`, `run_context.data_date=2026-07-07`. Running the gate with `--as-of 2026-07-15` (detector age 8 days, under the default 10-day max) and a news-reaction-failure-analyzer report whose `verdict` is `NOT_CONFIRMED`:

- Crowding normalizes to `CONFIRMED`, `classification=CROWDED_SHORT`, `direction=LONG` (rule 1 does not fire -- crowding is usable).
- News normalizes to `NOT_CONFIRMED` (rule 3 fires; rule 2's INVALID check found nothing).
- `setup_status = REJECTED`, `missing_confirmations = [{"step": "news_failure", "state": "NOT_CONFIRMED", "reason": "<the news report's own verdict_reason>"}]`.
- `direction` remains `LONG` in the output (crowding was confirmed, even though the overall setup was rejected) -- an audit trail of what crowd was being faded, not an actionable signal.

Running the same detector report with `--as-of` set to a later date pushes the detector's age past `--max-detector-age-days`, which instead produces `INSUFFICIENT_EVIDENCE` with reason `detector_json_stale` -- crowding itself becomes unusable before news is ever inspected (rule 1).
