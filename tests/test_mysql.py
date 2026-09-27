"""MySQL-gated verification: transactions, locking, concurrency, plans.

Runs ONLY when FIDE_TEST_MYSQL_URL is set (dedicated LOCAL rehearsal
database — never Beta/Production/Swiss). The default suite stays
SQLite-fast; this file proves production-concurrency behavior:

- commit / rollback / nested (savepoint) transactions
- exactly-once job claiming under two racing workers (FOR UPDATE)
- stale-job recovery on MySQL
- full small-pipeline tick on MySQL
- EXPLAIN plans for every hot query (no silent full scans)
"""
import os
import threading
import time

import pytest
from sqlalchemy import text

MYSQL_URL = os.environ.get("FIDE_TEST_MYSQL_URL", "")
needs_mysql = pytest.mark.skipif(
    not MYSQL_URL, reason="FIDE_TEST_MYSQL_URL not set")


@pytest.fixture()
def mysql_app(tmp_path):
    from config import Settings
    from infrastructure import migrations
    from infrastructure.database import db
    from interfaces.factory import create_app

    data_dir = tmp_path / "mysql-data"
    data_dir.mkdir(exist_ok=True)
    settings = Settings(
        database_url=MYSQL_URL, api_key="k", admin_key="a",
        secret_key="s", data_directory=str(data_dir),
        rate_limit_per_minute=0, history_federations=["IRI"])
    app = create_app(settings)
    app.config["TESTING"] = True
    with app.app_context():
        migrations.upgrade(db.engine)
        # Isolate: empty data tables, keep schema_migrations history.
        db.session.execute(text("DELETE FROM fide_ratings"))
        db.session.execute(text("DELETE FROM fide_import_jobs"))
        db.session.execute(text("DELETE FROM fide_players"))
        db.session.commit()
        yield app
        db.session.remove()


def _lock_timeout(seconds=15):
    from infrastructure.database import db
    db.session.execute(
        text("SET SESSION innodb_lock_wait_timeout = %d" % seconds))


class TestMySQLTransactions:
    @needs_mysql
    def test_commit_and_rollback(self, mysql_app):
        from infrastructure.database import db
        from infrastructure.models import FidePlayerModel
        from infrastructure.repositories import FidePlayerRepository
        with mysql_app.app_context():
            db.session.add(FidePlayerModel(fide_id="T1", name="Tmp"))
            db.session.rollback()
            assert FidePlayerRepository.get_by_fide_id("T1") is None
            db.session.add(FidePlayerModel(fide_id="T1", name="Tmp"))
            db.session.commit()
            assert FidePlayerRepository.get_by_fide_id("T1").name == "Tmp"

    @needs_mysql
    def test_nested_savepoint(self, mysql_app):
        from infrastructure.database import db
        from infrastructure.repositories import FidePlayerRepository
        with mysql_app.app_context():
            from infrastructure.models import FidePlayerModel
            db.session.add(FidePlayerModel(fide_id="T2", name="Outer"))
            try:
                with db.session.begin_nested():
                    db.session.add(FidePlayerModel(fide_id="T2", name="Dup"))
                    db.session.flush()  # PK conflict -> savepoint rolls back
            except Exception:
                pass
            db.session.commit()
            assert FidePlayerRepository.get_by_fide_id("T2").name == "Outer"


