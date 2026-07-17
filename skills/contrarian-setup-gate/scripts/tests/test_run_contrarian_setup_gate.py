"""Tests for run_contrarian_setup_gate.py -- the CLI wrapper.

Covers: the 4-class JSON-load hardening matrix across all three inputs
(missing file, non-UTF-8 binary, invalid JSON, and top-level wrong-shape --
the last one exercised end-to-end through gate_logic's normalize_*, since
load_json_file() itself only distinguishes unreadable/parse_error), real-
schema fixture end-to-end runs for every setup_status, and Markdown
rendering for every status (never crashes on a null block).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import gate_logic
import pytest
import run_contrarian_setup_gate as cli

SYMBOL = "B6"
AS_OF = "2026-07-15"


# --- Section 1: load_json_file 4-class hardening ----------------------------


def test_load_json_file_missing_file_is_unreadable(tmp_path: Path) -> None:
    data, reason = cli.load_json_file(str(tmp_path / "does_not_exist.json"))
    assert data is None
    assert reason == "unreadable"


def test_load_json_file_directory_is_unreadable(tmp_path: Path) -> None:
    data, reason = cli.load_json_file(str(tmp_path))
    assert data is None
    assert reason == "unreadable"


def test_load_json_file_non_utf8_binary_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "binary.json"
    path.write_bytes(b"\xff\xfe\x00bad")
    data, reason = cli.load_json_file(str(path))
    assert data is None
    assert reason == "unreadable"


def test_load_json_file_invalid_json_is_parse_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not valid json", encoding="utf-8")
    data, reason = cli.load_json_file(str(path))
    assert data is None
    assert reason == "parse_error"


@pytest.mark.parametrize("raw_text", ["[1, 2, 3]", "null", '"a string"', "42"])
def test_load_json_file_wrong_top_level_shape_loads_but_flows_to_malformed(
    tmp_path: Path, raw_text: str
) -> None:
    """Valid JSON but the wrong top-level shape loads fine here -- it is
    gate_logic.normalize_*'s job to fail closed to `<input>_malformed`."""
    path = tmp_path / "wrong_shape.json"
    path.write_text(raw_text, encoding="utf-8")
    data, reason = cli.load_json_file(str(path))
    assert reason is None
    result = gate_logic.normalize_crowding(data, None, symbol=SYMBOL, as_of=AS_OF, max_age_days=10)
    assert result.state == gate_logic.STATE_INVALID
    assert result.reason == "detector_malformed"


def test_load_json_file_valid_json_succeeds(tmp_path: Path) -> None:
    path = tmp_path / "ok.json"
    path.write_text(json.dumps({"a": 1}), encoding="utf-8")
    data, reason = cli.load_json_file(str(path))
    assert reason is None
    assert data == {"a": 1}


# --- Section 1b: PR #249 user-review round 3 -- whole-file non-finite scan --


def test_load_json_file_overflow_number_is_non_finite(tmp_path: Path) -> None:
    """A syntactically valid JSON number that overflows to `inf` on parse
    (THE USER'S REPRO shape: 1e309) must be caught here, before the data
    ever reaches gate_logic."""
    path = tmp_path / "overflow.json"
    path.write_text('{"classification": 1e309}', encoding="utf-8")
    data, reason = cli.load_json_file(str(path))
    assert data is None
    assert reason == "non_finite"


def test_load_json_file_literal_infinity_is_non_finite(tmp_path: Path) -> None:
    """A bare `Infinity` JSON literal (a non-standard extension json.loads
    accepts by default) must also be caught."""
    path = tmp_path / "infinity.json"
    path.write_text('{"verdict": Infinity}', encoding="utf-8")
    data, reason = cli.load_json_file(str(path))
    assert data is None
    assert reason == "non_finite"


def test_load_json_file_negative_infinity_inside_a_list_is_non_finite(tmp_path: Path) -> None:
    """A non-finite value nested inside a LIST (mirroring a `skipped[]`
    entry) must be caught -- the scan is not top-level-keys-only."""
    path = tmp_path / "neg_inf_in_list.json"
    path.write_text('{"skipped": [{"symbol": "B6", "note": -Infinity}]}', encoding="utf-8")
    data, reason = cli.load_json_file(str(path))
    assert data is None
    assert reason == "non_finite"


def test_load_json_file_nan_literal_nested_deep_is_non_finite(tmp_path: Path) -> None:
    """A NaN literal buried several levels deep (mirroring a nested
    run_context field) must be caught -- the scan recurses to any depth."""
    path = tmp_path / "nan_deep.json"
    path.write_text('{"run_context": {"params": {"nested": {"value": NaN}}}}', encoding="utf-8")
    data, reason = cli.load_json_file(str(path))
    assert data is None
    assert reason == "non_finite"


