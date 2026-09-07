"""Import tests: lifecycle, ordering, stale recovery, rollback, pruning.

Network is never touched: downloads are injected fakes writing tiny
fixture ZIPs; the full September-scale file is never required.
"""
import os
import zipfile

from application import checks, import_service
from application.import_service import JobRunnerConfig
from infrastructure import storage
from infrastructure.database import db
from infrastructure.models import FidePlayerModel, FideRatingModel
from infrastructure.repositories import (
    FideImportJobRepository, FidePlayerRepository, FideRatingRepository,
)

FIXTURES = {"standard": "small_standard.xml", "rapid": "small_rapid.xml",
            "blitz": "small_blitz.xml"}


def make_cfg(app, fixture_path):
    settings = app.config["FIDE_SETTINGS"]

    def fake_download(url, dest, **kw):
        rtype = os.path.basename(dest).replace(".zip", "")
        xml = open(fixture_path(FIXTURES[rtype]), "rb").read()
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with zipfile.ZipFile(dest, "w") as zf:
            zf.writestr("players_list.xml", xml)
        return os.path.getsize(dest)

    def fake_verify(url):
        return {"ok": True, "status": 200, "detail": "OK"}

    return JobRunnerConfig(
        data_dir=settings.data_directory, base_url="http://localhost/",
        worker_timeout_seconds=600, batch_size=2,
        history_federations=["IRI"],
        download_fn=fake_download, verify_url_fn=fake_verify)


def run_period(app, cfg, period):
    import_service.ensure_period_jobs(period)
    results = [import_service.tick(period, cfg) for _ in range(3)]
    assert [r["status"] for r in results] == ["success"] * 3
    return results


class TestLifecycle:
    def test_full_period(self, app, fixture_path):
        cfg = make_cfg(app, fixture_path)
        results = run_period(app, cfg, "2026-09")
        assert [r["rating_type"] for r in results] == [
            "standard", "rapid", "blitz"]
        assert FidePlayerRepository.count() == 4
        assert FideRatingRepository.count() == 9
        by_type = FideRatingRepository.count_by_type("2026-09")
        assert by_type == {"standard": 4, "rapid": 3, "blitz": 2}
        # Consumed archives are deleted; jobs completed with heartbeats.
        for rtype in ("standard", "rapid", "blitz"):
            zpath = storage.tmp_zip_path(
                cfg.data_dir, "2026-09", rtype)
            assert not os.path.exists(zpath)
            job = FideImportJobRepository.get("2026-09", rtype)
            assert job.state == "completed"
            assert job.heartbeat_at is not None
            assert job.records_processed > 0

    def test_verify_report_after_import(self, app, fixture_path):
        cfg = make_cfg(app, fixture_path)
        run_period(app, cfg, "2026-09")
        report = checks.verify_data("2026-09")
        assert report["ok"] is True
        assert report["players"] == 4
        assert report["by_type"]["standard"] == 4
        assert report["duplicate_keys"] == []
        assert report["orphan_ratings"] == 0

    def test_rerun_is_idempotent(self, app, fixture_path):
        cfg = make_cfg(app, fixture_path)
        run_period(app, cfg, "2026-09")
        job = FideImportJobRepository.get("2026-09", "standard")
        job.state = "failed"
        job.error_code = "X"
        db.session.commit()
        import_service.retry_job("2026-09", "standard")
        # Rapid/blitz completed; standard re-runs alone is NOT eligible
        # via tick (successors completed, claim scans in order and finds
        # standard pending with no incomplete predecessor -> claimed).
        result = import_service.tick("2026-09", cfg)
        assert result["status"] == "success"
        assert FidePlayerRepository.count() == 4
        assert FideRatingRepository.count_by_type("2026-09")["standard"] == 4

    def test_staged_upload_preferred(self, app, fixture_path):
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs("2026-09")
        zpath = storage.tmp_zip_path(cfg.data_dir, "2026-09", "standard")
        xml = open(fixture_path("small_standard.xml"), "rb").read()
        os.makedirs(os.path.dirname(zpath), exist_ok=True)
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("players_list.xml", xml)

        calls = []

        def spy_download(url, dest, **kw):
            calls.append(url)
            return make_cfg(app, fixture_path).download_fn(
                url, dest, **kw)

        cfg.download_fn = spy_download
        result = import_service.tick("2026-09", cfg)
        assert result["status"] == "success"
        assert result["provenance"] == "staged"
        assert calls == []  # no network when staged


