"""Reliability & recovery tests (Phase 4).

Controlled failure injection against a disposable SQLite database —
never Production. Every test proves the same contract: a crash at any
pipeline boundary leaves consistent state, a recoverable job, and (after
explicit retry) convergence to the correct final counts with no
duplicates.

Provenance: application/import_service.py (tick/claim/heartbeat/fail),
infrastructure/repositories.py (claim/stale), infrastructure/
downloader.py + storage.py (file boundaries), domain/xml_validation.py.
"""
import os
import zipfile

import pytest
import requests

from application import import_service
from application.import_service import JobRunnerConfig
from infrastructure import storage
from infrastructure.database import db
from infrastructure.models import FidePlayerModel, FideRatingModel
from infrastructure.repositories import (
    FideImportJobRepository, FidePlayerRepository, FideRatingRepository,
)
from tests.test_import import FIXTURES, make_cfg

PERIOD = "2026-09"


def _stage(cfg, period, rtype, xml_bytes, member="players_list.xml"):
    zpath = storage.tmp_zip_path(cfg.data_dir, period, rtype)
    os.makedirs(os.path.dirname(zpath), exist_ok=True)
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr(member, xml_bytes)
    return zpath


def _standard_xml(fixture_path):
    return open(fixture_path(FIXTURES["standard"]), "rb").read()


def _fail_commit_nth(monkeypatch, n, message="simulated connection loss"):
    """Make the Nth session commit raise; later commits pass through."""
    real = db.session.commit
    calls = {"count": 0}

    def boom(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == n:
            raise Exception(message)
        return real(*args, **kwargs)

    monkeypatch.setattr(db.session, "commit", boom)
    return calls


def _assert_clean_failed(period, rtype):
    job = FideImportJobRepository.get(period, rtype)
    assert job.state == "failed"
    assert job.error_code
    return job


def _assert_converged(period):
    assert FidePlayerRepository.count() == 4
    assert FideRatingRepository.count() == 9
    by_type = FideRatingRepository.count_by_type(period)
    assert by_type == {"standard": 4, "rapid": 3, "blitz": 2}
    from application import checks
    assert checks.verify_data(period)["ok"] is True


class TestClaimBoundaries:
    """Scenarios A/B: crash before/after the claim commit (Q2/Q3)."""

    def test_rollback_before_claim_commit_leaves_pending(self, app):
        # Claim flushes but a crash before commit must change nothing.
        import_service.ensure_period_jobs(PERIOD)
        job, reason = FideImportJobRepository.claim_next_job(PERIOD, 600)
        assert reason == "claimed"
        db.session.rollback()  # simulated crash before commit
        db.session.expunge_all()
        assert FideImportJobRepository.get(
            PERIOD, "standard").state == "pending"

    def test_claim_commit_persists(self, app):
        import_service.ensure_period_jobs(PERIOD)
        job, reason = FideImportJobRepository.claim_next_job(PERIOD, 600)
        assert reason == "claimed"
        db.session.commit()  # the tick's claim commit
        db.session.expunge_all()
        assert FideImportJobRepository.get(
            PERIOD, "standard").state == "downloading"

    def test_second_worker_cannot_claim(self, app, fixture_path):
        # Q5: one claimed job, a second worker must back off, not double-run.
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)
        job, _ = FideImportJobRepository.claim_next_job(PERIOD, 600)
        db.session.commit()  # worker A owns the claim (fresh heartbeat)
        result = import_service.tick(PERIOD, cfg)  # worker B
        assert result["status"] == "idle"
        assert result["reason"] == "busy"
        assert FidePlayerRepository.count() == 0  # nothing processed twice
        assert FideImportJobRepository.get(
            PERIOD, "standard").state == "downloading"

    def test_retry_while_active_rejected(self, app):
        import_service.ensure_period_jobs(PERIOD)
        _, _ = FideImportJobRepository.claim_next_job(PERIOD, 600)
        db.session.commit()
        with pytest.raises(ValueError):
            import_service.retry_job(PERIOD, "standard")

    def test_missing_predecessor_blocks(self, app, fixture_path):
        # Ordering gate: rapid must not run when standard never existed.
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)
        doomed = FideImportJobRepository.get(PERIOD, "standard")
        db.session.delete(doomed)
        db.session.commit()
        result = import_service.tick(PERIOD, cfg)
        assert result["status"] == "idle"
        assert result["reason"] == "blocked"


