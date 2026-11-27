"""Prove the consumer-contract test catches API changes that would break the apps,
and lets through the ones that wouldn't.

Each case edits a temporary copy of the repo, then runs
tests/api/test_consumer_contracts.py in that copy:

  breaking   a change an app would notice: the contract test must FAIL
  safe       a change no app depends on: the contract test must PASS

A contract test that fails on everything is as useless as one that passes on
everything, so both directions are checked.

    python scripts/prove_contracts.py     # needs the consumer pacts in consumers/
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (name, expected result, file, old text, new text)
CASES = [
    (
        "an event field the apps read is renamed",
        "breaking",
        [
            ("app/schemas.py", "    available_seats: int", "    seats_left: int"),
            ("app/main.py", "available_seats=r.available", "seats_left=r.available"),
            ("app/main.py", "available_seats=available", "seats_left=available"),
        ],
    ),
    (
        "a sold seat answers 400 instead of 409",
        "breaking",
        [
            (
                "app/main.py",
                'HTTPException(409, "One or more seats are already taken")',
                'HTTPException(400, "One or more seats are already taken")',
            )
        ],
    ),
    (
        "another buyer's order answers 403 instead of 404",
        "breaking",
        [
            (
                "app/main.py",
                'HTTPException(404, "Order not found")',
                'HTTPException(403, "Order not found")',
            )
        ],
    ),
    (
        "a stats field no app uses is renamed",
        "safe",
        [
            ("app/schemas.py", "    seats_total: int", "    total_seats: int"),
            ("app/main.py", "seats_total=row.total", "total_seats=row.total"),
        ],
    ),
]


IGNORE = shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.egg-info", "results")


def run_contract_test(repo_copy: Path) -> bool:
    """True if the contract test passes in this copy of the repo.

    It runs inside the copy because pytest puts the rootdir first on the import
    path: run from the real repo, it would import the real, unedited app.
    """
    env = {**os.environ, "REQUIRE_PACTS": "1", "PACT_DIR": str(repo_copy / "consumers")}
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/api/test_consumer_contracts.py",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=repo_copy,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def main() -> int:
    wrong = 0
    with tempfile.TemporaryDirectory() as tmp:
        baseline = Path(tmp) / "baseline"
        shutil.copytree(ROOT, baseline, ignore=IGNORE)
        if not run_contract_test(baseline):
            print("The contract test fails against the unchanged API; fix that first.")
            return 1
        print("unchanged API: contract holds")
        for i, (name, expected, edits) in enumerate(CASES):
            copy = Path(tmp) / f"case{i}"
            shutil.copytree(ROOT, copy, ignore=IGNORE)
            for file, old, new in edits:
                path = copy / file
                text = path.read_text()
                if text.count(old) != 1:
                    print(f"case {name!r}: {old!r} must appear exactly once in {file}")
                    return 1
                path.write_text(text.replace(old, new))
            passed = run_contract_test(copy)
            right = passed if expected == "safe" else not passed
            verdict = "ok   " if right else "WRONG"
            outcome = "contract holds" if passed else "contract broken"
            print(f"{verdict} {expected:8} {name}: {outcome}")
            wrong += not right
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
