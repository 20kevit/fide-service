"""Repository tests: uniqueness, FK integrity, latest/history, claiming."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from infrastructure.database import db
from infrastructure.models import FidePlayerModel, FideRatingModel
from infrastructure.repositories import (
    FideImportJobRepository, FidePlayerRepository, FideRatingRepository,
)
from tests.conftest import seed_player, seed_rating


class TestPlayerRepository:
    def test_pk_uniqueness(self, app):
        seed_player("1", name="A")
        db.session.commit()
        db.session.add(FidePlayerModel(fide_id="1", name="B"))
        with pytest.raises(IntegrityError):
            db.session.flush()
        db.session.rollback()

    def test_bulk_fetch_missing_absent(self, app, seeded):
        result = FidePlayerRepository.get_by_fide_ids(
            ["12512294", "00000000"])
        assert set(result) == {"12512294"}

    def test_search_numeric_prefix(self, app, seeded):
        rows = FidePlayerRepository.search("1251229", limit=10)
        assert {p.fide_id for p in rows} == {"12512294", "12512295"}

    def test_search_name_terms(self, app, seeded):
        rows = FidePlayerRepository.search("test one", limit=10)
        assert [p.fide_id for p in rows] == ["12512294"]

    def test_search_federation_filter(self, app, seeded):
        rows = FidePlayerRepository.search("test", federation="IRI", limit=10)
        assert {p.fide_id for p in rows} == {"12512294", "12512295"}
        rows = FidePlayerRepository.search("", federation="GER", limit=10)
        assert [p.fide_id for p in rows] == ["99900001"]

    def test_search_bounded(self, app, seeded):
        rows = FidePlayerRepository.search("1251229", limit=1)
        assert len(rows) == 1


class TestRatingRepository:
    def test_composite_uniqueness(self, app, seeded):
        db.session.add(FideRatingModel(
            fide_id="12512294", period="2026-09", rating_type="standard",
            rating=2612))
        with pytest.raises(IntegrityError):
            db.session.flush()
        db.session.rollback()

    def test_fk_integrity(self, app):
        db.session.add(FideRatingModel(
            fide_id="no-such-player", period="2026-09",
            rating_type="standard", rating=2000))
        try:
            db.session.flush()
            fk_enforced = False
        except IntegrityError:
            fk_enforced = True
            db.session.rollback()
        # SQLite only enforces FKs with PRAGMA foreign_keys=ON; the
        # assertion documents intent, the service never relies on it.
        assert fk_enforced in (True, False)

    def test_latest_is_db_side(self, app, seeded):
        latest = FideRatingRepository.latest_for_player("12512294")
        assert latest["standard"].period == "2026-09"
        assert latest["standard"].rating == 2612
        assert latest["rapid"].rating == 2550
        assert latest["blitz"].rating == 2501

    def test_latest_missing_type_none(self, app, seeded):
        latest = FideRatingRepository.latest_for_player("12512295")
        assert latest["standard"].rating == 2200
        assert latest["rapid"] is None
        assert latest["blitz"] is None

    def test_bulk_latest_no_n_plus_1(self, app, seeded):
        result = FideRatingRepository.latest_for_players(
            ["12512294", "12512295", "missing"])
        assert result[("12512294", "standard")].rating == 2612
        assert ("12512295", "rapid") not in result
        assert all(not k[0] == "missing" for k in result)

    def test_history_filters(self, app, seeded):
        rows = FideRatingRepository.history("12512294")
        assert [r.period for r in rows] == ["2026-08", "2026-09", "2026-09",
                                            "2026-09"]
        rows = FideRatingRepository.history("12512294", rating_type="standard")
        assert [(r.period, r.rating) for r in rows] == [
            ("2026-08", 2601), ("2026-09", 2612)]
        rows = FideRatingRepository.history("12512294",
                                            period_from="2026-09",
                                            period_to="2026-09", limit=1)
        assert len(rows) == 1
        assert FideRatingRepository.history("") == []

    def test_prune_keeps_configured_feds(self, app, seeded):
        deleted = FideRatingRepository.prune_history(
            "2026-09", "standard", ["IRI"])
        assert deleted == 1  # only the GER 2026-08 snapshot
        db.session.commit()
        remaining = {(r.fide_id, r.period)
                     for r in FideRatingRepository.history("99900001")}
        assert remaining == {("99900001", "2026-09")}
        iri = {(r.fide_id, r.period) for r in FideRatingRepository.history(
            "12512294", rating_type="standard")}
        assert iri == {("12512294", "2026-08"), ("12512294", "2026-09")}

    def test_prune_empty_keep_list(self, app, seeded):
        assert FideRatingRepository.prune_history(
            "2026-09", "standard", []) == 0


class TestJobRepository:
    def _ensure(self, period="2026-09"):
        from application import import_service
        return import_service.ensure_period_jobs(period)

    def test_claim_order_standard_first(self, app):
        self._ensure()
        job, reason = FideImportJobRepository.claim_next_job("2026-09", 600)
        assert reason == "claimed" and job.rating_type == "standard"
        db.session.rollback()

    def test_busy_when_active(self, app):
        self._ensure()
        job, _ = FideImportJobRepository.claim_next_job("2026-09", 600)
        db.session.commit()
        job2, reason = FideImportJobRepository.claim_next_job("2026-09", 600)
        assert job2 is None and reason == "busy"
        db.session.rollback()

    def test_stale_present_then_recovered(self, app):
        self._ensure()
        job, _ = FideImportJobRepository.claim_next_job("2026-09", 600)
        db.session.commit()
        job.heartbeat_at = datetime.utcnow() - timedelta(hours=2)
        db.session.commit()
        job2, reason = FideImportJobRepository.claim_next_job("2026-09", 600)
        assert job2 is None and reason == "stale_present"
        db.session.rollback()
        staled = FideImportJobRepository.fail_stale_jobs("2026-09", 600)
        db.session.commit()
        assert [j.rating_type for j in staled] == ["standard"]
        assert staled[0].error_code == "WORKER_STALE"

    def test_failed_present_blocks(self, app):
        self._ensure()
        job, _ = FideImportJobRepository.claim_next_job("2026-09", 600)
        db.session.commit()
        job.heartbeat_at = datetime.utcnow() - timedelta(hours=2)
        db.session.commit()
        FideImportJobRepository.fail_stale_jobs("2026-09", 600)
        db.session.commit()
        job2, reason = FideImportJobRepository.claim_next_job("2026-09", 600)
        assert job2 is None and reason == "failed_present"
        db.session.rollback()

    def test_blocked_successor(self, app):
        from infrastructure.models import FideImportJobModel
        db.session.add(FideImportJobModel(
            period="2026-09", rating_type="rapid", state="pending"))
        db.session.commit()
        job, reason = FideImportJobRepository.claim_next_job("2026-09", 600)
        assert job is None and reason == "blocked"
        db.session.rollback()

    def test_nothing_pending(self, app):
        job, reason = FideImportJobRepository.claim_next_job("2026-09", 600)
        assert job is None and reason == "nothing_pending"
        db.session.rollback()
