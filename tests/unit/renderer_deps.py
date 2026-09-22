"""Renderer dependencies are required, not optional.

`pytest.importorskip` is the right tool for a genuinely optional extra. It is
the wrong tool here, and using it was a real defect: python-docx and ReportLab
are declared runtime dependencies of the API and the render worker, so their
absence is a broken environment, not a reason to hold an opinion in reserve.

The failure mode it produced is the dangerous kind. `make test-unit` would
print a green summary while every DOCX and PDF assertion - determinism, ATS
extraction, anti-fabrication - quietly skipped, so the suite would report
success about documents it had never rendered. A red run that says "install
your dependencies" is strictly better than a green run that proves nothing.

Missing dependency therefore fails at collection, loudly, naming the package
and the manifest that declares it.
"""

from __future__ import annotations

import importlib


class MissingRendererDependencyError(RuntimeError):
    """A declared JobForge dependency is absent from this environment."""


def require_dependency(module: str, package: str, manifest: str) -> None:
    """Import `module` or fail the run.

    Deliberately not `pytest.importorskip`, and deliberately not caught
    anywhere: raising during collection turns the whole renderer suite red,
    which is the intended signal.
    """
    try:
        importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - exercised by a broken env
        raise MissingRendererDependencyError(
            f"{package} is not installed, so M8 renderer verification cannot "
            f"run. It is declared in {manifest}; install it with "
            f"`pip install -r {manifest}`. These tests must not be skipped: a "
            "green suite that never rendered a document proves nothing about "
            "the file a candidate would send to an employer."
        ) from exc