def test_load_json_file_clean_file_with_ordinary_floats_is_unaffected(tmp_path: Path) -> None:
    """Control: ordinary finite floats (including negative and
    fractional) must never trip the non-finite scan."""
    path = tmp_path / "clean.json"
    path.write_text('{"stop_reference": 1.372, "net_position": -87903.0}', encoding="utf-8")
    data, reason = cli.load_json_file(str(path))
    assert reason is None
    assert data == {"stop_reference": 1.372, "net_position": -87903.0}


def test_contains_non_finite_directly() -> None:
    assert cli._contains_non_finite(float("inf")) is True
    assert cli._contains_non_finite(float("-inf")) is True
    assert cli._contains_non_finite(float("nan")) is True
    assert cli._contains_non_finite({"a": {"b": [1, 2, float("inf")]}}) is True
    assert cli._contains_non_finite({"a": [1, 2, 3], "b": "text", "c": None, "d": True}) is False
    assert cli._contains_non_finite(1.372) is False
    assert cli._contains_non_finite(42) is False


# --- Section 2: fixture builders (real-schema shapes) -----------------------


def _detector_fixture(*, symbol=SYMBOL, classification="CROWDED_SHORT", data_date="2026-07-07"):
    return {
        "schema_version": "1.0",
        "skill": "cot-contrarian-detector",
        "run_context": {"schema_version": "1.0", "as_of": "2026-07-12", "data_date": data_date},
        "markets": [
            {
                "symbol": symbol,
                "status": "ok",
                "data_date": data_date,
                "classification": classification,
            }
        ],
        "skipped": [],
    }


def _news_fixture(
    *,
    symbol=SYMBOL,
    direction="CROWDED_SHORT",
    verdict="CONFIRMED",
    confidence="HIGH",
    as_of="2026-07-13",
):
    return {
        "schema_version": "1.0",
        "skill": "news-reaction-failure-analyzer",
        "symbol": symbol,
        "direction": direction,
        "expected_direction": "BULLISH",
        "actual_reaction": "NO_REACTION",
        "verdict": verdict,
        "confidence": confidence,
        "verdict_reason": "no_significant_drift" if verdict != "CONFIRMED" else None,
        "relevant_events_used": 3,
        "aggregate": {"mean_z3": 0.1, "drift_stat": 0.2, "responded_ratio": 0.33},
        "evidence": [],
        "clusters": [],
        "dropped_events": [],
        "run_context": {"as_of": as_of},
    }


def _price_fixture(
    *,
    symbol=SYMBOL,
    direction="CROWDED_SHORT",
    verdict="CONFIRMED",
    confidence="HIGH",
    as_of="2026-07-14",
):
    checks = {
        "weekly_key_reversal": {"triggered": True, "week_of": "2026-07-06", "detail": ""},
        "failed_extreme": {"triggered": False, "week_of": None, "detail": ""},
        "failed_breakout": {"triggered": False, "week_of": None, "detail": ""},
    }
    return {
        "symbol": symbol,
        "direction": direction,
        "mode": "data",
        "verdict": verdict,
        "confidence": confidence,
        "verdict_reason": "key_reversal" if verdict == "CONFIRMED" else "no_reversal_evidence",
        "checks": checks if verdict == "CONFIRMED" else None,
        "swing_levels": {"stop_reference": 1.3720} if verdict == "CONFIRMED" else None,
        "handoff": {
            "price_action": {
                "verdict": verdict,
                "confidence": confidence,
                "stop_reference": 1.3720 if verdict == "CONFIRMED" else None,
            }
        },
        "run_context": {"as_of": as_of, "schema_version": "1.0"},
    }


def _run_cli(monkeypatch, tmp_path: Path, argv: list[str]) -> int:
    monkeypatch.setattr(
        sys, "argv", ["run_contrarian_setup_gate.py", *argv, "--output-dir", str(tmp_path)]
    )
    return cli.main()


def _write(tmp_path: Path, name: str, payload) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


# --- Section 3: end-to-end scenarios -----------------------------------------


