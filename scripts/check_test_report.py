"""Reject empty, failing or skipped pytest JUnit reports in required CI suites."""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree


def check_report(path: Path) -> int:
    try:
        # Reports are generated locally by pytest, never from external input.
        root = ElementTree.parse(path).getroot()  # noqa: S314
    except (OSError, ElementTree.ParseError) as exc:
        print(f"Invalid test report: {exc}", file=sys.stderr)
        return 1
    cases = list(root.iter("testcase"))
    rejected = sum(
        any(case.find(tag) is not None for tag in ("skipped", "failure", "error")) for case in cases
    )
    if (
        not cases
        or rejected
        or next(root.iter("error"), None) is not None
        or next(root.iter("failure"), None) is not None
    ):
        print(f"Rejected {path}: {len(cases)} tests, {rejected} non-passing", file=sys.stderr)
        return 1
    print(f"Verified {path}: {len(cases)} passed, none skipped")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/check_test_report.py REPORT.xml", file=sys.stderr)
        sys.exit(2)
    sys.exit(check_report(Path(sys.argv[1])))
