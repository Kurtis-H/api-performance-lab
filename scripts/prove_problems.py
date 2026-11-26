"""Prove every performance problem is real and caught by its test.

For each problem, run its test twice:
  1. with the fix in place, where it must pass
  2. with the problem switched on (PERF_BUGS=<name>), where it must fail

A test that passes either way isn't testing anything, so the script exits
non-zero if any problem goes uncaught.

    python scripts/prove_problems.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import KNOWN_BUGS  # noqa: E402


def run_marker(problem: str, bugs: str) -> tuple[int, float]:
    env = {**os.environ, "PERF_BUGS": bugs}
    started = time.perf_counter()
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/api",
            "-m",
            f"catches_{problem}",
            "-q",
            "-p",
            "no:cacheprovider",
            "--no-header",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.perf_counter() - started
    if result.returncode not in (0, 1):
        # 5 = no tests collected, anything else = pytest itself broke.
        print(result.stdout[-2000:], result.stderr[-2000:], sep="\n")
        raise SystemExit(f"pytest could not run the {problem} test (exit {result.returncode})")
    return result.returncode, elapsed


def main() -> int:
    print(f"{'problem':<16} {'fixed':<8} {'broken':<8} verdict")
    print("-" * 48)
    uncaught = []
    for problem in sorted(KNOWN_BUGS):
        fixed_code, _ = run_marker(problem, "")
        broken_code, _ = run_marker(problem, problem)
        fixed = "pass" if fixed_code == 0 else "FAIL"
        broken = "fail" if broken_code == 1 else "PASS"
        ok = fixed_code == 0 and broken_code == 1
        if not ok:
            uncaught.append(problem)
        print(f"{problem:<16} {fixed:<8} {broken:<8} {'caught' if ok else 'NOT CAUGHT'}")

    print()
    if uncaught:
        print(f"Not proven: {', '.join(uncaught)}")
        return 1
    print(f"All {len(KNOWN_BUGS)} problems are real and caught by their tests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
