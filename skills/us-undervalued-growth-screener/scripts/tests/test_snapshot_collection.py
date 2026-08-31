"""v3.7 PR A: sharded estimate snapshot + shared coverage semantics."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import coverage_semantics as COV  # noqa: E402
import estimate_snapshot as SNAP  # noqa: E402
import run_pipeline as PIPELINE  # noqa: E402
from fmp_client import ApiCallBudgetExceeded  # noqa: E402
from screen_universe import requires_unit_reconciliation  # noqa: E402

AS_OF = datetime(2026, 8, 30, 23, 0, tzinfo=timezone.utc)


def _listing(symbol: str, **overrides) -> dict:
    row = {
        "symbol": symbol,
        "company_name": f"{symbol} Corp",
        "exchange": "NASDAQ",
        "sector": "Technology",
        "industry": "Software - Application",
        "price": 20.0,
        "market_cap": 2_000_000_000,
        "volume": 1_000_000,
        "is_actively_trading": True,
        "is_common_stock": True,
        "common_stock": True,
        "currency": "USD",
        "country": "US",
        "isin": None,
        "is_adr": False,
        "sector_profile_type": "general",
    }
    row.update(overrides)
    return row


def _estimate_rows(fy1: float = 2.0) -> list[dict]:
    rows = []
    for offset, eps in ((0, fy1), (1, fy1 * 1.15), (2, fy1 * 1.3)):
        year = 2026 + offset
        rows.append(
            {
                "date": f"{year}-12-31",
                "fiscalYear": str(year),
                "epsAvg": eps,
                "epsHigh": eps * 1.1,
                "epsLow": eps * 0.9,
                "revenueAvg": 1_000_000_000 * (1 + 0.1 * offset),
                "numAnalystsEps": 4,
                "numAnalystsRevenue": 4,
            }
        )
    return rows


class FakeClient:
    def __init__(self, estimates: dict[str, list[dict]], budget: int | None = None):
        self._estimates = estimates
        self._budget = budget
        self.calls = 0

    def get_analyst_estimates(self, symbol: str, *, period: str = "annual", limit: int = 6):
        if self._budget is not None and self.calls >= self._budget:
            raise ApiCallBudgetExceeded("budget exhausted")
        self.calls += 1
        return self._estimates.get(symbol, [])

    def diagnostics(self) -> dict:
        return {"api_calls_made": self.calls, "cache_hits": 0}


def _config() -> dict:
    return dict(PIPELINE.DEFAULT_CONFIG)


class StableShardTests(unittest.TestCase):
    def test_shard_assignment_is_deterministic_and_case_insensitive(self) -> None:
        self.assertEqual(SNAP.stable_shard("AAPL", 8), SNAP.stable_shard(" aapl ", 8))
        first = [SNAP.stable_shard(f"SYM{i}", 8) for i in range(200)]
        second = [SNAP.stable_shard(f"SYM{i}", 8) for i in range(200)]
        self.assertEqual(first, second)
        self.assertTrue(all(0 <= shard < 8 for shard in first))
        # every shard gets some members over a reasonable universe
        self.assertEqual(len(set(first)), 8)


class ClassifySymbolTests(unittest.TestCase):
    def _classify(self, listing: dict, normalized: dict) -> str:
        return SNAP.classify_symbol(
            listing, normalized, requires_unit_reconciliation=requires_unit_reconciliation
        )

    def test_precedence(self) -> None:
        base = _listing("T")
        self.assertEqual(
            self._classify(_listing("T", is_common_stock=False), {"estimate_periods": []}),
            "excluded",
        )
        self.assertEqual(
            self._classify(_listing("T", country="CN"), {"estimate_periods": []}),
            "unit_mismatch",
        )
        self.assertEqual(self._classify(base, {"estimate_periods": []}), "no_estimates")
        self.assertEqual(
            self._classify(base, {"estimate_periods": [{}], "fy1_eps": -0.5}), "negative_eps"
        )
        self.assertEqual(
            self._classify(base, {"estimate_periods": [{}], "fy1_eps": 2.0}), "evaluable"
        )

    def test_implausible_forward_pe_is_unit_mismatch(self) -> None:
        verdict = self._classify(
            _listing("T"), {"estimate_periods": [{}], "fy1_eps": 20.0, "forward_pe": 0.45}
        )
        self.assertEqual(verdict, "unit_mismatch")


class CollectEstimatesStageTests(unittest.TestCase):
    def _universe(self) -> list[dict]:
        rows = [_listing(f"SYM{i}") for i in range(20)]
        rows.append(_listing("FRGN", country="CN", currency="CNY"))
        rows.append(_listing("NOEST"))
        return rows

    def _snapshot(self, tmp: str) -> tuple[Path, list[dict]]:
        snapshot_dir = Path(tmp) / "snap"
        universe = self._universe()
        SNAP.create_snapshot(snapshot_dir, universe, shard_count=2, as_of=AS_OF)
        return snapshot_dir, universe

    def _estimates_for(self, universe: list[dict]) -> dict[str, list[dict]]:
        estimates = {row["symbol"]: _estimate_rows() for row in universe}
        estimates["NOEST"] = []
        return estimates

    def test_full_shard_collection_classifies_and_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_dir, universe = self._snapshot(tmp)
            estimates = self._estimates_for(universe)
            for shard in (0, 1):
                result = PIPELINE.execute_collect_estimates(
                    FakeClient(estimates),
                    _config(),
                    analysis_as_of=AS_OF,
                    snapshot_dir=snapshot_dir,
                    shard_index=shard,
                    shard_count=2,
                    resume=False,
                )
                self.assertEqual(result.exit_code, 0)
                self.assertEqual(result.summary["status"], "shard_complete")
            status = SNAP.snapshot_status(SNAP.load_manifest(snapshot_dir))
            self.assertTrue(status["all_shards_complete"])
            self.assertTrue(status["classification_matches_universe"])
            self.assertEqual(status["universe_count"], len(universe))
            totals = status["classified_totals"]
            self.assertEqual(totals["unit_mismatch"], 1)  # FRGN
            self.assertEqual(totals["no_estimates"], 1)  # NOEST
            self.assertEqual(totals["evaluable"], len(universe) - 2)

    def test_budget_exhaustion_is_partial_and_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_dir, universe = self._snapshot(tmp)
            estimates = self._estimates_for(universe)
            shard0 = [row for row in universe if SNAP.stable_shard(row["symbol"], 2) == 0]
            partial = PIPELINE.execute_collect_estimates(
                FakeClient(estimates, budget=3),
                _config(),
                analysis_as_of=AS_OF,
                snapshot_dir=snapshot_dir,
                shard_index=0,
                shard_count=2,
                resume=False,
            )
            self.assertEqual(partial.exit_code, 3)
            self.assertEqual(partial.summary["status"], "shard_partial_budget")
            self.assertTrue(partial.summary["budget_exhausted"])
            manifest = SNAP.load_manifest(snapshot_dir)
            self.assertEqual(manifest["shards"]["0"]["status"], "partial")
            self.assertEqual(manifest["shards"]["0"]["attempted"], 3)

            # a second run without --resume must refuse
            with self.assertRaises(ValueError):
                PIPELINE.execute_collect_estimates(
                    FakeClient(estimates),
                    _config(),
                    analysis_as_of=AS_OF,
                    snapshot_dir=snapshot_dir,
                    shard_index=0,
                    shard_count=2,
                    resume=False,
                )

            resumed = PIPELINE.execute_collect_estimates(
                FakeClient(estimates),
                _config(),
                analysis_as_of=AS_OF,
                snapshot_dir=snapshot_dir,
                shard_index=0,
                shard_count=2,
                resume=True,
            )
            self.assertEqual(resumed.exit_code, 0)
            manifest = SNAP.load_manifest(snapshot_dir)
            entry = manifest["shards"]["0"]
            self.assertEqual(entry["status"], "complete")
            self.assertEqual(entry["attempted"], len(shard0))
            rows = SNAP.load_shard_rows(snapshot_dir, 0)
            self.assertEqual(len({row["symbol"] for row in rows}), len(shard0))

    def test_shard_count_mismatch_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_dir, _ = self._snapshot(tmp)
            with self.assertRaises(ValueError):
                PIPELINE.execute_collect_estimates(
                    FakeClient({}),
                    _config(),
                    analysis_as_of=AS_OF,
                    snapshot_dir=snapshot_dir,
                    shard_index=0,
                    shard_count=4,
                    resume=False,
                )


class CoverageSemanticsSharedTests(unittest.TestCase):
    def test_pipeline_and_evaluator_share_one_implementation(self) -> None:
        import evaluate_candidates as EVAL

        self.assertIs(PIPELINE.classify_ranking_scope, COV.classify_ranking_scope)
        # the evaluator delegates to the shared derive function
        self.assertIn(
            "derive_ranking_scope_from_audit", EVAL._derive_ranking_scope.__code__.co_names
        )

    def test_validate_coverage_block(self) -> None:
        block = COV.build_coverage_block(
            ranking_scope="final_scoped",
            listing_universe_count=2371,
            economic_attempt_count=180,
            economically_evaluable_count=98,
            quality_probe_count=35,
            deep_dive_count=3,
        )
        self.assertEqual(COV.validate_coverage_block(block), [])
        broken = dict(block)
        broken["economic_attempt_count"] = 5000
        problems = COV.validate_coverage_block(broken)
        self.assertTrue(any("exceeds listing_universe_count" in p for p in problems))
        broken2 = dict(block)
        broken2["ranking_scope"] = "complete"
        self.assertTrue(COV.validate_coverage_block(broken2))
        broken3 = dict(block)
        del broken3["economically_evaluable_count"]
        self.assertTrue(COV.validate_coverage_block(broken3))


if __name__ == "__main__":
    unittest.main()
