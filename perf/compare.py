"""Performance regression gate: compare a k6 run with a saved baseline.

    python -m perf.compare results/load.json --baseline perf/baseline.json
    python -m perf.compare results/load.json --baseline perf/baseline.json --update

Rules, per endpoint:
  * queries: any increase in the max SQL statements per request fails. Query
    counts don't depend on machine speed, so this gate has no noise.
  * p95 latency: fails when above baseline * (1 + tolerance) + slack_ms.
    CI machines vary, so this gate is deliberately loose.
  * error rate (whole run): fails when it rises more than 1 point.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TOLERANCE = 1.0  # p95 may double...
DEFAULT_SLACK_MS = 25.0  # ...plus this, so tiny numbers don't trip on noise
ERROR_RATE_SLACK = 0.01


@dataclass(frozen=True)
class Finding:
    endpoint: str
    metric: str
    baseline: float | None
    current: float | None
    limit: float | None
    ok: bool

    @property
    def verdict(self) -> str:
        return "ok" if self.ok else "REGRESSION"


def extract(k6_summary: dict[str, Any]) -> dict[str, Any]:
    """Pull per-endpoint numbers out of k6's handleSummary data."""
    metrics = k6_summary.get("metrics", {})
    endpoints: dict[str, dict[str, float]] = {}
    for key, metric in metrics.items():
        if not key.startswith("http_req_duration{name:"):
            continue
        name = key[len("http_req_duration{name:") : -1]
        values = metric.get("values", {})
        queries = metrics.get(f"db_queries{{name:{name}}}", {}).get("values", {})
        requests = metrics.get(f"http_reqs{{name:{name}}}", {}).get("values", {})
        endpoints[name] = {
            "p50_ms": round(float(values.get("med", 0.0)), 2),
            "p95_ms": round(float(values.get("p(95)", 0.0)), 2),
            "queries_max": float(queries.get("max", 0.0)),
            "requests": float(requests.get("count", 0.0)),
        }
    failed = metrics.get("http_req_failed", {}).get("values", {})
    return {"endpoints": endpoints, "error_rate": round(float(failed.get("rate", 0.0)), 4)}


def compare(
    current: dict[str, Any],
    baseline: dict[str, Any],
    tolerance: float = DEFAULT_TOLERANCE,
    slack_ms: float = DEFAULT_SLACK_MS,
) -> list[Finding]:
    findings: list[Finding] = []
    for name, base in sorted(baseline["endpoints"].items()):
        now = current["endpoints"].get(name)
        if now is None or not now.get("requests"):
            findings.append(Finding(name, "requests", base.get("requests"), 0.0, None, False))
            continue

        q_limit = base["queries_max"]
        findings.append(
            Finding(
                name,
                "queries_max",
                q_limit,
                now["queries_max"],
                q_limit,
                now["queries_max"] <= q_limit,
            )
        )
        p95_limit = round(base["p95_ms"] * (1 + tolerance) + slack_ms, 1)
        findings.append(
            Finding(
                name,
                "p95_ms",
                base["p95_ms"],
                now["p95_ms"],
                p95_limit,
                now["p95_ms"] <= p95_limit,
            )
        )

    err_limit = round(baseline["error_rate"] + ERROR_RATE_SLACK, 4)
    findings.append(
        Finding(
            "(all)",
            "error_rate",
            baseline["error_rate"],
            current["error_rate"],
            err_limit,
            current["error_rate"] <= err_limit,
        )
    )
    return findings


def _num(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:g}" if value == int(value) else f"{value:.2f}"


def to_markdown(findings: list[Finding]) -> str:
    regressions = [f for f in findings if not f.ok]
    title = (
        f"### Performance gate: {len(regressions)} regression(s)"
        if regressions
        else "### Performance gate: no regressions"
    )
    rows = [
        title,
        "",
        "| Endpoint | Metric | Baseline | This run | Limit | Result |",
        "|---|---|---:|---:|---:|---|",
    ]
    for f in findings:
        mark = "✅" if f.ok else "❌"
        rows.append(
            f"| `{f.endpoint}` | {f.metric} | {_num(f.baseline)} | {_num(f.current)} "
            f"| {_num(f.limit)} | {mark} {f.verdict} |"
        )
    return "\n".join(rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("summary", type=Path, help="k6 summary JSON (results/load.json)")
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--update", action="store_true", help="save this run as the baseline")
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    parser.add_argument("--slack-ms", type=float, default=DEFAULT_SLACK_MS)
    parser.add_argument("--markdown", type=Path, help="also write the table here")
    args = parser.parse_args(argv)

    current = extract(json.loads(args.summary.read_text()))
    if not current["endpoints"]:
        print("No per-endpoint data in the k6 summary. Was it a run of load.js?")
        return 2

    if args.update:
        args.baseline.write_text(json.dumps(current, indent=2) + "\n")
        print(f"Saved baseline with {len(current['endpoints'])} endpoints to {args.baseline}")
        return 0

    baseline = json.loads(args.baseline.read_text())
    findings = compare(current, baseline, args.tolerance, args.slack_ms)
    table = to_markdown(findings)
    print(table)
    if args.markdown:
        args.markdown.write_text(table)
    return 0 if all(f.ok for f in findings) else 1


if __name__ == "__main__":
    sys.exit(main())
