#!/usr/bin/env python3
"""Regenerate 0002_core_schema.down.sql from the up migration.

Run this after any change to the up migration so the two cannot drift:

    python3 scripts/gen_down_migration.py

A hand-maintained down migration is the classic place for schema drift to
hide, because nobody runs it until they urgently need it to work.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UP = ROOT / "migrations" / "sql" / "0002_core_schema.up.sql"
DOWN = ROOT / "migrations" / "sql" / "0002_core_schema.down.sql"

HEADER = """\
-- =====================================================================
-- Phase 2 core schema, reversed. Generated from 0002_core_schema.up.sql
-- by scripts/gen_down_migration.py so it cannot drift out of step.
--
-- Tables drop in reverse creation order with CASCADE, which also removes
-- the triggers and indexes attached to them. Extensions are NOT dropped:
-- other schemas in the same database may depend on them.
-- =====================================================================

-- 1. drop the deferred FKs that close the applications <-> resumes cycle
ALTER TABLE IF EXISTS applications
  DROP CONSTRAINT IF EXISTS fk_applications_resume_id_resumes;
ALTER TABLE IF EXISTS applications
  DROP CONSTRAINT IF EXISTS fk_applications_cover_letter_id_cover_letters;

-- 2. tables, reverse dependency order
"""


def build() -> str:
    up = UP.read_text()
    tables = re.findall(r"^CREATE TABLE (\w+)", up, re.MULTILINE)
    enums = re.findall(r"^CREATE TYPE (\w+) AS ENUM", up, re.MULTILINE)
    funcs = sorted(set(re.findall(r"CREATE OR REPLACE FUNCTION (\w+)", up)))

    out = [HEADER]
    out += [f"DROP TABLE IF EXISTS {t} CASCADE;" for t in reversed(tables)]
    out += ["", "-- 3. functions"]
    out += [
        f"DROP FUNCTION IF EXISTS {f}"
        f"({'UUID' if f == 'jobforge_increment_fact_usage' else ''}) CASCADE;"
        for f in funcs
    ]
    out += ["", "-- 4. enum types"]
    out += [f"DROP TYPE IF EXISTS {e};" for e in reversed(enums)]
    out += ["", "-- 5. extensions are deliberately left in place (see header).", ""]
    return "\n".join(out)


def main() -> int:
    generated = build()
    if "--check" in sys.argv:
        if DOWN.read_text() != generated:
            print("down migration is STALE; run: python3 scripts/gen_down_migration.py")
            return 1
        print("down migration is up to date")
        return 0
    DOWN.write_text(generated)
    print(f"wrote {DOWN.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