def test_cli_detector_only_crowded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    detector_path = _write(tmp_path, "detector.json", _detector_fixture())
    exit_code = _run_cli(
        monkeypatch,
        tmp_path,
        ["--symbol", SYMBOL, "--detector-json", detector_path, "--as-of", AS_OF],
    )
    assert exit_code == 0
    result = json.loads((tmp_path / f"contrarian_setup_gate_{SYMBOL}_{AS_OF}.json").read_text())
    assert result["setup_status"] == "CROWDED"
    assert result["direction"] == "LONG"
    assert (tmp_path / f"contrarian_setup_gate_{SYMBOL}_{AS_OF}.md").exists()


def test_cli_full_trio_ready_for_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    detector_path = _write(tmp_path, "detector.json", _detector_fixture())
    news_path = _write(tmp_path, "news.json", _news_fixture(verdict="CONFIRMED"))
    price_path = _write(tmp_path, "price.json", _price_fixture(verdict="CONFIRMED"))
    exit_code = _run_cli(
        monkeypatch,
        tmp_path,
        [
            "--symbol",
            SYMBOL,
            "--detector-json",
            detector_path,
            "--news-json",
            news_path,
            "--price-action-json",
            price_path,
            "--as-of",
            AS_OF,
        ],
    )
    assert exit_code == 0
    result = json.loads((tmp_path / f"contrarian_setup_gate_{SYMBOL}_{AS_OF}.json").read_text())
    assert result["setup_status"] == "READY_FOR_PLAN"
    assert result["direction"] == "LONG"
    assert result["gate_confidence"] == "HIGH"
    assert result["invalidation_level"] == 1.3720
    assert result["entry_trigger"] is not None


def test_cli_news_not_confirmed_rejects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    detector_path = _write(tmp_path, "detector.json", _detector_fixture())
    news_path = _write(tmp_path, "news.json", _news_fixture(verdict="NOT_CONFIRMED"))
    exit_code = _run_cli(
        monkeypatch,
        tmp_path,
        [
            "--symbol",
            SYMBOL,
            "--detector-json",
            detector_path,
            "--news-json",
            news_path,
            "--as-of",
            AS_OF,
        ],
    )
    assert exit_code == 0
    result = json.loads((tmp_path / f"contrarian_setup_gate_{SYMBOL}_{AS_OF}.json").read_text())
    assert result["setup_status"] == "REJECTED"
    assert result["missing_confirmations"][0]["step"] == "news_failure"


def test_cli_binary_detector_file_never_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binary/corrupt detector JSON -> INSUFFICIENT_EVIDENCE, exit 0, a
    report is written, and stdout carries no Python traceback."""
    detector_path = tmp_path / "detector.json"
    detector_path.write_bytes(b"\xff\xfe\x00bad")
    exit_code = _run_cli(
        monkeypatch,
        tmp_path,
        ["--symbol", SYMBOL, "--detector-json", str(detector_path), "--as-of", AS_OF],
    )
    assert exit_code == 0
    result = json.loads((tmp_path / f"contrarian_setup_gate_{SYMBOL}_{AS_OF}.json").read_text())
    assert result["setup_status"] == "INSUFFICIENT_EVIDENCE"
    assert result["inputs"]["crowding"]["state"] == "INVALID"
    assert result["missing_confirmations"][0]["reason"] == "detector_unreadable"


def test_cli_missing_detector_file_never_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exit_code = _run_cli(
        monkeypatch,
        tmp_path,
        ["--symbol", SYMBOL, "--detector-json", str(tmp_path / "nope.json"), "--as-of", AS_OF],
    )
    assert exit_code == 0
    result = json.loads((tmp_path / f"contrarian_setup_gate_{SYMBOL}_{AS_OF}.json").read_text())
    assert result["setup_status"] == "INSUFFICIENT_EVIDENCE"


def test_cli_malformed_news_json_top_level_list_never_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    detector_path = _write(tmp_path, "detector.json", _detector_fixture())
    news_path = _write(tmp_path, "news.json", [1, 2, 3])
    exit_code = _run_cli(
        monkeypatch,
        tmp_path,
        [
            "--symbol",
            SYMBOL,
            "--detector-json",
            detector_path,
            "--news-json",
            news_path,
            "--as-of",
            AS_OF,
        ],
    )
    assert exit_code == 0
    result = json.loads((tmp_path / f"contrarian_setup_gate_{SYMBOL}_{AS_OF}.json").read_text())
    assert result["setup_status"] == "INSUFFICIENT_EVIDENCE"
    assert result["inputs"]["news_failure"]["state"] == "INVALID"


def test_cli_news_verdict_list_never_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE USER'S PR #249 P1-1 REPRO, end-to-end through the CLI:
    `verdict: []` in a provided news report used to raise an uncaught
    TypeError (unhashable set-membership check) instead of the exit-0 +
    report contract every other degraded input honors."""
    detector_path = _write(tmp_path, "detector.json", _detector_fixture())
    news_data = _news_fixture(verdict="CONFIRMED")
    news_data["verdict"] = []
    news_path = _write(tmp_path, "news.json", news_data)
    exit_code = _run_cli(
        monkeypatch,
        tmp_path,
        [
            "--symbol",
            SYMBOL,
            "--detector-json",
            detector_path,
            "--news-json",
            news_path,
            "--as-of",
            AS_OF,
        ],
    )
    assert exit_code == 0
    result = json.loads((tmp_path / f"contrarian_setup_gate_{SYMBOL}_{AS_OF}.json").read_text())
    assert result["setup_status"] == "INSUFFICIENT_EVIDENCE"
    assert result["inputs"]["news_failure"]["state"] == "INVALID"
    assert result["missing_confirmations"][0]["reason"] == "news_malformed"


