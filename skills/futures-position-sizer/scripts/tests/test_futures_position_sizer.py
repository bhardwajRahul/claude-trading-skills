"""Tests for futures_position_sizer.py -- the CLI wrapper.

Covers: the hardened gate-json loader (unreadable / parse_error incl.
RecursionError / non_finite, mirroring contrarian-setup-gate's own
load_json_file), full mode-A and mode-B end-to-end runs, argparse-level
numeric validator rejections (inf/nan/1e309/zero/negative), exit-code
asymmetry (ConfigError -> 2, gate-caused NO_TRADE -> 0), and JSON/text
report generation.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import futures_position_sizer as cli
import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "futures_position_sizer.py"


def _run_cli(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
    )


def _ready_gate_fixture(**overrides) -> dict[str, Any]:
    fixture = {
        "schema_version": "1.0",
        "symbol": "B6",
        "setup_status": "READY_FOR_PLAN",
        "direction": "SHORT",
        "gate_confidence": "HIGH",
        "entry_trigger": "price-action confirmation: key_reversal at week_of=2026-07-06",
        "invalidation_level": 1.3450,
        "missing_confirmations": [],
        "warnings": [],
        "inputs": {},
        "run_context": {"symbol": "B6", "as_of": "2026-07-15", "schema_version": "1.0"},
    }
    fixture.update(overrides)
    return fixture


# --- Section 1: hardened gate-json loader -----------------------------------


def test_load_json_file_missing_file_is_unreadable(tmp_path: Path) -> None:
    data, reason = cli.load_json_file(str(tmp_path / "nope.json"))
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


def test_load_json_file_extreme_nesting_is_parse_error(tmp_path: Path) -> None:
    depth = 250_000
    raw_text = "[" * depth + "1" + "]" * depth
    path = tmp_path / "extreme.json"
    path.write_text(raw_text, encoding="utf-8")
    data, reason = cli.load_json_file(str(path))
    assert data is None
    assert reason == "parse_error"


def test_load_json_file_overflow_number_is_non_finite(tmp_path: Path) -> None:
    path = tmp_path / "overflow.json"
    path.write_text('{"invalidation_level": 1e309}', encoding="utf-8")
    data, reason = cli.load_json_file(str(path))
    assert data is None
    assert reason == "non_finite"


def test_load_json_file_literal_infinity_is_non_finite(tmp_path: Path) -> None:
    path = tmp_path / "infinity.json"
    path.write_text('{"invalidation_level": Infinity}', encoding="utf-8")
    data, reason = cli.load_json_file(str(path))
    assert data is None
    assert reason == "non_finite"


def test_load_json_file_nan_nested_deep_is_non_finite(tmp_path: Path) -> None:
    path = tmp_path / "deep_nan.json"
    path.write_text('{"a": {"b": {"c": [1, 2, NaN]}}}', encoding="utf-8")
    data, reason = cli.load_json_file(str(path))
    assert data is None
    assert reason == "non_finite"


def test_load_json_file_deep_but_finite_field_loads_normally(tmp_path: Path) -> None:
    fixture = _ready_gate_fixture()
    deep = 1.0
    for _ in range(500):
        deep = [deep]
    fixture["_deep_unused"] = deep
    path = tmp_path / "deep_finite.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    data, reason = cli.load_json_file(str(path))
    assert reason is None
    assert data["symbol"] == "B6"


def test_load_json_file_valid_json_succeeds(tmp_path: Path) -> None:
    path = tmp_path / "ok.json"
    path.write_text(json.dumps(_ready_gate_fixture()), encoding="utf-8")
    data, reason = cli.load_json_file(str(path))
    assert reason is None
    assert data["setup_status"] == "READY_FOR_PLAN"


# --- Section 2: numeric argparse validators (subprocess, exit code 2) ------


class TestNumericValidatorsRejectDegenerateValues:
    BASE_ARGS = [
        "--symbol",
        "ES",
        "--direction",
        "LONG",
        "--entry",
        "5000.25",
        "--stop",
        "4980.00",
        "--account-size",
        "100000",
        "--risk-pct",
        "1.0",
        "--output-dir",
        "/tmp/does-not-matter",
    ]

    def _with_override(self, flag: str, value: str) -> list[str]:
        args = list(self.BASE_ARGS)
        idx = args.index(flag)
        args[idx + 1] = value
        return args

    @pytest.mark.parametrize("bad_value", ["inf", "-inf", "nan", "1e309", "0", "-5"])
    def test_entry_rejects_degenerate_values(self, bad_value):
        result = _run_cli(self._with_override("--entry", bad_value))
        assert result.returncode == 2

    @pytest.mark.parametrize("bad_value", ["inf", "nan", "1e309", "0", "-5"])
    def test_stop_rejects_degenerate_values(self, bad_value):
        result = _run_cli(self._with_override("--stop", bad_value))
        assert result.returncode == 2

    @pytest.mark.parametrize("bad_value", ["inf", "nan", "1e309", "0", "-100000"])
    def test_account_size_rejects_degenerate_values(self, bad_value):
        result = _run_cli(self._with_override("--account-size", bad_value))
        assert result.returncode == 2

    @pytest.mark.parametrize("bad_value", ["inf", "nan", "0", "-1", "10.01"])
    def test_risk_pct_rejects_degenerate_or_out_of_range_values(self, bad_value):
        result = _run_cli(self._with_override("--risk-pct", bad_value))
        assert result.returncode == 2

    def test_risk_pct_accepts_boundary_10(self):
        result = _run_cli(self._with_override("--risk-pct", "10.0"))
        assert result.returncode == 0

    @pytest.mark.parametrize("bad_value", ["-1", "1.5", "inf", "nan"])
    def test_max_contracts_rejects_degenerate_values(self, bad_value):
        args = [*self.BASE_ARGS, "--max-contracts", bad_value]
        result = _run_cli(args)
        assert result.returncode == 2

    def test_max_contracts_zero_means_no_cap(self):
        args = [*self.BASE_ARGS, "--max-contracts", "0"]
        result = _run_cli(args)
        assert result.returncode == 0

    @pytest.mark.parametrize("bad_value", ["inf", "nan", "1e309", "0", "-1.2"])
    def test_fx_rate_rejects_degenerate_values(self, bad_value):
        args = [*self.BASE_ARGS, "--fx-rate", bad_value]
        result = _run_cli(args)
        assert result.returncode == 2


# --- Section 3: mode A / mode B end-to-end (in-process via cli.main) -------


def _argv(args, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["futures_position_sizer.py", *args])


class TestModeAEndToEnd:
    def test_es_long_hand_checked_sized(self, tmp_path, monkeypatch, capsys):
        out_dir = tmp_path / "reports"
        _argv(
            [
                "--symbol",
                "ES",
                "--direction",
                "LONG",
                "--entry",
                "5000.25",
                "--stop",
                "4980.00",
                "--account-size",
                "100000",
                "--risk-pct",
                "2.0",
                "--as-of",
                "2026-07-17",
                "--output-dir",
                str(out_dir),
                "--format",
                "json",
            ],
            monkeypatch,
        )
        exit_code = cli.main()
        assert exit_code == 0
        json_path = out_dir / "futures_position_size_ES_2026-07-17.json"
        assert json_path.exists()
        payload = json.loads(json_path.read_text())
        assert payload["sizing_status"] == "SIZED"
        assert payload["contracts"] == 1
        assert payload["risk_per_contract_usd"] == pytest.approx(1012.50)

    def test_geometry_violation_exits_2_no_report(self, tmp_path, monkeypatch, capsys):
        out_dir = tmp_path / "reports"
        _argv(
            [
                "--symbol",
                "ES",
                "--direction",
                "LONG",
                "--entry",
                "5000.00",
                "--stop",
                "5010.00",
                "--account-size",
                "100000",
                "--risk-pct",
                "1.0",
                "--output-dir",
                str(out_dir),
            ],
            monkeypatch,
        )
        exit_code = cli.main()
        assert exit_code == 2
        assert not out_dir.exists() or not any(out_dir.iterdir())

    def test_bond_off_grid_entry_exits_2(self, tmp_path, monkeypatch):
        out_dir = tmp_path / "reports"
        _argv(
            [
                "--symbol",
                "ZB",
                "--direction",
                "LONG",
                "--entry",
                "110.16",
                "--stop",
                "108.00",
                "--account-size",
                "100000",
                "--risk-pct",
                "1.0",
                "--output-dir",
                str(out_dir),
            ],
            monkeypatch,
        )
        exit_code = cli.main()
        assert exit_code == 2

    def test_zero_contracts_is_no_trade_exit_0(self, tmp_path, monkeypatch):
        out_dir = tmp_path / "reports"
        _argv(
            [
                "--symbol",
                "ES",
                "--direction",
                "LONG",
                "--entry",
                "5000.25",
                "--stop",
                "4980.00",
                "--account-size",
                "100000",
                "--risk-pct",
                "1.0",
                "--as-of",
                "2026-07-17",
                "--output-dir",
                str(out_dir),
                "--format",
                "json",
            ],
            monkeypatch,
        )
        exit_code = cli.main()
        assert exit_code == 0
        payload = json.loads((out_dir / "futures_position_size_ES_2026-07-17.json").read_text())
        assert payload["sizing_status"] == "NO_TRADE"
        assert payload["no_trade_reason"] == "risk_below_one_contract"

    def test_unknown_symbol_without_overrides_exits_2(self, tmp_path, monkeypatch):
        out_dir = tmp_path / "reports"
        _argv(
            [
                "--symbol",
                "ZZZZ",
                "--direction",
                "LONG",
                "--entry",
                "100.0",
                "--stop",
                "90.0",
                "--account-size",
                "100000",
                "--risk-pct",
                "1.0",
                "--output-dir",
                str(out_dir),
            ],
            monkeypatch,
        )
        exit_code = cli.main()
        assert exit_code == 2

    def test_unknown_symbol_with_full_overrides_sizes(self, tmp_path, monkeypatch):
        out_dir = tmp_path / "reports"
        _argv(
            [
                "--symbol",
                "ZZZZ",
                "--direction",
                "LONG",
                "--entry",
                "100.0",
                "--stop",
                "90.0",
                "--multiplier",
                "10",
                "--tick-size",
                "0.5",
                "--contract-currency",
                "USD",
                "--account-size",
                "100000",
                "--risk-pct",
                "5.0",
                "--as-of",
                "2026-07-17",
                "--output-dir",
                str(out_dir),
                "--format",
                "json",
            ],
            monkeypatch,
        )
        exit_code = cli.main()
        assert exit_code == 0
        payload = json.loads((out_dir / "futures_position_size_ZZZZ_2026-07-17.json").read_text())
        assert payload["sizing_status"] == "SIZED"

    def test_direction_and_gate_json_conflict_exits_2(self, tmp_path):
        # parser.error() raises SystemExit -- exercised via subprocess like
        # every other argparse-level (as opposed to fs.ConfigError-level)
        # usage error, matching position-sizer's own convention.
        gate_path = tmp_path / "gate.json"
        gate_path.write_text(json.dumps(_ready_gate_fixture()), encoding="utf-8")
        result = _run_cli(
            [
                "--gate-json",
                str(gate_path),
                "--direction",
                "LONG",
                "--entry",
                "1.35",
                "--account-size",
                "100000",
                "--risk-pct",
                "1.0",
            ]
        )
        assert result.returncode == 2


class TestModeBEndToEnd:
    def test_ready_gate_report_sizes_via_gate_stop(self, tmp_path, monkeypatch):
        gate_path = tmp_path / "gate.json"
        gate_path.write_text(json.dumps(_ready_gate_fixture()), encoding="utf-8")
        out_dir = tmp_path / "reports"
        _argv(
            [
                "--gate-json",
                str(gate_path),
                "--entry",
                "1.3400",
                "--account-size",
                "100000",
                "--risk-pct",
                "5.0",
                "--fx-rate",
                "1.0",
                "--as-of",
                "2026-07-17",
                "--output-dir",
                str(out_dir),
                "--format",
                "json",
            ],
            monkeypatch,
        )
        exit_code = cli.main()
        assert exit_code == 0
        payload = json.loads((out_dir / "futures_position_size_B6_2026-07-17.json").read_text())
        assert payload["direction"] == "SHORT"
        assert payload["stop"] == pytest.approx(1.3450)
        assert payload["gate"]["setup_status"] == "READY_FOR_PLAN"

    def test_non_ready_gate_report_is_no_trade_exit_0(self, tmp_path, monkeypatch):
        gate_path = tmp_path / "gate.json"
        gate_path.write_text(
            json.dumps(
                _ready_gate_fixture(setup_status="CROWDED", direction=None, invalidation_level=None)
            ),
            encoding="utf-8",
        )
        out_dir = tmp_path / "reports"
        _argv(
            [
                "--gate-json",
                str(gate_path),
                "--entry",
                "1.34",
                "--account-size",
                "100000",
                "--risk-pct",
                "1.0",
                "--as-of",
                "2026-07-17",
                "--output-dir",
                str(out_dir),
                "--format",
                "json",
            ],
            monkeypatch,
        )
        exit_code = cli.main()
        assert exit_code == 0
        payload = json.loads((out_dir / "futures_position_size_B6_2026-07-17.json").read_text())
        assert payload["sizing_status"] == "NO_TRADE"
        assert payload["no_trade_reason"] == "gate_not_ready"

    def test_binary_gate_file_is_no_trade_exit_0(self, tmp_path, monkeypatch):
        gate_path = tmp_path / "gate.json"
        gate_path.write_bytes(b"\xff\xfe\x00bad")
        out_dir = tmp_path / "reports"
        _argv(
            [
                "--gate-json",
                str(gate_path),
                "--symbol",
                "B6",
                "--entry",
                "1.34",
                "--account-size",
                "100000",
                "--risk-pct",
                "1.0",
                "--as-of",
                "2026-07-17",
                "--output-dir",
                str(out_dir),
                "--format",
                "json",
            ],
            monkeypatch,
        )
        exit_code = cli.main()
        assert exit_code == 0
        payload = json.loads((out_dir / "futures_position_size_B6_2026-07-17.json").read_text())
        assert payload["sizing_status"] == "NO_TRADE"
        assert payload["no_trade_reason"] == "gate_json_unreadable"

    def test_symbol_mismatch_is_no_trade_exit_0(self, tmp_path, monkeypatch):
        gate_path = tmp_path / "gate.json"
        gate_path.write_text(json.dumps(_ready_gate_fixture()), encoding="utf-8")
        out_dir = tmp_path / "reports"
        _argv(
            [
                "--gate-json",
                str(gate_path),
                "--symbol",
                "ES",
                "--entry",
                "5000",
                "--account-size",
                "100000",
                "--risk-pct",
                "1.0",
                "--as-of",
                "2026-07-17",
                "--output-dir",
                str(out_dir),
                "--format",
                "json",
            ],
            monkeypatch,
        )
        exit_code = cli.main()
        assert exit_code == 0
        payload = json.loads((out_dir / "futures_position_size_ES_2026-07-17.json").read_text())
        assert payload["no_trade_reason"] == "gate_symbol_mismatch"

    def test_gate_stop_off_tick_grid_bond_is_no_trade_exit_0(self, tmp_path, monkeypatch):
        gate_path = tmp_path / "gate.json"
        gate_path.write_text(
            json.dumps(
                _ready_gate_fixture(symbol="ZB", direction="LONG", invalidation_level=108.16)
            ),
            encoding="utf-8",
        )
        out_dir = tmp_path / "reports"
        _argv(
            [
                "--gate-json",
                str(gate_path),
                "--entry",
                "110.50",
                "--account-size",
                "100000",
                "--risk-pct",
                "1.0",
                "--as-of",
                "2026-07-17",
                "--output-dir",
                str(out_dir),
                "--format",
                "json",
            ],
            monkeypatch,
        )
        exit_code = cli.main()
        assert exit_code == 0
        payload = json.loads((out_dir / "futures_position_size_ZB_2026-07-17.json").read_text())
        assert payload["no_trade_reason"] == "gate_stop_off_tick_grid"

    def test_gate_stop_too_close_is_no_trade_exit_0(self, tmp_path, monkeypatch):
        gate_path = tmp_path / "gate.json"
        gate_path.write_text(
            json.dumps(_ready_gate_fixture(direction="LONG", invalidation_level=1.34995)),
            encoding="utf-8",
        )
        out_dir = tmp_path / "reports"
        _argv(
            [
                "--gate-json",
                str(gate_path),
                "--entry",
                "1.35000",
                "--account-size",
                "100000",
                "--risk-pct",
                "1.0",
                "--as-of",
                "2026-07-17",
                "--output-dir",
                str(out_dir),
                "--format",
                "json",
            ],
            monkeypatch,
        )
        exit_code = cli.main()
        assert exit_code == 0

    def test_entry_always_required_missing_exits_2(self, tmp_path):
        # parser.error() raises SystemExit -- see subprocess note above.
        gate_path = tmp_path / "gate.json"
        gate_path.write_text(json.dumps(_ready_gate_fixture()), encoding="utf-8")
        result = _run_cli(
            [
                "--gate-json",
                str(gate_path),
                "--account-size",
                "100000",
                "--risk-pct",
                "1.0",
            ]
        )
        assert result.returncode == 2


# --- Section 4: --list-specs -------------------------------------------------


def test_list_specs_prints_table_and_exits_0(capsys, monkeypatch):
    _argv(["--list-specs"], monkeypatch)
    exit_code = cli.main()
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "ES" in captured.out
    assert "GC" in captured.out


def test_list_specs_ignores_other_missing_required_args(monkeypatch):
    _argv(["--list-specs"], monkeypatch)
    exit_code = cli.main()
    assert exit_code == 0


# --- Section 5: currency / fx-rate guard ------------------------------------


def test_non_usd_symbol_without_fx_rate_exits_2(tmp_path, monkeypatch):
    out_dir = tmp_path / "reports"
    _argv(
        [
            "--symbol",
            "ZZZZ",
            "--direction",
            "LONG",
            "--entry",
            "100.0",
            "--stop",
            "90.0",
            "--multiplier",
            "10",
            "--tick-size",
            "0.5",
            "--contract-currency",
            "GBP",
            "--account-size",
            "100000",
            "--risk-pct",
            "1.0",
            "--output-dir",
            str(out_dir),
        ],
        monkeypatch,
    )
    exit_code = cli.main()
    assert exit_code == 2


def test_non_usd_symbol_with_fx_rate_sizes(tmp_path, monkeypatch):
    out_dir = tmp_path / "reports"
    _argv(
        [
            "--symbol",
            "ZZZZ",
            "--direction",
            "LONG",
            "--entry",
            "100.0",
            "--stop",
            "90.0",
            "--multiplier",
            "10",
            "--tick-size",
            "0.5",
            "--contract-currency",
            "GBP",
            "--fx-rate",
            "1.25",
            "--account-size",
            "100000",
            "--risk-pct",
            "5.0",
            "--as-of",
            "2026-07-17",
            "--output-dir",
            str(out_dir),
            "--format",
            "json",
        ],
        monkeypatch,
    )
    exit_code = cli.main()
    assert exit_code == 0
    payload = json.loads((out_dir / "futures_position_size_ZZZZ_2026-07-17.json").read_text())
    assert payload["fx_rate_used"] == 1.25


# --- Section 6: text format renders for SIZED and NO_TRADE -----------------


def test_text_format_renders_sized(tmp_path, monkeypatch, capsys):
    out_dir = tmp_path / "reports"
    _argv(
        [
            "--symbol",
            "ES",
            "--direction",
            "LONG",
            "--entry",
            "5000.25",
            "--stop",
            "4980.00",
            "--account-size",
            "100000",
            "--risk-pct",
            "2.0",
            "--as-of",
            "2026-07-17",
            "--output-dir",
            str(out_dir),
            "--format",
            "text",
        ],
        monkeypatch,
    )
    exit_code = cli.main()
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "SIZED" in captured.out
    assert "1" in captured.out


def test_text_format_renders_no_trade(tmp_path, monkeypatch, capsys):
    out_dir = tmp_path / "reports"
    _argv(
        [
            "--symbol",
            "ES",
            "--direction",
            "LONG",
            "--entry",
            "5000.25",
            "--stop",
            "4980.00",
            "--account-size",
            "100000",
            "--risk-pct",
            "1.0",
            "--as-of",
            "2026-07-17",
            "--output-dir",
            str(out_dir),
            "--format",
            "text",
        ],
        monkeypatch,
    )
    exit_code = cli.main()
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "NO_TRADE" in captured.out
    assert "risk_below_one_contract" in captured.out


# --- Section 7: JSON writer never emits non-standard tokens -----------------


def test_json_report_has_no_nan_or_infinity_tokens(tmp_path, monkeypatch):
    out_dir = tmp_path / "reports"
    _argv(
        [
            "--symbol",
            "ES",
            "--direction",
            "LONG",
            "--entry",
            "5000.25",
            "--stop",
            "4980.00",
            "--account-size",
            "100000",
            "--risk-pct",
            "2.0",
            "--as-of",
            "2026-07-17",
            "--output-dir",
            str(out_dir),
            "--format",
            "json",
        ],
        monkeypatch,
    )
    cli.main()
    raw_text = (out_dir / "futures_position_size_ES_2026-07-17.json").read_text()
    assert "NaN" not in raw_text
    assert "Infinity" not in raw_text
    json.loads(raw_text)  # must be strictly valid JSON
