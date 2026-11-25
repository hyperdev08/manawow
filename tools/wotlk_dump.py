"""Read the MySQL dumps published by the CMaNGOS/TrinityCore projects.

The 3.3.5 world databases are distributed as plain ``.sql`` dumps that use the
old MySQL style ``insert  into `table`(`col`,...) values (...),(...);`` (note the
two spaces and the column list).  Parsing them is not hard, but the values
contain commas, escaped quotes and semicolons, so a regular expression is not
enough.  This module implements a small tokenizer for the tuple list and
returns each row as a ``dict`` keyed by column name.

It is deliberately dependency-free: the worldserver should be able to rebuild
its NPC tables on a machine that only has Python and SQLite.
"""
from __future__ import annotations

import gzip
import re
from typing import Dict, Iterator, List, Optional, Sequence


_INSERT_RE_CACHE: Dict[str, re.Pattern[str]] = {}


def _insert_pattern(table: str) -> re.Pattern[str]:
    pattern = _INSERT_RE_CACHE.get(table)
    if pattern is None:
        pattern = re.compile(
            r"insert\s+into\s+`" + re.escape(table) + r"`\s*\(([^)]*)\)\s*values\s*",
            re.IGNORECASE,
        )
        _INSERT_RE_CACHE[table] = pattern
    return pattern


def _scan_tuple_list(text: str, start: int) -> tuple[str, int]:
    """Return ``(raw_tuple_list, end_pos)`` starting right after ``values``."""
    pos = start
    in_string = False
    escaped = False
    depth = 0
    while pos < len(text):
        char = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "'":
                in_string = False
        elif char == "'":
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == ";" and depth == 0:
            return text[start:pos], pos
        pos += 1
    return text[start:pos], pos


def iter_inserts(text: str, table: str) -> Iterator[tuple[List[str], str]]:
    """Yield ``(columns, raw_tuple_list)`` for every insert into ``table``.

    Big CMaNGOS tables (``spell_template``, ``item_template``, ``creature``)
    are split into several ``insert into ... values (...)`` statements so a
    single statement stays below MySQL's packet limits.  Callers that only look
    at the first statement silently lose most of the rows, so :func:`rows`
    aggregates all of them.
    """
    pattern = _insert_pattern(table)
    search_from = 0
    while True:
        match = pattern.search(text, search_from)
        if match is None:
            return
        columns = [column.strip().strip("`") for column in match.group(1).split(",")]
        raw, end = _scan_tuple_list(text, match.end())
        yield columns, raw
        search_from = end + 1


def find_insert(text: str, table: str) -> tuple[Optional[List[str]], Optional[str]]:
    """Return ``(columns, raw_tuple_list)`` for the first insert into ``table``."""
    for columns, raw in iter_inserts(text, table):
        return columns, raw
    return None, None


# MySQL backslash escapes used by the world dumps.
_ESCAPES = {
    "0": "\0",
    "b": "\b",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "Z": "\x1a",
    "\\": "\\",
    "'": "'",
    '"': '"',
}


def parse_tuples(raw: str) -> Iterator[List[str]]:
    """Yield one list of raw field strings per ``(...)`` tuple in ``raw``."""
    index = 0
    length = len(raw)
    while index < length:
        while index < length and raw[index] in " \t\r\n,":
            index += 1
        if index >= length:
            return
        if raw[index] != "(":
            raise ValueError(f"expected '(' at {index}: {raw[index:index + 40]!r}")
        index += 1
        fields: List[str] = []
        current: List[str] = []
        in_string = False
        escaped = False
        while index < length:
            char = raw[index]
            if in_string:
                if escaped:
                    current.append(_ESCAPES.get(char, char))
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == "'":
                    in_string = False
                else:
                    current.append(char)
            elif char == "'":
                in_string = True
            elif char == ",":
                fields.append("".join(current).strip())
                current = []
            elif char == ")":
                fields.append("".join(current).strip())
                index += 1
                break
            else:
                current.append(char)
            index += 1
        else:
            raise ValueError("unterminated tuple in insert statement")
        yield fields


def convert(raw: str):
    """Turn one raw SQL field into ``None``, ``int``, ``float`` or ``str``."""
    if raw == "NULL" or raw == "":
        return None
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    try:
        if "." in raw or "e" in raw.lower():
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def read_dump(path: str) -> str:
    """Read a plain SQL dump or the gzip-compressed CMaNGOS release asset."""
    with open(path, "rb") as probe:
        compressed = probe.read(2) == b"\x1f\x8b"
    if compressed:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def rows(text: str, table: str) -> tuple[Optional[List[str]], List[dict]]:
    """Return ``(columns, row_dicts)`` for every insert into ``table``.

    A table may be split across several ``insert`` statements; all of them are
    read so a multi-chunk dump does not silently lose rows.
    """
    columns: Optional[List[str]] = None
    parsed: List[dict] = []
    for chunk_columns, raw in iter_inserts(text, table):
        if columns is None:
            columns = chunk_columns
        for fields in parse_tuples(raw):
            values = [convert(field) for field in fields]
            if chunk_columns:
                parsed.append(dict(zip(chunk_columns, values)))
            else:
                parsed.append({str(i): value for i, value in enumerate(values)})
    return columns, parsed


def int_field(row: dict, name: str, default: int = 0) -> int:
    value = row.get(name, default)
    return default if value is None else int(value)


def str_field(row: dict, name: str, default: str = "") -> str:
    value = row.get(name, default)
    return default if value is None else str(value)


def float_field(row: dict, name: str, default: float = 0.0) -> float:
    value = row.get(name, default)
    return default if value is None else float(value)
