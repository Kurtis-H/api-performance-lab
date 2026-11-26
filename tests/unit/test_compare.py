"""Tests for the performance regression gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from perf.compare import compare, extract, main, to_markdown


def k6_summary(p95: float = 40.0, queries: float = 2, error_rate: float = 0.0) -> dict:
    """The parts of k6's handleSummary data the gate reads."""
    return {
        "metrics": {
            "http_req_duration{name:GET /events}": {"values": {"med": 20.0, "p(95)": p95}},
            "db_queries{name:GET /events}": {"values": {"max": queries}},
            "http_reqs{name:GET /events}": {"values": {"count": 500}},
            "http_req_failed": {"values": {"rate": error_rate}},
            "http_req_duration": {"values": {"p(95)": 99.0}},  # overall, ignored
        }
    }


BASELINE = extract(k6_summary())


def verdicts(current: dict) -> dict[str, bool]:
    return {f.metric: f.ok for f in compare(extract(current), BASELINE)}


def test_extract_reads_per_endpoint_numbers() -> None:
    assert BASELINE == {
        "endpoints": {
            "GET /events": {
                "p50_ms": 20.0,
                "p95_ms": 40.0,
                "queries_max": 2.0,
                "requests": 500.0,
            }
        },
        "error_rate": 0.0,
    }


def test_same_numbers_pass() -> None:
    assert all(verdicts(k6_summary()).values())


def test_one_extra_query_fails() -> None:
    assert verdicts(k6_summary(queries=3))["queries_max"] is False


def test_fewer_queries_pass() -> None:
    assert verdicts(k6_summary(queries=1))["queries_max"] is True


def test_latency_inside_tolerance_passes() -> None:
    # limit = 40 * 2 + 25 = 105
    assert verdicts(k6_summary(p95=105))["p95_ms"] is True


def test_latency_past_tolerance_fails() -> None:
    assert verdicts(k6_summary(p95=106))["p95_ms"] is False


def test_error_rate_rise_fails() -> None:
    assert verdicts(k6_summary(error_rate=0.02))["error_rate"] is False
    assert verdicts(k6_summary(error_rate=0.005))["error_rate"] is True


def test_endpoint_missing_from_run_fails() -> None:
    empty = {"endpoints": {}, "error_rate": 0.0}
    findings = compare(empty, BASELINE)
    assert [f.metric for f in findings if not f.ok] == ["requests"]


def test_markdown_flags_regressions() -> None:
    table = to_markdown(compare(extract(k6_summary(queries=21)), BASELINE))
    assert "1 regression(s)" in table
    assert "| `GET /events` | queries_max | 2 | 21 | 2 | ❌ REGRESSION |" in table


def test_cli_update_then_compare(tmp_path: Path) -> None:
    run = tmp_path / "load.json"
    baseline = tmp_path / "baseline.json"
    report = tmp_path / "report.md"

    run.write_text(json.dumps(k6_summary()))
    assert main([str(run), "--baseline", str(baseline), "--update"]) == 0
    assert json.loads(baseline.read_text()) == BASELINE

    assert main([str(run), "--baseline", str(baseline), "--markdown", str(report)]) == 0
    assert "no regressions" in report.read_text()

    run.write_text(json.dumps(k6_summary(queries=21)))
    assert main([str(run), "--baseline", str(baseline)]) == 1


def test_cli_refuses_a_summary_without_endpoints(tmp_path: Path) -> None:
    run = tmp_path / "smoke.json"
    run.write_text(json.dumps({"metrics": {}}))
    assert main([str(run), "--baseline", str(tmp_path / "b.json")]) == 2


@pytest.mark.parametrize("tolerance", [0.0, 0.5])
def test_tolerance_is_configurable(tolerance: float) -> None:
    findings = compare(extract(k6_summary(p95=80)), BASELINE, tolerance=tolerance, slack_ms=0)
    p95 = next(f for f in findings if f.metric == "p95_ms")
    assert p95.ok is False
