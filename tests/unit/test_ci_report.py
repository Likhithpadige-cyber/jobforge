"""Required CI suites must fail closed on missing, empty or skipped evidence."""

from pathlib import Path

import pytest

from scripts.check_test_report import check_report

pytestmark = [pytest.mark.unit, pytest.mark.safety]


@pytest.mark.parametrize(
    ("xml", "expected"),
    [
        ('<testsuites><testsuite><testcase name="ok"/></testsuite></testsuites>', 0),
        ('<testsuite><testcase name="ok"/></testsuite>', 0),
        ("<testsuites/>", 1),
        ("<testsuite><testcase><skipped/></testcase></testsuite>", 1),
        ("<testsuite><testcase><failure/></testcase></testsuite>", 1),
        ("<testsuite><testcase><error/></testcase></testsuite>", 1),
        ('<testsuite><error/><testcase name="ok"/></testsuite>', 1),
        ("not XML", 1),
    ],
)
def test_report_gate(tmp_path: Path, xml: str, expected: int) -> None:
    report = tmp_path / "report.xml"
    report.write_text(xml)
    assert check_report(report) == expected


def test_missing_report_is_failure(tmp_path: Path) -> None:
    assert check_report(tmp_path / "missing.xml") == 1