class TestPartialImport:
    """Step 4: batch1 committed, batch2 fails -> retry converges (Q7)."""

    def test_batch_failure_then_retry_converges(
            self, app, fixture_path, monkeypatch):
        from infrastructure.repositories import FidePlayerRepository as PR
        cfg = make_cfg(app, fixture_path)
        cfg.batch_size = 2  # 4 standard players -> commits after #2
        import_service.ensure_period_jobs(PERIOD)
        real_save = PR.save
        calls = {"count": 0}

        def boom_once(player):
            calls["count"] += 1
            if calls["count"] == 3:
                raise RuntimeError("simulated mid-import crash")
            return real_save(player)

        monkeypatch.setattr(PR, "save", boom_once)
        result = import_service.tick(PERIOD, cfg)
        assert result["status"] == "error"
        _assert_clean_failed(PERIOD, "standard")
        # Batch 1 committed exactly: first two XML rows, nothing else.
        assert FidePlayerRepository.count() == 2
        assert FideRatingRepository.count_by_type(PERIOD) == {"standard": 2}
        assert {p.fide_id for p in
                db.session.query(FidePlayerModel).all()} == {
                    "12512294", "12512295"}
        # Explicit retry reprocesses idempotently to the full state.
        import_service.retry_job(PERIOD, "standard")
        result = import_service.tick(PERIOD, cfg)
        assert result["status"] == "success"
        assert FidePlayerRepository.count() == 4
        assert FideRatingRepository.count_by_type(PERIOD) == {"standard": 4}
        dupes = (db.session.query(
            FideRatingModel.fide_id, FideRatingModel.period,
            FideRatingModel.rating_type)
            .filter(FideRatingModel.period == PERIOD).all())
        assert len(dupes) == len(set(dupes))

    def test_verify_gate_blocks_empty_result(
            self, app, fixture_path, monkeypatch):
        # Processed rows but zero stored rows must fail verification,
        # while committed identities survive and retry converges.
        from infrastructure.repositories import FideRatingRepository as RR
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)
        monkeypatch.setattr(RR, "save", lambda rating: rating)
        result = import_service.tick(PERIOD, cfg)
        assert result["status"] == "error"
        job = _assert_clean_failed(PERIOD, "standard")
        assert job.error_code == "INVALID_FIDE_FORMAT"
        assert FidePlayerRepository.count() == 4
        assert FideRatingRepository.count() == 0
        monkeypatch.undo()  # restore rating persistence for the retry
        import_service.retry_job(PERIOD, "standard")
        assert import_service.tick(PERIOD, cfg)["status"] == "success"
        assert FideRatingRepository.count_by_type(PERIOD) == {"standard": 4}

    def test_prune_failure_keeps_imported_data(
            self, app, fixture_path, monkeypatch):
        from infrastructure.repositories import FideRatingRepository as RR
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)

        def boom(*args, **kwargs):
            raise RuntimeError("simulated prune outage")

        monkeypatch.setattr(RR, "prune_history", boom)
        result = import_service.tick(PERIOD, cfg)
        assert result["status"] == "error"
        _assert_clean_failed(PERIOD, "standard")
        # Import committed before prune: data present, archive kept.
        assert FideRatingRepository.count_by_type(PERIOD) == {"standard": 4}
        assert os.path.isfile(storage.tmp_zip_path(
            cfg.data_dir, PERIOD, "standard"))
        monkeypatch.undo()  # restore pruning for the retry
        import_service.retry_job(PERIOD, "standard")
        assert import_service.tick(PERIOD, cfg)["status"] == "success"
        assert FideRatingRepository.count_by_type(PERIOD) == {"standard": 4}

    def test_cleanup_failure_still_completes(
            self, app, fixture_path, monkeypatch):
        # Cleanup is best-effort by design: its failure warns, never fails.
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)

        def boom(*args, **kwargs):
            raise OSError("simulated disk hiccup")

        monkeypatch.setattr(storage, "cleanup_old_files", boom)
        result = import_service.tick(PERIOD, cfg)
        assert result["status"] == "success"
        assert FideImportJobRepository.get(
            PERIOD, "standard").state == "completed"


