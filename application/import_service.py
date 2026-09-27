"""Job-based import orchestration. No Flask imports.

Lifecycle per step: ensure -> download -> validate -> import -> verify ->
cleanup -> completed. Steps run Standard -> Rapid -> Blitz; a later type
never runs before its dependency completes. One worker tick claims and
runs exactly ONE step (atomic claim + heartbeat + stale recovery).

The caller (CLI/API) owns the app context and builds a JobRunnerConfig
from service settings — the application layer never reads env itself.
Only this layer commits/rolls back; repositories stay flush-only.
"""
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from domain.errors import (
    FideError, FideErrorCategory, classify_exception,
    public_error_summary,
)
from domain.models import RATING_TYPE_ORDER, can_transition
from domain.parser import parse_fide_xml
from domain.validation import validate_period, validate_rating_type
from infrastructure import downloader, storage
from infrastructure.database import db
from infrastructure.models import (
    FideImportJobModel, FidePlayerModel, FideRatingModel,
)
from infrastructure.repositories import (
    FideImportJobRepository, FidePlayerRepository, FideRatingRepository,
)
from infrastructure.storage import FideStorageError

log = logging.getLogger(__name__)


@dataclass
class JobRunnerConfig:
    data_dir: str
    base_url: str
    connect_timeout: int = 10
    read_timeout: int = 30
    max_retries: int = 3
    worker_timeout_seconds: int = 600
    batch_size: int = 500
    history_federations: List[str] = field(default_factory=lambda: ["IRI"])
    upload_max_bytes: int = 120 * 1024 * 1024
    retention_days: int = 90
    # Injected seams for tests (defaults are the real implementations).
    download_fn: Callable = None
    verify_url_fn: Callable = None

    def __post_init__(self):
        if self.download_fn is None:
            self.download_fn = downloader.download_fide_zip
        if self.verify_url_fn is None:
            self.verify_url_fn = downloader.verify_rating_url


def job_to_dict(job: FideImportJobModel) -> Dict[str, Any]:
    # error_message is DELIBERATELY sanitized: the stored raw text can
    # embed SQL fragments, paths, or player data (see audit L4). API
    # clients receive only the curated code + summary; full detail stays
    # in the server-side worker logs for operators.
    return {
        "period": job.period,
        "rating_type": job.rating_type,
        "state": job.state,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "heartbeat_at": job.heartbeat_at.isoformat() if job.heartbeat_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "records_processed": job.records_processed or 0,
        "total_records": job.total_records,
        "error_code": job.error_code,
        "error_message": (public_error_summary(job.error_code)
                          if job.state == "failed" else None),
        "source_url": job.source_url,
        "file_name": job.file_name,
    }


def _transition(job: FideImportJobModel, to_state: str) -> None:
    if not can_transition(job.state, to_state):
        raise FideError(
            FideErrorCategory.UNKNOWN_ERROR, stage="job",
            detail="Illegal job transition %s -> %s." % (job.state, to_state))
    job.state = to_state


# ── HTTP/admin side: queue work, return immediately (no threads) ──────

def ensure_period_jobs(period: str) -> List[Dict[str, Any]]:
    """Create the three pending rows for a period (idempotent)."""
    validate_period(period)
    for rating_type in RATING_TYPE_ORDER:
        if FideImportJobRepository.get(period, rating_type) is None:
            FideImportJobRepository.save(FideImportJobModel(
                period=period, rating_type=rating_type, state="pending"))
    db.session.commit()
    return [job_to_dict(j)
            for j in FideImportJobRepository.get_for_period(period)]


def get_period_status(period: str) -> List[Dict[str, Any]]:
    validate_period(period)
    return [job_to_dict(j)
            for j in FideImportJobRepository.get_for_period(period)]


def retry_job(period: str, rating_type: str) -> Dict[str, Any]:
    """Re-queue one FAILED step. Completed/active steps are never reset."""
    validate_period(period)
    validate_rating_type(rating_type)
    job = FideImportJobRepository.get(period, rating_type)
    if job is None:
        raise ValueError("no job for %s/%s" % (period, rating_type))
    if job.state != "failed":
        raise ValueError("only failed steps can be retried (state=%s)"
                         % job.state)
    _transition(job, "pending")
    job.started_at = None
    job.heartbeat_at = None
    job.completed_at = None
    job.records_processed = 0
    job.total_records = None
    job.error_code = None
    job.error_message = None
    FideImportJobRepository.save(job)
    db.session.commit()
    return job_to_dict(job)


# ── CLI/worker side: one tick = one step ──────────────────────────────

