"""FIDE Service ORM models. Independent schema — no Swiss imports.

Tables (exactly three — no legacy mirrors):
  fide_players     identity/master record, fide_id PK (NOT NULL, unique)
  fide_ratings     per-period snapshots, UQ(fide_id, period, rating_type)
  fide_import_jobs persistent work tracker, UQ(period, rating_type)

Design notes vs Swiss:
- birth_year is String(16): the official <birthday> text is preserved
  verbatim instead of being squeezed into String(4).
- Titles are String(64): official otitle/foatitle values can be
  comma-separated lists (e.g. arbiter title sets observed in the wild),
  far beyond the old String(10). Never truncate official data.
- name is String(255) for long official names.
- fide_ratings carries an extra composite lookup index
  (fide_id, rating_type, period) so latest-per-type reads are served by
  the database, never resolved in Python over full histories.
- Job states include "verifying" (post-import consistency check).
"""
from datetime import datetime

from infrastructure.database import db


class FidePlayerModel(db.Model):
    """Official FIDE identity for one player. ALL federations welcome."""

    __tablename__ = "fide_players"

    fide_id = db.Column(db.String(20), primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    sex = db.Column(db.String(1), default="M")
    federation = db.Column(db.String(5), default="", index=True)
    title = db.Column(db.String(64), default="")
    wtitle = db.Column(db.String(64), default="")
    otitle = db.Column(db.String(64), default="")
    foatitle = db.Column(db.String(64), default="")
    birth_year = db.Column(db.String(16), default="")
    inactive = db.Column(db.Boolean, default=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    __table_args__ = (
        db.Index("ix_fide_players_name", "name"),
    )


class FideRatingModel(db.Model):
    """One rating snapshot: (player, period, type)."""

    __tablename__ = "fide_ratings"

    id = db.Column(db.Integer, primary_key=True)
    fide_id = db.Column(db.String(20),
                        db.ForeignKey("fide_players.fide_id"),
                        nullable=False, index=True)
    period = db.Column(db.String(7), nullable=False, index=True)
    rating_type = db.Column(db.String(10), nullable=False)
    rating = db.Column(db.Integer, default=0)
    games = db.Column(db.Integer, default=0)
    k_factor = db.Column(db.Integer, default=20)

    __table_args__ = (
        db.UniqueConstraint("fide_id", "period", "rating_type",
                            name="uq_fide_rating_period_type"),
        db.Index("ix_fide_ratings_lookup", "fide_id", "rating_type", "period"),
    )


class FideImportJobModel(db.Model):
    """One import step: (period, rating_type). Tracks WORK, not data."""

    __tablename__ = "fide_import_jobs"

    id = db.Column(db.Integer, primary_key=True)
    period = db.Column(db.String(7), nullable=False, index=True)
    rating_type = db.Column(db.String(10), nullable=False)
    # pending|downloading|validating|importing|verifying|completed|failed
    state = db.Column(db.String(20), nullable=False, default="pending")
    started_at = db.Column(db.DateTime, nullable=True)
    heartbeat_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    records_processed = db.Column(db.Integer, default=0)
    total_records = db.Column(db.Integer, nullable=True)
    error_code = db.Column(db.String(40), nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    source_url = db.Column(db.String(255), nullable=True)
    file_name = db.Column(db.String(255), nullable=True)

    __table_args__ = (
        db.UniqueConstraint("period", "rating_type",
                            name="uq_fide_job_period_type"),
        db.Index("ix_fide_import_jobs_state", "state"),
    )