def test_cli_news_confidence_dict_never_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE USER'S PR #249 P1-1 REPRO, end-to-end: `confidence: {}` used to
    raise an uncaught TypeError deep inside gate_confidence computation."""
    detector_path = _write(tmp_path, "detector.json", _detector_fixture())
    news_data = _news_fixture(verdict="CONFIRMED")
    news_data["confidence"] = {}
    news_path = _write(tmp_path, "news.json", news_data)
    exit_code = _run_cli(
        monkeypatch,
        tmp_path,
        [
            "--symbol",
            SYMBOL,
            "--detector-json",
            detector_path,
            "--news-json",
            news_path,
            "--as-of",
            AS_OF,
        ],
    )
    assert exit_code == 0
    result = json.loads((tmp_path / f"contrarian_setup_gate_{SYMBOL}_{AS_OF}.json").read_text())
    assert result["setup_status"] == "INSUFFICIENT_EVIDENCE"
    assert result["inputs"]["news_failure"]["state"] == "INVALID"
    assert result["missing_confirmations"][0]["reason"] == "news_malformed"


def test_cli_detector_classification_int_never_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirror of the user's PR #249 P1-1 repro for the detector's own
    `classification` field: a wrong-typed value used to raise TypeError
    in the `classification not in FADE_DIRECTION` membership check."""
    detector_data = _detector_fixture()
    detector_data["markets"][0]["classification"] = 123
    detector_path = _write(tmp_path, "detector.json", detector_data)
    exit_code = _run_cli(
        monkeypatch,
        tmp_path,
        ["--symbol", SYMBOL, "--detector-json", detector_path, "--as-of", AS_OF],
    )
    assert exit_code == 0
    result = json.loads((tmp_path / f"contrarian_setup_gate_{SYMBOL}_{AS_OF}.json").read_text())
    assert result["setup_status"] == "INSUFFICIENT_EVIDENCE"
    assert result["inputs"]["crowding"]["state"] == "INVALID"
    assert result["missing_confirmations"][0]["reason"] == "detector_unknown_classification"


def test_cli_news_confidence_banana_fails_closed_not_passthrough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #249 P1-2 REPRO, end-to-end: an unknown confidence string used
    to reach `gate_confidence` in the output verbatim instead of being
    rejected as INVALID with a named reason."""
    detector_path = _write(tmp_path, "detector.json", _detector_fixture())
    price_path = _write(tmp_path, "price.json", _price_fixture(verdict="CONFIRMED"))
    news_data = _news_fixture(verdict="CONFIRMED")
    news_data["confidence"] = "BANANA"
    news_path = _write(tmp_path, "news.json", news_data)
    exit_code = _run_cli(
        monkeypatch,
        tmp_path,
        [
            "--symbol",
            SYMBOL,
            "--detector-json",
            detector_path,
            "--news-json",
            news_path,
            "--price-action-json",
            price_path,
            "--as-of",
            AS_OF,
        ],
    )
    assert exit_code == 0
    result = json.loads((tmp_path / f"contrarian_setup_gate_{SYMBOL}_{AS_OF}.json").read_text())
    assert result["setup_status"] == "INSUFFICIENT_EVIDENCE"
    assert result["gate_confidence"] is None
    assert result["inputs"]["news_failure"]["state"] == "INVALID"
    assert result["missing_confirmations"][0]["reason"] == "news_unknown_confidence"


def test_cli_price_verdict_reason_banana_p1_a_repro(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE USER'S P1-A REPRO, end-to-end: verdict_reason "BANANA" on a
    CONFIRMED price-action report used to reach READY_FOR_PLAN with
    entry_trigger="price-action confirmation: BANANA"."""
    detector_path = _write(tmp_path, "detector.json", _detector_fixture())
    news_path = _write(tmp_path, "news.json", _news_fixture(verdict="CONFIRMED"))
    price_data = _price_fixture(verdict="CONFIRMED")
    price_data["verdict_reason"] = "BANANA"
    price_path = _write(tmp_path, "price.json", price_data)
    exit_code = _run_cli(
        monkeypatch,
        tmp_path,
        [
            "--symbol",
            SYMBOL,
            "--detector-json",
            detector_path,
            "--news-json",
            news_path,
            "--price-action-json",
            price_path,
            "--as-of",
            AS_OF,
        ],
    )
    assert exit_code == 0
    result = json.loads((tmp_path / f"contrarian_setup_gate_{SYMBOL}_{AS_OF}.json").read_text())
    assert result["setup_status"] == "INSUFFICIENT_EVIDENCE"
    assert result["entry_trigger"] is None
    assert result["inputs"]["price_action"]["state"] == "INVALID"
    assert result["missing_confirmations"][0]["reason"] == "price_action_unknown_reason"


