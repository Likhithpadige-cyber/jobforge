"""Static guards on the M8 renderer dependency declaration.

The defect these exist to prevent has already happened once. The DOCX and
PDF renderers were written, measured and reported as verified while
python-docx and ReportLab were declared in no manifest at all: they happened
to be installed in the environment that wrote them. A clean checkout could
not have installed them, the production image would not have contained them,
and the test suite hid it by skipping every renderer assertion and printing
a green summary.

Two properties are asserted here, both offline and both cheap:

* the manifests declare the renderer dependencies, so a clean environment
  installs them;
* the renderer tests refuse rather than skip when they are absent, so a
  broken environment cannot masquerade as a passing one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = ROOT / "requirements.txt"
REQUIREMENTS_DEV = ROOT / "requirements-dev.txt"

pytestmark = pytest.mark.unit

#: Runtime, because the API serves artifacts and the render worker makes them.
RUNTIME_PACKAGES = ("python-docx", "reportlab")
#: Test-only: two independent extractors used to check what an ATS would read.
DEV_PACKAGES = ("pypdf", "pdfminer.six")

RENDERER_TESTS = (
    "test_render_docx.py",
    "test_render_pdf.py",
    "test_render_determinism.py",
    "test_render_no_fabrication.py",
)


def _pin(manifest: Path, package: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(package)}==(\S+)\s*$", re.MULTILINE)
    match = pattern.search(manifest.read_text())
    return match.group(1) if match else None


@pytest.mark.parametrize("package", RUNTIME_PACKAGES)
def test_the_renderer_libraries_are_declared_as_runtime_dependencies(package: str) -> None:
    """A production image is built from requirements.txt alone.

    If these move to requirements-dev.txt the API and the render worker ship
    without them and fail at the first render, in front of a user.
    """
    assert _pin(REQUIREMENTS, package), (
        f"{package} must be pinned in requirements.txt: the render worker and "
        "the artifact endpoints depend on it at runtime"
    )


@pytest.mark.parametrize("package", DEV_PACKAGES)
def test_the_extraction_libraries_are_declared_as_dev_dependencies(package: str) -> None:
    assert _pin(REQUIREMENTS_DEV, package), (
        f"{package} must be pinned in requirements-dev.txt: CI installs that "
        "file and the ATS extraction assertions import it"
    )


@pytest.mark.parametrize("package", RUNTIME_PACKAGES + DEV_PACKAGES)
def test_every_renderer_dependency_is_pinned_exactly(package: str) -> None:
    """The repository pins with `==` throughout.

    Determinism was measured against specific builds of these libraries. A
    range would let a resume's bytes change without a single test changing.
    """
    manifest = REQUIREMENTS if package in RUNTIME_PACKAGES else REQUIREMENTS_DEV
    assert _pin(manifest, package) is not None


@pytest.mark.parametrize("name", RENDERER_TESTS)
def test_renderer_tests_refuse_rather_than_skip_on_a_missing_dependency(name: str) -> None:
    """`importorskip` here would restore the silent-skip failure exactly."""
    source = (ROOT / "tests" / "unit" / name).read_text()
    assert "importorskip" not in source, (
        f"{name} must not use pytest.importorskip: a missing renderer "
        "dependency has to fail the run, not shrink it"
    )
    assert "require_dependency(" in source, (
        f"{name} must declare its renderer dependencies via require_dependency"
    )