def tick(period: str, cfg: JobRunnerConfig,
         timeout_seconds: Optional[int] = None) -> Dict[str, Any]:
    """Expire stale jobs, claim ONE eligible step, run it, exit.

    Safe to invoke concurrently: the second tick reports busy/blocked
    and changes nothing.
    """
    validate_period(period)
    # Explicit None means "use the configured default"; any integer —
    # including 0 — is honored verbatim (``or`` would silently swallow 0).
    timeout = (cfg.worker_timeout_seconds if timeout_seconds is None
               else timeout_seconds)
    try:
        staled = FideImportJobRepository.fail_stale_jobs(period, timeout)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        log.error("Stale-sweep failed: %s", exc)
        return {"status": "error", "message": str(exc)}
    try:
        job, reason = FideImportJobRepository.claim_next_job(period, timeout)
    except Exception as exc:
        db.session.rollback()
        log.error("Claim failed: %s", exc)
        return {"status": "error", "message": str(exc)}
    if job is None:
        db.session.rollback()  # release locks, change nothing
        return {"status": "idle", "reason": reason,
                "staled": [j.rating_type for j in staled]}
    try:
        db.session.commit()  # persist the claim, then work
    except Exception as exc:
        db.session.rollback()
        return {"status": "error", "message": str(exc)}
    result = run_claimed_job(job, cfg)
    result["staled"] = [j.rating_type for j in staled]
    return result


def _heartbeat(job: FideImportJobModel, state: Optional[str] = None,
               processed: Optional[int] = None,
               total: Optional[int] = None) -> None:
    if state is not None and state != job.state:
        _transition(job, state)
    job.heartbeat_at = datetime.utcnow()
    if processed is not None:
        job.records_processed = processed
    if total is not None:
        job.total_records = total
    db.session.add(job)
    db.session.commit()


def _fail_job(job: FideImportJobModel, exc: Exception) -> Dict[str, Any]:
    try:
        db.session.rollback()
    except Exception:
        pass
    try:
        job = db.session.merge(job)  # may have been expunged mid-batch
    except Exception:
        pass
    if isinstance(exc, FideError):
        code, message = exc.category, exc.summary()
    else:
        code = classify_exception(exc)
        message = "%s: %s" % (code, exc)
    try:
        _transition(job, "failed")
    except FideError:
        job.state = "failed"  # terminal safety net, never crash here
    job.error_code = code
    job.error_message = message
    job.completed_at = datetime.utcnow()
    job.heartbeat_at = datetime.utcnow()
    FideImportJobRepository.save(job)
    db.session.commit()
    log.error("Import job failed [%s/%s/%s]: %s",
              job.period, job.rating_type, code, message)
    return {"status": "error", "period": job.period,
            "rating_type": job.rating_type,
            "category": code, "message": message}


