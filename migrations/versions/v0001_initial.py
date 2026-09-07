"""Migration 0001: initial FIDE schema (fresh database).

Creates fide_players, fide_ratings, fide_import_jobs via the ORM table
metadata so DDL stays portable across SQLite (local dev) and MySQL
(production). Idempotent (checkfirst=True).
"""

DESCRIPTION = "initial FIDE schema: players, ratings, import jobs"


def upgrade(engine) -> None:
    from infrastructure.models import (
        FideImportJobModel, FidePlayerModel, FideRatingModel,
    )
    for model in (FidePlayerModel, FideRatingModel, FideImportJobModel):
        model.__table__.create(bind=engine, checkfirst=True)