class TestCommitFailures:
    """Step 11 (SQLite): connection loss at commit boundaries (Q4)."""

    def test_sweep_commit_failure_leaves_job_untouched(
            self, app, fixture_path, monkeypatch):
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)
        _stage(cfg, PERIOD, "standard", _standard_xml(fixture_path))
        _fail_commit_nth(monkeypatch, 1)
        result = import_service.tick(PERIOD, cfg)
        assert result["status"] == "error"
        assert "simulated" in result["message"]
        # Sweep runs before any claim: job still pending, data absent.
        assert FideImportJobRepository.get(
            PERIOD, "standard").state == "pending"
        assert FidePlayerRepository.count() == 0
        assert os.path.isfile(storage.tmp_zip_path(
            cfg.data_dir, PERIOD, "standard"))
        # Next tick retries cleanly to success.
        assert import_service.tick(PERIOD, cfg)["status"] == "success"

    def test_heartbeat_commit_failure_fails_job_recoverably(
            self, app, fixture_path, monkeypatch):
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)
        _stage(cfg, PERIOD, "standard", _standard_xml(fixture_path))
        _fail_commit_nth(monkeypatch, 3)  # sweep, claim, staged heartbeat
        result = import_service.tick(PERIOD, cfg)
        assert result["status"] == "error"
        job = _assert_clean_failed(PERIOD, "standard")
        assert job.records_processed == 0  # died before any batch
        import_service.retry_job(PERIOD, "standard")
        assert import_service.tick(PERIOD, cfg)["status"] == "success"
        assert FideRatingRepository.count_by_type(PERIOD) == {"standard": 4}

    def test_claim_failure_returns_error_without_state_change(
            self, app, fixture_path, monkeypatch):
        from infrastructure import repositories as repos
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)
        orig = repos.FideImportJobRepository.claim_next_job
        calls = {"count": 0}

        def flaky(period, timeout):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("simulated connection drop")
            return orig(period, timeout)

        monkeypatch.setattr(repos.FideImportJobRepository,
                            "claim_next_job", flaky)
        result = import_service.tick(PERIOD, cfg)
        assert result["status"] == "error"
        assert FideImportJobRepository.get(
            PERIOD, "standard").state == "pending"
        assert [import_service.tick(PERIOD, cfg)["status"]
                for _ in range(3)] == ["success"] * 3


class TestStaleMechanics:
    """Step 7: timeout source, heartbeat, stale transition, retry."""

    def test_fresh_heartbeat_never_stale(self, app):
        import_service.ensure_period_jobs(PERIOD)
        _, _ = FideImportJobRepository.claim_next_job(PERIOD, 600)
        db.session.commit()
        staled = FideImportJobRepository.fail_stale_jobs(PERIOD, 600)
        assert staled == []
        assert FideImportJobRepository.get(
            PERIOD, "standard").state == "downloading"

    def test_missing_heartbeat_is_stale(self, app):
        import_service.ensure_period_jobs(PERIOD)
        job, _ = FideImportJobRepository.claim_next_job(PERIOD, 600)
        db.session.commit()
        job.heartbeat_at = None
        db.session.commit()
        staled = FideImportJobRepository.fail_stale_jobs(PERIOD, 600)
        assert [j.rating_type for j in staled] == ["standard"]
        assert FideImportJobRepository.get(
            PERIOD, "standard").state == "failed"

    def test_zero_timeout_forces_stale_tick(
            self, app, fixture_path):
        # timeout_seconds comes from the caller (worker_timeout_seconds in
        # production); 0 proves the timeout source, not a magic constant.
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)
        _, _ = FideImportJobRepository.claim_next_job(PERIOD, 600)
        db.session.commit()
        result = import_service.tick(PERIOD, cfg, timeout_seconds=0)
        assert result["status"] == "idle"
        assert result["reason"] == "failed_present"
        assert result["staled"] == ["standard"]
        import_service.retry_job(PERIOD, "standard")
        assert import_service.tick(PERIOD, cfg)["status"] == "success"


