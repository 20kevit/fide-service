"""Migration 0002: widen official-text columns (real-data fix).

The September 2026 official files carry otitle values like
'SI,NI,FI,LSI,FA' (comma-separated title sets) and long names that
exceed the initial String(10)/String(200) widths. MySQL strict mode
rejected them mid-import; SQLite never enforces lengths, which is why
unit tests missed it. Widening is non-destructive (no data rewrite,
no constraint change).

MySQL: ALTER ... MODIFY COLUMN. SQLite: no-op (lengths unenforced).
"""

DESCRIPTION = "widen name/title columns for official data"

_COLUMNS = {
    # Nullability preserved exactly; only the widths change.
    "name": "VARCHAR(255) NOT NULL",
    "title": "VARCHAR(64) NULL DEFAULT ''",
    "wtitle": "VARCHAR(64) NULL DEFAULT ''",
    "otitle": "VARCHAR(64) NULL DEFAULT ''",
    "foatitle": "VARCHAR(64) NULL DEFAULT ''",
}


def upgrade(engine) -> None:
    if engine.dialect.name != "mysql":
        return
    from sqlalchemy import text
    with engine.begin() as conn:
        for column, ddl in _COLUMNS.items():
            conn.execute(text(
                "ALTER TABLE fide_players MODIFY COLUMN %s %s"
                % (column, ddl)))
