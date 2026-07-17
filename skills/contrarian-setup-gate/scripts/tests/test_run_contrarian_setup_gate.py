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