class TestMySQLClaiming:
    @needs_mysql
    def test_concurrent_claim_single_owner(self, mysql_app):
        """Two workers race for one step: exactly one owns it (FOR UPDATE)."""
        from application import import_service
        period = "2031-05"
        with mysql_app.app_context():
            import_service.ensure_period_jobs(period)
        barrier = threading.Barrier(2)
        outcomes = {}

        def worker(name):
            try:
                with mysql_app.app_context():
                    from infrastructure.database import db
                    from infrastructure.repositories import (
                        FideImportJobRepository)
                    _lock_timeout()
                    barrier.wait(timeout=60)
                    job, reason = FideImportJobRepository.claim_next_job(
                        period, 600)
                    if job is None:
                        db.session.rollback()
                        outcomes[name] = reason
                    else:
                        time.sleep(1.0)  # hold the row lock while "working"
                        db.session.commit()
                        outcomes[name] = "claimed:" + job.rating_type
            except Exception as exc:  # never hang the join silently
                outcomes[name] = "EXC %s: %s" % (
                    type(exc).__name__, str(exc)[:160])

        threads = [threading.Thread(target=worker, args=(n,))
                   for n in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert sorted(outcomes.values()) == ["busy", "claimed:standard"], \
            outcomes

    @needs_mysql
    def test_stale_recovery_on_mysql(self, mysql_app, fixture_path=None):
        import zipfile
        from datetime import datetime, timedelta
        from application import import_service
        from application.import_service import JobRunnerConfig
        from infrastructure.database import db
        from infrastructure.repositories import (
            FideImportJobRepository, FidePlayerRepository)
        period = "2031-06"
        with mysql_app.app_context():
            settings = mysql_app.config["FIDE_SETTINGS"]

            def fake_download(url, dest, **kw):
                import os as _os
                tests_dir = _os.path.dirname(_os.path.abspath(__file__))
                xml = open(_os.path.join(
                    tests_dir, "fixtures", "small_standard.xml"), "rb").read()
                _os.makedirs(_os.path.dirname(dest), exist_ok=True)
                with zipfile.ZipFile(dest, "w") as zf:
                    zf.writestr("players_list.xml", xml)
                return _os.path.getsize(dest)

            cfg = JobRunnerConfig(
                data_dir=settings.data_directory,
                base_url="http://localhost/", batch_size=2,
                history_federations=["IRI"],
                download_fn=fake_download,
                verify_url_fn=lambda u: {"ok": True, "status": 200,
                                         "detail": "OK"})
            import_service.ensure_period_jobs(period)
            job, _ = FideImportJobRepository.claim_next_job(period, 600)
            db.session.commit()
            job.heartbeat_at = datetime.utcnow() - timedelta(hours=3)
            db.session.commit()
            result = import_service.tick(period, cfg)
            assert result["status"] == "idle"
            assert result["reason"] == "failed_present"
            assert result["staled"] == ["standard"]
            retried = import_service.retry_job(period, "standard")
            assert retried["state"] == "pending"
            result = import_service.tick(period, cfg)
            assert result["status"] == "success", result
            assert FidePlayerRepository.count() == 4


class TestMySQLPlans:
    """EXPLAIN every hot query. Indexed access required, except the
    documented substring name search (leading wildcard cannot use a
    B-tree; fide_id-prefix and federation filters stay indexed)."""

    @needs_mysql
    def test_plans(self, mysql_app):
        from infrastructure.database import db
        from tests.conftest import seed_player, seed_rating
        with mysql_app.app_context():
            seed_player("12512294", name="Alpha, Test One", federation="IRI")
            seed_rating("12512294", "2026-09", "standard", 2612)
            seed_rating("12512294", "2026-08", "standard", 2601)
            db.session.commit()

            cases = {
                "player_by_pk":
                    ("SELECT * FROM fide_players WHERE fide_id = '12512294'",
                     True),
                "rating_by_uq":
                    ("SELECT * FROM fide_ratings WHERE fide_id = '12512294' "
                     "AND period = '2026-09' AND rating_type = 'standard'",
                     True),
                "history_range":
                    ("SELECT * FROM fide_ratings WHERE fide_id = '12512294' "
                     "ORDER BY period ASC",
                     True),
                "latest_group":
                    ("SELECT fide_id, rating_type, MAX(period) FROM "
                     "fide_ratings WHERE fide_id IN ('12512294') "
                     "GROUP BY fide_id, rating_type",
                     True),
                "batch_in":
                    ("SELECT * FROM fide_players WHERE fide_id "
                     "IN ('12512294','00000000')",
                     True),
                "job_claim":
                    ("SELECT * FROM fide_import_jobs WHERE period = '2026-09'",
                     True),
                "federation_filter":
                    ("SELECT * FROM fide_players WHERE federation = 'IRI' "
                     "LIMIT 20",
                     True),
                "name_substring":
                    ("SELECT * FROM fide_players WHERE name LIKE '%test%' "
                     "LIMIT 20",
                     False),  # documented: leading wildcard => full scan
                "orphan_covering":
                    ("SELECT COUNT(*) FROM fide_ratings LEFT OUTER JOIN "
                     "fide_players ON fide_ratings.fide_id = "
                     "fide_players.fide_id "
                     "WHERE fide_players.fide_id IS NULL",
                     True),  # covered by the UQ composite, no single-col idx
            }
            summary = {}
            for name, (sql, indexed) in cases.items():
                rows = db.session.execute(text("EXPLAIN " + sql)).mappings().all()
                keys = {(r.get("key"), r.get("type")) for r in rows}
                summary[name] = sorted("%s/%s" % k for k in keys)
                if indexed:
                    assert all(k[0] for k in keys), (name, summary[name])
            print("\nMySQL EXPLAIN summary: %s" % summary)


class TestMySQLMigrations:
    """Forward migration chain on a disposable MySQL database."""

    @needs_mysql
    def test_chain_applies_and_v0003_drops_legacy_index(self, mysql_app):
        import importlib
        from infrastructure import migrations
        from infrastructure.database import db
        with mysql_app.app_context():
            assert migrations.pending_migrations(db.engine) == []
            assert "0003_drop_redundant_rating_index" in (
                migrations.applied_versions(db.engine))
            module = importlib.import_module(
                "migrations.versions.v0003_drop_redundant_rating_index")
            # Simulate a pre-0003 schema, then upgrade twice.
            db.session.execute(text(
                "CREATE INDEX ix_fide_ratings_fide_id "
                "ON fide_ratings (fide_id)"))
            module.upgrade(db.engine)
            module.upgrade(db.engine)  # idempotent no-op
            leftovers = db.session.execute(text(
                "SHOW INDEX FROM fide_ratings "
                "WHERE Key_name = 'ix_fide_ratings_fide_id'")).all()
            assert leftovers == []
            assert migrations.pending_migrations(db.engine) == []
