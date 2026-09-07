"""CLI: migrations, worker ticks, verification. Server-shell trust only."""
import click
from flask import current_app

from application import checks, import_service
from infrastructure import migrations, storage


def _runner_config(**overrides):
    settings = current_app.config["FIDE_SETTINGS"]
    args = dict(
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
    args.update({k: v for k, v in overrides.items() if v is not None})
    return import_service.JobRunnerConfig(**args)


def register(app):
    @app.cli.command("db-upgrade")
    def db_upgrade():
        """Apply pending FIDE migrations to the service database."""
        from infrastructure.database import db
        applied = migrations.upgrade(db.engine)
        if applied:
            click.echo("Applied: %s" % ", ".join(applied))
        else:
            click.echo("Already up to date.")

    @app.cli.command("db-status")
    def db_status():
        """Show applied vs pending migrations."""
        from infrastructure.database import db
        click.echo("Applied: %s"
                   % (migrations.applied_versions(db.engine) or "none"))
        click.echo("Pending: %s"
                   % (migrations.pending_migrations(db.engine) or "none"))

    @app.cli.command("fide-ensure-jobs")
    @click.option("--period", required=True, help="YYYY-MM")
    def fide_ensure_jobs(period):
        """Create pending (period x standard|rapid|blitz) job rows."""
        jobs = import_service.ensure_period_jobs(period)
        for job in jobs:
            click.echo("%s/%s: %s"
                       % (job["period"], job["rating_type"], job["state"]))

    @app.cli.command("fide-worker")
    @click.option("--period", default=None, help="YYYY-MM (default: current)")
    @click.option("--timeout", "timeout_s", type=int, default=None)
    def fide_worker(period, timeout_s):
        """Run ONE worker tick: claim a single step, execute it, exit.

        Designed for Cron every few minutes. Prints OK/IDLE/ERROR.
        """
        period = period or storage.get_period_string()
        result = import_service.tick(
            period, _runner_config(), timeout_seconds=timeout_s)
        status = result.get("status")
        if status == "success":
            click.echo("OK: %(rating_type)s %(processed)d processed "
                       "(%(created)d new) in %(duration_seconds)ds"
                       % result)
        elif status == "idle":
            click.echo("IDLE: %s" % result.get("reason"))
        else:
            click.echo("ERROR: %s" % result.get("message"))

    @app.cli.command("fide-retry")
    @click.option("--period", required=True, help="YYYY-MM")
    @click.option("--type", "rating_type", required=True,
                  help="standard|rapid|blitz")
    def fide_retry(period, rating_type):
        """Re-queue one failed step for explicit retry."""
        job = import_service.retry_job(period, rating_type)
        click.echo("Re-queued %s/%s -> %s"
                   % (job["period"], job["rating_type"], job["state"]))

    @app.cli.command("fide-verify")
    @click.option("--period", default="", help="YYYY-MM (default: all)")
    def fide_verify(period):
        """Print the dataset verification report (counts + integrity)."""
        report = checks.verify_data(period)
        click.echo("ok=%s players=%d ratings=%d by_type=%s periods=%s" % (
            report["ok"], report["players"], report["ratings"],
            report["by_type"], report["periods"]))
        for issue in ("duplicate_keys", "invalid_rating_types"):
            if report[issue]:
                click.echo("%s: %s" % (issue, report[issue]))
        for issue in ("empty_fide_ids", "orphan_ratings"):
            if report[issue]:
                click.echo("%s: %d" % (issue, report[issue]))

    @app.cli.command("fide-cleanup")
    def fide_cleanup():
        """Sweep staged tmp dirs older than the retention window."""
        settings = current_app.config["FIDE_SETTINGS"]
        removed = storage.cleanup_old_files(
            settings.data_directory, settings.raw_retention_days)
        click.echo("Removed %d old tmp directorie(s)." % removed)
