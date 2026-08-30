# Migration v3.6 → v3.6.1

v3.6.1 hardens the bounded per-symbol fallback that runs when FMP bulk endpoints are plan-gated (HTTP 402). Schema 3 and contract 3.5 are unchanged; the discovery stage gains an honest scope vocabulary, a stratified seed with a documented selection basis, a pre-selection quality probe, and a persistent endpoint-capability cache.

## Runtime identity

```text
skill_version       = 3.6.1
schema_version      = 3
contract_revision   = 3.5
runtime_fingerprint = ug-v3.6.1-claude-code-direct-fmp-20260830
```

Runtime identity is intentionally different from v3.6.0. Do not mix v3.6.0 audits, packets, or checkpoints with v3.6.1 artifacts; rerun discovery.

## Why

A live run on 2026-08-30 (2,371 listings, all five bulk endpoints 402) showed that the fallback seeded 80 of 2,371 names with no economic data at seed time, that the within-cell ranking saturated at USD 100M/day and fell back to ticker order for large caps, that `provider_exhausted=true` was passed unconditionally, that `scope_complete=true` was read as economic coverage, and that a name with EV/FCF 126x reached the deep-dive slots because no FCF evidence existed before selection.

## Behavioural changes

### Seed selection (`diversified_seed`)

- Cells are sector × market-cap bucket. Quota per cell ∝ √(cell size), reconciled to the seed limit with Hamilton apportionment; every non-empty cell gets at least one seat. The result is independent of cell iteration order.
- Within a cell: `pre_enrichment_score` desc → raw single-day dollar volume desc → market cap desc → fewer missing price/volume fields → `sha256(analysis_date:symbol)`. The raw ticker string is never a tie-break. The log10 dollar-volume term is no longer capped.
- `audit/seed-audit.json` (also embedded in `provider-prefilter-audit.json` and `run-summary.json`) records `seed_selection_basis` (`stratified_liquidity_proxy` when economic fields are absent for the majority of rows), `economic_metrics_available_for_seed`, `cell_count`, `quota_method`, tie-break counters, and the configured/effective seed limits.

### Dynamic seed limit

New config keys with defaults: `pre_enrichment_limit: 180` (was 80), `seed_limit_cap: 200`, `quality_probe_limit: 35`, `candidate_packet_reserve_calls: 30`, `retry_reserve_calls: 25`.

```text
reserved  = quality_probe_limit + exact_liquidity_limit
          + candidate_packet_reserve_calls + retry_reserve_calls
effective = min(pre_enrichment_limit, seed_limit_cap,
                max_api_calls - api_calls_made - reserved)
```

An effective limit below 20 fails the run with `estimate seed budget insufficient` instead of silently producing a thin pool. The probe reserve is counted twice per target (key metrics + annual income statement).

### Quality probe before pool selection

After estimate normalization, the union of lane rows is ranked by best lane score and the top `quality_probe_limit` symbols receive one `key-metrics-ttm` call each. Rows gain `roic_pct`, `fcf_yield_pct`, `ev_to_fcf`, `net_debt_to_ebitda`, `sbc_revenue_pct`, and `sbc_adjusted_fcf_yield_pct` (computed on the market-cap basis: FCF yield − SBC/revenue × revenue/market cap). `audit/quality-probe-audit.json` records attempts, resolutions, and calls used.

Lane scores now include an FCF-yield term (weight 1.0 core_garp / quality_near_miss, 0.5 high_growth_exception) and a leverage penalty above 2.5x net debt / EBITDA. A probe-resolved row with SBC-adjusted (or standard) FCF yield below 1% is excluded from every lane except `high_growth_exception`, where it stays with `provider_prefilter_flags: ["weak_fcf_support"]` and a −10 score. Exclusions are listed under `fcf_prefilter_excluded_symbols` in the discovery audit.

### Honest scope fields

Route selection and completeness are separate thresholds: the bulk route is used from `bulk_estimate_minimum_coverage_pct` (20%), but `economic_screen_scope_complete` / `economic_candidate_universe_exhausted` are true only when the bulk estimates covered every listing-universe symbol (exact count equality, `covered_symbol_count >= universe_symbol_count`; deliberately not a configurable ratio, so no setting can declare a partially covered run complete). A 25%- or even 99%-covered bulk run is a bounded economic screen, exactly like the per-symbol fallback.