def run_claimed_job(job: FideImportJobModel,
                    cfg: JobRunnerConfig) -> Dict[str, Any]:
    """Execute one claimed step end-to-end in THIS process."""
    period, rating_type = job.period, job.rating_type
    started = time.monotonic()
    zip_path = storage.tmp_zip_path(cfg.data_dir, period, rating_type)

    try:
        # ── download (or reuse a staged upload) ──
        if os.path.isfile(zip_path):
            provenance = "staged"
            job.file_name = os.path.basename(zip_path)
            _heartbeat(job)  # still owned + fresh, no state change
        else:
            provenance = "downloaded"
            url = downloader.build_rating_url(
                cfg.base_url, rating_type, period)
            job.source_url = url
            job.file_name = os.path.basename(url)
            _heartbeat(job, state="downloading")
            verdict = cfg.verify_url_fn(url)
            if not verdict["ok"]:
                raise FideError(
                    FideErrorCategory.HTTP_ERROR if verdict.get("status")
                    else FideErrorCategory.NETWORK_CONNECTION_ERROR,
                    stage="download",
                    detail="Official source for %s is unreachable (%s): %s"
                    % (rating_type, url, verdict.get("detail")))
            last_beat = [0.0]

            def _progress(_fraction):
                now = time.monotonic()
                if now - last_beat[0] >= 10:
                    last_beat[0] = now
                    _heartbeat(job)

            cfg.download_fn(
                url, zip_path, progress_cb=_progress,
                connect_timeout=cfg.connect_timeout,
                read_timeout=cfg.read_timeout,
                max_retries=cfg.max_retries)
            _heartbeat(job)

        # ── validate (streamed from the archive, never extracted) ──
        try:
            member, declared = storage.inspect_zip(zip_path)
        except FideStorageError as exc:
            raise FideError(FideErrorCategory.INVALID_CONTENT,
                            stage="validate",
                            detail="Archive is not valid: %s" % exc)
        _heartbeat(job, state="validating")

        def _open():
            return storage.open_zip_member(zip_path, member)

        from domain.xml_validation import validate_stream
        validation = validate_stream(
            _open, "%s#%s" % (os.path.basename(zip_path), member),
            size_bytes=declared or 0, max_bytes=None)

        # ── import (streamed, this rating_type only) ──
        _heartbeat(job, state="importing",
                   total=validation.players_found or None)
        processed = 0
        created = 0
        stream = _open()
        try:
            for player_data in parse_fide_xml(
                    stream, allowed_federations=None,
                    rating_type=rating_type):
                fide_id = (player_data.fide_id or "").strip()
                if not fide_id or not (player_data.name or "").strip():
                    continue
                processed += 1
                if _upsert_player(player_data, period, rating_type):
                    created += 1
                if processed % cfg.batch_size == 0:
                    _heartbeat(job, processed=processed)
                    db.session.expunge_all()  # bounded memory
                    job = db.session.merge(job)  # re-attach (expunged too)
            db.session.commit()
            _heartbeat(job, processed=processed)
            db.session.expunge_all()
            job = db.session.merge(job)
        finally:
            try:
                stream.close()
            except Exception:
                pass

        # ── verify (counts must reconcile before anything is deleted) ──
        _heartbeat(job, state="verifying")
        stored = (db.session.query(FideRatingModel)
                  .filter(FideRatingModel.period == period)
                  .filter(FideRatingModel.rating_type == rating_type)
                  .count())
        if processed == 0 or stored == 0:
            raise FideError(
                FideErrorCategory.INVALID_FIDE_FORMAT, stage="verify",
                detail="Import produced no records (processed=%d, stored=%d)."
                % (processed, stored))

        # ── history scope: keep older periods only for configured feds ──
        # Prune runs in id-ordered chunks; each chunk heartbeats (which
        # also commits it), so a large prune can never sit past the
        # worker stale timeout with a dead heartbeat.
        pruned = FideRatingRepository.prune_history(
            period, rating_type, cfg.history_federations,
            progress_cb=lambda _deleted: _heartbeat(job))
        db.session.commit()

        # ── cleanup: delete the consumed archive, sweep old tmp dirs ──
        # Both steps are best-effort: the import already committed, so a
        # leftover file must never flip a success into a failure (the
        # next successful tick sweeps again).
        try:
            if os.path.isfile(zip_path):
                os.remove(zip_path)
        except OSError as exc:
            log.warning("Tmp ZIP cleanup failed for %s: %s", zip_path, exc)
        try:
            storage.cleanup_old_files(cfg.data_dir, cfg.retention_days)
        except OSError as exc:
            log.warning("Retention cleanup failed for %s: %s",
                        cfg.data_dir, exc)

        duration = int(time.monotonic() - started)
        _transition(job, "completed")
        job.records_processed = processed
        job.completed_at = datetime.utcnow()
        job.heartbeat_at = datetime.utcnow()
        job.error_code = None
        job.error_message = None
        FideImportJobRepository.save(job)
        db.session.commit()
        log.info("Import job completed [%s/%s via %s]: %d processed "
                 "(%d new players, %d stored, %d pruned) in %ss",
                 period, rating_type, provenance, processed, created,
                 stored, pruned, duration)
        return {"status": "success", "period": period,
                "rating_type": rating_type, "processed": processed,
                "created": created, "stored": stored, "pruned": pruned,
                "provenance": provenance, "duration_seconds": duration}
    except FideError as exc:
        return _fail_job(job, exc)
    except Exception as exc:  # never leak a traceback past the worker
        log.error("Import job crashed: %s", exc)
        return _fail_job(job, exc)


def _upsert_player(player_data, period: str, rating_type: str) -> bool:
    """Upsert ONE identity + this step's rating snapshot.

    Returns True when the player row was newly created. Idempotent by
    primary/unique key. No commit here — the caller commits per batch.
    """
    created = False
    player = FidePlayerRepository.get_by_fide_id(player_data.fide_id)
    # Federation is the one identity field with read-path semantics
    # attached (federation filter, history keep-list): normalize once at
    # the write boundary so "iri"/" IRI " can never slip past the
    # uppercase comparisons. Names/titles stay verbatim by policy.
    federation = (player_data.federation or "").strip().upper()
    if player is None:
        player = FidePlayerModel(
            fide_id=player_data.fide_id,
            name=player_data.name,
            federation=federation,
            sex=player_data.sex,
            title=player_data.title or "",
            wtitle=player_data.wtitle or "",
            otitle=player_data.otitle or "",
            foatitle=player_data.foatitle or "",
            birth_year=player_data.birth_year or "",
            inactive=player_data.inactive,
        )
        FidePlayerRepository.save(player)
        created = True
    else:
        player.name = player_data.name
        player.sex = player_data.sex
        player.federation = federation
        player.title = player_data.title or ""
        player.wtitle = player_data.wtitle or ""
        player.otitle = player_data.otitle or ""
        player.foatitle = player_data.foatitle or ""
        player.birth_year = player_data.birth_year or ""
        player.inactive = player_data.inactive
    rating, games, k = player_data.rating_for(rating_type)
    existing = FideRatingRepository.get(
        player_data.fide_id, period, rating_type)
    if existing is None:
        FideRatingRepository.save(FideRatingModel(
            fide_id=player_data.fide_id, period=period,
            rating_type=rating_type, rating=rating,
            games=games, k_factor=k))
    else:
        existing.rating = rating
        existing.games = games
        existing.k_factor = k
    return created
