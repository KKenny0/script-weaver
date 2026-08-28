"""Verify that an existing database still matches the schema this code ships.

The expected v1 shape is parsed from the migration itself, so the two can never
drift apart. Any mismatch fails startup with a clear error instead of writing
facts into a database this version cannot reason about.
"""

from __future__ import annotations

import re
import sqlite3

from . import sqlite as _sqlite_module
from .sqlite import SchemaError

_CONSTRAINT_KEYWORDS = {"UNIQUE", "PRIMARY", "FOREIGN", "CHECK", "CONSTRAINT"}
_CREATE_TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\(", re.IGNORECASE)
_ALTER_ADD_COLUMN = re.compile(r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)", re.IGNORECASE)


def expected_tables(migration_version: int = 1) -> dict[str, set[str]]:
    # Read MIGRATIONS_DIR at call time so runtime overrides (tests, embedders) apply.
    files = sorted(_sqlite_module.MIGRATIONS_DIR.glob("*.sql"))
    tables: dict[str, set[str]] = {}
    for file in files:
        version = int(file.name.split("_", 1)[0])
        if version > migration_version:
            continue
        # Migrations accumulate: a later file's ALTERs union into earlier CREATEs.
        for table, columns in _parse_create_tables(file.read_text(encoding="utf-8")).items():
            tables.setdefault(table, set()).update(columns)
    return tables


def _parse_create_tables(script: str) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for match in _ALTER_ADD_COLUMN.finditer(script):
        result.setdefault(match.group(1), set()).add(match.group(2))
    for match in _CREATE_TABLE.finditer(script):
        name = match.group(1)
        depth, start = 1, match.end()
        index = start
        while depth and index < len(script):
            if script[index] == "(":
                depth += 1
            elif script[index] == ")":
                depth -= 1
            index += 1
        body = script[start:index - 1]
        columns = set()
        nested = 0
        parts, current = [], []
        for char in body:
            if char == "(":
                nested += 1
            elif char == ")":
                nested -= 1
            if char == "," and not nested:
                parts.append("".join(current))
                current = []
            else:
                current.append(char)
        parts.append("".join(current))
        for part in parts:
            first = part.strip().split("(")[0].split()
            if not first:
                continue
            keyword = first[0].upper()
            if keyword in _CONSTRAINT_KEYWORDS:
                continue
            columns.add(first[0])
        result[name] = columns
    return result


def verify_shape(connection: sqlite3.Connection, migration_version: int = 1) -> None:
    expected = expected_tables(migration_version)
    problems: list[str] = []
    for table, columns in expected.items():
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            problems.append(f"missing table {table}")
            continue
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        missing = sorted(columns - actual)
        extra = sorted(actual - columns)
        if missing:
            problems.append(f"table {table} missing columns: {missing}")
        if extra:
            problems.append(f"table {table} has unexpected columns: {extra}")
    if problems:
        raise SchemaError("database schema is incompatible with this version: " + "; ".join(problems))


# Backwards-compatible alias used by sqlite.py.
verify_v1_shape = verify_shape