class TestFailure:
    def test_malformed_archive_fails_clean(self, app, fixture_path):
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs("2026-09")
        zpath = storage.tmp_zip_path(cfg.data_dir, "2026-09", "standard")
        os.makedirs(os.path.dirname(zpath), exist_ok=True)
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("players_list.xml", "<playerslist><broken")
        result = import_service.tick("2026-09", cfg)
        assert result["status"] == "error"
        job = FideImportJobRepository.get("2026-09", "standard")
        assert job.state == "failed"
        assert job.error_code in ("INVALID_XML", "INVALID_FIDE_FORMAT",
                                  "PARSER_ERROR")
        # Failed imports leave no corrupted state behind…
        assert FidePlayerRepository.count() == 0
        assert FideRatingRepository.count() == 0
        # …but keep the archive for diagnosis/retry.
        assert os.path.isfile(zpath)

    def test_unreachable_source(self, app):
        settings = app.config["FIDE_SETTINGS"]
        cfg = JobRunnerConfig(
            data_dir=settings.data_directory, base_url="http://localhost/",
            download_fn=make_cfg(app, lambda n: n).download_fn,
            verify_url_fn=lambda url: {"ok": False, "status": 404,
                                       "detail": "HTTP status 404"})
        import_service.ensure_period_jobs("2026-09")
        result = import_service.tick("2026-09", cfg)
        assert result["status"] == "error"
        job = FideImportJobRepository.get("2026-09", "standard")
        assert job.state == "failed"

    def test_stale_recovery(self, app, fixture_path):
        from datetime import datetime, timedelta
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs("2026-09")
        job, _ = FideImportJobRepository.claim_next_job("2026-09", 600)
        db.session.commit()
        job.heartbeat_at = datetime.utcnow() - timedelta(hours=3)
        db.session.commit()
        result = import_service.tick("2026-09", cfg)
        assert result["status"] == "idle"
        assert result["reason"] == "failed_present"
        assert result["staled"] == ["standard"]
        retried = import_service.retry_job("2026-09", "standard")
        assert retried["state"] == "pending"
        result = import_service.tick("2026-09", cfg)
        assert result["status"] == "success"

    def test_retry_rejects_non_failed(self, app, fixture_path):
        import pytest
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs("2026-09")
        with pytest.raises(ValueError):
            import_service.retry_job("2026-09", "standard")
        run_period(app, cfg, "2026-08")
        with pytest.raises(ValueError):
            import_service.retry_job("2026-08", "standard")


class TestHistoryScope:
    def test_history_iri_only_current_global(self, app, fixture_path):
        cfg = make_cfg(app, fixture_path)
        run_period(app, cfg, "2026-08")
        run_period(app, cfg, "2026-09")
        # Current ratings exist for the foreign player…
        ger = {(r.period, r.rating_type)
               for r in FideRatingRepository.history("99900001")}
        assert ("2026-09", "standard") in ger
        # …but older non-IRI history was pruned while IRI history stays.
        assert ("2026-08", "standard") not in ger
        iri = {(r.period, r.rating_type)
               for r in FideRatingRepository.history(
                   "12512294", rating_type="standard")}
        assert ("2026-08", "standard") in iri
        assert ("2026-09", "standard") in iri
        # Identity is global: all four players present.
        assert FidePlayerRepository.count() == 4
