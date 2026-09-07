"""Protected admin surface: jobs, uploads, verification.

Separated from the public read API by credential (effective admin key)
and by rate-limit bucket. Every mutation is audit-logged (period, type,
outcome — never key material).
"""
import logging
import os

from flask import Blueprint, current_app, jsonify, request

from application import checks, import_service
from domain.validation import validate_period, validate_rating_type
from infrastructure import storage
from infrastructure.repositories import FideImportJobRepository
from interfaces.api.auth import require_admin_key
from interfaces.api.errors import ApiError

log = logging.getLogger(__name__)

bp = Blueprint("api_admin", __name__)


def _runner_config():
    settings = current_app.config["FIDE_SETTINGS"]
    return import_service.JobRunnerConfig(
        data_dir=settings.data_directory,
        base_url=settings.download_base_url,
        connect_timeout=settings.connect_timeout,
        read_timeout=settings.read_timeout,
        max_retries=settings.max_retries,
        worker_timeout_seconds=settings.import_timeout_seconds,
        batch_size=settings.import_batch_size,
        history_federations=settings.history_federations,
        upload_max_bytes=settings.upload_max_bytes,
        retention_days=settings.raw_retention_days,
    )


@bp.post("/admin/jobs/ensure")
@require_admin_key
def ensure_jobs():
    body = request.get_json(silent=True) or {}
    period = (body.get("period") or "").strip()
    try:
        jobs = import_service.ensure_period_jobs(period)
    except ValueError as exc:
        raise ApiError("BAD_REQUEST", str(exc))
    log.info("Admin: ensured jobs for period %s", period)
    return jsonify({"data": {"period": period, "jobs": jobs}}), 202


@bp.get("/admin/jobs")
@require_admin_key
def list_jobs():
    period = (request.args.get("period") or "").strip()
    try:
        limit = int(request.args.get("limit", 30))
    except (TypeError, ValueError):
        raise ApiError("BAD_REQUEST", "Parameter 'limit' must be an integer.")
    limit = max(1, min(limit, 100))
    if period:
        try:
            jobs = import_service.get_period_status(period)
        except ValueError as exc:
            raise ApiError("BAD_REQUEST", str(exc))
    else:
        jobs = [import_service.job_to_dict(j)
                for j in FideImportJobRepository.recent(limit=limit)]
    return jsonify({"data": {"jobs": jobs}})


@bp.post("/admin/jobs/retry")
@require_admin_key
def retry_job():
    body = request.get_json(silent=True) or {}
    period = (body.get("period") or "").strip()
    rating_type = (body.get("rating_type") or "").strip()
    try:
        job = import_service.retry_job(period, rating_type)
    except ValueError as exc:
        raise ApiError("BAD_REQUEST", str(exc))
    log.info("Admin: retried job %s/%s", period, rating_type)
    return jsonify({"data": {"job": job}})


@bp.post("/admin/uploads")
@require_admin_key
def stage_upload():
    """Stage a manually-provided monthly ZIP for one (period, type).

    The worker prefers a staged file over downloading. Multipart fields:
    period, rating_type, file (.zip).
    """
    settings = current_app.config["FIDE_SETTINGS"]
    period = (request.form.get("period") or "").strip()
    rating_type = (request.form.get("rating_type") or "").strip()
    try:
        validate_period(period)
        validate_rating_type(rating_type)
    except ValueError as exc:
        raise ApiError("BAD_REQUEST", str(exc))
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise ApiError("BAD_REQUEST", "Multipart field 'file' is required.")
    if not upload.filename.lower().endswith(".zip"):
        raise ApiError("BAD_REQUEST", "Only .zip archives are accepted.")
    dest = storage.tmp_zip_path(
        settings.data_directory, period, rating_type)
    try:
        size = storage.save_upload(upload, dest, settings.upload_max_bytes)
    except Exception as exc:
        from domain.errors import FideError
        if isinstance(exc, FideError):
            raise ApiError("BAD_REQUEST", exc.summary())
        raise ApiError("BAD_REQUEST", "Upload could not be staged.")
    try:
        member, declared = storage.inspect_zip(dest)
    except Exception:
        os.remove(dest)
        raise ApiError("BAD_REQUEST",
                       "Uploaded archive contains no usable FIDE XML.")
    log.info("Admin: staged upload %s/%s (%d bytes, member %s)",
             period, rating_type, size, member)
    return jsonify({"data": {"period": period, "rating_type": rating_type,
                             "file_name": os.path.basename(dest),
                             "size_bytes": size, "member": member,
                             "member_bytes": declared}}), 201


@bp.get("/admin/verify")
@require_admin_key
def verify():
    period = (request.args.get("period") or "").strip()
    if period:
        try:
            validate_period(period)
        except ValueError as exc:
            raise ApiError("BAD_REQUEST", str(exc))
    return jsonify({"data": checks.verify_data(period)})