def test_cli_price_verdict_reason_missing_p1_a_repro(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE USER'S P1-A REPRO (missing variant), end-to-end: a null
    verdict_reason on a CONFIRMED report used to reach READY_FOR_PLAN with
    entry_trigger=null."""
    detector_path = _write(tmp_path, "detector.json", _detector_fixture())
    news_path = _write(tmp_path, "news.json", _news_fixture(verdict="CONFIRMED"))
    price_data = _price_fixture(verdict="CONFIRMED")
    price_data["verdict_reason"] = None
    price_path = _write(tmp_path, "price.json", price_data)
    exit_code = _run_cli(
        monkeypatch,
        tmp_path,
        [
            "--symbol",
            SYMBOL,
            "--detector-json",
            detector_path,
            "--news-json",
            news_path,
            "--price-action-json",
            price_path,
            "--as-of",
            AS_OF,
        ],
    )
    assert exit_code == 0
    result = json.loads((tmp_path / f"contrarian_setup_gate_{SYMBOL}_{AS_OF}.json").read_text())
    assert result["setup_status"] == "INSUFFICIENT_EVIDENCE"
    assert result["missing_confirmations"][0]["reason"] == "price_action_malformed"


def test_cli_price_stop_reference_overflow_to_infinity_p1_b_repro(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE USER'S P1-B REPRO, end-to-end: `stop_reference: 1e309` is a
    syntactically valid JSON number that overflows to Python's `inf` on
    parse. This used to reach READY_FOR_PLAN with
    "invalidation_level": Infinity in the written file -- not valid
    standard JSON, and unusable as a stop level for a downstream
    position-sizing skill. As of the round-3 fix, the CLI's whole-file
    non-finite scan now catches this BEFORE gate_logic's stop_reference-
    specific validation ever runs, so the reason is `price_action_non_finite`
    (not `price_action_invalid_stop_reference`, which is still reachable
    for finite-but-otherwise-bad values like 0, negative, or bool)."""
    detector_path = _write(tmp_path, "detector.json", _detector_fixture())
    news_path = _write(tmp_path, "news.json", _news_fixture(verdict="CONFIRMED"))
    price_data = _price_fixture(verdict="CONFIRMED")
    price_data["swing_levels"]["stop_reference"] = 1e309
    price_data["handoff"]["price_action"]["stop_reference"] = 1e309
    price_path = _write(tmp_path, "price.json", price_data)
    exit_code = _run_cli(
        monkeypatch,
        tmp_path,
        [
            "--symbol",
            SYMBOL,
            "--detector-json",
            detector_path,
            "--news-json",
            news_path,
            "--price-action-json",
            price_path,
            "--as-of",
            AS_OF,
        ],
    )
    assert exit_code == 0
    output_path = tmp_path / f"contrarian_setup_gate_{SYMBOL}_{AS_OF}.json"
    output_text = output_path.read_text()
    assert "Infinity" not in output_text  # never emitted, even indirectly
    result = json.loads(output_text)  # standard-JSON-compliant: no NaN/Infinity tokens to parse
    assert result["setup_status"] == "INSUFFICIENT_EVIDENCE"
    assert result["invalidation_level"] is None
    assert result["missing_confirmations"][0]["reason"] == "price_action_non_finite"


def test_cli_price_stop_reference_nan_literal_p1_b_repro(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare `NaN` JSON literal (a non-standard extension `json.loads`
    accepts by default) must also fail closed, not just numeric overflow.
    As of the round-3 fix, this is caught by the CLI's whole-file scan
    (`price_action_non_finite`) before gate_logic's own stop_reference
    validation would otherwise have run."""
    detector_path = _write(tmp_path, "detector.json", _detector_fixture())
    news_path = _write(tmp_path, "news.json", _news_fixture(verdict="CONFIRMED"))
    price_data = _price_fixture(verdict="CONFIRMED")
    price_path = tmp_path / "price_nan.json"
    raw_text = json.dumps(price_data).replace("1.372", "NaN")
    price_path.write_text(raw_text, encoding="utf-8")
    exit_code = _run_cli(
        monkeypatch,
        tmp_path,
        [
            "--symbol",
            SYMBOL,
            "--detector-json",
            detector_path,
            "--news-json",
            news_path,
            "--price-action-json",
            str(price_path),
            "--as-of",
            AS_OF,
        ],
    )
    assert exit_code == 0
    result = json.loads((tmp_path / f"contrarian_setup_gate_{SYMBOL}_{AS_OF}.json").read_text())
    assert result["setup_status"] == "INSUFFICIENT_EVIDENCE"
    assert result["missing_confirmations"][0]["reason"] == "price_action_non_finite"


def test_cli_price_stop_reference_bool_true_p1_b_repro(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`isinstance(True, int)` is True in Python -- a bare `true` JSON
    literal for stop_reference must not silently pass as 1.0. Unlike the
    overflow/NaN repros above, `bool` is never a `float`, so the CLI's
    whole-file non-finite scan does not intercept it -- this still
    reaches gate_logic's own `price_action_invalid_stop_reference` check,
    confirming that check remains reachable for finite-but-wrong-typed
    values after the round-3 fix."""
    detector_path = _write(tmp_path, "detector.json", _detector_fixture())
    news_path = _write(tmp_path, "news.json", _news_fixture(verdict="CONFIRMED"))
    price_data = _price_fixture(verdict="CONFIRMED")
    price_data["swing_levels"]["stop_reference"] = True
    price_data["handoff"]["price_action"]["stop_reference"] = True
    price_path = _write(tmp_path, "price.json", price_data)
    exit_code = _run_cli(
        monkeypatch,
        tmp_path,
        [
            "--symbol",
            SYMBOL,
            "--detector-json",
            detector_path,
            "--news-json",
            news_path,
            "--price-action-json",
            price_path,
            "--as-of",
            AS_OF,
        ],
    )
    assert exit_code == 0
    result = json.loads((tmp_path / f"contrarian_setup_gate_{SYMBOL}_{AS_OF}.json").read_text())
    assert result["setup_status"] == "INSUFFICIENT_EVIDENCE"
    assert result["missing_confirmations"][0]["reason"] == "price_action_invalid_stop_reference"


def test_generate_json_report_allow_nan_false_raises_on_residual_non_finite_value(
    tmp_path: Path,
) -> None:
    """Writer-level defense-in-depth test (PR #249 user-review round 2,
    P1-B): if a non-finite float ever slipped past gate_logic's own
    validation (a future regression), the CLI's JSON writer must raise
    loudly via allow_nan=False rather than silently emitting non-standard
    JSON."""
    bad_result = {"setup_status": "READY_FOR_PLAN", "invalidation_level": float("inf")}
    output_path = tmp_path / "bad.json"
    with pytest.raises(ValueError):
        cli.generate_json_report(bad_result, output_path)


# --- Section 4: PR #249 user-review round 3 -- non-finite anywhere, end-to-end --


def _assert_clean_non_finite_report(
    tmp_path: Path, expected_status: str, expected_reason: str
) -> None:
    """Shared assertions for the (a)-(d) repros: exit 0, BOTH reports
    written (JSON and MD), the expected status/reason, and no traceback
    left in the JSON report's content (a crash would have produced no
    file, or a partial one -- reading it back via json.loads is itself
    proof the file is valid, complete JSON)."""
    json_path = tmp_path / f"contrarian_setup_gate_{SYMBOL}_{AS_OF}.json"
    md_path = tmp_path / f"contrarian_setup_gate_{SYMBOL}_{AS_OF}.md"
    assert json_path.exists(), "JSON report was not written -- the CLI must never partially fail"
    assert md_path.exists(), "Markdown report was not written -- the CLI must never partially fail"
    result = json.loads(json_path.read_text())  # raises if the file isn't valid, complete JSON
    assert result["setup_status"] == expected_status
    assert any(item["reason"] == expected_reason for item in result["missing_confirmations"])


def test_cli_detector_classification_overflow_p1_round3_repro(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) THE USER'S ROUND-3 REPRO: detector classification: 1e309.
    Before this fix, normalization correctly marked crowding INVALID, but
    then the raw `inf` value echoed into the audit block crashed the
    writer's allow_nan=False -> exit 1, no/partial report."""
    detector_data = _detector_fixture()
    detector_data["markets"][0]["classification"] = 1e309
    detector_path = tmp_path / "detector.json"
    detector_path.write_text(json.dumps(detector_data), encoding="utf-8")
    exit_code = _run_cli(
        monkeypatch,
        tmp_path,
        ["--symbol", SYMBOL, "--detector-json", str(detector_path), "--as-of", AS_OF],
    )
    assert exit_code == 0
    _assert_clean_non_finite_report(tmp_path, "INSUFFICIENT_EVIDENCE", "detector_non_finite")


def test_cli_news_verdict_overflow_p1_round3_repro(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) THE USER'S ROUND-3 REPRO variant: news verdict: 1e309."""
    detector_path = _write(tmp_path, "detector.json", _detector_fixture())
    news_data = _news_fixture(verdict="CONFIRMED")
    news_data["verdict"] = 1e309
    news_path = tmp_path / "news.json"
    news_path.write_text(json.dumps(news_data), encoding="utf-8")
    exit_code = _run_cli(
        monkeypatch,
        tmp_path,
        [
            "--symbol",
            SYMBOL,
            "--detector-json",
            detector_path,
            "--news-json",
            str(news_path),
            "--as-of",
            AS_OF,
        ],
    )
    assert exit_code == 0
    _assert_clean_non_finite_report(tmp_path, "INSUFFICIENT_EVIDENCE", "news_non_finite")


def test_cli_nan_literal_nested_deep_in_run_context_p1_round3_repro(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) A bare NaN literal nested deep inside run_context -- a field
    the gate doesn't even read -- must still reject the whole file."""
    detector_data = _detector_fixture()
    detector_path = tmp_path / "detector.json"
    raw_text = json.dumps(detector_data)
    # Inject a deeply-nested NaN into run_context.params without disturbing
    # the rest of the structure.
    raw_text = raw_text.replace(
        '"data_date": "2026-07-07"}',
        '"data_date": "2026-07-07", "params": {"nested": {"value": NaN}}}',
        1,
    )
    detector_path.write_text(raw_text, encoding="utf-8")
    exit_code = _run_cli(
        monkeypatch,
        tmp_path,
        ["--symbol", SYMBOL, "--detector-json", str(detector_path), "--as-of", AS_OF],
    )
    assert exit_code == 0
    _assert_clean_non_finite_report(tmp_path, "INSUFFICIENT_EVIDENCE", "detector_non_finite")


def test_cli_negative_infinity_inside_skipped_entry_p1_round3_repro(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(d) -Infinity inside a skipped[] entry -- again, a field the gate's
    decision logic doesn't read for a symbol that IS present in
    markets[] -- must still reject the whole file (whole-file scan, not
    field-scoped)."""
    detector_data = _detector_fixture()
    detector_data["skipped"] = [{"symbol": "ZZ", "note": float("-inf")}]
    detector_path = tmp_path / "detector.json"
    # json.dumps (default allow_nan=True) renders float('-inf') as the
    # bare `-Infinity` token, which json.loads reads back as -inf.
    raw_text = json.dumps(detector_data)
    assert "-Infinity" in raw_text
    detector_path.write_text(raw_text, encoding="utf-8")
    exit_code = _run_cli(
        monkeypatch,
        tmp_path,
        ["--symbol", SYMBOL, "--detector-json", str(detector_path), "--as-of", AS_OF],
    )
    assert exit_code == 0
    _assert_clean_non_finite_report(tmp_path, "INSUFFICIENT_EVIDENCE", "detector_non_finite")


def test_cli_clean_file_control_unaffected_by_non_finite_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(e) Control: an entirely ordinary, finite-numbers-only trio must
    still reach READY_FOR_PLAN exactly as before -- the new scan must not
    have any false-positive cost on legitimate reports."""
    detector_path = _write(tmp_path, "detector.json", _detector_fixture())
    news_path = _write(tmp_path, "news.json", _news_fixture(verdict="CONFIRMED"))
    price_path = _write(tmp_path, "price.json", _price_fixture(verdict="CONFIRMED"))
    exit_code = _run_cli(
        monkeypatch,
        tmp_path,
        [
            "--symbol",
            SYMBOL,
            "--detector-json",
            detector_path,
            "--news-json",
            news_path,
            "--price-action-json",
            price_path,
            "--as-of",
            AS_OF,
        ],
    )
    assert exit_code == 0
    result = json.loads((tmp_path / f"contrarian_setup_gate_{SYMBOL}_{AS_OF}.json").read_text())
    assert result["setup_status"] == "READY_FOR_PLAN"


def test_cli_format_json_only_skips_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    detector_path = _write(tmp_path, "detector.json", _detector_fixture())
    _run_cli(
        monkeypatch,
        tmp_path,
        [
            "--symbol",
            SYMBOL,
            "--detector-json",
            detector_path,
            "--as-of",
            AS_OF,
            "--format",
            "json",
        ],
    )
    assert (tmp_path / f"contrarian_setup_gate_{SYMBOL}_{AS_OF}.json").exists()
    assert not (tmp_path / f"contrarian_setup_gate_{SYMBOL}_{AS_OF}.md").exists()


def test_cli_invalid_as_of_is_argparse_error(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_contrarian_setup_gate.py",
            "--symbol",
            SYMBOL,
            "--detector-json",
            "x.json",
            "--as-of",
            "not-a-date",
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        cli.parse_arguments()
    assert exc_info.value.code == 2


# --- Section 4: Markdown rendering for every status --------------------------


def _result_for_status(status: str) -> dict:
    crowding = gate_logic.NormalizedInput(
        kind=gate_logic.STEP_CROWDING,
        state=gate_logic.STATE_CONFIRMED
        if status != "INSUFFICIENT_EVIDENCE"
        else gate_logic.STATE_INVALID,
        classification="CROWDED_SHORT" if status != "INSUFFICIENT_EVIDENCE" else None,
        direction="LONG" if status != "INSUFFICIENT_EVIDENCE" else None,
        reason=None if status != "INSUFFICIENT_EVIDENCE" else "detector_json_stale",
    )
    if status == "REJECTED":
        news = gate_logic.NormalizedInput(
            kind=gate_logic.STEP_NEWS,
            state=gate_logic.STATE_NOT_CONFIRMED,
            reason="no_reversal_evidence",
        )
        price = gate_logic.pending_input(gate_logic.STEP_PRICE)
    elif status == "CROWDED":
        news = gate_logic.pending_input(gate_logic.STEP_NEWS)
        price = gate_logic.pending_input(gate_logic.STEP_PRICE)
    elif status == "WATCHING_PRICE":
        news = gate_logic.NormalizedInput(
            kind=gate_logic.STEP_NEWS, state=gate_logic.STATE_CONFIRMED, confidence="HIGH"
        )
        price = gate_logic.pending_input(gate_logic.STEP_PRICE)
    elif status == "READY_FOR_PLAN":
        news = gate_logic.NormalizedInput(
            kind=gate_logic.STEP_NEWS, state=gate_logic.STATE_CONFIRMED, confidence="HIGH"
        )
        price = gate_logic.NormalizedInput(
            kind=gate_logic.STEP_PRICE,
            state=gate_logic.STATE_CONFIRMED,
            confidence="MEDIUM",
            stop_reference=1.372,
            entry_trigger="price-action confirmation: key_reversal at week_of=2026-07-06",
        )
    else:  # INSUFFICIENT_EVIDENCE
        news = gate_logic.pending_input(gate_logic.STEP_NEWS)
        price = gate_logic.pending_input(gate_logic.STEP_PRICE)

    return gate_logic.build_gate_result(
        symbol=SYMBOL,
        crowding=crowding,
        news=news,
        price=price,
        max_detector_age_days=10,
        max_report_age_days=7,
        as_of=AS_OF,
    )


@pytest.mark.parametrize(
    "status", ["READY_FOR_PLAN", "WATCHING_PRICE", "CROWDED", "REJECTED", "INSUFFICIENT_EVIDENCE"]
)
def test_markdown_report_renders_for_every_status(tmp_path: Path, status: str) -> None:
    result = _result_for_status(status)
    assert result["setup_status"] == status
    md_path = tmp_path / "report.md"
    cli.generate_markdown_report(result, md_path)  # must not raise on any null block
    text = md_path.read_text(encoding="utf-8")
    assert status in text
    assert result["symbol"] in text


def test_markdown_report_ready_for_plan_shows_warning() -> None:
    result = _result_for_status("READY_FOR_PLAN")
    assert "price_action_confidence_medium" in result["warnings"]
