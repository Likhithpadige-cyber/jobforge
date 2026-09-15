"""Split a SQL script into individually executable statements.

Migrations in this repository are hand-written SQL files executed from a thin
Alembic wrapper. Handing a whole multi-statement file to one `op.execute()`
works only if the driver uses PostgreSQL's *simple* query protocol; asyncpg
uses the *extended* protocol, where the server parses one statement per Parse
message and rejects the rest with

    cannot insert multiple commands into a prepared statement

So the file is split here and executed statement by statement, on the same
connection and therefore inside the same migration transaction: transactional
DDL is preserved exactly, and so is the ordering the file expresses.

Splitting SQL on ";" is wrong in a way that is quiet rather than loud, which
is why this is a scanner rather than a `str.split`. A semicolon means
"end of statement" only when it appears in none of:

* a single-quoted literal, where '' is an escaped quote;
* an E'' literal, where a backslash escapes the next character;
* a double-quoted identifier, where "" is an escaped quote;
* a dollar-quoted body - `$$ ... $$` or `$tag$ ... $tag$` - which is how every
  PL/pgSQL function and DO block in these migrations is written, and which
  contains semicolons on almost every line;
* a `--` line comment or a `/* */` block comment, which PostgreSQL nests.

Getting any one of those wrong produces fragments that fail at apply time with
a syntax error pointing at the middle of a function body, which is a memorably
unpleasant thing to debug at deploy o'clock.
"""

from __future__ import annotations

import re

#: A dollar-quote opener: `$$` or `$tag$`, where tag is an identifier.
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def split_sql_statements(sql: str) -> list[str]:
    """Return `sql` as a list of statements, comments and blank runs removed.

    Each returned statement is stripped and carries no trailing semicolon, so
    it can be handed straight to a driver. Order is preserved.
    """
    statements: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(sql)

    while index < length:
        char = sql[index]

        # --- comments -------------------------------------------------
        if sql.startswith("--", index):
            end = sql.find("\n", index)
            index = length if end == -1 else end
            continue
        if sql.startswith("/*", index):
            depth = 1
            index += 2
            while index < length and depth:
                if sql.startswith("/*", index):
                    depth += 1
                    index += 2
                elif sql.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            continue

        # --- dollar-quoted body ---------------------------------------
        if char == "$":
            match = _DOLLAR_TAG.match(sql, index)
            if match:
                closing = match.group(0)
                end = sql.find(closing, match.end())
                if end == -1:
                    # Unterminated: hand the remainder back rather than
                    # inventing a boundary inside a function body.
                    buffer.append(sql[index:])
                    index = length
                    continue
                end += len(closing)
                buffer.append(sql[index:end])
                index = end
                continue

        # --- quoted literals and identifiers --------------------------
        if char in {"'", '"'}:
            escaped = char == "'" and index > 0 and sql[index - 1] in {"e", "E"}
            end = index + 1
            while end < length:
                if escaped and sql[end] == "\\":
                    end += 2
                    continue
                if sql[end] == char:
                    if end + 1 < length and sql[end + 1] == char:
                        end += 2  # '' or "" is an escaped quote
                        continue
                    end += 1
                    break
                end += 1
            buffer.append(sql[index:end])
            index = end
            continue

        # --- statement boundary ---------------------------------------
        if char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            index += 1
            continue

        buffer.append(char)
        index += 1

    trailing = "".join(buffer).strip()
    if trailing:
        statements.append(trailing)
    return statements
