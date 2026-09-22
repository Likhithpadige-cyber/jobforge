#!/usr/bin/env python3
"""Run the synthetic offline grounding corpus and emit machine-readable metrics."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "evals" / "grounding_cases.json"


def summarize(results: dict[str, bool]) -> dict[str, object]:
    corpus = json.loads(CORPUS.read_text())
    outcomes = {}
    errors = []
    for name, passed in results.items():
        if not passed:
            errors.append(name)
        if name.startswith("test_grounding_case["):
            outcomes[name.removeprefix("test_grounding_case[").removesuffix("]")] = passed
    categories: dict[str, dict[str, int]] = {}
    mechanisms: dict[str, dict[str, int]] = {}
    missing = []
    for case in corpus["cases"]:
        key = f'{case["category"]}.{case["id"]}'
        if key not in outcomes:
            missing.append(key)
        for collection, group in ((categories, case["category"]), (mechanisms, case["kind"])):
            counts = collection.setdefault(group, {"passed": 0, "total": 0})
            counts["total"] += 1
            counts["passed"] += int(outcomes.get(key, False))
    return {
        "suite": "jobforge-offline-grounding", "corpus_version": corpus["version"],
        "corpus_sha256": hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
        "mode": "offline", "provider_calls": 0,
        "prompt_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sorted((ROOT / "packages" / "agents" /
                                              "resume_optimizer" / "prompts").glob("*.txt"))},
        "measurement": (
            "deterministic guard and scripted replay enforcement; not live-model accuracy"
        ),
        "passed": not errors and not missing and len(outcomes) == len(corpus["cases"]),
        "cases": len(corpus["cases"]), "categories": categories, "mechanisms": mechanisms,
        "failed_or_skipped_tests": errors, "missing_cases": missing,
    }


class Reporter:
    def __init__(self) -> None:
        self.results: dict[str, bool] = {}

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        name = report.nodeid.split("::")[-1]
        if report.when == "call" or not report.passed:
            self.results[name] = self.results.get(name, True) and report.passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="optional JSON report destination")
    args = parser.parse_args()
    reporter = Reporter()
    with redirect_stdout(sys.stderr):
        result = pytest.main([str(ROOT / "tests" / "evals"), "-q"], plugins=[reporter])
    report = summarize(reporter.results)
    report["passed"] = bool(report["passed"]) and result == 0
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(serialized)
    print(serialized, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
