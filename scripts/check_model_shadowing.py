#!/usr/bin/env python3
"""Detect SQLAlchemy declarative name-shadowing bugs.

Class bodies are ordinary namespaces. A column named ``text`` shadows an
imported ``text`` helper for every later expression in that same class body,
so ``Index(..., postgresql_where=text("..."))`` silently calls the column
instead of SQLAlchemy's ``text()``. That is a runtime failure at import time,
not a type error, and it is easy to reintroduce.

This checker runs on the AST, so it needs no dependencies installed.

It reports three categories:

  SHADOW-CALL   a name is bound as a column and then *called* later in the
                same class body. Always a bug.
  RESERVED      a column name collides with a Declarative reserved attribute
                (metadata, registry, ...). Always a bug.
  ANNOTATION    a column name collides with a module-level import that is also
                used inside a ``Mapped[...]`` annotation in that class.
                SQLAlchemy de-stringifies annotations at mapper-configuration
                time, so this is at best fragile.

    python3 scripts/check_model_shadowing.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "packages" / "core" / "models"

# Attribute names SQLAlchemy's DeclarativeBase owns. Using them as columns
# breaks the mapper outright.
RESERVED_ATTRS = {"metadata", "registry", "__table__", "__mapper__"}

findings: list[tuple[str, str, str]] = []


def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            names.update(a.asname or a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _annotation_names(node: ast.AST) -> set[str]:
    """Names referenced inside an annotation, including string annotations."""
    found: set[str] = set()
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            found |= _annotation_names(ast.parse(node.value, mode="eval").body)
        except SyntaxError:
            return found
        return found
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            found.add(sub.id)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            try:
                found |= _annotation_names(ast.parse(sub.value, mode="eval").body)
            except SyntaxError:
                continue
    return found


def check_module(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module_names = _module_level_names(tree)
    rel = path.relative_to(ROOT)

    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        # Column names, in the order they are bound in the class body.
        bound: list[tuple[str, int]] = []
        annotation_refs: set[str] = set()

        for stmt in cls.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                bound.append((stmt.target.id, stmt.lineno))
                annotation_refs |= _annotation_names(stmt.annotation)
            elif isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        bound.append((target.id, stmt.lineno))

        bound_names = {name for name, _ in bound}

        # RESERVED
        for name, lineno in bound:
            if name in RESERVED_ATTRS:
                findings.append(
                    ("RESERVED", f"{rel}:{lineno}",
                     f"{cls.name}.{name} collides with a Declarative reserved attribute")
                )

        # SHADOW-CALL: a bound name later called within the same class body.
        first_bind = {}
        for name, lineno in bound:
            first_bind.setdefault(name, lineno)

        for node in ast.walk(cls):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            called = node.func.id
            if called in bound_names and node.lineno > first_bind[called]:
                findings.append(
                    ("SHADOW-CALL", f"{rel}:{node.lineno}",
                     f"{cls.name}.{called} is a column but is called as {called}(...) "
                     f"on line {node.lineno}; use an alias import instead")
                )

        # ANNOTATION: column name equals a module import used in annotations.
        for name, lineno in bound:
            if name in module_names and name in annotation_refs:
                findings.append(
                    ("ANNOTATION", f"{rel}:{lineno}",
                     f"{cls.name}.{name} shadows the module-level import `{name}`, "
                     f"which is also used inside a Mapped[...] annotation in this class")
                )


def main() -> int:
    if not MODELS_DIR.is_dir():
        print(f"no models directory at {MODELS_DIR}")
        return 1

    modules = sorted(MODELS_DIR.glob("*.py"))
    for path in modules:
        check_module(path)

    print(f"\nSQLAlchemy name-shadowing audit ({len(modules)} model modules)")
    print("=" * 78)
    if not findings:
        print("  no shadowing issues found")
        print("=" * 78 + "\n")
        return 0

    for category, location, detail in findings:
        print(f"  [{category:<12}] {location}\n                 {detail}")
    print("=" * 78)
    print(f"{len(findings)} issue(s) found\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