`run-summary.json` and `NEXT_ACTION.json` add `listing_enumeration_complete`, `economic_screen_scope_complete`, `listing_universe_count`, `estimate_seed_count`, `estimate_seed_coverage_pct`, `valid_estimate_count`, `valid_estimate_coverage_pct`. `scope_complete` is retained for readers of v3.6.0 output and now carries `scope_complete_deprecated_note`. The discovery audit adds `listing_provider_exhausted`, `estimate_seed_exhausted`, `economic_candidate_universe_exhausted`, and `provider_exhausted_scope` (`estimate_seed` on the fallback path). The contract-validated `screening_audit.scope` block is unchanged.

### Growth basis

**Actuals are verified or absent, and growth is compared on one basis.** `latest_actual_eps` is populated only from (a) a provider row explicitly marked as actual whose period has ended AND whose publication timestamp is at or before `analysis_as_of` (rows without a provable publication time are rejected — a current snapshot replayed against a historical as-of must not leak later-published actuals), or (b) the annual income statement fetched during the quality probe whose `acceptedDate` (with time of day; the date-only `filingDate` is never accepted) is at or before `analysis_as_of` (`latest_actual_verified: true`, `latest_actual_basis: gaap_diluted`, `latest_actual_source_ids`). An unmarked prior-year estimate row is never reported as an actual; it is exposed as `fy0_consensus_eps` and, because it shares the provider's (usually adjusted) basis with FY1/FY3, it is the comparator for `current_year_growth_pct`, `fy1_eps_below_fy0_consensus`, and `growth_pattern` (`growth_pattern_basis: consensus_same_basis`; `unknown` without a prior-year row). The GAAP actual is reported alongside (`current_year_growth_pct_vs_gaap_actual`, `fy1_eps_below_latest_actual`) and, when it differs from the FY0 consensus by more than 15%, `estimate_basis_likely_adjusted: true` warns that GAAP-vs-consensus comparisons are mixed-basis. Found on DOCS: FY1 non-GAAP consensus $1.35 vs GAAP actual $0.98 looked like a decline; same-basis FY26 non-GAAP $1.52 → $1.35 is the real (and still negative) comparison.

`normalize_estimates.py` adds `latest_actual_eps`, `latest_actual_period_end`, `fy1_eps_below_latest_actual`, `current_year_growth_pct`, `eps_growth_fy1_to_fy3_pct` (alias of `eps_growth_pct`), `eps_growth_actual_to_fy3_pct`, `growth_pattern` (`steady | accelerating | trough_recovery | declining | unknown`), and `growth_basis_source_ids`. A `trough_recovery` row is removed from `core_garp` and admitted to `quality_near_miss` with the `earnings_recovery` flag.

### Deep-dive selection with a small budget

`screen_universe._selection_lane` now also routes `growth_pattern == trough_recovery` rows to `quality_near_miss` (the pool-stage rule alone did not reach the final lane). When `max_deep_dive_candidates` is smaller than the lane plan total (default 2/1/1/1 = 5), selection walks the priority order and treats each lane quota as a cap instead of filling lanes in plan order; with a 3-name budget the best cyclical can now win a slot rather than every slot going to the first two lanes. Budgets at or above the plan total keep the lane-first fill.

### Cyclicality and foreign private issuers

Gold, silver, precious/base metals, copper, uranium, coal, metals & mining, and mineral names classify as cyclicality 4; aluminum and semiconductor equipment as 3 (the bare `semiconductor` needle was removed so equipment names do not inherit 4). Rows whose ISIN prefix (or, failing that, listing country) is not `US` carry `foreign_private_issuer_review`, and their packets add `form_20f_6k_verification` to `required_next_checks`. Neither flag excludes a name.

### Endpoint capability cache

The generated client remembers a 402/403 bulk response in the SQLite cache (`capability:<url>`, 30-day TTL) and pre-disables that endpoint on later runs without spending a call. `respect_capability_cache=False` re-probes unconditionally. Diagnostics add `capability_cache_hits` and `remaining_calls`.

## Operator checklist

1. Build `market-context.json` and `global-sources.json` **before** running `run_pipeline.py`; every source `retrieved_at` must precede `analysis_as_of`.
2. Run discovery, then `manage_run_state.py init` / `set-screening-audit` / `set-funnel --preflight-passed-count N`.
3. `set-screening-audit` now copies the enrichment queue and provider-prefilter pool into `run/audit/` and rewrites their paths to the same run-relative base as the universe/candidate artifacts, and `prepublish_audit.py` also accepts audit-relative bare names, so `--artifact-root <run>` resolves every artifact from one root.
4. Keep shared `fmp-*` source entries byte-identical across candidate ledgers and the global ledger.
