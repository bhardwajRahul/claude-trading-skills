# Full-universe estimate snapshot (v3.7 — sharded collection)

The v3.6.1 pilot economically evaluates ~4% of the listed universe and is
therefore permanently `ranking_scope: final_scoped`. The v3.7 path earns
`final_marketwide` the only honest way: by attempting estimate acquisition for
EVERY listed symbol at least once. On plans where bulk endpoints are 402, that
means collecting per-symbol across deterministic shards within each run's API
budget, persisted into a frozen snapshot.

## Stage: `collect-estimates`

```bash
python3 scripts/run_pipeline.py \
  --stage collect-estimates \
  --shard-index 0 --shard-count 8 \
  --snapshot-dir .cache/us-garp/snapshot-2026-08 \
  --config assets/claude-code-config.example.json \
  [--resume]
```

- The FIRST invocation enumerates listings and **freezes the universe** into
  the snapshot directory (`snapshot-manifest.json` + `universe.jsonl`,
  identified by `snapshot_id` = timestamp + universe SHA-256). Every later
  shard run writes into that frozen snapshot; new listings and delistings go
  to the NEXT snapshot, never mixed in.
- Shard membership is `sha256(symbol) % shard_count` — the same symbol always
  lands in the same shard, so multi-day collection is deterministic.
- Each attempted symbol is normalized (FY1-FY3 EPS/revenue, analyst counts)
  and classified into exactly one bucket:
  `evaluable / no_estimates / negative_eps / unit_mismatch / excluded`
  (precedence: excluded > unit_mismatch > no_estimates > negative_eps >
  evaluable; the unit gate is the round-8 fail-closed
  `requires_unit_reconciliation`).
- Budget exhaustion mid-shard exits with code **3**, records the shard as
  `partial` in the manifest, and is resumable with `--resume`. A shard file
  that already exists without `--resume` is refused.
- Shard summaries (`shard-<i>-summary.json`) and the manifest carry attempted
  / expected counts, per-bucket classification, `as_of`, and calls used.

## Readiness invariants (enforced before `screen-full-snapshot`, PR B)

1. Every shard `status: complete`.
2. Classification counts sum EXACTLY to the frozen universe count.
3. The spread between the oldest and newest shard `as_of` is bounded
   (stale shards must be re-collected).

Only a run screened from a snapshot satisfying all three may emit
`ranking_scope: final_marketwide`.

## Operating on the FMP Starter plan

~2,371 symbols ≈ 1 estimate call each. With a 350-call per-run budget and the
750-call daily plan limit, 8 shards (~300 symbols each) collect in 2 shards/day
over 4 days. After the initial build, only incremental refresh is needed
(new snapshot per refresh cycle; post-earnings and revised names first).

## Shared coverage semantics

`scripts/coverage_semantics.py` is the single source of truth for
`classify_ranking_scope` / `build_coverage_block` /
`derive_ranking_scope_from_audit` / `validate_coverage_block`; both
`run_pipeline` (discovery) and `evaluate_candidates` (final evaluation) import
it, so the tri-state semantics cannot drift between the two sides.