class TestIdempotency:
    """Step 5: repeated operations converge to one correct state."""

    def test_double_run_converges(self, app, fixture_path):
        from tests.test_import import run_period
        cfg = make_cfg(app, fixture_path)
        run_period(app, cfg, PERIOD)
        before_players = FidePlayerRepository.count()
        before_ratings = FideRatingRepository.count()
        # Force a full same-data re-import through failed->retry.
        for rtype in ("standard", "rapid", "blitz"):
            job = FideImportJobRepository.get(PERIOD, rtype)
            job.state = "failed"
            job.error_code = "X"
        db.session.commit()
        for rtype in ("standard", "rapid", "blitz"):
            import_service.retry_job(PERIOD, rtype)
            assert import_service.tick(PERIOD, cfg)["status"] == "success"
        assert FidePlayerRepository.count() == before_players
        assert FideRatingRepository.count() == before_ratings
        _assert_converged(PERIOD)
        for rtype in ("standard", "rapid", "blitz"):
            job = FideImportJobRepository.get(PERIOD, rtype)
            assert job.state == "completed"
            assert job.error_code is None

    def test_duplicate_rows_in_file_last_wins(self, app, fixture_path):
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)
        xml = (_standard_xml(fixture_path).replace(
            b"</playerslist>",
            b"<player><fideid>12512294</fideid><name>Alpha, Test One</name>"
            b"<country>IRI</country><sex>M</sex><title>GM</title>"
            b"<rating>2700</rating><games>1</games><k>10</k>"
            b"<birthday>1999</birthday></player></playerslist>"))
        _stage(cfg, PERIOD, "standard", xml)
        assert import_service.tick(PERIOD, cfg)["status"] == "success"
        rows = [r for r in db.session.query(FideRatingModel).filter_by(
            fide_id="12512294", period=PERIOD,
            rating_type="standard").all()]
        assert len(rows) == 1
        assert (rows[0].rating, rows[0].games) == (2700, 1)
        assert FidePlayerRepository.count() == 4


class _FakeResp:
    """Minimal requests response double (status/headers/body/errors)."""

    def __init__(self, status=200, body=b"", headers=None, stream_exc=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self._stream_exc = stream_exc

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(
                "HTTP %d" % self.status_code)
            err.response = self
            raise err

    def iter_content(self, chunk_size=65536):
        if self._stream_exc is not None:
            raise self._stream_exc
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]


def _tiny_zip():
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("players_list.xml", b"<playerslist></playerslist>")
    return buf.getvalue()


