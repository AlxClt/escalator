"""Schema card: the one rendering of a database's tables, shown in the prompt and by `get_schema`.

Deterministic: tables sorted by name, columns in declaration order, descriptions from the effective
BIRD/Arcwise overlay. Every lookup goes through `Sandbox.introspect`, never through agent SQL.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from pathlib import Path

from escalator.datasets.descriptions import effective_descriptions
from escalator.env.sandbox import Sandbox

_TABLES_SQL = "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
_COLUMNS_SQL = "SELECT name, type, pk FROM pragma_table_info(?) ORDER BY cid"
_FKS_SQL = 'SELECT "from", "table", "to" FROM pragma_foreign_key_list(?) ORDER BY id, seq'


class UnknownTable(LookupError):
    def __init__(self, names: Sequence[str]) -> None:
        super().__init__(f"unknown table: {', '.join(names)}")
        self.names = list(names)


def _decode(raw: bytes) -> str:
    # Three effective description files are cp1252 (data/audit/descriptions_encoding.json).
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252")


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _column_descriptions(path: Path) -> dict[str, dict[str, str]]:
    """Description fields per column, keyed by case-folded original column name."""
    reader = csv.DictReader(io.StringIO(_decode(path.read_bytes())))
    reader.fieldnames = [h.strip().casefold() for h in reader.fieldnames or ()]
    out: dict[str, dict[str, str]] = {}
    for row in reader:
        key = _clean(row.get("original_column_name")).casefold()
        if key:
            out.setdefault(key, {k: _clean(v) for k, v in row.items() if isinstance(v, str)})
    return out


def _column_line(name: str, type_: str, pk: bool, fk: str | None, desc: dict[str, str] | None) -> str:
    head = " ".join(p for p in (name, type_, "PK" if pk else "", f"FK→{fk}" if fk else "") if p)
    if not desc:
        return head
    expanded = desc.get("column_name", "")
    text = desc.get("column_description", "")
    values = desc.get("value_description", "")
    body = text
    if expanded and expanded.casefold() != name.casefold():
        body = f"{expanded}: {text}" if text else expanded
    if values:
        body = f"{body}. Values: {values}" if body else f"Values: {values}"
    return f"{head} — {body}" if body else head


def table_names(sandbox: Sandbox, db_id: str) -> list[str]:
    names = [str(r[0]) for r in sandbox.introspect(db_id, _TABLES_SQL)]
    return sorted(names, key=lambda n: (n.casefold(), n))


def render_card(sandbox: Sandbox, db_id: str, tables: Sequence[str] | None = None) -> str:
    """Full card, or only `tables` (exact names, canonical order). Raises UnknownTable."""
    names = table_names(sandbox, db_id)
    if tables is not None:
        wanted = set(tables)
        unknown = sorted(wanted - set(names))
        if unknown:
            raise UnknownTable(unknown)
        names = [n for n in names if n in wanted]
    files = effective_descriptions(db_id, root=sandbox.db_root)
    blocks: list[str] = []
    for table in names:
        path = files.get(table.casefold())
        descs = _column_descriptions(path) if path is not None else {}
        fks: dict[str, str] = {}
        for col, ref_table, ref_col in sandbox.introspect(db_id, _FKS_SQL, (table,)):
            fks.setdefault(str(col), f"{ref_table}.{ref_col}" if ref_col is not None else str(ref_table))
        lines = [f"## {table}"]
        for name, type_, pk in sandbox.introspect(db_id, _COLUMNS_SQL, (table,)):
            col = str(name)
            lines.append(_column_line(col, str(type_ or ""), bool(pk), fks.get(col), descs.get(col.casefold())))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
