"""Migration 0003: drop the redundant fide_id single-column index.

``ix_fide_ratings_fide_id`` duplicated the leftmost prefix of both
composites (``uq_fide_rating_period_type`` and ``ix_fide_ratings_lookup``),
so every rating INSERT paid for an index no query plan needs — verified by
EXPLAIN on all hot queries before removal. Dropping it reduces write
amplification and storage; reads shift to the covering composites.

Idempotent: checks ``information_schema`` first, so fresh databases
(created from current ORM metadata, which no longer declares the index)
and re-runs are safe no-ops. MySQL only: SQLite dev/test databases never
enforce or need this (fresh ones never create it).

Rollback follows repository policy: no downgrades — restore from backup.
Re-adding would be
``ALTER TABLE fide_ratings ADD INDEX ix_fide_ratings_fide_id (fide_id)``.
"""

DESCRIPTION = "drop redundant ix_fide_ratings_fide_id (covered by composites)"

INDEX_NAME = "ix_fide_ratings_fide_id"


def upgrade(engine) -> None:
    if engine.dialect.name != "mysql":
        return
    from sqlalchemy import text
    with engine.begin() as conn:
        exists = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() "
            "AND table_name = 'fide_ratings' "
            "AND index_name = :name"),
            {"name": INDEX_NAME}).scalar()
        if exists:
            conn.execute(text(
                "ALTER TABLE fide_ratings DROP INDEX %s" % INDEX_NAME))