class TestDownloaderRecovery:
    """Step 8: every downloader failure mode, no sleeps, no network."""

    def test_dns_failure_classified(self, tmp_path, monkeypatch):
        from infrastructure import downloader
        from domain.errors import FideErrorCategory

        def boom(*args, **kwargs):
            raise requests.exceptions.ConnectionError(
                "getaddrinfo failed: nodename nor servname")

        monkeypatch.setattr(requests, "get", boom)
        dest = str(tmp_path / "d.zip")
        with pytest.raises(Exception) as exc:
            downloader.download_fide_zip(
                "https://x/download/a.zip", dest, max_retries=0)
        assert exc.value.category == FideErrorCategory.NETWORK_DNS_ERROR
        assert not os.path.exists(dest)
        assert not os.path.exists(dest + ".part")

    def test_timeout_classified(self, tmp_path, monkeypatch):
        from infrastructure import downloader
        from domain.errors import FideErrorCategory

        def boom(*args, **kwargs):
            raise requests.exceptions.ConnectTimeout("timed out")

        monkeypatch.setattr(requests, "get", boom)
        with pytest.raises(Exception) as exc:
            downloader.download_fide_zip(
                "https://x/download/a.zip", str(tmp_path / "d.zip"),
                max_retries=0)
        assert exc.value.category == FideErrorCategory.NETWORK_TIMEOUT

    def test_tls_failure_classified(self, tmp_path, monkeypatch):
        from infrastructure import downloader
        from domain.errors import FideErrorCategory

        def boom(*args, **kwargs):
            raise requests.exceptions.SSLError("certificate verify failed")

        monkeypatch.setattr(requests, "get", boom)
        with pytest.raises(Exception) as exc:
            downloader.download_fide_zip(
                "https://x/download/a.zip", str(tmp_path / "d.zip"),
                max_retries=0)
        assert exc.value.category == FideErrorCategory.NETWORK_TLS_ERROR

    def _get_with(self, monkeypatch, resp):
        def fake(*args, **kwargs):
            return resp
        monkeypatch.setattr(requests, "get", fake)

    def test_http_429_fails_fast(self, tmp_path, monkeypatch):
        from infrastructure import downloader
        from domain.errors import FideErrorCategory
        self._get_with(monkeypatch, _FakeResp(status=429))
        with pytest.raises(Exception) as exc:
            downloader.download_fide_zip(
                "https://x/download/a.zip", str(tmp_path / "d.zip"),
                max_retries=3)
        assert exc.value.category == FideErrorCategory.HTTP_RATE_LIMIT

    def test_http_404_fails_fast(self, tmp_path, monkeypatch):
        from infrastructure import downloader
        from domain.errors import FideErrorCategory
        self._get_with(monkeypatch, _FakeResp(status=404))
        with pytest.raises(Exception) as exc:
            downloader.download_fide_zip(
                "https://x/download/a.zip", str(tmp_path / "d.zip"),
                max_retries=3)
        assert exc.value.category == FideErrorCategory.HTTP_ERROR

    def test_http_500_no_retry_when_disabled(self, tmp_path, monkeypatch):
        from infrastructure import downloader
        from domain.errors import FideErrorCategory
        self._get_with(monkeypatch, _FakeResp(status=500))
        with pytest.raises(Exception) as exc:
            downloader.download_fide_zip(
                "https://x/download/a.zip", str(tmp_path / "d.zip"),
                max_retries=0)
        assert exc.value.category == FideErrorCategory.HTTP_ERROR

    def test_non_zip_body_rejected_atomically(
            self, tmp_path, monkeypatch):
        from infrastructure import downloader
        from domain.errors import FideErrorCategory
        self._get_with(monkeypatch, _FakeResp(
            status=200, body=b"<html>error page</html>",
            headers={"Content-Length": "22"}))
        dest = str(tmp_path / "d.zip")
        with pytest.raises(Exception) as exc:
            downloader.download_fide_zip(url := "https://x/download/a.zip",
                                         dest, max_retries=0)
        assert url.startswith("https://")
        assert exc.value.category == FideErrorCategory.INVALID_CONTENT
        assert not os.path.exists(dest)
        assert not os.path.exists(dest + ".part")

    def test_mid_stream_break_drops_partial(
            self, tmp_path, monkeypatch):
        from infrastructure import downloader
        from domain.errors import FideErrorCategory
        self._get_with(monkeypatch, _FakeResp(
            status=200, body=_tiny_zip(),
            stream_exc=requests.exceptions.ConnectionError(
                "reset by peer")))
        dest = str(tmp_path / "d.zip")
        with pytest.raises(Exception) as exc:
            downloader.download_fide_zip(
                "https://x/download/a.zip", dest, max_retries=0)
        assert exc.value.category == (
            FideErrorCategory.NETWORK_CONNECTION_ERROR)
        assert not os.path.exists(dest)
        assert not os.path.exists(dest + ".part")

    def test_happy_path_streams_atomically(self, tmp_path, monkeypatch):
        from infrastructure import downloader
        body = _tiny_zip()
        self._get_with(monkeypatch, _FakeResp(
            status=200, body=body,
            headers={"Content-Length": str(len(body))}))
        dest = str(tmp_path / "d.zip")
        assert downloader.download_fide_zip(
            "https://x/download/a.zip", dest, max_retries=0) == len(body)
        assert os.path.getsize(dest) == len(body)
        assert not os.path.exists(dest + ".part")

    def test_head_probe_results(self, monkeypatch):
        from infrastructure import downloader

        def fake_head_404(*args, **kwargs):
            return _FakeResp(status=404)

        def fake_head_down(*args, **kwargs):
            raise requests.exceptions.ConnectionError("unreachable")

        monkeypatch.setattr(requests, "head", fake_head_404)
        verdict = downloader.verify_rating_url("https://x/a.zip")
        assert verdict == {"ok": False, "status": 404,
                           "detail": "HTTP status 404"}
        monkeypatch.setattr(requests, "head", fake_head_down)
        verdict = downloader.verify_rating_url("https://x/a.zip")
        assert verdict["ok"] is False and verdict["status"] is None


class TestFilesystemRecovery:
    """Step 10: leftovers are never trusted, never grow, never block."""

    def test_truncated_zip_fails_and_is_kept(
            self, app, fixture_path):
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)
        # Raw truncated bytes (NOT a valid zip containing garbage — that
        # is a different, already-covered INVALID_XML path).
        zpath = storage.tmp_zip_path(cfg.data_dir, PERIOD, "standard")
        os.makedirs(os.path.dirname(zpath), exist_ok=True)
        with open(zpath, "wb") as fh:
            fh.write(b"PK\x03\x04" + b"\x00" * 97)
        result = import_service.tick(PERIOD, cfg)
        assert result["status"] == "error"
        job = _assert_clean_failed(PERIOD, "standard")
        assert job.error_code == "INVALID_CONTENT"
        assert FidePlayerRepository.count() == 0
        assert os.path.isfile(zpath)  # kept for diagnosis/retry

    def test_empty_xml_fails_with_zero_rows(
            self, app, fixture_path):
        cfg = make_cfg(app, fixture_path)
        import_service.ensure_period_jobs(PERIOD)
        zpath = _stage(cfg, PERIOD, "standard",
                       b"<playerslist></playerslist>")
        result = import_service.tick(PERIOD, cfg)
        assert result["status"] == "error"
        assert _assert_clean_failed(
            PERIOD, "standard").error_code == "INVALID_FIDE_FORMAT"
        assert FidePlayerRepository.count() == 0
        assert FideRatingRepository.count() == 0
        assert os.path.isfile(zpath)

    def test_retention_sweeps_only_old_period_dirs(self, tmp_path):
        from infrastructure.storage import cleanup_old_files
        base = str(tmp_path)
        old = os.path.join(base, "tmp", "2020-01")
        current = os.path.join(base, "tmp", PERIOD)
        stray = os.path.join(base, "tmp", "not-a-period")
        for path in (old, current, stray):
            os.makedirs(path, exist_ok=True)
            open(os.path.join(path, "x.zip"), "wb").write(b"z")
        assert cleanup_old_files(base, retention_days=90) == 1
        assert not os.path.exists(old)
        assert os.path.isfile(os.path.join(current, "x.zip"))
        assert os.path.isfile(os.path.join(stray, "x.zip"))

    def test_missing_root_sweep_is_noop(self, tmp_path):
        from infrastructure.storage import cleanup_old_files
        assert cleanup_old_files(str(tmp_path / "absent"), 90) == 0


class TestApiReliability:
    """Step 12: total backend failure still yields a safe envelope."""

    def test_database_down_gives_500_envelope(self, tmp_path):
        from config import Settings
        from interfaces.factory import create_app
        app = create_app(Settings(
            database_url="sqlite:////nonexistent-dir-xyz-123/fide.db",
            api_key="k", admin_key="a", secret_key="s",
            data_directory=str(tmp_path),
            rate_limit_per_minute=0, auth_fail_limit_per_hour=0,
            history_federations=["IRI"]))
        app.config["TESTING"] = True
        # Let the 500 handler run instead of propagating (as in production).
        app.config["PROPAGATE_EXCEPTIONS"] = False
        resp = app.test_client().get(
            "/api/v1/players/12512294", headers={"X-API-Key": "k"})
        assert resp.status_code == 500
        body = resp.get_json()["error"]
        assert body["code"] == "INTERNAL"
        assert "request_id" in body
        text = resp.get_data(as_text=True)
        assert "Traceback" not in text
        assert "sqlalchemy" not in text.lower()
        assert "nonexistent-dir" not in text

    def test_internal_exception_is_generic(self, client, auth_headers,
                                           monkeypatch):
        import application.player_service as ps
        from interfaces.api import players as players_bp

        def boom(*args, **kwargs):
            raise RuntimeError("simulated handler bug")

        monkeypatch.setattr(ps, "get_player", boom)
        assert players_bp is not None  # blueprint import intact
        client.application.config["PROPAGATE_EXCEPTIONS"] = False
        resp = client.get("/api/v1/players/12512294", headers=auth_headers)
        assert resp.status_code == 500
        assert resp.get_json()["error"]["code"] == "INTERNAL"
        assert "simulated handler bug" not in resp.get_data(as_text=True)
